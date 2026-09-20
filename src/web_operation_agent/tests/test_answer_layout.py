"""Several records read field by field are one table, and a filter the answer stands on is
shown (P7.5 CR-07).

"列出最近一个月的订单和日期。" The model filtered the orders by date with compute and then
cited each order's reference and date as separate field values. The answer rendered eight
headed lines, each with an internal id ("sale.order/13") and its own source line, and never
said which dates "the last month" meant.
"""
from __future__ import annotations

import unittest

from web_operation_agent.answer.answer_protocol import compute, submit
from web_operation_agent.answer.answer_view import render_payload
from web_operation_agent.answer.evidence import Origin, QueryEvidence, RecordEvidence, TurnEvidence

AT = "2026-09-18T00:00:00+00:00"
ORDERS = (("sale.order/13", "S00013", "2026-09-15 12:27:38", "900.00"),
          ("sale.order/1", "S00001", "2026-09-14 09:23:42", "287.50"),
          ("sale.order/12", "S00012", "2026-08-04 12:27:38", "1000.00"))


def _turn() -> TurnEvidence:
    origin = Origin(kind="typed", reference="query_sale_orders#1", observed_at=AT,
                    scope="confirmed sales orders visible to the delegated user")
    turn = TurnEvidence()
    turn.record(QueryEvidence(evidence_id="query#1", origin=origin, collection="sale_orders",
                              conditions=("state not in draft,sent,cancel",),
                              matched_count=len(ORDERS), covers_whole_scope=True))
    index = 0
    for record_id, name, date, amount in ORDERS:
        for field, meaning, value, value_type, unit in (
                ("name", "order reference", name, "text", ""),
                ("date_order", "order date", date, "text", ""),
                ("amount_total", "total including taxes", amount, "money", "USD")):
            index += 1
            turn.record(RecordEvidence(
                evidence_id=f"record#{index}", origin=origin, record_id=record_id,
                field_name=field, field_meaning=meaning, field_was_read=True, value=value,
                value_type=value_type, unit=unit))
    return turn


def _render(turn: TurnEvidence, conclusions: list[dict], user_text: str) -> str:
    answer = submit(turn, {"outcome": "answered", "answer": {
        "version": 1, "coverage": "complete", "conclusions": conclusions}})
    return render_payload(answer, user_text=user_text, max_chars=4000)


class SeveralRecordsAreOneTable(unittest.TestCase):
    def test_field_values_of_several_records_render_as_rows(self):
        turn = _turn()
        refs = [ref for ref in turn.ids() if ref.startswith("record#")
                and turn.get(ref).field_name in ("name", "date_order")
                and turn.get(ref).record_id != "sale.order/12"]
        text = _render(turn, [{"kind": "field_value", "evidence_ref": ref} for ref in refs],
                       "列出最近一个月的订单和日期。")
        self.assertIn("| order reference | order date |", text)
        self.assertIn("| S00013 | 2026-09-15 12:27:38 |", text)
        self.assertIn("| S00001 | 2026-09-14 09:23:42 |", text)
        self.assertNotIn("sale.order/", text)
        self.assertEqual(text.count("来源："), 1)

    def test_rows_without_a_name_keep_the_record_id(self):
        turn = _turn()
        refs = [ref for ref in turn.ids() if ref.startswith("record#")
                and turn.get(ref).field_name == "amount_total"][:2]
        text = _render(turn, [{"kind": "field_value", "evidence_ref": ref} for ref in refs],
                       "这两张订单多少钱？")
        self.assertIn("| 记录 | total including taxes |", text)
        self.assertIn("| sale.order/13 | 900.00 USD |", text)

    def test_one_record_keeps_its_own_lines(self):
        turn = _turn()
        text = _render(turn, [{"kind": "field_value", "evidence_ref": "record#1"}],
                       "S00013 的单号是什么？")
        self.assertNotIn("| ---", text)
        self.assertIn("S00013", text)

    def test_a_field_value_is_headed_by_its_records_name_when_the_read_had_it(self):
        turn = _turn()
        text = _render(turn, [{"kind": "field_value", "evidence_ref": "record#3"}],
                       "S00013 多少钱？")
        self.assertIn("total including taxes**（S00013）：900.00 USD", text)
        self.assertNotIn("sale.order/13", text)


class AFilterTheListStandsOnIsShown(unittest.TestCase):
    def test_a_computed_selection_names_its_filter_in_the_readers_notation(self):
        turn = _turn()
        compute(turn, {"operation": "filter", "dataset_ref": "query#1",
                       "field": "amount_total", "comparison": "gt", "threshold": "500"},
                result_ref="calc#1", observed_at=AT)
        text = _render(turn, [{"kind": "selection", "evidence_ref": "calc#1",
                               "fields": ["name", "amount_total"]}],
                       "金额大于 500 的订单有哪些？").replace("**", "")
        self.assertIn("共 2 条（筛选：amount\\_total \\> 500）", text)
        self.assertNotIn("state not in", text,
                         "the adapter's own query conditions stay in the answer basis")


class APageFactSaysWhatItCovers(unittest.TestCase):
    def test_the_scope_of_a_page_fact_is_a_sentence_not_an_epoch(self):
        from web_operation_agent.answer.evidence import PageEvidence
        turn = TurnEvidence()
        turn.record(PageEvidence(
            evidence_id="page#1",
            origin=Origin(kind="page", reference="observation#3", observed_at=AT,
                          scope="observed page only, epoch 3; not an API or complete collection"),
            page_route="/odoo/customers/13", saw="value", detail="电话", value="+1 510 555 0102"))
        text = _render(turn, [{"kind": "page_observation", "evidence_ref": "page#1"}],
                       "页面上 Azure Interior 的电话是多少？")
        self.assertIn("只代表当时页面上显示的内容，不代表完整数据", text)
        self.assertNotIn("epoch", text)


if __name__ == "__main__":
    unittest.main()
