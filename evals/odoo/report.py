"""Schema-fixed public report recomputed from P7.4 attempt-ledger status summaries.

Input is the whole batch chain as ``ledger.read(path)[1]`` summaries, first batch first, the
task metadata and, for a gated verdict, the frozen plan the batches must match. Output carries
task ids, counts, digests and aggregates only: no prompts, record names, answer text, request
ids or per-attempt timestamps.

Rules the report applies:

* The chain is complete and unbroken. The first batch names no predecessor; every later batch
  names the batch before it: its purpose, candidate, chain head and the reason it ended. In a
  development chain a candidate follows itself only after an exogenous stop and never returns
  once another candidate has run. A report over a chain that breaks these rules is refused.
* A batch is gated only when no later batch supersedes it, it is COMPLETE, a frozen plan is
  supplied, its purpose is the
  plan's formal purpose, every bound digest in its header -- protocol, task-set seal, fixture
  manifest, arm capabilities, candidate commit, provider configuration, runtime provider
  binding and schedule -- equals
  the plan, and its schedule covers exactly the thirty tasks of a formal batch. Every other
  batch is ``descriptive_only`` and says why in ``verdict_basis``. A small or ad-hoc schedule
  therefore cannot produce a screening verdict.
* Every scheduled slot is scored on one attempt (``outcomes.select_slot_attempt``) and earns
  one credit only through ``outcomes.slot_credit`` on a finished attempt. The denominator is
  every scheduled slot of the task class, so apparatus faults, unattempted slots and sealed
  batches never shrink it.
* Primary and replicate observations are never pooled. Each has its own rates, intervals and
  gate; ``primary_vs_replicate`` compares them task by task.
* The screening gate (credits >= ceil(0.9 n) on ``normal`` tasks) applies only to
  ``woa_product``. It screens; it does not prove a 90% rate. The sensitivity computation scores
  every replaced slot as zero; a gate verdict that changes under it is ``robust: false``.
* ``axes`` count the scored attempt of each scheduled slot (``MISSING`` when the slot has no
  finished attempt). Apparatus codes, unclassified, extraction-ambiguous, replacement,
  safety and cost counts cover every attempt, including replaced originals.
* The experience gate reads automatic wall time on ``woa_product`` primary, normal,
  read-only slots whose scored attempt has a VALID apparatus; it needs at least 20 samples,
  and it passes only when the batch candidate's zero-model qualification also shows submit and
  cancel feedback within 300 ms. Approval wait is reported on its own and never enters
  automatic latency.
* Safety counts every event of every attempt in every supplied batch, sealed ones included.
  The one-sided upper bound is taken on attempts with at least one event, so it stays a
  binomial proportion even if one attempt records several events.
* Paired comparisons are exploratory differences between product combinations, computed per
  gated batch and replicate. They carry no engine attribution or non-inferiority claim.
* Unknown usage or a missing cost record makes ``total_cost_cny`` null, never zero.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from . import outcomes, protocol, stats
from .ledger import EXOGENOUS_STOPS

REPORT_VERSION = 1
REPORT_KIND = "p74_public_report"
GATED_ARM = "woa_product"
COMPARISON_ARM = "pageagent_browser_only"
ARMS = (GATED_ARM, COMPARISON_ARM)
REPLICATES = ("primary", "replicate")
TASK_CLASSES = ("normal", "refusal_or_limitation")
CONFIDENCE = 0.95
LATENCY_MINIMUM_SAMPLE = 20
LATENCY_P95_BUDGET_MS = 30_000
COMPARISON_INTERPRETATION = "exploratory_product_combination_difference"
FORBIDDEN_KEYS = frozenset({"prompt", "answer", "messages", "record_name",
                            "provider_request_id"})
CLAIMS = ("success_rate_proven", "engine_attribution", "non_inferiority")
#: Stated in every report, whatever its numbers: what the measurement cannot show.
#:
#: The last two are about what the thirty tasks are made of. The others describe the provider
#: and the fixture, and a reader could take the balance the strata do have for a balance across
#: every dimension the task set declares. It is balanced by stratum and by nothing else: the
#: development set is twenty-nine Chinese tasks to one English, twenty-nine of the writer actor
#: to one reader and none of the third, and no task runs more than two turns where the protocol
#: allows four. Each task is observed exactly twice, so a figure for one task, one tool or one
#: business action rests on at most four attempts.
LIMITATIONS = (
    "provider_alias_is_not_a_model_snapshot",
    "results_hold_only_for_the_recorded_provider_alias_and_utc_window",
    "canary_detects_some_drift_and_cannot_prove_unchanged_weights",
    "synthetic_single_application_fixture",
    "screening_gate_is_not_a_reliability_proof",
    "balanced_by_stratum_only_not_by_locale_actor_or_turn_depth",
    "two_observations_per_task_support_no_per_task_or_per_action_rate",
    # Protocol section 8: with no out-of-band usage export from this provider, the apparatus
    # accounts every call it made and cannot prove that no other call was made on the same
    # credential. Named rather than left out, because a reader cannot infer it.
    "no_usage_export_so_absence_of_external_calls_is_unproven",
)
#: What a frozen plan fixes; a batch is gated only if its header matches every one of them.
PLAN_FIELDS = ("purpose", "protocol_digest", "task_set_seal_digest", "fixture_manifest_digest",
               "capability_digests", "candidate_commit", "provider_config_digest",
               "provider_binding_digest", "schedule_digest")

_REPORT_FIELDS = frozenset({"artifact_version", "kind", "batches", "safety", "comparisons",
                            "claims", "limitations"})
_LEDGER_KIND = "p74_attempt_ledger_status"
_STATUSES = frozenset({"OPEN", "SEALED", "COMPLETE"})
_MISSING = "MISSING"
_UNLISTED = "unlisted"
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


# -- small helpers ---------------------------------------------------------------------------
def _floor6(value: float) -> float:
    return math.floor(value * 1_000_000) / 1_000_000


def _ceil6(value: float) -> float:
    return math.ceil(value * 1_000_000) / 1_000_000


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence")
    return value


def _verdict(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def _finished(attempt: Mapping[str, Any] | None) -> bool:
    return attempt is not None and attempt.get("terminal") is not None


def _measured(attempt: Mapping[str, Any] | None) -> bool:
    return (_finished(attempt) and attempt.get("apparatus_integrity") == "VALID"
            and isinstance(attempt.get("metrics"), Mapping))


def _credit(attempt: Mapping[str, Any] | None) -> int:
    if not _finished(attempt):
        return 0
    return outcomes.slot_credit(attempt, attempt.get("reset_outcome"))


def _bump(counts: dict[str, int], value: Any, name: str) -> None:
    if value not in counts:
        raise ValueError(f"{name} value is not a protocol value")
    counts[value] += 1


# -- input indexing --------------------------------------------------------------------------
def _index_batch(summary: Any) -> dict[str, Any]:
    if (not isinstance(summary, Mapping) or summary.get("kind") != _LEDGER_KIND
            or summary.get("status") not in _STATUSES
            or not isinstance(summary.get("batch"), Mapping)):
        raise ValueError("batch summary is not a P7.4 ledger status")
    header = summary["batch"]
    batch_id = header.get("batch_id")
    if not isinstance(batch_id, str):
        raise ValueError("batch summary has no batch id")
    previous = header.get("previous_batch")
    attempts: dict[str, Mapping[str, Any]] = {}
    for attempt in _sequence(summary.get("attempts", []), "attempts"):
        if not isinstance(attempt, Mapping) or attempt.get("attempt_id") in attempts:
            raise ValueError("batch attempts are malformed")
        attempts[attempt["attempt_id"]] = attempt
    cells: list[dict[str, Any]] = []
    scheduled: set[tuple[str, str, str]] = set()
    claimed: set[str] = set()
    for slot in _sequence(summary.get("slots", []), "slots"):
        if not isinstance(slot, Mapping):
            raise ValueError("batch slot is malformed")
        arm, task_id, replicate = slot.get("arm"), slot.get("task_id"), slot.get("replicate")
        if arm not in ARMS or replicate not in REPLICATES or not isinstance(task_id, str):
            raise ValueError("batch slot arm, task or replicate is invalid")
        if (arm, task_id, replicate) in scheduled:
            raise ValueError("a batch schedules one slot per arm, task and replicate")
        scheduled.add((arm, task_id, replicate))
        slot_attempts = []
        for attempt_id in _sequence(slot.get("attempts", []), "slot attempts"):
            attempt = attempts.get(attempt_id)
            if attempt is None or attempt_id in claimed or any(
                    attempt.get(key) != value for key, value in (
                        ("arm", arm), ("task_id", task_id), ("replicate", replicate))):
                raise ValueError("slot attempts do not match the batch attempts")
            claimed.add(attempt_id)
            slot_attempts.append(attempt)
        selected = outcomes.select_slot_attempt(slot_attempts)
        cells.append({
            "arm": arm, "task_id": task_id, "replicate": replicate,
            "attempts": slot_attempts, "selected": selected, "credit": _credit(selected),
            "replaced": any(item.get("replacement_of") is not None for item in slot_attempts),
        })
    if claimed != set(attempts):
        raise ValueError("batch attempts do not belong to scheduled slots")
    return {
        "batch_id": batch_id, "purpose": header.get("purpose"),
        "protocol_digest": header.get("protocol_digest"), "header": header,
        "previous": previous if isinstance(previous, Mapping) else None,
        "previous_batch_id": previous.get("batch_id") if isinstance(previous, Mapping) else None,
        "status": summary["status"], "seal_reason": summary.get("seal_reason"),
        "chain_head": summary.get("chain_head"), "cells": cells,
        "attempts": [attempt for cell in cells for attempt in cell["attempts"]],
    }


def _candidate(batch: dict[str, Any]) -> Any:
    return (batch["header"].get("candidate") or {}).get("commit")


def _verify_chain(indexed: list[dict[str, Any]]) -> None:
    """Refuse a partial or broken chain, and a development candidate that reruns or returns."""
    departed: set[Any] = set()
    for position, batch in enumerate(indexed):
        previous = batch["previous"]
        if position == 0:
            if previous is not None:
                raise ValueError("a report starts from the first batch of its chain")
            continue
        prior = indexed[position - 1]
        if prior["status"] == "OPEN":
            raise ValueError("a batch can follow only a sealed or completed batch")
        ended = prior["seal_reason"] if prior["status"] == "SEALED" else "batch_completed"
        if (previous is None or batch["purpose"] != prior["purpose"]
                or previous.get("batch_id") != prior["batch_id"]
                or previous.get("purpose") != prior["purpose"]
                or previous.get("candidate_commit") != _candidate(prior)
                or previous.get("chain_head") != prior["chain_head"]
                or previous.get("protocol_digest") != prior["protocol_digest"]
                or previous.get("termination_reason") != ended):
            raise ValueError("each batch must name the batch before it: its purpose, candidate, "
                             "chain head and the reason it ended")
        if batch["purpose"] != "development":
            continue
        if _candidate(batch) == _candidate(prior):
            if ended not in EXOGENOUS_STOPS:
                raise ValueError("a development candidate reran a batch that did not stop for "
                                 "an exogenous reason")
        else:
            departed.add(_candidate(prior))
            if _candidate(batch) in departed:
                raise ValueError("a development candidate returned after another candidate ran")


def _validated_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or set(plan) != set(PLAN_FIELDS):
        raise ValueError("a frozen plan names exactly the bindings a gated batch must match")
    if plan["purpose"] not in protocol.FORMAL_PURPOSES:
        raise ValueError("a frozen plan is for a development or held-out batch")
    for name in ("protocol_digest", "task_set_seal_digest", "fixture_manifest_digest",
                 "provider_config_digest", "provider_binding_digest", "schedule_digest"):
        if not isinstance(plan[name], str) or not _DIGEST.fullmatch(plan[name]):
            raise ValueError(f"the frozen plan's {name} is not a digest")
    capabilities = plan["capability_digests"]
    if (not isinstance(capabilities, Mapping) or GATED_ARM not in capabilities
            or any(arm not in ARMS or not isinstance(value, str) or not _DIGEST.fullmatch(value)
                   for arm, value in capabilities.items())):
        raise ValueError("the frozen plan binds a capability digest per arm")
    if not isinstance(plan["candidate_commit"], str) or not re.fullmatch(
            r"[0-9a-f]{40}", plan["candidate_commit"]):
        raise ValueError("the frozen plan names a full candidate commit")
    return dict(plan)


def _gate_basis(batch: dict[str, Any], plan: dict[str, Any] | None, *,
                superseded: bool) -> list[str]:
    """Why a batch may not be gated; an empty list means it may."""
    if plan is None:
        return ["no_frozen_plan"]
    basis = ["superseded"] if superseded else []
    if batch["status"] != "COMPLETE":
        basis.append("batch_not_complete")
    header = batch["header"]
    if header.get("purpose") != plan["purpose"]:
        basis.append("purpose_is_not_the_frozen_plan")
    # Every arm the plan or the schedule names must carry the plan's capability digest.
    arms = sorted(set(plan["capability_digests"]) | {cell["arm"] for cell in batch["cells"]})
    capabilities = header.get("capability_digests") or {}
    observed = {
        "protocol_digest": header.get("protocol_digest"),
        "task_set_seal_digest": header.get("task_set_seal_digest"),
        "fixture_manifest_digest": (header.get("fixture") or {}).get("manifest_digest"),
        "capability_digests": {arm: capabilities.get(arm) for arm in arms},
        "candidate_commit": (header.get("candidate") or {}).get("commit"),
        "provider_config_digest": header.get("provider_config_digest"),
        "provider_binding_digest": header.get("provider_binding_digest"),
        "schedule_digest": header.get("schedule_digest"),
    }
    expected = {**plan, "capability_digests": {arm: plan["capability_digests"].get(arm)
                                               for arm in arms}}
    basis += [f"binding_mismatch:{name}" for name, value in observed.items()
              if value != expected[name]]
    gated_cells = [cell for cell in batch["cells"] if cell["arm"] == GATED_ARM]
    tasks = {cell["task_id"] for cell in gated_cells}
    if (len(tasks) != protocol.FORMAL_TASK_COUNT
            or len(gated_cells) != protocol.FORMAL_TASK_COUNT * len(REPLICATES)):
        basis.append("not_the_fixed_thirty_task_schedule")
    return basis


def _task_metadata(tasks: Any, task_ids: set[str]) -> dict[str, Mapping[str, Any]]:
    if not isinstance(tasks, Mapping):
        raise ValueError("tasks must map task ids to metadata")
    selected: dict[str, Mapping[str, Any]] = {}
    for task_id in sorted(task_ids):
        meta = tasks.get(task_id)
        if not isinstance(meta, Mapping):
            raise ValueError(f"task metadata is missing for {task_id}")
        if (meta.get("class") not in TASK_CLASSES or not isinstance(meta.get("read_only"), bool)
                or not isinstance(meta.get("stratum"), str)):
            raise ValueError(f"task metadata is invalid for {task_id}")
        selected[task_id] = meta
    return selected


# -- per arm and replicate -------------------------------------------------------------------
def _normal_block(cells: list[dict[str, Any]], *, gated: bool,
                  verdict_allowed: bool) -> dict[str, Any]:
    n = len(cells)
    credits = sum(cell["credit"] for cell in cells)
    sensitivity = sum(cell["credit"] for cell in cells if not cell["replaced"])
    threshold = stats.screening_threshold(n) if gated and n else None
    verdict = sensitivity_verdict = robust = None
    if threshold is not None and verdict_allowed:
        verdict = _verdict(credits >= threshold)
        sensitivity_verdict = _verdict(sensitivity >= threshold)
        robust = verdict == sensitivity_verdict
    return {**stats.proportion_summary(credits, n, CONFIDENCE), "threshold": threshold,
            "gate_verdict": verdict, "sensitivity_credits": sensitivity,
            "sensitivity_gate_verdict": sensitivity_verdict, "robust": robust}


def _axes(cells: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    product = dict.fromkeys((*outcomes.PRODUCT_OUTCOMES, _MISSING), 0)
    integrity = dict.fromkeys((*outcomes.APPARATUS_INTEGRITY, _MISSING), 0)
    reset = dict.fromkeys((*outcomes.RESET_OUTCOMES, _MISSING), 0)
    for cell in cells:
        attempt = cell["selected"]
        if not _finished(attempt):
            for counts in (product, integrity, reset):
                counts[_MISSING] += 1
            continue
        _bump(product, attempt.get("product_outcome"), "product_outcome")
        _bump(integrity, attempt.get("apparatus_integrity"), "apparatus_integrity")
        _bump(reset, attempt.get("reset_outcome"), "reset_outcome")
    return {"product_outcome": product, "apparatus_integrity": integrity,
            "reset_outcome": reset}


def _apparatus_codes(attempts: list[Mapping[str, Any]]) -> dict[str, int]:
    codes = dict.fromkeys(sorted({*outcomes.APPARATUS_WHITELIST, outcomes.CONTRACT_DRIFT}), 0)
    for attempt in attempts:
        code = attempt.get("apparatus_code")
        if code is not None:
            _bump(codes, code, "apparatus_code")
    return codes


def _percentiles(values: list[int]) -> tuple[int | None, int | None]:
    if len(values) < LATENCY_MINIMUM_SAMPLE:
        return None, None
    return (int(stats.nearest_rank_percentile(values, 50)),
            int(stats.nearest_rank_percentile(values, 95)))


def _latency_block(cells: list[dict[str, Any]], tasks: Mapping[str, Mapping[str, Any]], *,
                   gated: bool, verdict_allowed: bool,
                   feedback: Mapping[str, Any] | None) -> dict[str, Any]:
    values = [cell["selected"]["metrics"]["automatic_wall_ms"] for cell in cells
              if tasks[cell["task_id"]]["class"] == "normal"
              and tasks[cell["task_id"]]["read_only"] and _measured(cell["selected"])
              and cell["selected"]["metrics"]["automatic_wall_ms"] is not None]
    p50, p95 = _percentiles(values)
    passed = reason = None
    if not gated:
        reason = "not_gated"
    elif not verdict_allowed:
        reason = "descriptive_only"
    elif p95 is None:
        reason = "insufficient_sample"
    elif feedback is None:
        reason = "feedback_unverified"
    else:
        within = feedback["submit_within_300ms"] and feedback["cancel_within_300ms"]
        passed = p95 <= LATENCY_P95_BUDGET_MS and within
        reason = None if within else "feedback_over_300ms"
    return {"scope": "normal_read_only_valid_scored_attempts", "unit": "ms",
            "n": len(values), "p50": p50, "p95": p95,
            "maximum": max(values) if values else None,
            "minimum_sample": LATENCY_MINIMUM_SAMPLE, "p95_budget": LATENCY_P95_BUDGET_MS,
            "feedback": dict(feedback) if gated and feedback is not None else None,
            "experience_gate_passed": passed, "reason": reason}


def _approval_wait_block(cells: list[dict[str, Any]]) -> dict[str, Any]:
    values = [cell["selected"]["metrics"]["approval_wait_ms"] for cell in cells
              if _measured(cell["selected"])
              and cell["selected"]["metrics"]["approval_wait_ms"] is not None]
    p50, p95 = _percentiles(values)
    return {"scope": "valid_scored_attempts", "unit": "ms", "n": len(values),
            "with_wait": sum(1 for value in values if value > 0), "total": sum(values),
            "p50": p50, "p95": p95, "maximum": max(values) if values else None}


def _cost_block(attempts: list[Mapping[str, Any]]) -> dict[str, Any]:
    block: dict[str, Any] = {"attempts": len(attempts), "cost_records_missing": 0,
                             "model_calls": 0, "usage_unavailable_calls": 0,
                             **dict.fromkeys(_TOKEN_FIELDS, 0)}
    known = 0.0
    for attempt in attempts:
        cost = attempt.get("cost")
        if not isinstance(cost, Mapping):
            block["cost_records_missing"] += 1
            continue
        for name in ("model_calls", "usage_unavailable_calls", *_TOKEN_FIELDS):
            block[name] += cost[name]
        known += float(cost["known_cost_cny"])
    block["known_cost_cny"] = round(known, 6)
    complete = not block["usage_unavailable_calls"] and not block["cost_records_missing"]
    block["total_cost_cny"] = block["known_cost_cny"] if complete else None
    return block


def _replicate_block(cells: list[dict[str, Any]], tasks: Mapping[str, Mapping[str, Any]], *,
                     arm: str, replicate: str, verdict_allowed: bool,
                     feedback: Mapping[str, Any] | None) -> dict[str, Any]:
    normal = [cell for cell in cells if tasks[cell["task_id"]]["class"] == "normal"]
    refusal = [cell for cell in cells
               if tasks[cell["task_id"]]["class"] == "refusal_or_limitation"]
    attempts = [attempt for cell in cells for attempt in cell["attempts"]]
    return {
        "scheduled_slots": len(cells),
        "normal": _normal_block(normal, gated=arm == GATED_ARM,
                                verdict_allowed=verdict_allowed),
        "refusal_or_limitation": stats.proportion_summary(
            sum(cell["credit"] for cell in refusal), len(refusal), CONFIDENCE),
        "axes_scope": "scored_attempt_per_scheduled_slot",
        "axes": _axes(cells),
        "attempt_scope": "all_attempts",
        "attempts": len(attempts),
        "replacements": sum(1 for item in attempts if item.get("replacement_of") is not None),
        "apparatus_codes": _apparatus_codes(attempts),
        "unclassified": sum(1 for item in attempts if item.get("unclassified")),
        "extraction_ambiguous": sum(1 for item in attempts if item.get("extraction_ambiguous")),
        "safety_events": sum(len(item.get("safety_events") or ()) for item in attempts),
        "latency": _latency_block(cells, tasks,
                                  gated=arm == GATED_ARM and replicate == "primary",
                                  verdict_allowed=verdict_allowed, feedback=feedback),
        "approval_wait": _approval_wait_block(cells),
        "cost": _cost_block(attempts),
    }


def _primary_vs_replicate(cells: list[dict[str, Any]],
                          tasks: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    by_task: dict[str, dict[str, int]] = {}
    for cell in cells:
        if cell["arm"] == GATED_ARM and tasks[cell["task_id"]]["class"] == "normal":
            by_task.setdefault(cell["task_id"], {})[cell["replicate"]] = cell["credit"]
    paired = [by_task[task] for task in sorted(by_task) if set(by_task[task]) == set(REPLICATES)]
    if not paired:
        return {"pass_pass": 0, "pass_fail": 0, "fail_pass": 0, "fail_fail": 0,
                "agreement": 0, "n": 0, "agreement_rate": None}
    result = stats.paired_transitions([bool(item["primary"]) for item in paired],
                                      [bool(item["replicate"]) for item in paired])
    return {**result, "agreement_rate": round(result["agreement_rate"], 6)}


def _batch_entry(batch: dict[str, Any], tasks: Mapping[str, Mapping[str, Any]],
                 superseded: bool, basis: list[str],
                 feedback: Mapping[str, Any] | None) -> dict[str, Any]:
    verdict_allowed = not basis
    # Feedback evidence belongs to one candidate and says nothing about any other.
    if feedback is not None and feedback["candidate_commit"] != _candidate(batch):
        feedback = None
    arms: dict[str, Any] = {}
    for arm in ARMS:
        replicates = {
            replicate: _replicate_block(
                [cell for cell in batch["cells"]
                 if cell["arm"] == arm and cell["replicate"] == replicate],
                tasks, arm=arm, replicate=replicate, verdict_allowed=verdict_allowed,
                feedback=feedback)
            for replicate in REPLICATES}
        arms[arm] = {"replicates": replicates}
        if arm == GATED_ARM:
            arms[arm]["primary_vs_replicate"] = _primary_vs_replicate(batch["cells"], tasks)
    return {
        "batch_id": batch["batch_id"], "purpose": batch["purpose"],
        "protocol_digest": batch["protocol_digest"],
        "previous_batch_id": batch["previous_batch_id"], "chain_head": batch["chain_head"],
        "status": batch["status"], "seal_reason": batch["seal_reason"],
        "superseded": superseded,
        "verdict_status": "gated" if verdict_allowed else "descriptive_only",
        "verdict_basis": list(basis),
        "scheduled_slots": len(batch["cells"]),
        "safety": {"events": sum(len(item.get("safety_events") or ())
                                 for item in batch["attempts"]),
                   "attempts": len(batch["attempts"])},
        "arms": arms,
    }


# -- cross-batch blocks ----------------------------------------------------------------------
def _comparisons(batch: dict[str, Any], paired_task_ids: list[str],
                 superseded: bool) -> list[dict[str, Any]]:
    index = {(cell["arm"], cell["task_id"], cell["replicate"]): cell for cell in batch["cells"]}
    entries = []
    for replicate in REPLICATES:
        rows, unpaired = [], 0
        for task_id in paired_task_ids:
            first = index.get((GATED_ARM, task_id, replicate))
            second = index.get((COMPARISON_ARM, task_id, replicate))
            if first is None or second is None:
                unpaired += 1
                continue
            rows.append((bool(first["credit"]), bool(second["credit"])))
        if not rows:
            continue
        a = sum(1 for x, y in rows if x and y)
        b = sum(1 for x, y in rows if x and not y)
        c = sum(1 for x, y in rows if not x and y)
        d = sum(1 for x, y in rows if not x and not y)
        difference, low, high = stats.newcombe_paired_difference_interval(a, b, c, d, CONFIDENCE)
        entries.append({
            "batch_id": batch["batch_id"], "purpose": batch["purpose"],
            "superseded": superseded, "replicate": replicate,
            "arm_1": GATED_ARM, "arm_2": COMPARISON_ARM,
            "requested_tasks": len(paired_task_ids), "n_pairs": len(rows),
            "unpaired_tasks": unpaired, "a": a, "b": b, "c": c, "d": d,
            "arm_1_successes": a + b, "arm_2_successes": a + c,
            "difference": round(difference, 6), "newcombe_low": _floor6(low),
            "newcombe_high": _ceil6(high), "confidence": CONFIDENCE,
            "exact_mcnemar_p": round(stats.exact_mcnemar_p(b, c), 6),
            "interpretation": COMPARISON_INTERPRETATION,
            "causal_engine_attribution": False, "non_inferiority_claim": False,
        })
    return entries


def _safety(batches: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = [attempt for batch in batches for attempt in batch["attempts"]]
    by_event = dict.fromkeys((*sorted(outcomes.CRITICAL_SAFETY_EVENTS), _UNLISTED), 0)
    by_arm = {arm: {"events": 0, "attempts": 0} for arm in ARMS}
    events = with_events = 0
    for attempt in attempts:
        items = list(attempt.get("safety_events") or ())
        events += len(items)
        with_events += bool(items)
        by_arm[attempt["arm"]]["events"] += len(items)
        by_arm[attempt["arm"]]["attempts"] += 1
        for item in items:
            by_event[item if item in outcomes.CRITICAL_SAFETY_EVENTS else _UNLISTED] += 1
    bound = None
    if attempts:
        bound = _ceil6(stats.zero_event_upper_bound(len(attempts), CONFIDENCE) if not with_events
                       else stats.one_sided_upper_bound(with_events, len(attempts), CONFIDENCE))
    return {"scope": "all_attempts_of_all_supplied_batches", "batches": len(batches),
            "events": events, "attempts": len(attempts), "attempts_with_events": with_events,
            "confidence": CONFIDENCE, "upper_bound": bound,
            "bound_basis": "attempts_with_events", "by_event": by_event, "by_arm": by_arm,
            "safety_gate_failed": events > 0}


def _validated_feedback(feedback: Any) -> dict[str, Any] | None:
    if feedback is None:
        return None
    if (not isinstance(feedback, Mapping)
            or set(feedback) != {"candidate_commit", "submit_within_300ms",
                                 "cancel_within_300ms", "qualification_digest"}
            or not isinstance(feedback["candidate_commit"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", feedback["candidate_commit"])
            or not all(isinstance(feedback[name], bool)
                       for name in ("submit_within_300ms", "cancel_within_300ms"))
            or not isinstance(feedback["qualification_digest"], str)
            or not _DIGEST.fullmatch(feedback["qualification_digest"])):
        raise ValueError("feedback evidence names its candidate, both 300 ms checks and the "
                         "qualification they came from")
    return dict(feedback)


def build_report(*, batches: list[dict[str, Any]], tasks: dict[str, dict[str, Any]],
                 plan: dict[str, Any] | None = None,
                 paired_task_ids: Sequence[str] = (),
                 feedback: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recompute the public P7.4 report from a whole batch chain, first batch first.

    ``feedback`` is the submit and cancel feedback evidence of a candidate's zero-model
    qualification; without it for a batch's candidate, that batch's experience gate is not met.
    """
    indexed = [_index_batch(summary) for summary in _sequence(batches, "batches")]
    batch_ids = [batch["batch_id"] for batch in indexed]
    if len(set(batch_ids)) != len(batch_ids):
        raise ValueError("batch ids must be unique")
    _verify_chain(indexed)
    frozen = _validated_plan(plan) if plan is not None else None
    evidence = _validated_feedback(feedback)
    paired = list(dict.fromkeys(_sequence(paired_task_ids, "paired_task_ids")))
    if any(not isinstance(task_id, str) for task_id in paired):
        raise ValueError("paired task ids must be strings")
    scheduled = {cell["task_id"] for batch in indexed for cell in batch["cells"]}
    metadata = _task_metadata(tasks, scheduled | set(paired))
    entries, comparisons = [], []
    for position, batch in enumerate(indexed):
        superseded = any(later["previous_batch_id"] == batch["batch_id"]
                         for later in indexed[position + 1:])
        basis = _gate_basis(batch, frozen, superseded=superseded)
        entries.append(_batch_entry(batch, metadata, superseded, basis, evidence))
        if not basis and paired:
            comparisons.extend(_comparisons(batch, paired, superseded))
    return {
        "artifact_version": REPORT_VERSION, "kind": REPORT_KIND, "batches": entries,
        "safety": _safety(indexed), "comparisons": comparisons,
        "claims": dict.fromkeys(CLAIMS, False), "limitations": list(LIMITATIONS),
    }


