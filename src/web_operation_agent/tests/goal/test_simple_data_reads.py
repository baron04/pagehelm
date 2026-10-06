"""Flat reads preserve typed evidence, exact vocabulary and the literal actor scope."""
from dataclasses import replace
from datetime import date
import unittest

from web_operation_agent.answer.evidence import ComputationEvidence, QueryEvidence, RecordEvidence, SelectionEvidence
from web_operation_agent.business.errors import BusinessToolIdentityError, BusinessToolPermissionError, BusinessToolRequestError
from web_operation_agent.goal.data import Compare, EntitySchema, FieldSchema, FilterSchema, LinkValue, PageSchema, Ref, Row
from web_operation_agent.goal.read_schemas import DATA_READ_SCHEMAS, SIMPLE_DATA_READ_SCHEMAS
from web_operation_agent.goal.read_simple import compile_read
from web_operation_agent.goal.read_tools import DATA_READ_NAMES, SIMPLE_DATA_READ_NAMES, DataReadTools
from web_operation_agent.tests.goal.test_data_read_tools import Data, context
from web_operation_agent.transport.browser import TurnCancelled


class RelatedData(Data):
    def __init__(self):
        super().__init__()
        records = self._schema.entities[0]
        records = replace(records, fields=(*records.fields,
            FieldSchema("owner", "Owner", "link", relation="owners", listed=True),
            FieldSchema("owner_name", "Owner Name", "text", listed=True),
            FieldSchema("ready", "Ready", "bool", listed=True)))
        self._schema = replace(self._schema, entities=(records,
            EntitySchema("owners", "Owners", fields=(
                FieldSchema("email", "Email", "text", listed=True),), operations=frozenset({"read"}))),
            pages=(PageSchema("active", "records", "Active records",
                scope=Compare("status", "eq", "active")),
                PageSchema("defaults", "records", "Filtered records",
                    defaults=(FilterSchema("empty", "Missing status", Compare("status", "eq", "missing")),)),
                PageSchema("owners", "owners", "Owners")))
        self.owner_rows = (Row("a", "Owner A", {"email": "owner-a@example.test"},
                               {"email": "owner-a@example.test"}),
                           Row("b", "Owner B", {"email": "owner-b@example.test"},
                               {"email": "owner-b@example.test"}))
        self.rows_data = tuple(replace(row, values={**row.values,
            "owner": LinkValue("a" if i < 2 else "b", "Owner A" if i < 2 else "Owner B"),
            "owner_name": "", "ready": i < 2}) for i, row in enumerate(self.rows_data))

    def find(self, entity, text, *, limit=8):
        self.calls.append("find")
        rows = self.owner_rows if entity == "owners" else self.rows_data
        return tuple(Ref(entity, r.id, r.display) for r in rows if text.casefold() in r.display.casefold())[:limit]

    def read(self, entity, id, fields):
        self.calls.append("read")
        rows = self.owner_rows if entity == "owners" else self.rows_data
        return self.project(next(r for r in rows if r.id == id), fields)


