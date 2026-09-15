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

    def test_schedule_seeds_are_derived_per_purpose_from_a_freeze_commit(self):
        seed = protocol.derive_schedule_seed("a" * 40)
        self.assertEqual(seed, protocol.derive_schedule_seed("a" * 40, purpose="heldout"))
        self.assertNotEqual(seed, protocol.derive_schedule_seed("b" * 40))
        self.assertNotEqual(seed, protocol.derive_schedule_seed("a" * 40, purpose="development"))
        for commit, purpose in (("abc123", "heldout"), ("A" * 40, "heldout"),
                                ("a" * 40, "apparatus_qualification")):
            with self.subTest(commit=commit, purpose=purpose), self.assertRaises(ValueError):
                protocol.derive_schedule_seed(commit, purpose=purpose)

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

    def test_formal_schedules_fix_thirty_tasks_observed_twice(self):
        seed = protocol.derive_schedule_seed("d" * 40, purpose="development")
        development = protocol.formal_schedule("development", self.ids, seed=seed)
        self.assertEqual(len(development), 60)
        self.assertEqual({slot["arm"] for slot in development}, {"woa_product"})
        heldout = protocol.formal_schedule(
            "heldout", self.ids, seed=seed,
            pageagent_eligible=self.eligible["pageagent_browser_only"])
        self.assertEqual(len(heldout), 60 + 2 * len(self.eligible["pageagent_browser_only"]))
        cases = {
            "twenty_nine_tasks": ("development", self.ids[:29], seed, ()),
            "pageagent_in_development": ("development", self.ids, seed, ["d01"]),
            "unformal_purpose": ("apparatus_qualification", self.ids, seed, ()),
            "chosen_seed": ("development", self.ids, "lucky", ()),
        }
        for label, (purpose, ids, value, eligible) in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                protocol.formal_schedule(purpose, ids, seed=value, pageagent_eligible=eligible)

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
        # The machine protocol states exactly what the gateway enforces.
        from evals.odoo import gateway
        self.assertEqual(set(parameters["never_forwarded"]), set(gateway.PAGEAGENT_ENGINE_FIELDS))
        self.assertEqual(set(parameters["shared_fields"]), gateway.SHARED_REQUEST_FIELDS)

    def test_the_stop_policy_and_gateway_outcomes_state_what_the_ledger_enforces(self):
        from evals.odoo import ledger, outcomes
        document = protocol.document()
        self.assertEqual(set(document["stop_policy"]["seal_reasons"]), ledger._SEAL_REASONS)
        self.assertEqual(set(document["stop_policy"]["exogenous_stops"]), ledger.EXOGENOUS_STOPS)
        self.assertEqual(set(document["stop_policy"]["heldout_same_protocol_restart_after"]),
                         ledger.HELDOUT_RESTART_REASONS)
        self.assertEqual(set(document["stop_policy"]["heldout_audited_restart_after"]),
                         ledger.HELDOUT_AUDITED_RESTART_REASONS)
        self.assertEqual(document["batch_chain"]["previous_batch"][-1], "continuation_record")
        self.assertEqual(len(document["gateway_outcomes"]), len(outcomes.GATEWAY_OUTCOMES))
        self.assertTrue(set(outcomes.SEALING_APPARATUS_CODES).isdisjoint(
            document["replacement_policy"]["eligible_apparatus_codes"]))


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


if __name__ == "__main__":
    unittest.main()
