from __future__ import annotations

import hashlib
import json
import unittest

from web_operation_agent.application import (APPLICATION_ADAPTER_VERSION,
                                              AnonymousIdentityPort,
                                              ApplicationDescriptor,
                                              ApplicationPorts,
                                              ConservativeMutationReconciler,
                                              DeclaredDataClassifier,
                                              DeclaredRoutePort,
                                              ResultPostconditionVerifier,
                                              SourceCapability,
                                              bind_application)
from web_operation_agent.business import (BusinessSkill, BusinessToolContractError,
                                          BusinessToolResult, BusinessToolRuntime)


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
            content={"invoices": [{"id": "invoice-1", "ui_path":
                                    "/invoices/invoice-1"}]},
            audit={"operation": name, "count": 0},
            allowed_routes=("/invoices/invoice-1",),
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
    def __init__(self, executor=None):
        data = DeclaredDataClassifier((
            ("query_invoices", ("business.invoice",)),
        ))
        postconditions = ResultPostconditionVerifier(())
        routes = DeclaredRoutePort(
            "ledger-demo", (r"/invoices/[a-z0-9-]+",))
        self.business_tools = BusinessToolRuntime(
            (_skill(),), executor or _InvoiceExecutor(), application_id="ledger-demo",
            data_classifier=data, postcondition_verifier=postconditions,
            route_validator=routes)
        self.ports = ApplicationPorts(
            identity=AnonymousIdentityPort(),
            routes=routes,
            postconditions=postconditions,
            reconciliation=ConservativeMutationReconciler(),
            data=data,
        )
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
        result = bound.business_tools.execute(
            "query_invoices", {}, {"ledger.invoice-query"})
        self.assertEqual(result.data_classes, ("business.invoice",))
        self.assertEqual(result.allowed_routes, ("/invoices/invoice-1",))

    def test_route_port_rejects_a_tool_route_outside_the_application_contract(self):
        class OutsideRouteExecutor(_InvoiceExecutor):
            def execute(self, name, args):
                return BusinessToolResult(
                    content={"route": "/settings/admin"},
                    audit={"operation": name},
                    allowed_routes=("/settings/admin",),
                )

        bound = bind_application(_InvoiceAdapter(OutsideRouteExecutor()))
        with self.assertRaisesRegex(BusinessToolContractError, "route outside"):
            bound.business_tools.execute(
                "query_invoices", {}, {"ledger.invoice-query"})

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

    def test_descriptor_origin_matching_normalizes_host_and_default_port(self):
        descriptor = _InvoiceAdapter().descriptor
        self.assertTrue(descriptor.owns_url("https://LEDGER.EXAMPLE:443/invoices/1"))
        self.assertFalse(descriptor.owns_url("https://ledger.example.evil/invoices/1"))
        self.assertFalse(descriptor.owns_url("https://user@ledger.example/invoices/1"))

    def test_route_port_rejects_undeclared_or_session_handoff_queries(self):
        routes = DeclaredRoutePort(
            "ledger-demo", (r"/invoices/[a-z0-9-]+",),
            session_handoff_query_keys=frozenset({"token"}),
            allowed_tool_query_keys=frozenset({"view"}),
        )
        self.assertEqual(
            routes.validate_tool_route("/invoices/invoice-1?view=summary"),
            "/invoices/invoice-1?view=summary")
        for route in (
                "/invoices/invoice-1?token=secret",
                "/invoices/invoice-1?unknown=1",
                "/invoices/invoice-1?view=a&view=b"):
            with self.subTest(route=route), self.assertRaisesRegex(
                    ValueError, "query"):
                routes.validate_tool_route(route)

    def test_generic_route_port_does_not_invent_source_contract_semantics(self):
        routes = _InvoiceAdapter().ports.routes
        self.assertEqual(
            routes.route_identity("https://ledger.example/invoices/invoice-1"),
            "/invoices/invoice-1")
        self.assertEqual(
            routes.contract_route_identity("https://ledger.example/invoices/invoice-1"),
            ("", ""))
        self.assertEqual(routes.discover_source_scopes(object()), frozenset())

    def test_route_port_requires_named_groups_for_contract_identity(self):
        with self.assertRaisesRegex(ValueError, "scope and route"):
            DeclaredRoutePort(
                "ledger-demo", (r"/invoices/[a-z0-9-]+",),
                contract_route_pattern=r"/invoices/(?P<route>[^/]+)")

    def test_contract_route_pattern_and_template_must_be_configured_together(self):
        with self.assertRaisesRegex(ValueError, "configured together"):
            DeclaredRoutePort(
                "ledger-demo", (r"/invoices/[a-z0-9-]+",),
                contract_route_pattern=r"/(?P<scope>[a-z]+)/control/(?P<route>[^/]+)")
        with self.assertRaisesRegex(ValueError, "configured together"):
            DeclaredRoutePort(
                "ledger-demo", (r"/invoices/[a-z0-9-]+",),
                contract_route_template="/{scope}/control/{route}")

    def test_contract_route_template_requires_both_placeholders(self):
        with self.assertRaisesRegex(ValueError, "requires"):
            DeclaredRoutePort(
                "ledger-demo", (r"/invoices/[a-z0-9-]+",),
                contract_route_pattern=r"/(?P<scope>[a-z]+)/control/(?P<route>[^/]+)",
                contract_route_template="/{scope}/control/")

    def test_compose_contract_route_inverts_contract_route_identity(self):
        routes = DeclaredRoutePort(
            "ledger-demo", (r"/invoices/[a-z0-9-]+",),
            contract_route_pattern=r"/(?P<scope>[A-Za-z0-9_]+)/control/(?P<route>[^?;#]+)",
            contract_route_template="/{scope}/control/{route}")
        composed = routes.compose_contract_route("accounting", "findPayments")
        self.assertEqual(composed, "/accounting/control/findPayments")
        self.assertEqual(routes.contract_route_identity(composed),
                         ("accounting", "findpayments"))

    def test_compose_contract_route_refuses_an_unconfigured_adapter(self):
        routes = DeclaredRoutePort("ledger-demo", (r"/invoices/[a-z0-9-]+",))
        with self.assertRaisesRegex(ValueError, "no contract route template"):
            routes.compose_contract_route("accounting", "findPayments")


