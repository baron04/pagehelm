"""A turn that ends without an answer says so in the reader's language (P7.5 CR-15).

Stopping a turn, declining a write, running out of budget and an uncertain write all end
without model prose, and the product's fallback sentences were English only: a Chinese
conversation stopped with the Stop button ended on "The requested action was cancelled."
J6.03 requires no whole English system sentence in a Chinese conversation.
"""
from __future__ import annotations

import re
import unittest

from web_operation_agent.execution import product
from web_operation_agent.execution.product import AgentTurnMessage
from web_operation_agent.tests.execution.test_product import _message


def _answer(reason: str, text: str) -> str:
    return product.render_neutral_user_answer(product.AgentAnswerContext(
        message=AgentTurnMessage.from_wire({**_message(), "text": text}),
        agent=product.RunResult(run_id="run", task_ref="task", arm="U",
                                terminal_reason=reason,
                                detail="the user stopped this task while the provider was working"),
        business_result=None, max_chars=4_000))


class TheFallbackSpeaksTheReadersLanguage(unittest.TestCase):
    REASONS = {"user_cancelled": "已取消。", "approval_denied": "已取消。",
               "indeterminate_write": "这次操作的结果不确定，没有自动重试。",
               "step_budget": "任务在完成前就结束了。",
               "wall_clock_budget": "任务在完成前就结束了。",
               "agent_refused": "这个请求没能完成。",
               "agent_finished": "这一轮已结束，但没有给出回答。"}

    def test_a_chinese_turn_ends_in_chinese(self):
        for reason, expected in self.REASONS.items():
            with self.subTest(reason=reason):
                answer = _answer(reason, "逐条列出全部客户的名称、电话和邮箱")
                self.assertEqual(answer, expected)
                self.assertIsNone(re.search(r"[A-Za-z]{3,}", answer))

    def test_an_english_turn_keeps_its_sentences(self):
        self.assertEqual(_answer("user_cancelled", "List every customer."),
                         "The requested action was cancelled.")
        self.assertEqual(_answer("indeterminate_write", "Invoice S00010."),
                         "The action outcome is uncertain. It was not retried automatically.")

    def test_the_internal_detail_is_never_the_answer(self):
        for reason in self.REASONS:
            with self.subTest(reason=reason):
                self.assertNotIn("stopped this task", _answer(reason, "列出客户"))


if __name__ == "__main__":
    unittest.main()
