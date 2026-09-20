"""The distinctions evidence exists to keep, each one traceable to an answer that went wrong.

Every test here names the failure it prevents. Four answers against a live Odoo were wrong
in four different ways, and three of them were wrong because two states had been folded into
one: not-read folded into empty, a page label folded into a value, a partial read folded into
a whole. The fourth needed a kind of evidence that has no record at all.
"""
from __future__ import annotations

import unittest

from web_operation_agent.answer.evidence import (
    ComputationEvidence, EvidenceUnknown, Origin, PageEvidence, QueryEvidence, RecordEvidence,
    TurnEvidence,
)


def _origin(kind: str = "typed", **over) -> Origin:
    return Origin(kind=kind, reference=over.get("reference", "query_quotations#1"),
                  observed_at=over.get("observed_at", "2026-09-12T10:04:07+00:00"),
                  scope=over.get("scope", "open quotations, neither confirmed nor cancelled"))


class NotReadIsNotEmpty(unittest.TestCase):
    """An answer said a customer's phone and mobile "are both empty".

    The read had asked for name, email, city and country; the page showed the labels and no
    values. The phone was `(603)-996-3829`. Both states have to be expressible, and they
    have to be different.
    """

    def test_a_field_that_was_read_and_holds_nothing_says_so(self):
        item = RecordEvidence(
            evidence_id="record#1", origin=_origin(), record_id="res.partner/10",
            field_name="mobile", field_meaning="mobile telephone",
            field_was_read=True, value=None, value_type="text")

        self.assertTrue(item.is_known_empty)

    def test_a_field_that_was_never_read_is_not_empty(self):
        item = RecordEvidence(
            evidence_id="record#2", origin=_origin(), record_id="res.partner/10",
            field_name="phone", field_meaning="telephone", field_was_read=False)

        self.assertFalse(item.is_known_empty)

    def test_an_unread_field_cannot_be_given_a_value(self):
        """Otherwise the two states collapse again at the first careless construction."""
        with self.assertRaisesRegex(ValueError, "not read cannot carry a value"):
            RecordEvidence(
                evidence_id="record#3", origin=_origin(), record_id="res.partner/10",
                field_name="phone", field_meaning="telephone",
                field_was_read=False, value="(603)-996-3829")


class APageLabelIsNotAValue(unittest.TestCase):
    """The Runtime's page text carries form labels, not the contents of their inputs.

    Reading "Phone" off a record page establishes that the control is there. It was read as
    establishing that the field was empty.
    """

    def test_a_control_with_no_readable_value_cannot_carry_one(self):
        with self.assertRaisesRegex(ValueError, "control with no readable value"):
            PageEvidence(
                evidence_id="page#1", origin=_origin("page", reference="observation#4"),
                page_route="/odoo/customers/10", saw="control_only",
                detail="a Phone field label with no readable value",
                value="(603)-996-3829")

    def test_a_value_seen_on_the_page_must_be_carried(self):
        with self.assertRaisesRegex(ValueError, "must carry it"):
            PageEvidence(
                evidence_id="page#2", origin=_origin("page", reference="observation#4"),
                page_route="/odoo/vendor-bills", saw="value", detail="Tax Excluded column")

    def test_page_evidence_cannot_claim_to_be_a_typed_read(self):
        """One answer described reading the DOM as "through the read-only API"."""
        with self.assertRaisesRegex(ValueError, "must come from a page observation"):
            PageEvidence(
                evidence_id="page#3", origin=_origin("typed"),
                page_route="/odoo/vendor-bills", saw="text", detail="three bill rows")


class FindingNothingIsEvidence(unittest.TestCase):
    """`S00042` is necessarily absent from what a search for it returns.

    The previous design would have refused "S00042 is not among the open quotations" for
    exactly that reason -- the one correct answer it was built to allow.
    """

    def test_a_query_that_matched_nothing_is_still_evidence(self):
        item = QueryEvidence(
            evidence_id="query#1", origin=_origin(), collection="quotations",
            conditions=("name contains S00042",), matched_count=0,
            records_scanned=5, covers_whole_scope=True)

        self.assertTrue(item.found_nothing)
        self.assertEqual(item.conditions, ("name contains S00042",))

    def test_the_conditions_are_what_it_stands_on(self):
        with self.assertRaisesRegex(ValueError, "condition must not be empty"):
            QueryEvidence(evidence_id="query#2", origin=_origin(), collection="quotations",
                          conditions=("",), matched_count=0)


