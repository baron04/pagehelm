from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evals.twenty.driver import ScheduleDriverError, run_schedule
from evals.twenty.evaluator import EvaluationInputError, make_state
from evals.twenty.records import initialize_batch, verify_batch
from evals.twenty.tests.test_records import frozen_artifacts
from evals.twenty.tests.test_taskset import task_set


BATCH_ID = "driver-unit-batch-v1"
_DIGEST = "sha256:" + "a" * 64
FIXTURE = {
    "container": "twenty-test-db-1", "database": "default",
    "db_user": "postgres", "workspace_schema": "workspace_test",
}
COMPANY = {
    "id": "7e172546-a20e-4c7a-861c-a6478a944d37",
    "name": "Synthetic Acme", "domain": "acme.example.com",
    "created_at": "2026-09-08T01:00:00+00:00",
    "updated_at": "2026-09-08T01:00:00+00:00",
    "deleted_at": None,
}


class StubFixture:
    """Return an unchanging synthetic projection unless a test mutates ``companies``."""

    def __init__(self):
        self.companies = [dict(COMPANY)]
        self.captures = 0

    def capture(self, *, role: str) -> dict:
        self.captures += 1
        return make_state(
            fixture=FIXTURE, companies=self.companies,
            guards={"people": {"row_count": 0, "digest": _DIGEST},
                    "opportunities": {"row_count": 0, "digest": _DIGEST}},
            captured_at="2026-09-09T00:00:00+00:00", role=role)


class StubRunner:
    """Emit one valid shared projection for whichever engine the schedule asked for."""

    def __init__(self, protocol: dict, *, kind: str | None = None, status: str = "completed"):
        self.protocol = protocol
        self.kind = kind
        self.status = status
        self.calls: list[dict] = []

    def _engine_kind(self, arm_id: str) -> str:
        engine = next(arm["engine"] for arm in self.protocol["arms"]
                      if arm["arm_id"] == arm_id)
        return ("woa_twenty_run" if engine == "web_operation_agent"
                else "pageagent_twenty_run")

    def __call__(self, *, case, arm_id, task_id, schedule_position, run_id):
        self.calls.append({
            "arm_id": arm_id, "task_id": task_id, "position": schedule_position,
            "run_id": run_id, "kind": case["evaluator_task"]["kind"]})
        # Trials never overlap, and a resumed drive continues the same timeline, so the
        # interval comes from the driver-allocated run number rather than instance state.
        sequence = int(run_id.rsplit("-", 1)[1])
        started = datetime(2026, 9, 9, tzinfo=timezone.utc) + timedelta(
            seconds=2 * (sequence - 1))
        task_kind = case["evaluator_task"]["kind"]
        if self.status == "completed" and task_kind == "query":
            evidence = {
                "artifact_version": 1, "execution_status": "completed", "complete": True,
                "companies": [{"name": COMPANY["name"], "domain": COMPANY["domain"]}],
            }
        elif self.status == "completed" and task_kind == "open":
            evidence = {
                "artifact_version": 1, "execution_status": "completed",
                "page": {"path": f"/object/company/{COMPANY['id']}",
                         "name": COMPANY["name"], "domain": COMPANY["domain"]},
            }
        else:
            evidence = {"artifact_version": 1, "execution_status": "failed"}
        return {
            "artifact_version": 1,
            "kind": self.kind or self._engine_kind(arm_id),
            "status": self.status,
            "apparatus_code": None,
            "error_code": None if self.status == "completed" else "agent_refused",
            "run_id": run_id,
            "case": {**{key: case[key] for key in (
                "set_id", "split", "task_id", "task_digest", "template_family_digest")},
                "evaluator_task": {"kind": task_kind}},
            "evidence": evidence,
            "started_at": started.isoformat(),
            "finished_at": (started + timedelta(seconds=1)).isoformat(),
            "wall_ms": 1_000,
            "trial": {
                "accounting": {
                    "model_calls": 1, "usage_unavailable_calls": 0,
                    "provider_reported_models": ["stub-model"],
                    "provider_request_ids_digest": _DIGEST,
                    "known_input_tokens": 100, "known_output_tokens": 20,
                    "known_cached_tokens": 0, "known_reasoning_tokens": 5,
                    "known_cost_cny": 0.001, "total_cost_cny": 0.001,
                    "spend_ledger_slice_digest": _DIGEST,
                },
                "page_actions": 1, "business_calls": 1,
                "approvals_requested": 0, "approvals_approved": 0, "approvals_denied": 0,
                "automatic_retries": 0, "user_interventions": 0,
                "safety": {"unauthorized_writes": 0, "duplicate_writes": 0,
                           "forbidden_state_changes": 0, "cross_identity_actions": 0},
                "session_ledger_digest": _DIGEST, "public_answer_digest": _DIGEST,
                "runtime_trace_digest": _DIGEST,
            },
            "run_digest": _DIGEST,
        }


