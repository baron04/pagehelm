import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evals.odoo import protocol

DEVELOPMENT = Path(__file__).resolve().parents[1] / "tasksets" / "p74-odoo-development-v1.json"


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.task_set = json.loads(DEVELOPMENT.read_text())
        self.ids = [task["task_id"] for task in self.task_set["tasks"]]
        self.eligible = {"woa_product": self.ids,
                         "pageagent_browser_only": protocol.pageagent_eligibility(self.task_set)}

    def test_seed_is_derived_from_the_second_freeze_commit(self):
        seed = protocol.derive_schedule_seed("a" * 40)
        self.assertEqual(seed, protocol.derive_schedule_seed("a" * 40))
        self.assertNotEqual(seed, protocol.derive_schedule_seed("b" * 40))
        with self.assertRaises(ValueError):
            protocol.derive_schedule_seed("abc123")

    def test_schedule_is_deterministic_and_never_pools_observations(self):
        seed = protocol.derive_schedule_seed("c" * 40)
        slots = protocol.build_schedule(self.ids, eligible=self.eligible, seed=seed)
        self.assertEqual(slots, protocol.build_schedule(self.ids, eligible=self.eligible,
                                                        seed=seed))
        replicates = [slot["replicate"] for slot in slots]
        self.assertEqual(replicates, sorted(replicates, key=protocol.OBSERVATIONS.index))
        woa = [slot for slot in slots if slot["arm"] == "woa_product"]
        self.assertEqual(len(woa), 60)
        self.assertEqual(len({(slot["task_id"], slot["replicate"]) for slot in woa}), 60)
        self.assertEqual(len({slot["slot_id"] for slot in slots}), len(slots))

    def test_pageagent_only_on_single_turn_read_tasks(self):
        eligible = set(self.eligible["pageagent_browser_only"])
        by_id = {task["task_id"]: task for task in self.task_set["tasks"]}
        self.assertTrue(eligible)
        for task_id in eligible:
            task = by_id[task_id]
            self.assertEqual(len(task["messages"]), 1)
            self.assertEqual(task["max_mutations"], 0)
            self.assertIsNone(task["approval_oracle"])
            self.assertEqual(task["class"], "normal")
        writes = {task_id for task_id, task in by_id.items() if task["max_mutations"]}
        self.assertFalse(eligible & writes)

    def test_schedule_rejects_more_arms_or_partial_woa(self):
        with self.assertRaises(ValueError):
            protocol.build_schedule(self.ids, eligible={**self.eligible, "unknown_arm": []},
                                    seed="0" * 32)
        with self.assertRaises(ValueError):
            protocol.build_schedule(self.ids, eligible={"woa_product": self.ids[:5]},
                                    seed="0" * 32)


class DocumentTests(unittest.TestCase):
    def test_b_is_recorded_as_excluded_and_capabilities_are_per_arm(self):
        document = protocol.document()
        self.assertEqual(document["stage"], "draft")
        self.assertNotIn("pageagent_skill_api", document["arms"])
        self.assertEqual(document["excluded_arms"]["pageagent_skill_api"]["status"],
                         "excluded_before_build")
        self.assertEqual(set(document["capability_digests"]), set(protocol.ARMS))
        self.assertEqual(document["budgets"]["automatic_retries"], 0)
        self.assertNotEqual(protocol.arm_capability_digest("woa_product"),
                            protocol.arm_capability_digest("pageagent_browser_only"))

    def test_both_arms_share_the_products_request_parameters(self):
        parameters = protocol.PROVIDER["request_parameters"]
        policy = json.loads(protocol.PRODUCT_MODEL_OPTIONS.read_text())
        self.assertEqual(parameters["model_options"],
                         policy[protocol.PROVIDER["requested_model_alias"]])
        self.assertEqual(parameters["identical_for"], sorted(protocol.ARMS))
        self.assertEqual(parameters["temperature"], 0.0)
        self.assertNotIn("top_p", protocol.PROVIDER)
        self.assertEqual(set(parameters["never_forwarded"]), {"top_p", "max_completion_tokens"})

    def test_first_freeze_binds_its_seals(self):
        tasksets = DEVELOPMENT.parent
        development = json.loads((tasksets / "p74-odoo-development-v1.seal.json").read_text())
        heldout = json.loads((tasksets / "p74-odoo-heldout-v1.seal.json").read_text())
        split = json.loads((tasksets / "p74-odoo-split-v1.json").read_text())
        record = protocol.first_freeze_record(development_seal=development,
                                              heldout_seal=heldout, split_verification=split)
        self.assertEqual(protocol.document(first_freeze=record)["stage"], "first_frozen")
        with self.assertRaises(ValueError):
            protocol.first_freeze_record(development_seal=heldout, heldout_seal=development,
                                         split_verification=split)


class ImplementationBindingTests(unittest.TestCase):
    def test_document_names_runners_evaluator_reset_and_batch_chain(self):
        document = protocol.document()
        for key in ("runners", "evaluator", "reset", "batch_chain", "replacement_policy",
                    "stop_policy", "approval_oracle", "statistics"):
            self.assertIn(key, document)
        named = {path for paths in protocol.APPARATUS_MODULES.values() for path in paths}
        digests = document["implementation_digests"]
        self.assertEqual({path for role in digests.values() for path in role}, named)
        self.assertIn("evals/odoo/runner_woa.py", digests["runners"])
        self.assertIn("evals/odoo/evaluator.py", digests["evaluator"])
        self.assertIn("evals/odoo/fixture.py", digests["reset"])
        self.assertTrue({"evals/odoo/qualify.py", "evals/odoo/scripted.py"}.isdisjoint(named))

    def test_any_apparatus_change_changes_its_implementation_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for paths in protocol.APPARATUS_MODULES.values():
                for relative in paths:
                    (root / relative).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(protocol.REPOSITORY / relative, root / relative)
            before = protocol.implementation_digests(root)
            self.assertEqual(before, protocol.implementation_digests())
            evaluator = root / "evals/odoo/evaluator.py"
            evaluator.write_bytes(evaluator.read_bytes() + b"\n")
            after = protocol.implementation_digests(root)
            self.assertNotEqual(before["evaluator"], after["evaluator"])
            self.assertEqual(before["runners"], after["runners"])
            evaluator.unlink()
            with self.assertRaises(ValueError):
                protocol.implementation_digests(root)

    def test_second_freeze_binds_the_candidate_and_its_apparatus(self):
        record = protocol.second_freeze_record(candidate_commit="d" * 40)
        self.assertEqual(record["implementation_digests"], protocol.implementation_digests())
        self.assertEqual(protocol.document(second_freeze=record)["stage"], "second_frozen")
        for bad in ("abc123", "D" * 40, None):
            with self.subTest(commit=bad), self.assertRaises(ValueError):
                protocol.second_freeze_record(candidate_commit=bad)


if __name__ == "__main__":
    unittest.main()
