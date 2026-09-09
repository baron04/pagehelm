from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evals.twenty.evaluator import EvaluationInputError
from evals.twenty.protocol import freeze_protocol
from evals.twenty.records import append_trial, initialize_batch, verify_batch
from evals.twenty.tests.test_protocol import artifacts, protocol_draft


def frozen_artifacts() -> tuple[dict, dict, dict, dict]:
    development, test, split = artifacts()
    protocol = freeze_protocol(
        protocol_draft(), development_seal=development,
        test_seal=test, split_verification=split)
    return protocol, development, test, split


def trial(protocol: dict, test: dict, index: int, *,
          status: str = "valid", models: list[str] | None = None,
          unknown_usage: bool = False, safety_event: bool = False) -> dict:
    pair = protocol["schedule"][index // 2]
    comparison = next(
        item for item in protocol["comparisons"]
        if item["comparison_id"] == pair["comparison_id"])
    arm_order = index % 2 + 1
    second_arm = (
        comparison["right_arm"]
        if pair["first_arm"] == comparison["left_arm"] else comparison["left_arm"])
    arm_id = pair["first_arm"] if arm_order == 1 else second_arm
    started = datetime(2026, 9, 9, tzinfo=timezone.utc) + timedelta(seconds=index * 2)
    finished = started + timedelta(seconds=1)
    valid = status == "valid"
    model_calls = 1 if valid else 0
    selected_models = (["qwen3.8-flash"] if models is None else models) if model_calls else []
    evidence_digest = "sha256:" + "a" * 64
    return {
        "artifact_version": 1,
        "kind": "twenty_trial_record",
        "batch_id": "qualification-batch-v1",
        "run_id": f"run-{index + 1:03d}",
        "protocol_id": protocol["protocol_id"],
        "protocol_digest": protocol["protocol_digest"],
        "schedule_position": pair["position"],
        "arm_order": arm_order,
        "comparison_id": pair["comparison_id"],
        "task_id": pair["task_id"],
        "task_digest": test["task_digests"][pair["task_id"]],
        "arm_id": arm_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "trial_status": status,
        "apparatus_code": None if valid else "provider_outage",
        "evaluation_status": "PASS" if valid else "INVALID",
        "evaluation_digest": evidence_digest if valid else None,
        "terminal_reason": "completed" if valid else "provider_unavailable",
        "metrics": {
            "model_calls": model_calls,
            "usage_unavailable_calls": 1 if unknown_usage else 0,
            "provider_reported_models": selected_models,
            "provider_request_ids_digest": evidence_digest if model_calls else None,
            "known_input_tokens": 100 if model_calls and not unknown_usage else 0,
            "known_output_tokens": 20 if model_calls and not unknown_usage else 0,
            "known_cached_tokens": 0,
            "known_reasoning_tokens": 5 if model_calls and not unknown_usage else 0,
            "known_cost_cny": 0.001 if model_calls and not unknown_usage else 0,
            "total_cost_cny": None if unknown_usage else (
                0.001 if model_calls else 0),
            "page_actions": 1 if valid else 0,
            "business_calls": 0,
            "wall_ms": 1000,
            "approvals_requested": 0,
            "approvals_approved": 0,
            "approvals_denied": 0,
            "automatic_retries": 0,
            "user_interventions": 0,
        },
        "safety": {
            "unauthorized_writes": 1 if safety_event else 0,
            "duplicate_writes": 0,
            "forbidden_state_changes": 0,
            "cross_identity_actions": 0,
        },
        "evidence": {
            "session_ledger_digest": evidence_digest if valid else None,
            "spend_ledger_slice_digest": evidence_digest if valid else None,
            "before_state_digest": evidence_digest if valid else None,
            "after_state_digest": evidence_digest if valid else None,
            "public_answer_digest": evidence_digest if valid else None,
            "runtime_trace_digest": evidence_digest if valid else None,
        },
    }


class BatchRecordTests(unittest.TestCase):
    def setUp(self):
        self.protocol, self.development, self.test, self.split = frozen_artifacts()
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "batch.jsonl"
        self.kwargs = {
            "protocol": self.protocol,
            "development_seal": self.development,
            "test_seal": self.test,
            "split_verification": self.split,
        }
        initialize_batch(
            self.path, batch_id="qualification-batch-v1",
            created_at="2026-09-08T23:59:59+00:00", **self.kwargs)

    def tearDown(self):
        self.directory.cleanup()

    def test_append_is_private_chained_and_bound_to_the_frozen_schedule(self):
        result = append_trial(self.path, trial(self.protocol, self.test, 0), **self.kwargs)
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["completed_trials"], 1)
        self.assertEqual(result["outcomes"]["PASS"], 1)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(verify_batch(self.path, **self.kwargs), result)

        lines = self.path.read_text().splitlines()
        entry = json.loads(lines[1])
        entry["record"]["terminal_reason"] = "tampered"
        lines[1] = json.dumps(entry)
        self.path.write_text("\n".join(lines) + "\n")
        os.chmod(self.path, 0o600)
        with self.assertRaisesRegex(EvaluationInputError, "chain"):
            verify_batch(self.path, **self.kwargs)

    def test_wrong_next_arm_is_rejected_without_modifying_the_ledger(self):
        record = trial(self.protocol, self.test, 0)
        record["arm_id"] = "pageagent-skill" if record["arm_id"] != "pageagent-skill" else "woa-browser"
        before = self.path.read_bytes()
        with self.assertRaisesRegex(EvaluationInputError, "next frozen schedule"):
            append_trial(self.path, record, **self.kwargs)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_usage_is_never_reported_as_zero_total_cost(self):
        record = trial(self.protocol, self.test, 0, unknown_usage=True)
        result = append_trial(self.path, record, **self.kwargs)
        self.assertIsNone(result["total_cost_cny"])
        self.assertEqual(result["usage_unavailable_calls"], 1)

        other_path = Path(self.directory.name) / "bad-cost.jsonl"
        initialize_batch(
            other_path, batch_id="qualification-batch-v1",
            created_at="2026-09-08T23:59:59+00:00", **self.kwargs)
        record["metrics"]["total_cost_cny"] = 0
        with self.assertRaisesRegex(EvaluationInputError, "unknown usage"):
            append_trial(other_path, record, **self.kwargs)

    def test_pair_model_drift_is_rejected_before_append(self):
        append_trial(self.path, trial(self.protocol, self.test, 0), **self.kwargs)
        before = self.path.read_bytes()
        second = trial(self.protocol, self.test, 1, models=["changed-snapshot"])
        with self.assertRaisesRegex(EvaluationInputError, "different provider model"):
            append_trial(self.path, second, **self.kwargs)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(verify_batch(self.path, **self.kwargs)["completed_trials"], 1)

    def test_apparatus_invalid_and_safety_event_make_the_batch_terminal(self):
        invalid = append_trial(
            self.path, trial(self.protocol, self.test, 0, status="apparatus_invalid"),
            **self.kwargs)
        self.assertEqual(invalid["status"], "SEALED_INVALID")
        with self.assertRaisesRegex(EvaluationInputError, "terminal"):
            append_trial(self.path, trial(self.protocol, self.test, 1), **self.kwargs)

        safety_path = Path(self.directory.name) / "safety.jsonl"
        initialize_batch(
            safety_path, batch_id="qualification-batch-v1",
            created_at="2026-09-08T23:59:59+00:00", **self.kwargs)
        stopped = append_trial(
            safety_path, trial(self.protocol, self.test, 0, safety_event=True),
            **self.kwargs)
        self.assertEqual(stopped["status"], "STOPPED_SAFETY")

    def test_provider_outage_can_be_sealed_without_an_impossible_model_snapshot(self):
        record = trial(self.protocol, self.test, 0, status="apparatus_invalid")
        record["metrics"].update({
            "model_calls": 1,
            "usage_unavailable_calls": 1,
            "provider_reported_models": [],
            "provider_request_ids_digest": "sha256:" + "c" * 64,
            "total_cost_cny": None,
        })
        result = append_trial(self.path, record, **self.kwargs)
        self.assertEqual(result["status"], "SEALED_INVALID")
        self.assertEqual(result["usage_unavailable_calls"], 1)
        self.assertIsNone(result["total_cost_cny"])

    def test_valid_trial_cannot_omit_its_provider_model_snapshot(self):
        record = trial(self.protocol, self.test, 0, unknown_usage=True)
        record["metrics"]["provider_reported_models"] = []
        with self.assertRaisesRegex(EvaluationInputError, "reported-model"):
            append_trial(self.path, record, **self.kwargs)

    def test_existing_batch_is_never_overwritten(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(EvaluationInputError, "already exists"):
            initialize_batch(
                self.path, batch_id="qualification-batch-v1",
                created_at="2026-09-08T23:59:59+00:00", **self.kwargs)
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_duplicate_append_has_exactly_one_winner(self):
        record = trial(self.protocol, self.test, 0)

        def attempt():
            try:
                append_trial(self.path, record, **self.kwargs)
                return "appended"
            except EvaluationInputError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(outcomes, ["appended", "rejected"])
        self.assertEqual(verify_batch(self.path, **self.kwargs)["completed_trials"], 1)


if __name__ == "__main__":
    unittest.main()
