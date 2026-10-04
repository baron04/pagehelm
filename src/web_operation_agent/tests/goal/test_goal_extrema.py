"""Complete comparisons must not mistake a truncated sample or mode for an extreme."""
import unittest
from datetime import date

from web_operation_agent.goal.data import (DataRefused, EntitySchema, FieldSchema, LinkValue,
                                           Row, Schema, matches)
from web_operation_agent.goal.executor import _render
from web_operation_agent.goal.ports import Resolved
from web_operation_agent.goal.resolve.extrema import resolve_extreme
from web_operation_agent.goal.spec import Deliverable, Filter, GoalSpec, Metric
from web_operation_agent.goal.resolve.indexing import index_from_schema
from web_operation_agent.goal.resolve.resolver import Resolver


class ExtremeTests(unittest.TestCase):
    def resolve(self, amounts, *, operation="max", units=None, after=None):
        rows = tuple(Row(str(i), f"Record {i}", {"amount": v},
                         {"amount": str(v)}, {"amount": (units or [""] * len(amounts))[i]})
                     for i, v in enumerate(amounts))
        return resolve_extreme(operation=operation, label="Amount", key="amount", subject="Records",
                               count=len(rows), after=len(rows) if after is None else after, rows=rows)

    def test_extreme_is_not_frequency_and_keeps_every_tie(self):
        result = self.resolve([0, 0, 0, 8000, 3500, 8000, None, -2])
        self.assertEqual(result.aggregate.extreme, ("max", "Amount", "8000"))
        self.assertEqual(result.aggregate.records, ("Record 3", "Record 5"))
        for zh in (False, True):
            answer = "\n".join(_render(result, None, zh))
            self.assertIn("8000", answer)
            self.assertNotIn("Record 4", answer)
            self.assertNotIn("最多", answer)

    def test_minimum_includes_negative_and_zero_and_excludes_missing(self):
        self.assertEqual(self.resolve([None, "", 0, -2, -2], operation="min").aggregate.records,
                         ("Record 3", "Record 4"))
        self.assertEqual(self.resolve([None, 0], operation="min").aggregate.extreme[2], "0")

    def test_no_known_values_is_not_zero(self):
        result = self.resolve([None, ""])
        self.assertIsNone(result.aggregate.extreme[2])
        self.assertIn("No known numeric values", _render(result, None, False)[0])

    def test_invalid_values_units_and_incomplete_reads_refuse(self):
        for value in (True, float("inf"), float("nan"), "123"):
            with self.subTest(value=value):
                self.assertTrue(self.resolve([value]).problem)
        self.assertTrue(self.resolve([1, 2], units=["USD", "EUR"]).problem)
        self.assertTrue(self.resolve([1, 2], units=["USD", ""]).problem)
        self.assertTrue(self.resolve([1], units=["unknown-currency"]).problem)
        self.assertTrue(self.resolve([1], after=2).problem)
        for rows in ((Row("1", "A", {"amount": 2}),),
                     (Row("1", "A", {"amount": 2}), Row("1", "B", {"amount": 2})),
                     (Row("", "A", {"amount": 2}), Row("2", "B", {"amount": 2})),
                     (Row("1", "A"), Row("2", "B", {"amount": 2}))):
            self.assertTrue(resolve_extreme(operation="max", label="Amount", key="amount",
                                           subject="Records", count=2, after=2, rows=rows).problem)

    def test_model_contract_accepts_min_max(self):
        for op in ("min", "max"):
            self.assertEqual(Metric.from_json({"op": op, "field": "Amount"}).op, op)

    def test_empty_scope_is_reported_without_claiming_a_missing_capability(self):
        result = Resolved(kind="fact", problem="no matches", no_matches="Contracts · name = Synthetic")
        for zh, words in ((True, "没有找到符合"), (False, "No record matches")):
            answer = "\n".join(_render(result, None, zh))
            self.assertIn(words, answer)
            self.assertNotIn("Could not do", answer)


_FIELDS_SCHEMA = Schema(entities=(
    EntitySchema("record", "Records", fields=(
        FieldSchema("name", "Name", "text", listed=True),
        FieldSchema("amount", "Amount", "money", listed=True),
        FieldSchema("customer", "Customer", "link", relation="customer", listed=True),
        FieldSchema("note", "Note", "text", listed=True),
        FieldSchema("status", "Status", "text", listed=True),
        FieldSchema("unrequested", "Unrequested", "text", listed=True))),
    EntitySchema("customer", "Customers", fields=(FieldSchema("name", "Name", "text"),))))


class _FieldsPort:
    """A bound actor's projected rows; another actor's larger record stays hidden."""
    def __init__(self, *, operation="max"):
        value = 20 if operation == "max" else -20
        self.visible = tuple(Row(rid, "Shared name", {
            "name": name, "amount": amount, "customer": LinkValue(customer, "Raw name"),
            "note": note, "status": "Active" if rid in ("a", "b") else "Other",
            "unrequested": "private unrequested value"}, {
            "name": name, "amount": f"{amount:.2f} EUR", "customer": shown,
            **({"note": "Presented note"} if note is not None else {})}, {"amount": "EUR"})
            for rid, name, amount, customer, shown, note in (
                ("b", "Exact B", value, "party-b", "Displayed B", "raw note"),
                ("a", "Exact A", value, "party-a", "Displayed A", None),
                ("c", "Nonwinner", 0, "party-c", "Displayed C", "raw note")))
        self.hidden = Row("hidden", "Other actor", {"amount": value * 100})
        self.row_calls, self.count_calls = [], 0
        self.after = None
        self.refused = False

    def name_of(self, text):
        return text

    def count(self, entity, where):
        self.count_calls += 1
        count = sum(matches(where, row) for row in self.visible)
        return self.after if self.count_calls > 1 and self.after is not None else count

    def rows(self, entity, where, *, fields=(), order=(), limit=80):
        self.row_calls.append((entity, fields, limit))
        if self.refused:
            raise DataRefused("actor was refused", permission_denied=True)
        return tuple(Row(row.id, row.display,
            {key: value for key, value in row.values.items() if key in fields},
            {key: value for key, value in row.shown.items() if key in fields},
            {key: value for key, value in row.units.items() if key in fields})
            for row in self.visible if matches(where, row))[:limit]


