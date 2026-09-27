"""Zero-model checks for the frozen follow-up set and numbered replies."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
import unittest

from evals.odoo.goals.judge import score
from evals.odoo.goals.run_goals import (
    TASKSETS, first_turn_met, load_tasks, numbered_choice, task_for_numbered_reply,
)


class MultiTurnHoldout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = load_tasks("mt-holdout")["tasks"]
        cls.dev = load_tasks("mt-dev")["tasks"]

    def test_frozen_set_has_forty_new_prompts_and_six_reply_types(self):
        path, pinned_digest = TASKSETS["mt-holdout"]
        seal = json.loads(path.with_suffix(".seal.json").read_text())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), pinned_digest)
        self.assertEqual(seal["task_set_sha256"], f"sha256:{pinned_digest}")
        dev_path, dev_digest = TASKSETS["mt-dev"]
        self.assertEqual(hashlib.sha256(dev_path.read_bytes()).hexdigest(), dev_digest)
        self.assertEqual(seal["development_set_sha256"], f"sha256:{dev_digest}")
        self.assertEqual(len(self.tasks), 40)
        self.assertEqual(len({task["id"] for task in self.tasks}), 40)
        prompts = [task["prompt"] for task in self.tasks]
        self.assertEqual(len(set(prompts)), 40)
        self.assertFalse(set(prompts) & {task["prompt"] for task in self.dev})
        self.assertEqual(Counter(task["group"] for task in self.tasks),
                         {"C": 7, "X": 7, "R": 7, "V": 6, "N": 7, "B": 6})
        for task in self.tasks:
            self.assertEqual(task["rubric_version"], 7)
            self.assertEqual(len(task["replies"]), 1)
            self.assertEqual(len(task["parts"]), 1)
            self.assertIn("first_turn", task)
            self.assertTrue(task["parts"][0]["quote"])
            for check in [task["parts"][0]["check"],
                          *(option["check"] for option in task["truth"].get("numbered_choices", {}).get("options", []))]:
                for key, value in check.items():
                    if key.endswith("_regex"):
                        re.compile(value)

    def test_numbered_truth_follows_the_actual_list_order(self):
        examples = {
            "mh35": ["S00002", "S00003"],
            "mh36": ["Office Chair", "Office Chair Black"],
            "mh37": ["INV/2026/00002", "INV/2026/00003"],
            "mh38": ["RINV/2026/00001", "RINV/2026/00002"],
            "mh39": ["S00019", "S00020"],
            "mh40": ["Corner Desk Right Sit", "Corner Desk Left Sit"],
        }
        numbered = [task for task in self.tasks if task["group"] == "B"]
        self.assertEqual(set(examples), {task["id"] for task in numbered})
        for task in numbered:
            a, b = examples[task["id"]]
            for first, second in [(a, b), (b, a)]:
                with self.subTest(task=task["id"], first=first):
                    reply = f"请选一项：\n1. {first}\n2. {second}"
                    turns = [{"answer": reply, "terminal_reason": "needs_clarification"}]
                    selected = numbered_choice(task, reply)
                    self.assertEqual(selected["name"],
                                     first if task["truth"]["numbered_choices"]["selection"] == 1 else second)
                    self.assertTrue(first_turn_met(task, turns))
                    self.assertEqual(task_for_numbered_reply(task, turns)["parts"][0]["check"],
                                     selected["check"])
            # The ordinal has no truth if the assistant omitted, repeated or
            # mixed candidates in its numbered list.
            for invalid in [f"1. {a}", f"1. {a}\n2. {a}",
                            f"1. {a} {b}\n2. another item"]:
                self.assertIsNone(numbered_choice(task, invalid))
                self.assertFalse(first_turn_met(task, [{"answer": invalid,
                                                        "terminal_reason": "needs_clarification"}]))
                self.assertEqual(task_for_numbered_reply(task, [{"answer": invalid}])["parts"][0]["check"],
                                 {"impossible": True})

    def test_numbered_second_order_scores_second_listed_page(self):
        task = next(task for task in self.tasks if task["id"] == "mh35")
        turns = [{"answer": "1. S00003\n2. S00002"}]
        bound = task_for_numbered_reply(task, turns)
        good = score(bound, probe={"path": "/odoo/sales/2", "breadcrumb_last": "S00002"},
                     visited=[], answer="已打开 S00002", finished=True, write_detected=False)
        wrong = score(bound, probe={"path": "/odoo/sales/3", "breadcrumb_last": "S00003"},
                      visited=[], answer="已打开 S00003", finished=True, write_detected=False)
        self.assertEqual(good.outcome, "correct")
        self.assertNotEqual(wrong.outcome, "correct")


if __name__ == "__main__":
    unittest.main()
