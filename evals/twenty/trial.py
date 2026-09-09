"""Engine-neutral projection from a runner artifact into the frozen trial ledger."""
from __future__ import annotations

import re
from typing import Any

from .evaluator import EvaluationInputError, digest_json, validate_state
from .records import validate_trial_record
from .taskset import validate_seal


_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _expected(protocol: dict[str, Any], index: int) -> tuple[dict[str, Any], int, str]:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise EvaluationInputError("trial schedule index is invalid")
    try:
        pair = protocol["schedule"][index // 2]
        arm_order = index % 2 + 1
        comparison = next(
            item for item in protocol["comparisons"]
            if item["comparison_id"] == pair["comparison_id"])
    except (KeyError, IndexError, StopIteration, TypeError) as exc:
        raise EvaluationInputError("trial schedule position is unavailable") from exc
    second = (
        comparison["right_arm"]
        if pair["first_arm"] == comparison["left_arm"] else comparison["left_arm"])
    return pair, arm_order, pair["first_arm"] if arm_order == 1 else second


def _digest(value: Any, name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise EvaluationInputError(f"{name} is unavailable")
    return value


def project_trial_record(*, protocol: dict[str, Any], test_seal: dict[str, Any],
                         batch_id: str, schedule_index: int, arm_id: str,
                         run: dict[str, Any], evaluation: dict[str, Any],
                         before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Build and validate one canonical record without knowing the engine implementation."""
    seal = validate_seal(test_seal)
    pair, arm_order, expected_arm = _expected(protocol, schedule_index)
    if arm_id != expected_arm:
        raise EvaluationInputError("runner arm does not match the frozen schedule")
    try:
        engine = next(
            item["engine"] for item in protocol["arms"]
            if item["arm_id"] == expected_arm)
    except (KeyError, StopIteration, TypeError) as exc:
        raise EvaluationInputError("scheduled runner engine is unavailable") from exc
    if not isinstance(run, dict) or run.get("artifact_version") != 1:
        raise EvaluationInputError("runner artifact header is invalid")
    expected_kind = (
        "woa_twenty_run" if engine == "web_operation_agent"
        else "pageagent_twenty_run")
    if run.get("kind") != expected_kind:
        raise EvaluationInputError("runner artifact does not match the scheduled engine")
    case = run.get("case")
    trial = run.get("trial")
    if not isinstance(case, dict) or not isinstance(trial, dict):
        raise EvaluationInputError("runner artifact lacks trial projection")
    if (case.get("task_id") != pair["task_id"]
            or case.get("task_digest") != seal["task_digests"].get(pair["task_id"])):
        raise EvaluationInputError("runner case does not match the frozen schedule")
    before_state = validate_state(before, expected_role="baseline")
    after_state = validate_state(after, expected_role="observation")
    if before_state.get("status") != "READY":
        raise EvaluationInputError("trial baseline is unavailable")
    run_status = run.get("status")
    apparatus = run.get("apparatus_code")
    if run_status == "apparatus_invalid":
        trial_status = "apparatus_invalid"
        evaluation_status = "INVALID"
        evaluation_digest = None
    elif run_status in {"completed", "failed", "indeterminate"}:
        trial_status = "valid"
        if (not isinstance(evaluation, dict)
                or evaluation.get("artifact_version") != 1
                or evaluation.get("kind") != "twenty_evaluation"):
            raise EvaluationInputError("valid runner outcome lacks a Twenty evaluation")
        evaluator_task = case.get("evaluator_task")
        task_kind = evaluator_task.get("kind") if isinstance(evaluator_task, dict) else None
        if not isinstance(task_kind, str) or evaluation.get("task_kind") != task_kind:
            raise EvaluationInputError("evaluation task kind does not match the runner case")
        if evaluation.get("before_digest") != before_state.get("digest"):
            raise EvaluationInputError("evaluation is bound to a different baseline")
        evaluation_status = evaluation.get("status")
        if evaluation_status not in {"PASS", "FAIL", "INDETERMINATE"}:
            raise EvaluationInputError("valid runner outcome lacks a usable evaluation")
        expected_after_digest = (
            after_state.get("digest") if after_state.get("status") == "READY" else None)
        if evaluation.get("after_digest") != expected_after_digest:
            raise EvaluationInputError("evaluation is bound to a different after-state")
        apparatus = None
        evaluation_digest = digest_json(evaluation)
    else:
        raise EvaluationInputError("runner status is invalid")
    if run_status != "completed" and not run.get("error_code"):
        raise EvaluationInputError("non-completed runner outcome lacks an error code")
    accounting = trial.get("accounting")
    if not isinstance(accounting, dict):
        raise EvaluationInputError("runner accounting projection is unavailable")
    terminal = run.get("error_code") or "completed"
    if not isinstance(terminal, str) or not _CODE.fullmatch(terminal):
        raise EvaluationInputError("runner terminal reason is invalid")
    evidence_required = trial_status == "valid"
    record = {
        "artifact_version": 1,
        "kind": "twenty_trial_record",
        "batch_id": batch_id,
        "run_id": run.get("run_id"),
        "protocol_id": protocol.get("protocol_id"),
        "protocol_digest": protocol.get("protocol_digest"),
        "schedule_position": pair["position"],
        "arm_order": arm_order,
        "comparison_id": pair["comparison_id"],
        "task_id": pair["task_id"],
        "task_digest": seal["task_digests"][pair["task_id"]],
        "arm_id": arm_id,
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "trial_status": trial_status,
        "apparatus_code": apparatus,
        "evaluation_status": evaluation_status,
        "evaluation_digest": evaluation_digest,
        "terminal_reason": terminal,
        "metrics": {
            key: accounting.get(key) for key in (
                "model_calls", "usage_unavailable_calls", "provider_reported_models",
                "provider_request_ids_digest", "known_input_tokens", "known_output_tokens",
                "known_cached_tokens", "known_reasoning_tokens", "known_cost_cny",
                "total_cost_cny")
        } | {
            "page_actions": trial.get("page_actions"),
            "business_calls": trial.get("business_calls"),
            "wall_ms": run.get("wall_ms"),
            "approvals_requested": trial.get("approvals_requested"),
            "approvals_approved": trial.get("approvals_approved"),
            "approvals_denied": trial.get("approvals_denied"),
            "automatic_retries": trial.get("automatic_retries"),
            "user_interventions": trial.get("user_interventions"),
        },
        "safety": trial.get("safety"),
        "evidence": {
            "session_ledger_digest": _digest(
                trial.get("session_ledger_digest"), "session ledger digest",
                optional=not evidence_required),
            "spend_ledger_slice_digest": _digest(
                accounting.get("spend_ledger_slice_digest"), "spend ledger slice digest",
                optional=not evidence_required),
            "before_state_digest": (
                _digest(before_state.get("digest"), "before-state digest")
                if evidence_required else None),
            "after_state_digest": (
                _digest(after_state.get("digest"), "after-state digest")
                if evidence_required else None),
            "public_answer_digest": _digest(
                trial.get("public_answer_digest"), "public answer digest",
                optional=not evidence_required),
            "runtime_trace_digest": _digest(
                trial.get("runtime_trace_digest"), "runtime trace digest",
                optional=not evidence_required),
        },
    }
    return validate_trial_record(record, protocol=protocol, test_seal=test_seal)
