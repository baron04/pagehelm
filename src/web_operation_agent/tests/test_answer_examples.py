"""The seven cases the contract says an answer must get right, end to end.

Each one is a question a live Odoo was asked on 2026-09-12, with the values it returned, and
each one was answered wrongly by the product as it stood. They run here from evidence through
computation and conclusions to the text a reader would see, because that whole path is what
failed -- the arithmetic was right, the tool results were right, and the reader still got a
table of five unrelated records.

Contract: `.workbench/phase-6/answer-contract.md` §6.
"""
from __future__ import annotations

import unittest

from web_operation_agent.answer_view import render
from web_operation_agent.computation import ComputationRequest, run
from web_operation_agent.conclusion import (
    Aggregate, AnswerDraft, ConclusionRefused, FieldValue, NotFound, Selection, Undetermined,
    verify,
)
from web_operation_agent.evidence import (
    Origin, PageEvidence, QueryEvidence, RecordEvidence, TurnEvidence,
)

AT = "2026-09-12T10:04:07+00:00"
QUOTES = "open quotations, neither confirmed nor cancelled"
CUSTOMERS = "customers of this company"

#: What the application returned for the five open quotations.
OPEN = (("sale.order/1", "1740.00"), ("sale.order/2", "2947.50"), ("sale.order/3", "377.50"),
        ("sale.order/5", "405.00"), ("sale.order/19", "1740.00"))


def _typed(reference: str, scope: str) -> Origin:
    return Origin(kind="typed", reference=reference, observed_at=AT, scope=scope)


def _quotation_turn(*, whole: bool = True) -> TurnEvidence:
    turn = TurnEvidence()
    turn.record(QueryEvidence(
        evidence_id="query#1", origin=_typed("query_quotations#1", QUOTES),
        collection="quotations", conditions=("state in draft,sent",),
        matched_count=len(OPEN), records_scanned=len(OPEN), covers_whole_scope=whole))
    for index, (record_id, amount) in enumerate(OPEN, start=1):
        turn.record(RecordEvidence(
            evidence_id=f"record#{index}", origin=_typed("query_quotations#1", QUOTES),
            record_id=record_id, field_name="amount_total", field_meaning="报价单金额",
            field_was_read=True, value=amount, value_type="money", unit="USD"))
    return turn


class Example1AGroundedLookup(unittest.TestCase):
    """哪些报价单还没确认？ -- five, and the answer says which condition found them."""

    def test_the_selection_carries_the_condition_that_found_them(self):
        turn = _quotation_turn()
        selection = run(turn, ComputationRequest(
            operation="filter", over=turn.ids(), field_name="amount_total",
            comparison="gt", threshold="0"), evidence_id="computed#1", observed_at=AT)
        answer = verify(AnswerDraft((Selection("computed#1"),), "complete"), turn)

        self.assertEqual(len(selection.members), 5)
        self.assertEqual(answer.coverage, "complete")
        text = render(answer, user_text="哪些报价单还没确认？", max_chars=4000)
        self.assertIn("sale.order/2", text)
        self.assertIn(QUOTES, text)


class Example2NotFoundWithinAScope(unittest.TestCase):
    """报价单 S00042 是哪个客户的？

    The answer was a table of the five that do exist, with no mention of S00042. The
    identifier is necessarily absent from what a search for it returns, which is why this
    conclusion stands on the query rather than on any value.
    """

    def test_the_answer_names_what_was_looked_for_without_internal_filter_syntax(self):
        turn = TurnEvidence()
        turn.record(QueryEvidence(
            evidence_id="query#1", origin=_typed("query_quotations#1", QUOTES),
            collection="quotations", conditions=("name contains S00042",),
            matched_count=0, records_scanned=5, covers_whole_scope=True))
        answer = verify(AnswerDraft((NotFound("S00042", "query#1"),), "complete"), turn)
        text = render(answer, user_text="报价单 S00042 是哪个客户的？", max_chars=4000)

        self.assertIn("S00042", text)
        self.assertIn("未找到", text)
        self.assertNotIn("name contains", text)
        # Absence is claimed only for what this actor could search, never system-wide.
        self.assertIn("当前账号有权访问的范围", text)
        self.assertIn("不代表整个系统中不存在", text)
        english = render(answer, user_text="Which customer owns S00042?", max_chars=4000)
        self.assertIn("the current account can access", english)
        self.assertIn("not a claim that it exists nowhere", english)

    def test_the_five_unrelated_records_cannot_be_the_answer(self):
        """A query that matched five does not establish that a sixth is absent."""
        turn = _quotation_turn()
        with self.assertRaisesRegex(ConclusionRefused, "matched 5 records"):
            verify(AnswerDraft((NotFound("S00042", "query#1"),), "complete"), turn)


