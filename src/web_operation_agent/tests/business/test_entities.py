"""Application-neutral entity discovery and bounded query contract."""
from __future__ import annotations

import inspect
import time
import unittest

import web_operation_agent.business.entities as entity_module
from web_operation_agent.answer.conclusion import Aggregate
from web_operation_agent.answer.evidence import TurnEvidence
from web_operation_agent.business.entities import (
    DESCRIBE_ENTITY_SCHEMA,
    DESCRIBE_ENTITY_TOOL,
    DISCOVER_ENTITIES_SCHEMA,
    DISCOVER_ENTITIES_TOOL,
    QUERY_ENTITY_RECORDS_SCHEMA,
    QUERY_ENTITY_RECORDS_TOOL,
    RESOLVE_RECORD_REFERENCE_SCHEMA,
    RESOLVE_RECORD_REFERENCE_TOOL,
    EntityCatalog,
    EntityAggregate,
    EntityCell,
    EntityChoice,
    EntityDescriptor,
    EntityField,
    EntityPageHint,
    EntityQueryPage,
    EntityReadTools,
    EntityRecord,
    ReferenceLookupTools,
    ReferenceMatch,
)
from web_operation_agent.business.runtime import (
    BusinessToolContractError,
    BusinessToolPermissionError,
    BusinessToolRequestError,
)
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.tests.support.entity_read_contract import (
    assert_entity_read_contract,
)


class _Port:
    def __init__(self):
        self.revision = "1"
        self.field_label = "Status"
        self.query_calls = 0

    def catalog(self, *, context=None):
        return EntityCatalog(
            binding_digest=self.revision * 64,
            entities=(EntityDescriptor(
                reference="internal.work.item", label="Work Items",
                search_text="work items tasks tickets",
                page_hints=(EntityPageHint(
                    name="Work Items", breadcrumbs=("Operations / Work Items",),
                    views=("list",)),),
            ),),
            complete=True,
            scope="entities visible to the current fixture actor",
        )

    def fields(self, entity, *, context=None):
        return (
            EntityField(reference="technical_title", label="Title", kind="text"),
            EntityField(
                reference="technical_state_code", label=self.field_label, kind="choice",
                choices=(EntityChoice("open", "Open"), EntityChoice("done", "Done"))),
        )

    def query(self, request, *, context=None):
        self.query_calls += 1
        values = {"technical_title": "Prepare release", "technical_state_code": "Done"}
        return EntityQueryPage(
            records=(EntityRecord(
                reference="internal.work.item/42", display_id="42",
                cells=tuple(EntityCell(field.reference, values[field.reference])
                            for field in request.fields),
            ),),
            matched_count=1,
            source_total=3,
            source="fixture actor API",
            scope="work items visible to the current fixture actor",
        )


class RoutedPort(_Port):
    """The same entity, with the application saying where each record opens."""

    def query(self, request, *, context=None):
        page = super().query(request, context=context)
        return EntityQueryPage(
            records=tuple(EntityRecord(reference=record.reference,
                                       display_id=record.display_id, cells=record.cells,
                                       route="/work/items/42")
                          for record in page.records),
            matched_count=page.matched_count, source_total=page.source_total,
            source=page.source, scope=page.scope)


class EntityRecordRoutes(unittest.TestCase):
    """A record found through the generic read carries where it opens, when the
    application can say, and that exact route becomes one the turn may open."""

    def _query(self, port):
        tools = EntityReadTools(port, application_id="fixture")
        entity_id = tools.execute(DISCOVER_ENTITIES_TOOL, {"query": "work items"}) \
            .content["candidates"][0]["entity_id"]
        fields = tools.execute(DESCRIBE_ENTITY_TOOL, {"entity_id": entity_id}) \
            .content["fields"]
        return tools.execute(QUERY_ENTITY_RECORDS_TOOL, {
            "entity_id": entity_id, "field_ids": [fields[0]["field_id"]]})

    def test_a_routed_record_is_openable_and_nothing_else_is(self):
        result = self._query(RoutedPort())
        self.assertEqual(result.content["records"][0]["ui_path"], "/work/items/42")
        self.assertEqual(result.allowed_routes, ("/work/items/42",))
        plain = self._query(_Port())
        self.assertNotIn("ui_path", plain.content["records"][0])
        self.assertEqual(plain.allowed_routes, ())

    def test_a_route_that_is_not_an_application_path_is_refused(self):
        for bad in ("work/items/42", "//evil.example/x", "https://evil.example/x"):
            with self.subTest(route=bad), self.assertRaises(ValueError):
                EntityRecord(reference="r", display_id="1", cells=(), route=bad)