class ANumberTravelsWithItsUnit(unittest.TestCase):
    """An answer reported a tax-excluded column as the amount owed: 541.10 against 622.27."""

    def test_money_without_a_currency_is_refused(self):
        with self.assertRaisesRegex(ValueError, "must carry its unit or currency"):
            RecordEvidence(
                evidence_id="record#4", origin=_origin(), record_id="sale.order/2",
                field_name="amount_total", field_meaning="order total",
                field_was_read=True, value="2947.50", value_type="money")

    def test_a_computed_total_without_a_currency_is_refused(self):
        with self.assertRaisesRegex(ValueError, "must carry its unit or currency"):
            ComputationEvidence(
                evidence_id="computed#1", origin=_origin("computed", reference="sum#1"),
                operation="sum(amount_total)", inputs=("query#1",),
                value="7210.00", value_type="money")


class AComputationNamesWhatItRanOn(unittest.TestCase):
    def test_a_computation_over_evidence_from_another_turn_is_refused(self):
        """Whoever reads the answer has to be able to recompute it from this turn."""
        turn = TurnEvidence()
        with self.assertRaisesRegex(EvidenceUnknown, "does not have"):
            turn.record(ComputationEvidence(
                evidence_id="computed#1", origin=_origin("computed", reference="sum#1"),
                operation="sum(amount_total)", inputs=("query#99",),
                value="7210.00", value_type="money", unit="USD"))

    def test_a_computation_over_recorded_evidence_is_accepted(self):
        turn = TurnEvidence()
        turn.record(QueryEvidence(
            evidence_id="query#1", origin=_origin(), collection="quotations",
            conditions=("state in draft,sent",), matched_count=5, covers_whole_scope=True))
        name = turn.record(ComputationEvidence(
            evidence_id="computed#1", origin=_origin("computed", reference="sum#1"),
            operation="sum(amount_total)", inputs=("query#1",),
            value="7210.00", value_type="money", unit="USD"))

        self.assertEqual(name, "computed#1")
        self.assertEqual(len(turn), 2)

    def test_a_computation_must_name_something(self):
        with self.assertRaisesRegex(ValueError, "must name the evidence"):
            ComputationEvidence(
                evidence_id="computed#2", origin=_origin("computed", reference="sum#2"),
                operation="count()", inputs=(), value="5", value_type="count")


class OneTurnHoldsItsOwnEvidence(unittest.TestCase):
    def test_the_same_id_cannot_be_recorded_twice(self):
        turn = TurnEvidence()
        item = QueryEvidence(evidence_id="query#1", origin=_origin(), collection="quotations",
                             conditions=("all",), matched_count=5)
        turn.record(item)
        with self.assertRaisesRegex(ValueError, "already recorded"):
            turn.record(item)

    def test_a_missing_reference_names_every_one_that_is_missing(self):
        """A conclusion citing three absent ids should not be debugged three times."""
        turn = TurnEvidence()
        with self.assertRaises(EvidenceUnknown) as caught:
            turn.require_all(("query#7", "record#8"))

        self.assertIn("query#7", str(caught.exception))
        self.assertIn("record#8", str(caught.exception))

    def test_an_id_has_a_shape(self):
        with self.assertRaisesRegex(ValueError, "<kind>#<number>"):
            QueryEvidence(evidence_id="not an id", origin=_origin(),
                          collection="quotations", conditions=("all",), matched_count=1)


class ScopeIsNotACompletenessVerdict(unittest.TestCase):
    """Four questions were compressed into one `source_complete` boolean.

    Whether every record was seen, every field was read, everything this user may see was
    covered, and whether the field means what the answer assumes, are different. The answers
    that went wrong went wrong in the seams.
    """

    def test_covering_the_scope_and_reading_a_field_are_separate_facts(self):
        query = QueryEvidence(
            evidence_id="query#1", origin=_origin(), collection="customers",
            conditions=("name contains Acme",), matched_count=1, records_scanned=2,
            covers_whole_scope=True)
        unread = RecordEvidence(
            evidence_id="record#1", origin=_origin(), record_id="res.partner/10",
            field_name="phone", field_meaning="telephone", field_was_read=False)

        self.assertTrue(query.covers_whole_scope)
        self.assertFalse(unread.field_was_read)


if __name__ == "__main__":
    unittest.main()
