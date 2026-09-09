"""Assemble one Web Operation Agent turn into the engine-neutral Twenty run artifact.

The assembler consumes what the product path already produces: the ``AgentTurnExecution``
or the typed boundary error, the private Session Ledger events of that run, the SpendMeter
accounting slice, the final page as observed by the runtime and sampled by the trusted host,
and the browser/API identity check. It never reads database truth, the evaluator, or a
model's tool arguments, and it never repairs the public user answer.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from evals.twenty.evidence import (
    EvidenceContractError, case_projection, disclosed_routes, failed_evidence,
    indeterminate_evidence, open_evidence, query_evidence, validate_execution_case,
)
from evals.twenty.outcomes import classify_woa_outcome
from web_operation_agent import __version__ as _PRODUCT_VERSION
from web_operation_agent.product import AgentTurnExecution


RUNNER_VERSION = 1
ENGINE_NAME = "web_operation_agent"
#: ``ServerAgentSession`` truncates its own ``agent_said`` at 500 characters, well below
#: the turn policy limit. An answer at that ceiling may have been cut by the engine rather
#: than written short, so the artifact reports the condition instead of hiding it.
ENGINE_ANSWER_CEILING = 500
ENGINE_RELEASE = f"web-operation-agent@{_PRODUCT_VERSION}"
_BUSINESS_ATTEMPT_EVENTS = frozenset({
    "business_tool_result", "business_tool_rejected", "business_mutation_result",
    "business_mutation_failed", "business_mutation_rejected",
})
_ROUTE_OPENED_EVENT = "business_route_opened"


class WoaRunAssemblyError(ValueError):
    """The assembler input cannot describe one Web Operation Agent trial."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise WoaRunAssemblyError("artifact must be finite JSON") from exc


def digest_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class FinalPageSample:
    """The final page as the runtime observed it and as the trusted host sampled it."""

    document_url: str
    observation_url: str
    observation_id: str
    observation_digest: str
    visible_projection: str


@dataclass(frozen=True, slots=True)
class TurnEvidence:
    """Everything a runner host collected about one turn, before any classification."""

    execution: AgentTurnExecution | None = None
    boundary_error_code: str | None = None
    ledger_events: tuple[dict[str, Any], ...] = ()
    accounting: dict[str, Any] | None = None
    business_results: tuple[dict[str, Any], ...] = ()
    final_page: FinalPageSample | None = None
    session_binding_unchanged: bool = False
    principal_bound: bool = False
    cancelled: bool = False
    page_error_count: int = 0


def _count(events: Sequence[dict[str, Any]], *names: str) -> int:
    return sum(1 for event in events if event.get("event") in names)


def _approvals(events: Sequence[dict[str, Any]]) -> tuple[int, int, int]:
    requested = _count(events, "approval_requested")
    approved = sum(1 for event in events
                   if event.get("event") == "approval_decided"
                   and event.get("decision") == "approved")
    denied = sum(1 for event in events
                 if event.get("event") == "approval_decided"
                 and event.get("decision") == "denied")
    return requested, approved, denied


def permitted_routes(events: Sequence[dict[str, Any]],
                     business_results: Sequence[dict[str, Any]]) -> list[str]:
    """Routes the server authorized for navigation: a disclosed ``ui_path`` whose digest the
    session recorded when it opened a business route."""
    opened = {
        event.get("url_digest") for event in events
        if event.get("event") == _ROUTE_OPENED_EVENT and isinstance(event.get("url_digest"), str)
    }
    return [route for route in disclosed_routes(business_results)
            if hashlib.sha256(route.encode("utf-8")).hexdigest() in opened]


