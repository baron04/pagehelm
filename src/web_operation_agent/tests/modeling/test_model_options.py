"""Per-model request options.

Some providers bound answer and reasoning tokens separately. Reasoning allowances therefore
live in policy data keyed by model name rather than in model-specific code branches.
"""
import json
import io
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.modeling import client as M
from web_operation_agent.tests.session_fixture import SessionFixture


class Policy(unittest.TestCase):
    def test_an_unknown_model_is_sent_nothing_extra(self):
        """An option a provider does not understand is a 400, so absence means silence."""
        self.assertEqual(M.options_for("some-model-we-have-never-run"), {})

    def test_a_configured_model_carries_its_reasoning_allowance(self):
        self.assertTrue(M.options_for("qwen3.8-flash")["thinking_budget"] > 0)

    def test_the_react_model_disables_parallel_tool_calls(self):
        self.assertIs(M.options_for("qwen3.8-flash")["parallel_tool_calls"], False)

    def test_the_policy_may_not_rewrite_the_request_itself(self):
        saved = M.MODEL_OPTIONS.get("m-x")
        M.MODEL_OPTIONS["m-x"] = {"model": "something-else", "messages": [], "max_tokens": 1,
                                  "thinking_budget": 32}
        try:
            self.assertEqual(M.options_for("m-x"), {"thinking_budget": 32})
        finally:
            M.MODEL_OPTIONS.pop("m-x", None)
            if saved is not None:
                M.MODEL_OPTIONS["m-x"] = saved

    def test_every_entry_is_an_object_and_the_file_documents_itself(self):
        raw = json.loads(
            (Path(M.__file__).parents[1] / "policies" / "model_options.json").read_text())
        self.assertIn("_why_thinking_budget", raw)
        self.assertIn("_why_parallel_tool_calls", raw)
        for name, opts in raw.items():
            if name.startswith("_"):
                self.assertIsInstance(opts, str)
            else:
                self.assertIsInstance(opts, dict, name)


class _Recorder:
    """A ChatModel with the network removed: keeps the body it would have posted."""

    def __init__(self, model, options=None):
        self.model = model
        self.options = ({k: v for k, v in options.items() if k not in M._RESERVED_BODY}
                        if options is not None else M.options_for(model))
        self.temperature, self.max_completion_tokens = 0.0, 2048
        self.body = None

    def complete(self, messages, tools, run_id, max_tokens=None, options=None, timeout_s=None):
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "max_tokens": max_tokens or self.max_completion_tokens}
        body.update(self.options)
        if options:
            body.update({k: v for k, v in options.items() if k not in M._RESERVED_BODY})
        self.body = body
        return {"content": "", "finish_reason": "stop", "reasoning": ""}


class TheRequestBody(unittest.TestCase):
    def test_the_models_allowance_is_sent_on_an_ordinary_call(self):
        c = _Recorder("qwen3.8-flash")
        c.complete([], None, "r")
        self.assertEqual(c.body["thinking_budget"],
                         M.options_for("qwen3.8-flash")["thinking_budget"])

    def test_a_caller_may_ask_for_more_thinking_for_one_call(self):
        c = _Recorder("qwen3.8-flash")
        c.complete([], None, "r", options={"thinking_budget": 8192})
        self.assertEqual(c.body["thinking_budget"], 8192)

    def test_a_caller_may_not_redirect_the_call_to_another_model(self):
        c = _Recorder("qwen3.8-flash")
        c.complete([], None, "r", options={
            "model": "elsewhere", "temperature": 2, "stream": True})
        self.assertEqual(c.body["model"], "qwen3.8-flash")
        self.assertEqual(c.body["temperature"], 0.0)
        self.assertNotIn("stream", c.body)