class ReferenceLookupContractTests(unittest.TestCase):
    class _Lookup:
        def __init__(self):
            self.calls = []

        def lookup(self, entity, reference, *, limit, context=None):
            self.calls.append((entity.reference, reference, limit))
            if reference == "Prepare release":
                return ((ReferenceMatch("42", "Prepare release", "/work/items/42"),), False)
            if reference == "duplicate":
                return ((ReferenceMatch("42", "duplicate", "/work/items/42"),
                         ReferenceMatch("43", "duplicate", "/work/items/43")), False)
            return (), False

    def setUp(self):
        self.lookup = self._Lookup()
        self.tools = ReferenceLookupTools(
            _Port(), self.lookup, application_id="fixture.app")

    def test_one_call_resolves_actor_catalog_entity_and_record_without_native_names(self):
        result = self.tools.execute({
            "entity_query": "work items", "reference": "Prepare release"})
        self.assertEqual(self.lookup.calls, [
            ("internal.work.item", "Prepare release", 5)])
        self.assertEqual(result.content["candidates"][0]["record_id"], "42")
        self.assertRegex(result.content["candidates"][0]["entity_id"],
                         r"^entity_[0-9a-f]{32}$")
        self.assertEqual(result.allowed_routes, ("/work/items/42",))
        self.assertNotIn("internal.work.item", result.render())
        self.assertNotIn("model", RESOLVE_RECORD_REFERENCE_SCHEMA["function"]
                         ["parameters"]["properties"])

    def test_ambiguity_and_empty_are_not_autoselected_or_global_not_found(self):
        ambiguous = self.tools.execute({
            "entity_query": "work items", "reference": "duplicate"})
        self.assertEqual(ambiguous.content["returned_count"], 2)
        self.assertEqual(ambiguous.allowed_routes,
                         ("/work/items/42", "/work/items/43"))
        empty = self.tools.execute({
            "entity_query": "work items", "reference": "missing"})
        self.assertEqual(empty.content["candidates"], [])
        self.assertNotIn("not_found", empty.content)
        self.assertEqual(empty.content["searched_entity_count"], 1)

    def test_malformed_port_results_and_unbounded_requests_fail(self):
        with self.assertRaises(BusinessToolRequestError):
            self.tools.execute({"entity_query": "work items", "reference": "x", "limit": 6})
        self.lookup.lookup = lambda *args, **kwargs: (("unvalidated",), False)
        with self.assertRaises(BusinessToolContractError):
            self.tools.execute({"entity_query": "work items", "reference": "x"})

    def test_a_permission_refusal_does_not_become_a_not_found_claim(self):
        def refused(*args, **kwargs):
            raise BusinessToolPermissionError("record lookup denied")

        self.lookup.lookup = refused
        result = self.tools.execute({
            "entity_query": "work items", "reference": "Prepare release"})
        self.assertEqual(result.content["candidates"], [])
        self.assertEqual(result.content["permission_omissions"], 1)
        self.assertTrue(result.content["truncated_or_incomplete"])


