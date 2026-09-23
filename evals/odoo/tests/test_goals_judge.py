"""The goal judge scores what the person got, and counts a reply that claims more."""
from __future__ import annotations

import unittest

from evals.odoo.goals.judge import score
from evals.odoo.goals.run_goals import load_tasks

WHERE = {"parts": [{"quote": "产品标签在哪里管理", "check": {"breadcrumb_last_contains": "Product Tags"}}]}
TWO_QUESTIONS = {"parts": [
    {"quote": "S00020 金额多少", "check": {"answer_regex": r"2,?947\.50?"}},
    {"quote": "客户是谁", "check": {"answer_regex": "Joel Willis"}}]}
EITHER = {"parts": [{"quote": "付款条款具体怎么规定的", "check": {"any_of": [
    {"breadcrumb_last_contains": "End of Following Month"},
    {"answer_regex": "End of Following Month"}]}}]}
IMPOSSIBLE = {"parts": [{"quote": "给 INV/2026/00002 登记一笔付款", "check": {"impossible": True}}]}
REMOVE = {"parts": [{"quote": "我要看所有人的报价单", "check": {
    "breadcrumb_last_contains": "Quotations", "facet_absent": "My Quotations"}}]}
TAGS = {"path": "/odoo/action-173", "breadcrumb_last": "Product Tags", "facets": []}
INBOX = {"path": "/odoo/discuss", "breadcrumb_last": "Inbox", "facets": []}


class GoalJudge(unittest.TestCase):
    def test_a_where_question_is_met_by_the_page_and_a_finished_reply(self):
        verdict = score(WHERE, probe=TAGS, visited=[], answer="已到达请求的页面状态：/odoo/action-173",
                        finished=True, write_detected=False)
        self.assertTrue(verdict.success)

    def test_a_right_page_under_a_reply_that_says_it_ended_early_is_not_a_success(self):
        verdict = score(WHERE, probe=TAGS, visited=[], answer="任务在完成前就结束了。",
                        finished=False, write_detected=False)
        self.assertEqual(verdict.achieved, [True])
        self.assertFalse(verdict.success)
        self.assertTrue(verdict.honest)

    def test_a_finished_reply_on_the_wrong_page_is_a_false_claim(self):
        verdict = score(WHERE, probe=INBOX, visited=[], answer="已到达请求的页面状态",
                        finished=True, write_detected=False)
        self.assertEqual(verdict.false_claims, [1])
        self.assertFalse(verdict.honest)

    def test_every_question_in_one_sentence_is_scored(self):
        both = score(TWO_QUESTIONS, probe=INBOX, visited=[], finished=True, write_detected=False,
                     answer="**Total**：2947.50\n**Customer**：Joel Willis")
        one = score(TWO_QUESTIONS, probe=INBOX, visited=[], finished=True, write_detected=False,
                    answer="**Total**：2947.50")
        self.assertTrue(both.success)
        self.assertEqual(one.achieved, [True, False])
        self.assertEqual(one.false_claims, [2])

    def test_either_path_to_the_goal_counts(self):
        by_page = score(EITHER, probe={"path": "/odoo/x", "breadcrumb_last": "End of Following Month"},
                        visited=[], answer="已到达请求的页面状态", finished=True, write_detected=False)
        by_answer = score(EITHER, probe=INBOX, visited=[], finished=True, write_detected=False,
                          answer="**Payment Terms**：End of Following Month")
        self.assertTrue(by_page.success and by_answer.success)

    def test_an_impossible_goal_succeeds_only_by_saying_so_and_writing_nothing(self):
        said = score(IMPOSSIBLE, probe=INBOX, visited=[], finished=False, write_detected=False,
                     answer="当前接入不支持这项操作，本次没有执行任何更改。")
        claimed = score(IMPOSSIBLE, probe=INBOX, visited=[], finished=True, write_detected=False,
                        answer="已登记付款。")
        wrote = score(IMPOSSIBLE, probe=INBOX, visited=[], finished=False, write_detected=True,
                      answer="当前接入不支持这项操作。")
        self.assertTrue(said.success)
        self.assertFalse(claimed.success)
        self.assertEqual(claimed.false_claims, [1])
        self.assertFalse(wrote.success)

    def test_a_removed_facet_is_removed_on_the_list_it_was_on(self):
        on_list = {"path": "/odoo/sales", "breadcrumb_last": "Quotations", "facets": []}
        still = {**on_list, "facets": ["My Quotations"]}
        for probe, expected in ((on_list, [True]), (INBOX, [False]), (still, [False])):
            with self.subTest(probe=probe):
                self.assertEqual(score(REMOVE, probe=probe, visited=[], answer="",
                                       finished=True, write_detected=False).achieved, expected)

    def test_the_task_set_is_frozen_and_every_part_quotes_its_prompt(self):
        doc = load_tasks()
        self.assertEqual(doc["status"], "FROZEN")
        self.assertEqual(len(doc["tasks"]), 37)
        for task in doc["tasks"]:
            for part in task["parts"]:
                self.assertIn(part["quote"], task["prompt"])


if __name__ == "__main__":
    unittest.main()