class ProviderDeadline(unittest.TestCase):
    def _chat(self):
        chat = object.__new__(M.ChatModel)
        chat.base = "https://provider.example/v1"
        chat._key = "test-key"
        chat.model = "test-model"
        chat.provider = "test-provider"
        chat.max_completion_tokens = 10
        chat.temperature = 0.0
        chat.timeout = 180
        chat.options = {}

        class _Meter:
            price = {}

            @staticmethod
            def record(**_kwargs):
                return None

        chat.meter = _Meter()
        return chat

    def test_turn_deadline_caps_the_provider_socket_timeout(self):
        payload = {"usage": {"prompt_tokens": 0, "completion_tokens": 0}, "choices": [{
            "finish_reason": "stop", "message": {"content": "done"}}]}
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        response.__enter__ = lambda stream=response: stream
        response.__exit__ = lambda stream=response, *_args: None
        with patch.object(M.urllib.request, "urlopen", return_value=response) as opened:
            result = self._chat().complete(
                [{"role": "user", "content": "test"}], None, "run", timeout_s=7.5)

        self.assertEqual(result["content"], "done")
        self.assertEqual(opened.call_args.kwargs["timeout"], 7.5)

    def test_invalid_turn_timeout_is_rejected_before_network(self):
        with patch.object(M.urllib.request, "urlopen") as opened:
            with self.assertRaisesRegex(ValueError, "positive finite"):
                self._chat().complete([], None, "run", timeout_s=float("inf"))
        opened.assert_not_called()


