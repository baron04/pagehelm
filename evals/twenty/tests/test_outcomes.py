from __future__ import annotations

import unittest

from evals.twenty.outcomes import (
    business_apparatus_code, classify_host_errors, classify_woa_outcome,
    provider_apparatus_code,
)


class SharedOutcomeProjectionTests(unittest.TestCase):
    def test_engine_failures_remain_scorable(self):
        for code in ("model_call_budget", "provider_deadline", "cancelled"):
            self.assertIsNone(provider_apparatus_code(code), code)
        for code in (
                "business_request", "business_call_budget", "route_call_budget",
                "route_refused", "cancelled"):
            self.assertIsNone(business_apparatus_code(code), code)

    def test_external_failures_use_the_frozen_apparatus_classes(self):
        self.assertEqual(
            provider_apparatus_code("provider_transport"), "provider_outage")
        self.assertEqual(
            business_apparatus_code("business_unavailable"), "transport_contract")
        self.assertEqual(
            business_apparatus_code("business_contract"), "transport_contract")
        self.assertEqual(
            business_apparatus_code("business_identity"), "credential_or_identity")
        self.assertEqual(
            business_apparatus_code("business_authentication"),
            "credential_or_identity")

    def test_first_apparatus_fault_wins_for_every_runner(self):
        self.assertEqual(classify_host_errors([
            ("business", "business_request"),
            ("provider", "provider_transport"),
        ]), ("provider_outage", "provider_transport"))
        self.assertEqual(classify_host_errors([
            ("business", "business_identity"),
            ("provider", "model_call_budget"),
        ]), ("credential_or_identity", "business_identity"))
        self.assertEqual(classify_host_errors([
            ("business", "business_request"),
        ]), (None, "business_request"))

    def test_woa_uses_the_same_typed_business_and_provider_classes(self):
        self.assertEqual(classify_woa_outcome(
            terminal_reason="agent_finished",
            events=[{"event": "business_tool_rejected",
                     "error_code": "business_unavailable"}],
        ), ("transport_contract", "business_unavailable"))
        self.assertEqual(classify_woa_outcome(
            terminal_reason="provider_error",
            events=[{"event": "provider_failed",
                     "provider_error_code": "provider_http"}],
        ), ("provider_outage", "provider_http"))
        self.assertEqual(classify_woa_outcome(
            terminal_reason="agent_refused",
            events=[{"event": "business_tool_rejected",
                     "error_code": "business_request"}],
        ), (None, "agent_refused"))

    def test_woa_boundary_and_transport_failures_are_apparatus_faults(self):
        self.assertEqual(classify_woa_outcome(
            terminal_reason="", boundary_error_code="identity_unavailable",
        ), ("credential_or_identity", "identity_unavailable"))
        for code in ("identity_binding", "session_changed"):
            self.assertEqual(classify_woa_outcome(
                terminal_reason="", boundary_error_code=code,
            ), ("credential_or_identity", code))
        self.assertEqual(classify_woa_outcome(
            terminal_reason="action_policy_budget"), (None, "action_policy_budget"))
        self.assertEqual(classify_woa_outcome(
            terminal_reason="", boundary_error_code="stale_message",
        ), ("transport_contract", "stale_message"))
        self.assertEqual(classify_woa_outcome(
            terminal_reason="transport_error",
        ), ("transport_contract", "transport_error"))


if __name__ == "__main__":
    unittest.main()
