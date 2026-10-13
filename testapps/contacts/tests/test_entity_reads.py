"""The synthetic REST adapter runs the same generic entity-read contract as Odoo."""
from __future__ import annotations

import time
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from web_operation_agent.business.entities import (
    ENTITY_READ_TOOLS, ENTITY_READ_SCHEMAS, RESOLVE_RECORD_REFERENCE_SCHEMA,
    EntityAggregate, EntityField, EntityReadTools,
    ReferenceLookupTools,
)
from web_operation_agent.business.runtime import (
    BusinessToolPermissionError,
    BusinessToolRequestError,
)
from web_operation_agent.answer.evidence import TurnEvidence
from web_operation_agent.goal.planning_reads import PlanningReads
from jsonschema import Draft202012Validator
from web_operation_agent.answer.conclusion import Aggregate
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control, _obs
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
    def test_skill_read_surface_tracks_identities_from_the_real_rest_adapter(self):
        with MinimalContactsApplication() as application:
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN)
            reads = adapter.business_tools._executor
            surface = PlanningReads((*ENTITY_READ_SCHEMAS, RESOLVE_RECORD_REFERENCE_SCHEMA))
            def execute(name, args):
                schema = next(s['function']['parameters'] for s in surface.schemas() if s['function']['name'] == name)
                Draft202012Validator(schema).validate(args)
                result = reads.execute(name, args).content
                surface.observe(name, result)
                return result
            found = execute('resolve_record_reference', {'entity_query': 'Contacts', 'reference': 'Grace Hopper'})
            entity = found['candidates'][0]['entity_id']
            description = execute('describe_entity', {'entity_id': entity})
            email = next(f['field_id'] for f in description['fields'] if f['label'] == 'Email')
            result = execute('query_entity_records', {'entity_id': entity, 'field_ids': [email]})
            self.assertTrue(result['records'])
            self.assertEqual(application.write_requests, 0)

    def test_count_only_exact_evidence_does_not_depend_on_returned_page(self):
        entity = self.tools.execute("discover_entities", {"query": "contacts"}).content["candidates"][0]["entity_id"]
        for offset in (0, 1, 20):
            result = self.tools.execute("query_entity_records", {
                "entity_id": entity, "field_ids": [], "offset": offset, "limit": 1})
            evidence = TurnEvidence()
            evidence.capture(result.evidence, namespace="call1")
            aggregate = Aggregate("call1-count#1").verify(evidence)
            self.assertEqual("2", aggregate["value"])
            self.assertTrue(aggregate["covers_whole_scope"])

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

    def test_reference_lookup_uses_the_rest_adapter_and_returns_bounded_routes(self):
        port = _ContactsEntityReadPort(_FakeContactsExecutor())
        tools = ReferenceLookupTools(port, port, application_id=APPLICATION_ID)
        result = tools.execute({
            "entity_query": "联系人", "reference": "Ada Lovelace"})
        self.assertEqual(result.content["candidates"][0]["record_id"], "c-1")
        self.assertEqual(result.allowed_routes, ("/contacts/c-1",))
        self.assertEqual(result.content["returned_count"], 1)

    def test_reference_lookup_reads_the_live_rest_application(self):
        with MinimalContactsApplication() as application:
            adapter = MinimalContactsAdapter(
                application.origin, api_token=DEMO_API_TOKEN)
            result = adapter.business_tools._executor._reference_lookup.execute({
                "entity_query": "contacts", "reference": "Grace Hopper"})
            self.assertEqual(result.content["candidates"][0]["record_id"],
                             "grace-hopper")
            self.assertEqual(result.allowed_routes, ("/contacts/grace-hopper",))

    def test_bound_reference_recovery_uses_live_rest_type_without_reranking(self):
        with MinimalContactsApplication() as application:
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN)
            executor = adapter.business_tools._executor
            refs = executor._reference_lookup
            miss = refs.execute({"entity_query": "compiler", "reference": "Grace Hopper"})
            self.assertEqual(miss.content["candidates"], [])
            # This is the same actor-scoped opaque ID ordinary entity tools expose.
            found = refs.execute({"entity_query": "contacts", "reference": "Ada Lovelace"})
            entity_id = found.content["candidates"][0]["entity_id"]
            suffix_miss = refs.execute({"entity_id": entity_id, "reference": "Grace Hopper contact"})
            self.assertEqual(suffix_miss.content["candidates"], [])
            recovered = refs.execute({"entity_id": entity_id, "reference": "Grace Hopper"})
            self.assertEqual(recovered.allowed_routes, ("/contacts/grace-hopper",))
            self.assertEqual(recovered.content["searched_entity_count"], 1)
            self.assertEqual(recovered.content["lookup_mode"], "bound_entity")
            self.assertEqual(application.write_requests, 0)

    def test_page_entry_contract_uses_the_rest_application_not_odoo_actions(self):
        with MinimalContactsApplication() as application:
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN)
            result = adapter.business_tools._executor._page_entries.execute({
                "query": "联系人"})
            self.assertEqual(result.content["candidates"][0]["ui_path"], "/contacts")
            self.assertEqual(result.allowed_routes, ("/contacts",))
            self.assertEqual(adapter.ports.routes.validate_tool_route("/contacts"),
                             "/contacts")
            detail = adapter.business_tools._executor._page_entries.execute({
                "query": "Contacts", "detail_query": "Email"})
            self.assertEqual(detail.content["candidates"][0]["part_matches"][0]["label"],
                             "Email")

    def test_goal_candidates_combine_rest_record_field_and_page_metadata(self):
        with MinimalContactsApplication() as application:
            adapter = MinimalContactsAdapter(
                application.origin, api_token=DEMO_API_TOKEN)
            result = adapter.business_tools._executor.execute(
                "resolve_goal_candidates", {"goals": [
                    {"delivery": "answer", "subject_query": "Contacts",
                     "reference": "Grace Hopper", "detail_query": "Email"},
                    {"delivery": "page", "subject_query": "Contacts",
                     "detail_query": "Email"}]})
            answer, page = result.content["goals"]
            self.assertEqual(answer["record_candidates"][0]["record_id"],
                             "grace-hopper")
            self.assertEqual(answer["field_candidates"][0]["fields"][0]["label"],
                             "Email")
            self.assertEqual(page["page_candidates"][0]["part_matches"][0]["label"],
                             "Email")
            self.assertIn("/contacts/grace-hopper", result.allowed_routes)
            self.assertIn("/contacts", result.allowed_routes)

    def test_rest_adapter_keeps_page_and_data_reads_for_location_requests(self):
        adapter = MinimalContactsAdapter("http://127.0.0.1:1", api_token="delegated")
        with tempfile.TemporaryDirectory() as tmp:
            session = ServerAgentSession(
                task=TaskSpec("contacts-location", "联系人在哪里管理？"),
                chat=_Chat([]), browser=_Browser([_obs(1)]),
                control=_Control([[], []]),
                ledger=SessionLedger(Path(tmp) / "audit.jsonl", "contacts-location"),
                business_tools=adapter.business_tools)
            names = {schema["function"]["name"] for schema in session.tool_schemas()}
            self.assertTrue({"find_page_entries", "resolve_record_reference",
                             "discover_entities", "query_entity_records",
                             "list_contacts"} <= names)

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

    def test_rest_port_can_bind_a_fixed_unit_from_its_own_schema(self):
        class RestCreditsPort(_ContactsEntityReadPort):
            def fields(self, entity, *, context=None):
                return super().fields(entity, context=context) + (
                    EntityField("credits", "Synthetic credits", "monetary", summable=True,
                                fixed_unit="TEST"),)

            def query(self, request, *, context=None):
                page = super().query(request, context=context)
                # The synthetic application defines one TEST credit per name character.
                matched = [row for row in self._executor._contact_rows()
                           if all(self._matches(row, f) for f in request.filters)]
                return replace(page, aggregates=(EntityAggregate(
                    "credits", str(sum(len(r["name"]) for r in matched)), len(matched), "TEST"),))

        tools = EntityReadTools(RestCreditsPort(_FakeContactsExecutor()), application_id=APPLICATION_ID)
        entity = tools.execute("discover_entities", {"query": "contacts"}).content[
            "candidates"][0]["entity_id"]
        fields = {f["label"]: f["field_id"] for f in tools.execute(
            "describe_entity", {"entity_id": entity}).content["fields"]}
        result = tools.execute("query_entity_records", {
            "entity_id": entity, "field_ids": [fields["Name"]], "limit": 1,
            "aggregate_field_ids": [fields["Synthetic credits"]]})
        self.assertEqual({"field_id": fields["Synthetic credits"], "label": "Synthetic credits",
                          "sum": "24", "count": 2, "unit": "TEST"}, result.content["aggregates"][0])

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
