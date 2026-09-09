"""One serial driver for every engine in a frozen Twenty protocol.

The driver owns the order: for each scheduled pair it captures the before state, instantiates
the task against that baseline, calls one engine's runner, captures the after state, evaluates
with the independent evaluator, projects the common trial record and appends it to the
append-only batch ledger. It contains no PageAgent or Web Operation Agent special case; an
engine appears only as a runner adapter supplied by the caller. It never selects, reorders,
parallelizes or silently retries a trial, and an interrupted trial is never recorded as
completed.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .evaluator import EvaluationInputError, evaluate, validate_state
from .protocol import verify_protocol
from .records import append_trial, verify_batch
from .taskset import instantiate_task, validate_task_set
from .trial import project_trial_record


DRIVER_VERSION = 1
_TERMINAL_BATCH_STATUSES = frozenset({"SEALED_INVALID", "STOPPED_SAFETY", "COMPLETE"})


class ScheduleDriverError(EvaluationInputError):
    """The driver inputs or the observed run order cannot support a batch."""


class RunnerAdapter(Protocol):
    """Execute one instantiated case for one arm and return that engine's run artifact."""

    def __call__(self, *, case: dict[str, Any], arm_id: str, task_id: str,
                 schedule_position: int, run_id: str) -> dict[str, Any]: ...


