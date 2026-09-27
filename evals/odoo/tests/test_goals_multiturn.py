"""The follow-up development set and the runner's reading of a task's turns."""
from __future__ import annotations

from collections import Counter
import unittest

from evals.odoo.goals.judge import score
from evals.odoo.goals.run_goals import first_turn_met, load_tasks


class MultiTurnDevelopmentSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = {task["id"]: task for task in load_tasks("mt-dev")["tasks"]}

    def test_every_task_goes_on_after_its_request(self):
        self.assertEqual(len(self.tasks), 16)
        self.assertTrue(all(task["replies"] for task in self.tasks.values()))
        self.assertEqual(Counter(task["group"] for task in self.tasks.values()),
                         {"C": 6, "R": 5, "X": 2, "V": 2, "N": 1})
        self.assertTrue(all(task["rubric_version"] == 7 for task in self.tasks.values()))

    def test_the_first_turn_is_checked_as_the_task_says(self):
        task = self.tasks["mt01"]
        asked = [{"answer": "你指的是哪一个？ 1. S00019 2. S00020", "terminal_reason": "needs_clarification"},
                 {"answer": "已打开：S00020", "terminal_reason": "agent_finished"}]
        self.assertIs(first_turn_met(task, asked), True)
        # Opening one of them straight away is not asking.
        opened = [{"answer": "已打开：S00020", "terminal_reason": "agent_finished"}]
        self.assertIs(first_turn_met(task, opened), False)
        answered = self.tasks["mt09"]
        self.assertIs(first_turn_met(answered, [{"answer": "**S00006 · Customer**：Lumber Inc",
                                                 "terminal_reason": "agent_finished"}]), True)
        self.assertIsNone(first_turn_met({"id": "x"}, asked))

    def test_the_outcome_is_the_last_turns(self):
        task = self.tasks["mt09"]
        self.assertEqual(score(task, probe={}, visited=[], answer="**S00006 · Total**：750.00 USD",
                               finished=True, write_detected=False).outcome, "correct")
        page = self.tasks["mt11"]
        probe = {"path": "/odoo/sales/6", "breadcrumb_last": "S00006"}
        self.assertEqual(score(page, probe=probe, visited=[], answer="已打开：S00006",
                               finished=True, write_detected=False).outcome, "correct")


if __name__ == "__main__":
    unittest.main()
