"""Projection of replay state into the public ledger status summary."""
from __future__ import annotations

from typing import Any

from evals.odoo.reporting.validation import _SEAL_PRIORITY

def summarize(state) -> dict[str, Any]:
    status = "OPEN"
    if state.terminal is not None:
        status = "COMPLETE" if state.terminal["kind"] == "batch_completed" else "SEALED"
    attempts = []
    for attempt_id in state.attempt_order:
        item = state.attempts[attempt_id]
        outcome = item["outcome"] or {}
        attempts.append({
            "attempt_id": attempt_id, "slot_id": item["slot_id"], "arm": item["arm"],
            "task_id": item["task_id"], "replicate": item["replicate"],
            "replacement_of": item["replacement_of"], "terminal": item["terminal"],
            "product_outcome": outcome.get("product_outcome"),
            "apparatus_integrity": outcome.get("apparatus_integrity"),
            "apparatus_code": outcome.get("apparatus_code"),
            "unclassified": outcome.get("unclassified"),
            "safety_events": outcome.get("safety_events") or [],
            "extraction_ambiguous": outcome.get("extraction_ambiguous"),
            "reset_outcome": item["reset"], "cost": item["cost"],
            "metrics": item["metrics"],
            "replacement_eligible": item["replacement_eligible"],
        })
    return {
        "artifact_version": state.version,
        "kind": "p74_attempt_ledger_status",
        "status": status,
        #: What this batch may do next, from what the ledger holds. Every rule below was
        #: read as its own condition in three places -- the driver, the entry and the
        #: harness -- and the defects that caused were combinations of those conditions no
        #: single place enumerated, not rules anyone had left out. An
        #: operator's stop is deliberately not here: it lives outside the ledger, and a
        #: caller weighs it against this.
        "disposition": state._disposition(status),
        "seal_reason": state.terminal["reason"] if state.terminal else None,
        "pending_seal": state.pending_seal,
        #: The calls this batch holds for. While any is listed it takes no new attempt,
        #: forwards no new call and cannot complete; a reconciliation of the bill lifts it.
        "cost_unknown": sorted(state.cost_unknown),
        # Every reason met, most serious first: the batch seals for the first of them and
        # the rest are kept rather than lost to whichever arrived first.
        "seal_reasons": sorted(state.seal_reasons, key=_SEAL_PRIORITY.index),
        "batch": {key: state.header[key] for key in (
            "batch_id", "protocol_id", "protocol_digest", "schedule_digest", "purpose",
            "provider_config_digest", "task_set_seal_digest", "fixture",
            "capability_digests", "candidate", "schedule_seed",
            "provider_binding_digest")} if state.header else None,
        "slots": [{**{key: slot[key] for key in ("slot_id", "arm", "task_id",
                                                   "replicate")},
                   "attempts": list(slot["attempts"])}
                  for slot in (state.slots[item] for item in state.slot_order)],
        "attempts": attempts,
        "open_attempt": state.open_attempt,
        "provider_model": state.batch_model,
        "replacements": dict(state.replacements_by_arm),
        "chain_head": state.head,
    }
