"""Actor action metadata does not bypass Odoo's reviewed navigation boundary."""
from __future__ import annotations

import copy
import unittest

from web_operation_agent.business.page_entries import PageEntryTools
from web_operation_agent.business.errors import BusinessToolContractError
from web_operation_agent_odoo.server.descriptors import _catalog_digest
from web_operation_agent_odoo.server.page_entries import OdooPageEntryPort

from .test_descriptors import _payload


class _Client:
    def __init__(self, payload, arch=None):
        self.payload = payload
        self.arch = arch

    def call(self, model, method, args, kwargs):
        if (model, method, args, kwargs) == (
                "woa.runtime.descriptor", "catalog", [], {}):
            return copy.deepcopy(self.payload)
        if (model, method, args, kwargs) == (
                "sale.order", "get_view", [], {"view_type": "form"}):
            return {"arch": self.arch}
        if (model, method, args, kwargs) == (
                "sale.order", "fields_get", [], {"attributes": ["string", "type"]}):
            return {"invoice_payment_term_id": {"string": "Payment Terms", "type": "many2one"},
                    "secret_token": {"string": "Secret Token", "type": "char"},
                    "line_detail": {"string": "Line Detail", "type": "char"}}
        raise AssertionError((model, method, args, kwargs))


def _digest(payload):
    payload["descriptor_digest"] = _catalog_digest(
        payload["models"], payload["actions"],
        payload["omitted_models"], payload["omitted_actions"])


class OdooPageEntryTests(unittest.TestCase):
    def _find(self, payload, query, *, detail_query="", arch=None):
        return PageEntryTools(OdooPageEntryPort(_Client(payload, arch)),
                              application_id="odoo").execute({
                                  "query": query,
                                  **({"detail_query": detail_query} if detail_query else {})})

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

    def test_actor_view_and_field_metadata_yield_hints_not_click_authority(self):
        payload = _payload()
        arch = ('<form><sheet><notebook><page name="notes" string="Internal Notes">'
                '<field name="invoice_payment_term_id"/>'
                '<field name="secret_token"/>'
                '<field name="order_line"><form><field name="line_detail"/>'
                '</form></field>'
                '</page></notebook></sheet></form>')
        result = self._find(payload, "Sales Orders", detail_query="Internal Notes",
                            arch=arch)
        candidate = result.content["candidates"][0]
        self.assertEqual([(part["kind"], part["label"])
                          for part in candidate["part_matches"]],
                         [("tab", "Internal Notes")])
        self.assertNotIn("secret", str(candidate).lower())
        result = self._find(payload, "Sales Orders", detail_query="Payment Terms",
                            arch=arch)
        self.assertEqual(candidate["ui_path"], result.content["candidates"][0]["ui_path"])
        self.assertEqual(result.content["candidates"][0]["part_matches"][0]["label"],
                         "Payment Terms")
        nested = self._find(payload, "Sales Orders", detail_query="Line Detail",
                            arch=arch)
        self.assertEqual(nested.content["candidates"][0]["part_matches"], [])

    def test_unsafe_view_markup_is_not_parsed_as_page_metadata(self):
        with self.assertRaises(BusinessToolContractError):
            self._find(_payload(), "Sales Orders", detail_query="Internal Notes",
                       arch='<!DOCTYPE form [<!ENTITY x "internal">]><form/>')


if __name__ == "__main__":
    unittest.main()
