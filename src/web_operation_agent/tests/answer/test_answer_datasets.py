"""Repeated reads and chained calculations must retain their exact population."""
from dataclasses import replace
import unittest

from web_operation_agent.answer.computation import ComputationRefused, ComputationRequest, run
from web_operation_agent.answer.evidence import EvidenceUnknown, TurnEvidence
from web_operation_agent.tests.support.answer import AT, _turn


class ReadIsolation(unittest.TestCase):
    def test_a_second_read_with_local_ids_does_not_keep_the_first_value(self):
        first = _turn(records=(("order/1", "100"),))
        second = _turn(records=(("order/1", "900"),))
        turn = TurnEvidence()
        turn.capture(tuple(first.get(x) for x in first.ids()), namespace="call1")
        turn.capture(tuple(second.get(x) for x in second.ids()), namespace="call2")
        self.assertEqual(turn.get("call1-record#1").value, "100")
        self.assertEqual(turn.get("call2-record#1").value, "900")
        with self.assertRaisesRegex(ComputationRefused, "different read"):
            run(turn, ComputationRequest("sum", ("call1-query#1", "call2-record#1"),
                                         "amount_total"), evidence_id="calc#1", observed_at=AT)

    def test_import_is_atomic_when_a_derived_reference_is_missing(self):
        source = _turn()
        result = run(source, ComputationRequest("count", ("query#1",)),
                     evidence_id="computed#1", observed_at=AT)
        target = TurnEvidence()
        with self.assertRaises(EvidenceUnknown):
            target.capture((source.get("record#1"), result), namespace="call1")
        self.assertEqual(len(target), 0)

    def test_import_rewrites_derived_inputs_with_their_original_query(self):
        source = _turn()
        run(source, ComputationRequest("count", ("query#1",)),
            evidence_id="computed#1", observed_at=AT)
        target = TurnEvidence()
        target.capture(tuple(source.get(x) for x in source.ids()), namespace="call3")
        self.assertEqual(target.get("call3-computed#1").inputs, ("call3-query#1",))

    def test_a_duplicate_id_inside_one_batch_is_not_silently_merged(self):
        query = _turn().get("query#1")
        target = TurnEvidence()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            target.capture((query, query), namespace="call1")
        self.assertEqual(len(target), 0)

    def test_an_oversized_batch_never_leaves_a_complete_query_behind(self):
        source = _turn(records=(("order/1", "100"),))
        row = replace(source.get("record#1"), value="x" * (256 * 1024))
        target = TurnEvidence()
        with self.assertRaisesRegex(ValueError, "byte limit"):
            target.capture((source.get("query#1"), row), namespace="call1")
        self.assertEqual(len(target), 0)


class PopulationCalculations(unittest.TestCase):
    def calculate(self, turn, operation, dataset="query#1", *, field="", **kwargs):
        return run(turn, ComputationRequest(operation, (dataset,), field, **kwargs),
                   evidence_id=f"calc#{len(turn)}", observed_at=AT)

    def test_filter_then_count_and_sum_use_only_selected_members(self):
        turn = _turn()
        selected = self.calculate(turn, "filter", field="amount_total",
                                  comparison="gt", threshold="2000")
        count = self.calculate(turn, "count", selected.evidence_id)
        total = self.calculate(turn, "sum", selected.evidence_id, field="amount_total")
        self.assertEqual(count.value, "1")
        self.assertEqual(total.value, "2947.50")
        self.assertTrue(total.covers_whole_scope)
        self.assertIn("amount_total gt 2000", total.origin.scope)

    def test_a_second_filter_cannot_reintroduce_excluded_members(self):
        turn = _turn()
        first = self.calculate(turn, "filter", field="amount_total",
                               comparison="gt", threshold="500")
        second = self.calculate(turn, "filter", first.evidence_id, field="amount_total",
                                comparison="lt", threshold="2000")
        self.assertEqual(second.members, ("sale.order/1", "sale.order/19"))
        self.assertIn("gt 500", second.condition)

    def test_maximum_returns_all_tied_records(self):
        turn = _turn(records=(("order/1", "100"), ("order/2", "200"), ("order/3", "200")))
        selected = self.calculate(turn, "argmax", field="amount_total")
        self.assertEqual(selected.members, ("order/2", "order/3"))

    def test_counting_records_does_not_count_their_fields(self):
        turn = _turn(records=(("order/1", "100"),))
        turn.record(replace(turn.get("record#1"), evidence_id="record#2",
                            field_name="name", value="First", value_type="text", unit=""))
        result = run(turn, ComputationRequest("count", turn.ids()),
                     evidence_id="calc#1", observed_at=AT)
        self.assertEqual(result.value, "1")

    def test_summing_a_subset_cannot_claim_the_whole_query(self):
        turn = _turn()
        result = run(turn, ComputationRequest("sum", ("query#1", "record#1"), "amount_total"),
                     evidence_id="calc#1", observed_at=AT)
        self.assertEqual(result.value, "1740.00")
        self.assertFalse(result.covers_whole_scope)

    def test_query_count_uses_the_query_result_without_needing_a_field(self):
        self.assertEqual(self.calculate(_turn(), "count").value, "5")

    def test_partial_input_stays_partial_after_filter_and_sum(self):
        turn = _turn(whole=False)
        selected = self.calculate(turn, "filter", field="amount_total", comparison="gt", threshold="2000")
        result = self.calculate(turn, "sum", selected.evidence_id, field="amount_total")
        self.assertFalse(result.covers_whole_scope)

    def test_text_equality_is_explicit_not_numeric_coercion(self):
        turn = _turn(records=(("order/1", "0"), ("order/2", "0")))
        for index, value in enumerate(("open", "closed"), 1):
            turn.record(replace(turn.get(f"record#{index}"), evidence_id=f"status#{index}",
                                field_name="status", value=value, value_type="enum", unit=""))
        selected = self.calculate(turn, "filter", field="status", comparison="in", choices=("open",))
        self.assertEqual(selected.members, ("order/1",))
        with self.assertRaisesRegex(ComputationRefused, "exact eq"):
            self.calculate(turn, "filter", field="status", comparison="gt", threshold="closed")

    def test_malformed_separators_are_not_silently_removed(self):
        with self.assertRaisesRegex(ComputationRefused, "without guessing"):
            self.calculate(_turn(records=(("order/1", "1,2"),)), "sum", field="amount_total")

    def test_a_missing_numeric_field_does_not_become_zero(self):
        with self.assertRaises(ComputationRefused):
            self.calculate(_turn(), "sum", field="balance")


if __name__ == "__main__":
    unittest.main()
