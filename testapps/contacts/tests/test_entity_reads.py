"""The synthetic REST adapter runs the same generic entity-read contract as Odoo."""
from __future__ import annotations

import time
import unittest
from dataclasses import replace

from web_operation_agent.business.entities import (
    ENTITY_READ_TOOLS, EntityAggregate, EntityField, EntityReadTools,
)
from web_operation_agent.business.runtime import (
    BusinessToolPermissionError,
    BusinessToolRequestError,
)
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.tests.support.entity_read_contract import (
    assert_entity_read_contract,
)
from testapps.contacts.adapter import (
    APPLICATION_ID,
    MinimalContactsAdapter,
    _ContactsEntityReadPort,
)
from testapps.contacts.app import DEMO_API_TOKEN, MinimalContactsApplication


class _FakeContactsExecutor:
    @staticmethod
    def _contact_rows():
        return [
            {"id": "c-2", "name": "Grace Hopper", "email": "grace@example.test"},
            {"id": "c-1", "name": "Ada Lovelace", "email": "ada@example.test"},
        ]

    @staticmethod
    def _scope(context):
        return "contacts the delegated synthetic actor may read"


class ContactsEntityReadContractTests(unittest.TestCase):
    def setUp(self):
        self.tools = EntityReadTools(
            _ContactsEntityReadPort(_FakeContactsExecutor()),
            application_id=APPLICATION_ID,
        )

    def test_shared_application_neutral_contract(self):
        assert_entity_read_contract(
            self, self.tools,
            query="address book contacts", entity_label="Contacts",
            field_label="Name", filter_operator="contains", filter_value="ada",
            expected_record_id="c-1", expected_value="Ada Lovelace",
        )

    def test_a_rest_backed_test_adapter_can_supply_the_same_exact_sum_contract(self):
        class RestMetricPort(_ContactsEntityReadPort):
            def fields(self, entity, *, context=None):
                return super().fields(entity, context=context) + (
                    EntityField(reference="name_length", label="Name length",
                                kind="integer", summable=True, searchable=False),)

            def query(self, request, *, context=None):
                page = super().query(request, context=context)
                if not request.aggregate_fields:
                    return page
                matched = [row for row in self._executor._contact_rows()
                           if all(self._matches(row, condition)
                                  for condition in request.filters)]
                return replace(page, aggregates=(EntityAggregate(
                    field_reference="name_length",
                    value=str(sum(len(row["name"]) for row in matched)),
                    count=len(matched)),))

        tools = EntityReadTools(RestMetricPort(_FakeContactsExecutor()),
                                application_id=APPLICATION_ID)
        entity_id = tools.execute("discover_entities", {"query": "contacts"}) \
            .content["candidates"][0]["entity_id"]
        fields = {item["label"]: item["field_id"] for item in
                  tools.execute("describe_entity", {"entity_id": entity_id})
                  .content["fields"]}
        result = tools.execute("query_entity_records", {
            "entity_id": entity_id, "field_ids": [fields["Name"]],
            "aggregate_field_ids": [fields["Name length"]], "limit": 1,
        })
        self.assertEqual(result.content["aggregates"], [{
            "field_id": fields["Name length"], "label": "Name length",
            "sum": "24", "count": 2,
        }])
        self.assertTrue(result.content["completeness"]["result_truncated"])

    def test_shared_contract_reads_the_live_rest_application(self):
        with MinimalContactsApplication() as application:
            adapter = MinimalContactsAdapter(
                application.origin, api_token=DEMO_API_TOKEN)
            assert_entity_read_contract(
                self, adapter.business_tools._executor._entity_reads,
                query="address book contacts", entity_label="Contacts",
                field_label="Name", filter_operator="contains", filter_value="grace",
                expected_record_id="grace-hopper", expected_value="Grace Hopper",
            )

    def test_adapter_exposes_the_same_three_tool_names_without_native_query_escape_hatches(self):
        adapter = MinimalContactsAdapter("http://127.0.0.1:1", api_token="delegated")
        self.assertTrue(ENTITY_READ_TOOLS <= adapter.business_tools.tool_names)
        manifest = adapter.business_tools.catalog_manifest
        generic = {item["name"] for item in manifest["tools"]
                   if item["name"] in ENTITY_READ_TOOLS}
        self.assertEqual(generic, ENTITY_READ_TOOLS)
        self.assertFalse({"table", "endpoint", "method", "selector"} & generic)

    def test_raw_resource_and_field_names_are_rejected_before_the_rest_adapter_reads(self):
        with self.assertRaisesRegex(BusinessToolRequestError, "entity_id"):
            self.tools.execute("query_entity_records", {
                "entity_id": "contacts", "field_ids": ["name"],
            })

    def test_actor_change_stales_the_catalog_binding(self):
        def context(actor):
            now = int(time.time() * 1_000)
            principal = PrincipalContext(
                schema_version=1, application_id=APPLICATION_ID,
                tenant_id="synthetic", workspace_id="workspace", actor_id=actor,
                delegation_id="delegation", issued_at=now - 1_000,
                expires_at=now + 60_000, scopes=("contacts:read",),
            )
            return OperationContext.create(
                principal, session_id="session", turn_id="turn",
                operation_id=f"operation-{actor}", deadline_ms=now + 30_000,
                now_ms=now,
            )

        one = context("actor-one")
        found = self.tools.execute("discover_entities", {"query": "contacts"}, context=one)
        entity_id = found.content["candidates"][0]["entity_id"]
        with self.assertRaisesRegex(BusinessToolRequestError, "absent or stale"):
            self.tools.execute("describe_entity", {"entity_id": entity_id},
                               context=context("actor-two"))

    def test_runtime_refuses_missing_scope_before_the_rest_adapter_is_called(self):
        adapter = MinimalContactsAdapter("http://127.0.0.1:1", api_token="delegated")
        now = int(time.time() * 1_000)
        principal = PrincipalContext(
            schema_version=1, application_id=APPLICATION_ID,
            tenant_id="synthetic", workspace_id="workspace", actor_id="reader",
            delegation_id="delegation", issued_at=now - 1_000,
            expires_at=now + 60_000, scopes=("unrelated:read",),
        )
        context = OperationContext.create(
            principal, session_id="session", turn_id="turn",
            operation_id="discover", deadline_ms=now + 30_000, now_ms=now,
        )
        skill_id = "minimal.runtime.records-query"
        with self.assertRaises(BusinessToolPermissionError):
            adapter.business_tools.execute(
                "discover_entities", {"query": "contacts"}, {skill_id}, context=context)


if __name__ == "__main__":
    unittest.main()
