"""Grounding: candidate values numbered, ids bound to exact names, days read by shape."""
from __future__ import annotations

import unittest

from web_operation_agent.goal.ground import bind, days, ground, numbers_in, pick, render
from web_operation_agent.goal.narrow import KnownReference
from web_operation_agent.goal.ports import Candidate
from web_operation_agent.goal.spec import GoalSpec


class _Port:
    def __init__(self):
        self.asked = None

    def candidates(self, *, entities, known, dates, context=None):
        self.asked = (entities, known, dates)
        return (Candidate(kind="record", entity="order", display="S00016", detail="Total 1582.00"),
                Candidate(kind="line", entity="line", display="[FURN_6667] Acoustic Bloc Screens (White)",
                          detail="S00016: Quantity 4.0"))


class GroundingTests(unittest.TestCase):
    def test_candidates_are_numbered_and_rendered_one_per_line(self):
        port = _Port()
        found = ground(port, entities=("line",), known=(KnownReference("Gemini", "partner", "Gemini Furniture"),),
                       dates=("2026-08-22",))
        self.assertEqual([c.id for c in found], ["v1", "v2"])
        self.assertEqual(port.asked, (("line",), (("partner", "Gemini Furniture"),), ("2026-08-22",)))
        self.assertEqual(render(found).splitlines()[1],
                         "v2 line line: [FURN_6667] Acoustic Bloc Screens (White) — S00016: Quantity 4.0")
        self.assertEqual(ground(object(), entities=(), known=()), ())

    def test_ids_are_bound_to_exact_names_and_unknown_ids_reported(self):
        found = ground(_Port(), entities=(), known=())
        spec = GoalSpec.from_json({"deliverables": [
            {"kind": "fact", "entity": "line", "fields": ["Quantity"], "metric": {"op": "count", "filters": [
                {"field": "Order Reference", "values": ["v1"]}, {"field": "Product", "values": ["[v2]"]}]}},
            {"kind": "fact", "entity": "order", "record": "v9"},
            {"kind": "fact", "entity": "order", "record": "S00001"}]})
        bound, count, unknown = bind(spec, found)
        filters = bound.deliverables[0].metric.filters
        self.assertEqual(filters[0].values, ("S00016",))
        self.assertEqual(filters[1].values, ("[FURN_6667] Acoustic Bloc Screens (White)",))
        self.assertEqual((count, unknown), (2, ("v9",)))
        # The person's own words are never treated as ids.
        self.assertEqual(bound.deliverables[2].record, "S00001")
        self.assertEqual(bind(GoalSpec.from_json({"deliverables": [{"kind": "fact", "entity": "order",
                                                                     "record": "S00001"}]}), found)[1:], (0, ()))

    def test_a_line_binds_to_its_record_and_item_wherever_its_id_is_written(self):
        line = (Candidate(kind="line", entity="line", display="Acoustic Bloc Screens (White)", id="v1",
                          binds=(("Order Reference", "S00016"), ("Product", "Acoustic Bloc Screens (White)"))),)
        as_record = GoalSpec.from_json({"deliverables": [
            {"kind": "fact", "entity": "line", "record": "v1", "fields": ["Unit Price"]}]})
        bound, count, _ = bind(as_record, line)
        d = bound.deliverables[0]
        self.assertEqual((d.record, count), ("", 1))
        self.assertEqual([(f.field, f.values) for f in d.metric.filters],
                         [("Order Reference", ("S00016",)), ("Product", ("Acoustic Bloc Screens (White)",))])
        # As a condition's value: the item, with the record it is on added.
        as_value = GoalSpec.from_json({"deliverables": [
            {"kind": "fact", "entity": "line", "fields": ["Unit Price"],
             "metric": {"op": "count", "filters": [{"field": "Product", "values": ["v1"]}]}}]})
        d = bind(as_value, line)[0].deliverables[0]
        self.assertEqual([(f.field, f.values) for f in d.metric.filters],
                         [("Product", ("Acoustic Bloc Screens (White)",)), ("Order Reference", ("S00016",))])
        # A record condition already declared is replaced by the line's own record.
        with_owner = GoalSpec.from_json({"deliverables": [
            {"kind": "fact", "entity": "line", "record": "v1",
             "metric": {"op": "count", "filters": [{"field": "Order Reference", "values": ["Gemini"]}]}}]})
        d = bind(with_owner, line)[0].deliverables[0]
        self.assertEqual([(f.field, f.values) for f in d.metric.filters],
                         [("Order Reference", ("S00016",)), ("Product", ("Acoustic Bloc Screens (White)",))])

    def test_amounts_are_read_in_digits_and_cjk_numerals(self):
        self.assertEqual(numbers_in("Azure 那张两万美元的客户退款"), {"20000"})
        self.assertEqual(numbers_in("Gemini 那笔 1,582.00 美元订单，8 月 29 日"), {"1582"})
        self.assertEqual(numbers_in("一万一千七百五十 的那张"), {"11750"})
        self.assertEqual(numbers_in("第 3 行"), set())

    def test_one_described_record_is_picked_by_its_amount(self):
        found = (Candidate(kind="record", entity="move", display="RINV/2026/00002",
                           detail="Date 2026-08-03; Total -20000.0", id="v1",
                           binds=(("Journal Entry", "RINV/2026/00002"),)),
                 Candidate(kind="record", entity="move", display="RINV/2026/00001",
                           detail="Date 2026-08-02; Total -11750.0", id="v2",
                           binds=(("Journal Entry", "RINV/2026/00001"),)),
                 Candidate(kind="line", entity="line", display="Large Meeting Table", id="v3"))
        lines = GoalSpec.from_json({"deliverables": [
            {"kind": "fact", "entity": "line", "fields": ["Quantity"],
             "metric": {"op": "count", "filters": [{"field": "Journal Entry", "values": ["RINV"], "op": "contains"},
                                                    {"field": "Total", "values": ["20000"]},
                                                    {"field": "Product", "values": ["Large Meeting Table"]}]}}]})
        picked, names = pick(lines, "Azure 那张两万美元的退款，会议桌退了几件？", "2026-09-26", found)
        self.assertEqual(names, ("RINV/2026/00002",))
        self.assertEqual([(f.field, f.values) for f in picked.deliverables[0].metric.filters],
                         [("Product", ("Large Meeting Table",)), ("Journal Entry", ("RINV/2026/00002",))])
        same_entity = GoalSpec.from_json({"deliverables": [{"kind": "page", "entity": "move"}]})
        self.assertEqual(pick(same_entity, "11750 的那张退款", "2026-09-26", found)[0].deliverables[0].record,
                         "RINV/2026/00001")
        # A list is a condition, not one record; an amount no candidate has picks nothing.
        a_list = GoalSpec.from_json({"deliverables": [{"kind": "list", "entity": "move"}]})
        self.assertEqual(pick(a_list, "不低于 20000 的退款", "2026-09-26", found)[1], ())
        self.assertEqual(pick(same_entity, "30000 的那张", "2026-09-26", found)[1], ())
        named = GoalSpec.from_json({"deliverables": [{"kind": "page", "entity": "move", "record": "RINV/2026/00001"}]})
        self.assertEqual(pick(named, "两万的那张", "2026-09-26", found)[1], ())

    def test_one_line_written_as_its_item_is_the_picked_records_line(self):
        found = (Candidate(kind="record", entity="order", display="S00016",
                           detail="Order Date 2026-08-22; Total 1582.0", id="v1",
                           binds=(("Order Reference", "S00016"),)),
                 Candidate(kind="record", entity="order", display="S00009",
                           detail="Order Date 2026-08-10; Total 900.0", id="v2",
                           binds=(("Order Reference", "S00009"),)),
                 Candidate(kind="line", entity="line", display="[FURN_6667] Acoustic Bloc Screens (White)",
                           id="v3", binds=(("Order Reference", "S00016"),
                                           ("Product", "[FURN_6667] Acoustic Bloc Screens (White)"))),
                 Candidate(kind="line", entity="line", display="[FURN_6667] Acoustic Bloc Screens (White)",
                           id="v4", binds=(("Order Reference", "S00009"),
                                           ("Product", "[FURN_6667] Acoustic Bloc Screens (White)"))))
        spec = GoalSpec.from_json({"deliverables": [
            {"kind": "fact", "entity": "line", "record": "Acoustic Bloc Screens", "fields": ["Subtotal"],
             "metric": {"op": "count", "filters": [{"field": "Customer", "values": ["Gemini Furniture"]},
                                                    {"field": "Order Date", "values": ["2026-08-22"]}]}}]})
        picked, names = pick(spec, "八月二十二日那张 Gemini 订单，隔音屏这一行的小计是多少？", "2026-09-26", found)
        self.assertEqual(names, ("S00016",))
        d = picked.deliverables[0]
        self.assertEqual(d.record, "")
        self.assertEqual([(f.field, f.values) for f in d.metric.filters],
                         [("Order Reference", ("S00016",)),
                          ("Product", ("[FURN_6667] Acoustic Bloc Screens (White)",))])
        # An item none of the offered lines carries: nothing is picked.
        other = GoalSpec.from_json({"deliverables": [
            {"kind": "fact", "entity": "line", "record": "Desk", "fields": ["Subtotal"]}]})
        self.assertEqual(pick(other, "八月二十二日那张订单", "2026-09-26", found)[1], ())

    def test_days_are_read_by_shape_in_the_current_year(self):
        self.assertEqual(days("8 月 29 日的订单", "2026-09-26"), ("2026-08-29",))
        self.assertEqual(days("八月二十二日那张", "2026-09-26"), ("2026-08-22",))
        self.assertEqual(days("Azure 八月二日那张退款", "2026-09-26"), ("2026-08-02",))
        self.assertEqual(days("十二月十日", "2026-09-26"), ("2026-12-10",))
        self.assertEqual(days("八月初那张", "2026-09-26"), ())
        self.assertEqual(numbers_in("八月二十二日那张订单"), set())
        self.assertEqual(set(days("orders of Aug 22 and 2025-06-30, also 9/1", "2026-09-26")),
                         {"2025-06-30", "2026-08-22", "2026-09-01"})
        self.assertEqual(days("2026 年 8 月 3 日", "2026-09-26"), ("2026-08-03",))
        self.assertEqual(days("13 月 40 日", "2026-09-26"), ())


if __name__ == "__main__":
    unittest.main()