def _open_page_evidence(final_page: FinalPageSample | None,
                        events: Sequence[dict[str, Any]],
                        business_results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if final_page is None:
        raise EvidenceContractError("final page was not sampled")
    path = urlsplit(final_page.document_url).path
    if urlsplit(final_page.observation_url).path != path:
        raise EvidenceContractError("final page observation disagrees with the document")
    return open_evidence(
        path=path, visible_projection=final_page.visible_projection,
        business_results=business_results,
        permitted_routes=permitted_routes(events, business_results))


def assemble_woa_run(*, case: dict[str, Any], run_id: str, evidence: TurnEvidence,
                     capability_profile: str, source_mode: str, started_at: str,
                     started_monotonic: float) -> dict[str, Any]:
    """Classify one turn and project it into the ``woa_twenty_run`` artifact."""
    selected = validate_execution_case(case)
    if not isinstance(run_id, str) or not run_id.strip():
        raise WoaRunAssemblyError("run_id is invalid")
    if capability_profile not in {"browser_only", "skill_api"} or source_mode not in {"off", "on"}:
        raise WoaRunAssemblyError("engine capability profile or source mode is invalid")
    if not isinstance(evidence, TurnEvidence):
        raise WoaRunAssemblyError("evidence must be a TurnEvidence")
    execution = evidence.execution
    if execution is not None and not isinstance(execution, AgentTurnExecution):
        raise WoaRunAssemblyError("execution must be an AgentTurnExecution or None")
    if execution is not None and evidence.boundary_error_code:
        raise WoaRunAssemblyError("a turn cannot both finish and fail at the boundary")
    events = tuple(event for event in evidence.ledger_events if isinstance(event, dict))
    business_results = tuple(evidence.business_results)

    status = "failed"
    apparatus_code: str | None = None
    error_code: str | None = None
    evidence_out: dict[str, Any] = failed_evidence()
    if evidence.boundary_error_code:
        apparatus_code, error_code = classify_woa_outcome(
            terminal_reason="", boundary_error_code=evidence.boundary_error_code)
    elif evidence.cancelled:
        # The user stopped the task before the turn started. That is a scorable product
        # outcome, not a fault of the apparatus.
        error_code = "user_cancelled"
    elif execution is None:
        apparatus_code, error_code = "transport_contract", "turn_missing"
    else:
        apparatus_code, error_code = classify_woa_outcome(
            terminal_reason=execution.terminal_reason, events=events)
        if apparatus_code is None:
            if execution.terminal_reason == "indeterminate_write":
                status = "indeterminate"
                evidence_out = indeterminate_evidence()
                error_code = "indeterminate_write"
            elif execution.terminal_reason != "agent_finished":
                error_code = execution.terminal_reason
            elif evidence.cancelled:
                error_code = "cancelled"
            else:
                try:
                    evidence_out = (
                        query_evidence(execution.user_answer, business_results)
                        if selected["task_kind"] == "query"
                        else _open_page_evidence(
                            evidence.final_page, events, business_results))
                    status, error_code = "completed", None
                except EvidenceContractError:
                    error_code = "evidence_contract"
        if not evidence.session_binding_unchanged:
            # A replaced or missing browser session invalidates even a claimed success.
            apparatus_code, error_code = "credential_or_identity", "session_changed"
    if apparatus_code is not None:
        status, evidence_out = "apparatus_invalid", failed_evidence()

    requested, approved, denied = _approvals(events)
    attempts = _count(events, *_BUSINESS_ATTEMPT_EVENTS)
    turn: dict[str, Any] | None = None
    answer_digest: str | None = None
    if execution is not None:
        answer_digest = digest_json(execution.user_answer)
        turn = {
            "status": execution.status.value,
            "terminal_reason": execution.terminal_reason,
            "steps_used": execution.agent.steps_used,
            "page_actions": execution.agent.page_actions,
            "policy_blocks": execution.agent.policy_blocks,
            "wall_seconds": execution.agent.wall_seconds,
            "success_claim": execution.agent.success_claim,
            "answer_fallback_used": execution.answer_fallback_used,
            "answer_chars": len(execution.user_answer),
            "answer_at_engine_ceiling": len(execution.user_answer) >= ENGINE_ANSWER_CEILING,
            "business_attempts": attempts,
            "business_rejections": _count(events, "business_tool_rejected"),
            "route_rejections": _count(events, "page_route_rejected"),
        }
    ledger_digest = digest_json(list(events)) if execution is not None or events else None
    final_page = None
    if evidence.final_page is not None:
        final_page = {
            "path": urlsplit(evidence.final_page.document_url).path,
            "observation_id": evidence.final_page.observation_id,
            "observation_digest": evidence.final_page.observation_digest,
        }
    identity = {
        "principal_bound": bool(evidence.principal_bound),
        "session_binding_unchanged": bool(evidence.session_binding_unchanged),
    }
    runtime_trace_digest = digest_json({
        "turn": turn,
        "boundary_error_code": evidence.boundary_error_code,
        "session_ledger_digest": ledger_digest,
        "business_result_digests": [digest_json(item) for item in business_results],
        "final_page": final_page,
        "identity": identity,
        "page_errors": int(evidence.page_error_count),
        "cancelled": bool(evidence.cancelled),
    })
    trial = {
        "accounting": dict(evidence.accounting) if isinstance(evidence.accounting, dict) else None,
        "page_actions": execution.agent.page_actions if execution is not None else 0,
        "business_calls": attempts,
        "approvals_requested": requested,
        "approvals_approved": approved,
        "approvals_denied": denied,
        "automatic_retries": 0,
        "user_interventions": 0,
        "safety": {
            "unauthorized_writes": _count(events, "write_refused"),
            "duplicate_writes": 0,
            "forbidden_state_changes": 0,
            "cross_identity_actions": 0,
        },
        "session_ledger_digest": ledger_digest,
        "public_answer_digest": answer_digest,
        "runtime_trace_digest": runtime_trace_digest,
    }
    result = {
        "artifact_version": RUNNER_VERSION,
        "kind": "woa_twenty_run",
        "status": status,
        "apparatus_code": apparatus_code,
        "error_code": error_code,
        "run_id": run_id,
        "case": case_projection(selected),
        "engine": {
            "name": ENGINE_NAME,
            "release": ENGINE_RELEASE,
            "capability_profile": capability_profile,
            "source_mode": source_mode,
        },
        "evidence": evidence_out,
        "turn": turn,
        "boundary_error_code": evidence.boundary_error_code,
        "identity": identity,
        "final_page": final_page,
        "session_ledger": {"event_count": len(events), "digest": ledger_digest},
        "cancelled": bool(evidence.cancelled),
        "page_error_count": int(evidence.page_error_count),
        "runtime_trace_digest": runtime_trace_digest,
        "trial": trial,
        "started_at": started_at,
        "finished_at": utc_now(),
        "wall_ms": int((time.monotonic() - started_monotonic) * 1_000),
    }
    return {**result, "run_digest": digest_json(result)}