class SourceCapabilityTests(unittest.TestCase):
    """Four settings that only make sense together, and only with a map behind them."""

    def _map(self):
        from web_operation_agent.schemas import SCHEMA_VERSIONS, SourceMap
        return SourceMap(schema_version=SCHEMA_VERSIONS["SourceMap"],
                         attestation_id="fixture", repository_id="fixture/invented",
                         release="v0", relations=[])

    def test_no_compiler_means_no_source_settings_at_all(self):
        capability = SourceCapability.none()
        self.assertEqual(capability.session_kwargs(), {
            "source_map": None, "source_contract": False,
            "runtime_source": False, "source_affordance_mode": "off"})

    def test_a_source_backed_setting_without_a_map_is_refused(self):
        for kwargs in ({"source_contract": True}, {"runtime_source": True},
                       {"source_affordance_mode": "auto"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "version-bound SourceMap"):
                    SourceCapability(**kwargs)

    def test_an_unknown_affordance_mode_is_refused(self):
        with self.assertRaisesRegex(ValueError, "source_affordance_mode"):
            SourceCapability(source_map=self._map(), source_affordance_mode="sometimes")

    def test_the_empty_affordance_mode_needs_no_map(self):
        # "empty" is the paired control: the interface exists and returns nothing, which is
        # how a comparison keeps the tool surface identical across arms.
        self.assertEqual(SourceCapability(source_affordance_mode="empty").source_map, None)

    def test_a_session_takes_the_capability_itself_not_its_keywords(self):
        """`session_kwargs()` survives for adapters that still spread it; a session does not.

        Spreading four keywords is how a composition came to enable three of them. The
        session now accepts only the whole value, so there is nothing to pick from.
        """
        import inspect
        from web_operation_agent.session import ServerAgentSession
        accepted = set(inspect.signature(ServerAgentSession.__init__).parameters)
        self.assertIn("source", accepted)
        self.assertEqual(accepted & set(SourceCapability.none().session_kwargs()), set())


if __name__ == "__main__":
    unittest.main()
