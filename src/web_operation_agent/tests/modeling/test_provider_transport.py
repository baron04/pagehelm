"""The provider transport keeps connections it may reuse, and retries only what was never processed."""
from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from web_operation_agent.execution.budget import TurnBudget
from web_operation_agent.execution import loop
from web_operation_agent.execution.receipt import MeasuredChat, TurnReceipt
from web_operation_agent.modeling import client as M

MESSAGES = [{"role": "user", "content": "hello"}]
REPLY = json.dumps({"id": "x", "object": "chat.completion",
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()


class _Server:
    """A local provider that counts the TCP connections it accepts."""

    def __init__(self, *, http11=True, close_after_first=False, drop_response_to=None,
                 status=200, reply=REPLY):
        outer = self
        self.connections = 0
        self.requests = 0
        self.bodies = []
        self.request_ids = []
        self.idle_closed = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if http11 else "HTTP/1.0"

            def setup(self):
                super().setup()
                outer.connections += 1

            def do_POST(self):
                outer.bodies.append(json.loads(self.rfile.read(int(self.headers.get("content-length", "0")))))
                outer.requests += 1
                outer.request_ids.append(self.headers["x-woa-request-id"])
                if outer.requests == drop_response_to:
                    self.close_connection = True
                    return
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(reply)))
                if status != 200:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(reply)
                if close_after_first and outer.requests == 1:
                    # Keep-alive was promised, then the idle connection is dropped: what a
                    # provider or proxy does to a connection that sat idle too long.
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    outer.idle_closed.set()
                    self.close_connection = True

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # The stdlib's 0.5-second poll makes every fixture shutdown wait unnecessarily.
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class _ProviderCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"}
        patcher = mock.patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def chat(self, server: _Server):
        self.addCleanup(server.close)
        self.ledger = Path(self.directory.name) / f"spend-{id(server)}.jsonl"
        chat = M.ChatModel(
            M.SpendMeter(ledger=self.ledger),
            env={"AGENT_OPENAI_BASE_URL": f"http://127.0.0.1:{server.server.server_port}/v1",
                 "AGENT_OPENAI_API_KEY": "key", "AGENT_OPENAI_MODEL": "qwen3.8-flash"},
            runtime_guard=M.ProviderRuntimeGuard())
        self.addCleanup(chat.close_connections)
        return chat

    def call(self, chat):
        return chat.complete_cancellable(MESSAGES, None, "run", cancelled=lambda: False,
                                         timeout_s=5)

    def rows(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines() if line]


