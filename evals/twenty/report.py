"""Schema-fixed public recomputation from a verified batch ledger.

The generator reads only the frozen protocol and the append-only ledger, both of which it
re-verifies. It emits counts, outcome tallies, action, model-call, wall-time and per-trial
cost aggregates, approval counts, token/cost completeness, safety counters, the batch stop
reason and, per preregistered comparison, the declared write boundary with paired
concordance counts. The paired counts carry no direction: they measure how often the two
arms disagree, which is the variance input `power.py` needs, not which arm won. It never
emits prompts, company data, per-trial timing, request IDs, run IDs, credentials, raw
traces, or any held-out task content, and it refuses to phrase a small qualification batch
as a comparative result.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from statistics import median
from typing import Any

from .evaluator import ARTIFACT_VERSION, EvaluationInputError, digest_json
from .records import _validate_entries, _decode, _locked_ledger, _status
from .protocol import verify_protocol


REPORT_VERSION = 1
#: Minimum sample-size policy retained for later comparative estimators. Batch purpose is an
#: explicit preregistered protocol field; completing a large scripted qualification must not
#: silently relabel it as a product benchmark.
MINIMUM_COMPARATIVE_TRIALS_PER_ARM = 30
_OUTCOMES = ("PASS", "FAIL", "INDETERMINATE", "INVALID")
_SAFETY = (
    "unauthorized_writes", "duplicate_writes", "forbidden_state_changes",
    "cross_identity_actions",
)
_AGGREGATE_FIELDS = {"count", "total", "median", "maximum"}
_ARM_FIELDS = {
    "arm_id", "engine", "engine_release", "capability_profile", "source_mode",
    "source_interface", "approval_boundary", "mutation_journal",
    "trials", "valid_trials", "apparatus_invalid_trials", "outcomes",
    "terminal_reasons", "page_actions", "business_calls", "model_calls", "wall_ms",
    "known_input_tokens", "known_output_tokens", "usage_unavailable_calls",
    "known_cost_cny", "known_cost_cny_per_trial", "total_cost_cny",
    "approvals_requested", "approvals_approved", "approvals_denied", "safety",
}
_ARM_AGGREGATES = ("page_actions", "business_calls", "model_calls", "wall_ms",
                   "known_cost_cny_per_trial")
_COMPARISON_FIELDS = {
    "comparison_id", "comparison_type", "left_arm", "right_arm", "approval_boundary",
    "eligible_tasks", "pairs_scheduled", "pairs_completed", "pairs_both_valid",
    "concordant_pairs", "discordant_pairs", "discordance_rate",
}
_REPORT_FIELDS = {
    "artifact_version", "kind", "purpose", "comparative_claim_supported",
    "protocol_id", "protocol_digest", "development_seal_digest", "test_seal_digest",
    "application_release", "browser_release", "requested_model", "scheduled_pairs",
    "expected_trials", "completed_trials", "batch_status", "stop_reason", "outcomes",
    "valid_trials", "apparatus_invalid_trials", "apparatus_codes",
    "provider_reported_models", "model_calls", "usage_unavailable_calls",
    "known_input_tokens", "known_output_tokens", "known_cost_cny", "total_cost_cny",
    "safety_events", "safety", "arms", "comparisons", "interpretation", "report_digest",
}
_INTERPRETATION = (
    "This report recomputes one batch from its verified ledger. It qualifies the "
    "execution and evaluation apparatus only. It does not establish a task success "
    "rate, an advantage or disadvantage against PageAgent, a source-retrieval effect, "
    "or a no-gain conclusion. Paired concordance counts describe how often two arms "
    "disagree on the same task; they carry no direction and exist to plan sample size. "
    "A comparison declared capability_only reports what each arm can do and cannot "
    "support a claim about write safety."
)


def _has_fields(value: Any, fields: set[str] | frozenset[str]) -> bool:
    return isinstance(value, dict) and set(value) == set(fields)


def _aggregate(values: list[int] | list[float], *, digits: int | None = None) -> dict[str, Any]:
    if not values:
        return {"count": 0, "total": 0, "median": None, "maximum": None}
    total, mid, top = sum(values), float(median(values)), max(values)
    if digits is not None:
        total, mid, top = round(total, digits), round(mid, digits), round(top, digits)
    return {"count": len(values), "total": total, "median": mid, "maximum": top}


def _comparison_summary(comparison: Mapping[str, Any], protocol: Mapping[str, Any],
                        records: list[dict[str, Any]]) -> dict[str, Any]:
    """Count paired agreement for one comparison without saying which arm passed."""
    comparison_id = comparison["comparison_id"]
    by_position: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        if record["comparison_id"] == comparison_id:
            by_position.setdefault(record["schedule_position"], []).append(record)
    completed = [pair for pair in by_position.values() if len(pair) == 2]
    both_valid = [pair for pair in completed
                  if all(record["trial_status"] == "valid" for record in pair)]
    discordant = sum(
        (pair[0]["evaluation_status"] == "PASS") != (pair[1]["evaluation_status"] == "PASS")
        for pair in both_valid)
    return {
        "comparison_id": comparison_id,
        "comparison_type": comparison["comparison_type"],
        "left_arm": comparison["left_arm"],
        "right_arm": comparison["right_arm"],
        "approval_boundary": comparison["approval_boundary"],
        "eligible_tasks": len(comparison["eligible_task_ids"]),
        "pairs_scheduled": sum(
            row["comparison_id"] == comparison_id for row in protocol["schedule"]),
        "pairs_completed": len(completed),
        "pairs_both_valid": len(both_valid),
        "concordant_pairs": len(both_valid) - discordant,
        "discordant_pairs": discordant,
        "discordance_rate": (round(discordant / len(both_valid), 6) if both_valid else None),
    }


def _arm_summary(arm: Mapping[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    selected = [record for record in records if record["arm_id"] == arm["arm_id"]]
    valid = [record for record in selected if record["trial_status"] == "valid"]
    unknown_calls = sum(record["metrics"]["usage_unavailable_calls"] for record in selected)
    known_cost = round(sum(record["metrics"]["known_cost_cny"] for record in selected), 6)
    return {
        "arm_id": arm["arm_id"],
        "engine": arm["engine"],
        "engine_release": arm["engine_release"],
        "capability_profile": arm["capability_profile"],
        "source_mode": arm["source_mode"],
        "source_interface": arm["source_interface"],
        "approval_boundary": arm["approval_boundary"],
        "mutation_journal": arm["mutation_journal"],
        "trials": len(selected),
        "valid_trials": len(valid),
        "apparatus_invalid_trials": len(selected) - len(valid),
        "outcomes": {name: sum(record["evaluation_status"] == name for record in selected)
                     for name in _OUTCOMES},
        "terminal_reasons": {
            reason: sum(record["terminal_reason"] == reason for record in selected)
            for reason in sorted({record["terminal_reason"] for record in selected})
        },
        "page_actions": _aggregate([record["metrics"]["page_actions"] for record in valid]),
        "business_calls": _aggregate([record["metrics"]["business_calls"] for record in valid]),
        "model_calls": _aggregate([record["metrics"]["model_calls"] for record in selected]),
        "wall_ms": _aggregate([record["metrics"]["wall_ms"] for record in valid]),
        "known_input_tokens": sum(record["metrics"]["known_input_tokens"] for record in selected),
        "known_output_tokens": sum(record["metrics"]["known_output_tokens"] for record in selected),
        "usage_unavailable_calls": unknown_calls,
        "known_cost_cny": known_cost,
        "known_cost_cny_per_trial": _aggregate(
            [record["metrics"]["known_cost_cny"] for record in selected], digits=6),
        "total_cost_cny": None if unknown_calls else known_cost,
        "approvals_requested": sum(
            record["metrics"]["approvals_requested"] for record in selected),
        "approvals_approved": sum(record["metrics"]["approvals_approved"] for record in selected),
        "approvals_denied": sum(record["metrics"]["approvals_denied"] for record in selected),
        "safety": {name: sum(record["safety"][name] for record in selected) for name in _SAFETY},
    }


def build_report(*, protocol: Mapping[str, Any], development_seal: Mapping[str, Any],
                 test_seal: Mapping[str, Any], split_verification: Mapping[str, Any],
                 ledger_path: str | Path) -> dict[str, Any]:
    """Recompute one schema-fixed public report from a verified ledger and frozen protocol."""
    verification = verify_protocol(
        protocol, development_seal=development_seal, test_seal=test_seal,
        split_verification=split_verification)
    with _locked_ledger(Path(ledger_path)) as handle:
        values = _decode(handle)
        header, records = _validate_entries(values, protocol, test_seal)
    status = _status(header, records)
    unknown_calls = sum(record["metrics"]["usage_unavailable_calls"] for record in records)
    known_cost = round(sum(record["metrics"]["known_cost_cny"] for record in records), 6)
    safety_events = sum(sum(record["safety"].values()) for record in records)
    stop_reason = (
        "apparatus_invalid" if status == "SEALED_INVALID"
        else "safety_event" if status == "STOPPED_SAFETY"
        else "schedule_complete" if status == "COMPLETE" else "batch_open")
    arms = [_arm_summary(arm, records)
            for arm in sorted(protocol["arms"], key=lambda item: item["arm_id"])]
    comparisons = [_comparison_summary(comparison, protocol, records)
                   for comparison in sorted(protocol["comparisons"],
                                            key=lambda item: item["comparison_id"])]
    core = {
        "artifact_version": REPORT_VERSION,
        "kind": "twenty_public_report",
        "purpose": protocol["purpose"],
        "comparative_claim_supported": False,
        "protocol_id": protocol["protocol_id"],
        "protocol_digest": protocol["protocol_digest"],
        "development_seal_digest": development_seal["seal_digest"],
        "test_seal_digest": test_seal["seal_digest"],
        "application_release": protocol["environment"]["application_release"],
        "browser_release": protocol["environment"]["browser_release"],
        "requested_model": protocol["provider"]["requested_model"],
        "scheduled_pairs": verification["scheduled_pairs"],
        "expected_trials": header["expected_trials"],
        "completed_trials": len(records),
        "batch_status": status,
        "stop_reason": stop_reason,
        "outcomes": {name: sum(record["evaluation_status"] == name for record in records)
                     for name in _OUTCOMES},
        "valid_trials": sum(record["trial_status"] == "valid" for record in records),
        "apparatus_invalid_trials": sum(
            record["trial_status"] == "apparatus_invalid" for record in records),
        "apparatus_codes": {
            code: sum(record["apparatus_code"] == code for record in records)
            for code in sorted({record["apparatus_code"] for record in records
                                if record["apparatus_code"]})
        },
        "provider_reported_models": sorted({
            model for record in records
            for model in record["metrics"]["provider_reported_models"]}),
        "model_calls": sum(record["metrics"]["model_calls"] for record in records),
        "usage_unavailable_calls": unknown_calls,
        "known_input_tokens": sum(record["metrics"]["known_input_tokens"] for record in records),
        "known_output_tokens": sum(record["metrics"]["known_output_tokens"] for record in records),
        "known_cost_cny": known_cost,
        "total_cost_cny": None if unknown_calls else known_cost,
        "safety_events": safety_events,
        "safety": {name: sum(record["safety"][name] for record in records) for name in _SAFETY},
        "arms": arms,
        "comparisons": comparisons,
        "interpretation": _INTERPRETATION,
    }
    if unknown_calls and core["total_cost_cny"] is not None:
        raise EvaluationInputError("unknown provider usage cannot report a total cost")
    return {**core, "report_digest": digest_json(core)}


def verify_report(value: Any) -> dict[str, Any]:
    """Check that a published report is canonical and still labelled non-comparative."""
    if (not isinstance(value, dict) or value.get("artifact_version") != REPORT_VERSION
            or value.get("kind") != "twenty_public_report"):
        raise EvaluationInputError("public report header is invalid")
    if set(value) != _REPORT_FIELDS:
        raise EvaluationInputError("public report has unexpected fields")
    if (value.get("purpose") not in {
            "apparatus_qualification", "development_pilot", "formal_benchmark"}
            or value.get("interpretation") != _INTERPRETATION):
        raise EvaluationInputError("public report labels are invalid")
    if not _has_fields(value.get("outcomes"), frozenset(_OUTCOMES)) \
            or not _has_fields(value.get("safety"), frozenset(_SAFETY)):
        raise EvaluationInputError("public report counters have unexpected fields")
    arms = value.get("arms")
    if not isinstance(arms, list) or any(
            not isinstance(arm, dict) or set(arm) != _ARM_FIELDS
            or not _has_fields(arm.get("outcomes"), frozenset(_OUTCOMES))
            or not _has_fields(arm.get("safety"), frozenset(_SAFETY))
            or any(not _has_fields(arm.get(name), frozenset(_AGGREGATE_FIELDS))
                   for name in _ARM_AGGREGATES)
            for arm in arms):
        raise EvaluationInputError("public report arm schema is invalid")
    comparisons = value.get("comparisons")
    if not isinstance(comparisons, list) or any(
            not _has_fields(comparison, _COMPARISON_FIELDS)
            or comparison.get("approval_boundary") not in {"equivalent", "capability_only"}
            for comparison in comparisons):
        raise EvaluationInputError("public report comparison schema is invalid")
    core = {key: value[key] for key in value if key != "report_digest"}
    if value.get("report_digest") != digest_json(core):
        raise EvaluationInputError("public report digest does not match its content")
    if value.get("comparative_claim_supported") is not False:
        raise EvaluationInputError("public report may not assert a comparative claim")
    return {
        "artifact_version": ARTIFACT_VERSION,
        "kind": "twenty_public_report_verification",
        "status": "READY",
        "purpose": value["purpose"],
        "completed_trials": value["completed_trials"],
        "report_digest": value["report_digest"],
    }
