"""The reply the model gave is the answer, not the note it wrote on the way out (P7.5 CR-09).

Asked "你能帮我做什么？", the model replied in plain text with a complete list of what the
session can do. The loop asked it to call a tool, it called `finish(done=true)`, and its `why`
-- "已向用户说明本助手可提供的能力……", a note about the reply -- replaced the reply in the
panel. The capability list lost the form prefill it had named, and the answer read as a run
report.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.session import TOOL_SCHEMAS, ServerAgentSession, TaskSpec
from web_operation_agent.tests.test_business import _Browser, _Chat, _observation

REPLY = ("我可以帮你查询客户、报价单、销售订单、产品和发票；在你确认后创建客户、确认报价单、"
         "为订单开票；也可以预填一张新客户表单，由你自己提交。")
NOTE = "已向用户说明本助手可提供的能力。"


def _run(script, prompt="你能帮我做什么？"):
    tmp = tempfile.TemporaryDirectory()
    ledger = SessionLedger(Path(tmp.name) / "session.jsonl", "cr09")
    session = ServerAgentSession(
        task=TaskSpec(task_ref="T", prompt=prompt, supported_scope="Answer questions."),
        chat=_Chat(script), browser=_Browser(), control=ReadOnlyControl(), ledger=ledger,
        step_budget=6, wall_budget_s=30, run_id="cr09")
    session.entry_observation = _observation(1)
    session.plan()
    result = session.run()
    tmp.cleanup()
    return result, ledger


class TheReplyIsTheAnswer(unittest.TestCase):
    def test_a_plain_reply_followed_by_finish_is_what_the_user_reads(self):
        result, ledger = _run([(None, {"content": REPLY}),
                               ("finish", {"done": True, "why": NOTE})])
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(result.agent_said, REPLY)
        kept = ledger.of_kind("plain_reply_kept")
        self.assertEqual([row["why"] for row in kept], [NOTE])

    def test_a_tool_between_the_reply_and_finish_makes_the_reply_stale(self):
        result, ledger = _run([(None, {"content": REPLY}), ("observe", {}),
                               ("finish", {"done": True, "why": "页面上是客户列表。"})])
        self.assertEqual(result.agent_said, "页面上是客户列表。")
        self.assertEqual(ledger.of_kind("plain_reply_kept"), [])

    def test_giving_up_says_why_it_gave_up(self):
        result, _ = _run([(None, {"content": REPLY}),
                          ("finish", {"done": False, "why": "这项查询目前不支持。"})])
        self.assertEqual(result.terminal_reason, "agent_refused")
        self.assertEqual(result.agent_said, "这项查询目前不支持。")

    def test_finish_without_an_earlier_reply_is_unchanged(self):
        result, ledger = _run([("finish", {"done": True, "why": "好的。"})])
        self.assertEqual(result.agent_said, "好的。")
        self.assertEqual(ledger.of_kind("plain_reply_kept"), [])

    def test_the_finish_schema_says_who_reads_why(self):
        finish = next(tool["function"] for tool in TOOL_SCHEMAS
                      if tool["function"]["name"] == "finish")
        description = finish["parameters"]["properties"]["why"]["description"]
        self.assertIn("the user reads this as your reply", description)
        self.assertIn("without tool or field names", description)


if __name__ == "__main__":
    unittest.main()