class FixtureController(Protocol):
    """Capture and reset the isolated application state around one trial."""

    def capture(self, *, role: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class TrialOutcome:
    schedule_position: int
    arm_order: int
    arm_id: str
    task_id: str
    trial_status: str
    evaluation_status: str
    terminal_reason: str
    apparatus_code: str | None
    run_digest: str | None
    batch_status: str


def _reset_required(task_kind: str) -> bool:
    """A create task changes business state, so it needs a real restore before the next trial."""
    return task_kind == "create"


def _arm_for(protocol: Mapping[str, Any], index: int) -> tuple[dict[str, Any], int, str]:
    schedule = protocol["schedule"]
    pair = schedule[index // 2]
    arm_order = index % 2 + 1
    comparison = next(item for item in protocol["comparisons"]
                      if item["comparison_id"] == pair["comparison_id"])
    second = (comparison["right_arm"]
              if pair["first_arm"] == comparison["left_arm"] else comparison["left_arm"])
    return pair, arm_order, pair["first_arm"] if arm_order == 1 else second


def _ready_state(value: Any, *, role: str, what: str) -> dict[str, Any]:
    state = validate_state(value, expected_role=role)
    if state.get("status") != "READY":
        raise ScheduleDriverError(f"{what} is unavailable")
    return state


def _apparatus_fault(run: dict[str, Any], code: str) -> dict[str, Any]:
    """Preserve run accounting while making a driver-observed apparatus fault terminal."""
    return {
        **run,
        "status": "apparatus_invalid",
        "apparatus_code": code,
        "error_code": code,
        "evidence": {"artifact_version": 1, "execution_status": "failed"},
        "run_digest": None,
    }


def _forbidden_state_change(run: dict[str, Any]) -> dict[str, Any]:
    """Record a read-only state mutation in the common safety projection."""
    trial = dict(run.get("trial") or {})
    safety = dict(trial.get("safety") or {})
    safety["forbidden_state_changes"] = safety.get("forbidden_state_changes", 0) + 1
    return {**run, "trial": {**trial, "safety": safety}, "run_digest": None}


def run_schedule(*, protocol: Mapping[str, Any], development_seal: Mapping[str, Any],
                 test_seal: Mapping[str, Any], split_verification: Mapping[str, Any],
                 task_set: Mapping[str, Any], fixture: FixtureController,
                 runners: Mapping[str, RunnerAdapter], ledger_path: str | Path,
                 batch_id: str, restore: Callable[[], dict[str, Any]] | None = None,
                 start_index: int = 0, max_trials: int | None = None) -> list[TrialOutcome]:
    """Execute the frozen schedule serially and append each completed trial exactly once."""
    verify_protocol(
        protocol, development_seal=development_seal, test_seal=test_seal,
        split_verification=split_verification)
    tasks = validate_task_set(task_set)
    ledger_kwargs = {
        "protocol": protocol, "development_seal": development_seal,
        "test_seal": test_seal, "split_verification": split_verification,
    }
    status = verify_batch(ledger_path, **ledger_kwargs)
    if status["status"] in _TERMINAL_BATCH_STATUSES:
        raise ScheduleDriverError("batch ledger is already terminal")
    if status["batch_id"] != batch_id:
        raise ScheduleDriverError("batch ledger belongs to another batch")
    completed = status["completed_trials"]
    if completed != start_index:
        raise ScheduleDriverError("driver start index does not continue the batch ledger")
    expected = status["expected_trials"]
    if (max_trials is not None and (isinstance(max_trials, bool)
                                    or not isinstance(max_trials, int) or max_trials < 1)):
        raise ScheduleDriverError("max_trials must be a positive integer or None")
    limit = expected if max_trials is None else min(expected, completed + max_trials)
    if set(runners) - {arm["arm_id"] for arm in protocol["arms"]}:
        raise ScheduleDriverError("a runner is bound to an arm outside the frozen protocol")

    outcomes: list[TrialOutcome] = []
    for index in range(completed, limit):
        pair, arm_order, arm_id = _arm_for(protocol, index)
        runner = runners.get(arm_id)
        if runner is None:
            raise ScheduleDriverError("no runner is bound to the next scheduled arm")
        before = fixture.capture(role="baseline")
        before_state = _ready_state(before, role="baseline", what="trial baseline")
        if arm_order == 2:
            pair_baseline = status.get("open_pair_baseline_digest")
            if not isinstance(pair_baseline, str) or before_state["digest"] != pair_baseline:
                raise ScheduleDriverError("paired trial baseline does not match the first arm")
        case = instantiate_task(tasks, pair["task_id"], before)
        task_kind = case["evaluator_task"]["kind"]
        if _reset_required(task_kind) and restore is None:
            raise ScheduleDriverError("a write task requires an explicit restore")
        run_id = f"{batch_id}-{index + 1:04d}"
        run = runner(case=case, arm_id=arm_id, task_id=pair["task_id"],
                     schedule_position=pair["position"], run_id=run_id)
        after = fixture.capture(role="observation")
        if not isinstance(run, dict):
            raise ScheduleDriverError("runner did not return a run artifact")
        after_state = validate_state(after, expected_role="observation")
        evaluation = evaluate(
            task=case["evaluator_task"], before=before, after=after,
            evidence=run.get("evidence"))
        selected_run = run
        after_unavailable = after_state.get("status") != "READY"
        if _reset_required(task_kind):
            assert restore is not None
            reset_failed = False
            try:
                outcome = restore()
                restored = validate_state(
                    fixture.capture(role="baseline"), expected_role="baseline")
                reset_failed = bool(
                    not isinstance(outcome, dict) or outcome.get("status") != "PASS"
                    or restored.get("status") != "READY"
                    or restored.get("digest") != before_state["digest"])
            except Exception:
                reset_failed = True
            if reset_failed:
                selected_run = _apparatus_fault(run, "fixture_reset_mismatch")
            elif after_unavailable:
                selected_run = _apparatus_fault(run, "fixture_state_unavailable")
        elif after_unavailable:
            selected_run = _apparatus_fault(run, "fixture_state_unavailable")
        elif after_state["digest"] != before_state["digest"]:
            selected_run = _forbidden_state_change(run)
        record = project_trial_record(
            protocol=protocol, test_seal=test_seal, batch_id=batch_id,
            schedule_index=index, arm_id=arm_id, run=selected_run, evaluation=evaluation,
            before=before, after=after)
        # Append a normal record only after the next arm's state is known to be clean. A failed
        # reset or read-only mutation is instead recorded as terminal, preserving accounting
        # while preventing continuation against contaminated or unknown state.
        summary = append_trial(ledger_path, record, **ledger_kwargs)
        status = summary
        outcomes.append(TrialOutcome(
            schedule_position=pair["position"], arm_order=arm_order, arm_id=arm_id,
            task_id=pair["task_id"], trial_status=record["trial_status"],
            evaluation_status=record["evaluation_status"],
            terminal_reason=record["terminal_reason"],
            apparatus_code=record["apparatus_code"],
            run_digest=(selected_run.get("run_digest")
                        if isinstance(selected_run.get("run_digest"), str) else None),
            batch_status=summary["status"]))
        if summary["status"] in _TERMINAL_BATCH_STATUSES:
            break
    return outcomes
