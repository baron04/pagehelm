"""The arithmetic, and every way two inputs can fail to add up.

The values are the ones a live Odoo returned on 2026-09-12, so the numbers here are the
numbers the wrong answers got wrong: five open quotations totalling 7,210.00, of which one
is over 2,000.
"""
from __future__ import annotations

import unittest

from web_operation_agent.answer.computation import ComputationRefused, ComputationRequest, run
from web_operation_agent.answer.evidence import (
    Origin, QueryEvidence, RecordEvidence, TurnEvidence,
)
from web_operation_agent.tests.support.answer import AT, QUOTATIONS, SCOPE, _turn


def _over(turn: TurnEvidence) -> tuple[str, ...]:
    return tuple(name for name in turn.ids())


class TheTotalIsPerformedNotParaphrased(unittest.TestCase):
    """An answer that asked for the total got a table with no total in it.

    The model had computed 7,210.00 correctly and the product discarded the sentence. The
    number now comes from here, and carries what it is a total *of*.
    """

    def test_summing_the_open_quotations(self):
        turn = _turn()
        result = run(turn, ComputationRequest(operation="sum", over=_over(turn),
                                              field_name="amount_total"),
                     evidence_id="computed#1", observed_at=AT)

        self.assertEqual(result.value, "7210.00")
        self.assertEqual(result.unit, "USD")
        self.assertTrue(result.covers_whole_scope)

    def test_the_scale_the_inputs_had_is_the_scale_the_total_has(self):
        turn = _turn()
        result = run(turn, ComputationRequest(operation="sum", over=_over(turn),
                                              field_name="amount_total"),
                     evidence_id="computed#1", observed_at=AT)
        self.assertEqual(result.value, "7210.00", "a money total must not lose a decimal")

    def test_counting_needs_no_field(self):
        turn = _turn()
        result = run(turn, ComputationRequest(operation="count", over=_over(turn)),
                     evidence_id="computed#1", observed_at=AT)
        self.assertEqual(result.value, "5")
        self.assertEqual(result.value_type, "count")

    def test_exact_empty_sum_uses_certified_field_without_inventing_currency(self):
        turn = TurnEvidence()
        turn.record(QueryEvidence(
            evidence_id="query#1",
            origin=Origin("typed", "read_orders", AT, "orders for exact customer"),
            collection="orders", conditions=("customer id = 10",),
            matched_count=0, records_scanned=0, covers_whole_scope=True,
            count_exact=True, summable_fields=("amount_total",)))
        result = run(turn, ComputationRequest(
            operation="sum", over=("query#1",), field_name="amount_total"),
            evidence_id="computed#1", observed_at=AT)
        self.assertEqual((result.value, result.value_type, result.unit),
                         ("0", "number", ""))
        self.assertTrue(result.covers_whole_scope)

    def test_empty_sum_does_not_accept_an_untyped_or_partial_field(self):
        turn = TurnEvidence()
        turn.record(QueryEvidence(
            evidence_id="query#1",
            origin=Origin("typed", "read_orders", AT, "orders for exact customer"),
            collection="orders", conditions=("customer id = 10",),
            matched_count=0, records_scanned=0, covers_whole_scope=True,
            count_exact=True))
        with self.assertRaisesRegex(ComputationRefused, "no records"):
            run(turn, ComputationRequest(
                operation="sum", over=("query#1",), field_name="amount_total"),
                evidence_id="computed#1", observed_at=AT)


class AFilterIsASelectionNotARowDump(unittest.TestCase):
    """"Which quotations are over 2000" was answered with all five.

    Four of them are not. The selection carries the condition that ran and the records it
    kept, so the answer cannot widen back to everything the query returned.
    """

    def test_only_the_record_over_the_threshold_is_kept(self):
        turn = _turn()
        result = run(turn, ComputationRequest(operation="filter", over=_over(turn),
                                              field_name="amount_total",
                                              comparison="gt", threshold="2000"),
                     evidence_id="computed#1", observed_at=AT)

        self.assertEqual(result.members, ("sale.order/2",))
        self.assertEqual(result.condition, "amount_total gt 2000")

    def test_keeping_nothing_is_a_result(self):
        """"None of them are over 2000" is an answer, and not the same as "I could not tell"."""
        turn = _turn()
        result = run(turn, ComputationRequest(operation="filter", over=_over(turn),
                                              field_name="amount_total",
                                              comparison="gt", threshold="99999"),
                     evidence_id="computed#1", observed_at=AT)
        self.assertEqual(result.members, ())


class ASubtotalSaysSo(unittest.TestCase):
    """A sum over the fifty records one page returned is not the total of two hundred."""

    def test_a_sum_over_a_partial_query_does_not_claim_the_whole_scope(self):
        turn = _turn(whole=False, records=QUOTATIONS[:2])
        result = run(turn, ComputationRequest(operation="sum", over=_over(turn),
                                              field_name="amount_total"),
                     evidence_id="computed#1", observed_at=AT)

        self.assertEqual(result.value, "4687.50")
        self.assertFalse(result.covers_whole_scope,
                         "a partial input must not produce a whole-scope result")


