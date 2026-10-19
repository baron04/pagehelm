"""Summary delivery shares the existing turn-local references and fail-closed budget."""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.answer.evidence import Origin
from web_operation_agent.execution.model_loop_contracts import LoopToolResult
from web_operation_agent.execution.read_summary import (
    SummaryColumn, SummaryRead, SummaryRow, SummarySpec, compute_summary)
from web_operation_agent.execution.turn_trace import TurnTrace
from web_operation_agent.privacy import SessionHandoffPolicy
from web_operation_agent.tests.execution.test_product_model_loop import LoopFixture, call, reply
from web_operation_agent.tests.goal.test_data_read_tools import context


def summary(*, actor=None, count=0, complete=True):
    operation = context()
    return compute_summary(SummaryRead(
        Origin("typed", "native collection", "2026-10-18T12:00:00Z", "status=active"),
        actor or operation.principal.actor_authority_digest,
        (SummaryColumn("value", "number", "kg"),),
        tuple(SummaryRow(str(i), (Decimal("0.1"),)) for i in range(count)), count, count, complete),
        SummarySpec.from_dict({"measures": [{"op": "count", "as": "count"},
            {"op": "sum", "column": "value", "as": "total"}]}))


class SummaryDeliveryTests(unittest.TestCase):
    def test_native_text_delivery_cannot_cross_actor_even_when_display_text_matches(self):
        from web_operation_agent.answer.answering import StructuredQueryAnswer
        projection = StructuredQueryAnswer(collection="items", fields=("name",), records=(("same",),),
            source="native", scope="all", observed_at="now", source_complete=True,
            result_truncated=False, matched_count=1, source_total=1, records_scanned=1,
            actor_digest="f" * 64, record_ids=("actual-id",))
        loop = self.fixture(projection)
        answer = loop.run("List every name")
        self.assertIn("could not be fully verified", answer)
        self.assertFalse(any(event == "read_answer_selected" for event, _ in loop.events))

    def fixture(self, projection, *, finish=None):
        result = LoopToolResult("Summary prepared", kind="read", query_answer=projection)
        loop = LoopFixture([reply(call("summary")),
            reply(call("finish", status="done", answer="", read_answer="read-1")),
            reply(call("finish", status="cannot", answer="The requested result could not be fully verified."))],
            lambda *_: result)
        loop.state.approvals = NS(principal=context().principal)
        loop.config = NS(max_user_answer_chars=8000)
        return loop

    def test_zero_population_still_delivers_a_count_and_exact_sum_with_source(self):
        loop = self.fixture(summary())
        answer = loop.run()
        self.assertIn("| 0 | 0 |", answer)
        self.assertIn("status=active", answer)
        self.assertEqual([facts["records"] for event, facts in loop.events if event == "read_answer_selected"], [1])

    def test_foreign_actor_or_incomplete_read_cannot_be_delivered(self):
        for projection in (summary(actor="f" * 64), summary(count=1, complete=False)):
            with self.subTest(projection=projection):
                loop = self.fixture(projection)
                self.assertIn("could not be fully verified", loop.run())
                self.assertFalse(any(event == "read_answer_selected" for event, _ in loop.events))

    def test_summary_cannot_be_replaced_with_model_prose_or_an_old_reference(self):
        projection = summary(count=3)
        for bad in ({"answer": "The total is 999"}, {"answer": "", "read_answer": "old-turn"}):
            loop = self.fixture(projection)
            loop.state.chat.replies.insert(1, reply(call("finish", status="done", **bad)))
            answer = loop.run()
            self.assertIn("0.3", answer)
            self.assertNotIn("999", answer)
            self.assertEqual(len(loop.state.chat.seen), 3)

    def test_server_rendered_roster_uses_the_configured_budget_without_raising_model_text_limit(self):
        rows=tuple(SummaryRow(str(i),(f"Item {i:03d} " + "x"*90,)) for i in range(100))
        projection=compute_summary(SummaryRead(Origin("typed","native list","now","all"),
            context().principal.actor_authority_digest,(SummaryColumn("name","text"),),rows,100,100,True),
            SummarySpec.from_dict({"columns":["name"],"order_by":[{"column":"name","direction":"asc"}]}))
        loop=self.fixture(projection)
        loop.config=NS(max_user_answer_chars=12000)
        self.assertEqual(8000,loop._finish_tool({})["function"]["parameters"]["properties"]["answer"]["maxLength"])
        answer=loop.run("List all names")
        self.assertGreater(len(answer),8000)
        self.assertIn("Item 099",answer)
        self.assertTrue(any(event=="read_answer_selected" for event,_ in loop.events))

    def test_over_budget_result_is_not_replaced_by_a_partial_table(self):
        loop = self.fixture(summary(count=3))
        loop.config = NS(max_user_answer_chars=512)
        # A real, bounded source scope can consume the remaining delivery budget.
        loop.handler = lambda *_: LoopToolResult("Summary", kind="read", query_answer=replace(
            summary(count=3), origin=Origin("typed", "collection", "now", "scope " * 180)))
        self.assertIn("could not be fully verified", loop.run())

    def test_page_auto_completion_cannot_drop_an_existing_summary_or_exact_list(self):
        from web_operation_agent.tests.execution.test_product_model_loop import ModelLoopTests
        for projection in (summary(count=3), ModelLoopTests.read_projection(count=3)):
            def handler(loop, name, arguments):
                if name == "read":
                    return LoopToolResult("Prepared", kind="read", query_answer=projection)
                loop._dispatch(loop.state, "goto", {"url": "https://synthetic.test/list"})
                return LoopToolResult("Opened", kind="page", dispatch="acknowledged")
            loop = LoopFixture([reply(call("read")),
                reply(call("goto", path="/list", answer="All results are above")),
                reply(call("finish", status="done", answer="", read_answer="read-1"))], handler)
            loop.state.approvals = NS(principal=context().principal)
            answer = loop.run("Show the result and open its page")
            self.assertNotIn("All results are above", answer)
            self.assertEqual(len(loop.state.chat.seen), 3)
            self.assertTrue(any(event == "read_answer_selected" for event, _ in loop.events))

    def test_selected_server_values_are_only_recorded_in_opt_in_private_trace(self):
        loop = self.fixture(summary(count=3))
        with TemporaryDirectory() as tmp:
            trace = TurnTrace(Path(tmp), SessionHandoffPolicy.extending(()))
            loop._trace = trace
            answer = loop.run()
            path = trace.write(run_id="r", turn_id="t", prompt="total?", start_route="/list",
                terminal_reason="agent_finished", wall_seconds=1, answer=answer)
            record = json.loads(path.read_text())
        delivery = record["server_deliveries"][0]
        self.assertEqual(delivery["rendered_text"], answer)
        self.assertEqual(delivery["projection"]["matched_record_ids"], ["0", "1", "2"])
        self.assertEqual(delivery["projection"]["origin"]["scope"], "status=active")
        event = next(facts for name, facts in loop.events if name == "read_answer_selected")
        self.assertEqual(set(event), {"ref", "records", "digest"})

    def test_private_delivery_with_a_credential_is_withheld_instead_of_logged(self):
        with TemporaryDirectory() as tmp:
            trace = TurnTrace(Path(tmp), SessionHandoffPolicy.extending(()))
            trace.record_delivery(reference="read-1", projection={"password": "private-secret"},
                rendered_text="x", presented_records=1, presentation_complete=True)
            path = trace.write(run_id="r", turn_id="t", prompt="q", start_route="/",
                terminal_reason="agent_finished", wall_seconds=1, answer="x")
            record = json.loads(path.read_text())
        self.assertEqual(record["withheld"], "credential_form")
        self.assertNotIn("private-secret", json.dumps(record))

    def test_selected_source_is_bound_to_its_read_not_the_latest_read(self):
        projection = summary(count=3)
        def handler(loop, name, arguments):
            return LoopToolResult("Prepared", kind="read", query_answer=projection,
                                  read_key="/native/first" if name == "first" else "/native/later")
        loop = LoopFixture([reply(call("first")), reply(call("later")),
            reply(call("finish", status="done", answer="", read_answer="read-1"))], handler)
        loop.state.approvals = NS(principal=context().principal)
        loop.config = NS(max_user_answer_chars=8000)
        with TemporaryDirectory() as tmp:
            trace = TurnTrace(Path(tmp), SessionHandoffPolicy.extending(()))
            loop._trace = trace
            answer = loop.run()
            record = json.loads(trace.write(run_id="r", turn_id="t", prompt="q", start_route="/",
                terminal_reason="agent_finished", wall_seconds=1, answer=answer).read_text())
        self.assertEqual(record["server_deliveries"][0]["source_reference"], "/native/first")
        self.assertNotIn("source_reference", {k for _, facts in loop.events for k in facts})

    def test_credential_in_private_source_reference_withholds_entire_record(self):
        with TemporaryDirectory() as tmp:
            trace = TurnTrace(Path(tmp), SessionHandoffPolicy.extending(()))
            trace.record_delivery(reference="read-1", projection={}, rendered_text="safe",
                presented_records=0, presentation_complete=True,
                source_reference="/native?password=private-secret")
            record = json.loads(trace.write(run_id="r", turn_id="t", prompt="q", start_route="/",
                terminal_reason="agent_finished", wall_seconds=1, answer="safe").read_text())
        self.assertEqual(record["withheld"], "credential_form")
        self.assertNotIn("private-secret", json.dumps(record))


if __name__ == "__main__":
    unittest.main()
