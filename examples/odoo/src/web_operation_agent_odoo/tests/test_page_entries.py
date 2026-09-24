"""Actor action metadata does not bypass Odoo's reviewed navigation boundary."""
from __future__ import annotations

import copy
import unittest

from web_operation_agent.business.page_entries import PageEntryTools
from web_operation_agent_odoo.server.descriptors import _catalog_digest
from web_operation_agent_odoo.server.page_entries import OdooPageEntryPort

from .test_descriptors import _payload


class _Client:
    def __init__(self, payload):
        self.payload = payload

    def call(self, model, method, args, kwargs):
        assert (model, method, args, kwargs) == (
            "woa.runtime.descriptor", "catalog", [], {})
        return copy.deepcopy(self.payload)


def _digest(payload):
    payload["descriptor_digest"] = _catalog_digest(
        payload["models"], payload["actions"],
        payload["omitted_models"], payload["omitted_actions"])


class OdooPageEntryTests(unittest.TestCase):
    def _find(self, payload, query):
        return PageEntryTools(OdooPageEntryPort(_Client(payload)),
                              application_id="odoo").execute({"query": query})

    def test_reviewed_action_without_a_menu_can_be_discovered_and_opened(self):
        payload = _payload()
        payload["actions"][0]["breadcrumbs"] = []
        _digest(payload)
        result = self._find(payload, "Sales Orders")
        self.assertEqual(result.content["candidates"][0]["label"], "Sales Orders")
        self.assertEqual(result.content["candidates"][0]["ui_path"], "/odoo/orders")
        self.assertEqual(result.allowed_routes, ("/odoo/orders",))

    def test_current_record_dependency_has_no_standalone_route(self):
        payload = _payload()
        payload["actions"][0]["context"] = "{'active_id': active_id}"
        _digest(payload)
        result = self._find(payload, "Sales Orders")
        candidate = result.content["candidates"][0]
        self.assertFalse(candidate["openable"])
        self.assertEqual(candidate["unavailable_reason"], "requires_record_context")
        self.assertNotIn("ui_path", candidate)
        self.assertEqual(result.allowed_routes, ())

    def test_actor_visible_but_unreviewed_action_is_information_only(self):
        payload = _payload()
        payload["actions"].append({
            "action_id": 13, "xml_id": "custom.hidden_action", "name": "Hidden Orders",
            "route": "action-custom.hidden_action", "model": "sale.order",
            "readable": True, "view_modes": ["list"], "breadcrumbs": [],
            "domain": "[]", "context": "{}",
        })
        _digest(payload)
        result = self._find(payload, "Hidden Orders")
        candidate = result.content["candidates"][0]
        self.assertEqual(candidate["unavailable_reason"],
                         "not_reviewed_for_navigation")
        self.assertNotIn("ui_path", candidate)


if __name__ == "__main__":
    unittest.main()
