"""Schema-fixed public recomputation from a verified batch ledger.

The generator reads only the frozen protocol and the append-only ledger, both of which it
re-verifies. It emits counts, outcome tallies, action and model-call aggregates, token/cost
completeness, safety counters and the batch stop reason. It never emits prompts, company
data, per-trial timing, request IDs, run IDs, credentials, raw traces, or any held-out task
content, and it refuses to phrase a small qualification batch as a comparative result.
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
#: A batch this small qualifies the apparatus. It cannot support a success rate, a comparison
#: between engines, or a source-retrieval conclusion, so the report labels itself.
MINIMUM_COMPARATIVE_TRIALS_PER_ARM = 30
_OUTCOMES = ("PASS", "FAIL", "INDETERMINATE", "INVALID")
_SAFETY = (
    "unauthorized_writes", "duplicate_writes", "forbidden_state_changes",
    "cross_identity_actions",
)


def _purpose(records: list[dict[str, Any]], expected: int) -> str:
    """Name what this batch can support, never what it proves about an engine."""
    if not records:
        return "apparatus_qualification"
    per_arm: dict[str, int] = {}
    for record in records:
        per_arm[record["arm_id"]] = per_arm.get(record["arm_id"], 0) + 1
    if len(records) < expected:
        return "apparatus_qualification"
    if min(per_arm.values()) < MINIMUM_COMPARATIVE_TRIALS_PER_ARM:
        return "development_pilot"
    return "formal_benchmark"


def _aggregate(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "total": 0, "median": None, "maximum": None}
    return {
        "count": len(values),
        "total": sum(values),
        "median": float(median(values)),
        "maximum": max(values),
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
        "known_input_tokens": sum(record["metrics"]["known_input_tokens"] for record in selected),
        "known_output_tokens": sum(record["metrics"]["known_output_tokens"] for record in selected),
        "usage_unavailable_calls": unknown_calls,
        "known_cost_cny": known_cost,
        "total_cost_cny": None if unknown_calls else known_cost,
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
    core = {
        "artifact_version": REPORT_VERSION,
        "kind": "twenty_public_report",
        "purpose": _purpose(records, header["expected_trials"]),
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
        "interpretation": (
            "This report recomputes one batch from its verified ledger. It qualifies the "
            "execution and evaluation apparatus only. It does not establish a task success "
            "rate, an advantage or disadvantage against PageAgent, a source-retrieval effect, "
            "or a no-gain conclusion."
        ),
    }
    if unknown_calls and core["total_cost_cny"] is not None:
        raise EvaluationInputError("unknown provider usage cannot report a total cost")
    return {**core, "report_digest": digest_json(core)}


def verify_report(value: Any) -> dict[str, Any]:
    """Check that a published report is canonical and still labelled non-comparative."""
    if (not isinstance(value, dict) or value.get("artifact_version") != REPORT_VERSION
            or value.get("kind") != "twenty_public_report"):
        raise EvaluationInputError("public report header is invalid")
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
