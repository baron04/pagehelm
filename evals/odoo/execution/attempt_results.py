"""Pure attempt-result projection, classification and accounting helpers."""
from __future__ import annotations

from typing import Any

from evals.odoo.domain import outcomes
from evals.odoo.execution import trace
from evals.odoo.judging import capture


def agent_assertions(start: dict[str, Any] | None, end: dict[str, Any] | None,
                     audit: list[dict[str, Any]], *, product_started: bool,
                     audit_observed: bool = True) -> tuple[dict[str, Any], list[str]]:
    """Return content-free Agent-state assertions and any residue they expose."""
    closed = trace.closed_proposals(audit)
    prepared = set(end["prepared_proposals"]) if end is not None else set()
    record = {"start": _agent_counts(start), "end": _agent_counts(end),
              "open_proposals": len(prepared - closed),
              "closed_proposals": len(prepared & closed)}
    residue = []
    if start is not None and any(record["start"].values()):
        residue.append("agent_residue:inherited_state")
    if end is None:
        if product_started:
            residue.append("agent_state_unreadable")
        return record, residue
    residue += [f"agent_residue:{key}" for key in ("active_sessions", "unresolved_mutations")
                if end[key]]
    if record["open_proposals"]:
        residue.append("agent_residue:open_proposals" if audit_observed
                       else "agent_audit_unreadable")
    return record, residue


def _agent_counts(state: dict[str, Any] | None) -> dict[str, int] | None:
    if state is None:
        return None
    return {"active_sessions": int(state["active_sessions"]),
            "unresolved_mutations": int(state["unresolved_mutations"]),
            "prepared_proposals": len(state["prepared_proposals"]),
            "conversation_rows": int(state["conversation_rows"])}


def turns_before_fault(settled: dict[int, int], first_fault_seq: int | None) -> int:
    """Count leading turns that ended before the first faulty gateway record."""
    count = 0
    while count in settled and (first_fault_seq is None or settled[count] < first_fault_seq):
        count += 1
    return count


def final_page(artifact: dict[str, Any], scope: dict[str, Any]) -> capture.Capture:
    """Return the final-page capture, preserving why the page was not observed."""
    final = artifact["final"]
    if isinstance(final, dict):
        return capture.Present(final, "final_page", scope)
    for field in ("final_page_error", "control_channel_error"):
        fault = artifact[field]
        if fault:
            return capture.Unavailable(str(fault["type"]), "final_page", scope)
    if artifact["browser_exit"]:
        return capture.Unavailable(str(artifact["browser_exit"]), "final_page", scope)
    return capture.Unavailable("not_captured", "final_page", scope)


def trace_counts(projected: dict[str, Any], *, whole: bool) -> dict[str, int | None]:
    """Return trace counters, or unknown values when the trace was incomplete."""
    return {name: (projected[name] if whole else None) for name in
            ("page_actions", "invalid_page_actions", "business_calls", "repeated_observations")}


def turn_bounds(settled: dict[int, int], index: int) -> tuple[int | None, int | None]:
    """Return request numbers made by one turn as ``(after, through)``."""
    return (0 if index == 0 else settled.get(index - 1)), settled.get(index)


def final_turn_requests(settled: dict[int, int], turns: int) -> tuple[int, int] | None:
    """Return the final turn's provable request-number span."""
    if turns <= 0:
        return None
    after, through = turn_bounds(settled, turns - 1)
    return None if after is None or through is None else (after, through)


def scored_terminal(runner_terminal: str | None, facts: outcomes.GatewayFacts,
                    budget_exhausted: str | None = None,
                    final_turn: tuple[int, int] | None = None, *,
                    stopped: bool = False) -> str | None:
    """Choose the typed terminal supported by runner and gateway evidence."""
    generic = runner_terminal in outcomes.GENERIC_PROVIDER_TERMINALS
    if facts.terminal == "model_call_budget" and runner_terminal in outcomes.PRODUCT_TERMINALS:
        return facts.terminal
    if budget_exhausted is not None and (
            runner_terminal in outcomes.PRODUCT_TERMINALS
            or stopped and (generic or runner_terminal is None)):
        return "wall_clock_budget"
    if generic and final_turn is not None:
        for seq, terminal in facts.terminals:
            if final_turn[0] < seq == final_turn[1]:
                return terminal
    return runner_terminal


UNASSERTED_RESIDUE = frozenset({"agent_state_unreadable", "agent_audit_unreadable"})


def with_residue(outcome: str, residue: list[str]) -> str:
    """Prevent a reset from passing while observed or unasserted residue remains."""
    if outcome not in {"PASS", "NOT_REQUIRED"} or not residue:
        return outcome
    return "FAIL" if set(residue) - UNASSERTED_RESIDUE else "INDETERMINATE"


def attempt_cost(attempt_id: str, requests: list[dict],
                 responses: list[dict]) -> dict[str, Any]:
    """Aggregate one attempt's provider usage without treating unknown usage as zero."""
    by_seq = {item["request_seq"]: item for item in responses}
    unavailable = sum(1 for item in requests
                      if by_seq.get(item["request_seq"]) is None
                      or by_seq[item["request_seq"]]["usage"] is None
                      or by_seq[item["request_seq"]]["cost_cny"] is None)
    metered = [item for item in responses if item["usage"] is not None]
    known = round(sum(item["cost_cny"] for item in responses
                      if item["cost_cny"] is not None), 6)
    return {
        "attempt_id": attempt_id, "model_calls": len(requests),
        "usage_unavailable_calls": unavailable,
        "input_tokens": sum(item["usage"]["input_tokens"] for item in metered),
        "output_tokens": sum(item["usage"]["output_tokens"] for item in metered),
        "cached_tokens": sum(item["usage"]["cached_tokens"] for item in metered),
        "reasoning_tokens": sum(item["usage"]["reasoning_tokens"] for item in metered),
        "known_cost_cny": known, "total_cost_cny": None if unavailable else known,
    }