class ExtremeFieldResolutionTests(unittest.TestCase):
    def resolve(self, port, *, operation="max", kind="list",
                fields=("Customer", "customer", "Name", "Amount", "Note"), filters=()):
        resolver = Resolver(port, index_from_schema(_FIELDS_SCHEMA), _FIELDS_SCHEMA,
                            today=date(2026, 10, 3))
        return resolver.resolve(GoalSpec(deliverables=(Deliverable(
            kind=kind, entity="record", fields=fields, limit=1,
            metric=Metric(op=operation, field="Amount", filters=filters)),))).deliverables[0]

    def test_declared_fields_survive_resolution_and_rendering_for_all_ties(self):
        for operation in ("max", "min"):
            for kind in ("aggregate", "list"):
                with self.subTest(operation=operation, kind=kind):
                    port = _FieldsPort(operation=operation)
                    result = self.resolve(port, operation=operation, kind=kind)
                    self.assertFalse(result.problem)
                    self.assertEqual(port.row_calls, [("record", ("amount", "customer", "name", "note"), 4)])
                    self.assertEqual(port.count_calls, 2)
                    self.assertEqual(len(result.aggregate.records), 2)  # limit never drops ties
                    self.assertIn("Exact A", result.aggregate.records[0])
                    self.assertIn("Exact B", result.aggregate.records[1])
                    for zh in (True, False):
                        answer = "\n".join(_render(result, None, zh))
                        for shown in ("Customer: Displayed A", "Customer: Displayed B",
                                      "Note: (empty)", "Note: Presented note", "EUR"):
                            self.assertIn(shown, answer)
                        for hidden in ("Other actor", "Nonwinner", "Displayed C", "Raw name",
                                       "party-a", "raw note", "private unrequested value"):
                            self.assertNotIn(hidden, answer)
                        self.assertEqual(answer.count("Customer: Displayed A"), 1)
                        self.assertEqual(answer.count("Customer: Displayed B"), 1)

    def test_requested_fields_keep_the_declared_scope_condition(self):
        from dataclasses import replace
        port = _FieldsPort()
        other = port.visible[2]
        port.visible = (*port.visible[:2], replace(other,
            values={**other.values, "amount": 999}, shown={**other.shown, "amount": "999.00 EUR"}))
        result = self.resolve(port, filters=(Filter("Status", ("Active",)),))
        self.assertEqual(result.aggregate.count, 2)
        self.assertEqual(result.aggregate.extreme, ("max", "Amount", "20.00 EUR"))
        self.assertEqual(port.row_calls, [("record", ("amount", "customer", "name", "note"), 3)])
        answer = "\n".join(_render(result, None, False))
        self.assertNotIn("Nonwinner", answer)
        self.assertNotIn("999", answer)
        self.assertIn("Displayed A", answer)
        self.assertIn("Displayed B", answer)

    def test_no_extra_fields_keeps_name_only_extreme_contract(self):
        port = _FieldsPort()
        result = self.resolve(port, fields=())
        self.assertEqual(port.row_calls[0][1], ("amount",))
        self.assertEqual(result.aggregate.records, ("Shared name", "Shared name"))

    def test_unknown_declared_field_is_explicit_and_does_not_broaden_read(self):
        port = _FieldsPort()
        result = self.resolve(port, fields=("Customer", "Missing exact field"))
        self.assertEqual(result.missing_fields, ("Missing exact field",))
        self.assertIsNone(result.aggregate)
        self.assertEqual(port.row_calls, [])

    def test_extra_columns_do_not_weaken_completeness_values_units_or_authority(self):
        from dataclasses import replace
        for failure in ("count", "value", "invalid", "unit", "unknown-unit", "duplicate", "permission"):
            with self.subTest(failure=failure):
                port = _FieldsPort()
                if failure == "count":
                    port.after = 4
                elif failure == "value":
                    port.visible = (replace(port.visible[0], values={"customer": LinkValue("b", "B")}),
                                    *port.visible[1:])
                elif failure == "invalid":
                    port.visible = (replace(port.visible[0], values={**port.visible[0].values, "amount": True}),
                                    *port.visible[1:])
                elif failure in ("unit", "unknown-unit"):
                    unit = "GBP" if failure == "unit" else "unknown-currency"
                    port.visible = (replace(port.visible[0], units={"amount": unit}), *port.visible[1:])
                elif failure == "duplicate":
                    port.visible = (replace(port.visible[0], id="a"), *port.visible[1:])
                else:
                    port.refused = True
                result = self.resolve(port)
                self.assertTrue(result.problem)
                self.assertIsNone(result.aggregate)
                if failure == "permission":
                    self.assertEqual(result.denied, "read")

    def test_comparison_limit_is_checked_before_reading_any_requested_columns(self):
        port = _FieldsPort()
        port.count = lambda *args: 4001
        result = self.resolve(port)
        self.assertIn("limit", result.problem)
        self.assertIsNone(result.aggregate)
        self.assertEqual(port.row_calls, [])


if __name__ == "__main__":
    unittest.main()