class NativeCompletionTransport(unittest.TestCase):
    ENV = {"AGENT_OPENAI_BASE_URL": "https://provider.example/v1",
           "AGENT_OPENAI_API_KEY": "test-key",
           "AGENT_OPENAI_MODEL": "qwen3.8-flash"}

    @staticmethod
    def _response(payload):
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        response.__enter__ = lambda stream=response: stream
        response.__exit__ = lambda stream=response, *_args: None
        return response

    def test_shared_quota_refuses_before_http_and_spend(self):
        from web_operation_agent.durable import SQLiteSessionStore, SQLiteProviderQuota
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            quota = SQLiteProviderQuota(SQLiteSessionStore(path / "quota.db"),
                                        scope="tenant-provider", max_requests=1)
            self.assertTrue(quota.reserve())
            ledger = path / "spend.jsonl"
            chat = M.ChatModel(M.SpendMeter(ledger=ledger), env=self.ENV, shared_quota=quota)
            with patch.object(M.urllib.request, "urlopen") as opened:
                with self.assertRaises(M.ProviderError) as caught:
                    chat.request_completion({"model": "qwen3.8-flash", "messages": [{"role": "user", "content": "hello"}]}, "run")
                self.assertEqual(caught.exception.code, "provider_capacity")
                self.assertIs(caught.exception.dispatched, False)
                opened.assert_not_called()
            self.assertFalse(ledger.exists())
            self.assertEqual(chat.runtime_guard.status()["in_flight"], 0)

    def test_framework_native_body_is_not_rewritten_and_full_response_is_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            meter = M.SpendMeter(ledger=Path(directory) / "spend.jsonl")
            chat = M.ChatModel(meter, env=self.ENV)
            body = {
                "model": "qwen3.8-flash",
                "messages": [{"role": "user", "content": "Click the only button."}],
                "tools": [{"type": "function", "function": {
                    "name": "AgentOutput", "description": "Choose one action.",
                    "parameters": {"type": "object", "properties": {},
                                   "additionalProperties": False}}}],
                "tool_choice": {"type": "function", "function": {"name": "AgentOutput"}},
                "temperature": 0,
                "top_p": 1,
                "max_completion_tokens": 512,
                "parallel_tool_calls": False,
            }
            payload = {
                "id": "reply-1", "model": "provider-snapshot-2026-09-09",
                "usage": {"prompt_tokens": 17, "completion_tokens": 9,
                          "completion_tokens_details": {"reasoning_tokens": 3}},
                "choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "call-1", "type": "function", "function": {
                            "name": "AgentOutput", "arguments": "{}"}}]}}],
            }
            captured = []

            def respond(request, timeout):
                captured.append((json.loads(request.data), timeout))
                return self._response(payload)

            with patch.object(M.urllib.request, "urlopen", side_effect=respond):
                result = chat.request_completion(body, "native-run", timeout_s=12)

            self.assertEqual(result, payload)
            self.assertEqual(captured[0][0], body)
            self.assertGreater(captured[0][1], 11.9)
            self.assertLessEqual(captured[0][1], 12)
            report = meter.run_report("native-run")
            self.assertEqual(report["model_calls"], 1)
            self.assertEqual(report["provider_reported_models"],
                             ["provider-snapshot-2026-09-09"])
            self.assertEqual(report["known_input_tokens"], 17)
            self.assertEqual(report["known_output_tokens"], 9)
            self.assertEqual(report["known_reasoning_tokens"], 3)
            self.assertRegex(report["provider_request_ids_digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(report["spend_ledger_slice_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_model_redirect_and_streaming_are_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            chat = M.ChatModel(
                M.SpendMeter(ledger=Path(directory) / "spend.jsonl"), env=self.ENV)
            messages = [{"role": "user", "content": "test"}]
            invalid = (
                {"model": "other-model", "messages": messages},
                {"model": "qwen3.8-flash", "messages": messages, "stream": True},
            )
            with patch.object(M.urllib.request, "urlopen") as opened:
                for body in invalid:
                    with self.subTest(body=body), self.assertRaises(M.ProviderError):
                        chat.request_completion(body, "native-run")
            opened.assert_not_called()

    def test_native_extra_field_credential_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            chat = M.ChatModel(
                M.SpendMeter(ledger=Path(directory) / "spend.jsonl"), env=self.ENV)
            body = {
                "model": "qwen3.8-flash",
                "messages": [{"role": "user", "content": "test"}],
                "metadata": {"api_key": "must-not-leave"},
            }
            with patch.object(M.urllib.request, "urlopen") as opened:
                with self.assertRaises(ValueError):
                    chat.request_completion(body, "native-run")
            opened.assert_not_called()

    def test_http_statuses_reach_the_product_as_bounded_failure_classes(self):
        cases = {
            401: ("provider_authentication", False),
            403: ("provider_authentication", False),
            429: ("provider_rate_limited", True),
            503: ("provider_unavailable", True),
            418: ("provider_http", False),
        }
        for status, (code, retryable) in cases.items():
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                chat = M.ChatModel(
                    M.SpendMeter(ledger=Path(directory) / "spend.jsonl"), env=self.ENV)
                failure = M.urllib.error.HTTPError(
                    "https://provider.example/v1/chat/completions", status,
                    "synthetic", {}, io.BytesIO(b"provider body is not exposed"))
                with patch.object(M.urllib.request, "urlopen", side_effect=failure):
                    with self.assertRaises(M.ProviderError) as caught:
                        chat.complete(
                            [{"role": "user", "content": "test"}], None,
                            f"status-{status}")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn("provider body", str(caught.exception))
                row = json.loads((Path(directory) / "spend.jsonl").read_text())
                self.assertEqual(row["error_code"], code)


class ProviderRuntimeProtection(unittest.TestCase):
    def test_cancellation_aborts_an_inflight_socket_and_releases_the_guard(self):
        request_started = threading.Event()
        release_handler = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", "0")))
                request_started.set()
                release_handler.wait(5)
                try:
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{}')
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                guard = M.ProviderRuntimeGuard(max_concurrent_requests=1)
                chat = M.ChatModel(
                    M.SpendMeter(ledger=Path(directory) / "spend.jsonl"),
                    env={"AGENT_OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                         "AGENT_OPENAI_API_KEY": "key",
                         "AGENT_OPENAI_MODEL": "qwen3.8-flash"},
                    runtime_guard=guard,
                )
                cancelled = threading.Event()
                outcome = []

                def call():
                    try:
                        chat.complete_cancellable(
                            [{"role": "user", "content": "test"}], None, "run",
                            cancelled=cancelled.is_set, timeout_s=5)
                    except Exception as exc:
                        outcome.append(exc)

                worker = threading.Thread(target=call)
                worker.start()
                self.assertTrue(request_started.wait(2))
                started = time.monotonic()
                cancelled.set()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(len(outcome), 1)
                self.assertIsInstance(outcome[0], M.ProviderError)
                self.assertEqual(outcome[0].code, "provider_cancelled")
                self.assertEqual(guard.status()["in_flight"], 0)
                self.assertFalse(guard.status()["circuit_open"])
                row = json.loads((Path(directory) / "spend.jsonl").read_text())
                self.assertEqual(row["accounting_status"], "usage_unavailable")
                self.assertEqual(row["error_code"], "provider_cancelled")
        finally:
            release_handler.set()
            server.shutdown()
            server.server_close()
            server_thread.join(2)

    def test_cancellable_deadline_releases_the_guard_for_the_next_call(self):
        calls = 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal calls
                calls += 1
                self.rfile.read(int(self.headers.get("content-length", "0")))
                if calls == 1:
                    time.sleep(.3)
                payload = {"usage": {"prompt_tokens": 1, "completion_tokens": 1},
                           "choices": [{"finish_reason": "stop",
                                        "message": {"content": "done"}}]}
                try:
                    encoded = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("content-length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                guard = M.ProviderRuntimeGuard(max_concurrent_requests=1)
                chat = M.ChatModel(
                    M.SpendMeter(ledger=Path(directory) / "spend.jsonl"),
                    env={"AGENT_OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                         "AGENT_OPENAI_API_KEY": "key",
                         "AGENT_OPENAI_MODEL": "qwen3.8-flash"},
                    runtime_guard=guard,
                )
                with self.assertRaises(M.ProviderError) as timed_out:
                    chat.complete_cancellable(
                        [{"role": "user", "content": "first"}], None, "first",
                        cancelled=lambda: False, timeout_s=.05)
                self.assertEqual(timed_out.exception.code, "provider_deadline")
                self.assertEqual(guard.status()["in_flight"], 0)
                self.assertEqual(
                    guard.status()["consecutive_transient_failures"], 0)

                reply = chat.complete_cancellable(
                    [{"role": "user", "content": "second"}], None, "second",
                    cancelled=lambda: False, timeout_s=1)
                self.assertEqual(reply["content"], "done")
                self.assertEqual(guard.status()["in_flight"], 0)
                self.assertEqual(chat.meter.run_report("first")["usage_unavailable_calls"], 1)
                self.assertEqual(chat.meter.run_report("second")["model_calls"], 1)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(2)

    def test_capacity_wait_is_bounded_and_does_not_change_circuit_health(self):
        guard = M.ProviderRuntimeGuard(max_concurrent_requests=1)
        guard.acquire(1)
        try:
            with self.assertRaises(M.ProviderError) as caught:
                guard.acquire(.01)
            self.assertEqual(caught.exception.code, "provider_capacity")
            self.assertEqual(guard.status()["in_flight"], 1)
            self.assertFalse(guard.status()["circuit_open"])
        finally:
            guard.release()
        self.assertEqual(guard.status()["in_flight"], 0)

    def test_circuit_opens_after_consecutive_transient_failures_and_recovers(self):
        now = [10.0]
        guard = M.ProviderRuntimeGuard(
            failure_threshold=2, cooldown_s=5, clock=lambda: now[0])
        for _ in range(2):
            guard.acquire(1)
            guard.record_outcome(M.ProviderOutcome.TRANSIENT_PROVIDER_FAILURE)
            guard.release()
        self.assertTrue(guard.status()["circuit_open"])
        self.assertEqual(guard.status()["retry_after_s"], 5)
        with self.assertRaises(M.ProviderError) as caught:
            guard.acquire(1)
        self.assertEqual(caught.exception.code, "provider_circuit_open")
        now[0] += 5
        guard.acquire(1)
        guard.record_outcome(M.ProviderOutcome.SUCCESS)
        guard.release()
        self.assertFalse(guard.status()["circuit_open"])
        self.assertEqual(guard.status()["consecutive_transient_failures"], 0)

    def test_cancel_and_deadline_do_not_change_existing_provider_health(self):
        guard = M.ProviderRuntimeGuard(failure_threshold=3)
        for _ in range(2):
            guard.record_outcome(M.ProviderOutcome.TRANSIENT_PROVIDER_FAILURE)
        for outcome in (M.ProviderOutcome.CANCELLED, M.ProviderOutcome.DEADLINE):
            guard.record_outcome(outcome)
            self.assertEqual(
                guard.status()["consecutive_transient_failures"], 2)
            self.assertFalse(guard.status()["circuit_open"])
        guard.record_outcome(M.ProviderOutcome.PERMANENT_PROVIDER_FAILURE)
        self.assertEqual(guard.status()["consecutive_transient_failures"], 0)

    def test_waiting_for_capacity_can_be_cancelled_without_touching_health(self):
        guard = M.ProviderRuntimeGuard(max_concurrent_requests=1)
        guard.acquire(1)
        cancelled = threading.Event()
        outcome = []

        def wait_for_slot():
            try:
                guard.acquire(5, cancelled=cancelled.is_set)
            except Exception as exc:
                outcome.append(exc)

        worker = threading.Thread(target=wait_for_slot)
        worker.start()
        time.sleep(.05)
        started = time.monotonic()
        cancelled.set()
        worker.join(1)
        guard.release()

        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(outcome[0].code, "provider_cancelled")
        self.assertFalse(outcome[0].dispatched)
        self.assertEqual(guard.status()["consecutive_transient_failures"], 0)

    def test_connecting_can_be_cancelled_before_dispatch_without_a_spend_row(self):
        connect_started = threading.Event()
        release_connect = threading.Event()

        def blocked_connect(_connection):
            connect_started.set()
            release_connect.wait(5)

        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "spend.jsonl"
            guard = M.ProviderRuntimeGuard(max_concurrent_requests=1)
            chat = M.ChatModel(
                M.SpendMeter(ledger=ledger),
                env={"AGENT_OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
                     "AGENT_OPENAI_API_KEY": "key",
                     "AGENT_OPENAI_MODEL": "qwen3.8-flash"},
                runtime_guard=guard,
            )
            cancelled = threading.Event()
            outcome = []

            def call():
                try:
                    chat.complete_cancellable(
                        [{"role": "user", "content": "test"}], None, "connect-cancel",
                        cancelled=cancelled.is_set, timeout_s=5)
                except Exception as exc:
                    outcome.append(exc)

            with patch.object(M.http.client.HTTPConnection, "connect", blocked_connect):
                worker = threading.Thread(target=call)
                worker.start()
                self.assertTrue(connect_started.wait(1))
                started = time.monotonic()
                cancelled.set()
                worker.join(1)
                release_connect.set()

            self.assertFalse(worker.is_alive())
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(outcome[0].code, "provider_cancelled")
            self.assertFalse(outcome[0].dispatched)
            self.assertEqual(chat.meter.calls, 0)
            self.assertFalse(ledger.exists())
            self.assertEqual(guard.status()["in_flight"], 0)
            self.assertEqual(guard.status()["consecutive_transient_failures"], 0)

    def test_http_failures_trip_shared_guard_without_hidden_retries(self):
        now = [10.0]
        guard = M.ProviderRuntimeGuard(
            failure_threshold=2, cooldown_s=5, clock=lambda: now[0])
        with tempfile.TemporaryDirectory() as directory:
            chat = M.ChatModel(
                M.SpendMeter(ledger=Path(directory) / "spend.jsonl"),
                env={"AGENT_OPENAI_BASE_URL": "https://provider.example/v1",
                     "AGENT_OPENAI_API_KEY": "key",
                     "AGENT_OPENAI_MODEL": "qwen3.8-flash"},
                runtime_guard=guard,
            )

            def unavailable(request, timeout):
                raise M.urllib.error.HTTPError(
                    request.full_url, 503, "unavailable", {}, io.BytesIO(b"ignored"))

            with patch.object(M.urllib.request, "urlopen", side_effect=unavailable) as opened:
                for _ in range(2):
                    with self.assertRaises(M.ProviderError) as caught:
                        chat.complete([{"role": "user", "content": "test"}], None, "run")
                    self.assertEqual(caught.exception.code, "provider_unavailable")
                    self.assertTrue(caught.exception.retryable)
                with self.assertRaises(M.ProviderError) as caught:
                    chat.complete([{"role": "user", "content": "test"}], None, "run")
                self.assertEqual(caught.exception.code, "provider_circuit_open")
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(chat.meter.calls, 2)
            self.assertEqual(chat.meter.unmetered_calls, 2)
            rows = [json.loads(line) for line in
                    (Path(directory) / "spend.jsonl").read_text().splitlines()]
            self.assertEqual(
                [row["accounting_status"] for row in rows],
                ["usage_unavailable", "usage_unavailable"],
            )

    def test_request_id_is_unique_and_slot_wait_reduces_socket_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            class GuardWithMeasuredWait(M.ProviderRuntimeGuard):
                def acquire(self, timeout_s):
                    super().acquire(timeout_s)
                    return .25

            chat = M.ChatModel(
                M.SpendMeter(ledger=Path(directory) / "spend.jsonl"),
                env={"AGENT_OPENAI_BASE_URL": "https://provider.example/v1",
                     "AGENT_OPENAI_API_KEY": "key",
                     "AGENT_OPENAI_MODEL": "qwen3.8-flash"},
                runtime_guard=GuardWithMeasuredWait(max_concurrent_requests=1),
            )
            payload = {"usage": {"prompt_tokens": 0, "completion_tokens": 0}, "choices": [{
                "finish_reason": "stop", "message": {"content": "done"}}]}
            replies = []

            def respond(request, timeout):
                replies.append((request.get_header("X-woa-request-id"), timeout))
                response = io.BytesIO(json.dumps(payload).encode())
                response.__enter__ = lambda stream=response: stream
                response.__exit__ = lambda stream=response, *_args: None
                return response

            with patch.object(M.urllib.request, "urlopen", side_effect=respond):
                chat.complete([{"role": "user", "content": "one"}], None, "run", timeout_s=2)
                chat.complete([{"role": "user", "content": "two"}], None, "run", timeout_s=2)
            self.assertEqual(len(replies), 2)
            self.assertTrue(all(value and len(value) == 32 for value, _timeout in replies))
            self.assertNotEqual(replies[0][0], replies[1][0])
            self.assertEqual([timeout for _value, timeout in replies], [1.75, 1.75])
            records = [json.loads(line) for line in
                       (Path(directory) / "spend.jsonl").read_text().splitlines()]
            self.assertEqual(
                [record["provider_request_id"] for record in records],
                [value for value, _timeout in replies],
            )

    def test_missing_usage_is_unknown_cost_and_cannot_be_reported_as_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            chat = M.ChatModel(
                M.SpendMeter(ledger=Path(directory) / "spend.jsonl"),
                env={"AGENT_OPENAI_BASE_URL": "https://provider.example/v1",
                     "AGENT_OPENAI_API_KEY": "key",
                     "AGENT_OPENAI_MODEL": "qwen3.8-flash"},
            )
            payload = {"choices": [{
                "finish_reason": "stop", "message": {"content": "done"}}]}
            response = io.BytesIO(json.dumps(payload).encode())
            response.__enter__ = lambda stream=response: stream
            response.__exit__ = lambda stream=response, *_args: None
            with patch.object(M.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(M.ProviderError) as caught:
                    chat.complete([{"role": "user", "content": "test"}], None, "run")

            self.assertEqual(caught.exception.code, "provider_usage")
            row = json.loads((Path(directory) / "spend.jsonl").read_text())
            self.assertEqual(row["accounting_status"], "usage_unavailable")
            self.assertIsNone(row["call_cost_cny"])

    def test_missing_usage_still_retains_a_valid_provider_model_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            chat = M.ChatModel(
                M.SpendMeter(ledger=Path(directory) / "spend.jsonl"),
                env={"AGENT_OPENAI_BASE_URL": "https://provider.example/v1",
                     "AGENT_OPENAI_API_KEY": "key",
                     "AGENT_OPENAI_MODEL": "qwen3.8-flash"},
            )
            payload = {"model": "snapshot-without-usage", "choices": [{
                "finish_reason": "stop", "message": {"content": "done"}}]}
            response = io.BytesIO(json.dumps(payload).encode())
            response.__enter__ = lambda stream=response: stream
            response.__exit__ = lambda stream=response, *_args: None
            with patch.object(M.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(M.ProviderError):
                    chat.complete([{"role": "user", "content": "test"}], None, "run")

            report = chat.meter.run_report("run")
            self.assertEqual(report["usage_unavailable_calls"], 1)
            self.assertEqual(report["provider_reported_models"],
                             ["snapshot-without-usage"])
            self.assertIsNone(report["total_cost_cny"])


class PlannerAllowance(unittest.TestCase):
    def test_the_planner_asks_for_a_multiple_of_what_the_model_declares(self):
        from web_operation_agent.planning.contract import PLANNER_THINKING_FACTOR, thinking_options
        c = _Recorder("qwen3.8-flash")
        base = c.options["thinking_budget"]
        self.assertEqual(thinking_options(c, PLANNER_THINKING_FACTOR),
                         {"thinking_budget": base * PLANNER_THINKING_FACTOR})

    def test_it_grows_with_the_answer_budget(self):
        from web_operation_agent.planning.contract import thinking_options
        c = _Recorder("qwen3.8-flash")
        first = thinking_options(c, 4 * 2 ** 0)["thinking_budget"]
        second = thinking_options(c, 4 * 2 ** 1)["thinking_budget"]
        self.assertEqual(second, 2 * first)

    def test_a_model_with_no_such_knob_is_asked_for_nothing(self):
        """Otherwise switching provider would start posting a field it never advertised."""
        from web_operation_agent.planning.contract import thinking_options
        self.assertIsNone(thinking_options(_Recorder("some-model-we-have-never-run"), 4))
        self.assertIsNone(thinking_options(object(), 4))


if __name__ == "__main__":
    unittest.main()


class WhatTheAgentIsToldAfterAWrite(unittest.TestCase):
    """After a write is permitted, the agent may stop and verify instead of submitting again."""

    def _rendered(self, state):
        from web_operation_agent.execution.session import ServerAgentSession, _render_observation
        from web_operation_agent.schemas import (SCHEMA_VERSIONS, ActionReceipt, ElementRef,
                                                 PageObservation)
        obs = PageObservation(schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=1,
                              url="http://app/x", title="T", landmarks=[],
                              elements=[ElementRef("e1", "link", "L", "a", 1)], messages=[])
        rec = ActionReceipt(schema_version=SCHEMA_VERSIONS["ActionReceipt"], command_id="c",
                            action="click", args_echo={}, ok=True, url_before="a",
                            url_after="b", navigated=True)
        fixture = SessionFixture()
        try:
            return fixture.session()._render(obs, rec, "", {"state": state})
        finally:
            fixture.close()

    def test_a_permitted_write_offers_the_choice_of_stopping(self):
        from web_operation_agent.grounding import WRITE_PERMITTED
        text = self._rendered(WRITE_PERMITTED)
        self.assertIn("call finish", text)

    def test_it_no_longer_states_the_write_as_a_fact(self):
        """A consumed permit must not be described as a saved application change."""
        from web_operation_agent.grounding import WRITE_PERMITTED
        low = self._rendered(WRITE_PERMITTED).lower()
        self.assertNotIn("wrote to the application", low)
        self.assertIn("does not establish that it was sent or saved", low)

    def test_a_refused_write_is_not_invited_to_finish(self):
        from web_operation_agent.grounding import WRITE_REFUSED
        text = self._rendered(WRITE_REFUSED)
        self.assertIn("That request was not forwarded", text)
        self.assertNotIn("call finish", text)


class SessionRecoveryIsVisible(unittest.TestCase):
    def test_the_action_ledger_carries_it_per_action(self):
        from dataclasses import replace
        from web_operation_agent.tests.execution.test_session import (_Browser, _Chat, _Control,
                                                            _obs, _session)

        class ReloginBrowser(_Browser):
            def execute(self, action, args=None, expect_navigation=False):
                return replace(super().execute(action, args, expect_navigation),
                               relogin=True)

        with tempfile.TemporaryDirectory() as tmp:
            session = _session(
                tmp,
                _Chat([("click", {"ref": "e1"}),
                       ("finish", {"done": False, "why": "done"})]),
                ReloginBrowser([_obs(1), _obs(2)]),
                _Control([[], [], []]),
            )
            session.run()
            rows = [json.loads(line) for line in (Path(tmp) / "l.jsonl").read_text().splitlines()]

        action = next(row for row in rows
                      if row.get("event") == "action_receipt" and row.get("tool") == "click")
        self.assertTrue(action["relogin"])
