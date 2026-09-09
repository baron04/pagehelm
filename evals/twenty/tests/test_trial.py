from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evals.twenty.evaluator import digest_json, make_state
from evals.twenty.records import append_trial, initialize_batch, verify_batch
from evals.twenty.tests.test_records import frozen_artifacts
from evals.twenty.trial import project_trial_record


_DIGEST = "sha256:" + "a" * 64


def _state(role: str) -> dict:
    return make_state(
        fixture={
            "container": "fixture-db", "database": "fixture",
            "db_user": "postgres", "workspace_schema": "workspace_fixture",
        },
        companies=[{
            "id": "f45ee421-8a3e-4aa5-a1cf-7207cc6754e1",
            "name": "Synthetic Acme", "domain": "acme.test",
            "created_at": "2026-09-09T00:00:00+00:00",
            "updated_at": "2026-09-09T00:00:00+00:00",
            "deleted_at": None,
        }],
        guards={
            "people": {"row_count": 0, "digest": _DIGEST},
            "opportunities": {"row_count": 0, "digest": _DIGEST},
        },
        captured_at="2026-09-09T00:00:00+00:00", role=role)


def _arm(protocol: dict, index: int) -> str:
    pair = protocol["schedule"][index // 2]
    comparison = next(
        item for item in protocol["comparisons"]
        if item["comparison_id"] == pair["comparison_id"])
    if index % 2 == 0:
        return pair["first_arm"]
    return (comparison["right_arm"]
            if pair["first_arm"] == comparison["left_arm"]
            else comparison["left_arm"])


def _run(protocol: dict, test_seal: dict, index: int, kind: str) -> dict:
    pair = protocol["schedule"][index // 2]
    arm = next(item for item in protocol["arms"] if item["arm_id"] == _arm(protocol, index))
    started = datetime(2026, 9, 9, tzinfo=timezone.utc) + timedelta(seconds=index * 2)
    accounting = {
        "model_calls": 1,
        "usage_unavailable_calls": 0,
        "provider_reported_models": ["qwen3.8-flash"],
        "provider_request_ids_digest": _DIGEST,
        "known_input_tokens": 100,
        "known_output_tokens": 20,
        "known_cached_tokens": 0,
        "known_reasoning_tokens": 5,
        "known_cost_cny": 0.001,
        "total_cost_cny": 0.001,
        "spend_ledger_slice_digest": _DIGEST,
    }
    return {
        "artifact_version": 1,
        "kind": kind,
        "run_id": f"projected-run-{index + 1}",
        "status": "completed",
        "apparatus_code": None,
        "error_code": None,
        "engine": {
            "name": arm["engine"], "release": arm["engine_release"],
            "capability_profile": arm["capability_profile"],
            "source_mode": arm["source_mode"],
        },
        "case": {
            "task_id": pair["task_id"],
            "task_digest": test_seal["task_digests"][pair["task_id"]],
            "evaluator_task": {"kind": "query"},
        },
        "started_at": started.isoformat(),
        "finished_at": (started + timedelta(seconds=1)).isoformat(),
        "wall_ms": 1_000,
        "trial": {
            "accounting": accounting,
            "page_actions": 1,
            "business_calls": 1,
            "approvals_requested": 0,
            "approvals_approved": 0,
            "approvals_denied": 0,
            "automatic_retries": 0,
            "user_interventions": 0,
            "safety": {
                "unauthorized_writes": 0,
                "duplicate_writes": 0,
                "forbidden_state_changes": 0,
                "cross_identity_actions": 0,
            },
            "session_ledger_digest": _DIGEST,
            "public_answer_digest": _DIGEST,
            "runtime_trace_digest": _DIGEST,
        },
    }


class TrialProjectionTests(unittest.TestCase):
    def test_pageagent_and_woa_runs_share_one_ledger_projection(self):
        protocol, development, test_seal, split = frozen_artifacts()
        before, after = _state("baseline"), _state("observation")
        evaluation = {
            "artifact_version": 1, "kind": "twenty_evaluation",
            "status": "PASS", "task_kind": "query", "checks": {"passed": True},
            "before_digest": before["digest"], "after_digest": after["digest"],
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "qualification.jsonl"
            kwargs = {
                "protocol": protocol, "development_seal": development,
                "test_seal": test_seal, "split_verification": split,
            }
            initialize_batch(
                ledger, batch_id="qualification-batch-v1",
                created_at="2026-09-08T23:59:59+00:00", **kwargs)
            kinds = []
            for index in range(len(protocol["schedule"]) * 2):
                arm_id = _arm(protocol, index)
                engine = next(
                    arm["engine"] for arm in protocol["arms"]
                    if arm["arm_id"] == arm_id)
                kind = ("woa_twenty_run" if engine == "web_operation_agent"
                        else "pageagent_twenty_run")
                kinds.append(kind)
                record = project_trial_record(
                    protocol=protocol, test_seal=test_seal,
                    batch_id="qualification-batch-v1", schedule_index=index,
                    arm_id=arm_id, run=_run(protocol, test_seal, index, kind),
                    evaluation=evaluation, before=before, after=after)
                self.assertEqual(record["evaluation_digest"], digest_json(evaluation))
                append_trial(ledger, record, **kwargs)
            status = verify_batch(ledger, **kwargs)
            self.assertEqual(status["completed_trials"], len(protocol["schedule"]) * 2)
            self.assertEqual(status["outcomes"]["PASS"], len(protocol["schedule"]) * 2)
            self.assertEqual(set(kinds), {"pageagent_twenty_run", "woa_twenty_run"})

    def test_runner_kind_must_match_the_scheduled_engine(self):
        protocol, _development, test_seal, _split = frozen_artifacts()
        before, after = _state("baseline"), _state("observation")
        evaluation = {
            "artifact_version": 1, "kind": "twenty_evaluation",
            "status": "PASS", "task_kind": "query", "checks": {"passed": True},
            "before_digest": before["digest"], "after_digest": after["digest"],
        }
        index = next(
            candidate for candidate in range(len(protocol["schedule"]) * 2)
            if next(
                arm["engine"] for arm in protocol["arms"]
                if arm["arm_id"] == _arm(protocol, candidate))
            == "web_operation_agent")
        run = _run(protocol, test_seal, index, "pageagent_twenty_run")
        with self.assertRaisesRegex(ValueError, "scheduled engine"):
            project_trial_record(
                protocol=protocol, test_seal=test_seal,
                batch_id="qualification-batch-v1", schedule_index=index,
                arm_id=_arm(protocol, index), run=run, evaluation=evaluation,
                before=before, after=after)

    def test_runner_capabilities_must_match_the_frozen_arm(self):
        protocol, _development, test_seal, _split = frozen_artifacts()
        before, after = _state("baseline"), _state("observation")
        evaluation = {
            "artifact_version": 1, "kind": "twenty_evaluation",
            "status": "PASS", "task_kind": "query", "checks": {"passed": True},
            "before_digest": before["digest"], "after_digest": after["digest"],
        }
        index = 0
        arm_id = _arm(protocol, index)
        engine = next(arm["engine"] for arm in protocol["arms"] if arm["arm_id"] == arm_id)
        kind = "woa_twenty_run" if engine == "web_operation_agent" else "pageagent_twenty_run"
        run = _run(protocol, test_seal, index, kind)
        run["engine"]["source_mode"] = "on" if run["engine"]["source_mode"] == "off" else "off"
        with self.assertRaisesRegex(ValueError, "frozen arm"):
            project_trial_record(
                protocol=protocol, test_seal=test_seal,
                batch_id="qualification-batch-v1", schedule_index=index,
                arm_id=arm_id, run=run, evaluation=evaluation,
                before=before, after=after)

    def test_evaluation_must_be_bound_to_the_same_fixture_states(self):
        protocol, _development, test_seal, _split = frozen_artifacts()
        before, after = _state("baseline"), _state("observation")
        index = 0
        arm_id = _arm(protocol, index)
        engine = next(
            arm["engine"] for arm in protocol["arms"]
            if arm["arm_id"] == arm_id)
        kind = ("woa_twenty_run" if engine == "web_operation_agent"
                else "pageagent_twenty_run")
        evaluation = {
            "artifact_version": 1, "kind": "twenty_evaluation",
            "status": "PASS", "task_kind": "query", "checks": {"passed": True},
            "before_digest": "sha256:" + "f" * 64,
            "after_digest": after["digest"],
        }
        with self.assertRaisesRegex(ValueError, "different baseline"):
            project_trial_record(
                protocol=protocol, test_seal=test_seal,
                batch_id="qualification-batch-v1", schedule_index=index,
                arm_id=arm_id, run=_run(protocol, test_seal, index, kind),
                evaluation=evaluation, before=before, after=after)


if __name__ == "__main__":
    unittest.main()
