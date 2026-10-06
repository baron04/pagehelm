"""A page handoff is a verified marker, not an unapproved browser write."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.execution.target import required_highlight_control
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control, _obs


class HandoffBrowser(_Browser):
    def execute(self, action, args=None, expect_navigation=False):
        receipt = super().execute(action, args, expect_navigation)
        return replace(receipt, navigated=False)


class PageHandoffTests(unittest.TestCase):
    def test_final_explicit_marker_wins_over_earlier_dialog_opener(self):
        self.assertEqual(required_highlight_control(
            "找到「Pay」后高亮「Create Payment」，不要提交"), "Create Payment")

    def _run(self, tmp, script, *, pages=None, browser_type=HandoffBrowser,
             matched_mutation=False, prompt="请找到 Go 按钮，由我自己点击", contract=False):
        observations = pages or [_obs(1), _obs(2)]
        for observation in observations:
            observation.render_state = "ready"
        browser = browser_type(observations)
        session = ServerAgentSession(
            task=TaskSpec("T", prompt),
            chat=_Chat(script), browser=browser, control=_Control([[], [], []]),
            ledger=SessionLedger(Path(tmp) / "audit.jsonl", "handoff"),
            step_budget=6, require_task_contract=contract)
        session.entry_observation = browser.pages[0]
        session._explicit_mutation_intent = matched_mutation
        return session.run(), browser

    def test_highlight_finishes_without_click_or_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, browser = self._run(tmp, [("highlight", {"ref": "e1"})])
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertIn("尚未代你点击或提交", result.agent_said)
        self.assertEqual([name for name, _ in browser.done], ["highlight"])
        self.assertIsNone(result.verified_answer)

    def test_declared_marker_handoff_is_verified_and_still_tells_the_person_to_continue(self):
        prompt = "请标出 Go 按钮"
        with tempfile.TemporaryDirectory() as tmp:
            result, browser = self._run(tmp, [('declare_task', {'action_request':prompt,
                'action_kind':'page', 'answer_requests':[], 'answer_kinds':[]}),
                ('highlight', {'ref':'e1'})], prompt=prompt, contract=True)
        self.assertEqual('agent_finished', result.terminal_reason)
        self.assertIn('尚未代你点击或提交', result.agent_said)
        self.assertEqual([name for name, _ in browser.done], ['highlight'])
        self.assertEqual('highlight', result.verified_answer['action']['action'])

    def test_marker_does_not_erase_accompanying_question_at_automatic_handoff(self):
        prompt = "请标出 Go 按钮，查询金额"
        with tempfile.TemporaryDirectory() as tmp:
            result, browser = self._run(tmp, [('declare_task', {'action_request':'请标出 Go 按钮',
                'action_kind':'page', 'answer_requests':['查询金额'], 'answer_kinds':['data']}),
                ('highlight', {'ref':'e1'}), ('finish', {'outcome':'unable'})],
                prompt=prompt, contract=True)
        self.assertEqual('agent_refused', result.terminal_reason)
        self.assertIn('未完成问答：查询金额', result.agent_said)
        self.assertIn('尚未代你点击或提交', result.agent_said)
        self.assertEqual([name for name, _ in browser.done], ['highlight'])

    def test_missing_target_is_not_dispatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, browser = self._run(tmp, [
                ("highlight", {"ref": "e9"}),
                ("finish", {"done": False, "why": "no matching control"}),
            ])
        self.assertEqual(browser.done, [])
        self.assertNotEqual(result.terminal_reason, "agent_finished")

    def test_quoted_final_control_cannot_be_replaced_by_an_earlier_opener(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, browser = self._run(
                tmp, [("highlight", {"ref": "e1"}),
                      ("finish", {"done": False, "why": "dialog not opened"})],
                prompt="打开对话框，再帮我标出「Create Payment」按钮")
        self.assertEqual(browser.done, [])
        self.assertNotEqual(result.terminal_reason, "agent_finished")

    def test_a_matched_typed_mutation_cannot_be_replaced_by_a_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, browser = self._run(tmp, [
                ("highlight", {"ref": "e1"}),
                ("finish", {"done": False, "why": "use the registered write"}),
            ], matched_mutation=True)
        self.assertEqual(browser.done, [])
        self.assertNotEqual(result.terminal_reason, "agent_finished")

    def test_failed_receipt_cannot_claim_handoff(self):
        class FailedBrowser(HandoffBrowser):
            def execute(self, action, args=None, expect_navigation=False):
                receipt = super().execute(action, args, expect_navigation)
                return replace(receipt, ok=False, error_code="capability_refused")

        with tempfile.TemporaryDirectory() as tmp:
            result, browser = self._run(tmp, [("highlight", {"ref": "e1"})],
                                        browser_type=FailedBrowser)
        self.assertEqual([name for name, _ in browser.done], ["highlight"])
        self.assertNotEqual(result.terminal_reason, "agent_finished")

    def test_user_takeover_ends_turn_without_claiming_success(self):
        class IntervenedBrowser(HandoffBrowser):
            def execute(self, action, args=None, expect_navigation=False):
                receipt = super().execute(action, args, expect_navigation)
                return replace(receipt, ok=False, error_code="user_intervened")

        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._run(tmp, [("highlight", {"ref": "e1"})],
                                  browser_type=IntervenedBrowser)
        self.assertEqual(result.terminal_reason, "user_cancelled")

        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._run(tmp, [("fill", {"ref": "e1", "value": "x"})],
                                  browser_type=IntervenedBrowser)
        self.assertEqual(result.terminal_reason, "user_cancelled")
        self.assertEqual(result.page_actions, 0)


if __name__ == "__main__":
    unittest.main()
