"""A list headlines how many records match, not how many rows came back (P7.5 CR-10).

"当前有哪些客户？列出名称和电话。" read 20 rows of 72 matching customers. The answer's headline
was "customer_rank > 0：20 条符合" -- adapter query syntax, and a count that was false: 72
matched, 20 were listed. Its only other word on the matter was "还无法确认: customer_rank > 0
covered only part of customers visible to the delegated user", the verifier's own English
note, so the reader learned neither the total nor how to see the rest.
"""
from __future__ import annotations

import unittest

from web_operation_agent.answer.answer_protocol import compute, submit
from web_operation_agent.answer.answer_view import render_payload
from web_operation_agent.answer.evidence import Origin, QueryEvidence, RecordEvidence, TurnEvidence

AT = "2026-09-18T00:00:00+00:00"
SCOPE = "customers visible to the delegated user"
NAMES = ("Azure Interior", "Acme Corporation", "P75 Harbor Foods")


def _turn(*, matched: int, returned=NAMES, whole: bool, count_exact: bool) -> TurnEvidence:
    origin = Origin(kind="typed", reference="query_customers#3", observed_at=AT,
                    scope=SCOPE + "; customer_rank > 0")
    turn = TurnEvidence()
    turn.record(QueryEvidence(
        evidence_id="query#1", origin=origin, collection="customers",
        conditions=("customer_rank > 0",), matched_count=matched,
        records_scanned=matched, covers_whole_scope=whole, count_exact=count_exact))
    for index, name in enumerate(returned, start=1):
        turn.record(RecordEvidence(
            evidence_id=f"record#{index}", origin=origin, record_id=f"res.partner/{index}",
            field_name="name", field_meaning="customer name", field_was_read=True,
            value=name, value_type="text"))
    return turn


def _answer(turn: TurnEvidence, user_text: str, coverage: str = "complete") -> tuple[str, dict]:
    answer = submit(turn, {"outcome": "answered", "answer": {
        "version": 1, "coverage": coverage, "presentation": "table",
        "conclusions": [{"kind": "selection", "evidence_ref": "query#1", "fields": ["name"]}]}})
    return render_payload(answer, user_text=user_text, max_chars=4000).replace("**", ""), answer


class TheHeadlineCountsMatchesNotRows(unittest.TestCase):
    def test_a_limited_read_says_how_many_match_how_many_are_listed_and_how_to_see_the_rest(self):
        text, answer = _answer(_turn(matched=72, whole=False, count_exact=True),
                               "当前有哪些客户？列出名称和电话。")
        self.assertIn("共 72 条，下面列出其中 3 条", text)
        self.assertIn("其余 69 条没有列出", text)
        self.assertIn("到应用的列表页查看全部", text)
        for internal in ("customer_rank", "covered only part", "条符合", "delegated user; "):
            self.assertNotIn(internal, text)
        self.assertEqual(answer["coverage"], "partial")

    def test_the_same_in_english(self):
        text, _ = _answer(_turn(matched=72, whole=False, count_exact=True),
                          "Which customers are there?")
        self.assertIn("72 in total, 3 listed below", text)
        self.assertIn("The other 69 are not listed", text)
        self.assertNotIn("customer_rank", text)

    def test_a_total_the_application_did_not_count_is_not_stated(self):
        text, _ = _answer(_turn(matched=3, whole=False, count_exact=False), "有哪些客户？")
        self.assertIn("下面列出 3 条，符合条件的总数未能确认", text)
        self.assertIn("可能还有其他记录符合条件", text)
        self.assertNotIn("共 3 条", text)

    def test_a_complete_list_is_just_its_count(self):
        text, answer = _answer(_turn(matched=3, whole=True, count_exact=True), "有哪些客户？")
        self.assertIn("共 3 条", text)
        self.assertNotIn("下面列出", text)
        self.assertNotIn("已回答目前可以确认的部分", text)
        self.assertEqual(answer["coverage"], "complete")


ORDERS = "confirmed sales orders visible to the delegated user"


def _orders(names: tuple[str, ...], *, looked_for: str = "S00011") -> TurnEvidence:
    """A search for an order by name, with each match's name and order date read."""
    origin = Origin(kind="typed", reference="query_sale_orders#1", observed_at=AT,
                    scope=ORDERS + "; name contains S00011")
    turn = TurnEvidence()
    turn.record(QueryEvidence(
        evidence_id="query#1", origin=origin, collection="sale_orders",
        conditions=("name contains S00011",), matched_count=len(names),
        records_scanned=len(names), covers_whole_scope=True, count_exact=True,
        looked_for=looked_for, exact_matches=1 if looked_for else None))
    for index, name in enumerate(names, start=1):
        for field, value in (("name", name), ("date_order", f"2026-08-2{index} 15:50:57")):
            turn.record(RecordEvidence(
                evidence_id=f"{field.partition('_')[0]}#{index}", origin=origin,
                record_id=f"sale.order/{index}", field_name=field, field_meaning=field,
                field_was_read=True, value=value, value_type="text"))
    compute(turn, {"operation": "count", "dataset_ref": "query#1"},
            result_ref="count#1", observed_at=AT)
    return turn


def _said(turn: TurnEvidence, *refs: tuple[str, str]) -> str:
    answer = submit(turn, {"outcome": "answered", "answer": {
        "version": 1, "coverage": "complete",
        "conclusions": [{"kind": kind, "evidence_ref": ref} for kind, ref in refs]}})
    return render_payload(answer, user_text="S00011 下单时间", max_chars=4000).replace("**", "")


class ACountTheListAlreadyShowsIsNotSaid(unittest.TestCase):
    """Asked when S00011 was ordered, a turn bound to count its read put "数量：1" above the
    date. The count stays required; it is left unsaid only when it adds nothing."""

    def test_one_match_shown_by_its_field_needs_no_count(self):
        text = _said(_orders(("S00011",)), ("aggregate", "count#1"),
                     ("field_value", "date#1"))
        self.assertIn("2026-08-21 15:50:57", text)
        self.assertNotIn("数量", text)
        self.assertNotIn("匹配的记录", text)

    def test_a_count_larger_than_the_listed_records_says_what_it_counted(self):
        text = _said(_orders(("S00011", "S000110")), ("aggregate", "count#1"),
                     ("field_value", "date#1"))
        self.assertIn("与“S00011”匹配的记录：2 条", text)
        self.assertNotIn("数量", text)

    def test_every_match_listed_leaves_the_count_to_the_list(self):
        text = _said(_orders(("S00011", "S000110")), ("aggregate", "count#1"),
                     ("field_value", "date#1"), ("field_value", "date#2"))
        self.assertNotIn("匹配的记录", text)
        self.assertIn("2026-08-22 15:50:57", text)

    def test_a_count_of_a_read_that_searched_no_name_keeps_its_plain_label(self):
        text = _said(_orders(("S00011", "S000110"), looked_for=""),
                     ("aggregate", "count#1"), ("field_value", "date#1"))
        self.assertIn("数量：2", text)

    def test_a_count_alone_is_still_said(self):
        text = _said(_orders(("S00011",)), ("aggregate", "count#1"))
        self.assertIn("与“S00011”匹配的记录：1 条", text)


if __name__ == "__main__":
    unittest.main()
