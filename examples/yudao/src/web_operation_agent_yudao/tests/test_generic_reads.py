"""The shared discovery/read contract over synthetic Yudao response shapes."""
from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from web_operation_agent.business.errors import (BusinessToolContractError, BusinessToolRequestError,
                                                 BusinessToolUnavailableError,
                                                 BusinessToolPermissionError,
                                                 BusinessToolAuthenticationError)
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent.tests.support.entity_read_contract import assert_entity_read_contract
from web_operation_agent_yudao.application import YudaoApplication
from web_operation_agent_yudao.client import YudaoReadRefused
from web_operation_agent_yudao.generic_reads import YudaoReadTools
from web_operation_agent_yudao.goal import YudaoGoalData
from web_operation_agent_yudao.ui_source import UiSource
from .test_goal import CONTRACTS, DOCS, MENUS, _Client, _prop


class GenericReadTests(unittest.TestCase):
    def test_actor_menu_non_list_page_and_wrapper_survive_without_source_or_entity(self):
        menus = deepcopy(MENUS) + [{"name": "Support workspace", "path": "/support",
            "component": "ParentView", "children": [
                {"name": "Help", "path": "help", "component": "support/help/overview"}]}]
        with patch("web_operation_agent_yudao.tests.test_goal.MENUS", menus):
            pages = self.tools.execute("find_page_entries", {"query": "Help"})
            wrapper = self.tools.execute("find_page_entries", {"query": "Support workspace", "limit": 1})
            index = self.data.index()
            self.assertEqual("", index.page("/support/help").entity)
            self.assertIsNone(index.entity("/support/help"))
            self.assertEqual(("/support/help",), pages.allowed_routes)
            self.assertTrue(pages.content["catalog_complete"])
            self.assertEqual((), wrapper.allowed_routes)
            self.assertFalse(wrapper.content["candidates"][0]["openable"])
            self.assertEqual("not_standalone_page", wrapper.content["candidates"][0]["unavailable_reason"])
            # The existing data page retains its actor-readable entity and list view.
            self.assertEqual("crm/contract", index.page("/crm/contract").entity)
            self.assertEqual(("list",), index.page("/crm/contract").views)
            port = DataGoalPort(self.data)
            with patch.object(self.data, "count", side_effect=AssertionError("not a data page")), \
                 patch.object(self.data, "rows", side_effect=AssertionError("not a data page")), \
                 patch.object(self.data, "find", side_effect=AssertionError("not a data page")):
                spec = GoalSpec.from_json({"deliverables": [{"kind": "page", "page": {"key": "/support/help"}}]})
                self.assertEqual("/support/help", port.resolve(spec).deliverables[0].page.url)
                for declaration in ({"kind": "aggregate", "metric": {"op": "count"}},
                                    {"kind": "list"}, {"kind": "fact", "fields": ["Name"]},
                                    {"kind": "page", "page": {"filters_on": ["Mine"]}},
                                    {"kind": "handoff", "action": "create"}):
                    spec = GoalSpec.from_json({"deliverables": [{**declaration,
                        "page": {"key": "/support/help", **declaration.get("page", {})}}]})
                    result = port.resolve(spec)
                    self.assertEqual("refused", result.deliverables[0].read_failure)
                    self.assertIsNone(result.deliverables[0].page)
                    self.assertEqual((), result.reads)

    def test_page_catalog_rechecks_generation_and_source_hints_never_add_routes(self):
        menus = deepcopy(MENUS) + [{"name": "Help", "path": "/help", "component": "support/help/overview"}]
        self.data._ui = UiSource(records={"/source-only": "/source-only/detail/{id}"})
        with patch("web_operation_agent_yudao.tests.test_goal.MENUS", menus):
            found = self.tools.execute("find_page_entries", {"query": "Help"})
            self.assertEqual(("/help",), found.allowed_routes)
        self.assertEqual((), self.tools.execute("find_page_entries", {"query": "source-only"}).allowed_routes)
        with self.assertRaises(DataRefused):
            self.data.page_address("/source-only")
        self.client.read_generation = 1
        removed = self.tools.execute("find_page_entries", {"query": "Help"})
        self.assertEqual([], removed.content["candidates"])
        self.assertEqual((), removed.allowed_routes)
        with self.assertRaises(DataRefused):
            self.data.page_address("/help")

    def test_catalog_reports_restricted_coverage_and_refuses_invalid_menu_payloads(self):
        restricted = YudaoReadTools(YudaoGoalData(self.client, self.docs, modules=("crm",)), application_id="yudao")
        self.assertFalse(restricted.execute("find_page_entries", {"query": "合同"}).content["catalog_complete"])
        with patch("web_operation_agent_yudao.tests.test_goal.MENUS", deepcopy(MENUS) + [{"name": "Malformed"}]):
            data = YudaoGoalData(_Client(), self.docs)
            with self.assertRaises(DataRefused):
                data.page_entries()
            self.assertIsNone(data._schema)
            self.assertFalse(data._menu_entries)

    def test_catalog_binding_cannot_mix_credential_generations(self):
        pages = self.tools.pages._port
        binding = pages.entities._binding

        def changed(context):
            self.client.read_generation = 1
            return binding(context)

        with patch.object(pages.entities, "_binding", side_effect=changed):
            with self.assertRaises(BusinessToolUnavailableError):
                self.tools.execute("find_page_entries", {"query": "合同"})

    def test_context_only_external_fragment_and_duplicate_menu_paths_fail_closed(self):
        unqualified = [{"name": "Context record", "path": "/customer/:id", "component": "customer/detail"},
                       {"name": "External page", "path": "https://example.test/help", "component": "help"},
                       {"name": "Protocol relative", "path": "//example.test/help", "component": "help"},
                       {"name": "Fragment page", "path": "/help#details", "component": "help"}]
        with patch("web_operation_agent_yudao.tests.test_goal.MENUS", unqualified):
            for query in ("Context record", "External page", "Protocol relative", "Fragment page"):
                found = self.tools.execute("find_page_entries", {"query": query})
                self.assertEqual((), found.allowed_routes)
                self.assertFalse(found.content["candidates"][0]["openable"])
            self.assertEqual((), self.data.schema().pages)
        duplicate = [{"name": "Help A", "path": "/help", "component": "help"},
                     {"name": "Help B", "path": "/help", "component": "help"}]
        with patch("web_operation_agent_yudao.tests.test_goal.MENUS", duplicate):
            data = YudaoGoalData(_Client(), self.docs)
            with self.assertRaises(DataRefused):
                data.page_entries()
            self.assertIsNone(data._schema)
            self.assertFalse(data._menu_entries)

    def test_count_only_does_not_fetch_rows_or_linked_names(self):
        entity = self.entity("合同")
        with patch.object(self.data, "rows", side_effect=AssertionError("unnecessary rows")):
            result = self.tools.execute("query_entity_records", {
                "entity_id": entity, "field_ids": []})
        self.assertEqual([], result.content["records"])
        count = next(e for e in result.evidence if e.evidence_id == "count#1")
        self.assertEqual("2", count.value)
        self.assertEqual(("query#1",), count.inputs)
        self.assertTrue(count.covers_whole_scope)

    def test_count_only_preserves_filters_and_certifies_zero_matches(self):
        entity = self.entity("合同")
        fields = self.fields(entity)
        for label, value, expected in (("审批状态", "20", "1"),
                                       ("合同名称", "not-in-this-fixture", "0")):
            with self.subTest(label=label):
                result = self.tools.execute("query_entity_records", {
                    "entity_id": entity, "field_ids": [], "filters": [{
                        "field_id": fields[label]["field_id"], "operator": "=", "value": value}]})
                self.assertEqual(expected, result.evidence[-1].value)
                self.assertEqual("count()", result.evidence[-1].operation)
                self.assertTrue(result.evidence[-1].covers_whole_scope)


    def setUp(self):
        self.client = _Client()
        self.docs = deepcopy(DOCS)
        self.ui = UiSource(records={"/crm/customer": "/crm/customer/detail/{id}"})
        self.data = YudaoGoalData(self.client, self.docs, ui=self.ui)
        self.tools = YudaoReadTools(self.data, application_id="yudao")

    def entity(self, query):
        return self.tools.execute("discover_entities", {"query": query}).content[
            "candidates"][0]["entity_id"]

    def fields(self, entity):
        return {f["label"]: f for f in self.tools.execute(
            "describe_entity", {"entity_id": entity}).content["fields"]}

    def test_same_black_box_contract_as_contacts_and_odoo(self):
        assert_entity_read_contract(
            self, self.tools.entity_reads, query="客户", entity_label="客户", field_label="客户名称",
            filter_operator="contains", filter_value="云帆", expected_record_id="5",
            expected_value="云帆科技有限公司")

    def test_page_addresses_and_record_addresses_are_distinct(self):
        refs = self.tools.execute("resolve_record_reference", {
            "entity_query": "客户", "reference": "云帆"})
        self.assertEqual(("/crm/customer/detail/5",), refs.allowed_routes)
        # This entity has a collection page but no qualified record-detail template.
        contracts = self.tools.execute("resolve_record_reference", {
            "entity_query": "合同", "reference": "云帆"})
        self.assertEqual((), contracts.allowed_routes)
        pages = self.tools.execute("find_page_entries", {"query": "合同"})
        self.assertIn("/crm/contract", pages.allowed_routes)

    def test_bound_record_lookup_uses_only_discovered_type_and_preserves_exact_route(self):
        customer = self.entity("客户")
        miss = self.tools.execute("resolve_record_reference", {
            "entity_id": customer, "reference": "云帆科技有限公司客户"})
        self.assertEqual(miss.allowed_routes, ())
        self.client.calls.clear()
        found = self.tools.execute("resolve_record_reference", {
            "entity_id": customer, "reference": "云帆科技有限公司"})
        self.assertEqual(found.allowed_routes, ("/crm/customer/detail/5",))
        self.assertEqual(found.content["searched_entity_count"], 1)
        self.assertEqual(found.content["lookup_mode"], "bound_entity")
        self.assertFalse(any(path.startswith("crm/contract/") for path, _params in self.client.calls))

    def test_complete_counts_sum_cents_and_stable_paging(self):
        self.docs["components"]["schemas"]["Contract"]["properties"]["totalPrice"][
            "description"] = "合同金额，单位：分"
        entity = self.entity("合同")
        fields = self.fields(entity)
        name, price = (fields[label]["field_id"] for label in ("合同名称", "合同金额"))
        self.assertTrue(fields["合同金额"]["summable"])
        result = self.tools.execute("query_entity_records", {
            "entity_id": entity, "field_ids": [name, price], "limit": 1, "offset": 1,
            "aggregate_field_ids": [price]})
        self.assertEqual("12", result.content["records"][0]["record_id"])
        self.assertEqual("75.0", result.content["records"][0]["values"][1]["value"])
        self.assertEqual("元", result.content["records"][0]["values"][1]["unit"])
        self.assertEqual(2, result.content["completeness"]["matched_count"])
        self.assertTrue(result.content["completeness"]["result_truncated"])
        aggregate = result.content["aggregates"][0]
        self.assertEqual("373.0", aggregate["sum"])
        self.assertEqual("元", aggregate["unit"])
        # All CRM record-permission scenes were used; a duplicate scene record is merged.
        self.assertEqual({1, 2, 3}, {p["sceneType"] for path, p in self.client.calls
                                   if path == "crm/contract/page"})

    def test_unknown_currency_cannot_be_summed(self):
        entity = self.entity("合同")
        price = self.fields(entity)["合同金额"]
        self.assertFalse(price["summable"])
        with self.assertRaises(BusinessToolRequestError):
            self.tools.execute("query_entity_records", {
                "entity_id": entity, "field_ids": [price["field_id"]],
                "aggregate_field_ids": [price["field_id"]]})

    def test_choice_and_date_conditions_are_applied_together(self):
        entity = self.entity("合同")
        fields = self.fields(entity)
        result = self.tools.execute("query_entity_records", {
            "entity_id": entity, "field_ids": [fields["合同名称"]["field_id"]],
            "filters": [
                {"field_id": fields["审批状态"]["field_id"], "operator": "=", "value": "20"},
                {"field_id": fields["下单日期"]["field_id"], "operator": ">=",
                 "value": "2026-09-01T00:00:00"}]})
        self.assertEqual(["11"], [r["record_id"] for r in result.content["records"]])

    def test_renewal_rebuilds_permissions_and_invalidates_opaque_ids(self):
        old = self.entity("合同")
        permission_reads = sum(p == "system/auth/get-permission-info" for p, _ in self.client.calls)
        self.client.read_generation = 1
        with patch("web_operation_agent_yudao.tests.test_goal.MENUS", []):
            with self.assertRaises(BusinessToolRequestError):
                self.tools.execute("describe_entity", {"entity_id": old})
            self.assertEqual([], self.tools.execute("discover_entities", {"query": "合同"}).content[
                "candidates"])
        self.assertEqual(permission_reads + 1,
                         sum(p == "system/auth/get-permission-info" for p, _ in self.client.calls))

    def test_renewal_during_metadata_read_refuses_mixed_generations(self):
        compile_schema = self.data._compile
        def renew_during_compile():
            schema = compile_schema()
            self.client.read_generation = 1
            return schema
        with patch.object(self.data, "_compile", side_effect=renew_during_compile):
            with self.assertRaises(BusinessToolUnavailableError):
                self.entity("合同")
        # A fresh attempt uses the renewed account's permissions.
        with patch("web_operation_agent_yudao.tests.test_goal.MENUS", []):
            self.assertEqual([], self.tools.execute("discover_entities", {"query": "合同"}).content[
                "candidates"])

    def test_authentication_and_permission_failures_retain_their_type(self):
        for code, error in ((401, BusinessToolAuthenticationError),
                            (403, BusinessToolPermissionError),
                            (500, BusinessToolUnavailableError)):
            with self.subTest(code=code), patch.object(self.data, "schema",
                    side_effect=YudaoReadRefused("synthetic refusal", code=code)):
                with self.assertRaises(error):
                    self.entity("客户")

    def test_zoned_date_condition_uses_the_accounts_local_time(self):
        entity = self.entity("合同")
        fields = self.fields(entity)
        result = self.tools.execute("query_entity_records", {
            "entity_id": entity, "field_ids": [fields["合同名称"]["field_id"]],
            "filters": [{"field_id": fields["下单日期"]["field_id"], "operator": ">=",
                         "value": "2026-08-31T16:00:00Z"}]})
        self.assertEqual(["11"], [r["record_id"] for r in result.content["records"]])

    def test_bad_money_is_never_relabeled_or_implicitly_summed_as_zero(self):
        self.docs["components"]["schemas"]["Contract"]["properties"]["totalPrice"][
            "description"] = "合同金额，单位：分"
        entity = self.entity("合同")
        price = self.fields(entity)["合同金额"]["field_id"]
        # A raw string has not passed the legacy reader's cents normalization.
        for value in ("29800", None, True, float("inf")):
            rows = deepcopy(CONTRACTS)
            rows[1][0]["totalPrice"] = value
            with self.subTest(value=value), patch(
                    "web_operation_agent_yudao.tests.test_goal.CONTRACTS", rows):
                self.data._read.clear()
                with self.assertRaises(BusinessToolContractError):
                    self.tools.execute("query_entity_records", {
                        "entity_id": entity, "field_ids": [price],
                        "aggregate_field_ids": [price]})

    def test_credential_fields_are_excluded_and_duplicate_names_do_not_break_reads(self):
        props = self.docs["components"]["schemas"]["Contract"]["properties"]
        props["accessKey"] = _prop("string", "访问密钥")
        entity = self.entity("合同")
        fields = self.fields(entity)
        self.assertNotIn("访问密钥", fields)
        self.assertNotIn("应用密钥", fields)
        rows = deepcopy(CONTRACTS)
        rows[2][0]["name"] = rows[1][0]["name"]
        with patch("web_operation_agent_yudao.tests.test_goal.CONTRACTS", rows):
            result = self.tools.execute("query_entity_records", {
                "entity_id": entity, "field_ids": [fields["合同名称"]["field_id"]]})
        self.assertEqual(["11", "12"], [r["record_id"] for r in result.content["records"]])

    def test_incomplete_reads_and_long_values_never_become_truncated_answers(self):
        entity = self.entity("合同")
        name = self.fields(entity)["合同名称"]["field_id"]
        args = {"entity_id": entity, "field_ids": [name]}
        with patch.object(self.data, "rows", side_effect=DataRefused("incomplete")):
            with self.assertRaises(BusinessToolUnavailableError):
                self.tools.execute("query_entity_records", args)
        rows = deepcopy(CONTRACTS)
        rows[1][0]["name"] = "长" * 301
        with patch("web_operation_agent_yudao.tests.test_goal.CONTRACTS", rows):
            with self.assertRaises(BusinessToolContractError):
                self.tools.execute("query_entity_records", args)

    def test_composed_application_offers_only_scoped_read_tools(self):
        app = YudaoApplication("https://synthetic.invalid", None, self.docs,
                               client=self.client, ui=self.ui, conventions=None)
        bound = bind_application(app)
        self.assertEqual(set(self.tools.tool_effects()), set(bound.business_tools.tool_names))
        self.assertFalse(bound.business_tools.has_mutations)
        self.assertEqual(frozenset({"yudao.read"}), app.identity.required_scopes)


if __name__ == "__main__":
    unittest.main()
