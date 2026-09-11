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

    def test_report_aggregates_wall_time_cost_and_approvals_per_arm(self):
        self._append(4)
        report = build_report(ledger_path=self.ledger, **self.kwargs)

        populated = [arm for arm in report["arms"] if arm["trials"]]
        self.assertTrue(populated)
        for arm in populated:
            self.assertEqual(arm["wall_ms"]["median"], 1000.0)
            self.assertEqual(arm["known_cost_cny_per_trial"]["count"], arm["trials"])
            self.assertEqual(arm["known_cost_cny_per_trial"]["median"], 0.001)
            self.assertEqual(arm["approvals_requested"], 0)
            self.assertIn(arm["approval_boundary"], {"none", "page_dialog", "server_proposal"})
            self.assertIn(arm["mutation_journal"], {"none", "volatile", "durable"})

    def test_report_counts_paired_disagreement_without_naming_a_winner(self):
        """Discordance is the variance input for power planning, not an effect estimate."""
        # Pair 1: both arms pass. Pair 2: the second arm fails. Pair 3: one arm is invalid.
        for index in range(6):
            append_trial(
                self.ledger,
                trial(self.protocol, self.test_seal, index,
                      status="apparatus_invalid" if index == 5 else "valid",
                      evaluation="FAIL" if index == 3 else "PASS"),
                **self.kwargs)
        report = build_report(ledger_path=self.ledger, **self.kwargs)

        self.assertEqual(verify_report(report)["status"], "READY")
        by_id = {row["comparison_id"]: row for row in report["comparisons"]}
        self.assertEqual(set(by_id), {c["comparison_id"] for c in self.protocol["comparisons"]})
        touched = [self.protocol["schedule"][position]["comparison_id"] for position in range(3)]
        totals = {
            key: sum(by_id[cid][key] for cid in set(touched))
            for key in ("pairs_completed", "pairs_both_valid", "concordant_pairs",
                        "discordant_pairs")
        }
        self.assertEqual(totals, {"pairs_completed": 3, "pairs_both_valid": 2,
                                  "concordant_pairs": 1, "discordant_pairs": 1})
        discordant = by_id[touched[1]]
        self.assertEqual(discordant["discordance_rate"],
                         round(discordant["discordant_pairs"]
                               / discordant["pairs_both_valid"], 6))
        for row in report["comparisons"]:
            self.assertEqual(row["pairs_scheduled"], sum(
                pair["comparison_id"] == row["comparison_id"]
                for pair in self.protocol["schedule"]))
            self.assertIn(row["approval_boundary"], {"equivalent", "capability_only"})
            self.assertFalse({"left_pass", "right_pass", "winner", "effect"} & set(row))
        skill = next(row for row in report["comparisons"]
                     if row["comparison_type"] == "skill_api")
        self.assertEqual(skill["approval_boundary"], "capability_only")

        nested = {**report, "comparisons": [dict(row) for row in report["comparisons"]]}
        nested["comparisons"][0]["left_arm_passes"] = 1
        nested.pop("report_digest")
        from evals.twenty.evaluator import digest_json

        nested["report_digest"] = digest_json(nested)
        with self.assertRaisesRegex(EvaluationInputError, "comparison schema"):
            verify_report(nested)

    def test_report_tallies_outcomes_per_task_kind_so_refusals_can_be_counted(self):
        # Pair 1 is a query pair; pair 2 is a refuse pair where the second arm did not refuse.
        for index in range(4):
            append_trial(
                self.ledger,
                trial(self.protocol, self.test_seal, index,
                      task_kind="refuse" if index >= 2 else "query",
                      evaluation="FAIL" if index == 3 else "PASS"),
                **self.kwargs)
        report = build_report(ledger_path=self.ledger, **self.kwargs)

        self.assertEqual(verify_report(report)["status"], "READY")
        self.assertEqual(report["outcomes_by_task_kind"]["refuse"],
                         {"PASS": 1, "FAIL": 1, "INDETERMINATE": 0, "INVALID": 0})
        self.assertEqual(report["outcomes_by_task_kind"]["query"]["PASS"], 2)
        per_arm = {kind for arm in report["arms"] for kind in arm["outcomes_by_task_kind"]}
        self.assertEqual(per_arm, {"query", "refuse"})

        mixed = trial(self.protocol, self.test_seal, 4, task_kind="open")
        append_trial(self.ledger, mixed, **self.kwargs)
        with self.assertRaisesRegex(EvaluationInputError, "task kind"):
            append_trial(self.ledger, trial(self.protocol, self.test_seal, 5, task_kind="query"),
                         **self.kwargs)

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