class Example3ATotal(unittest.TestCase):
    """未确认的报价单一共多少钱？ -- 7210.00 USD, and it is a total, not a subtotal."""

    def test_the_total_is_computed_and_carries_its_currency(self):
        turn = _quotation_turn()
        run(turn, ComputationRequest(operation="sum", over=turn.ids(),
                                     field_name="amount_total"),
            evidence_id="computed#1", observed_at=AT)
        answer = verify(AnswerDraft((Aggregate("computed#1"),), "complete"), turn)
        text = render(answer, user_text="未确认的报价单一共多少钱？", max_chars=4000)

        self.assertEqual(answer.coverage, "complete")
        self.assertIn("7210.00", text)
        self.assertIn("USD", text)
        self.assertIn("合计", text)

    def test_a_total_over_part_of_the_scope_is_called_a_subtotal(self):
        turn = _quotation_turn(whole=False)
        run(turn, ComputationRequest(operation="sum", over=turn.ids(),
                                     field_name="amount_total"),
            evidence_id="computed#1", observed_at=AT)
        answer = verify(AnswerDraft((Aggregate("computed#1"),), "complete"), turn)
        text = render(answer, user_text="一共多少钱？", max_chars=4000)

        self.assertEqual(answer.coverage, "partial", "a subtotal cannot answer completely")
        self.assertIn("小计", text)


class Example4AFilter(unittest.TestCase):
    """未确认报价单里金额超过 2000 的有哪些？ -- one, and the answer shows one."""

    def test_only_the_matching_record_reaches_the_reader(self):
        turn = _quotation_turn()
        run(turn, ComputationRequest(operation="filter", over=turn.ids(),
                                     field_name="amount_total",
                                     comparison="gt", threshold="2000"),
            evidence_id="computed#1", observed_at=AT)
        answer = verify(AnswerDraft((Selection("computed#1"),), "complete"), turn)
        text = render(answer, user_text="金额超过 2000 的有哪些？", max_chars=4000)

        self.assertIn("sale.order/2", text)
        for absent in ("sale.order/1", "sale.order/3", "sale.order/5", "sale.order/19"):
            self.assertNotIn(absent, text, "a record that does not match must not be listed")


class Example5aAFieldThatWasNeverRead(unittest.TestCase):
    """Acme 的电话是多少？ -- answered "the field is empty". It was (603)-996-3829."""

    def _turn(self) -> TurnEvidence:
        turn = TurnEvidence()
        turn.record(RecordEvidence(
            evidence_id="record#1", origin=_typed("query_customers#1", CUSTOMERS),
            record_id="res.partner/10", field_name="phone", field_meaning="电话",
            field_was_read=False))
        turn.record(PageEvidence(
            evidence_id="page#1",
            origin=Origin(kind="page", reference="observation#3", observed_at=AT,
                          scope="the customer record page as rendered"),
            page_route="/odoo/customers/10", saw="control_only",
            detail="a Phone field label"))
        return turn

    def test_an_unread_field_cannot_be_reported_as_a_value(self):
        turn = self._turn()
        with self.assertRaisesRegex(ConclusionRefused, "never read"):
            verify(AnswerDraft((FieldValue("res.partner/10", "phone", "record#1"),),
                               "complete"), turn)

    def test_a_page_showing_only_the_label_cannot_be_reported_as_empty(self):
        turn = self._turn()
        answer = verify(AnswerDraft(
            (Undetermined("Acme 的电话", "page#1"),), "complete"), turn)
        text = render(answer, user_text="Acme 的电话是多少？", max_chars=4000)

        self.assertEqual(answer.coverage, "undetermined")
        self.assertIn("无法确定", text)
        self.assertIn("Phone field label", text)


