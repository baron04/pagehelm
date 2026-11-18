"""The provider transport keeps connections it may reuse, and retries only what was never processed."""
from __future__ import annotations

import base64
import http.client
import json
import select
import shutil
import socket
import ssl
import subprocess
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
                 status=200, reply=REPLY, ssl_context=None):
        outer = self
        self.connections = 0
        self.requests = 0
        self.bodies = []
        self.request_ids = []
        self.proxy_authorizations = []
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
                outer.proxy_authorizations.append(self.headers.get("proxy-authorization"))
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
        # A keep-alive handler idles on its client socket; clients close first,
        # so never join handlers here (they are daemon threads anyway).
        self.server.block_on_close = False
        if ssl_context is not None:
            self.server.socket = ssl_context.wrap_socket(self.server.socket, server_side=True)
        scheme = "https" if ssl_context is not None else "http"
        self.url = f"{scheme}://127.0.0.1:{self.server.server_port}/v1"
        # The stdlib's 0.5-second poll makes every fixture shutdown wait unnecessarily.
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class _HeldServer:
    """A provider that parks each POST until told to answer that one caller.

    The reply echoes the message marker, so a test proves each response reached
    the call that sent the matching request.
    """

    def __init__(self):
        outer = self
        self.lock = threading.Lock()
        self.posts = 0
        self.connections = 0
        self.received = {}
        self.release = {}
        self.all_arrived = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                with outer.lock:
                    outer.connections += 1

            def do_POST(self):
                body = json.loads(
                    self.rfile.read(int(self.headers.get("content-length", "0"))))
                marker = body["messages"][-1]["content"]
                with outer.lock:
                    outer.posts += 1
                    outer.received[marker] = self.headers["x-woa-request-id"]
                    event = outer.release.setdefault(marker, threading.Event())
                    if len(outer.received) == 2:
                        outer.all_arrived.set()
                event.wait(10)
                reply = json.dumps(
                    {"id": "x", "object": "chat.completion",
                     "choices": [{"message": {"role": "assistant",
                                              "content": f"reply-{marker}"}}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
                try:
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(reply)))
                    self.end_headers()
                    self.wfile.write(reply)
                    self.wfile.flush()
                except OSError:
                    pass  # a cancelled caller's socket is already dead

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        # A cancelled caller resets its socket while a handler may still be
        # parked or reading; the reset is expected, not a server fault.
        self.server.handle_error = lambda *_args: None
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True)
        self.thread.start()

    def release_marker(self, marker: str) -> None:
        with self.lock:
            self.release.setdefault(marker, threading.Event()).set()

    def close(self):
        with self.lock:
            for event in self.release.values():
                event.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class _ConnectProxy:
    """A loopback-only HTTP CONNECT relay that records every tunnel request.

    The relay sees the CONNECT line and its headers in clear text; everything it
    forwards afterwards is the client's TLS stream, which it cannot inspect.
    """

    def __init__(self):
        self.request_lines = []
        self.authorizations = []
        self._done = threading.Event()
        self._sockets = []
        self._threads = []
        self._lock = threading.Lock()
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self._done.is_set():
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            if self._done.is_set():
                conn.close()
                return
            with self._lock:
                self._sockets.append(conn)
            handler = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            with self._lock:
                self._threads.append(handler)
            handler.start()

    def _handle(self, conn):
        upstream = None
        try:
            conn.settimeout(15)
            head = b""
            while b"\r\n\r\n" not in head and len(head) <= 16384:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            if b"\r\n\r\n" not in head:
                return
            request, _, rest = head.partition(b"\r\n\r\n")
            lines = request.decode("latin-1").split("\r\n")
            headers = {}
            for line in lines[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            self.request_lines.append(lines[0])
            self.authorizations.append(headers.get("proxy-authorization"))
            host, _, port = lines[0].split(" ")[1].rpartition(":")
            if host not in ("127.0.0.1", "localhost", "::1"):
                return
            upstream = socket.create_connection((host, int(port)), timeout=15)
            upstream.settimeout(15)
            with self._lock:
                self._sockets.append(upstream)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            self._relay(conn, upstream)
        except (OSError, ValueError, IndexError):
            pass
        finally:
            conn.close()
            if upstream is not None:
                upstream.close()

    def _relay(self, client, upstream):
        pair = {client: upstream, upstream: client}
        while not self._done.is_set():
            try:
                readable, _, _ = select.select(list(pair), [], [], 5)
            except (OSError, ValueError):
                return
            for source in readable:
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    pair[source].sendall(data)
                except OSError:
                    return

    def close(self):
        self._done.set()
        try:
            # A blocked accept() does not notice close() on this platform;
            # one self-connection lets it observe _done and return.
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            pass
        self.listener.close()
        with self._lock:
            sockets = list(self._sockets)
            threads = list(self._threads)
        for conn in sockets:
            conn.close()
        for thread in threads:
            thread.join(2)
        self.thread.join(2)


class _ProviderCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"}
        patcher = mock.patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def chat(self, server):
        self.addCleanup(server.close)
        self.ledger = Path(self.directory.name) / f"spend-{id(server)}.jsonl"
        chat = M.ChatModel(
            M.SpendMeter(ledger=self.ledger),
            env={"AGENT_OPENAI_BASE_URL": server.url,
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


class ProviderConcurrency(_ProviderCase):
    """Two calls in flight on one ChatModel share nothing but the connection cache."""

    def _run_pair(self, server, chat):
        outcomes = {}

        def call(tag, cancelled):
            try:
                outcomes[tag] = chat.complete_cancellable(
                    [{"role": "user", "content": tag}], None, f"run-{tag}",
                    cancelled=cancelled, timeout_s=15)
            except M.ProviderError as error:
                outcomes[tag] = error

        cancel_a = threading.Event()
        a = threading.Thread(target=call, args=("A", cancel_a.is_set), daemon=True)
        b = threading.Thread(target=call, args=("B", lambda: False), daemon=True)
        a.start()
        b.start()
        self.assertTrue(server.all_arrived.wait(10), "both POSTs were in flight")
        return outcomes, cancel_a, a, b

    def _idle_connections(self, chat):
        cache = chat._connection_cache()
        with cache._lock:
            return [c for entries in cache._idle.values() for c, _ in entries]

    def test_two_in_flight_calls_each_get_their_own_response(self):
        server = _HeldServer()
        chat = self.chat(server)
        outcomes, _cancel_a, a, b = self._run_pair(server, chat)
        server.release_marker("A")
        server.release_marker("B")
        for thread in (a, b):
            thread.join(10)
            self.assertFalse(thread.is_alive())

        self.assertEqual(server.posts, 2, "each call dispatched exactly one physical POST")
        self.assertEqual(set(server.received), {"A", "B"})
        self.assertNotEqual(server.received["A"], server.received["B"])
        for tag in ("A", "B"):
            self.assertEqual(outcomes[tag]["content"], f"reply-{tag}")
        rows = {row["provider_request_id"]: row for row in self.rows()}
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(set(rows), {server.received["A"], server.received["B"]})
        for tag in ("A", "B"):
            self.assertEqual(rows[server.received[tag]]["accounting_status"], "reported")
            self.assertEqual(rows[server.received[tag]]["logical_run_id"], f"run-{tag}")
        self.assertEqual(server.connections, 2)
        self.assertEqual(len(self._idle_connections(chat)), 2)
        self.assertEqual(chat.meter.calls, 2)
        self.assertEqual(chat.meter.unmetered_calls, 0)

    def test_cancelling_one_of_two_in_flight_calls_leaves_the_other_untouched(self):
        server = _HeldServer()
        chat = self.chat(server)
        outcomes, cancel_a, a, b = self._run_pair(server, chat)
        cancel_a.set()
        a.join(10)
        self.assertFalse(a.is_alive(), "the cancelled call did not return")
        # B is still parked on the server while A is already dead.
        server.release_marker("B")
        b.join(10)
        self.assertFalse(b.is_alive(), "the surviving call did not return")

        # Each call sent one physical POST; the reply reached its own caller.
        self.assertEqual(server.posts, 2)
        self.assertEqual(set(server.received), {"A", "B"})
        self.assertNotEqual(server.received["A"], server.received["B"])
        self.assertEqual(outcomes["B"]["content"], "reply-B")

        # The cancelled call keeps its dispatch state and unknown usage.
        error = outcomes["A"]
        self.assertIsInstance(error, M.ProviderError)
        self.assertEqual((error.code, error.dispatched, error.retryable),
                         ("provider_cancelled", True, False))
        self.assertEqual(error.request_id, server.received["A"])
        rows = {row["provider_request_id"]: row for row in self.rows()}
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(set(rows), {server.received["A"], server.received["B"]})
        self.assertEqual(chat.meter.calls, 2)
        self.assertEqual(chat.meter.unmetered_calls, 1)
        cancelled = rows[server.received["A"]]
        self.assertEqual(cancelled["accounting_status"], "usage_unavailable")
        self.assertEqual(cancelled["error_code"], "provider_cancelled")
        self.assertEqual(cancelled["logical_run_id"], "run-A")
        for key in ("in", "out", "cached", "reasoning", "call_cost_cny"):
            self.assertIsNone(cancelled[key])
        self.assertEqual(rows[server.received["B"]]["accounting_status"], "reported")

        # Two in-flight calls needed two sockets; the cancelled one was closed,
        # the answered one went back to the cache and serves the next call.
        self.assertEqual(server.connections, 2)
        self.assertEqual(chat._transport_retries, 0)
        self.assertEqual(len(self._idle_connections(chat)), 1)
        server.release_marker("C")
        follow_up = chat.complete_cancellable(
            [{"role": "user", "content": "C"}], None, "run-C",
            cancelled=lambda: False, timeout_s=15)
        self.assertEqual(follow_up["content"], "reply-C")
        self.assertEqual(server.posts, 3)
        self.assertEqual(server.connections, 2,
                         "the surviving connection was reused for the next call")
        idle = self._idle_connections(chat)
        self.assertEqual(len(idle), 1)
        chat.close_connections()
        self.assertTrue(all(connection.sock is None for connection in idle))
        self.assertEqual(len(self._idle_connections(chat)), 0)

    def test_a_cancel_hook_that_outlives_its_call_cannot_kill_the_next_call(self):
        # The watcher thread polls the caller's hook; a hook that blocks keeps the
        # watcher alive past the call's own cleanup. A connection must not be
        # cached while its watcher can still close it under the next caller.
        server = _HeldServer()
        chat = self.chat(server)
        entered, gate = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        def sticky_cancel():
            if threading.current_thread().name == "woa-provider-cancel":
                entered.set()
                return gate.wait(30)
            return False

        first = {}
        a = threading.Thread(
            target=lambda: first.__setitem__(
                "A", chat.complete_cancellable(
                    [{"role": "user", "content": "A"}], None, "run-A",
                    cancelled=sticky_cancel, timeout_s=15)),
            daemon=True)
        a.start()
        self.assertTrue(entered.wait(10), "the watcher parked inside the cancel hook")
        server.release_marker("A")
        a.join(10)
        self.assertFalse(a.is_alive())
        self.assertEqual(first["A"]["content"], "reply-A")
        self.assertEqual(len(self._idle_connections(chat)), 0,
                         "a connection whose watcher is still live is not cached")
        self.assertEqual(server.connections, 1)

        # The stale watcher now wakes and closes its own dead connection; the next
        # call gets a fresh socket and is unaffected.
        gate.set()
        server.release_marker("B")
        second = chat.complete_cancellable(
            [{"role": "user", "content": "B"}], None, "run-B",
            cancelled=lambda: False, timeout_s=15)
        self.assertEqual(second["content"], "reply-B")
        self.assertEqual(server.posts, 2)
        self.assertEqual(server.connections, 2)

    def test_multiple_models_sharing_guard_and_pool_enforce_process_concurrency_and_reuse(self):
        server = _HeldServer()
        self.addCleanup(server.close)
        ledger = Path(self.directory.name) / "shared-spend.jsonl"
        guard = M.ProviderRuntimeGuard(max_concurrent_requests=1)
        pool = M.ProviderConnectionPool(max_idle=4)
        self.addCleanup(pool.close_all)
        env = {"AGENT_OPENAI_BASE_URL": server.url,
               "AGENT_OPENAI_API_KEY": "key", "AGENT_OPENAI_MODEL": "qwen3.8-flash"}
        chat_a = M.ChatModel(M.SpendMeter(ledger=ledger), env=env,
                             runtime_guard=guard, connection_pool=pool)
        chat_b = M.ChatModel(M.SpendMeter(ledger=ledger), env=env,
                             runtime_guard=guard, connection_pool=pool)

        # Sequential calls across two ChatModel instances reuse the shared pool connection.
        server.release_marker("seq-1")
        self.assertEqual(chat_a.complete_cancellable(
            [{"role": "user", "content": "seq-1"}], None, "r1", cancelled=lambda: False, timeout_s=5)["content"],
            "reply-seq-1")
        # Closing one session's ChatModel does not tear down the shared pool.
        chat_a.close_connections()
        server.release_marker("seq-2")
        self.assertEqual(chat_b.complete_cancellable(
            [{"role": "user", "content": "seq-2"}], None, "r2", cancelled=lambda: False, timeout_s=5)["content"],
            "reply-seq-2")
        self.assertEqual(server.connections, 1)

        # Simultaneous calls across two ChatModel instances sharing one guard serialize at limit=1.
        results = {}
        first_arrived = threading.Event()
        orig_release = server.release_marker

        def call_a():
            results["A"] = chat_a.complete_cancellable(
                [{"role": "user", "content": "conc-A"}], None, "rA", cancelled=lambda: False, timeout_s=5)

        def call_b():
            try:
                results["B"] = chat_b.complete_cancellable(
                    [{"role": "user", "content": "conc-B"}], None, "rB", cancelled=lambda: False, timeout_s=0.1)
            except M.ProviderError as exc:
                results["B"] = exc

        ta = threading.Thread(target=call_a, daemon=True)
        ta.start()
        deadline = time.monotonic() + 5
        while "conc-A" not in server.received and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("conc-A", server.received)
        first_arrived.set()
        tb = threading.Thread(target=call_b, daemon=True)
        tb.start()
        tb.join(5)
        self.assertIsInstance(results["B"], M.ProviderError)
        self.assertEqual(results["B"].code, "provider_capacity")
        self.assertFalse(results["B"].dispatched)
        orig_release("conc-A")
        ta.join(5)
        self.assertEqual(results["A"]["content"], "reply-conc-A")
        pool.close_all()


class ProviderTransportTLS(_ProviderCase):
    """Real TLS and CONNECT-proxy transport for the provider client.

    Certificates and keys are minted into a temporary directory; verification is
    never disabled. The client only trusts the fixture CA through SSL_CERT_FILE,
    so a certificate from any other authority must still be rejected.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        openssl = shutil.which("openssl")
        if not openssl:
            raise unittest.SkipTest("openssl is required to mint temporary certificates")
        cls.certificates = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.certificates.cleanup)
        directory = Path(cls.certificates.name)

        def mint(*args):
            subprocess.run((openssl, *args), cwd=directory, check=True,
                           capture_output=True)

        try:
            mint("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                 "-subj", "/CN=woa-test-ca",
                 "-addext", "basicConstraints=critical,CA:TRUE",
                 "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                 "-keyout", "ca.key", "-out", "ca.pem")
            mint("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                 "-subj", "/CN=woa-other-ca",
                 "-addext", "basicConstraints=critical,CA:TRUE",
                 "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                 "-keyout", "other.key", "-out", "other.pem")
            mint("req", "-new", "-newkey", "rsa:2048", "-nodes",
                 "-subj", "/CN=127.0.0.1", "-keyout", "server.key", "-out", "server.csr")
            (directory / "san.cnf").write_text(
                "subjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n")
            mint("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                 "-CAcreateserial", "-days", "2", "-extfile", "san.cnf", "-out", "server.pem")
            mint("req", "-new", "-newkey", "rsa:2048", "-nodes",
                 "-subj", "/CN=127.0.0.2", "-keyout", "wrong.key", "-out", "wrong.csr")
            (directory / "wrong.cnf").write_text(
                "subjectAltName=IP:127.0.0.2\nbasicConstraints=critical,CA:FALSE\n")
            mint("x509", "-req", "-in", "wrong.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                 "-CAcreateserial", "-days", "2", "-extfile", "wrong.cnf", "-out", "wrong.pem")
        except subprocess.CalledProcessError as exc:
            raise unittest.SkipTest(
                f"openssl could not mint test certificates: {exc.stderr!r}")
        cls.ca_pem = directory / "ca.pem"
        cls.other_pem = directory / "other.pem"
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(directory / "server.pem", directory / "server.key")
        cls.wrong_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.wrong_context.load_cert_chain(directory / "wrong.pem", directory / "wrong.key")

    def setUp(self):
        super().setUp()
        # Trust only the fixture CA: default verification still runs, so a chain
        # signed by anything else must fail like a real MITM would.
        self.trust(self.ca_pem)

    def trust(self, pem: Path) -> None:
        patcher = mock.patch.dict("os.environ", {"SSL_CERT_FILE": str(pem)})
        patcher.start()
        self.addCleanup(patcher.stop)
        try:
            import certifi
        except ImportError:
            return
        # The non-cancellable path trusts certifi.where() instead of SSL_CERT_FILE.
        patcher = mock.patch.object(certifi, "where", return_value=str(pem))
        patcher.start()
        self.addCleanup(patcher.stop)

    def proxied(self, proxy: _ConnectProxy) -> None:
        self.addCleanup(proxy.close)
        url = f"http://proxyuser:p%40ss@127.0.0.1:{proxy.port}"
        patcher = mock.patch.dict("os.environ", {
            "https_proxy": url, "HTTPS_PROXY": url,
            "no_proxy": "bypass.invalid", "NO_PROXY": "bypass.invalid"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_https_calls_share_one_verified_connection(self):
        server = _Server(ssl_context=self.server_context)
        chat = self.chat(server)
        for _ in range(3):
            self.assertEqual(self.call(chat)["content"], "ok")
        self.assertEqual(server.requests, 3)
        self.assertEqual(server.connections, 1)
        self.assertEqual(len(set(server.request_ids)), 3)
        self.assertEqual(chat._transport_retries, 0)
        self.assertEqual(len(self.rows()), 3)

    def test_a_non_cancellable_call_verifies_the_same_certificate(self):
        server = _Server(ssl_context=self.server_context)
        chat = self.chat(server)
        message = chat.complete(MESSAGES, None, "run", timeout_s=5)
        self.assertEqual(message["content"], "ok")
        self.assertEqual(server.requests, 1)
        self.assertEqual(server.connections, 1)
        row, = self.rows()
        self.assertEqual(row["accounting_status"], "reported")

    def test_a_certificate_for_a_different_host_is_rejected(self):
        server = _Server(ssl_context=self.wrong_context)
        chat = self.chat(server)
        with self.assertRaises(M.ProviderError) as caught:
            self.call(chat)
        error = caught.exception
        self.assertEqual(error.code, "provider_transport")
        self.assertFalse(error.dispatched)
        self.assertEqual(server.connections, 0, "hostname mismatch must fail the handshake")
        self.assertEqual(server.requests, 0)

    def test_a_certificate_from_an_unknown_authority_is_rejected(self):
        server = _Server(ssl_context=self.server_context)
        chat = self.chat(server)
        self.trust(self.other_pem)
        with self.assertRaises(M.ProviderError) as caught:
            self.call(chat)
        error = caught.exception
        self.assertEqual(error.code, "provider_transport")
        self.assertFalse(error.dispatched)
        self.assertEqual(error.request_id, "")
        self.assertEqual(chat._transport_retries, 1)
        with self.assertRaises(M.ProviderError) as caught:
            chat.complete(MESSAGES, None, "run", timeout_s=5)
        self.assertEqual(caught.exception.code, "provider_transport")
        self.assertEqual(server.connections, 0, "no TLS handshake ever completed")
        self.assertEqual(server.requests, 0)
        # urlopen cannot prove zero bytes were sent, so its failed leg is
        # conservatively booked as dispatched with unknown usage.
        row, = self.rows()
        self.assertEqual(row["accounting_status"], "usage_unavailable")

    def test_https_calls_share_one_tunnel_through_the_proxy(self):
        proxy = _ConnectProxy()
        server = _Server(ssl_context=self.server_context)
        chat = self.chat(server)
        self.proxied(proxy)
        for _ in range(3):
            self.assertEqual(self.call(chat)["content"], "ok")
        self.assertEqual(server.requests, 3)
        self.assertEqual(server.connections, 1)
        self.assertEqual(len(set(server.request_ids)), 3)
        port = server.server.server_port
        self.assertEqual(proxy.request_lines, [f"CONNECT 127.0.0.1:{port} HTTP/1.1"])

    def test_proxy_credentials_stop_at_the_tunnel(self):
        proxy = _ConnectProxy()
        server = _Server(ssl_context=self.server_context)
        chat = self.chat(server)
        self.proxied(proxy)
        self.assertEqual(self.call(chat)["content"], "ok")
        message = chat.complete(MESSAGES, None, "run", timeout_s=5)
        self.assertEqual(message["content"], "ok")
        expected = "Basic " + base64.b64encode(b"proxyuser:p@ss").decode()
        port = server.server.server_port
        self.assertEqual(proxy.request_lines, [f"CONNECT 127.0.0.1:{port} HTTP/1.1"] * 2)
        self.assertEqual(proxy.authorizations, [expected, expected],
                         "the CONNECT request carries the proxy credential")
        self.assertEqual(server.proxy_authorizations, [None, None],
                         "Proxy-Authorization must not cross the tunnel to the provider")

    def test_a_response_lost_inside_the_tunnel_is_not_resent(self):
        proxy = _ConnectProxy()
        server = _Server(ssl_context=self.server_context, drop_response_to=1)
        chat = self.chat(server)
        self.proxied(proxy)
        with self.assertRaises(M.ProviderError) as caught:
            self.call(chat)
        error = caught.exception
        self.assertEqual((error.code, error.dispatched, error.retryable),
                         ("provider_transport", True, False))
        self.assertEqual(server.requests, 1, "the dispatched POST was not resent")
        self.assertEqual(server.request_ids, [error.request_id])
        lost, = self.rows()
        self.assertEqual(lost["provider_request_id"], error.request_id)
        self.assertEqual(lost["accounting_status"], "usage_unavailable")
        self.assertEqual(self.call(chat)["content"], "ok")
        self.assertEqual(server.requests, 2)
        self.assertEqual(len(proxy.request_lines), 2, "a lost response cost a new tunnel")
        self.assertEqual(server.connections, 2)
        self.assertEqual(len(set(server.request_ids)), 2)
        self.assertEqual(len(self.rows()), 2)

    def test_a_response_lost_over_verified_tls_is_not_resent(self):
        server = _Server(ssl_context=self.server_context, drop_response_to=1)
        chat = self.chat(server)
        with self.assertRaises(M.ProviderError) as caught:
            self.call(chat)
        error = caught.exception
        self.assertEqual((error.code, error.dispatched, error.retryable),
                         ("provider_transport", True, False))
        self.assertEqual(server.requests, 1)
        self.assertEqual(server.request_ids, [error.request_id])
        lost, = self.rows()
        self.assertEqual(lost["provider_request_id"], error.request_id)
        self.assertEqual(lost["accounting_status"], "usage_unavailable")
        self.assertEqual(self.call(chat)["content"], "ok")
        self.assertEqual(server.requests, 2)
        self.assertEqual(server.connections, 2)
        self.assertEqual(len(set(server.request_ids)), 2)
        self.assertEqual(len(self.rows()), 2)


if __name__ == "__main__":
    unittest.main()
