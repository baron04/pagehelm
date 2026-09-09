from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evals.twenty.evaluator import EvaluationInputError
from evals.twenty.records import append_trial, initialize_batch
from evals.twenty.report import (
    MINIMUM_COMPARATIVE_TRIALS_PER_ARM, build_report, verify_report,
)
from evals.twenty.tests.test_records import frozen_artifacts, trial


BATCH_ID = "qualification-batch-v1"
_BANNED = ("PASS is better", "outperform", "NO-GAIN", "no gain", "superior")


class PublicReportTests(unittest.TestCase):
    def setUp(self):
        self.protocol, self.development, self.test_seal, self.split = frozen_artifacts()
        self.directory = tempfile.TemporaryDirectory()
        self.ledger = Path(self.directory.name) / "batch.jsonl"
        self.kwargs = {
            "protocol": self.protocol, "development_seal": self.development,
            "test_seal": self.test_seal, "split_verification": self.split,
        }
        initialize_batch(
            self.ledger, batch_id=BATCH_ID, created_at="2026-09-08T23:59:59+00:00",
            **self.kwargs)

    def tearDown(self):
        self.directory.cleanup()

    def _append(self, count: int, **overrides):
        for index in range(count):
            append_trial(
                self.ledger, trial(self.protocol, self.test_seal, index, **overrides),
                **self.kwargs)

    def test_report_recomputes_counts_and_labels_itself_non_comparative(self):
        self._append(4)
        report = build_report(ledger_path=self.ledger, **self.kwargs)

        self.assertEqual(verify_report(report)["status"], "READY")
        self.assertEqual(report["completed_trials"], 4)
        self.assertEqual(report["outcomes"]["PASS"], 4)
        self.assertEqual(report["valid_trials"], 4)
        self.assertEqual(report["batch_status"], "OPEN")
        self.assertEqual(report["stop_reason"], "batch_open")
        self.assertEqual(report["purpose"], "apparatus_qualification")
        self.assertFalse(report["comparative_claim_supported"])
        self.assertEqual(report["protocol_digest"], self.protocol["protocol_digest"])
        self.assertEqual(report["model_calls"], 4)
        self.assertEqual(report["safety_events"], 0)
        self.assertEqual(
            {arm["arm_id"] for arm in report["arms"]},
            {arm["arm_id"] for arm in self.protocol["arms"]})
        # The interpretation names these conclusions only to disclaim them; no other field
        # may carry a comparative or no-gain phrase.
        self.assertIn("does not establish", report["interpretation"])
        body = {key: value for key, value in report.items() if key != "interpretation"}
        rendered = json.dumps(body, ensure_ascii=False).casefold()
        for phrase in _BANNED:
            self.assertNotIn(phrase.casefold(), rendered)
        self.assertLess(4, MINIMUM_COMPARATIVE_TRIALS_PER_ARM)

    def test_report_never_exposes_run_ids_timing_detail_or_task_content(self):
        self._append(2)
        report = build_report(ledger_path=self.ledger, **self.kwargs)
        rendered = json.dumps(report, ensure_ascii=False)

        self.assertNotIn("run-001", rendered)
        self.assertNotIn("started_at", rendered)
        self.assertNotIn("finished_at", rendered)
        self.assertNotIn("evidence", rendered)
        self.assertNotIn("evaluation_digest", rendered)
        for task_id in self.test_seal["task_digests"]:
            self.assertNotIn(task_id, rendered)
        for digest in self.test_seal["task_digests"].values():
            self.assertNotIn(digest, rendered)

    def test_unknown_provider_usage_never_becomes_a_zero_total_cost(self):
        self._append(1, unknown_usage=True)
        report = build_report(ledger_path=self.ledger, **self.kwargs)

        self.assertEqual(report["usage_unavailable_calls"], 1)
        self.assertIsNone(report["total_cost_cny"])
        self.assertTrue(any(arm["total_cost_cny"] is None for arm in report["arms"]
                            if arm["trials"]))

    def test_a_sealed_or_stopped_batch_reports_its_stop_reason(self):
        self._append(1, status="apparatus_invalid")
        report = build_report(ledger_path=self.ledger, **self.kwargs)
        self.assertEqual(report["batch_status"], "SEALED_INVALID")
        self.assertEqual(report["stop_reason"], "apparatus_invalid")
        self.assertEqual(report["apparatus_codes"]["provider_outage"], 1)
        self.assertEqual(report["outcomes"]["INVALID"], 1)

        safety_ledger = Path(self.directory.name) / "safety.jsonl"
        initialize_batch(
            safety_ledger, batch_id=BATCH_ID, created_at="2026-09-08T23:59:59+00:00",
            **self.kwargs)
        append_trial(
            safety_ledger, trial(self.protocol, self.test_seal, 0, safety_event=True),
            **self.kwargs)
        stopped = build_report(ledger_path=safety_ledger, **self.kwargs)
        self.assertEqual(stopped["batch_status"], "STOPPED_SAFETY")
        self.assertEqual(stopped["stop_reason"], "safety_event")
        self.assertEqual(stopped["safety_events"], 1)
        self.assertEqual(stopped["safety"]["unauthorized_writes"], 1)

    def test_a_tampered_or_comparative_report_is_rejected(self):
        self._append(1)
        report = build_report(ledger_path=self.ledger, **self.kwargs)

        changed = {**report, "outcomes": {**report["outcomes"], "PASS": 99}}
        with self.assertRaisesRegex(EvaluationInputError, "digest"):
            verify_report(changed)

        claimed = {**report, "comparative_claim_supported": True}
        claimed.pop("report_digest")
        from evals.twenty.evaluator import digest_json

        claimed["report_digest"] = digest_json(claimed)
        with self.assertRaisesRegex(EvaluationInputError, "comparative claim"):
            verify_report(claimed)

        leaked = {**report, "private_prompt": "held-out content"}
        leaked.pop("report_digest")
        leaked["report_digest"] = digest_json(leaked)
        with self.assertRaisesRegex(EvaluationInputError, "unexpected fields"):
            verify_report(leaked)

        nested = {**report, "arms": [dict(arm) for arm in report["arms"]]}
        nested["arms"][0]["run_id"] = "private-run"
        nested.pop("report_digest")
        nested["report_digest"] = digest_json(nested)
        with self.assertRaisesRegex(EvaluationInputError, "arm schema"):
            verify_report(nested)


if __name__ == "__main__":
    unittest.main()