class Example5bAFieldThatIsGenuinelyEmpty(unittest.TestCase):
    """Acme 的手机号是多少？ -- `mobile` really is empty, and saying so is correct.

    Paired with 5a on purpose. If "read and empty" and "never read" could be answered with
    the same sentence, a system could pass every one of these by always saying it did not
    know.
    """

    def test_read_and_empty_is_a_value_conclusion(self):
        turn = TurnEvidence()
        turn.record(RecordEvidence(
            evidence_id="record#1", origin=_typed("query_customers#1", CUSTOMERS),
            record_id="res.partner/10", field_name="mobile", field_meaning="手机号",
            field_was_read=True, value=None, value_type="text"))
        answer = verify(AnswerDraft(
            (FieldValue("res.partner/10", "mobile", "record#1"),), "complete"), turn)
        text = render(answer, user_text="Acme 的手机号是多少？", max_chars=4000)

        self.assertEqual(answer.coverage, "complete",
                         "an empty field is an answer, not a gap")
        self.assertIn("内容为空", text)

    def test_a_read_field_cannot_be_called_undetermined(self):
        turn = TurnEvidence()
        turn.record(RecordEvidence(
            evidence_id="record#1", origin=_typed("query_customers#1", CUSTOMERS),
            record_id="res.partner/10", field_name="mobile", field_meaning="手机号",
            field_was_read=True, value=None, value_type="text"))
        with self.assertRaisesRegex(ConclusionRefused, "say what it holds"):
            verify(AnswerDraft((Undetermined("手机号", "record#1"),), "complete"), turn)


class Example6PageEvidenceOnly(unittest.TestCase):
    """列出所有供应商及其欠款 -- answered from the DOM, wrongly, and called an API read.

    Answering from the current page is legitimate if its scope is stated. It is still a
    partial answer: the question asked for all suppliers.
    """

    def test_a_current_page_answer_is_partial_and_says_it_came_from_the_page(self):
        turn = TurnEvidence()
        turn.record(PageEvidence(
            evidence_id="page#1",
            origin=Origin(kind="page", reference="observation#2", observed_at=AT,
                          scope="the vendor bills list, current page"),
            page_route="/odoo/vendor-bills", saw="text",
            detail="three bill rows on the current page"))
        turn.record(QueryEvidence(
            evidence_id="query#1",
            origin=Origin(kind="page", reference="observation#2", observed_at=AT,
                          scope="the vendor bills list, current page"),
            collection="vendor bills", conditions=("whatever the current page showed",),
            matched_count=3, covers_whole_scope=False))
        answer = verify(AnswerDraft(
            (Undetermined("全部供应商的欠款", "query#1"),), "complete"), turn)
        text = render(answer, user_text="列出所有供应商及其欠款", max_chars=4000)

        self.assertNotEqual(answer.coverage, "complete",
                            "the current page does not answer a question about all of them")
        self.assertIn("无法确定", text)


class ARenderedAnswerNeverFallsBackToTheLastQuery(unittest.TestCase):
    """The original defect wearing a different hat.

    Every failure above raises rather than quietly rendering the rows the last call
    returned, because that is precisely what produced a table of five unrelated quotations
    for a question about a sixth.
    """

    def test_an_answer_asserting_nothing_is_refused(self):
        with self.assertRaisesRegex(ConclusionRefused, "asserts nothing"):
            verify(AnswerDraft((), "complete"), _quotation_turn())

    def test_a_conclusion_naming_evidence_from_nowhere_is_refused(self):
        with self.assertRaisesRegex(ConclusionRefused, "recorded no evidence"):
            verify(AnswerDraft((Aggregate("computed#9"),), "complete"), _quotation_turn())


if __name__ == "__main__":
    unittest.main()