def _artifacts() -> tuple[dict, dict, dict, dict, dict]:
    """Freeze the shared test protocol together with the held-out task set it seals."""
    protocol, development, test_seal, split = frozen_artifacts()
    return protocol, development, test_seal, split, task_set("test", family_prefix="heldout")


def _first_write_pair(protocol: dict, tasks: dict) -> int:
    kinds = {task["task_id"]: task["kind"] for task in tasks["tasks"]}
    return next(index for index, pair in enumerate(protocol["schedule"])
                if kinds[pair["task_id"]] == "create")


class ScheduleDriverTests(unittest.TestCase):
    def setUp(self):
        (self.protocol, self.development, self.test_seal, self.split,
         self.tasks) = _artifacts()
        self.directory = tempfile.TemporaryDirectory()
        self.ledger = Path(self.directory.name) / "batch.jsonl"
        self.fixture = StubFixture()
        self.kwargs = {
            "protocol": self.protocol, "development_seal": self.development,
            "test_seal": self.test_seal, "split_verification": self.split,
        }
        initialize_batch(
            self.ledger, batch_id=BATCH_ID, created_at="2026-09-08T23:59:59+00:00",
            **self.kwargs)

    def tearDown(self):
        self.directory.cleanup()

    def _drive(self, runner=None, **overrides):
        selected = runner or StubRunner(self.protocol)
        options = {
            "task_set": self.tasks, "fixture": self.fixture,
            "runners": {arm["arm_id"]: selected for arm in self.protocol["arms"]},
            "ledger_path": self.ledger, "batch_id": BATCH_ID,
        }
        options.update(overrides)
        return run_schedule(**self.kwargs, **options), selected

    def test_trials_follow_the_frozen_pair_order_exactly_once(self):
        outcomes, runner = self._drive(max_trials=4)

        self.assertEqual(len(outcomes), 4)
        self.assertEqual([outcome.schedule_position for outcome in outcomes], [1, 1, 2, 2])
        self.assertEqual([outcome.arm_order for outcome in outcomes], [1, 2, 1, 2])
        expected = []
        for pair in self.protocol["schedule"][:2]:
            comparison = next(item for item in self.protocol["comparisons"]
                              if item["comparison_id"] == pair["comparison_id"])
            second = (comparison["right_arm"] if pair["first_arm"] == comparison["left_arm"]
                      else comparison["left_arm"])
            expected += [pair["first_arm"], second]
        self.assertEqual([outcome.arm_id for outcome in outcomes], expected)
        self.assertEqual([call["run_id"] for call in runner.calls], [
            f"{BATCH_ID}-{index:04d}" for index in range(1, 5)])
        self.assertEqual(verify_batch(self.ledger, **self.kwargs)["completed_trials"], 4)

    def test_driver_refuses_to_reorder_repeat_or_resume_from_the_wrong_point(self):
        self._drive(max_trials=2)
        for start_index in (0, 1, 3):
            with self.subTest(start_index=start_index), self.assertRaisesRegex(
                    ScheduleDriverError, "start index"):
                self._drive(max_trials=1, start_index=start_index)
        self.assertEqual(verify_batch(self.ledger, **self.kwargs)["completed_trials"], 2)

        outcomes, _runner = self._drive(max_trials=1, start_index=2)
        self.assertEqual(outcomes[0].schedule_position, 2)

    def test_wrong_engine_artifact_is_rejected_before_it_reaches_the_ledger(self):
        runner = StubRunner(self.protocol, kind="pageagent_twenty_run")
        engines = {arm["arm_id"]: arm["engine"] for arm in self.protocol["arms"]}
        first_arm = self.protocol["schedule"][0]["first_arm"]
        if engines[first_arm] != "web_operation_agent":
            runner.kind = "woa_twenty_run"
        with self.assertRaisesRegex(EvaluationInputError, "scheduled engine"):
            self._drive(runner=runner, max_trials=1)
        self.assertEqual(verify_batch(self.ledger, **self.kwargs)["completed_trials"], 0)

    def test_unbound_or_unknown_arms_stop_before_execution(self):
        runner = StubRunner(self.protocol)
        with self.assertRaisesRegex(ScheduleDriverError, "outside the frozen protocol"):
            run_schedule(
                **self.kwargs, task_set=self.tasks, fixture=self.fixture,
                runners={"invented-arm": runner}, ledger_path=self.ledger,
                batch_id=BATCH_ID)
        with self.assertRaisesRegex(ScheduleDriverError, "no runner is bound"):
            run_schedule(
                **self.kwargs, task_set=self.tasks, fixture=self.fixture,
                runners={arm["arm_id"]: runner for arm in self.protocol["arms"][:1]
                         if arm["arm_id"] != self.protocol["schedule"][0]["first_arm"]},
                ledger_path=self.ledger, batch_id=BATCH_ID)
        self.assertEqual(runner.calls, [])

    def test_a_batch_for_another_id_or_an_unavailable_baseline_is_refused(self):
        with self.assertRaisesRegex(ScheduleDriverError, "another batch"):
            self._drive(max_trials=1, batch_id="other-batch-v1")

        class UnavailableFixture(StubFixture):
            def capture(self, *, role: str) -> dict:
                from evals.twenty.evaluator import make_unavailable_state

                return make_unavailable_state(
                    fixture=FIXTURE, captured_at="2026-09-09T00:00:00+00:00", role=role)

        with self.assertRaisesRegex(ScheduleDriverError, "baseline is unavailable"):
            self._drive(max_trials=1, fixture=UnavailableFixture())

    def test_a_read_only_trial_that_changes_state_stops_the_driver(self):
        class DriftingFixture(StubFixture):
            def capture(self, *, role: str) -> dict:
                if role == "observation":
                    self.companies = []
                return super().capture(role=role)

        with self.assertRaisesRegex(ScheduleDriverError, "changed the fixture state"):
            self._drive(max_trials=1, fixture=DriftingFixture())

    def test_a_write_task_without_a_successful_restore_stops_the_driver(self):
        # Read-only pairs need no restore; the first scheduled create task does.
        before_write = _first_write_pair(self.protocol, self.tasks) * 2
        self._drive(max_trials=before_write)
        self.assertEqual(
            verify_batch(self.ledger, **self.kwargs)["completed_trials"], before_write)

        with self.assertRaisesRegex(ScheduleDriverError, "explicit restore"):
            self._drive(max_trials=1, start_index=before_write)
        self.assertEqual(
            verify_batch(self.ledger, **self.kwargs)["completed_trials"], before_write + 1)

        with self.assertRaisesRegex(ScheduleDriverError, "did not return to the baseline"):
            self._drive(max_trials=1, start_index=before_write + 1,
                        restore=lambda: {"status": "INDETERMINATE"})

        outcomes, _runner = self._drive(
            max_trials=1, start_index=before_write + 2, restore=lambda: {"status": "PASS"})
        self.assertEqual(len(outcomes), 1)

    def test_a_terminal_batch_can_never_be_continued(self):
        class SealingRunner(StubRunner):
            def __call__(self, **kwargs):
                run = super().__call__(**kwargs)
                run["status"] = "apparatus_invalid"
                run["apparatus_code"] = "provider_outage"
                run["error_code"] = "provider_http"
                run["trial"]["accounting"].update({
                    "model_calls": 1, "usage_unavailable_calls": 1,
                    "provider_reported_models": [], "total_cost_cny": None,
                    "known_input_tokens": 0, "known_output_tokens": 0,
                    "known_reasoning_tokens": 0, "known_cost_cny": 0.0,
                })
                return run

        outcomes, _runner = self._drive(runner=SealingRunner(self.protocol), max_trials=2)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].apparatus_code, "provider_outage")
        self.assertEqual(outcomes[0].batch_status, "SEALED_INVALID")
        with self.assertRaisesRegex(ScheduleDriverError, "terminal"):
            self._drive(max_trials=1, start_index=1)


if __name__ == "__main__":
    unittest.main()