class SimpleDataReadTests(unittest.TestCase):
    def setUp(self):
        self.data = RelatedData()
        self.bound = []
        def factory(ctx, check):
            self.bound.append(ctx)
            return self.data
        self.tools = DataReadTools(factory, "sample", "records:read", today=date(2026, 10, 6), surface="simple")

    def call(self, operation, *, request="Read records", ctx=None, checkpoint=lambda: None, **args):
        return self.tools.execute("read_data", {"operation": operation, "entity": "records", **args},
            context=ctx or context(), checkpoint=checkpoint, user_request=request)

    def test_surface_selection_retains_default_and_hides_declaration_from_simple(self):
        legacy = replace(self.tools, surface="declaration")
        self.assertEqual((DATA_READ_NAMES, DATA_READ_SCHEMAS), (legacy.names, legacy.schemas))
        self.assertEqual((SIMPLE_DATA_READ_NAMES, SIMPLE_DATA_READ_SCHEMAS), (self.tools.names, self.tools.schemas))
        self.assertEqual({"inspect_data", "read_data"}, {s["function"]["name"] for s in self.tools.schemas})
        with self.assertRaises(BusinessToolRequestError):
            self.tools.execute("resolve_data", {"deliverables": []}, context=context(),
                               checkpoint=lambda: None, user_request="records")
        with self.assertRaises(BusinessToolRequestError):
            legacy.execute("read_data", {"operation": "count", "entity": "records"}, context=context(),
                           checkpoint=lambda: None, user_request="records")
        self.assertFalse(self.bound)
        with self.assertRaises(ValueError):
            replace(self.tools, surface="unknown")

    def test_sum_compiles_whole_scope_without_a_model_total_flag(self):
        condition = {"field": "Status", "values": ["active"]}
        spec = compile_read({"operation": "sum", "entity": "records", "field": "Amount", "filters": [condition]})
        d = spec.deliverables[0]
        self.assertEqual(("aggregate", "sum", True, "Amount"), (d.kind, d.metric.op, d.metric.total, d.metric.field))
        self.assertEqual(("Status", ("active",)), (d.metric.filters[0].field, d.metric.filters[0].values))
        result = self.call("sum", field="Amount", filters=[condition])
        self.assertEqual([("count()", "2"), ("sum amount", "24")],
            [(e.operation, e.value) for e in result.evidence if isinstance(e, ComputationEvidence)])
        self.assertEqual((), result.allowed_routes)
        self.assertIsNone(result.form_draft)

    def test_list_and_total_keep_the_same_conditions_and_partial_status(self):
        filters = [{"field": "Status", "values": ["active"]}]
        listed = self.call("list", fields=["Email"], filters=filters, limit=1)
        summed = self.call("sum", field="Amount", filters=filters)
        queries = [next(e for e in r.evidence if isinstance(e, QueryEvidence)) for r in (listed, summed)]
        self.assertEqual(queries[0].conditions, queries[1].conditions)
        self.assertEqual([2, 2], [e.matched_count for e in queries])
        self.assertFalse(queries[0].covers_whole_scope)
        self.assertEqual("one@example.test", next(e.value for e in listed.evidence
            if isinstance(e, RecordEvidence) and e.field_name == "email"))

    def test_extrema_read_all_ties_and_requested_columns(self):
        for operation, value, members in (("max", "12", ("records:1", "records:2")),
                                          ("min", "-3", ("records:3",))):
            with self.subTest(operation=operation):
                result = self.call(operation, field="Amount", fields=["Email"])
                selection = next(e for e in result.evidence if isinstance(e, SelectionEvidence))
                self.assertEqual(members, selection.members)
                self.assertTrue(selection.covers_whole_scope)
                calc = next(e for e in result.evidence if isinstance(e, ComputationEvidence))
                self.assertEqual(value, calc.value)
                self.assertEqual(len(members), sum(isinstance(e, RecordEvidence) and e.field_name == "email"
                                                  for e in result.evidence))

    def test_named_record_must_satisfy_additional_conditions(self):
        result = self.call("record", record="Smaller", fields=["Email"],
                           filters=[{"field": "Status", "values": ["active"]}])
        self.assertFalse(result.evidence)
        self.assertTrue(result.content["deliverables"][0]["no_matches"])
        self.assertNotIn("read", self.data.calls)
        result = self.call("record", fields=["Email"], filters=[{"field": "Status", "values": ["other"]}])
        self.assertEqual("three@example.test", next(e.value for e in result.evidence
            if isinstance(e, RecordEvidence) and e.field_name == "email"))
        result = self.call("record", fields=["Email"], filters=[{"field": "Status", "values": ["active"]}])
        self.assertFalse(result.evidence)
        self.assertTrue(result.content["deliverables"][0]["choices"])

    def test_page_and_default_scopes_are_not_lifted_when_empty(self):
        result = self.call("sum", field="Amount", page="active",
                           filters=[{"field": "Status", "values": ["other"]}])
        self.assertEqual("0", next(e.value for e in result.evidence if isinstance(e, ComputationEvidence)
                                   and e.operation == "sum amount"))
        for operation, args in (("count", {}), ("list", {"fields": ["Email"]}),
                                ("record", {"record": "Smaller", "fields": ["Email"]})):
            with self.subTest(operation=operation):
                result = self.call(operation, page="defaults", **args)
                self.assertFalse(any(isinstance(e, RecordEvidence) for e in result.evidence))
                self.assertTrue(all(e.matched_count == 0 for e in result.evidence if isinstance(e, QueryEvidence)))
        self.data.calls.clear()
        with self.assertRaises(BusinessToolRequestError):
            self.call("count", page="owners")
        self.assertNotIn("count", self.data.calls)

    def test_relationship_filter_is_not_a_link_follow_or_a_text_field_repair(self):
        result = self.call("count", filters=[{"field": "Owner", "values": ["Owner A"]}])
        self.assertEqual("2", next(e.value for e in result.evidence if isinstance(e, ComputationEvidence)))
        result = self.call("count", filters=[{"field": "Owner Name", "values": ["Owner A"]}])
        self.assertEqual("0", next(e.value for e in result.evidence if isinstance(e, ComputationEvidence)))
        result = self.call("count", filters=[{"field": "Owner", "values": ["A"]}])
        self.assertFalse(result.evidence)
        self.assertTrue(result.content["deliverables"][0]["problem"])
        result = self.call("record", record="Smaller", via="Owner", fields=["Email"])
        emails = [e for e in result.evidence if isinstance(e, RecordEvidence) and e.field_name == "email"]
        self.assertEqual([("owners:b", "owner-b@example.test")], [(e.record_id, e.value) for e in emails])
        result = self.call("min", field="Amount", filters=[{"field": "Owner", "values": ["Owner A"]}])
        self.assertEqual("12", next(e.value for e in result.evidence if isinstance(e, ComputationEvidence)))
        self.assertNotIn("three@example.test", result.render())

    def test_exact_field_alias_is_bound_but_partial_label_is_refused(self):
        fields = self.data._schema.entities[0].fields
        self.data._schema = replace(self.data._schema, entities=(replace(self.data._schema.entities[0],
            fields=(replace(fields[0], aliases=("总额",)), *fields[1:])), *self.data._schema.entities[1:]))
        result = self.call("sum", field="总额")
        self.assertEqual("21", next(e.value for e in result.evidence if isinstance(e, ComputationEvidence)
                                    and e.operation == "sum amount"))
        for args in ({"fields": ["Ema"]}, {"filters": [{"field": "Sta", "values": ["active"]}]}):
            self.data.calls.clear()
            with self.assertRaises(BusinessToolRequestError):
                self.call("list", **args)
            self.assertNotIn("count", self.data.calls)

    def test_boolean_not_equal_retains_its_operator_and_invalid_word_is_not_false(self):
        result = self.call("count", filters=[{"field": "Ready", "op": "!=", "values": ["true"]}])
        self.assertEqual("1", next(e.value for e in result.evidence if isinstance(e, ComputationEvidence)))
        result = self.call("count", filters=[{"field": "Ready", "values": ["Pending"]}])
        self.assertFalse(result.evidence)

    def test_unsupported_or_ignored_parameters_fail_before_actor_factory(self):
        bad = (
            {"operation": "count_distinct"}, {"operation": "sum"},
            {"operation": "list", "metric": {"op": "count"}},
            {"operation": "count", "record": "Smaller"}, {"operation": "count", "field": "Amount"},
            {"operation": "sum", "field": "Amount", "total": True},
            {"operation": "count", "limit": 1}, {"operation": "min", "field": "Amount", "limit": 1},
            {"operation": "list", "descending": False}, {"operation": "list", "fields": "Email"},
            {"operation": "record", "record": "Smaller"}, {"operation": "record", "fields": ["Email"]},
            {"operation": "list", "via": "Owner"}, {"operation": "list", "page": {"key": "active"}},
            {"operation": "count", "filters": [{"field": "Status", "values": []}]},
            {"operation": "count", "filters": [{"field": "Amount", "values": [12]}]},
            {"operation": "count", "filters": [{"field": "Status", "values": [""]}]},
            {"operation": "max", "field": "Amount", "group_by": "Status"},
        )
        for args in bad:
            with self.subTest(args=args), self.assertRaises(BusinessToolRequestError):
                self.tools.execute("read_data", {"entity": "records", **args}, context=context(),
                                   checkpoint=lambda: None, user_request="records")
        self.assertFalse(self.bound)

    def test_filters_cannot_discard_extra_numeric_values_or_unsupported_operators(self):
        for condition in ({"field": "Amount", "values": ["12", "-3"]},
                          {"field": "Amount", "op": "contains", "values": ["12"]},
                          {"field": "Status", "op": ">", "values": ["active"]}):
            self.data.calls.clear()
            with self.subTest(condition=condition), self.assertRaises(BusinessToolRequestError):
                self.call("count", filters=[condition])
            self.assertNotIn("count", self.data.calls)

    def test_undeclared_optional_facets_and_search_are_not_dispatched(self):
        for args in ({"page_filters": ["Missing status"], "page": "defaults"}, {"search": "Smaller"}):
            self.data.calls.clear()
            with self.subTest(args=args), self.assertRaises(BusinessToolRequestError):
                self.call("list", **args)
            self.assertNotIn("count", self.data.calls)
            self.assertNotIn("rows", self.data.calls)

    def test_identity_permission_and_cancel_checks_apply_to_flat_reads(self):
        for ctx, error in ((context(application="other"), BusinessToolIdentityError),
                           (context(scopes=("page:operate",)), BusinessToolPermissionError)):
            with self.assertRaises(error):
                self.call("count", ctx=ctx)
        self.assertFalse(self.bound)
        def checkpoint():
            if "count" in self.data.calls:
                raise TurnCancelled("stopped")
        with self.assertRaises(TurnCancelled):
            self.call("list", fields=["Email"], checkpoint=checkpoint)
        self.assertNotIn("rows", self.data.calls)

    def test_inspection_directs_the_model_to_the_selected_surface(self):
        result = self.tools.execute("inspect_data", {"query": "Records", "entity": "records"},
            context=context(), checkpoint=lambda: None, user_request="records")
        self.assertIn("in read_data", result.content["instructions"])
        self.assertNotIn("resolve_data", result.content["instructions"])


if __name__ == "__main__":
    unittest.main()
