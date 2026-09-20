"""Project one real product turn into deterministic Twenty breadth evidence.

This module is intentionally separate from the frozen Companies v1 assembler. It consumes
only product execution facts collected by the same runner host and never reads fixture truth.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

from evals.twenty.breadth_taskset import validate_breadth_execution_case
from evals.twenty.evidence import EvidenceContractError, bounded_text, create_evidence
from evals.twenty.outcomes import classify_woa_outcome
from evals.woa.assembler import (ENGINE_ANSWER_CEILING, ENGINE_NAME, ENGINE_RELEASE,
                                 FinalPageSample, TurnEvidence, WoaRunAssemblyError,
                                 digest_json, permitted_routes, utc_now)
from web_operation_agent.execution.product import AgentTurnExecution


RUNNER_VERSION = 2
_FIELDS = {
    "companies": ("name", "domain"),
    "people": ("name", "email"),
    "opportunities": ("name", "stage"),
}
_BUSINESS_ATTEMPT_EVENTS = frozenset({
    "business_tool_result", "business_tool_rejected", "business_mutation_result",
    "business_mutation_failed", "business_mutation_rejected",
})


def _count(events: tuple[dict[str, Any], ...], *names: str) -> int:
    return sum(1 for event in events if event.get("event") in names)


def _approvals(events: tuple[dict[str, Any], ...]) -> tuple[int, int, int]:
    requested = _count(events, "approval_requested")
    approved = sum(1 for event in events
                   if event.get("event") == "approval_decided"
                   and event.get("decision") == "approved")
    denied = sum(1 for event in events
                 if event.get("event") == "approval_decided"
                 and event.get("decision") == "denied")
    return requested, approved, denied


def _page(final_page: FinalPageSample | None, *, visible: bool) -> dict[str, str]:
    if final_page is None:
        raise EvidenceContractError("final page was not sampled")
    path = urlsplit(final_page.document_url).path
    if urlsplit(final_page.observation_url).path != path:
        raise EvidenceContractError("final page observation disagrees with the document")
    path = bounded_text(path, "final page path", 1_000)
    if not path.startswith("/") or path.startswith("//"):
        raise EvidenceContractError("final page path is not canonical")
    result = {"path": path}
    if visible:
        result["visible_projection"] = bounded_text(
            final_page.visible_projection, "visible page projection", 512 * 1024)
    return result


def _query(execution: AgentTurnExecution, *, entity: str) -> dict[str, Any]:
    result = execution.business_result
    projection = result.query_answer if result is not None else None
    expected = _FIELDS[entity]
    if (not execution.agent.business_answer_projected or projection is None
            or projection.collection != entity or projection.fields != expected):
        raise EvidenceContractError("typed query has no matching structured answer projection")
    public = projection.to_json()
    return {
        "artifact_version": 2,
        "execution_status": "completed",
        "complete": public["completeness"]["complete"],
        "records": [
            {"name": row[expected[0]], "detail": row[expected[1]]}
            for row in public["records"]
        ],
    }


def _case_projection(case: dict[str, Any]) -> dict[str, Any]:
    task = case["evaluator_task"]
    return {
        **{key: case[key] for key in (
            "set_id", "split", "task_id", "task_digest", "template_family_digest")},
        "evaluator_task": {"artifact_version": task["artifact_version"],
                           "kind": task["kind"],
                           **({"entity": task["entity"]} if "entity" in task else {})},
    }


def assemble_woa_breadth_run(
        *, case: dict[str, Any], run_id: str, evidence: TurnEvidence,
        capability_profile: str, source_mode: str, source_interface: str,
        started_at: str, started_monotonic: float) -> dict[str, Any]:
    """Classify one breadth turn and preserve only bounded, independently scored evidence."""
    selected = validate_breadth_execution_case(case)
    if not isinstance(run_id, str) or not run_id.strip():
        raise WoaRunAssemblyError("run_id is invalid")
    if capability_profile != "skill_api" or source_mode not in {"off", "on"}:
        raise WoaRunAssemblyError("breadth runner requires the skill_api capability profile")
    if source_interface not in {"absent", "paired"}:
        raise WoaRunAssemblyError("engine source interface is invalid")
    if not isinstance(evidence, TurnEvidence):
        raise WoaRunAssemblyError("evidence must be a TurnEvidence")
    execution = evidence.execution
    if execution is not None and not isinstance(execution, AgentTurnExecution):
        raise WoaRunAssemblyError("execution must be an AgentTurnExecution or None")
    if execution is not None and evidence.boundary_error_code:
        raise WoaRunAssemblyError("a turn cannot both finish and fail at the boundary")
    events = tuple(event for event in evidence.ledger_events if isinstance(event, dict))
    business_results = tuple(evidence.business_results)
    evaluator_task = selected["evaluator_task"]
    evaluator_kind = evaluator_task["kind"]

    status, apparatus_code, error_code = "failed", None, None
    evidence_out: dict[str, Any] = {"artifact_version": 2, "execution_status": "failed"}
    if evidence.boundary_error_code:
        apparatus_code, error_code = classify_woa_outcome(
            terminal_reason="", boundary_error_code=evidence.boundary_error_code)
    elif evidence.cancelled:
        error_code = "user_cancelled"
    elif execution is None:
        apparatus_code, error_code = "transport_contract", "turn_missing"
    else:
        apparatus_code, error_code = classify_woa_outcome(
            terminal_reason=execution.terminal_reason, events=events)
        if apparatus_code is None:
            try:
                if evaluator_kind == "refuse":
                    # The product has to say this itself, in typed form. Reading it off the
                    # task's own kind would let any stop -- out of steps, out of ideas, a
                    # search that found nothing -- be written down as a capability statement,
                    # which is the assembler answering its own question. The claim lives in
                    # the verified answer's capability_gap conclusion, not in the terminal
                    # name, and it only counts alongside a run that dispatched nothing.
                    dispatch_count = _count(events, "business_mutation_execution_started")
                    verified = getattr(execution.agent, "verified_answer", None)
                    conclusions = (verified or {}).get("conclusions") or ()
                    typed_gap = (len(conclusions) == 1
                                 and isinstance(conclusions[0], dict)
                                 and conclusions[0].get("kind") == "capability_gap")
                    if not typed_gap:
                        raise EvidenceContractError(
                            "the product produced no capability_gap conclusion; it ended with "
                            f"{execution.terminal_reason!r}")
                    if dispatch_count:
                        raise EvidenceContractError(
                            "a capability gap claims nothing ran, but the run dispatched "
                            f"{dispatch_count} mutation(s)")
                    # The evaluator's refusal evidence is exactly these four fields. What is
                    # stricter now is what earns them, not their shape.
                    evidence_out = {
                        "artifact_version": 2, "execution_status": "failed",
                        "refusal_code": "unsupported_operation",
                        "dispatch_count": dispatch_count,
                    }
                    error_code = "unsupported_operation"
                elif execution.terminal_reason == "indeterminate_write":
                    status, error_code = "indeterminate", "indeterminate_write"
                    evidence_out = {"artifact_version": 1,
                                    "execution_status": "indeterminate"}
                elif evaluator_kind in {"enumerate", "filter"}:
                    if execution.terminal_reason == "agent_finished":
                        try:
                            evidence_out = _query(execution, entity=selected["entity"])
                            status, error_code = "completed", None
                        except EvidenceContractError:
                            error_code = "query_answer_unavailable"
                    else:
                        error_code = execution.terminal_reason
                    if status != "completed":
                        # The runner observed a complete product turn, so an absent, stale,
                        # or wrong-collection answer is an Agent outcome rather than broken
                        # apparatus. Emit valid negative evidence so the independent evaluator
                        # scores it FAIL instead of excluding it as INVALID.
                        evidence_out = {
                            "artifact_version": 2,
                            "execution_status": "failed",
                            "complete": False,
                            "records": [],
                        }
                elif execution.terminal_reason != "agent_finished":
                    error_code = execution.terminal_reason
                elif evaluator_kind == "open_record":
                    page = _page(evidence.final_page, visible=True)
                    if page["path"] not in permitted_routes(events, business_results):
                        raise EvidenceContractError("record route was not opened with a permit")
                    evidence_out = {"artifact_version": 2,
                                    "execution_status": "completed", "page": page}
                    status, error_code = "completed", None
                elif evaluator_kind == "open_collection":
                    evidence_out = {"artifact_version": 2,
                                    "execution_status": "completed",
                                    "page": _page(evidence.final_page, visible=False)}
                    status, error_code = "completed", None
                else:
                    final_page = evidence.final_page
                    requested, approved, denied = _approvals(events)
                    evidence_out = create_evidence(
                        business_results=business_results,
                        mutation_dispatch_count=_count(
                            events, "business_mutation_execution_started"),
                        approvals_requested=requested, approvals_approved=approved,
                        approvals_denied=denied,
                        require_open=bool(evaluator_task["require_open"]),
                        path=(urlsplit(final_page.document_url).path
                              if final_page is not None else ""),
                        visible_projection=(final_page.visible_projection
                                            if final_page is not None else ""),
                        permitted_routes=permitted_routes(events, business_results))
                    status, error_code = "completed", None
            except EvidenceContractError:
                error_code = "evidence_contract"
        if not evidence.session_binding_unchanged:
            apparatus_code, error_code = "credential_or_identity", "session_changed"
    if apparatus_code is not None:
        status = "apparatus_invalid"
        evidence_out = {"artifact_version": 2, "execution_status": "failed"}

    requested, approved, denied = _approvals(events)
    attempts = _count(events, *_BUSINESS_ATTEMPT_EVENTS)
    turn = None
    answer_digest = None
    if execution is not None:
        answer_digest = digest_json(execution.user_answer)
        turn = {
            "status": execution.status.value,
            "terminal_reason": execution.terminal_reason,
            "steps_used": execution.agent.steps_used,
            "page_actions": execution.agent.page_actions,
            "policy_blocks": execution.agent.policy_blocks,
            "answer_chars": len(execution.user_answer),
            "answer_at_engine_ceiling": len(execution.user_answer) >= ENGINE_ANSWER_CEILING,
            "business_attempts": attempts,
        }
    ledger_digest = digest_json(list(events)) if execution is not None or events else None
    final_page = ({"path": urlsplit(evidence.final_page.document_url).path,
                   "observation_digest": evidence.final_page.observation_digest}
                  if evidence.final_page is not None else None)
    trace_digest = digest_json({
        "turn": turn, "boundary_error_code": evidence.boundary_error_code,
        "session_ledger_digest": ledger_digest,
        "business_result_digests": [digest_json(item) for item in business_results],
        "final_page": final_page,
        "session_binding_unchanged": evidence.session_binding_unchanged,
        "page_errors": int(evidence.page_error_count), "cancelled": bool(evidence.cancelled),
    })
    result = {
        "artifact_version": RUNNER_VERSION, "kind": "woa_twenty_breadth_run",
        "status": status, "apparatus_code": apparatus_code, "error_code": error_code,
        "run_id": run_id, "case": _case_projection(selected),
        "engine": {"name": ENGINE_NAME, "release": ENGINE_RELEASE,
                   "capability_profile": capability_profile, "source_mode": source_mode,
                   "source_interface": source_interface},
        "evidence": evidence_out, "turn": turn,
        "boundary_error_code": evidence.boundary_error_code,
        "identity": {"principal_bound": bool(evidence.principal_bound),
                     "session_binding_unchanged": bool(evidence.session_binding_unchanged)},
        "final_page": final_page,
        "session_ledger": {"event_count": len(events), "digest": ledger_digest},
        "cancelled": bool(evidence.cancelled),
        "page_error_count": int(evidence.page_error_count),
        "runtime_trace_digest": trace_digest,
        "trial": {
            "accounting": (dict(evidence.accounting)
                           if isinstance(evidence.accounting, dict) else None),
            "page_actions": execution.agent.page_actions if execution is not None else 0,
            "business_calls": attempts,
            "approvals_requested": requested, "approvals_approved": approved,
            "approvals_denied": denied, "automatic_retries": 0,
            "user_interventions": 0,
            "safety": {
                "unauthorized_writes": _count(events, "write_refused"),
                "duplicate_writes": max(
                    0, _count(events, "business_mutation_execution_started") - 1),
                "forbidden_state_changes": 0, "cross_identity_actions": 0,
            },
            "session_ledger_digest": ledger_digest,
            "public_answer_digest": answer_digest,
            "runtime_trace_digest": trace_digest,
        },
        "started_at": started_at, "finished_at": utc_now(),
        "wall_ms": int((time.monotonic() - started_monotonic) * 1_000),
    }
    return {**result, "run_digest": digest_json(result)}


__all__ = ["assemble_woa_breadth_run"]
