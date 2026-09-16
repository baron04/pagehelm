"""Common trace projection for both arms.

The two engines count page work differently -- WOA's audit ledger records typed tool calls
and observations, PageAgent's bridge counts executed DOM tools -- so no engine-specific
counter is used as a shared hard budget. Instead both traces are projected onto one small,
content-free schema that is reported, never enforced: page actions, invalid page actions,
business calls, repeated observations, approvals, the furthest mutation dispatch state and the
engine terminal mapped onto the protocol's product terminals.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

PAGE_ACTION_TOOLS = frozenset({
    "goto", "click", "fill", "select", "submit", "back", "open_skill_route", "enter_skill_app",
    "enter_application", "open_source_route", "open_source_candidate", "prefill_form",
})
AGENT_TOOLS = frozenset({"observe", "wait_for", "finish", "ask_user", "compute", "load_skill",
                         "lookup_source"})
#: WOA terminal reasons that are ordinary product outcomes. Anything else passes through
#: unmapped so the outcome classifier can decide whether evidence explains it.
WOA_TERMINALS = {
    "agent_finished": "completed", "agent_refused": "agent_refused",
    "needs_input": "needs_input", "business_mutation_refused": "business_rule_refused",
    "business_mutation_blocked": "business_rule_refused",
    "permission_denied": "permission_denied", "approval_denied": "approval_denied",
    "approval_expired": "approval_expired", "indeterminate_write": "indeterminate_write",
    "step_budget": "step_budget", "wall_clock_budget": "wall_clock_budget",
    "token_budget": "token_budget", "no_progress": "no_progress",
    "action_policy_budget": "policy_refused", "user_cancelled": "user_cancelled",
}
_DISPATCH_ORDER = {"none": 0, "proposed": 1, "dispatched": 2, "acknowledged": 3}
#: Audit events after which the run that made a proposal can no longer dispatch it. The product
#: records each with ``dispatched=False`` and ``side_effect_possible=False`` but keeps the
#: proposal's journal reservation ``prepared`` until it expires, so the journal alone cannot
#: tell a declined proposal from one still awaiting a decision.
CLOSED_WITHOUT_DISPATCH = frozenset({
    "business_mutation_denied", "business_mutation_approval_expired",
    "business_mutation_approval_failed", "business_mutation_approval_unavailable",
    "business_mutation_approval_mismatch", "business_mutation_approval_context_mismatch",
    "business_mutation_cancelled_before_dispatch", "business_mutation_blocked",
})


def project_woa(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    turns = []
    page_actions = invalid = business = repeated = 0
    approvals = {"requested": 0, "granted": 0, "denied": 0}
    proposals: list[dict[str, Any]] = []
    dispatch = "none"
    last_observation: dict[str, str] = {}
    for event in events:
        kind = event.get("event")
        run_id = str(event.get("run_id", ""))
        if kind == "observation":
            digest = str(event.get("digest", ""))
            if digest and last_observation.get(run_id) == digest:
                repeated += 1
            last_observation[run_id] = digest
        elif kind == "tool_call":
            tool = event.get("tool")
            if tool not in PAGE_ACTION_TOOLS and tool not in AGENT_TOOLS:
                business += 1
        elif kind == "business_mutation_proposed":
            proposals.append({"effect": event.get("effect"),
                              "targets": list(event.get("targets") or [])})
            dispatch = _furthest(dispatch, "proposed")
        elif kind == "business_mutation_execution_started":
            dispatch = _furthest(dispatch, "dispatched")
        elif kind == "business_mutation_result":
            dispatch = _furthest(dispatch, "acknowledged")
        elif kind == "business_mutation_indeterminate":
            dispatch = "unknown"
        elif kind == "run_finished":
            control = event.get("business_control") or {}
            approvals["requested"] += int(control.get("approval_requested", 0))
            approvals["granted"] += int(control.get("approval_granted", 0))
            approvals["denied"] += int(control.get("approval_denied", 0))
            page_actions += int(event.get("page_actions") or 0)
            invalid += int(event.get("policy_blocks") or 0) + int(event.get("write_refusals") or 0)
            reason = str(event.get("terminal_reason") or "")
            turns.append({"terminal_reason": reason,
                          "terminal": WOA_TERMINALS.get(reason, reason),
                          "steps_used": int(event.get("steps_used") or 0)})
    return {
        "turns": turns,
        "terminal": turns[-1]["terminal"] if turns else None,
        #: Whether these facts come from an audit that was read at all. A projection of nothing
        #: says nothing about what the product dispatched.
        "audit_readable": True,
        "page_actions": page_actions, "invalid_page_actions": invalid,
        "business_calls": business, "repeated_observations": repeated,
        "approvals": approvals, "proposals": proposals, "dispatch_state": dispatch,
        "observation_repeats_measured": True,
    }


def _furthest(current: str, candidate: str) -> str:
    if current == "unknown":
        return current
    return candidate if _DISPATCH_ORDER[candidate] > _DISPATCH_ORDER[current] else current


def closed_proposals(events: Sequence[dict[str, Any]]) -> set[str]:
    """Digests of proposals the audit closes without dispatch and never dispatches later."""
    closed: set[str] = set()
    dispatched: set[str] = set()
    for event in events:
        digest = event.get("proposal_digest")
        if not isinstance(digest, str) or not digest:
            continue
        kind = event.get("event")
        if kind == "business_mutation_execution_started":
            dispatched.add(digest)
        elif (kind in CLOSED_WITHOUT_DISPATCH and event.get("dispatched") is False
              and event.get("side_effect_possible") is False):
            closed.add(digest)
    return closed - dispatched


def project_pageagent(bridge: dict[str, Any] | None, *, host_errors: Sequence[tuple[str, str]] = (),
                      document_replaced: bool = False) -> dict[str, Any]:
    metrics = (bridge or {}).get("metrics") or {}
    if bridge is None:
        terminal = "document_replaced" if document_replaced else None
    elif bridge.get("terminal_reason") == "page_action_budget":
        terminal = "page_action_budget"
    elif bridge.get("status") == "completed":
        terminal = "completed" if bridge.get("success_claim") else "agent_unable"
    else:
        codes = [code for _host, code in host_errors]
        # A run that failed after its host recorded a failed provider call ended on that call;
        # without such a record, its bare status stays unmapped.
        terminal = ("model_call_budget" if "model_call_budget" in codes else
                    "provider_deadline" if "provider_deadline" in codes else
                    "provider_error" if any(host == "provider" for host, _code in host_errors)
                    else str(bridge.get("status") or "unknown"))
    return {
        "turns": [{"terminal_reason": terminal, "terminal": terminal,
                   "steps_used": int(metrics.get("steps") or 0)}],
        "terminal": terminal,
        "page_actions": int(metrics.get("page_actions") or 0),
        "invalid_page_actions": 0,
        "business_calls": int(metrics.get("business_calls") or 0),
        "repeated_observations": 0,
        "approvals": {"requested": 0, "granted": 0, "denied": 0},
        "proposals": [],
        "dispatch_state": "none",
        "observation_repeats_measured": False,
    }
