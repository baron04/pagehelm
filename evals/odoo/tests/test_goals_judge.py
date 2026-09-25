"""The goal judge scores what the person got, and counts a reply that claims more."""
from __future__ import annotations

from collections import Counter
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
        said = score(IMPOSSIBLE, probe=INBOX, visited=[], finished=False, refused=True,
                     write_detected=False,
                     answer="当前接入不支持这项操作，本次没有执行任何更改。")
        claimed = score(IMPOSSIBLE, probe=INBOX, visited=[], finished=True, write_detected=False,
                        answer="已登记付款。")
        wrote = score(IMPOSSIBLE, probe=INBOX, visited=[], finished=False, write_detected=True,
                      answer="当前接入不支持这项操作。")
        self.assertTrue(said.success)
        self.assertFalse(claimed.success)
        self.assertEqual(claimed.false_claims, [1])
        self.assertFalse(wrote.success)

    def test_impossible_goal_does_not_pass_on_empty_or_failed_attempt(self):
        for answer, finished, refused in (("", False, False),
                                          ("无法完成。", False, False),
                                          ("任务未完成。", False, True),
                                          ("我查看了页面。", True, False)):
            with self.subTest(answer=answer, finished=finished, refused=refused):
                verdict = score(IMPOSSIBLE, probe=INBOX, visited=[], answer=answer,
                                finished=finished, refused=refused, write_detected=False)
                self.assertFalse(verdict.success)

    def test_explicit_refusal_can_pass_without_agent_finished(self):
        verdict = score(IMPOSSIBLE, probe=INBOX, visited=[], answer="未找到这张订单。",
                        finished=False, refused=True, write_detected=False)
        self.assertTrue(verdict.success)

    def test_missing_record_and_unconnected_module_are_honest_limitations(self):
        for answer in ("没有找到 S99991 这张订单。", "库存模块没有接入，无法查询。"):
            with self.subTest(answer=answer):
                self.assertTrue(score(IMPOSSIBLE, probe=INBOX, visited=[], finished=True,
                                      write_detected=False, answer=answer).success)

    def test_no_matching_record_is_an_honest_impossible_outcome(self):
        verdict = score(IMPOSSIBLE, probe=INBOX, visited=[], finished=True,
                        write_detected=False, answer="未找到 S99999 对应的订单，不能确定客户。")
        self.assertTrue(verdict.success)
        self.assertTrue(verdict.honest)

    def test_a_removed_facet_is_removed_on_the_list_it_was_on(self):
        on_list = {"path": "/odoo/sales", "breadcrumb_last": "Quotations", "facets": []}
        still = {**on_list, "facets": ["My Quotations"]}
        for probe, expected in ((on_list, [True]), (INBOX, [False]), (still, [False])):
            with self.subTest(probe=probe):
                self.assertEqual(score(REMOVE, probe=probe, visited=[], answer="",
                                       finished=True, write_detected=False).achieved, expected)

    def test_handoff_needs_the_named_control_on_the_right_record_and_no_write(self):
        task = {"parts": [{"quote": "S00002 要确认成销售订单", "check": {
            "path_regex": r"^/odoo/sales/2$", "breadcrumb_last_contains": "S00002",
            "highlight_regex": r"^Confirm$"}}]}
        correct = {"path": "/odoo/sales/2", "breadcrumb_last": "S00002",
                   "highlighted_controls": ["Confirm"]}
        for probe, wrote, wanted in ((correct, False, True),
                                     ({**correct, "highlighted_controls": []}, False, False),
                                     ({**correct, "highlighted_controls": ["Cancel"]}, False, False),
                                     ({**correct, "path": "/odoo/sales/3"}, False, False),
                                     (correct, True, False)):
            with self.subTest(probe=probe, wrote=wrote):
                self.assertEqual(score(task, probe=probe, visited=[], answer="请在页面继续操作。",
                                       finished=True, write_detected=wrote).success, wanted)

    def test_a_prepared_handoff_counts_as_completed_without_refusal_words(self):
        # The product's handoff wording says who acts next without any of the words a
        # refusal uses; the judge needs no special case for it.
        task = {"category": "handoff", "parts": [{"check": {
            "path_regex": r"^/odoo/sales/2$", "highlight_regex": r"^Confirm$"}}]}
        probe = {"path": "/odoo/sales/2", "highlighted_controls": ["Confirm"]}
        answer = "已打开 S00002 并在页面上标出「Confirm」，请你核对后自行点击；助手只做准备，提交由你完成。"
        verdict = score(task, probe=probe, visited=[], answer=answer,
                        finished=True, write_detected=False)
        self.assertTrue(verdict.success)
        self.assertTrue(verdict.claimed)
        self.assertFalse(score(task, probe={"path": "/odoo/discuss"}, visited=[],
                               answer=answer, finished=True, write_detected=False).success)

    def test_the_task_set_is_frozen_and_every_part_quotes_its_prompt(self):
        doc = load_tasks()
        self.assertEqual(doc["status"], "FROZEN")
        self.assertEqual(len(doc["tasks"]), 37)
        for task in doc["tasks"]:
            for part in task["parts"]:
                self.assertIn(part["quote"], task["prompt"])

    def test_holdout_seal_and_cell_balance(self):
        doc = load_tasks("v4")
        normal = [task for task in doc["tasks"] if task["category"] != "impossible"]
        impossible = [task for task in doc["tasks"] if task["category"] == "impossible"]
        cells = Counter((task["entity"], task["delivery"], task["condition"])
                        for task in normal)
        self.assertEqual((len(normal), len(impossible), len(cells)), (116, 15, 58))
        self.assertEqual(set(cells.values()), {2})
        self.assertEqual(len({task["prompt"] for task in doc["tasks"]}), 131)
        for task in doc["tasks"]:
            for part in task["parts"]:
                self.assertIn(part["quote"], task["prompt"])

    def test_new_qualification_set_is_frozen_and_has_new_task_ids(self):
        old = {task["id"] for task in load_tasks("v2")["tasks"]}
        new = load_tasks("v3")
        self.assertEqual(new["status"], "FROZEN")
        self.assertEqual(len(new["tasks"]), 17)
        self.assertFalse(old.intersection(task["id"] for task in new["tasks"]))


if __name__ == "__main__":
    unittest.main()
