"""The product turn freezes a numeric target before reading and refuses scope drift."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web_operation_agent.answer.evidence import Origin, QueryEvidence
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.runtime import BusinessToolResult, BusinessToolRuntime
from web_operation_agent.execution import product, turn
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor, AgentTurnStatus
from web_operation_agent.schemas import POTENTIALLY_WRITING_ACTIONS
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.support.business import _Chat


class _TwoCollections:
    def __init__(self):
        self.calls = []

    @staticmethod
    def tool_schemas():
        return tuple({"type": "function", "function": {
            "name": name, "description": "Read one actor-visible collection.",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False}}}
                     for name in ("read_orders", "read_quotations"))

    @staticmethod
    def tool_effects():
        return {"read_orders": "read", "read_quotations": "read"}

    def execute(self, name, args, *, context=None):
        self.calls.append((name, args))
        if args:
            raise AssertionError("answer goals must not reach the application")
        count = 0 if name == "read_orders" else 2
        origin = Origin("typed", name, "2026-09-24T00:00:00Z",
                        f"actor-visible {name}")
        return BusinessToolResult(
            content={"collection": name, "matched_count": count},
            audit={"application": "synthetic", "operation": name},
            evidence=(QueryEvidence("query#1", origin, name, ("customer id = 10",),
                                    count, records_scanned=count,
                                    covers_whole_scope=True, count_exact=True),))


class GoalBindingProductTest(unittest.TestCase):
    def _run(self, script):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = _TwoCollections()
            chat = _Chat(script)
            executor = AgentTurnExecutor(
                channel=_Channel(), chat=chat,
                business_tools=BusinessToolRuntime(
                    (), adapter, numeric_goal_binding=True),
                config=AgentTurnConfig(
                    supported_scope="Read order and quotation counts.",
                    ledger_path=root / "run.jsonl",
                    disabled_actions=POTENTIALLY_WRITING_ACTIONS),
                control=ReadOnlyControl(), run_id_factory=lambda message: "goal-run")
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.run_once({
                    "message_id": "goal-task", "text": "Acme 有多少订单？",
                    "page_url": "http://app.test/companies"})
            return result, adapter, chat

    @staticmethod
    def _goal():
        return [{"target_read_tool": "read_orders", "measures": ["count"]}]

    @staticmethod
    def _finish(ref):
        return {"outcome": "answered", "answer": {
            "version": 1, "coverage": "complete",
            "conclusions": [{"kind": "aggregate", "evidence_ref": ref}]}}

    def test_correct_zero_count_can_complete_without_quotations(self):
        result, adapter, chat = self._run([
            ("read_orders", {"answer_goals": self._goal()}),
            ("compute", {"operation": "count", "dataset_ref": "call1-query#1"}),
            ("finish", self._finish("calc#2")),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("0", result.user_answer)
        self.assertEqual(adapter.calls, [("read_orders", {})])
        offered = next(tool for tool in chat.messages[0] if tool["role"] == "system")
        self.assertIn("web application", offered["content"])

    def test_quotation_count_cannot_replace_frozen_order_count(self):
        result, adapter, chat = self._run([
            ("read_orders", {"answer_goals": self._goal()}),
            ("read_quotations", {}),
            ("compute", {"operation": "count", "dataset_ref": "call2-query#1"}),
            ("finish", self._finish("calc#3")),
            ("finish", {"outcome": "unable"}),
        ])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertNotIn("共 2", result.user_answer)
        self.assertEqual(adapter.calls, [("read_orders", {}), ("read_quotations", {})])
        self.assertIn("only unrequested collection facts", str(chat.messages[-1]))

    def test_later_goal_rebinding_is_rejected_before_dispatch(self):
        result, adapter, chat = self._run([
            ("read_orders", {"answer_goals": self._goal()}),
            ("read_quotations", {"answer_goals": [
                {"target_read_tool": "read_quotations", "measures": ["count"]}]}),
            ("compute", {"operation": "count", "dataset_ref": "call1-query#1"}),
            ("finish", self._finish("calc#3")),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(adapter.calls, [("read_orders", {})])
        self.assertIn("already frozen", str(chat.messages[-1]))

    def test_identical_goal_restatement_does_not_waste_a_step(self):
        result, adapter, _ = self._run([
            ("read_orders", {"answer_goals": self._goal()}),
            ("read_quotations", {"answer_goals": self._goal()}),
            ("compute", {"operation": "count", "dataset_ref": "call1-query#1"}),
            ("finish", self._finish("calc#3")),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(adapter.calls, [("read_orders", {}), ("read_quotations", {})])

    def test_missing_first_declaration_never_dispatches_the_read(self):
        result, adapter, chat = self._run([
            ("read_orders", {}),
            ("read_orders", {"answer_goals": self._goal()}),
            ("finish", self._finish("goal-count#2")),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(adapter.calls, [("read_orders", {})])
        self.assertIn("first business read requires answer_goals", str(chat.messages[-1]))

    def test_empty_declaration_cannot_later_claim_complete_count(self):
        result, _, chat = self._run([
            ("read_orders", {"answer_goals": []}),
            ("compute", {"operation": "count", "dataset_ref": "call1-query#1"}),
            ("finish", self._finish("calc#2")),
            ("finish", {"outcome": "unable"}),
        ])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertIn("empty answer_goals", str(chat.messages[-1]))


if __name__ == "__main__":
    unittest.main()