class ProviderTransport(_ProviderCase):

    def test_sequential_calls_share_one_connection(self):
        server = _Server()
        chat = self.call_three(server)
        self.assertEqual(server.requests, 3)
        self.assertEqual(server.connections, 1)
        self.assertEqual(len(self.rows()), 3)

    def call_three(self, server):
        chat = self.chat(server)
        for _ in range(3):
            self.assertEqual(self.call(chat)["content"], "ok")
        return chat

    def test_a_provider_that_closes_after_each_response_is_never_reused(self):
        server = _Server(http11=False)
        self.call_three(server)
        self.assertEqual(server.connections, 3)

    def test_an_idle_connection_already_closed_is_replaced_before_sending(self):
        server = _Server(close_after_first=True)
        chat = self.chat(server)
        self.call(chat)
        self.assertTrue(server.idle_closed.wait(1))
        self.call(chat)
        self.assertEqual(server.requests, 2, "the closed connection processed no second request")
        self.assertEqual(server.connections, 2)
        self.assertEqual(len(self.rows()), 2, "one ledger row per answered call")

    def test_response_lost_after_a_reused_connection_received_the_post(self):
        server = _Server(drop_response_to=2)
        chat = self.chat(server)
        self.call(chat)
        with self.assertRaises(M.ProviderError) as caught:
            self.call(chat)
        error = caught.exception
        self.assertEqual(error.code, "provider_transport")
        self.assertTrue(error.dispatched)
        self.assertFalse(error.retryable)
        self.assertEqual(server.requests, 2)
        self.assertEqual(server.connections, 1)
        self.assertEqual(error.request_id, server.request_ids[1])
        first, lost = self.rows()
        self.assertEqual(first["accounting_status"], "reported")
        self.assertEqual(lost["provider_request_id"], error.request_id)
        self.assertEqual(lost["accounting_status"], "usage_unavailable")
        for key in ("in", "out", "call_cost_cny"):
            self.assertIsNone(lost[key])

        self.call(chat)
        self.assertEqual(server.connections, 2)
        self.assertEqual(len(set(server.request_ids)), 3)

    def test_a_non_cancellable_lost_response_keeps_unknown_usage(self):
        server = _Server(drop_response_to=1)
        chat = self.chat(server)
        with self.assertRaises(M.ProviderError) as caught:
            chat.complete(MESSAGES, None, "run", timeout_s=5)
        error = caught.exception
        self.assertEqual((error.code, error.dispatched, error.retryable),
                         ("provider_transport", True, False))
        self.assertEqual(server.request_ids, [error.request_id])
        row, = self.rows()
        self.assertEqual(row["provider_request_id"], error.request_id)
        self.assertEqual(row["accounting_status"], "usage_unavailable")
        self.assertIsNone(row["in"])

    def test_a_connect_failure_before_anything_is_sent_is_retried_once(self):
        server = _Server()
        chat = self.chat(server)
        original = http.client.HTTPConnection.connect
        attempts = []

        def flaky(connection):
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionResetError("handshake reset")
            return original(connection)

        with mock.patch.object(http.client.HTTPConnection, "connect", flaky):
            self.assertEqual(self.call(chat)["content"], "ok")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(chat._transport_retries, 1)
        self.assertEqual(server.requests, 1)

        # A second consecutive failure is the caller's error, not a loop.
        with mock.patch.object(http.client.HTTPConnection, "connect",
                               side_effect=ConnectionResetError("down")):
            fresh = self.chat(_Server())
            with self.assertRaises(M.ProviderError) as caught:
                self.call(fresh)
        self.assertEqual(caught.exception.code, "provider_transport")
        self.assertFalse(caught.exception.dispatched)

    def test_an_error_status_closes_the_connection(self):
        for cancellable in (True, False):
            with self.subTest(cancellable=cancellable):
                server = _Server(status=503)
                chat = self.chat(server)
                for _ in range(2):
                    with self.assertRaises(M.ProviderError) as caught:
                        if cancellable:
                            self.call(chat)
                        else:
                            chat.complete(MESSAGES, None, "run", timeout_s=5)
                    error = caught.exception
                    self.assertTrue(error.dispatched)
                    self.assertTrue(error.retryable)
                    self.assertEqual(error.request_id, server.request_ids[-1])
                self.assertEqual(server.requests, 2)
                self.assertEqual(server.connections, 2)
                self.assertEqual(len(set(server.request_ids)), 2)
                rows = self.rows()
                self.assertEqual([row["provider_request_id"] for row in rows], server.request_ids)
                self.assertTrue(all(row["accounting_status"] == "usage_unavailable" for row in rows))

    def test_an_http_retry_keeps_dispatch_unknown_usage_and_a_new_budgeted_id(self):
        server = _Server(status=503)
        chat = self.chat(server)
        budget, receipt = TurnBudget(steps=33, seconds=120), TurnReceipt()
        measured = MeasuredChat(chat, receipt, remaining_s=budget.remaining, budget=budget)
        events = []
        session = SimpleNamespace(_turn_budget=budget, chat=measured, run_id="run",
            wall_budget_s=120, _cancel_requested=lambda: False, tool_schemas=lambda: None,
            ledger=SimpleNamespace(append=lambda kind, **row: events.append({"event": kind, **row})))
        with mock.patch.object(loop, "PROVIDER_RETRY_PAUSE_DEFAULT_S", 0):
            with self.assertRaises(M.ProviderError) as caught:
                loop._complete_once_more_if_transient(session, MESSAGES, 1, time.time())
        self.assertTrue(caught.exception.dispatched)
        self.assertEqual(budget.used, 2)
        self.assertEqual(server.requests, 2)
        self.assertEqual(len(set(server.request_ids)), 2)
        retry, = events
        self.assertEqual((retry["event"], retry["dispatched"], retry["accounting_status"]),
                         ("provider_retry", True, "usage_unavailable"))
        self.assertEqual(retry["provider_request_id"], server.request_ids[0])
        self.assertEqual(caught.exception.request_id, server.request_ids[1])
        self.assertEqual([row["provider_request_id"] for row in self.rows()], server.request_ids)
        self.assertTrue(all(row["accounting_status"] == "usage_unavailable" for row in self.rows()))


if __name__ == "__main__":
    unittest.main()
