"""A list headlines how many records match, not how many rows came back (P7.5 CR-10).

"当前有哪些客户？列出名称和电话。" read 20 rows of 72 matching customers. The answer's headline
was "customer_rank > 0：20 条符合" -- adapter query syntax, and a count that was false: 72
matched, 20 were listed. Its only other word on the matter was "还无法确认: customer_rank > 0
covered only part of customers visible to the delegated user", the verifier's own English
note, so the reader learned neither the total nor how to see the rest.
"""
from __future__ import annotations

import unittest

from web_operation_agent.answer.answer_protocol import submit
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
        self.assertIn("customer_rank > 0", answer["sources"][0]["scope"],
                      "the executed condition stays in the answer basis")
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


if __name__ == "__main__":
    unittest.main()
