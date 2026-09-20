"""A question about a period is answered by comparing dates, not by eye (P7.5 CR-22).

"列出最近一个月的订单和日期。" The model asked the server to filter the orders by
`date_order >= 2026-08-18`; the field carried the type `text`, so the filter was refused as
"text and enum fields support exact eq, ne or in only". Left to pick the records itself, the
model listed every confirmed order, including one from six weeks earlier.
"""
from __future__ import annotations

import unittest

from web_operation_agent.answer.answer_protocol import compute, submit
from web_operation_agent.answer.answer_view import render_payload
from web_operation_agent.answer.computation import ComputationRefused
from web_operation_agent.answer.evidence import TurnEvidence
from web_operation_agent_odoo.tests.test_adapter import _FakeOdoo


def _turn(rows):
    executor = _FakeOdoo({"sale.order": rows})
    result = executor.execute("query_sale_orders", {"limit": 50})
    turn = TurnEvidence()
    for item in result.evidence:
        turn.record(item)
    return turn


def _orders():
    return [{"id": index, "name": name, "partner_id": [3, "Acme"], "state": "sale",
             "amount_total": 100.0 + index, "currency_id": [1, "USD"],
             "invoice_status": "to invoice", "date_order": date}
            for index, (name, date) in enumerate(
                [("S00010", "2026-09-13 12:27:38"), ("S00012", "2026-08-04 12:27:38"),
                 ("S00013", "2026-09-15 12:27:38")], start=10)]


class ADateFieldOrders(unittest.TestCase):
    def test_the_orders_of_the_last_month_are_the_ones_the_filter_kept(self):
        turn = _turn(_orders())
        selection = compute(turn, {"operation": "filter", "dataset_ref": "query#1",
                                   "field": "date_order", "comparison": "ge",
                                   "threshold": "2026-08-18"},
                            result_ref="calc#1", observed_at="2026-09-18T00:00:00+00:00")
        self.assertEqual(sorted(selection.members), ["sale.order/10", "sale.order/12"])
        answer = submit(turn, {"outcome": "answered", "answer": {
            "version": 1, "coverage": "complete", "presentation": "table",
            "conclusions": [{"kind": "selection", "evidence_ref": "calc#1",
                             "fields": ["name", "date_order"]}]}})
        text = render_payload(answer, user_text="列出最近一个月的订单和日期。", max_chars=4000)
        self.assertIn("S00010", text)
        self.assertIn("S00013", text)
        self.assertNotIn("S00012", text)
        self.assertIn("2026-08-18", text, "the answer says which dates it stands on")

    def test_a_date_operand_covers_its_whole_day(self):
        turn = _turn(_orders())
        kept = compute(turn, {"operation": "filter", "dataset_ref": "query#1",
                              "field": "date_order", "comparison": "le",
                              "threshold": "2026-08-04"},
                       result_ref="calc#1", observed_at="2026-09-18T00:00:00+00:00")
        self.assertEqual(list(kept.members), ["sale.order/11"])

    def test_the_latest_order_is_a_question_the_server_can_answer(self):
        turn = _turn(_orders())
        latest = compute(turn, {"operation": "argmax", "dataset_ref": "query#1",
                                "field": "date_order"},
                         result_ref="calc#1", observed_at="2026-09-18T00:00:00+00:00")
        self.assertEqual(list(latest.members), ["sale.order/12"])

    def test_dates_do_not_add_and_a_bad_operand_is_named(self):
        turn = _turn(_orders())
        with self.assertRaisesRegex(ComputationRefused, "do not add"):
            compute(turn, {"operation": "sum", "dataset_ref": "query#1",
                           "field": "date_order"},
                    result_ref="calc#1", observed_at="2026-09-18T00:00:00+00:00")
        with self.assertRaisesRegex(ComputationRefused, "ISO-8601"):
            compute(turn, {"operation": "filter", "dataset_ref": "query#1",
                           "field": "date_order", "comparison": "ge",
                           "threshold": "last month"},
                    result_ref="calc#1", observed_at="2026-09-18T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
