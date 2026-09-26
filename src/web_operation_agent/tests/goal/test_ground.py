"""Grounding: candidate values numbered, ids bound to exact names, days read by shape."""
from __future__ import annotations

import unittest

from web_operation_agent.goal.ground import bind, days, ground, render
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

    def test_days_are_read_by_shape_in_the_current_year(self):
        self.assertEqual(days("8 月 29 日的订单", "2026-09-26"), ("2026-08-29",))
        self.assertEqual(days("八月二十二日那张", "2026-09-26"), ())
        self.assertEqual(set(days("orders of Aug 22 and 2025-06-30, also 9/1", "2026-09-26")),
                         {"2025-06-30", "2026-08-22", "2026-09-01"})
        self.assertEqual(days("2026 年 8 月 3 日", "2026-09-26"), ("2026-08-03",))
        self.assertEqual(days("13 月 40 日", "2026-09-26"), ())


if __name__ == "__main__":
    unittest.main()