class InputsThatDoNotAddUpAreRefusedByName(unittest.TestCase):
    def test_two_currencies_do_not_total(self):
        turn = _turn()
        turn.record(RecordEvidence(
            evidence_id="record#9",
            origin=Origin(kind="typed", reference="query_quotations#1", observed_at=AT,
                          scope=SCOPE),
            record_id="sale.order/20", field_name="amount_total",
            field_meaning="order total", field_was_read=True, value="100.00",
            value_type="money", unit="EUR"))
        with self.assertRaisesRegex(ComputationRefused, "EUR, USD|USD, EUR"):
            run(turn, ComputationRequest(operation="sum", over=_over(turn),
                                         field_name="amount_total"),
                evidence_id="computed#1", observed_at=AT)

    def test_the_same_record_twice_would_double_the_total(self):
        turn = _turn()
        turn.record(RecordEvidence(
            evidence_id="record#9",
            origin=Origin(kind="typed", reference="query_quotations#2", observed_at=AT,
                          scope=SCOPE),
            record_id="sale.order/1", field_name="amount_total",
            field_meaning="order total", field_was_read=True, value="1740.00",
            value_type="money", unit="USD"))
        with self.assertRaisesRegex(ComputationRefused, "same record was given twice"):
            run(turn, ComputationRequest(operation="sum", over=_over(turn),
                                         field_name="amount_total"),
                evidence_id="computed#1", observed_at=AT)

    def test_a_field_that_was_never_read_has_nothing_to_add(self):
        turn = _turn()
        turn.record(RecordEvidence(
            evidence_id="record#9",
            origin=Origin(kind="typed", reference="query_quotations#1", observed_at=AT,
                          scope=SCOPE),
            record_id="sale.order/20", field_name="amount_total",
            field_meaning="order total", field_was_read=False))
        with self.assertRaisesRegex(ComputationRefused, "never read"):
            run(turn, ComputationRequest(operation="sum", over=_over(turn),
                                         field_name="amount_total"),
                evidence_id="computed#1", observed_at=AT)

    def test_reads_from_another_scope_describe_another_population(self):
        turn = _turn()
        turn.record(RecordEvidence(
            evidence_id="record#9",
            origin=Origin(kind="typed", reference="query_orders#1", observed_at=AT,
                          scope="confirmed sales orders"),
            record_id="sale.order/7", field_name="amount_total",
            field_meaning="order total", field_was_read=True, value="1000.00",
            value_type="money", unit="USD"))
        with self.assertRaisesRegex(ComputationRefused, "different scope"):
            run(turn, ComputationRequest(operation="sum", over=_over(turn),
                                         field_name="amount_total"),
                evidence_id="computed#1", observed_at=AT)

    def test_two_queries_are_two_populations(self):
        turn = _turn()
        turn.record(QueryEvidence(
            evidence_id="query#2",
            origin=Origin(kind="typed", reference="query_orders#1", observed_at=AT,
                          scope=SCOPE),
            collection="orders", conditions=("state = sale",), matched_count=15))
        with self.assertRaisesRegex(ComputationRefused, "exactly one query"):
            run(turn, ComputationRequest(operation="count", over=_over(turn)),
                evidence_id="computed#1", observed_at=AT)

    def test_a_value_this_cannot_read_is_refused_rather_than_guessed(self):
        """"$ -541.10" off a page is not a number this may quietly coerce."""
        turn = _turn(records=(("sale.order/1", "$ -541.10"),), unit="USD")
        with self.assertRaisesRegex(ComputationRefused, "without guessing"):
            run(turn, ComputationRequest(operation="sum", over=_over(turn),
                                         field_name="amount_total"),
                evidence_id="computed#1", observed_at=AT)

    def test_reading_the_wrong_field_is_named(self):
        turn = _turn()
        with self.assertRaisesRegex(ComputationRefused, "not payment_term"):
            run(turn, ComputationRequest(operation="sum", over=_over(turn),
                                         field_name="payment_term"),
                evidence_id="computed#1", observed_at=AT)


class TheOperationSetIsBoundedNotTheQuestions(unittest.TestCase):
    def test_an_operation_this_does_not_run_says_what_it_knows(self):
        with self.assertRaisesRegex(ComputationRefused, "is not an operation"):
            ComputationRequest(operation="regress", over=("query#1",), field_name="x")

    def test_a_filter_needs_a_comparison_and_something_to_compare_with(self):
        with self.assertRaisesRegex(ComputationRefused, "compares with one of"):
            ComputationRequest(operation="filter", over=("query#1",),
                               field_name="amount_total", threshold="2000")
        with self.assertRaisesRegex(ComputationRefused, "needs something to compare"):
            ComputationRequest(operation="filter", over=("query#1",),
                               field_name="amount_total", comparison="gt")

    def test_a_result_becomes_evidence_of_this_turn(self):
        turn = _turn()
        before = len(turn)
        run(turn, ComputationRequest(operation="count", over=_over(turn)),
            evidence_id="computed#1", observed_at=AT)
        self.assertEqual(len(turn), before + 1)
        self.assertIn("computed#1", turn)


if __name__ == "__main__":
    unittest.main()