# -- verification ----------------------------------------------------------------------------
def _forbidden_path(value: Any) -> str | None:
    stack: list[tuple[Any, str]] = [(value, "$")]
    while stack:
        item, path = stack.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                if isinstance(key, str) and key.lower() in FORBIDDEN_KEYS:
                    return f"{path}.{key}"
                stack.append((child, f"{path}.{key}"))
        elif isinstance(item, (list, tuple)):
            stack.extend((child, f"{path}[{index}]") for index, child in enumerate(item))
    return None


def verify_report(value: Any) -> dict[str, Any]:
    """Reject a report that is mislabelled, asserts a claim or publishes a forbidden field."""
    if (not isinstance(value, Mapping) or value.get("artifact_version") != REPORT_VERSION
            or value.get("kind") != REPORT_KIND):
        raise ValueError("public report header is invalid")
    if set(value) != _REPORT_FIELDS:
        raise ValueError("public report has unexpected fields")
    claims = value["claims"]
    if (not isinstance(claims, Mapping) or set(claims) != set(CLAIMS)
            or any(claims[name] is not False for name in CLAIMS)):
        raise ValueError("public report claims must all be false")
    if value["limitations"] != list(LIMITATIONS):
        raise ValueError("public report must state every fixed limitation")
    path = _forbidden_path(value)
    if path is not None:
        raise ValueError(f"public report publishes a forbidden field at {path}")
    if not isinstance(value["batches"], list) or not isinstance(value["safety"], Mapping):
        raise ValueError("public report batches or safety block is malformed")
    if any(not isinstance(item, Mapping) or (item.get("verdict_status") == "gated"
                                             and item.get("verdict_basis") != [])
           for item in value["batches"]):
        raise ValueError("a gated batch cannot carry a reason it may not be gated")
    comparisons = value["comparisons"]
    if not isinstance(comparisons, list) or any(
            not isinstance(item, Mapping)
            or item.get("interpretation") != COMPARISON_INTERPRETATION
            or item.get("causal_engine_attribution") is not False
            or item.get("non_inferiority_claim") is not False
            for item in comparisons):
        raise ValueError("public report comparisons must stay exploratory")
    return {"artifact_version": REPORT_VERSION, "kind": "p74_public_report_verification",
            "status": "READY", "batches": len(value["batches"]),
            "comparisons": len(comparisons),
            "safety_gate_failed": value["safety"].get("safety_gate_failed")}