class EntityReadContractTests(unittest.TestCase):
    def setUp(self):
        self.port = _Port()
        self.tools = EntityReadTools(self.port, application_id="fixture.app")

    def test_shared_contract_runs_without_application_framework_concepts(self):
        assert_entity_read_contract(
            self, self.tools,
            query="work items", entity_label="Work Items",
            field_label="Status", filter_operator="=", filter_value="done",
            expected_record_id="42", expected_value="Done",
        )
        source = inspect.getsource(entity_module).casefold()
        for forbidden in ("odoo", "fields_get", "json-rpc", "jsonrpc"):
            self.assertNotIn(forbidden, source)

    def test_agent_schemas_accept_only_opaque_generic_bindings(self):
        self.assertEqual(
            set(DISCOVER_ENTITIES_SCHEMA["function"]["parameters"]["properties"]),
            {"query", "limit"})
        self.assertEqual(
            set(DESCRIBE_ENTITY_SCHEMA["function"]["parameters"]["properties"]),
            {"entity_id", "field_query", "limit"})
        query = QUERY_ENTITY_RECORDS_SCHEMA["function"]["parameters"]["properties"]
        self.assertEqual(set(query), {"entity_id", "field_ids", "filters", "limit", "offset",
                                      "aggregate_field_ids"})
        self.assertFalse({"model", "table", "endpoint", "method", "domain", "order"}
                         & set(query))

    def test_native_references_do_not_cross_the_agent_facing_result(self):
        discovery = self.tools.execute(DISCOVER_ENTITIES_TOOL, {"query": "work items"})
        entity_id = discovery.content["candidates"][0]["entity_id"]
        description = self.tools.execute(DESCRIBE_ENTITY_TOOL, {
            "entity_id": entity_id, "field_query": "status",
        })
        field_id = description.content["fields"][0]["field_id"]
        result = self.tools.execute(QUERY_ENTITY_RECORDS_TOOL, {
            "entity_id": entity_id, "field_ids": [field_id],
        })

        self.assertNotIn("internal.work.item", result.render())
        self.assertNotIn("technical_state_code", result.render())
        self.assertEqual(result.evidence[1].record_id, "Work Items/42")
        self.assertEqual(result.evidence[1].field_name, "status")
        self.assertEqual(result.query_answer.fields, ("record_id", "status"))

    def test_entity_bindings_are_scoped_to_the_current_actor(self):
        def context(actor):
            now = int(time.time() * 1_000)
            principal = PrincipalContext(
                schema_version=1, application_id="fixture.app",
                tenant_id="tenant", workspace_id="workspace", actor_id=actor,
                delegation_id="delegation", issued_at=now - 1_000,
                expires_at=now + 60_000, scopes=("records:read",),
            )
            return OperationContext.create(
                principal, session_id="session", turn_id="turn",
                operation_id=f"operation-{actor}", deadline_ms=now + 30_000,
                now_ms=now,
            )

        first = context("actor-one")
        discovery = self.tools.execute(
            DISCOVER_ENTITIES_TOOL, {"query": "work items"}, context=first)
        entity_id = discovery.content["candidates"][0]["entity_id"]
        with self.assertRaisesRegex(BusinessToolRequestError, "absent or stale"):
            self.tools.execute(
                DESCRIBE_ENTITY_TOOL, {"entity_id": entity_id},
                context=context("actor-two"))

    def test_catalog_and_field_drift_invalidate_old_bindings(self):
        discovery = self.tools.execute(DISCOVER_ENTITIES_TOOL, {"query": "work items"})
        entity_id = discovery.content["candidates"][0]["entity_id"]
        description = self.tools.execute(DESCRIBE_ENTITY_TOOL, {"entity_id": entity_id})
        old_field = description.content["fields"][0]["field_id"]

        self.port.field_label = "Work title"
        with self.assertRaisesRegex(BusinessToolRequestError, "describe_entity"):
            self.tools.execute(QUERY_ENTITY_RECORDS_TOOL, {
                "entity_id": entity_id, "field_ids": [old_field],
            })
        self.assertEqual(self.port.query_calls, 0)

        self.port.revision = "2"
        with self.assertRaisesRegex(BusinessToolRequestError, "absent or stale"):
            self.tools.execute(DESCRIBE_ENTITY_TOOL, {"entity_id": entity_id})

    def test_missing_application_cells_fail_closed(self):
        discovery = self.tools.execute(DISCOVER_ENTITIES_TOOL, {"query": "work items"})
        entity_id = discovery.content["candidates"][0]["entity_id"]
        description = self.tools.execute(DESCRIBE_ENTITY_TOOL, {"entity_id": entity_id})
        ids = [item["field_id"] for item in description.content["fields"]]

        def malformed(_request, *, context=None):
            return EntityQueryPage(
                records=(EntityRecord(
                    reference="internal.work.item/42", display_id="42",
                    cells=(EntityCell("technical_title", "Prepare release"),),
                ),),
                matched_count=1, source_total=1,
                source="fixture actor API", scope="fixture actor scope",
            )

        self.port.query = malformed
        with self.assertRaisesRegex(BusinessToolContractError, "omitted"):
            self.tools.execute(QUERY_ENTITY_RECORDS_TOOL, {
                "entity_id": entity_id, "field_ids": ids,
            })

    def test_money_needs_a_valid_unit_and_never_leaks_its_native_binding(self):
        class MoneyPort(_Port):
            def fields(self, entity, *, context=None):
                return (EntityField(
                    reference="native_amount", label="Amount", kind="monetary",
                    unit_field_reference="native_currency_id"),)

            def query(self, request, *, context=None):
                return EntityQueryPage(
                    records=(EntityRecord(
                        reference="internal.work.item/42", display_id="42",
                        cells=(EntityCell("native_amount", "12.50", unit="USD"),),
                    ),), matched_count=1, source_total=1,
                    source="fixture actor API", scope="fixture actor scope")

        port = MoneyPort()
        tools = EntityReadTools(port, application_id="fixture.app")
        entity_id = tools.execute(DISCOVER_ENTITIES_TOOL, {"query": "work items"}) \
            .content["candidates"][0]["entity_id"]
        field_id = tools.execute(DESCRIBE_ENTITY_TOOL, {"entity_id": entity_id}) \
            .content["fields"][0]["field_id"]
        args = {"entity_id": entity_id, "field_ids": [field_id]}
        result = tools.execute(QUERY_ENTITY_RECORDS_TOOL, args)
        self.assertEqual(result.evidence[1].value_type, "money")
        self.assertEqual(result.evidence[1].unit, "USD")
        self.assertNotIn("native_currency_id", result.render())

        original_query = port.query

        def malformed(_request, *, context=None):
            page = original_query(_request, context=context)
            return EntityQueryPage(
                records=(EntityRecord(
                    reference=page.records[0].reference, display_id="42",
                    cells=(EntityCell("native_amount", "not a number", unit="USD"),),
                ),), matched_count=1, source_total=1,
                source=page.source, scope=page.scope)

        port.query = malformed
        with self.assertRaisesRegex(BusinessToolContractError, "nonnumeric"):
            tools.execute(QUERY_ENTITY_RECORDS_TOOL, args)

    def test_exact_sum_is_bound_to_same_query_scope_and_not_row_limit(self):
        class SummingPort(_Port):
            def fields(self, entity, *, context=None):
                return (EntityField(reference="private_due", label="Amount Due",
                                    kind="monetary", summable=True,
                                    unit_field_reference="private_currency"),)

            def query(self, request, *, context=None):
                return EntityQueryPage(
                    records=(EntityRecord(
                        reference="item/1", display_id="1",
                        cells=(EntityCell("private_due", "10.50", unit="USD"),)),),
                    matched_count=3, source_total=5,
                    source="actor API", scope="actor-visible unpaid records",
                    aggregates=(EntityAggregate("private_due", "92.75", 3, "USD"),),
                )

        tools = EntityReadTools(SummingPort(), application_id="fixture.app")
        entity_id = tools.execute(DISCOVER_ENTITIES_TOOL, {"query": "work items"}) \
            .content["candidates"][0]["entity_id"]
        field = tools.execute(DESCRIBE_ENTITY_TOOL, {"entity_id": entity_id}) \
            .content["fields"][0]
        self.assertTrue(field["summable"])
        result = tools.execute(QUERY_ENTITY_RECORDS_TOOL, {
            "entity_id": entity_id, "field_ids": [field["field_id"]],
            "aggregate_field_ids": [field["field_id"]], "limit": 1,
        })
        self.assertEqual(result.content["completeness"]["matched_count"], 3)
        self.assertTrue(result.content["completeness"]["result_truncated"])
        self.assertEqual(result.content["aggregates"], [{
            "field_id": field["field_id"], "label": "Amount Due",
            "sum": "92.75", "count": 3, "unit": "USD",
        }])
        self.assertEqual(result.evidence[1].inputs, ("query#1",))
        self.assertEqual(result.evidence[1].origin.kind, "typed")
        self.assertTrue(result.evidence[1].covers_whole_scope)
        turn = TurnEvidence()
        turn.capture(result.evidence, namespace="call1")
        self.assertEqual(Aggregate("call1-aggregate#1").verify(turn)["source_kind"],
                         "typed")
        self.assertNotIn("private_currency", result.render())

        class PartialPort(SummingPort):
            def query(self, request, *, context=None):
                page = super().query(request, context=context)
                return EntityQueryPage(
                    records=page.records, matched_count=3, source_total=5,
                    source=page.source, scope=page.scope,
                    aggregates=(EntityAggregate("private_due", "10.50", 1, "USD"),),
                )

        partial = EntityReadTools(PartialPort(), application_id="fixture.app")
        with self.assertRaisesRegex(BusinessToolContractError, "incomplete aggregate"):
            partial.execute(QUERY_ENTITY_RECORDS_TOOL, {
                "entity_id": entity_id, "field_ids": [field["field_id"]],
                "aggregate_field_ids": [field["field_id"]],
            })

    def test_schema_declared_fixed_currency_is_bound_and_cannot_drift(self):
        class FixedUnitPort(_Port):
            unit = "USD"
            aggregate_unit = "USD"

            def fields(self, entity, *, context=None):
                return (EntityField("amount", "Amount", "monetary", summable=True,
                                    fixed_unit="USD"),)

            def query(self, request, *, context=None):
                return EntityQueryPage(
                    records=(EntityRecord("r1", "1", (EntityCell("amount", "10.50", self.unit),)),),
                    matched_count=1, source_total=1, source="fixture API", scope="actor records",
                    aggregates=(EntityAggregate("amount", "10.50", 1, self.aggregate_unit),))

        port = FixedUnitPort()
        tools = EntityReadTools(port, application_id="fixture.app")
        entity = tools.execute("discover_entities", {"query": "work items"}).content[
            "candidates"][0]["entity_id"]
        field = tools.execute("describe_entity", {"entity_id": entity}).content["fields"][0]["field_id"]
        args = {"entity_id": entity, "field_ids": [field], "aggregate_field_ids": [field]}
        self.assertEqual("USD", tools.execute("query_entity_records", args).content["aggregates"][0]["unit"])
        for value in ("EUR", ""):
            port.unit = value
            with self.subTest(cell_unit=value), self.assertRaises(BusinessToolContractError):
                tools.execute("query_entity_records", args)
        port.unit, port.aggregate_unit = "USD", "EUR"
        with self.assertRaises(BusinessToolContractError):
            tools.execute("query_entity_records", args)
        for kwargs in ({"kind": "text", "fixed_unit": "USD"},
                       {"kind": "monetary", "fixed_unit": "USD", "unit_field_reference": "currency"},
                       {"kind": "monetary", "fixed_unit": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                EntityField(reference="amount", label="Amount", **kwargs)

    def test_related_record_binding_is_opaque_and_only_for_catalog_entities(self):
        class RelatedPort(_Port):
            target = "internal.work.item"

            def fields(self, entity, *, context=None):
                return (EntityField(
                    reference="native_relation", label="Owner", kind="reference",
                    related_entity_reference=self.target),)

            def query(self, request, *, context=None):
                return EntityQueryPage(
                    records=(EntityRecord(
                        reference="internal.work.item/42", display_id="42",
                        cells=(EntityCell("native_relation", "Alex",
                                          related_record_id="17"),),
                    ),), matched_count=1, source_total=1,
                    source="fixture actor API", scope="fixture actor scope")

        port = RelatedPort()
        tools = EntityReadTools(port, application_id="fixture.app")
        entity_id = tools.execute(DISCOVER_ENTITIES_TOOL, {"query": "work items"}) \
            .content["candidates"][0]["entity_id"]
        field_id = tools.execute(DESCRIBE_ENTITY_TOOL, {"entity_id": entity_id}) \
            .content["fields"][0]["field_id"]
        args = {"entity_id": entity_id, "field_ids": [field_id]}
        value = tools.execute(QUERY_ENTITY_RECORDS_TOOL, args) \
            .content["records"][0]["values"][0]
        self.assertEqual(value["related_entity_id"], entity_id)
        self.assertEqual(value["related_record_id"], "17")
        self.assertNotIn("internal.work.item", str(value))

        port.target = "internal.hidden.entity"
        field_id = tools.execute(DESCRIBE_ENTITY_TOOL, {"entity_id": entity_id}) \
            .content["fields"][0]["field_id"]
        value = tools.execute(QUERY_ENTITY_RECORDS_TOOL, {
            "entity_id": entity_id, "field_ids": [field_id],
        }).content["records"][0]["values"][0]
        self.assertNotIn("related_entity_id", value)
        self.assertNotIn("related_record_id", value)


if __name__ == "__main__":
    unittest.main()
