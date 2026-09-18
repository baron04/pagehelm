from __future__ import annotations

import json
import threading
import unittest

from evals.pageagent.host import (PageAgentHostError, PageAgentProviderContract,
                                  PageAgentProviderProxy)


class _Meter:
    def __init__(self):
        self.calls = 0

    def run_report(self, _run_id):
        return {
            "model_calls": self.calls,
            "usage_unavailable_calls": 0,
            "provider_reported_models": ["snapshot"] if self.calls else [],
            "provider_request_ids_digest": "sha256:" + "a" * 64 if self.calls else None,
            "known_input_tokens": self.calls * 10,
            "known_output_tokens": self.calls * 2,
            "known_cached_tokens": 0,
            "known_reasoning_tokens": 0,
            "known_cost_cny": 0.01 * self.calls,
            "total_cost_cny": 0.01 * self.calls,
            "spend_ledger_slice_digest": "sha256:" + "b" * 64 if self.calls else None,
        }


class _Client:
    model = "frozen-model"

    def __init__(self, *, error=None):
        self.meter = _Meter()
        self.error = error
        self.calls = []

    def request_completion(self, body, run_id, *, timeout_s):
        self.calls.append((body, run_id, timeout_s))
        if self.error:
            self.meter.calls += 1
            raise self.error
        self.meter.calls += 1
        return {
            "id": "response", "model": "snapshot",
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
        }


def _contract(**changes):
    values = {
        "model": "frozen-model", "temperature": 0, "top_p": 1,
        "max_completion_tokens": 512, "max_model_calls": 2, "wall_time_s": 30,
    }
    values.update(changes)
    return PageAgentProviderContract(**values)


def _payload(**body_changes):
    body = {
        "model": "frozen-model",
        "messages": [{"role": "user", "content": "synthetic task"}],
        "temperature": 0.0,
        "top_p": 1.0,
        "max_completion_tokens": 512,
        "parallel_tool_calls": False,
    }
    body.update(body_changes)
    return {
        "run_token": "opaque-run-token-1234",
        "url": "https://provider.invalid/v1/chat/completions",
        "method": "POST",
        "body": json.dumps(body),
    }


class ProviderContractTests(unittest.TestCase):
    def test_bridge_options_are_derived_from_the_frozen_contract(self):
        self.assertEqual(_contract().bridge_request_options(), {
            "temperature": 0.0, "top_p": 1.0,
            "max_completion_tokens": 512, "parallel_tool_calls": False,
        })

    def test_parallel_calls_cannot_be_enabled(self):
        with self.assertRaisesRegex(ValueError, "parallel"):
            _contract(parallel_tool_calls=True)


