"""The generic Odoo read path stays actor-scoped, opaque, bounded and read-only."""
from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from web_operation_agent.business.runtime import (BusinessToolContractError,
                                                  BusinessToolPermissionError,
                                                  BusinessToolRequestError)
from web_operation_agent.business.entities import (
    DESCRIBE_ENTITY_TOOL,
    DISCOVER_ENTITIES_SCHEMA,
    DISCOVER_ENTITIES_TOOL,
    QUERY_ENTITY_RECORDS_SCHEMA,
    QUERY_ENTITY_RECORDS_TOOL,
    EntityReadTools,
    ReferenceLookupTools,
)
from web_operation_agent.tests.support.entity_read_contract import (
    assert_entity_read_contract,
)
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent_odoo.server.application import OdooApplicationAdapter
from web_operation_agent_odoo.server.descriptors import _catalog_digest
from web_operation_agent_odoo.server.generic_reads import (
    OdooEntityReadPort, OdooReferenceLookupPort)


def _catalog():
    models = [
        {"model": "stock.picking", "label": "Transfers",
         "operations": ["read", "write"], "transient": False},
        {"model": "stock.move", "label": "Stock Move",
         "operations": ["read"], "transient": False},
        {"model": "res.users", "label": "Users",
         "operations": ["read"], "transient": False},
        {"model": "stock.backorder.confirmation", "label": "Backorder Confirmation",
         "operations": ["read"], "transient": True},
    ]
    actions = [
        {"action_id": 10, "xml_id": "stock.action_picking_tree_all",
         "name": "Transfers", "route": "inventory/transfers", "model": "stock.picking",
         "readable": True, "view_modes": ["list", "form"],
         "breadcrumbs": ["Inventory / Operations / Transfers"],
         "domain": "[]", "context": "{}"},
        {"action_id": 11, "xml_id": "base.action_res_users",
         "name": "Users", "route": "settings/users", "model": "res.users",
         "readable": True, "view_modes": ["list", "form"],
         "breadcrumbs": ["Settings / Users"], "domain": "[]", "context": "{}"},
        {"action_id": 12, "xml_id": "stock.action_backorder",
         "name": "Backorders", "route": "inventory/backorders",
         "model": "stock.backorder.confirmation", "readable": True,
         "view_modes": ["form"], "breadcrumbs": ["Inventory / Backorders"],
         "domain": "[]", "context": "{}"},
    ]
    payload = {
        "schema_version": 1, "odoo_release": "18.0", "language": "en_US",
        "company_id": 2, "module_digest": "sha256:" + "1" * 64,
        "models": models, "actions": actions,
        "omitted_models": ["unusual.model"], "omitted_actions": [],
    }
    payload["descriptor_digest"] = _catalog_digest(
        models, actions, payload["omitted_models"], payload["omitted_actions"])
    return payload


def _fields():
    return {
        "id": {"string": "ID", "type": "integer", "required": False,
               "readonly": True, "searchable": True, "store": True},
        "name": {"string": "Reference", "type": "char", "required": True,
                 "readonly": False, "searchable": True, "store": True},
        "state": {"string": "Status", "type": "selection", "required": False,
                  "readonly": True, "searchable": True, "store": True,
                  "selection": [["draft", "Draft"], ["done", "Done"]]},
        "scheduled_date": {"string": "Scheduled Date", "type": "datetime",
                           "required": False, "readonly": False,
                           "searchable": True, "store": True},
        "carrier_id": {"string": "Carrier", "type": "many2one", "required": False,
                       "readonly": False, "searchable": True, "store": True},
        "move_ids": {"string": "Moves", "type": "one2many", "required": False,
                     "readonly": True, "searchable": True, "store": True},
        "document": {"string": "Document", "type": "binary", "required": False,
                     "readonly": True, "searchable": False, "store": True},
        "access_token": {"string": "Access Token", "type": "char", "required": False,
                         "readonly": True, "searchable": True, "store": True},
        "x_studio_value": {"string": "Integration Secret", "type": "char",
                           "required": False, "readonly": True,
                           "searchable": True, "store": True},
    }


