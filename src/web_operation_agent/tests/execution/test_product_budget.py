"""Planning, retries and execution cannot reset one product turn's allowance."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor
from web_operation_agent.execution import turn
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.modeling.provider import ProviderError
from web_operation_agent.schemas import SourceMap, empty_contract, ContractStep, POTENTIALLY_WRITING_ACTIONS
from web_operation_agent.tests.execution.product_support import (
    _Channel, _RemoteBrowser, _message,
)
from web_operation_agent.tests.support.chat_port_contract import CancellableChatDouble
from web_operation_agent.tests.support.application import _InvoiceAdapter


def finish():
    return {"tool_calls": [{"id": "finish", "function": {
        "name": "finish", "arguments": '{"outcome":"unable"}'}}]}


def planned():
    contract = empty_contract("budget-test")
    contract.steps = [ContractStep("Inspect the current page", "fixture", "")]
    return {"content": json.dumps(contract.to_json())}


class Chat(CancellableChatDouble):
    model = "synthetic-model"
    provider = "synthetic-provider"
    meter = None
    options = {"enable_thinking": False}

    def __init__(self, replies, after_call=lambda: None):
        self.replies, self.calls = list(replies), []
        self.after_call = after_call

    def complete(self, messages, tools, run_id, **kwargs):
        self.calls.append({"run_id": run_id, "timeout_s": kwargs.get("timeout_s")})
        reply = self.replies.pop(0)
        self.after_call()
        if isinstance(reply, Exception):
            raise reply
        return reply


class ProductSharedBudgetTests(unittest.TestCase):
    def execute(self, chat, *, steps=3, seconds=60, source=False, channel=None,
                clock=None):
        capability = (SourceCapability(source_contract=True, source_map=SourceMap(
            schema_version=1, attestation_id="synthetic", repository_id="fixture",
            release="v1", relations=[])) if source else SourceCapability.none())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            url = "https://ledger.example/invoices"
            executor = AgentTurnExecutor(
                channel=channel or _Channel(page_url=url), chat=chat, control=ReadOnlyControl(),
                application=bind_application(_InvoiceAdapter()),
                run_id_factory=lambda _m: "budget-test",
                config=AgentTurnConfig(supported_scope="Read the page only.",
                    ledger_path=path, step_budget=steps, wall_budget_s=seconds,
                    disabled_actions=POTENTIALLY_WRITING_ACTIONS - {"goto"},
                    confirm_page_operations=True,
                    source=capability))
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                if clock is None:
                    result = executor.execute(_message(url))
                else:
                    with patch.object(turn.time, "monotonic", lambda: clock[0]):
                        result = executor.execute(_message(url))
            events = [json.loads(line) for line in path.read_text().splitlines()]
            return result, events, _RemoteBrowser.instances[-1]

    def test_transient_retry_cannot_exceed_the_request_limit(self):
        chat = Chat([ProviderError("synthetic outage", code="provider_unavailable"), finish()])
        result, events, browser = self.execute(chat, steps=1)
        self.assertEqual(result.terminal_reason, "step_budget")
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(result.agent.steps_used, 0)
        self.assertEqual(browser.actions, [])
        self.assertEqual(result.agent.model_receipt["model_requests"], 1)
        self.assertEqual(result.agent.model_receipt["budget"]["terminal_reason"], "step_budget")
        self.assertEqual(sum(e["event"] == "turn_model_receipt" for e in events), 1)

    def test_the_last_allowed_retry_may_finish_without_another_request(self):
        chat = Chat([ProviderError("synthetic outage", code="provider_unavailable"), finish()])
        result, _, _ = self.execute(chat, steps=2)
        self.assertEqual(result.terminal_reason, "agent_refused")
        self.assertEqual(len(chat.calls), 2)
        self.assertEqual(result.agent.steps_used, 1)
        receipt = result.agent.model_receipt
        self.assertEqual(receipt["model_outcomes"], {"failed": 1, "succeeded": 1})
        self.assertEqual(receipt["budget"]["model_requests_used"], 2)

    def test_planner_growth_uses_the_execution_allowance(self):
        chat = Chat([{"content": '["invoice"]'}, {"content": "{}", "finish_reason": "length"}, finish()])
        result, events, browser = self.execute(chat, source=True, steps=2)
        self.assertEqual(result.terminal_reason, "step_budget")
        self.assertEqual(len(chat.calls), 2)
        self.assertEqual(result.agent.steps_used, 0)
        self.assertEqual(browser.actions, [])
        self.assertEqual(result.agent.model_receipt["stages"]["source"]["model_requests"], 2)
        self.assertTrue(any(e["event"] == "planning_stopped" for e in events))

    def test_planning_and_loop_requests_keep_the_same_run_id(self):
        chat = Chat([{"content": '["invoice"]'}, planned(), finish()])
        result, _, _ = self.execute(chat, source=True, steps=3)
        self.assertEqual(result.terminal_reason, "agent_refused")
        self.assertEqual([c["run_id"] for c in chat.calls], ["budget-test"] * 3)
        receipt = result.agent.model_receipt
        self.assertEqual(receipt["model_requests"], 3)
        self.assertEqual(receipt["stages"]["source"]["model_requests"], 2)
        self.assertEqual(receipt["stages"]["model"]["model_requests"], 1)
        self.assertIsNone(receipt["accounting"]["total_cost_cny"])

    def test_a_late_planner_reply_cannot_reset_the_deadline(self):
        clock = [100.0]
        def advance():
            clock[0] += 61
        chat = Chat([{"content": '["invoice"]'}, finish()], after_call=advance)
        result, _, browser = self.execute(chat, source=True, clock=clock)
        self.assertEqual(result.terminal_reason, "wall_clock_budget")
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(browser.actions, [])
        self.assertEqual(result.agent.wall_seconds, 61)

    def test_default_two_minute_turn_stops_after_late_model_reply(self):
        budget = AgentTurnConfig(supported_scope="Read the page only.",
                                 ledger_path=Path("unused-audit.jsonl")).wall_budget_s
        self.assertEqual(budget, 120)
        clock = [100.0]
        chat = Chat([{"content": '["invoice"]'}, finish()],
                    after_call=lambda: clock.__setitem__(0, clock[0] + 121))
        result, events, browser = self.execute(chat, source=True, seconds=budget, clock=clock)
        self.assertEqual(result.terminal_reason, "wall_clock_budget")
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(browser.actions, [])
        self.assertTrue(any(event["event"] == "turn_model_receipt" for event in events))

    def test_planning_time_reduces_the_next_request_deadline(self):
        clock = [100.0]
        def advance():
            clock[0] += 2
        chat = Chat([{"content": '["invoice"]'}, planned(), finish()],
                    after_call=advance)
        result, _, _ = self.execute(chat, source=True, clock=clock)
        self.assertEqual(result.terminal_reason, "agent_refused")
        self.assertEqual([c["timeout_s"] for c in chat.calls], [60, 58, 56])
        self.assertEqual(result.agent.wall_seconds, 6)

    def test_cancellation_during_planning_does_not_dispatch_a_loop_request(self):
        channel = _Channel(page_url="https://ledger.example/invoices")
        chat = Chat([{"content": '["invoice"]'}, finish()],
                    after_call=lambda: setattr(channel, "cancelled", True))
        result, _, browser = self.execute(chat, source=True, channel=channel)
        self.assertEqual(result.terminal_reason, "user_cancelled")
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(browser.actions, [])

    def test_last_request_receipt_is_not_accepted_after_the_deadline(self):
        clock = [100.0]
        def advance():
            clock[0] += 61
        chat = Chat([finish()], after_call=advance)
        result, _, browser = self.execute(chat, clock=clock)
        self.assertEqual(result.terminal_reason, "wall_clock_budget")
        self.assertEqual(browser.actions, [])
        self.assertEqual(result.agent.model_receipt["model_inflight"], 0)

    def test_waiting_for_authenticated_approval_does_not_spend_the_turn_deadline(self):
        clock = [100.0]
        class Person(_Channel):
            def wait_for_approval(self, *args, **kwargs):
                clock[0] += 2
                return super().wait_for_approval(*args, **kwargs)
        channel = Person(page_url="https://ledger.example/invoices")
        # Use an exact link on the current page; no source or guessed route grants navigation.
        from web_operation_agent.schemas import ElementRef
        channel.latest_observation_envelope.observation.elements.append(
            ElementRef(ref="current-link", role="link", name="Invoices", tag="a", epoch=1,
                       attrs={"href": "/invoices/invoice-1"}))
        chat = Chat([{"tool_calls": [{"id": "navigate", "function": {
            "name": "goto", "arguments": '{"url":"/invoices/invoice-1"}'}}]}, finish()])
        result, _, browser = self.execute(chat, steps=2, seconds=1,
                                          channel=channel, clock=clock)
        self.assertIn("goto", browser.actions)
        self.assertNotEqual(result.terminal_reason, "wall_clock_budget")
        self.assertEqual(len(chat.calls), 2)
        self.assertEqual([c["timeout_s"] for c in chat.calls], [1, 1])
        self.assertEqual(result.agent.wall_seconds, 2)


class ProductPageLeaveTests(unittest.TestCase):
    def test_leave_to_work_the_page_is_asked_once_per_turn(self):
        from web_operation_agent.schemas import ElementRef
        url = "https://ledger.example/invoices"
        channel = _Channel(page_url=url)
        channel.latest_observation_envelope.observation.elements.append(
            ElementRef(ref="current-link", role="link", name="Invoices", tag="a", epoch=1,
                       attrs={"href": "/invoices/invoice-1"}))
        goto = {"tool_calls": [{"id": "navigate", "function": {
            "name": "goto", "arguments": '{"url":"/invoices/invoice-1"}'}}]}
        chat = Chat([planned(), goto, finish()] * 3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            executor = AgentTurnExecutor(
                channel=channel, chat=chat, control=ReadOnlyControl(),
                application=bind_application(_InvoiceAdapter()),
                run_id_factory=lambda m: "leave-" + m.message_id,
                config=AgentTurnConfig(supported_scope="Read the page only.",
                    ledger_path=path, step_budget=3, wall_budget_s=60,
                    disabled_actions=POTENTIALLY_WRITING_ACTIONS - {"goto"},
                    confirm_page_operations=True))
            gone = []
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                for i, fresh in enumerate((False, False, True)):
                    channel.new_conversation_pending = fresh
                    executor.execute({**_message(url), "message_id": f"message-{i}"})
                    gone.append("goto" in _RemoteBrowser.instances[-1].actions)
            events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(gone, [True, True, True])
        self.assertEqual([a.kind for a in channel.approvals], ["operate"] * 3)
        self.assertFalse(any(e["event"] == "operation_leave_carried" for e in events))


if __name__ == "__main__":
    unittest.main()
