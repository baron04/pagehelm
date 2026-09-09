from __future__ import annotations

import unittest

from evals.twenty.outcomes import (
    business_apparatus_code, classify_host_errors, provider_apparatus_code,
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


if __name__ == "__main__":
    unittest.main()
