"""The qualification's closure probes that need no live stack, run against synthetic ledgers so a
probe cannot drift from the contract it checks and fail only at the end of a live run."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from evals.odoo import canary, ledger, qualify
from evals.odoo.tests.test_ledger import CANDIDATE, DIGEST
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

    def test_a_ledger_of_another_contract_is_refused_whole_by_the_ledger_and_the_chain(self):
        result = qualify._ledger_contract_probe(self.probe, self.sample)
        self.assertTrue(result["holds"], result)

    def test_the_rehearsal_provider_scripts_the_open_attempt_and_applies_its_slot_faults(self):
        batch = Batch(self, small_slots(("t01", "t02")))
        ctx = SimpleNamespace(inputs=lambda _task: (SimpleNamespace(messages=("列出客户",)), None))
        provider = qualify.RehearsalProvider(ctx, batch.path.parent)
        self.addCleanup(provider.close)
        status, answer = provider.answer(
            {"messages": [{"role": "user", "content": canary.CANARY_PROMPT}]})
        self.assertEqual((status, answer["model"], answer["choices"][0]["message"]["content"]),
                         (200, qualify.REHEARSAL_MODEL, "391"))
        ledger.append(batch.path, "attempt_started", {
            "attempt_id": "attempt-001", "slot_id": "s000", "arm": "woa_product",
            "task_id": "t01", "replicate": "primary", "run_id": "run-001",
            "fixture_digest": DIGEST, "capability_digest": DIGEST,
            "provider_config_digest": DIGEST, "provider_binding_digest": DIGEST,
            "replacement_of": None})
        ran = []
        provider.actions[("batch-001", 0)] = lambda: ran.append(True)
        provider.faults[("batch-001", 0)] = "drifted"
        body = {"messages": [{"role": "user", "content": "TASK: 列出客户"}]}
        status, answer = provider.answer(body)
        self.assertEqual((status, answer["model"], ran), (200, "p74-rehearsal/drifted", [True]))
        # A task without a script ends unable.
        self.assertEqual(answer["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
                         "finish")
        provider.faults[("batch-001", 0)] = "unauthorized"
        self.assertEqual(provider.answer(body)[0], 401)
        self.assertEqual(ran, [True])


if __name__ == "__main__":
    unittest.main()
