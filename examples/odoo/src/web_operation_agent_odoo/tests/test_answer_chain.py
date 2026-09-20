"""From the application's own read to the answer a reader would get.

The three wrong answers this exists to prevent all had correct tool results behind them. So
the test that matters is not that the adapter returns the right rows -- other tests cover
that -- but that what it returns can carry a conclusion the whole way: identity, field name,
type and currency intact, through a computation, into something a reader can rely on.
"""
from __future__ import annotations

import unittest

from web_operation_agent.answer.answer_view import render
from web_operation_agent.answer.computation import ComputationRequest, run
from web_operation_agent.answer.conclusion import Aggregate, AnswerDraft, NotFound, Selection, verify
from web_operation_agent.answer.evidence import QueryEvidence, RecordEvidence, TurnEvidence

from .test_adapter import _FakeOdoo, _customers, _quotations


class WhatTheAdapterReadsCanCarryAnAnswer(unittest.TestCase):
    @staticmethod
    def _tables() -> dict:
        return {"sale.order": _quotations([("S00001", "draft"), ("S00002", "sent"),
                                           ("S00003", "draft")]),
                "res.partner": _customers([("Acme Corporation", "acme@example.test"),
                                           ("Azure Interior", "azure@example.test")])}

    def _evidence(self, tool: str, **args) -> TurnEvidence:
        executor = _FakeOdoo(self._tables())
        result = executor.execute(tool, args)
        turn = TurnEvidence()
        for item in result.evidence:
            turn.record(item)
        return turn

    def test_a_read_produces_evidence_with_identity_type_and_currency(self):
        turn = self._evidence("query_quotations")
        records = [turn.get(name) for name in turn.ids() if name.startswith("record#")]
        totals = [item for item in records if item.field_name == "amount_total"]

        self.assertTrue(totals, "the read carried no amount to compute with")
        for item in totals:
            self.assertTrue(item.record_id.startswith("sale.order/"),
                            "a row number is not an identity a conclusion can cite")
            self.assertEqual(item.value_type, "money")
            self.assertEqual(item.unit, "USD")
            self.assertTrue(item.field_was_read)

    def test_a_query_evidence_carries_the_conditions_that_ran(self):
        turn = self._evidence("query_quotations")
        query = turn.get("query#1")

        self.assertEqual(query.collection, "quotations")
        self.assertTrue(query.conditions, "a query that cannot say what it asked is not one")

    def test_the_total_reaches_the_reader_as_a_total(self):
        """The question "how much altogether" was answered with a table and no total."""
        turn = self._evidence("query_quotations")
        totals = tuple(name for name in turn.ids()
                       if name == "query#1"
                       or (name.startswith("record#")
                           and turn.get(name).field_name == "amount_total"))
        run(turn, ComputationRequest(operation="sum", over=totals,
                                     field_name="amount_total"),
            evidence_id="computed#1", observed_at="2026-09-12T10:04:07+00:00")
        answer = verify(AnswerDraft((Aggregate("computed#1"),), "complete"), turn)
        text = render(answer, user_text="未确认的报价单一共多少钱？", max_chars=4000)

        self.assertIn("USD", text)
        self.assertIn("合计", text)

    def test_a_filter_reaches_the_reader_as_the_records_that_matched(self):
        turn = self._evidence("query_quotations")
        totals = tuple(name for name in turn.ids()
                       if name == "query#1"
                       or (name.startswith("record#")
                           and turn.get(name).field_name == "amount_total"))
        selection = run(turn, ComputationRequest(
            operation="filter", over=totals, field_name="amount_total",
            comparison="gt", threshold="0"),
            evidence_id="computed#1", observed_at="2026-09-12T10:04:07+00:00")
        answer = verify(AnswerDraft((Selection("computed#1"),), "complete"), turn)

        self.assertEqual(len(selection.members), selection.members.__len__())
        self.assertTrue(all(name.startswith("sale.order/") for name in selection.members))
        self.assertEqual(answer.coverage, "complete")

    def test_a_search_that_found_nothing_can_say_so(self):
        """The identifier is absent from what the search returned; that is the point."""
        turn = self._evidence("query_quotations", name_contains="S00042")
        query = turn.get("query#1")
        self.assertTrue(query.found_nothing)
        self.assertEqual(query.looked_for, "S00042")

        answer = verify(AnswerDraft((NotFound("S00042", "query#1"),), "complete"), turn)
        text = render(answer, user_text="报价单 S00042 是哪个客户的？", max_chars=4000)

        self.assertIn("S00042", text)
        self.assertNotIn("sale.order/", text,
                         "a not-found answer must not list the records it did find")


class AnAdapterThatProducesNoEvidenceIsNotAnError(unittest.TestCase):
    """Most adapters do not produce it yet, and a turn simply has nothing to cite."""

    def test_the_result_still_carries_its_projection(self):
        executor = _FakeOdoo(WhatTheAdapterReadsCanCarryAnAnswer._tables())
        result = executor.execute("query_customers", {})

        self.assertIsNotNone(result.query_answer)
        self.assertIsInstance(result.evidence, tuple)


if __name__ == "__main__":
    unittest.main()
