"""One origin rule, and the places that used to have four of their own (P7.5 D-13)."""
from __future__ import annotations

import unittest

from web_operation_agent.application import APPLICATION_ADAPTER_VERSION, ApplicationDescriptor
from web_operation_agent.origins import (MAX_ORIGIN_LENGTH, OriginError, normalize_origin,
                                         origin_of)
from web_operation_agent.schemas import ClientHello, SchemaError


class AnOriginMeansTheSameThingEverywhere(unittest.TestCase):
    def test_the_same_place_written_differently_canonicalises_to_one_string(self):
        for written in ("https://App.Example", "https://app.example:443", "https://app.example/"):
            with self.subTest(written=written):
                self.assertEqual(normalize_origin(written), "https://app.example")
        self.assertEqual(normalize_origin("HTTPS://APP.EXAMPLE:8443"), "https://app.example:8443")
        self.assertEqual(normalize_origin("http://localhost:80"), "http://localhost")

    def test_an_ipv6_literal_keeps_its_brackets(self):
        self.assertEqual(normalize_origin("https://[2001:DB8::1]:8443"),
                         "https://[2001:db8::1]:8443")

    def test_an_origin_is_only_an_origin(self):
        for value in ("https://app.example/path", "https://app.example/?q=1",
                      "https://app.example/#f"):
            with self.subTest(value=value), self.assertRaises(OriginError):
                normalize_origin(value)

    def test_a_url_is_reduced_to_the_origin_it_belongs_to(self):
        self.assertEqual(origin_of("https://App.Example:443/orders?q=1#top"),
                         "https://app.example")
        self.assertEqual(origin_of("http://127.0.0.1:8069/odoo/customers/10"),
                         "http://127.0.0.1:8069")

    def test_what_is_refused_says_why(self):
        cases = {
            "https://u:p@app.example": "user information",
            "file:///x": "absolute http",
            "app.example": "absolute http",
            "": "bounded",
            "https://app.example:99999": "port is invalid",
            "https://" + "a" * MAX_ORIGIN_LENGTH: "bounded",
        }
        for value, reason in cases.items():
            with self.subTest(value=value[:40]), self.assertRaisesRegex(OriginError, reason):
                normalize_origin(value)
        with self.assertRaisesRegex(OriginError, "must be a string"):
            normalize_origin(None)

    def test_plain_http_is_the_developer_s_own_machine_or_nothing(self):
        self.assertEqual(normalize_origin("http://127.0.0.1:8069",
                                          require_secure_unless_loopback=True),
                         "http://127.0.0.1:8069")
        with self.assertRaisesRegex(OriginError, "loopback"):
            normalize_origin("http://app.example", require_secure_unless_loopback=True)
        # Only the application's own descriptor asks for that rule; a page may be served
        # over plain HTTP behind a proxy that terminates TLS.
        self.assertEqual(normalize_origin("http://app.example"), "http://app.example")


class TheModulesThatUsedToDisagree(unittest.TestCase):
    """Each of these once had its own rule; they now answer the same for the same input."""

    def descriptor(self, origin: str, module: str) -> ApplicationDescriptor:
        return ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION, application_id="app",
            display_name="App", origin=origin,
            navigation_mode="document", page_controller_module=module,
            catalog_sha256="0" * 64)

    def test_the_application_descriptor_and_the_wire_schema_agree(self):
        descriptor = self.descriptor("https://App.Example:443",
                                     "https://app.example/page-controller.js")
        hello = ClientHello(
            schema_version=1, session_id="s", client_id="c", runtime_instance_id="d",
            runtime="woa", capabilities=("observe",), page_origin="https://App.Example:443")
        self.assertEqual(descriptor.origin, "https://app.example")
        self.assertEqual(hello.page_origin, descriptor.origin)

    def test_the_wire_schema_refuses_what_the_shared_rule_refuses(self):
        for value in ("https://app.example/path", "https://u:p@app.example", "file:///x"):
            with self.subTest(value=value), self.assertRaises(SchemaError):
                ClientHello(schema_version=1, session_id="s", client_id="c",
                            runtime_instance_id="d", runtime="woa",
                            capabilities=("observe",), page_origin=value)

    def test_a_session_accepts_a_page_whose_origin_its_deployment_wrote_differently(self):
        """The deployment wrote the origin one way; the browser sends the canonical one."""
        from web_operation_agent.transport import RemoteSessionRegistry

        registry = RemoteSessionRegistry()
        grant, channel = registry.create(allowed_origins=["https://App.Example:443"],
                                         session_id="origin-shapes")
        self.assertEqual(grant.allowed_origins, ("https://app.example",))
        self.assertTrue(registry.origin_allowed(grant.session_id, "https://app.example"))
        channel.accept_hello(ClientHello(
            schema_version=1, session_id=grant.session_id, client_id="c",
            runtime_instance_id="d", runtime="woa", capabilities=("observe",),
            page_origin="https://app.example"))


if __name__ == "__main__":
    unittest.main()
