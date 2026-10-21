from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.modeling.client import KeyedLimitedChat, KeyedProviderAdmission
from web_operation_agent.modeling.observability import ObservedChat, RuntimeObservability


class _Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read(self, _limit):
        return b""


class _Chat:
    model = "scripted-observability"
    meter = None
    options = None
    egress_policy = None

    def complete(self, messages, tools, run_id, max_tokens=None, options=None,
                 timeout_s=None):
        return {"content": "done"}

    def complete_cancellable(self, messages, tools, run_id, *, cancelled,
                             max_tokens=None, options=None, timeout_s=None):
        return self.complete(messages, tools, run_id, max_tokens, options, timeout_s)


class RuntimeObservabilityTests(unittest.TestCase):
    def test_real_controlled_receiver_gets_otlp_and_alert_requests(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append((self.path, json.loads(self.rfile.read(length))))
                self.send_response(204)
                self.end_headers()

        receiver = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=receiver.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{receiver.server_port}"
        try:
            telemetry = RuntimeObservability(
                otlp_endpoint=origin, alert_webhook=origin + "/alerts")
            telemetry.record({"run_id": "real-1", "event": "provider_egress", "t": 1})
            telemetry.record({"run_id": "real-1", "event": "product_turn_finished",
                              "status": "finished", "t": 2})
            telemetry.alert("restore_failure")
            self.assertTrue(telemetry.flush())
            telemetry.close()
        finally:
            receiver.shutdown()
            receiver.server_close()
            thread.join(2)

        self.assertEqual([path for path, _ in received], [
            "/v1/traces", "/v1/metrics", "/alerts",
        ])
        self.assertNotIn("messages", json.dumps(received))

    def test_safe_ledger_events_export_standard_trace_and_metric_shapes(self):
        requests = []

        def send(request, timeout):
            requests.append((request.full_url, json.loads(request.data), timeout))
            return _Response()

        with tempfile.TemporaryDirectory() as tmp, patch(
                "urllib.request.urlopen", side_effect=send):
            telemetry = RuntimeObservability(
                otlp_endpoint="http://collector.test:4318", timeout_s=.1)
            ledger = SessionLedger(
                Path(tmp) / "audit.jsonl", "run-1", event_sink=telemetry)
            telemetry.model_wait("run-1", .025)
            ledger.append("provider_egress", messages=["private prompt"])
            telemetry.model_call("run-1", .1, "success")
            ledger.append("tool_call", step=1, tool="query_customers",
                          args={"name": "private customer"})
            ledger.append("business_tool_result", step=1,
                          records=[{"name": "private customer"}])
            ledger.append("answer_computed", step=2, answer="private answer")
            ledger.append("answer_verified", step=2)
            ledger.append("product_turn_finished", status="finished",
                          terminal_reason="finish")
            self.assertTrue(telemetry.flush())
            snapshot = telemetry.snapshot()
            telemetry.close()

        self.assertEqual(snapshot["turns"], 1)
        self.assertEqual(snapshot["exports_failed"], 0)
        self.assertEqual({url for url, _, _ in requests}, {
            "http://collector.test:4318/v1/traces",
            "http://collector.test:4318/v1/metrics",
        })
        encoded = json.dumps(requests, sort_keys=True)
        self.assertNotIn("private prompt", encoded)
        self.assertNotIn("private customer", encoded)
        self.assertNotIn("private answer", encoded)
        trace = next(body for url, body, _ in requests if url.endswith("/v1/traces"))
        names = [span["name"] for span in
                 trace["resourceSpans"][0]["scopeSpans"][0]["spans"]]
        self.assertEqual(names, ["woa.model", "woa.tool", "woa.answer.verify", "woa.turn"])

    def test_alerts_are_content_free_and_indeterminate_is_deduplicated_per_run(self):
        requests = []

        def send(request, timeout):
            requests.append(json.loads(request.data))
            return _Response()

        with patch("urllib.request.urlopen", side_effect=send):
            telemetry = RuntimeObservability(
                alert_webhook="http://alerts.test/events", cost_alert_cny=1)
            for index in range(3):
                telemetry.record({"run_id": f"provider-{index}",
                                  "event": "provider_failed", "t": 1})
            for index in range(3):
                telemetry.record({"run_id": f"deadline-{index}",
                                  "event": "provider_deadline", "t": 1})
            telemetry.record({"run_id": "cancelled-provider",
                              "event": "provider_cancelled", "t": 1})
            telemetry.record({"run_id": "write-1", "event": "write_outcome",
                              "state": "unknown", "detail": "private", "t": 2})
            telemetry.record({"run_id": "write-1", "event": "product_turn_finished",
                              "status": "indeterminate", "terminal_reason":
                              "indeterminate_write", "t": 3})
            telemetry.model_call("cost-1", .1, "success", known_cost_cny=1.25)
            self.assertTrue(telemetry.flush())
            snapshot = telemetry.snapshot()
            telemetry.close()

        self.assertEqual(snapshot["provider_failures"], 3)
        self.assertEqual(snapshot["indeterminate_writes"], 1)
        self.assertEqual([item["kind"] for item in requests], [
            "provider_sustained_failure", "provider_deadline_sustained",
            "indeterminate_write",
            "cost_threshold_exceeded",
        ])
        self.assertEqual(snapshot["provider_deadlines"], 3)
        self.assertEqual(snapshot["provider_deadline_streak"], 0)
        self.assertNotIn("private", json.dumps(requests))

    def test_stale_telemetry_run_is_closed_by_bounded_maintenance(self):
        telemetry = RuntimeObservability()
        telemetry.record({"run_id": "orphan-1", "event": "provider_egress", "t": 1})
        self.assertEqual(telemetry.reap_stale_runs(
            max_age_s=60, now_ns=61_000_000_000), 1)
        snapshot = telemetry.snapshot()
        telemetry.close()

        self.assertEqual(snapshot["active_runs"], 0)
        self.assertEqual(snapshot["turns"], 1)
        self.assertEqual(snapshot["failed_turns"], 1)
        self.assertEqual(snapshot["stale_runs_reaped"], 1)

    def test_observed_chat_preserves_results_and_export_failure_is_nonfatal(self):
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            telemetry = RuntimeObservability(
                otlp_endpoint="http://collector.test:4318", timeout_s=.01)
            chat = ObservedChat(_Chat(), telemetry)
            self.assertEqual(chat.complete([], [], "chat-1"), {"content": "done"})
            telemetry.record({"run_id": "chat-1", "event": "product_turn_finished",
                              "status": "finished", "t": 1})
            self.assertTrue(telemetry.flush())
            self.assertEqual(telemetry.snapshot()["exports_failed"], 2)
            telemetry.close()

    def test_diagnostic_wait_observer_cannot_change_the_provider_result(self):
        def broken(_run_id, _waited):
            raise RuntimeError("monitor is broken")

        chat = KeyedLimitedChat(
            _Chat(), KeyedProviderAdmission(1), "actor", wait_observer=broken)
        self.assertEqual(chat.complete([], [], "chat-2"), {"content": "done"})


if __name__ == "__main__":
    unittest.main()
