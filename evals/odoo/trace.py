"""Common trace projection for both arms.

The two engines count page work differently -- WOA's audit ledger records typed tool calls
and observations, PageAgent's bridge counts executed DOM tools -- so no engine-specific
counter is used as a shared hard budget. Instead both traces are projected onto one small,
content-free schema that is reported, never enforced: page actions, invalid page actions,
business calls, repeated observations, approvals, the furthest mutation dispatch state and the
engine terminal mapped onto the protocol's product terminals.

A count is projected only from a record that carries it. Where a trace says nothing -- a
PageAgent bridge that never reported its metrics, an engine that does not measure repeats -- the
count is ``None``, never zero: an engine's silence is not a record of doing no work.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
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
#: Where each projected approval count is read from in the product's own run record.
_APPROVAL_COUNTS = {"requested": "approval_requested", "granted": "approval_granted",
                    "denied": "approval_denied"}
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


#: What a projection can still say when the audit behind it could not be exported: nothing it
#: would have carried. Not the counts, not the approvals, and not how any turn ended -- an audit
#: nobody read says neither that a turn ended nor that it did not -- and a dispatch that has to
#: be taken as unknown because nothing was read either way.
UNREAD_TRACE: dict[str, Any] = {
    "turns": None, "terminal": None,
    "page_actions": None, "invalid_page_actions": None, "business_calls": None,
    "repeated_observations": None, "approvals": None, "dispatch_state": "unknown",
}


def _text(record: Mapping[str, Any], name: str) -> str | None:
    """The string ``record`` carries under ``name``, or ``None`` when it carries none."""
    value = record.get(name)
    return value if isinstance(value, str) and value else None


def _targets(record: Mapping[str, Any], name: str) -> list[str] | None:
    """The list of names ``record`` carries, or ``None`` when it carries none. A proposal whose
    targets were not recorded has unknown targets, which no approval may be matched against."""
    value = record.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def _count(record: Mapping[str, Any], name: str) -> int | None:
    """The count ``record`` carries under ``name``, or ``None`` when it carries none."""
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _add(total: int | None, *parts: int | None) -> int | None:
    """Add counts that may be unknown. A sum missing one of its parts is unknown."""
    if total is None or any(part is None for part in parts):
        return None
    return total + sum(part for part in parts if part is not None)


def project_woa(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    turns = []
    page_actions: int | None = 0
    invalid: int | None = 0
    business = repeated = 0
    approvals: dict[str, int] | None = {"requested": 0, "granted": 0, "denied": 0}
    proposals: list[dict[str, Any]] = []
    dispatch = "none"
    last_observation: dict[str, str] = {}
    # Approvals are counted only from each run's own record. A run the audit shows working but
    # never closing -- an Agent or driver that died mid-run -- leaves the approvals unknown:
    # counting it as none would turn a dispatch nobody saw approved into one nobody approved.
    runs_seen: set[str] = set()
    runs_recorded: set[str] = set()
    for event in events:
        kind = event.get("event")
        run_id = _text(event, "run_id")
        if run_id is not None:
            runs_seen.add(run_id)
            if kind == "run_finished":
                runs_recorded.add(run_id)
        if kind == "observation":
            digest = _text(event, "digest")
            # Only two observations that both name the same run and the same page repeat; one
            # that names neither is not evidence of a repeat.
            if digest is not None and run_id is not None:
                if last_observation.get(run_id) == digest:
                    repeated += 1
                last_observation[run_id] = digest
        elif kind == "tool_call":
            tool = event.get("tool")
            if tool not in PAGE_ACTION_TOOLS and tool not in AGENT_TOOLS:
                business += 1
        elif kind == "business_mutation_proposed":
            proposals.append({"effect": _text(event, "effect"),
                              "targets": _targets(event, "targets")})
            dispatch = _furthest(dispatch, "proposed")
        elif kind == "business_mutation_execution_started":
            dispatch = _furthest(dispatch, "dispatched")
        elif kind == "business_mutation_result":
            dispatch = _furthest(dispatch, "acknowledged")
        elif kind == "business_mutation_indeterminate":
            dispatch = "unknown"
        elif kind == "run_finished":
            control = event.get("business_control")
            decided = ({name: _count(control, field) for name, field in _APPROVAL_COUNTS.items()}
                       if isinstance(control, Mapping) else
                       dict.fromkeys(_APPROVAL_COUNTS))
            # A run whose record does not carry its approval counts leaves the attempt's
            # approvals unknown; reading the missing counts as zero would report a dispatch
            # nobody confirmed as one nobody approved.
            approvals = ({name: approvals[name] + value for name, value in decided.items()}
                         if approvals is not None and None not in decided.values() else None)
            page_actions = _add(page_actions, _count(event, "page_actions"))
            invalid = _add(invalid, _count(event, "policy_blocks"),
                           _count(event, "write_refusals"))
            # A run record that names no ending closed no turn: that is a missing ending, not
            # an ending named by the empty string.
            reason = _text(event, "terminal_reason")
            turns.append({"terminal_reason": reason,
                          "terminal": WOA_TERMINALS.get(reason, reason),
                          "steps_used": _count(event, "steps_used")})
    if runs_seen - runs_recorded:
        approvals = None
    return {
        "turns": turns,
        "terminal": turns[-1]["terminal"] if turns else None,
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
    reported = bridge.get("metrics") if isinstance(bridge, Mapping) else None
    metrics = reported if isinstance(reported, Mapping) else None
    if bridge is None:
        terminal = "document_replaced" if document_replaced else None
    elif bridge.get("terminal_reason") == "page_action_budget":
        terminal = "page_action_budget"
    elif bridge.get("status") == "completed":
        terminal = "completed" if bridge.get("success_claim") else "agent_unable"
    else:
        codes = [code for host, code in host_errors if host == "provider"]
        # A run that failed after its host recorded a failed provider call ended on that call.
        # One whose host failed on its own lost its ending with it: nothing names how the run
        # would have ended, and the control-channel fault the runner recorded says why. Without
        # either record, its bare status stays unmapped.
        terminal = ("model_call_budget" if "model_call_budget" in codes else
                    "provider_deadline" if "provider_deadline" in codes else
                    "provider_error" if codes else
                    None if any(host == "host" for host, _code in host_errors)
                    else str(bridge.get("status") or "unknown"))
    return {
        "turns": [{"terminal_reason": terminal, "terminal": terminal,
                   "steps_used": None if metrics is None else _count(metrics, "steps")}],
        "terminal": terminal,
        "page_actions": None if metrics is None else _count(metrics, "page_actions"),
        # The bridge reports neither refused page actions nor repeated observations, so this
        # arm has no count of them to project.
        "invalid_page_actions": None,
        "business_calls": None if metrics is None else _count(metrics, "business_calls"),
        "repeated_observations": None,
        "approvals": {"requested": 0, "granted": 0, "denied": 0},
        "proposals": [],
        "dispatch_state": "none",
        "observation_repeats_measured": False,
    }
