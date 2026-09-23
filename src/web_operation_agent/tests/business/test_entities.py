"""Application-neutral entity discovery and bounded query contract."""
from __future__ import annotations

import inspect
import time
import unittest

import web_operation_agent.business.entities as entity_module
from web_operation_agent.business.entities import (
    DESCRIBE_ENTITY_SCHEMA,
    DESCRIBE_ENTITY_TOOL,
    DISCOVER_ENTITIES_SCHEMA,
    DISCOVER_ENTITIES_TOOL,
    QUERY_ENTITY_RECORDS_SCHEMA,
    QUERY_ENTITY_RECORDS_TOOL,
    EntityCatalog,
    EntityCell,
    EntityChoice,
    EntityDescriptor,
    EntityField,
    EntityPageHint,
    EntityQueryPage,
    EntityReadTools,
    EntityRecord,
)
from web_operation_agent.business.runtime import (
    BusinessToolContractError,
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
        self.assertEqual(set(query), {"entity_id", "field_ids", "filters", "limit", "offset"})
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


if __name__ == "__main__":
    unittest.main()
