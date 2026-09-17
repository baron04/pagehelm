import copy
import tempfile
import unittest
from pathlib import Path

from evals.odoo import ledger, report
from evals.odoo.tests.test_ledger import header, slots


class ReportLimitationTests(unittest.TestCase):
    def build(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempts.jsonl"
            ledger.create(path, header(slots(1)))
            summary = ledger.read(path)[1]
        return report.build_report(
            batches=[summary], tasks={"task-000": {"class": "normal", "stratum": "s",
                                                   "read_only": True}})

    def test_every_report_states_the_provider_limitations(self):
        value = self.build()
        self.assertEqual(value["limitations"], list(report.LIMITATIONS))
        self.assertIn("provider_alias_is_not_a_model_snapshot", value["limitations"])
        self.assertEqual(report.verify_report(value)["status"], "READY")

    def test_a_report_that_drops_or_changes_a_limitation_is_refused(self):
        value = self.build()
        for mutate in (lambda item: item["limitations"].pop(),
                       lambda item: item["limitations"].__setitem__(0, "alias_verified"),
                       lambda item: item.pop("limitations")):
            candidate = copy.deepcopy(value)
            mutate(candidate)
            with self.subTest(candidate=candidate.get("limitations")):
                with self.assertRaises(ValueError):
                    report.verify_report(candidate)


if __name__ == "__main__":
    unittest.main()


class TaskSetCompositionTests(unittest.TestCase):
    """The two limitations about what the thirty tasks are made of, held against the tasks.

    A limitation is a claim, and a claim nobody checks goes stale the moment the task set is
    rebalanced. If any of these stops holding, the sentence that describes the set stops being
    true and must be rewritten rather than quietly kept.
    """

    def setUp(self):
        import json
        from evals.odoo.stack import REPOSITORY
        value = json.loads((REPOSITORY / "evals/odoo/tasksets"
                            / "p74-odoo-development-v1.json").read_text(encoding="utf-8"))
        self.tasks = value["tasks"] if isinstance(value, dict) else value

    def test_the_set_is_balanced_by_stratum_and_by_nothing_else(self):
        from collections import Counter
        from evals.odoo import protocol
        self.assertEqual(len(self.tasks), protocol.FORMAL_TASK_COUNT)
        strata = Counter(task["stratum"] for task in self.tasks)
        self.assertEqual(len(set(strata.values())), 1, strata)
        for field in ("locale", "actor"):
            counts = Counter(task[field] for task in self.tasks)
            with self.subTest(field=field):
                # One value carries all but one task: a reading along this axis has nothing to
                # compare, which is what the limitation says.
                self.assertGreaterEqual(max(counts.values()), len(self.tasks) - 1, counts)
        self.assertIn("balanced_by_stratum_only_not_by_locale_actor_or_turn_depth",
                      report.LIMITATIONS)

    def test_no_task_runs_deeper_than_two_turns(self):
        """The apparatus takes up to four user messages per task and the set uses two, so the
        limit on turn depth is the set's rather than the apparatus's."""
        from evals.odoo import tasks as taskmod
        deepest = max(task["fingerprint"]["turns"] for task in self.tasks)
        self.assertLessEqual(deepest, 2)
        longest = dict(self.tasks[0], messages=["a message"] * 4,
                       fingerprint={**self.tasks[0]["fingerprint"], "turns": 4})
        taskmod.validate_task(longest, "development")

    def test_each_task_is_observed_exactly_twice(self):
        from evals.odoo import protocol
        seed = "0" * 32
        slots = protocol.formal_schedule(
            "development", [task["task_id"] for task in self.tasks], seed=seed)
        from collections import Counter
        per_task = Counter(slot["task_id"] for slot in slots)
        self.assertEqual(set(per_task.values()), {2}, per_task)
        self.assertEqual(len(slots), 2 * protocol.FORMAL_TASK_COUNT)
        self.assertIn("two_observations_per_task_support_no_per_task_or_per_action_rate",
                      report.LIMITATIONS)