class ProviderProxyTests(unittest.TestCase):
    def _proxy(self, client=None, **kwargs):
        return PageAgentProviderProxy(
            run_token="opaque-run-token-1234", run_id="run-1",
            client=client or _Client(), contract=_contract(), **kwargs)

    def test_exact_request_is_forwarded_without_rewriting(self):
        client = _Client()
        now = [100.0]
        proxy = self._proxy(client, clock=lambda: now[0])
        payload = _payload(tool_choice={"type": "function"})

        response = proxy.handle(payload)

        self.assertEqual(client.calls[0][0], json.loads(payload["body"]))
        self.assertEqual(client.calls[0][1], "run-1")
        self.assertEqual(client.calls[0][2], 30)
        self.assertEqual(json.loads(response["body"])["model"], "snapshot")
        report = proxy.report()
        self.assertEqual(report["provider_attempts"], 1)
        self.assertEqual(report["provider_responses"], 1)
        self.assertEqual(report["accounting"]["model_calls"], 1)

    def test_token_url_method_and_frozen_options_fail_before_client(self):
        mutations = [
            {"run_token": "wrong-run-token-1234"},
            {"url": "https://provider.invalid/v1/other"},
            {"method": "GET"},
        ]
        for mutation in mutations:
            client = _Client()
            proxy = self._proxy(client)
            payload = {**_payload(), **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(PageAgentHostError):
                proxy.handle(payload)
            self.assertEqual(client.calls, [])
        for body_change in (
                {"model": "other"}, {"temperature": 1}, {"top_p": .5},
                {"max_completion_tokens": 511}, {"parallel_tool_calls": True},
                {"stream": True}):
            client = _Client()
            proxy = self._proxy(client)
            with self.subTest(body=body_change), self.assertRaises(PageAgentHostError):
                proxy.handle(_payload(**body_change))
            self.assertEqual(client.calls, [])

    def test_attempt_budget_is_hard_and_has_no_retry_path(self):
        client = _Client()
        proxy = PageAgentProviderProxy(
            run_token="opaque-run-token-1234", run_id="run-1", client=client,
            contract=_contract(max_model_calls=1))
        proxy.handle(_payload())
        with self.assertRaisesRegex(PageAgentHostError, "model_call_budget"):
            proxy.handle(_payload())
        self.assertEqual(len(client.calls), 1)

    def test_deadline_and_cancellation_stop_before_dispatch(self):
        now = [100.0]
        client = _Client()
        proxy = self._proxy(client, clock=lambda: now[0])
        now[0] = 131
        with self.assertRaisesRegex(PageAgentHostError, "provider_deadline"):
            proxy.handle(_payload())
        self.assertEqual(client.calls, [])

        cancelled = threading.Event()
        cancelled.set()
        client = _Client()
        proxy = self._proxy(client, cancelled=cancelled)
        with self.assertRaisesRegex(PageAgentHostError, "cancelled"):
            proxy.handle(_payload())
        self.assertEqual(client.calls, [])

    def test_client_error_is_redacted_at_the_page_boundary_but_accounted(self):
        class SecretError(RuntimeError):
            code = "provider_transport"

        client = _Client(error=SecretError("api_key=private"))
        proxy = self._proxy(client)
        with self.assertRaises(PageAgentHostError) as caught:
            proxy.handle(_payload())
        self.assertEqual(str(caught.exception), "provider_call")
        self.assertNotIn("private", str(caught.exception))
        report = proxy.report()
        self.assertEqual(report["last_error_code"], "provider_transport")
        self.assertEqual(report["accounting"]["model_calls"], 1)

    def test_only_a_typed_client_failure_is_the_providers(self):
        """A failure the completion client typed is the gateway's to explain. Anything else on
        that path is the host failing on its own, and says so, rather than passing for a failed
        provider call that no gateway record could explain."""
        typed = type("Typed", (RuntimeError,), {"code": "provider_rate_limited"})
        for error, code, origin in ((typed("x"), "provider_call", "provider"),
                                    (RuntimeError("boom"), "host_fault", "host")):
            with self.subTest(origin=origin):
                proxy = self._proxy(_Client(error=error))
                with self.assertRaises(PageAgentHostError) as caught:
                    proxy.handle(_payload())
                self.assertEqual((caught.exception.code, caught.exception.origin), (code, origin))
        budget = self._proxy(_Client())
        budget.handle(_payload())
        budget.handle(_payload())
        with self.assertRaises(PageAgentHostError) as caught:
            budget.handle(_payload())
        self.assertEqual((caught.exception.code, caught.exception.origin),
                         ("model_call_budget", "limit"))
        with self.assertRaises(PageAgentHostError) as caught:
            self._proxy(_Client()).handle({"bad": "payload"})
        self.assertEqual(caught.exception.origin, "host")

    def test_response_requires_a_provider_model_snapshot(self):
        client = _Client()

        def missing_model(body, run_id, *, timeout_s):
            client.calls.append((body, run_id, timeout_s))
            client.meter.calls += 1
            return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}, "choices": []}

        client.request_completion = missing_model
        proxy = self._proxy(client)
        with self.assertRaisesRegex(PageAgentHostError, "provider_response"):
            proxy.handle(_payload())
        self.assertEqual(proxy.report()["accounting"]["model_calls"], 1)

    def test_transport_counts_remain_available_when_ledger_report_fails(self):
        client = _Client()
        proxy = self._proxy(client)
        proxy.handle(_payload())
        client.meter.run_report = lambda _run_id: None

        with self.assertRaisesRegex(PageAgentHostError, "ledger_failure"):
            proxy.report()
        self.assertEqual(proxy.transport_report(), {
            "provider_attempts": 1,
            "provider_responses": 1,
            "last_error_code": None,
        })


if __name__ == "__main__":
    unittest.main()
