"""The generic Odoo read path stays actor-scoped, opaque, bounded and read-only."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.business.runtime import (BusinessToolContractError,
                                                  BusinessToolRequestError)
from web_operation_agent_odoo.server.application import OdooApplicationAdapter
from web_operation_agent_odoo.server.descriptors import _catalog_digest
from web_operation_agent_odoo.server.generic_reads import (
    DESCRIBE_MODEL_TOOL,
    DISCOVER_MODELS_SCHEMA,
    DISCOVER_MODELS_TOOL,
    QUERY_RECORDS_SCHEMA,
    QUERY_RECORDS_TOOL,
    OdooGenericReadTools,
)


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


class _FakeGeneric(OdooGenericReadTools):
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
        executor = _FakeGeneric()
        result = executor.execute_generic(
            DISCOVER_MODELS_TOOL, {"query": "inventory transfers"})
        self.assertEqual(result.content["returned_count"], 1)
        candidate = result.content["candidates"][0]
        self.assertRegex(candidate["candidate_id"], r"^m_[0-9a-f]{32}$")
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
                denied = executor.execute_generic(
                    DISCOVER_MODELS_TOOL, {"query": denied_query})
                self.assertNotIn(
                    denied_label,
                    {item["label"] for item in denied.content["candidates"]},
                )

    def test_schema_has_no_raw_model_or_method_argument(self):
        discovery = DISCOVER_MODELS_SCHEMA["function"]["parameters"]["properties"]
        query = QUERY_RECORDS_SCHEMA["function"]["parameters"]["properties"]
        self.assertEqual(set(discovery), {"query", "limit"})
        self.assertEqual(
            set(query), {"candidate_id", "field_ids", "filters", "limit", "offset"})
        self.assertFalse({"model", "method", "domain", "order"} & set(query))

    def test_a_catalog_change_invalidates_an_old_candidate(self):
        executor = _FakeGeneric()
        discovered = executor.execute_generic(
            DISCOVER_MODELS_TOOL, {"query": "transfers"})
        candidate_id = discovered.content["candidates"][0]["candidate_id"]
        executor.catalog_payload["actions"][0]["name"] = "Operations"
        executor.catalog_payload["descriptor_digest"] = _catalog_digest(
            executor.catalog_payload["models"], executor.catalog_payload["actions"],
            executor.catalog_payload["omitted_models"],
            executor.catalog_payload["omitted_actions"])
        with self.assertRaisesRegex(BusinessToolRequestError, "absent or stale"):
            executor.execute_generic(DESCRIBE_MODEL_TOOL, {"candidate_id": candidate_id})


class GenericDescriptionAndQueryTests(unittest.TestCase):
    def setUp(self):
        self.executor = _FakeGeneric()
        discovery = self.executor.execute_generic(
            DISCOVER_MODELS_TOOL, {"query": "transfers"})
        self.candidate_id = discovery.content["candidates"][0]["candidate_id"]

    def _description(self, field_query=""):
        return self.executor.execute_generic(DESCRIBE_MODEL_TOOL, {
            "candidate_id": self.candidate_id,
            **({"field_query": field_query} if field_query else {}),
            "limit": 40,
        })

    def test_fields_get_is_filtered_and_bound_to_opaque_field_ids(self):
        description = self._description()
        fields = description.content["fields"]
        self.assertEqual(
            {item["label"] for item in fields},
            {"ID", "Reference", "Status", "Scheduled Date", "Carrier"})
        self.assertTrue(all(item["field_id"].startswith("f_") for item in fields))
        encoded = json.dumps(description.content)
        self.assertNotIn("access_token", encoded)
        self.assertNotIn("Document", encoded)
        self.assertNotIn("Moves", encoded)
        self.assertNotIn("Integration Secret", encoded)
        self.assertIn(("stock.picking", "fields_get", [], {"attributes": [
            "string", "type", "required", "readonly", "searchable", "store", "selection",
        ]}), self.executor.calls)

    def test_query_resolves_ids_server_side_and_reports_exact_scope_and_counts(self):
        description = self._description()
        by_label = {item["label"]: item["field_id"]
                    for item in description.content["fields"]}
        result = self.executor.execute_generic(QUERY_RECORDS_TOOL, {
            "candidate_id": self.candidate_id,
            "field_ids": [by_label["Reference"], by_label["Status"], by_label["Carrier"]],
            "filters": [{"field_id": by_label["Status"],
                         "operator": "=", "value": "done"}],
            "limit": 10,
        })
        self.assertEqual(result.content["records"], [{
            "record_id": 7,
            "values": [
                {"field_id": by_label["Reference"], "label": "Reference",
                 "value": "WH/OUT/00007"},
                {"field_id": by_label["Status"], "label": "Status", "value": "Done"},
                {"field_id": by_label["Carrier"], "label": "Carrier", "value": "FastShip"},
            ],
        }])
        self.assertEqual(result.content["source_total"], 3)
        self.assertEqual(result.content["completeness"], {
            "source_total_known": True, "matching_count_exact": True,
            "result_truncated": False, "returned_count": 1, "matched_count": 1,
        })
        self.assertEqual(result.query_answer.matched_count, 1)
        self.assertTrue(result.query_answer.complete)
        self.assertEqual(result.evidence[0].conditions, ("Status = 'done'",))
        search = [call for call in self.executor.calls if call[1] == "search_read"]
        self.assertEqual(search, [("stock.picking", "search_read",
                                   [[["state", "=", "done"]]], {
                                       "fields": ["name", "state", "carrier_id"],
                                       "limit": 10, "offset": 0, "order": "id asc",
                                   })])

    def test_raw_names_unknown_ids_and_invalid_operators_never_reach_a_data_read(self):
        before = len([call for call in self.executor.calls if call[1].startswith("search")])
        for args in (
            {"candidate_id": "stock.picking", "field_ids": ["name"]},
            {"candidate_id": self.candidate_id, "field_ids": ["f_" + "0" * 32]},
        ):
            with self.subTest(args=args):
                with self.assertRaises(BusinessToolRequestError):
                    self.executor.execute_generic(QUERY_RECORDS_TOOL, args)
        after = len([call for call in self.executor.calls if call[1].startswith("search")])
        self.assertEqual(before, after)

        description = self._description()
        reference = next(item["field_id"] for item in description.content["fields"]
                         if item["label"] == "Reference")
        with self.assertRaisesRegex(BusinessToolRequestError, "not allowed"):
            self.executor.execute_generic(QUERY_RECORDS_TOOL, {
                "candidate_id": self.candidate_id, "field_ids": [reference],
                "filters": [{"field_id": reference, "operator": "child_of", "value": "x"}],
            })

    def test_missing_or_malformed_requested_values_fail_closed(self):
        description = self._description()
        by_label = {item["label"]: item["field_id"]
                    for item in description.content["fields"]}
        original = self.executor.call

        for row, message in (
            ({"id": 7, "name": "WH/OUT/00007"}, "omitted"),
            ({"id": 7, "name": "WH/OUT/00007", "carrier_id": {"id": 4}}, "invalid"),
        ):
            with self.subTest(message=message):
                def bad_call(model, method, args, kwargs=None, *, returned=row):
                    if method == "search_read":
                        return [returned]
                    return original(model, method, args, kwargs)

                self.executor.call = bad_call
                with self.assertRaisesRegex(BusinessToolContractError, message):
                    self.executor.execute_generic(QUERY_RECORDS_TOOL, {
                        "candidate_id": self.candidate_id,
                        "field_ids": [by_label["Reference"], by_label["Carrier"]],
                    })
                self.executor.call = original


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


if __name__ == "__main__":
    unittest.main()
