"""The real provider backend, exercised only against a local fake OpenAI-compatible server."""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from web_operation_agent.modeling.client import cost_with, price_for

from evals.odoo import gateway, ledger
from evals.odoo.tests.test_ledger import DIGEST, header, slots

MODEL = "qwen3.8-flash"
USAGE = {"prompt_tokens": 1200, "completion_tokens": 80,
         "prompt_tokens_details": {"cached_tokens": 200},
         "completion_tokens_details": {"reasoning_tokens": 30}}
BODY = {"model": MODEL, "messages": [{"role": "user", "content": "17 x 23?"}],
        "temperature": 0.0, "max_tokens": 64, "thinking_budget": 1024,
        "parallel_tool_calls": False}


def completion(model=MODEL, usage=USAGE):
    value = {"id": "chatcmpl-1", "object": "chat.completion",
             "choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": "391"}}]}
    if model is not None:
        value["model"] = model
    if usage is not None:
        value["usage"] = usage
    return value


class FakeProvider:
    """Answers each request with the next scripted (status, body bytes, delay) and records it."""

    def __init__(self, test, script):
        self.script = list(script)
        self.requests = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                provider.requests.append({"path": self.path, "headers": dict(self.headers),
                                          "body": json.loads(self.rfile.read(length))})
                status, data, delay = provider.script.pop(0)
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"


class UpstreamBackendTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.key = self.root / "evaluation.key"
        self.key.write_text("sk-test-evaluation-key\n")
        self.spend = self.root / "spend.jsonl"

    def backend(self, provider, *, timeout_s=10.0):
        return gateway.UpstreamBackend(base_url=provider.base_url, key_file=self.key,
                                       requested_model=MODEL, spend_ledger=self.spend,
                                       batch_id="p74-test", timeout_s=timeout_s)

    def rows(self):
        if not self.spend.exists():
            return []
        return [json.loads(line) for line in self.spend.read_text().splitlines()]

    def test_a_reported_call_is_forwarded_once_and_accounted_like_the_product_client(self):
        provider = FakeProvider(self, [(200, json.dumps(completion(model="qwen3.8-flash-0901"))
                                        .encode(), 0)])
        result = self.backend(provider).complete(dict(BODY), attempt_id="attempt-001",
                                                 call_index=1)
        self.assertEqual((result.status, result.http_status, result.response_model),
                         ("ok", 200, "qwen3.8-flash-0901"))
        self.assertEqual(result.usage, {"input_tokens": 1200, "output_tokens": 80,
                                        "cached_tokens": 200, "reasoning_tokens": 30})
        self.assertAlmostEqual(result.cost_cny,
                               round(cost_with(price_for(MODEL), 1200, 80, 200), 6))
        (request,) = provider.requests
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["body"], BODY)
        headers = {key.lower(): value for key, value in request["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer sk-test-evaluation-key")
        self.assertEqual(headers["x-woa-request-id"], result.provider_request_id)
        (row,) = self.rows()
        self.assertEqual((row["accounting_status"], row["logical_run_id"],
                          row["provider_request_id"], row["provider_reported_model"]),
                         ("reported", "attempt-001", result.provider_request_id,
                          "qwen3.8-flash-0901"))
        self.assertEqual((row["in"], row["out"], row["cached"]), (1200, 80, 200))

    def test_failed_or_unattributable_calls_leave_usage_unavailable_rows(self):
        provider = FakeProvider(self, [
            (503, b'{"error":"busy"}', 0),
            (200, b"not json", 0),
            (200, json.dumps(completion(usage=None)).encode(), 0),
            (200, json.dumps(completion(model=None)).encode(), 0),
        ])
        backend = self.backend(provider)
        results = [backend.complete(dict(BODY), attempt_id="attempt-002", call_index=index)
                   for index in range(1, 5)]
        self.assertEqual([(item.status, item.http_status, item.error_code) for item in results], [
            ("http_error", 503, "provider_unavailable"),
            ("contract_error", 200, "provider_response_contract"),
            ("contract_error", 200, "provider_response_contract"),
            ("contract_error", 200, "provider_response_contract")])
        self.assertEqual((results[2].usage, results[2].cost_cny, results[2].response_model),
                         (None, None, MODEL))
        self.assertIsNotNone(results[3].cost_cny)
        self.assertTrue(all(item.payload is None for item in results[1:]))
        self.assertEqual([(row["accounting_status"], row.get("error_code")) for row in self.rows()],
                         [("usage_unavailable", "provider_unavailable"),
                          ("usage_unavailable", "provider_response"),
                          ("usage_unavailable", "provider_usage"),
                          ("reported", None)])

    def test_a_timeout_is_a_deadline_and_a_refused_connection_is_lost(self):
        provider = FakeProvider(self, [(200, json.dumps(completion()).encode(), 1.5)])
        slow = self.backend(provider, timeout_s=0.3).complete(
            dict(BODY), attempt_id="attempt-003", call_index=1)
        self.assertEqual((slow.status, slow.error_code), ("transport_error", "provider_deadline"))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        backend = gateway.UpstreamBackend(
            base_url=f"http://127.0.0.1:{closed_port}/v1", key_file=self.key,
            requested_model=MODEL, spend_ledger=self.spend, batch_id="p74-test")
        lost = backend.complete(dict(BODY), attempt_id="attempt-003", call_index=2)
        self.assertEqual((lost.status, lost.error_code),
                         ("transport_error", "provider_connection_lost"))
        self.assertEqual([row["error_code"] for row in self.rows()],
                         ["provider_deadline", "provider_transport"])

    def test_rate_limits_credentials_deadlines_and_context_rejections_are_typed(self):
        provider = FakeProvider(self, [
            (429, b'{"error":{"message":"slow down"}}', 0),
            (401, b'{"error":{"code":"invalid_api_key"}}', 0),
            (403, b"forbidden", 0),
            (400, json.dumps({"error": {"code": "context_length_exceeded"}}).encode(), 0),
            (400, json.dumps({"error": {"message": "This model's maximum context length is "
                                                   "32768 tokens."}}).encode(), 0),
            (413, json.dumps({"message": "Prompt is too long"}).encode(), 0),
            (400, json.dumps({"error": {"message": "tools[0] is not a valid function"}})
             .encode(), 0),
            (408, b'{"error":{"message":"request timed out"}}', 0),
            (425, b"too early", 0),
        ])
        backend = self.backend(provider)
        codes = [backend.complete(dict(BODY), attempt_id="attempt-004",
                                  call_index=index).error_code for index in range(1, 10)]
        # A provider's 408 is its request deadline, typed like a timeout on this side.
        self.assertEqual(codes, ["provider_rate_limited", "provider_authentication",
                                 "provider_authentication", "provider_context_length",
                                 "provider_context_length", "provider_context_length",
                                 "provider_http", "provider_deadline", "provider_unavailable"])
        self.assertEqual([row["accounting_status"] for row in self.rows()],
                         ["usage_unavailable"] * 9)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_a_call_the_spend_ledger_cannot_record_is_an_accounting_failure(self):
        provider = FakeProvider(self, [(200, json.dumps(completion()).encode(), 0),
                                       (503, b"{}", 0)])
        locked = self.root / "locked"
        locked.mkdir()
        backend = gateway.UpstreamBackend(base_url=provider.base_url, key_file=self.key,
                                          requested_model=MODEL,
                                          spend_ledger=locked / "spend.jsonl",
                                          batch_id="p74-test")
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        results = [backend.complete(dict(BODY), attempt_id="attempt-005", call_index=index)
                   for index in (1, 2)]
        self.assertEqual([(item.status, item.error_code, item.cost_cny) for item in results],
                         [("accounting_error", "accounting_failure", None)] * 2)
        self.assertEqual((results[0].response_model, results[0].http_status), (MODEL, 200))
        self.assertEqual(len(provider.requests), 2)

    def test_endpoint_and_key_are_checked_before_any_call(self):
        for url in ("http://provider.example.test/v1", "ftp://127.0.0.1/v1",
                    "https://user:secret@provider.example.test/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                gateway.UpstreamBackend(base_url=url, key_file=self.key, requested_model=MODEL,
                                        spend_ledger=self.spend, batch_id="p74-test")
        gateway.UpstreamBackend(base_url="https://provider.example.test/v1", key_file=self.key,
                                requested_model=MODEL, spend_ledger=self.spend,
                                batch_id="p74-test")
        self.key.write_text("two tokens\n")
        with self.assertRaises(ValueError):
            gateway.UpstreamBackend(base_url="https://provider.example.test/v1",
                                    key_file=self.key, requested_model=MODEL,
                                    spend_ledger=self.spend, batch_id="p74-test")
        self.assertEqual(self.rows(), [])

    def test_the_identity_changes_with_endpoint_or_key_and_never_holds_the_key(self):
        other = self.root / "other.key"
        other.write_text("sk-another-evaluation-key\n")

        def identity(url, key):
            return gateway.UpstreamBackend(base_url=url, key_file=key, requested_model=MODEL,
                                           spend_ledger=self.spend, batch_id="p74-test").identity()

        base = identity("https://provider.example.test/v1", self.key)
        self.assertEqual(identity("HTTPS://Provider.Example.Test:443/v1/", self.key), base)
        for url, key in (("https://other.example.test/v1", self.key),
                         ("https://provider.example.test/v2", self.key),
                         ("https://provider.example.test:8443/v1", self.key),
                         ("https://provider.example.test/v1", other)):
            with self.subTest(url=url, key=key.name):
                self.assertNotEqual(identity(url, key), base)
        self.assertNotIn("sk-test-evaluation-key", json.dumps(base))
        self.assertEqual(self.rows(), [])

    def test_gateway_ledger_carries_the_upstream_request_id_usage_and_cost(self):
        provider = FakeProvider(self, [(200, json.dumps(completion()).encode(), 0)])
        path = self.root / "attempts.jsonl"
        ledger.create(path, header(slots(1)))
        ledger.append(path, "attempt_started", {
            "attempt_id": "attempt-001", "slot_id": "s000", "arm": "woa_product",
            "task_id": "task-000", "replicate": "primary", "run_id": "run-001",
            "fixture_digest": DIGEST, "capability_digest": DIGEST,
            "provider_config_digest": DIGEST, "provider_binding_digest": DIGEST,
            "replacement_of": None})
        service = gateway.ProviderGateway(
            ledger_path=path, backend=self.backend(provider),
            budgets=gateway.GatewayBudgets(MODEL, max_model_calls=2, max_output_tokens=2048,
                                           model_options={"thinking_budget": 1024,
                                                          "parallel_tool_calls": False})).start()
        self.addCleanup(service.close)
        service.open_attempt("attempt-001", "t" * 32)
        request = urllib.request.Request(
            service.base_url + "/chat/completions", data=json.dumps(BODY).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "Authorization": "Bearer " + "t" * 32})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(json.loads(response.read())["model"], MODEL)
        entries, summary = ledger.read(path)
        received = next(entry["body"] for entry in entries
                        if entry["kind"] == "model_response_received")
        (row,) = self.rows()
        self.assertEqual(received["provider_request_id"], row["provider_request_id"])
        self.assertEqual(received["usage"]["input_tokens"], 1200)
        self.assertAlmostEqual(received["cost_cny"], row["call_cost_cny"])
        self.assertEqual(summary["provider_model"], MODEL)


if __name__ == "__main__":
    unittest.main()
