"""The batch entry, end to end on the batch world: every slot, every call accounted, the report
gated on the header the batch declared, and the held-out rules."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from evals.odoo import batch, fixture, ledger, protocol
from evals.odoo.tests.batch_world import DIGEST, BatchWorld, DriverDied, git


class BatchEntryTests(BatchWorld):
    def run_batch(self, population="development"):
        return batch.run(self.paths, confirm_paid_calls=True, population=population)

    def test_a_batch_runs_every_slot_accounts_every_call_and_is_gated(self):
        result = self.run_batch()
        self.assertEqual((result["status"], result["attempts"]), ("COMPLETE", 60))
        rows = [json.loads(line) for line in self.paths.spend().read_text().splitlines()]
        self.assertEqual(len(rows), self.provider.product_calls)
        self.assertEqual(result["model_calls"], self.provider.product_calls)
        value = json.loads(Path(result["report"]).read_text())
        (entry,) = value["batches"]
        self.assertEqual((entry["verdict_status"], entry["verdict_basis"]), ("gated", []))
        primary = entry["arms"]["woa_product"]["replicates"]["primary"]["normal"]
        self.assertEqual(primary["gate_verdict"], "PASS")

    def test_it_refuses_before_anything_is_sent(self):
        with self.assertRaisesRegex(batch.BatchError, "confirm-paid-calls"):
            batch.run(self.paths, confirm_paid_calls=False)
        self.qualify(self.candidate(), status="FAILED",
                     name="p74a-qualification-20260918T010000Z.json")
        with self.assertRaisesRegex(batch.BatchError, "did not pass"):
            self.run_batch()
        self.assertEqual(self.provider.product_calls, 0)
        self.assertEqual(batch.batches(self.paths), [])

    def test_a_fixture_left_uncertain_is_rearmed_before_a_batch_runs(self):
        recovery = self.private / "recovery" / "recovery.json"
        record = json.loads(recovery.read_text())
        record = fixture._seal({key: value for key, value in record.items()
                                if key != "record_digest"} | {"status": "INDETERMINATE"})
        recovery.write_text(json.dumps(record))
        with self.assertRaisesRegex(batch.BatchError, "INDETERMINATE.*rearm"):
            self.run_batch()
        with self.assertRaisesRegex(batch.BatchError, "confirm-rearm"):
            batch.rearm(self.paths, confirm_rearm=False)
        calls = []
        restore = self.restore

        def recording(*args, **kwargs):
            calls.append(kwargs["rearm_after_failure"])
            return restore(*args, **kwargs)

        with mock.patch.object(fixture, "restore", recording):
            self.assertEqual(batch.rearm(self.paths, confirm_rearm=True)["restored"], True)
        self.assertEqual(calls, [True])
        self.assertEqual(self.provider.product_calls, 0)

    def test_an_interrupted_batch_is_resumed_not_restarted(self):
        self.world.die_on = {3}
        with self.assertRaises(DriverDied):
            self.run_batch()
        (open_one,) = batch.batches(self.paths)
        result = self.run_batch()
        self.assertEqual((result["batch_id"], result["status"]),
                         (open_one[2]["batch"]["batch_id"], "COMPLETE"))
        entries, summary = ledger.read(open_one[0])
        self.assertIn("interrupted_attempt", [entry["kind"] for entry in entries])
        self.assertEqual(sum(1 for item in summary["attempts"] if item["replacement_of"]), 1)

    def test_a_stop_seals_the_batch_before_its_next_attempt(self):
        self.world.before_call = {2: lambda: self.assertEqual(
            batch.stop(self.paths)["status"], "STOP_REQUESTED")}
        result = self.run_batch()
        self.assertEqual((result["status"], result["seal_reason"], result["attempts"]),
                         ("SEALED", "operator_stop", 2))
        self.assertFalse(self.paths.stop_request.exists())

    def test_the_heldout_set_runs_once_per_candidate_and_blind(self):
        seen = {}

        def midway():
            seen["status"] = batch.status(self.paths)["batches"][0]
            with self.assertRaisesRegex(batch.BatchError, "still open"):
                batch.build(self.paths, seen["status"]["batch_id"])

        self.world.before_call = {3: midway}
        result = self.run_batch("heldout")
        self.assertEqual(result["status"], "COMPLETE")
        self.assertNotIn("product_outcomes", seen["status"])
        self.assertNotIn("seal_reason", seen["status"])
        self.assertEqual(seen["status"]["attempts_finished"], 2)
        header = ledger.read(self.paths.ledgers / f"{result['batch_id']}.jsonl")[0][0]["body"]
        self.assertEqual(header["schedule_seed"], protocol.schedule_seed(
            "heldout", seal_digest=header["task_set_seal_digest"], commit=self.commit))
        self.assertIn("product_outcomes", batch.status(self.paths)["batches"][0])
        with self.assertRaisesRegex(batch.BatchError, "already ran the held-out set"):
            self.run_batch("heldout")


class QualificationRuleTests(BatchWorld):
    """A candidate that changed only the product runs on its smoke qualification; one that
    changed anything under evals/ needs the full one."""

    def commit_change(self, path: str) -> str:
        target = self.checkout / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{path} changed\n")
        git(self.checkout, "add", path)
        git(self.checkout, "commit", "-q", "-m", f"change {path}")
        self.commit = git(self.checkout, "rev-parse", "HEAD")
        return self.commit

    def smoke(self, name="p74a-smoke-20260918T020000Z.json"):
        target = self.private / "qualification" / name
        target.write_text(json.dumps({
            "status": "READY", "candidate": self.candidate(), "fixture_manifest_digest": DIGEST,
            "checks": {"submit_feedback_within_300ms": True,
                       "cancel_feedback_within_300ms": True}}))

    def test_a_product_change_runs_on_its_smoke_qualification(self):
        self.commit_change("src/product.py")
        with self.assertRaisesRegex(batch.BatchError, "has not passed"):
            batch.qualification(self.paths, self.candidate(), DIGEST)
        self.smoke()
        found = batch.qualification(self.paths, self.candidate(), DIGEST)
        self.assertEqual((found["kind"], found["full_qualification"]),
                         ("smoke", "p74a-qualification-20260918T000000Z.json"))

    def test_a_change_under_evals_needs_the_full_qualification(self):
        self.commit_change("evals/odoo/driver.py")
        self.smoke()
        with self.assertRaisesRegex(batch.BatchError, "evals/ changed since"):
            batch.qualification(self.paths, self.candidate(), DIGEST)

    def test_a_smoke_qualification_on_another_fixture_does_not_count(self):
        self.commit_change("src/product.py")
        self.smoke()
        with self.assertRaisesRegex(batch.BatchError, "another fixture manifest"):
            batch.qualification(self.paths, self.candidate(), "sha256:" + "1" * 64)
