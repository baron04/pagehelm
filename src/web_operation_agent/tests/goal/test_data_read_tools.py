"""Same-agent data reads preserve native values, identity and query completeness."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import time
import unittest

from web_operation_agent.answer.evidence import (ComputationEvidence, QueryEvidence,
                                                 RecordEvidence, SelectionEvidence, TurnEvidence)
from web_operation_agent.answer.conclusion import Selection
from web_operation_agent.business.errors import (BusinessToolContractError, BusinessToolIdentityError,
    BusinessToolPermissionError, BusinessToolRequestError, BusinessToolUnavailableError)
from web_operation_agent.goal.data import (DataRefused, EntitySchema, FieldSchema, Group, LinkValue,
                                          Ref, Row, Schema, matches)
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexField
from web_operation_agent.goal.read_tools import DataReadTools
from web_operation_agent.goal.resolve.indexing import index_from_schema
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.transport.browser import TurnCancelled
from web_operation_agent.modeling.spend import BudgetExhausted


def context(scopes=("records:read",), *, application="sample", expires=None):
    now = int(time.time() * 1000)
    principal = PrincipalContext(1, application, "tenant", "workspace", "actor", "delegation",
                                 now - 1000, expires or now + 60_000, scopes)
    return OperationContext.create(principal, session_id="session", turn_id="turn",
                                   operation_id="read", deadline_ms=now + 50_000)


class Data:
    def __init__(self):
        self.calls = []
        self.rows_data = (
            Row("1", "Shared name", {"amount": 12, "email": "one@example.test", "status": "active"},
                {"amount": "12.00 EUR", "email": "one@example.test"}, {"amount": "EUR"}),
            Row("2", "Shared name", {"amount": 12, "email": None, "status": "active"},
                {"amount": "12.00 EUR", "email": "(empty)"}, {"amount": "EUR"}),
            Row("3", "Smaller", {"amount": -3, "email": "three@example.test", "status": "other"},
                {"amount": "-3.00 EUR", "email": "three@example.test"}, {"amount": "EUR"}),
        )
        self._schema = Schema(entities=(EntitySchema("records", "Records", fields=(
            FieldSchema("amount", "Amount", "money", listed=True),
            FieldSchema("email", "Email", "text", listed=True),
            FieldSchema("status", "Status", "text", listed=True)), operations=frozenset({"read"})),))

    def index(self, *, context=None):
        self.calls.append("index")
        if context is None:
            raise AssertionError("index must carry the bound operation")
        return index_from_schema(self._schema)

    def schema(self):
        self.calls.append("schema")
        return self._schema

    def capabilities(self):
        return frozenset()

    def name_of(self, text):
        return text

    def time_zone(self):
        return "UTC"

    def find(self, entity, text, *, limit=8):
        self.calls.append("find")
        return tuple(Ref(entity, r.id, r.display) for r in self.rows_data if text == r.display)[:limit]

    def read(self, entity, id, fields):
        self.calls.append("read")
        return self.project(next(r for r in self.rows_data if r.id == id), fields)

    def count(self, entity, where):
        self.calls.append("count")
        return sum(matches(where, r) for r in self.rows_data)

    def rows(self, entity, where, *, fields=(), order=(), limit=80):
        self.calls.append("rows")
        return tuple(self.project(r, fields) for r in self.rows_data if matches(where, r))[:limit]

    def total(self, entity, where, field):
        self.calls.append("total")
        return Decimal(sum(r.values[field] for r in self.rows_data if matches(where, r))), "EUR"

    def groups(self, entity, where, field, *, limit=12, by_month=False):
        self.calls.append("groups")
        return (Group("active", "Active", 2), Group("other", "Other", 1))[:limit]

    @staticmethod
    def project(row, fields):
        return replace(row, values={k: v for k, v in row.values.items() if k in fields},
                       shown={k: v for k, v in row.shown.items() if k in fields},
                       units={k: v for k, v in row.units.items() if k in fields})


class DataReadToolsTests(unittest.TestCase):
    def setUp(self):
        self.data = Data()
        self.factory_calls = []
        def factory(ctx, checkpoint):
            self.factory_calls.append(ctx)
            return self.data
        self.tools = DataReadTools(factory, "sample", "records:read", today=date(2026, 10, 6))

    def call(self, *deliverables, request="Read records", checkpoint=lambda: None, ctx=None):
        return self.tools.execute("resolve_data", {"deliverables": list(deliverables)},
            context=ctx or context(), checkpoint=checkpoint, user_request=request)

    def test_unsupported_scope_forms_refuse_before_binding_or_reading(self):
        for declaration in (
            {"kind": "list", "entity": "records", "via": "Customer A", "fields": ["Email"]},
            {"kind": "aggregate", "entity": "records", "via": "Customer A", "metric": {"op": "count"}},
            {"kind": "list", "entity": "records", "metric": {"op": "sum", "field": "Amount"}},
        ):
            with self.subTest(declaration=declaration), self.assertRaises(BusinessToolRequestError):
                self.call(declaration)
        self.assertEqual([], self.factory_calls)
        self.assertEqual([], self.data.calls)

    def test_list_retains_filter_conditions_and_separate_sum_reads_same_scope(self):
        condition = {"field": "Status", "values": ["active"]}
        result = self.call(
            {"kind": "list", "entity": "records", "fields": ["Email"],
             "metric": {"op": "count", "filters": [condition]}},
            {"kind": "aggregate", "entity": "records", "metric": {
                "op": "sum", "field": "Amount", "total": True, "filters": [condition]}})
        queries = [e for e in result.evidence if isinstance(e, QueryEvidence)]
        self.assertTrue(all(e.matched_count == 2 and e.count_exact for e in queries))
        self.assertTrue(queries[0].covers_whole_scope)
        self.assertEqual(queries[0].conditions, queries[1].conditions)
        self.assertIn("24", [e.value for e in result.evidence if isinstance(e, ComputationEvidence)])

    def test_fact_has_exact_record_field_native_value_and_no_route(self):
        result = self.call({"kind": "fact", "entity": "records", "record": "Smaller",
                            "fields": ["Amount", "Email"]})
        values = [e for e in result.evidence if isinstance(e, RecordEvidence)]
        self.assertEqual({"records:3"}, {e.record_id for e in values})
        amount = next(e for e in values if e.field_name == "amount")
        self.assertEqual(("-3", "EUR", True), (amount.value, amount.unit, amount.field_was_read))
        self.assertEqual((), result.allowed_routes)
        self.assertIsNone(result.form_draft)
        self.assertEqual("", result.navigation_target)
        self.assertNotIn("12.00 EUR", result.render())

    def test_unread_field_is_not_empty_and_ambiguous_name_is_not_bound(self):
        old = self.data.read
        self.data.read = lambda *a: replace(old(*a), values={})
        result = self.call({"kind": "fact", "entity": "records", "record": "Smaller", "fields": ["Email"]})
        email = next(e for e in result.evidence if isinstance(e, RecordEvidence) and e.field_name == "email")
        self.assertFalse(email.field_was_read)
        self.assertFalse(email.is_known_empty)
        result = self.call({"kind": "fact", "entity": "records", "record": "Shared name", "fields": ["Email"]})
        self.assertFalse(result.evidence)
        self.assertEqual(["Shared name"], list(dict.fromkeys(result.content["deliverables"][0]["choices"])))

    def test_complete_list_and_truncated_list_are_distinct(self):
        for limit, complete in ((2, False), (3, True)):
            result = self.call({"kind": "list", "entity": "records", "fields": ["Email"], "limit": limit})
            query = next(e for e in result.evidence if isinstance(e, QueryEvidence))
            self.assertEqual((3, True, complete), (query.matched_count, query.count_exact, query.covers_whole_scope))
            held = TurnEvidence()
            refs = held.capture(result.evidence, namespace="call1")
            selection = Selection(refs[0], ("@name", "email")).verify(held)
            self.assertEqual(limit, len(selection["members"]))
            self.assertEqual(complete, selection["covers_whole_scope"])

    def test_changed_count_cannot_prove_complete_list(self):
        old = self.data.count
        calls = []
        def changed(*a):
            calls.append(1)
            return old(*a) + (len(calls) > 1)
        self.data.count = changed
        result = self.call({"kind": "list", "entity": "records", "fields": ["Email"]})
        query = next(e for e in result.evidence if isinstance(e, QueryEvidence))
        self.assertFalse(query.count_exact)
        self.assertFalse(query.covers_whole_scope)

    def test_count_sum_and_empty_count_use_native_typed_results(self):
        result = self.call({"kind": "aggregate", "entity": "records", "metric": {"op": "sum", "field": "Amount"}})
        calc = [e for e in result.evidence if isinstance(e, ComputationEvidence)]
        self.assertEqual([("3", ""), ("21", "EUR")], [(e.value, e.unit) for e in calc])
        self.assertTrue(all(e.covers_whole_scope for e in calc))
        result = self.call({"kind": "aggregate", "entity": "records", "metric": {"op": "count",
            "filters": [{"field": "Status", "values": ["missing"]}]}})
        self.assertEqual(0, next(e for e in result.evidence if isinstance(e, QueryEvidence)).matched_count)

    def test_extreme_keeps_every_tied_id_and_bound_fields(self):
        result = self.call({"kind": "list", "entity": "records", "fields": ["Email"], "limit": 1,
                            "metric": {"op": "max", "field": "Amount"}})
        selection = next(e for e in result.evidence if isinstance(e, SelectionEvidence))
        self.assertEqual(("records:1", "records:2"), selection.members)
        self.assertTrue(selection.covers_whole_scope)
        emails = [e.value for e in result.evidence if isinstance(e, RecordEvidence) and e.field_name == "email"]
        self.assertEqual(["one@example.test", None], emails)
        self.assertNotIn("three@example.test", result.render())
        turn = TurnEvidence()
        turn.capture(result.evidence, namespace="call1")
        self.assertEqual(2, len(Selection("call1-" + selection.evidence_id, ("email",)).verify(turn)["members"]))

    def test_bad_units_values_and_wrong_id_do_not_become_prose_facts(self):
        for invalid in (True, "3", float("nan"), float("inf")):
            self.data.total = lambda *a, v=invalid: (v, "EUR")
            with self.subTest(value=invalid), self.assertRaises(BusinessToolContractError):
                self.call({"kind": "aggregate", "entity": "records", "metric": {"op": "sum", "field": "Amount"}})
        self.data.read = lambda *a: Row("other", "Wrong actor record", {"email": "hidden@example.test"})
        with self.assertRaises(BusinessToolContractError):
            self.call({"kind": "fact", "entity": "records", "record": "Smaller", "fields": ["Email"]})

    def test_missing_list_column_and_undeclared_optional_scope_never_dropped(self):
        result = self.call({"kind": "list", "entity": "records", "fields": ["Missing"]})
        self.assertFalse(result.evidence)
        self.assertNotIn("rows", self.data.calls)
        for item in ({"metric": {"op": "count", "page_filters": ["My records"]}}, {"page": {"search": "One"}}):
            with self.assertRaises(BusinessToolRequestError):
                self.call({"kind": "aggregate", "entity": "records", **item})
        self.assertNotIn("count", self.data.calls)

    def test_grouping_is_not_replaced_by_total_and_truncated_groups_stay_partial(self):
        with self.assertRaises(BusinessToolRequestError):
            self.call({"kind": "aggregate", "entity": "records", "metric": {"op": "count"}},
                      request="Records grouped by Status")
        for top, complete in ((0, True), (1, False)):
            result = self.call({"kind": "aggregate", "entity": "records",
                "metric": {"op": "count", "group_by": "Status", "top": top}})
            groups = [e for e in result.evidence if isinstance(e, QueryEvidence)][-1]
            self.assertEqual(complete, groups.covers_whole_scope)

    def test_write_source_navigation_and_unknown_properties_refuse_before_factory(self):
        for item in ({"kind": "page"}, {"kind": "handoff"}, {"kind": "fact", "rules": ["source"]},
                     {"kind": "fact", "action": "Delete"}, {"kind": "list", "page": {"filters_on": ["Any"]}},
                     {"kind": "aggregate", "metric": {"op": "count", "filters": [{"field": "Status", "value": "x"}]}},
                     {"kind": "list", "descending": "yes"}):
            with self.subTest(item=item), self.assertRaises(BusinessToolRequestError):
                self.call({"entity": "records", **item})
        self.assertFalse(self.factory_calls)

    def test_actor_scope_and_missing_identity_fail_before_application_reads(self):
        for ctx, error in ((None, BusinessToolIdentityError), (context(application="foreign"), BusinessToolIdentityError),
                           (context(scopes=("page:operate",)), BusinessToolPermissionError)):
            with self.assertRaises(error):
                self.tools.execute("inspect_data", {"query": "Records"}, context=ctx,
                                   checkpoint=lambda: None, user_request="Records")
        self.assertFalse(self.factory_calls)
        self.assertFalse(self.data.calls)

    def test_cancel_or_deadline_after_subread_stops_remaining_reads(self):
        for error in (TurnCancelled("stopped"), BudgetExhausted("wall_budget", "expired")):
            self.data.calls.clear()
            def check():
                if "count" in self.data.calls:
                    raise error
            with self.subTest(error=type(error)), self.assertRaises(type(error)):
                self.call({"kind": "list", "entity": "records", "fields": ["Email"]}, checkpoint=check)
            self.assertNotIn("rows", self.data.calls)

    def test_permission_and_unavailable_index_have_typed_errors(self):
        def unavailable(**kwargs):
            raise DataRefused("connection unavailable", read_failed=True)
        self.data.index = unavailable
        with self.assertRaises(BusinessToolUnavailableError):
            self.tools.execute("inspect_data", {"query": "Records"}, context=context(),
                               checkpoint=lambda: None, user_request="Records")
        self.data.index = lambda **kw: index_from_schema(self.data._schema)
        self.data.count = lambda *a: (_ for _ in ()).throw(DataRefused("denied", permission_denied=True))
        with self.assertRaises(BusinessToolPermissionError):
            self.call({"kind": "aggregate", "entity": "records", "metric": {"op": "count"}})

    def test_invalid_count_and_duplicate_rows_fail_closed(self):
        self.data.count = lambda *a: True
        with self.assertRaises(BusinessToolContractError):
            self.call({"kind": "aggregate", "entity": "records", "metric": {"op": "count"}})
        self.data.count = lambda *a: 2
        self.data.rows = lambda *a, **kw: (self.data.rows_data[0], self.data.rows_data[0])
        with self.assertRaises(BusinessToolContractError):
            self.call({"kind": "list", "entity": "records"})

    def test_metadata_is_bounded_and_omission_is_explicit(self):
        self.data.index = lambda **kw: ApplicationIndex(entities=tuple(IndexEntity(f"records-{i}", f"Records {i}",
            fields=tuple(IndexField(f"f{n}", f"Field {n}", "text") for n in range(70))) for i in range(100)))
        result = self.tools.execute("inspect_data", {"query": "Records 99"}, context=context(),
                                   checkpoint=lambda: None, user_request="Records 99")
        self.assertEqual(6, result.content["shown_entities"])
        self.assertFalse(result.content["sample_complete"])
        self.assertFalse(result.content["entities"][0]["fields_complete"])
        self.assertEqual(40, len(result.content["entities"][0]["fields"]))
        self.assertFalse(result.evidence)

    def test_content_digest_changes_with_values_but_not_operation_nonce(self):
        d = {"kind": "aggregate", "entity": "records", "metric": {"op": "count"}}
        first = self.call(d)
        again = self.call(d)
        self.assertEqual(first.content_digest, again.content_digest)
        self.data.rows_data = self.data.rows_data[:1]
        changed = self.call(d)
        self.assertNotEqual(first.content_digest, changed.content_digest)


if __name__ == "__main__":
    unittest.main()
