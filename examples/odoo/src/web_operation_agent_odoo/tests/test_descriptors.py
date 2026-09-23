"""The runtime descriptor boundary rejects anything it cannot safely index."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest

from web_operation_agent.business.runtime import BusinessToolContractError
from web_operation_agent_odoo.server.descriptors import (
    OdooSourceManifest,
    OdooRuntimeCatalog,
    OdooRuntimeDescriptorReader,
    _catalog_digest,
)


def _payload():
    models = [
        {"model": "res.partner", "label": "Contact",
         "operations": ["create", "read", "write"], "transient": False},
        {"model": "sale.order", "label": "Sales Order",
         "operations": ["read"], "transient": False},
    ]
    actions = [{
        "action_id": 12,
        "xml_id": "sale.action_orders",
        "name": "Sales Orders",
        "route": "orders",
        "model": "sale.order",
        "readable": True,
        "view_modes": ["list", "form"],
        "breadcrumbs": ["Sales / Orders"],
        "domain": "[('state', 'not in', ('draft', 'sent'))]",
        "context": "{}",
    }]
    return {
        "schema_version": 1,
        "odoo_release": "18.0",
        "language": "en_US",
        "company_id": 2,
        "module_digest": "sha256:" + "1" * 64,
        "descriptor_digest": _catalog_digest(models, actions, [], []),
        "models": models,
        "actions": actions,
        "omitted_models": [],
        "omitted_actions": [],
    }


class RuntimeDescriptorCatalogTests(unittest.TestCase):
    def test_a_valid_catalog_becomes_immutable_typed_descriptors(self):
        catalog = OdooRuntimeCatalog.from_payload(_payload())
        self.assertEqual([item.model for item in catalog.models],
                         ["res.partner", "sale.order"])
        self.assertEqual(catalog.actions[0].breadcrumbs, ("Sales / Orders",))
        self.assertEqual(catalog.actions[0].view_modes, ("list", "form"))
        self.assertTrue(catalog.complete)

    def test_a_readable_action_without_a_menu_breadcrumb_is_accepted(self):
        payload = _payload()
        payload["actions"][0]["breadcrumbs"] = []
        payload["descriptor_digest"] = _catalog_digest(
            payload["models"], payload["actions"], [], [])
        catalog = OdooRuntimeCatalog.from_payload(payload)
        self.assertEqual(catalog.actions[0].breadcrumbs, ())
        self.assertTrue(catalog.actions[0].readable)

    def test_the_digest_covers_the_exact_models_and_actions(self):
        payload = _payload()
        payload["actions"][0]["route"] = "quotations"
        with self.assertRaisesRegex(BusinessToolContractError, "digest did not verify"):
            OdooRuntimeCatalog.from_payload(payload)

    def test_duplicates_and_false_readability_are_rejected(self):
        duplicate = _payload()
        duplicate["models"].append(copy.deepcopy(duplicate["models"][0]))
        duplicate["descriptor_digest"] = _catalog_digest(
            duplicate["models"], duplicate["actions"], [], [])
        with self.assertRaisesRegex(BusinessToolContractError, "repeats a model"):
            OdooRuntimeCatalog.from_payload(duplicate)

        unknown = _payload()
        unknown["actions"][0]["model"] = "hidden.model"
        unknown["descriptor_digest"] = _catalog_digest(
            unknown["models"], unknown["actions"], [], [])
        with self.assertRaisesRegex(BusinessToolContractError, "unknown action model"):
            OdooRuntimeCatalog.from_payload(unknown)

    def test_unsafe_routes_and_unsorted_operations_are_rejected(self):
        for mutate in (
                lambda value: value["actions"][0].update(route="../admin"),
                lambda value: value["models"][0].update(
                    operations=["read", "create", "write"]),
        ):
            with self.subTest(mutate=mutate):
                payload = _payload()
                mutate(payload)
                payload["descriptor_digest"] = _catalog_digest(
                    payload["models"], payload["actions"], [], [])
                with self.assertRaises(BusinessToolContractError):
                    OdooRuntimeCatalog.from_payload(payload)

    def test_omissions_are_explicit_and_make_the_catalog_incomplete(self):
        payload = _payload()
        payload["omitted_models"] = ["unusual.model"]
        payload["omitted_actions"] = [99]
        payload["descriptor_digest"] = _catalog_digest(
            payload["models"], payload["actions"],
            payload["omitted_models"], payload["omitted_actions"])
        catalog = OdooRuntimeCatalog.from_payload(payload)
        self.assertFalse(catalog.complete)
        self.assertEqual(catalog.omitted_models, ("unusual.model",))


class _Executor:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def call(self, model, method, args, kwargs):
        self.calls.append((model, method, args, kwargs))
        return self.payload


class RuntimeDescriptorReaderTests(unittest.TestCase):
    def test_the_reader_uses_only_the_installed_metadata_method(self):
        executor = _Executor(_payload())
        catalog = OdooRuntimeDescriptorReader(executor).catalog()
        self.assertEqual(catalog.company_id, 2)
        self.assertEqual(executor.calls, [
            ("woa.runtime.descriptor", "catalog", [], {}),
        ])

    def test_source_manifest_is_canonical_and_digest_verified(self):
        modules = [{"name": "base", "version": "18.0.1.0"},
                   {"name": "sale", "version": "18.0.1.2"}]
        encoded = json.dumps([(row["name"], row["version"]) for row in modules],
                             ensure_ascii=False, separators=(",", ":")).encode()
        payload = {"schema_version": 1, "odoo_release": "18.0",
                   "module_digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
                   "modules": modules}
        manifest = OdooSourceManifest.from_payload(payload)
        self.assertEqual([item.name for item in manifest.modules], ["base", "sale"])
        payload["modules"][1]["version"] = "changed"
        with self.assertRaisesRegex(BusinessToolContractError, "digest did not verify"):
            OdooSourceManifest.from_payload(payload)


if __name__ == "__main__":
    unittest.main()
