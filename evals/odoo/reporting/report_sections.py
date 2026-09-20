"""Cross-batch comparison, safety and feedback report sections."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from evals.odoo.reporting import stats
from evals.odoo.reporting.report import (
    ANSWER_SCOPE, ARMS, COMPARISON_ARM, COMPARISON_INTERPRETATION,
    COMPARISON_SCOPE, CONFIDENCE, GATED_ARM, REPLICATES, _UNLISTED,
    _DIGEST, _ceil6, _floor6, _candidate,
)
from evals.odoo.domain import outcomes

def comparisons(batch: dict[str, Any], paired_task_ids: list[str]) -> list[dict[str, Any]]:
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
            "replicate": replicate, "scope": COMPARISON_SCOPE,
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


def answer_task_results(batch: dict[str, Any], task_ids: list[str]) -> list[dict[str, Any]]:
    """Each arm's successes on the eligible answer tasks, counted apart and never paired."""
    index = {(cell["arm"], cell["task_id"], cell["replicate"]): cell for cell in batch["cells"]}
    entries = []
    for replicate in REPLICATES:
        arms = {}
        for arm in ARMS:
            cells = [index[(arm, task_id, replicate)] for task_id in task_ids
                     if (arm, task_id, replicate) in index]
            arms[arm] = stats.proportion_summary(sum(cell["credit"] for cell in cells),
                                                 len(cells), CONFIDENCE)
        if not any(block["n"] for block in arms.values()):
            continue
        entries.append({
            "batch_id": batch["batch_id"], "purpose": batch["purpose"],
            "replicate": replicate, "scope": ANSWER_SCOPE,
            "requested_tasks": len(task_ids), "arms": arms,
            "answer_visible_to_user": {GATED_ARM: True, COMPARISON_ARM: False},
            "paired": False, "interpretation": COMPARISON_INTERPRETATION,
        })
    return entries


def safety(batches: list[dict[str, Any]]) -> dict[str, Any]:
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


def validated_feedback(feedback: Any) -> dict[str, Any] | None:
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
