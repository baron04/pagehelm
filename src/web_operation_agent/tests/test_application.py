from __future__ import annotations

import hashlib
import json
import unittest

from web_operation_agent.application import (APPLICATION_ADAPTER_VERSION,
                                              ApplicationDescriptor,
                                              bind_application)
from web_operation_agent.business import (BusinessSkill, BusinessToolResult,
                                          BusinessToolRuntime)


_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_invoices",
        "description": "Read invoices from the delegated application account.",
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False},
    },
}


class _InvoiceExecutor:
    def tool_schemas(self):
        return (_TOOL_SCHEMA,)

    def tool_effects(self):
        return {"query_invoices": "read"}

    def execute(self, name, args):
        return BusinessToolResult(
            content={"invoices": []},
            audit={"operation": name, "count": 0},
        )

    def prepare_mutation(self, name, args):
        raise AssertionError("read-only adapter")

    def execute_mutation(self, name, args):
        raise AssertionError("read-only adapter")


def _skill() -> BusinessSkill:
    raw = json.dumps({
        "schema_version": 1,
        "skill_id": "ledger.invoice-query",
        "title": "Invoice lookup",
        "intent": "Find current invoices and their payment state.",
        "triggers": ["invoice", "invoices", "发票"],
        "instructions": ["Load current invoice data through the typed read tool."],
        "tool_names": ["query_invoices"],
        "provenance": ["mock://ledger/public-api"],
        "read_only": True,
    }, sort_keys=True).encode()
    return BusinessSkill.from_bytes(raw, artifact_uri="package://mock/invoice.json")


class _InvoiceAdapter:
    def __init__(self):
        self.business_tools = BusinessToolRuntime((_skill(),), _InvoiceExecutor())
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id="ledger-demo",
            display_name="Ledger Demo",
            origin="https://ledger.example",
            navigation_mode="document",
            page_controller_module="https://ledger.example/assets/page-controller.js",
            catalog_sha256=self.business_tools.catalog_sha256,
        )

    def health(self):
        return {"passed": True, "http_status": 200}


class ApplicationAdapterTests(unittest.TestCase):
    def test_second_adapter_exposes_an_unseen_goal_without_core_routing(self):
        bound = bind_application(_InvoiceAdapter())
        relevant = bound.business_tools.relevant("Show all overdue invoices")

        self.assertEqual(bound.descriptor.application_id, "ledger-demo")
        self.assertEqual([skill.skill_id for skill in relevant],
                         ["ledger.invoice-query"])
        self.assertEqual(bound.catalog["schema_version"], 1)
        self.assertEqual(bound.catalog["tools"][0]["name"], "query_invoices")
        self.assertEqual(bound.catalog["tools"][0]["effect"], "read")

    def test_binding_rejects_a_stale_catalog_descriptor(self):
        adapter = _InvoiceAdapter()
        adapter.descriptor = ApplicationDescriptor(
            **{**adapter.descriptor.to_json(),
               "catalog_sha256": hashlib.sha256(b"stale").hexdigest()})
        with self.assertRaisesRegex(ValueError, "stale"):
            bind_application(adapter)

    def test_descriptor_rejects_cross_origin_page_controller(self):
        adapter = _InvoiceAdapter()
        with self.assertRaisesRegex(ValueError, "same-origin"):
            ApplicationDescriptor(
                **{**adapter.descriptor.to_json(),
                   "page_controller_module": "https://cdn.example/controller.js"})


if __name__ == "__main__":
    unittest.main()