class _FakeClient:
    def __init__(self):
        self.catalog_payload = _catalog()
        self.calls = []

    def call(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        self.calls.append((model, method, args, kwargs))
        if (model, method) == ("woa.runtime.descriptor", "catalog"):
            return copy.deepcopy(self.catalog_payload)
        if (model, method) == ("stock.picking", "fields_get"):
            return copy.deepcopy(_fields())
        if (model, method) == ("stock.picking", "search_count"):
            return 3 if args == [[]] else 1
        if (model, method) == ("stock.picking", "search_read"):
            return [{"id": 7, "name": "WH/OUT/00007", "state": "done",
                     "scheduled_date": "2026-09-21 08:00:00",
                     "carrier_id": [4, "FastShip"]}]
        raise AssertionError((model, method, args, kwargs))


class GenericDiscoveryTests(unittest.TestCase):
    def test_discovery_returns_only_action_backed_non_sensitive_non_transient_models(self):
        client = _FakeClient()
        executor = EntityReadTools(OdooEntityReadPort(client), application_id="odoo")
        result = executor.execute(
            DISCOVER_ENTITIES_TOOL, {"query": "inventory transfers"})
        self.assertEqual(result.content["returned_count"], 1)
        candidate = result.content["candidates"][0]
        self.assertRegex(candidate["entity_id"], r"^entity_[0-9a-f]{32}$")
        self.assertEqual(candidate["label"], "Transfers")
        self.assertEqual(candidate["assistant_operations"], ["read"])
        encoded = json.dumps(result.content)
        self.assertNotIn("stock.picking", encoded)
        self.assertNotIn("res.users", encoded)
        self.assertFalse(result.content["catalog_complete"])
        for denied_query, denied_label in (
            ("users", "Users"),
            ("backorders", "Backorder Confirmation"),
            ("stock move", "Stock Move"),
        ):
            with self.subTest(query=denied_query):
                denied = executor.execute(
                    DISCOVER_ENTITIES_TOOL, {"query": denied_query})
                self.assertNotIn(
                    denied_label,
                    {item["label"] for item in denied.content["candidates"]},
                )

    def test_schema_has_no_raw_model_or_method_argument(self):
        discovery = DISCOVER_ENTITIES_SCHEMA["function"]["parameters"]["properties"]
        query = QUERY_ENTITY_RECORDS_SCHEMA["function"]["parameters"]["properties"]
        self.assertEqual(set(discovery), {"query", "limit"})
        self.assertEqual(
            set(query), {"entity_id", "field_ids", "filters", "limit", "offset",
                         "aggregate_field_ids"})
        self.assertFalse({"model", "method", "domain", "order"} & set(query))

    def test_a_catalog_change_invalidates_an_old_candidate(self):
        client = _FakeClient()
        executor = EntityReadTools(OdooEntityReadPort(client), application_id="odoo")
        discovered = executor.execute(
            DISCOVER_ENTITIES_TOOL, {"query": "transfers"})
        entity_id = discovered.content["candidates"][0]["entity_id"]
        client.catalog_payload["actions"][0]["name"] = "Operations"
        client.catalog_payload["descriptor_digest"] = _catalog_digest(
            client.catalog_payload["models"], client.catalog_payload["actions"],
            client.catalog_payload["omitted_models"],
            client.catalog_payload["omitted_actions"])
        with self.assertRaisesRegex(BusinessToolRequestError, "absent or stale"):
            executor.execute(DESCRIBE_ENTITY_TOOL, {"entity_id": entity_id})


class OdooReferenceLookupTests(unittest.TestCase):
    def test_partial_name_search_runs_only_after_exact_search_misses(self):
        class Client:
            def __init__(self):
                self.operators = []

            def call(self, model, method, args, kwargs):
                self.operators.append(kwargs["operator"])
                return [] if kwargs["operator"] == "=" else [[11, "Gemini Furniture"]]

        client = Client()
        matches, truncated = OdooReferenceLookupPort(client).lookup(
            type("Entity", (), {"reference": "res.partner"})(), "Gemini", limit=5)
        self.assertEqual(client.operators, ["=", "ilike"])
        self.assertEqual(matches[0].record_id, "11")
        self.assertFalse(truncated)

    def test_actor_name_search_does_not_inherit_customer_rank_filter(self):
        client = _FakeClient()
        client.catalog_payload["models"].append({
            "model": "res.partner", "label": "Contact",
            "operations": ["read"], "transient": False})
        client.catalog_payload["actions"].append({
            "action_id": 13, "xml_id": "base.action_partner_form",
            "name": "Customers", "route": "customers", "model": "res.partner",
            "readable": True, "view_modes": ["list", "form"],
            "breadcrumbs": ["Sales / Customers"], "domain": "[]", "context": "{}"})
        client.catalog_payload["descriptor_digest"] = _catalog_digest(
            client.catalog_payload["models"], client.catalog_payload["actions"],
            client.catalog_payload["omitted_models"],
            client.catalog_payload["omitted_actions"])
        original = client.call

        def call(model, method, args, kwargs=None):
            if (model, method) == ("res.partner", "name_search"):
                client.calls.append((model, method, args, kwargs))
                return [[11, "Gemini Furniture"]]
            return original(model, method, args, kwargs)

        client.call = call
        tools = ReferenceLookupTools(
            OdooEntityReadPort(client), OdooReferenceLookupPort(client),
            application_id="odoo")
        result = tools.execute({
            "entity_query": "customers", "reference": "Gemini Furniture"})
        self.assertEqual(result.content["candidates"][0]["record_id"], "11")
        self.assertEqual(result.content["candidates"][0]["ui_path"],
                         "/odoo/res.partner/11")
        self.assertEqual([call for call in client.calls if call[1] == "name_search"], [
            ("res.partner", "name_search", ["Gemini Furniture"],
             {"operator": "=", "limit": 6}),
        ])


class GenericDescriptionAndQueryTests(unittest.TestCase):
    def setUp(self):
        self.client = _FakeClient()
        self.executor = EntityReadTools(
            OdooEntityReadPort(self.client), application_id="odoo")
        discovery = self.executor.execute(
            DISCOVER_ENTITIES_TOOL, {"query": "transfers"})
        self.entity_id = discovery.content["candidates"][0]["entity_id"]

    def _description(self, field_query=""):
        return self.executor.execute(DESCRIBE_ENTITY_TOOL, {
            "entity_id": self.entity_id,
            **({"field_query": field_query} if field_query else {}),
            "limit": 40,
        })

    def test_shared_application_neutral_contract(self):
        assert_entity_read_contract(
            self, self.executor,
            query="inventory transfers", entity_label="Transfers",
            field_label="Reference", filter_operator="contains",
            filter_value="WH/OUT", expected_record_id="7",
            expected_value="WH/OUT/00007",
        )

    def test_fields_get_is_filtered_and_bound_to_opaque_field_ids(self):
        description = self._description()
        fields = description.content["fields"]
        self.assertEqual(
            {item["label"] for item in fields},
            {"ID", "Reference", "Status", "Scheduled Date", "Carrier"})
        self.assertTrue(all(item["field_id"].startswith("field_") for item in fields))
        encoded = json.dumps(description.content)
        self.assertNotIn("access_token", encoded)
        self.assertNotIn("Document", encoded)
        self.assertNotIn("Moves", encoded)
        self.assertNotIn("Integration Secret", encoded)
        self.assertIn(("stock.picking", "fields_get", [], {"attributes": [
            "string", "type", "required", "readonly", "searchable", "store", "selection",
            "currency_field", "relation",
        ]}), self.client.calls)

    def test_relation_keeps_an_actor_scoped_target_binding_for_followup(self):
        original = self.client.call
        fields = _fields()
        fields["carrier_id"]["relation"] = "stock.picking"

        def call(model, method, args, kwargs=None):
            if method == "fields_get":
                return copy.deepcopy(fields)
            return original(model, method, args, kwargs)

        self.client.call = call
        description = self._description("Carrier")
        carrier = description.content["fields"][0]["field_id"]
        result = self.executor.execute(QUERY_ENTITY_RECORDS_TOOL, {
            "entity_id": self.entity_id, "field_ids": [carrier],
        })
        value = result.content["records"][0]["values"][0]
        self.assertEqual(value["value"], "FastShip")
        self.assertEqual(value["related_entity_id"], self.entity_id)
        self.assertEqual(value["related_record_id"], "4")
        self.assertNotIn("stock.picking", json.dumps(value))

    def test_monetary_value_carries_actor_read_currency_without_exposing_native_field(self):
        original = self.client.call
        fields = _fields()
        fields["amount_total"] = {
            "string": "Total", "type": "monetary", "required": False,
            "readonly": True, "searchable": True, "store": True,
            "currency_field": "currency_id",
        }
        fields["currency_id"] = {
            "string": "Currency", "type": "many2one", "required": False,
            "readonly": True, "searchable": True, "store": True,
        }

        def call(model, method, args, kwargs=None):
            if method == "fields_get":
                return copy.deepcopy(fields)
            if method == "search_read":
                self.assertEqual(kwargs["fields"], ["amount_total", "currency_id"])
                return [{"id": 7, "amount_total": 120.5,
                         "currency_id": [1, "USD"]}]
            return original(model, method, args, kwargs)

        self.client.call = call
        description = self._description("Total")
        total = description.content["fields"][0]["field_id"]
        result = self.executor.execute(QUERY_ENTITY_RECORDS_TOOL, {
            "entity_id": self.entity_id, "field_ids": [total],
        })
        self.assertEqual(result.content["records"][0]["values"], [{
            "field_id": total, "label": "Total", "value": "120.5", "unit": "USD",
        }])
        self.assertEqual(result.evidence[1].value_type, "money")
        self.assertEqual(result.evidence[1].unit, "USD")
        self.assertNotIn("currency_id", json.dumps(result.content))

        def missing_currency(model, method, args, kwargs=None):
            if method == "search_read":
                return [{"id": 7, "amount_total": 120.5}]
            return call(model, method, args, kwargs)

        self.client.call = missing_currency
        with self.assertRaisesRegex(BusinessToolContractError, "omitted.*currency"):
            self.executor.execute(QUERY_ENTITY_RECORDS_TOOL, {
                "entity_id": self.entity_id, "field_ids": [total],
            })

    def test_unstored_currency_cannot_authorize_a_monetary_sum(self):
        original = self.client.call
        fields = _fields()
        fields["debit_limit"] = {
            "string": "Debit Limit", "type": "monetary", "store": True,
            "currency_field": "currency_id",
        }
        fields["currency_id"] = {
            "string": "Currency", "type": "many2one", "store": False,
        }

        def call(model, method, args, kwargs=None):
            if method == "fields_get":
                return copy.deepcopy(fields)
            return original(model, method, args, kwargs)

        self.client.call = call
        field = self._description("Debit Limit").content["fields"][0]
        self.assertFalse(field["summable"])
        with self.assertRaisesRegex(BusinessToolRequestError, "summable"):
            self.executor.execute(QUERY_ENTITY_RECORDS_TOOL, {
                "entity_id": self.entity_id, "field_ids": [field["field_id"]],
                "aggregate_field_ids": [field["field_id"]],
            })
        self.assertFalse(any(method == "read_group" for _, method, _, _ in self.client.calls))

    def test_query_resolves_ids_server_side_and_reports_exact_scope_and_counts(self):
        description = self._description()
        by_label = {item["label"]: item["field_id"]
                    for item in description.content["fields"]}
        result = self.executor.execute(QUERY_ENTITY_RECORDS_TOOL, {
            "entity_id": self.entity_id,
            "field_ids": [by_label["Reference"], by_label["Status"], by_label["Carrier"]],
            "filters": [{"field_id": by_label["Status"],
                         "operator": "=", "value": "done"}],
            "limit": 10,
        })
        self.assertEqual(result.content["records"], [{
            "record_id": "7",
            "values": [
                {"field_id": by_label["Reference"], "label": "Reference",
                 "value": "WH/OUT/00007"},
                {"field_id": by_label["Status"], "label": "Status", "value": "Done"},
                {"field_id": by_label["Carrier"], "label": "Carrier", "value": "FastShip"},
            ],
            # Odoo 18 opens any record at /odoo/<model>/<id> for an actor who may read
            # it, so a record found generically can be opened without a guessed address.
            "ui_path": "/odoo/stock.picking/7",
        }])
        self.assertEqual(result.allowed_routes, ("/odoo/stock.picking/7",))
        self.assertEqual(result.content["source_total"], 3)
        self.assertEqual(result.content["completeness"], {
            "source_total_known": True, "matching_count_exact": True,
            "result_truncated": False, "returned_count": 1, "matched_count": 1,
        })
        self.assertEqual(result.query_answer.matched_count, 1)
        self.assertTrue(result.query_answer.complete)
        self.assertEqual(result.evidence[0].conditions, ("Status = 'done'",))
        search = [call for call in self.client.calls if call[1] == "search_read"]
        self.assertEqual(search, [("stock.picking", "search_read",
                                   [[["state", "=", "done"]]], {
                                       "fields": ["name", "state", "carrier_id"],
                                       "limit": 10, "offset": 0, "order": "id asc",
                                   })])

    def test_aggregate_uses_application_sum_with_currency_and_full_filter_scope(self):
        original = self.client.call
        fields = _fields()
        fields["amount_due"] = {
            "string": "Amount Due", "type": "monetary", "required": False,
            "readonly": True, "searchable": True, "store": True,
            "currency_field": "currency_id",
        }
        fields["currency_id"] = {
            "string": "Currency", "type": "many2one", "required": False,
            "readonly": True, "searchable": True, "store": True,
        }

        def call(model, method, args, kwargs=None):
            if method == "fields_get":
                return copy.deepcopy(fields)
            if method == "search_count":
                return 100 if args == [[]] else 3
            if method == "search_read":
                return [{"id": 7, "name": "WH/OUT/00007"}]
            if method == "read_group":
                self.assertEqual(args, [
                    [["state", "=", "done"]], ["amount_due:sum"], ["currency_id"],
                ])
                self.assertEqual(kwargs, {"lazy": False})
                return [
                    {"currency_id": [1, "USD"], "amount_due": 90.25, "__count": 2},
                    {"currency_id": [2, "EUR"], "amount_due": 7.5, "__count": 1},
                ]
            return original(model, method, args, kwargs)

        self.client.call = call
        by_label = {item["label"]: item for item in self._description().content["fields"]}
        self.assertTrue(by_label["Amount Due"]["summable"])
        result = self.executor.execute(QUERY_ENTITY_RECORDS_TOOL, {
            "entity_id": self.entity_id,
            "field_ids": [by_label["Reference"]["field_id"]],
            "filters": [{"field_id": by_label["Status"]["field_id"],
                         "operator": "=", "value": "done"}],
            "aggregate_field_ids": [by_label["Amount Due"]["field_id"]],
            "limit": 1,
        })
        self.assertEqual([(item["sum"], item["unit"], item["count"])
                          for item in result.content["aggregates"]],
                         [("90.25", "USD", 2), ("7.5", "EUR", 1)])
        self.assertEqual(result.content["completeness"]["matched_count"], 3)
        self.assertNotIn("currency_id", json.dumps(result.content))

        def missing_unit(model, method, args, kwargs=None):
            if method == "read_group":
                return [{"currency_id": False, "amount_due": 97.75, "__count": 3}]
            return call(model, method, args, kwargs)

        self.client.call = missing_unit
        with self.assertRaisesRegex(BusinessToolContractError, "without its currency"):
            self.executor.execute(QUERY_ENTITY_RECORDS_TOOL, {
                "entity_id": self.entity_id,
                "field_ids": [by_label["Reference"]["field_id"]],
                "aggregate_field_ids": [by_label["Amount Due"]["field_id"]],
            })

    def test_raw_names_unknown_ids_and_invalid_operators_never_reach_a_data_read(self):
        before = len([call for call in self.client.calls if call[1].startswith("search")])
        for args in (
            {"entity_id": "stock.picking", "field_ids": ["name"]},
            {"entity_id": self.entity_id, "field_ids": ["field_" + "0" * 32]},
        ):
            with self.subTest(args=args):
                with self.assertRaises(BusinessToolRequestError):
                    self.executor.execute(QUERY_ENTITY_RECORDS_TOOL, args)
        after = len([call for call in self.client.calls if call[1].startswith("search")])
        self.assertEqual(before, after)

        description = self._description()
        reference = next(item["field_id"] for item in description.content["fields"]
                         if item["label"] == "Reference")
        with self.assertRaisesRegex(BusinessToolRequestError, "not allowed"):
            self.executor.execute(QUERY_ENTITY_RECORDS_TOOL, {
                "entity_id": self.entity_id, "field_ids": [reference],
                "filters": [{"field_id": reference, "operator": "child_of", "value": "x"}],
            })

    def test_missing_or_malformed_requested_values_fail_closed(self):
        description = self._description()
        by_label = {item["label"]: item["field_id"]
                    for item in description.content["fields"]}
        original = self.client.call

        for row, message in (
            ({"id": 7, "name": "WH/OUT/00007"}, "omitted"),
            ({"id": 7, "name": "WH/OUT/00007", "carrier_id": {"id": 4}}, "invalid"),
        ):
            with self.subTest(message=message):
                def bad_call(model, method, args, kwargs=None, *, returned=row):
                    if method == "search_read":
                        return [returned]
                    return original(model, method, args, kwargs)

                self.client.call = bad_call
                with self.assertRaisesRegex(BusinessToolContractError, message):
                    self.executor.execute(QUERY_ENTITY_RECORDS_TOOL, {
                        "entity_id": self.entity_id,
                        "field_ids": [by_label["Reference"], by_label["Carrier"]],
                    })
                self.client.call = original


class RelatedRecordLocalizationTests(unittest.TestCase):
    """A document number resolves its owner through two actor-visible reads."""

    def test_order_identifier_then_customer_id_yields_only_verified_customer_route(self):
        class Client(_FakeClient):
            def __init__(self, *, customer_visible):
                super().__init__()
                self.customer_visible = customer_visible
                models = [
                    {"model": model, "label": label, "operations": ["read"],
                     "transient": False}
                    for model, label in (("sale.order", "Sales Orders"),
                                         ("res.partner", "Customers"))
                ]
                actions = [
                    {"action_id": index, "xml_id": f"test.action_{index}",
                     "name": label, "route": route, "model": model,
                     "readable": True, "view_modes": ["list", "form"],
                     "breadcrumbs": [label], "domain": "[]", "context": "{}"}
                    for index, (model, label, route) in enumerate((
                        ("sale.order", "Sales Orders", "orders"),
                        ("res.partner", "Customers", "customers")), start=1)
                ]
                self.catalog_payload["models"] = models
                self.catalog_payload["actions"] = actions
                self.catalog_payload["omitted_models"] = []
                self.catalog_payload["descriptor_digest"] = _catalog_digest(
                    models, actions, [], [])

            def call(self, model, method, args, kwargs=None):
                if (model, method) == ("woa.runtime.descriptor", "catalog"):
                    return copy.deepcopy(self.catalog_payload)
                if method == "fields_get":
                    fields = {
                        "id": {"string": "ID", "type": "integer", "store": True},
                        "name": {"string": "Reference" if model == "sale.order"
                                 else "Name", "type": "char", "store": True},
                    }
                    if model == "sale.order":
                        fields["partner_id"] = {
                            "string": "Customer", "type": "many2one", "store": True,
                            "relation": "res.partner",
                        }
                    return fields
                if method == "search_count":
                    return (0 if model == "res.partner" and not self.customer_visible
                            and args != [[]] else 1)
                if method == "search_read":
                    if model == "res.partner":
                        return ([{"id": 4, "name": "Lumber Inc"}]
                                if self.customer_visible else [])
                    return [{"id": 7, "name": "S00006",
                             "partner_id": [4, "Lumber Inc"]}]
                raise AssertionError((model, method, args, kwargs))

        for visible in (True, False):
            with self.subTest(customer_visible=visible):
                tools = EntityReadTools(OdooEntityReadPort(Client(
                    customer_visible=visible)), application_id="odoo")
                order_id = tools.execute(DISCOVER_ENTITIES_TOOL, {
                    "query": "Sales Orders"}).content["candidates"][0]["entity_id"]
                order_fields = {item["label"]: item["field_id"] for item in
                                tools.execute(DESCRIBE_ENTITY_TOOL, {
                                    "entity_id": order_id}).content["fields"]}
                order = tools.execute(QUERY_ENTITY_RECORDS_TOOL, {
                    "entity_id": order_id,
                    "field_ids": [order_fields["Reference"], order_fields["Customer"]],
                    "filters": [{"field_id": order_fields["Reference"],
                                 "operator": "=", "value": "S00006"}],
                })
                self.assertEqual(order.content["completeness"]["matched_count"], 1)
                relation = order.content["records"][0]["values"][1]
                self.assertEqual(relation["value"], "Lumber Inc")
                self.assertEqual(relation["related_record_id"], "4")
                customer_id = relation["related_entity_id"]
                customer_fields = {item["label"]: item["field_id"] for item in
                                   tools.execute(DESCRIBE_ENTITY_TOOL, {
                                       "entity_id": customer_id}).content["fields"]}
                customer = tools.execute(QUERY_ENTITY_RECORDS_TOOL, {
                    "entity_id": customer_id,
                    "field_ids": [customer_fields["Name"]],
                    "filters": [{"field_id": customer_fields["ID"],
                                 "operator": "=", "value": 4}],
                })
                self.assertEqual(customer.content["completeness"]["matched_count"],
                                 1 if visible else 0)
                self.assertEqual(customer.allowed_routes,
                                 ("/odoo/res.partner/4",) if visible else ())


class GenericSkillRoutingTests(unittest.TestCase):
    @staticmethod
    def _key_file(root: Path) -> Path:
        path = root / "odoo-key.json"
        path.write_text(json.dumps({"database": "woa", "login": "reader@example.com",
                                    "api_key": "x" * 40}))
        return path

    def test_generic_is_a_fallback_but_does_not_displace_a_dedicated_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url="http://127.0.0.1:8069",
                api_key_path=self._key_file(Path(tmp)))
        generic = adapter.business_tools.select(
            "show inventory transfers", allow_mutations=False)
        self.assertTrue(generic.fallback_used)
        self.assertEqual(generic.candidate_ids[0], "odoo.runtime.records-query")

        dedicated = adapter.business_tools.select("show products", allow_mutations=False)
        self.assertFalse(dedicated.fallback_used)
        self.assertEqual(dedicated.candidate_ids, ("odoo.sales.products-query",))

    def test_runtime_refuses_missing_scope_before_odoo_rpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url="http://127.0.0.1:8069",
                api_key_path=self._key_file(Path(tmp)))
        now = int(time.time() * 1_000)
        principal = PrincipalContext(
            schema_version=1, application_id="odoo", tenant_id="db",
            workspace_id="company", actor_id="reader", delegation_id="delegation",
            issued_at=now - 1_000, expires_at=now + 60_000,
            scopes=("customers:read",),
        )
        context = OperationContext.create(
            principal, session_id="session", turn_id="turn",
            operation_id="discover", deadline_ms=now + 30_000, now_ms=now,
        )
        with self.assertRaises(BusinessToolPermissionError):
            adapter.business_tools.execute(
                "discover_entities", {"query": "transfers"},
                {"odoo.runtime.records-query"}, context=context)


if __name__ == "__main__":
    unittest.main()
