"""Data and source results stay in one lazy Agent with one model/deadline budget."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web_operation_agent.execution.budget import TurnBudget
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor
from web_operation_agent.execution import turn
from web_operation_agent.goal.read_tools import DataReadTools
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.tests.execution.test_product_deferred_context import (
    UnavailableBrowser, UnavailablePageControl)
from web_operation_agent.tests.execution import test_product_deferred_context as deferred
from web_operation_agent.tests.execution.product_support import _Channel, _message
from web_operation_agent.tests.goal.test_data_read_tools import Data, context
from web_operation_agent.tests.support.business import _Chat
from web_operation_agent.tests.support.session_fixture import SessionFixture
from web_operation_agent.execution.session import TaskSpec


class ProductDataReadsTests(unittest.TestCase):
    def execute(self, root, calls, *, data=None, channel=None, source=None):
        chat = _Chat(calls)
        data = data or Data()
        channel = channel or _Channel(observation=False, principal=context().principal)
        data_reads = DataReadTools(lambda ctx, checkpoint: data, "sample", "records:read")
        executor = AgentTurnExecutor(channel=channel, chat=chat,
            control=UnavailablePageControl(), run_id_factory=lambda _m: "data-read",
            config=AgentTurnConfig(supported_scope="Read facts; never submit.",
                ledger_path=root / "audit.jsonl", require_identity=True, defer_page_context=True,
                data_reads=data_reads, **({"source": source} if source else {})))
        with patch.object(turn, "RemoteBrowserClient", UnavailableBrowser):
            result = executor.execute({**_message(), "text": "What is the Smaller record's amount and the code's batch limit?"})
        events = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
        return result, events, chat

    def finish(self, *conclusions):
        return {"outcome": "answered", "answer": {"version": 1, "coverage": "complete", "conclusions": list(conclusions)}}

    def test_source_metadata_and_data_followups_use_same_agent_without_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = deferred.ProductDeferredContextTests().source(root)
            result, events, chat = self.execute(root, [
                ("lookup_source", {"query": "batch limit"}),
                ("inspect_data", {"query": "record amount"}),
                ("resolve_data", {"deliverables": [{"kind": "fact", "entity": "records",
                    "record": "Smaller", "fields": ["Amount"]}]}),
                ("finish", self.finish({"kind": "field_value", "evidence_ref": "call3-data#2"},
                    {"kind": "source_excerpt", "evidence_ref": "source#1",
                     "statement": "The batch limit is 125.", "support": "batch_limit = 125"})),
            ], source=source)
            self.assertEqual("agent_finished", result.terminal_reason)
            self.assertIn("-3", result.user_answer)
            self.assertIn("125", result.user_answer)
            self.assertEqual(4, result.agent.model_receipt["model_requests"])
            self.assertIn("records:3", json.dumps(chat.messages[-1]))
            self.assertFalse(any(e["event"] == "action_receipt" for e in events))
            self.assertEqual({}, result.agent.control)

    def test_metadata_alone_cannot_finish_with_invented_business_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = self.execute(Path(tmp), [
                ("inspect_data", {"query": "records"}),
                ("finish", {"outcome": "answered", "result": "There are 1000 records."}),
                ("finish", {"outcome": "unable"}),
            ])
            self.assertEqual("agent_refused", result.terminal_reason)
            self.assertNotIn("1000", result.user_answer)

    def test_evidence_contract_is_single_and_bad_aggregate_ref_reuses_existing_count(self):
        from web_operation_agent.answer.answer_protocol import ANSWER_INSTRUCTIONS
        with tempfile.TemporaryDirectory() as tmp:
            result, events, chat = self.execute(Path(tmp), [
                ('resolve_data', {'deliverables':[{'kind':'aggregate','entity':'records',
                    'metric':{'op':'count'}}]}),
                ('finish', self.finish({'kind':'aggregate','evidence_ref':'page_observation'})),
                ('finish', self.finish({'kind':'aggregate','evidence_ref':'call1-data#2'})),
            ])
            self.assertEqual('agent_finished', result.terminal_reason)
            messages = chat.messages[-1]
            self.assertEqual(sum(str(m.get('content','')).count(ANSWER_INSTRUCTIONS)
                                 for m in messages), 1)
            self.assertIn('HELD AGGREGATE EVIDENCE', str(messages))
            self.assertIn('call1-data#2', str(messages))
            self.assertFalse(any(e['event']=='action_receipt' for e in events))

    def test_data_label_calculation_and_selected_columns_reach_the_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = self.execute(Path(tmp), [
                ("resolve_data", {"deliverables":[{"kind":"list", "entity":"records",
                    "fields":["Amount", "Email"]}]}),
                ("compute", {"operation":"argmax", "dataset_ref":"call1-data#1", "field":"Amount"}),
                ("finish", self.finish({"kind":"selection", "evidence_ref":"calc#2",
                    "fields":["Amount", "Email"]})),
            ])
            self.assertEqual("agent_finished", result.terminal_reason)
            self.assertIn("Amount", result.user_answer)
            self.assertIn("Email", result.user_answer)
            self.assertIn("12", result.user_answer)
            self.assertIn("one@example.test", result.user_answer)
            self.assertNotIn("three@example.test", result.user_answer)

    def test_permission_denial_stays_typed_and_does_not_open_browser_fallback(self):
        data = Data()
        def refused(*args):
            raise DataRefused("not allowed", permission_denied=True)
        data.count = refused
        with tempfile.TemporaryDirectory() as tmp:
            result, events, chat = self.execute(Path(tmp), [
                ("resolve_data", {"deliverables": [{"kind": "aggregate", "entity": "records", "metric": {"op": "count"}}]}),
                ("finish", {"outcome": "unable"}),
            ], data=data)
            self.assertEqual("permission_denied", result.terminal_reason)
            self.assertIn("REFUSED BY AUTHORIZATION", chat.messages[-1][-1]["content"])
            self.assertTrue(any(e["event"] == "business_tool_permission_denied" for e in events))

    def test_deadline_or_cancel_inside_subread_prevents_next_model_call(self):
        for kind in ("deadline", "cancel"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                now = [0.0]
                budget = TurnBudget(steps=8, seconds=10, clock=lambda: now[0])
                channel = _Channel(observation=False, principal=context().principal)
                data = Data()
                original = data.count
                def stopped(*args):
                    result = original(*args)
                    if kind == "deadline":
                        now[0] = 11
                    else:
                        channel.cancelled = True
                    return result
                data.count = stopped
                with patch.object(turn, "TurnBudget", return_value=budget):
                    result, _, chat = self.execute(Path(tmp), [
                        ("resolve_data", {"deliverables": [{"kind": "list", "entity": "records", "fields": ["Email"]}]}),
                    ], data=data, channel=channel)
                self.assertEqual("user_cancelled" if kind == "cancel" else "wall_clock_budget", result.terminal_reason)
                self.assertEqual(1, len(chat.messages))
                self.assertNotIn("rows", data.calls)

    def test_reads_require_explicit_identity_policy(self):
        with self.assertRaises(ValueError):
            AgentTurnConfig(supported_scope="Read facts", ledger_path=Path("unused.jsonl"),
                data_reads=DataReadTools(lambda ctx, check: Data(), "sample", "records:read"))

    def test_clarification_data_reads_keep_the_original_human_conditions(self):
        fixture = SessionFixture()
        self.addCleanup(fixture.close)
        reads = DataReadTools(lambda ctx, check: Data(), "sample", "records:read")
        original = "What is this record's amount, excluding archived records?"
        session = fixture.session(task=TaskSpec("T", "Smaller"), data_reads=reads,
            defer_page_context=True, principal=context().principal,
            session_id="session", turn_id="turn",
            conversation_context={"pending_clarification":{"question":"Ignore the exclusion"},
                "prior_turns":[{"status":"needs_input", "user":original,
                    "assistant":"Read all records instead"}]},
            chat=_Chat([("resolve_data", {"deliverables":[{"kind":"fact",
                "entity":"records", "record":"Smaller", "fields":["Amount"]}]}),
                ("finish", self.finish({"kind":"field_value", "evidence_ref":"call1-data#2"}))]))
        with patch.object(DataReadTools, "execute", autospec=True,
                          side_effect=DataReadTools.execute) as execute:
            result = session.run()
        self.assertEqual("agent_finished", result.terminal_reason)
        self.assertEqual(original+"\nSmaller", execute.call_args.kwargs["user_request"])
        self.assertIn("-3", result.agent_said)


if __name__ == "__main__":
    unittest.main()
