"""Reusable assertions for every in-repository Application Adapter."""
from __future__ import annotations

from web_operation_agent.application import (
    ApplicationAdapter,
    ApplicationIdentityPort,
    ApplicationMutationReconciler,
    ApplicationRoutePort,
    BoundApplication,
    bind_application,
)
from web_operation_agent.business import BusinessDataClassifier, BusinessPostconditionVerifier


def assert_adapter_contract(case, adapter: ApplicationAdapter) -> BoundApplication:
    """Assert the executable invariants shared by real and synthetic adapters."""
    case.assertIsInstance(adapter, ApplicationAdapter)
    bound = bind_application(adapter)
    case.assertIsInstance(bound, BoundApplication)
    case.assertEqual(bound.descriptor.application_id, bound.ports.routes.application_id)
    case.assertEqual(bound.descriptor.catalog_sha256, bound.business_tools.catalog_sha256)
    case.assertEqual(bound.ports.data.tool_names, bound.business_tools.tool_names)
    case.assertEqual(
        bound.ports.postconditions.supported_tools,
        bound.business_tools.mutation_tool_names)
    case.assertEqual(
        bound.ports.reconciliation.supported_tools,
        bound.business_tools.mutation_tool_names)
    case.assertIsInstance(bound.ports.identity, ApplicationIdentityPort)
    case.assertIsInstance(bound.ports.routes, ApplicationRoutePort)
    case.assertIsInstance(bound.ports.data, BusinessDataClassifier)
    case.assertIsInstance(bound.ports.postconditions, BusinessPostconditionVerifier)
    case.assertIsInstance(bound.ports.reconciliation, ApplicationMutationReconciler)
    case.assertIs(bound.business_tools.route_validator, bound.ports.routes)
    case.assertIs(bound.business_tools.data_classifier, bound.ports.data)
    case.assertIs(bound.business_tools.postcondition_verifier, bound.ports.postconditions)
    for unsafe in ("https://other.invalid/record", "//other.invalid/record", "/../record"):
        with case.assertRaises(ValueError):
            bound.ports.routes.validate_tool_route(unsafe)
    return bound
