"""The Odoo web-client route grammar, and the session parameters it declares."""
import unittest

from web_operation_agent.privacy import (SESSION_HANDOFF_QUERY_KEYS, SessionHandoffPolicy)

from web_operation_agent_odoo.route import (ODOO_ROUTE_PORT, ODOO_SESSION_HANDOFF_KEYS,
                                            ODOO_SOURCE_SCOPE, odoo_route_identity)


class RouteGrammar(unittest.TestCase):
    def test_it_accepts_the_three_shapes_odoo_actually_serves(self):
        for route in ("/odoo/sales", "/odoo/orders/7", "/odoo/customers/44",
                      "/odoo/customers/new",
                      "/odoo/res.partner/11", "/odoo/product.template/2",
                      "/odoo/action-uom.product_uom_categ_form_action"):
            with self.subTest(route=route):
                self.assertEqual(ODOO_ROUTE_PORT.validate_tool_route(route), route)

    def test_it_refuses_anything_outside_the_web_client(self):
        for route in ("/web/login", "/odoo", "/odoo/sales/0", "/odoo/Sales",
                      "//evil.test/odoo/sales", "/odoo/sales/../secret",
                      "/jsonrpc", "/odoo/sales/7/8/9",
                      "/odoo/action-uom.product_uom_categ_form_action/../secret"):
            with self.subTest(route=route):
                with self.assertRaises(ValueError):
                    ODOO_ROUTE_PORT.validate_tool_route(route)

    def test_the_contract_identity_and_its_inverse_agree(self):
        self.assertEqual(odoo_route_identity("http://app:8069/odoo/sales"),
                         (ODOO_SOURCE_SCOPE, "sales"))
        self.assertEqual(odoo_route_identity("http://app:8069/odoo/orders/7"),
                         (ODOO_SOURCE_SCOPE, "orders"))
        self.assertEqual(
            odoo_route_identity("http://app:8069/odoo/action-sale.action_orders_to_invoice"),
            (ODOO_SOURCE_SCOPE, "action-sale.action_orders_to_invoice"))
        self.assertEqual(
            ODOO_ROUTE_PORT.compose_contract_route(ODOO_SOURCE_SCOPE, "sales"),
            "/odoo/sales")
        self.assertEqual(odoo_route_identity("http://app:8069/web/login"), ("", ""))

    def test_a_route_carrying_a_session_parameter_is_refused(self):
        for key in ODOO_SESSION_HANDOFF_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    ODOO_ROUTE_PORT.validate_tool_route(f"/odoo/sales?{key}=secret")


class DeclaredSessionVocabulary(unittest.TestCase):
    """F11's residual: an application's own session parameters must reach redaction.

    Odoo 18 keeps its session in an HttpOnly cookie and accepts no URL carrier, so the
    concrete leak the review named is closed upstream. The declaration still has to exist
    and still has to be consumed: older Odoo releases did put `session_id` in URLs, portal
    links still carry `access_token`, and the mechanism -- not this release's luck -- is what
    the next application depends on.
    """

    def test_odoo_declares_session_parameters_the_static_baseline_does_not_know(self):
        self.assertTrue(ODOO_SESSION_HANDOFF_KEYS)
        self.assertEqual(ODOO_ROUTE_PORT.session_handoff_query_keys,
                         frozenset(key.casefold() for key in ODOO_SESSION_HANDOFF_KEYS))
        self.assertFalse(ODOO_SESSION_HANDOFF_KEYS & SESSION_HANDOFF_QUERY_KEYS)

    def test_the_widened_policy_redacts_a_parameter_the_baseline_misses(self):
        from web_operation_agent.privacy import (has_unredacted_session_handoff,
                                                 redact_session_handoffs)
        carrier = "/web?session_id=ODOO_SESSION_SECRET&db=woa"
        # Reverse verification: the static baseline lets it straight through.
        self.assertFalse(has_unredacted_session_handoff(carrier, url_decode_depth=3))
        self.assertIn("ODOO_SESSION_SECRET", redact_session_handoffs(carrier))

        policy = SessionHandoffPolicy.extending(
            ODOO_ROUTE_PORT.session_handoff_query_keys)
        self.assertTrue(policy.has_unredacted(carrier, url_decode_depth=3))
        cleaned = policy.redact(carrier)
        self.assertNotIn("ODOO_SESSION_SECRET", cleaned)
        self.assertIn("db=woa", cleaned)


if __name__ == "__main__":
    unittest.main()
