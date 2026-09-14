"""The qualification's closure probes that need no live stack, run against synthetic ledgers so a
probe cannot drift from the contract it checks and fail only at the end of a live run."""
import tempfile
import unittest
from pathlib import Path

from evals.odoo import ledger, qualify
from evals.odoo.tests.test_ledger import CANDIDATE
from evals.odoo.tests.test_report import TASKS, Batch, small_slots


class ClosureProbeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.probe = Path(directory.name)
        samples = Batch(self, small_slots(("t01", "t02")))
        samples.run_all()
        negatives = Batch(self, small_slots(("t03",)), batch_id="batch-002")
        negatives.run_all()
        self.batches = {"samples": {"summary": samples.complete()},
                        "negatives": {"summary": negatives.complete()}}
        self.sample = ledger.read(samples.path)[0][0]["body"]

    def test_a_plan_copied_from_a_batch_is_accepted_and_still_does_not_gate_it(self):
        result = qualify._report_probe(self.batches, self.sample, TASKS)
        self.assertTrue(result["gates_only_on_the_frozen_plan"], result)
        self.assertIn("not_the_fixed_thirty_task_schedule",
                      result["with_a_plan_copied_from_the_batch"])

    def test_forged_bindings_and_chain_rules_are_refused_on_a_real_header(self):
        self.assertTrue(qualify._binding_probe(self.probe, self.sample, CANDIDATE)["refused"])
        self.assertTrue(qualify._chain_rule_probe(self.probe, self.sample)["holds"])

    def test_contract_and_provider_identity_probes_hold_without_a_provider(self):
        self.assertTrue(qualify._contract_probe()["fails_closed"])
        self.assertTrue(qualify._provider_identity_probe(self.probe)["follows"])


if __name__ == "__main__":
    unittest.main()
