"""Directory discovery supplies vocabulary without searching every business table."""
from __future__ import annotations

import json
import time
import unittest
from dataclasses import replace

from web_operation_agent.business.entities import (
    EntityCatalog, EntityDescriptor, EntityReadTools,
)
from web_operation_agent.business.errors import (
    BusinessToolPermissionError, BusinessToolRequestError,
)
from web_operation_agent.state.identity import OperationContext, PrincipalContext


def context(actor):
    now = int(time.time() * 1_000)
    return OperationContext.create(PrincipalContext(
        schema_version=1, application_id="directory.fixture", tenant_id="tenant",
        workspace_id="workspace", actor_id=actor, delegation_id="delegation",
        issued_at=now - 1_000, expires_at=now + 60_000, scopes=("records:read",)),
        session_id="session", turn_id="turn", operation_id="directory",
        deadline_ms=now + 30_000, now_ms=now)


class CatalogPort:
    def __init__(self, labels):
        self.value = EntityCatalog("1" * 64, tuple(
            EntityDescriptor(f"private.resource.{i}", label, "private discovery terms")
            for i, label in enumerate(labels)), True, "fixture actor's readable types")
        self.calls = []

    def catalog(self, *, context=None):
        self.calls.append(("catalog", context))
        return self.value

    def fields(self, entity, *, context=None):
        raise AssertionError("directory must not load schemas")

    def query(self, request, *, context=None):
        raise AssertionError("directory must not scan records")


class EntityDirectoryTests(unittest.TestCase):
    def tools(self, labels):
        self.port = CatalogPort(labels)
        return EntityReadTools(self.port, application_id="directory.fixture")

    def test_thousand_types_are_reachable_without_native_names_fields_or_records(self):
        expected = [f"Type {i:04}" for i in range(1_000)]
        tools = self.tools(reversed(expected))
        seen, args = [], {}
        for _ in range(20):
            result = tools.execute("discover_entities", args, context=context("one"))
            page = result.content
            self.assertLessEqual(len(json.dumps(page["entity_labels"], ensure_ascii=False,
                                               separators=(",", ":"))), 4_000)
            self.assertNotIn("private.resource", result.render())
            self.assertNotIn("private discovery terms", result.render())
            self.assertFalse(result.allowed_routes or result.evidence)
            seen.extend(page["entity_labels"])
            if page["next_cursor"] is None:
                self.assertFalse(page["truncated_or_incomplete"])
                break
            self.assertTrue(page["truncated_or_incomplete"])
            args = {"cursor": page["next_cursor"]}
        self.assertEqual(seen, expected)
        self.assertTrue(all(call[0] == "catalog" for call in self.port.calls))

    def test_long_escaped_labels_are_bounded_without_losing_coverage(self):
        labels = [f"{i:03} " + '"\\' * 75 for i in range(90)]
        tools = self.tools(labels)
        result = tools.execute("discover_entities", {})
        self.assertLess(result.content["returned_count"], 200)
        self.assertEqual(result.content["distinct_label_count"], 90)
        self.assertIsNotNone(result.content["next_cursor"])
        self.assertLessEqual(len(json.dumps(result.content["entity_labels"],
            ensure_ascii=False, separators=(",", ":"))), 4_000)

    def test_duplicate_labels_do_not_select_or_collapse_their_entity_ids(self):
        tools = self.tools(["Customers", "Customers", "Invoices"])
        directory = tools.execute("discover_entities", {}).content
        self.assertEqual(directory["entity_labels"], ["Customers", "Invoices"])
        self.assertEqual(directory["visible_entity_count"], 3)
        found = tools.execute("discover_entities", {"query": "Customers"}).content
        self.assertEqual(len({c["entity_id"] for c in found["candidates"]}), 2)

    def test_cursor_rejects_a_different_actor_or_catalog_generation(self):
        tools = self.tools(["Customers", "Invoices"])
        cursor = tools.execute("discover_entities", {"limit": 1},
                               context=context("one")).content["next_cursor"]
        with self.assertRaisesRegex(BusinessToolRequestError, "absent or stale"):
            tools.execute("discover_entities", {"cursor": cursor}, context=context("two"))
        self.port.value = replace(self.port.value, binding_digest="2" * 64)
        with self.assertRaisesRegex(BusinessToolRequestError, "absent or stale"):
            tools.execute("discover_entities", {"cursor": cursor}, context=context("one"))

    def test_changed_labels_invalidate_cursor_even_if_adapter_digest_is_unchanged(self):
        tools = self.tools(["Customers", "Invoices"])
        cursor = tools.execute("discover_entities", {"limit": 1}).content["next_cursor"]
        self.port.value = replace(self.port.value, entities=(
            EntityDescriptor("private.new", "Accounting", "safe terms"),
            *self.port.value.entities))
        with self.assertRaisesRegex(BusinessToolRequestError, "absent or stale"):
            tools.execute("discover_entities", {"cursor": cursor})

    def test_incomplete_and_empty_catalogs_never_claim_record_absence(self):
        tools = self.tools([])
        self.port.value = replace(self.port.value, complete=False)
        result = tools.execute("discover_entities", {}).content
        self.assertEqual(result["entity_labels"], [])
        self.assertIsNone(result["next_cursor"])
        self.assertTrue(result["truncated_or_incomplete"])
        self.assertIn("cannot prove", result["note"])

    def test_permission_denial_is_not_replaced_by_a_broader_catalog(self):
        tools = self.tools(["Customers"])
        def denied(*, context=None):
            raise BusinessToolPermissionError("catalog denied")
        self.port.catalog = denied
        with self.assertRaises(BusinessToolPermissionError):
            tools.execute("discover_entities", {})

    def test_search_defaults_and_request_bounds_are_preserved(self):
        tools = self.tools([f"Work {i}" for i in range(20)])
        found = tools.execute("discover_entities", {"query": "Work"}).content
        self.assertEqual(found["returned_count"], 5)
        for args in ({"query": ""}, {"query": "Work", "limit": 11},
                     {"query": "Work", "cursor": "directory_" + "0" * 32},
                     {"cursor": "invented"}, {"limit": True}, {"limit": 201},
                     {"resource": "private.resource.1"}):
            with self.subTest(args=args), self.assertRaises(BusinessToolRequestError):
                tools.execute("discover_entities", args)


if __name__ == "__main__":
    unittest.main()
