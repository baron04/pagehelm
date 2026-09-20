"""The model boundary is a contract, not a base class.

These cases pin the regression that made `evals/woa` report 22 of 36 failures at `61603fe`:
`ScriptedWoaChat(ChatModel)` never ran the provider constructor, inherited the real
`complete_cancellable`, and raised `AttributeError: max_completion_tokens` inside the turn,
which the runner then classified as `apparatus_invalid`. A boundary that is checked at
composition time cannot produce that failure at step one.
"""
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from web_operation_agent.modeling import client as M
from web_operation_agent.modeling.egress import AuditedChat
from web_operation_agent.tests.chat_port_contract import MESSAGES, ChatPortContract


REPLY = {"id": "x", "object": "chat.completion",
         "choices": [{"message": {"role": "assistant", "content": "ok"}}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _serve(handler_body, *, delay: float = 0.0, status: int = 200):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", "0")))
            if delay:
                time.sleep(delay)
            payload = json.dumps(handler_body).encode()
            try:
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class _Ledger:
    def __init__(self):
        self.events = []

    def append(self, event, **fields):
        self.events.append((event, fields))
        return {}


class _ProductDouble:
    """The shape every in-repo test double must now have: no provider inheritance."""

    model = "product-double"

    def __init__(self, replies=None):
        self.replies = list(replies or [{}])
        self.calls = 0

    def complete(self, messages, tools, run_id, max_tokens=None, options=None, timeout_s=None):
        self.calls += 1
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]

    def complete_cancellable(self, messages, tools, run_id, *, cancelled,
                             max_tokens=None, options=None, timeout_s=None):
        if cancelled():
            raise M.ProviderError("cancelled before dispatch",
                                  code="provider_cancelled", retryable=False)
        return self.complete(messages, tools, run_id, max_tokens=max_tokens,
                             options=options, timeout_s=timeout_s)


class _ChatModelCase(unittest.TestCase):
    """Shared fixture: a real ChatModel whose only fake part is the socket peer."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.servers = []

    def _chat(self, body=REPLY, *, delay=0.0, status=200, guard=None):
        server, thread = _serve(body, delay=delay, status=status)
        self.servers.append((server, thread))
        self.addCleanup(self._shutdown, server, thread)
        return M.ChatModel(
            M.SpendMeter(ledger=Path(self.directory.name) / f"spend-{len(self.servers)}.jsonl"),
            env={"AGENT_OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                 "AGENT_OPENAI_API_KEY": "key",
                 "AGENT_OPENAI_MODEL": "qwen3.8-flash"},
            runtime_guard=guard or M.ProviderRuntimeGuard(),
        )

    @staticmethod
    def _shutdown(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(2)


class RealChatModelContract(ChatPortContract, _ChatModelCase):
    def make_chat(self):
        return self._chat()

    def make_provider_error_chat(self):
        return self._chat({"error": "boom"}, status=503)

    def make_deadline_chat(self):
        return self._chat(delay=2.0)


class AuditedChatContract(ChatPortContract, _ChatModelCase):
    def make_chat(self):
        return AuditedChat(self._chat(), _Ledger())

    def make_provider_error_chat(self):
        return AuditedChat(self._chat({"error": "boom"}, status=503), _Ledger())

    def make_deadline_chat(self):
        return AuditedChat(self._chat(delay=2.0), _Ledger())


class ProductDoubleContract(ChatPortContract, unittest.TestCase):
    def make_chat(self):
        return _ProductDouble()


class TheContractIsCheckedAtCompositionTime(unittest.TestCase):
    def test_an_object_with_only_complete_is_refused_before_any_call(self):
        class OldSignature:
            model = "old"

            def complete(self, messages, tools, run_id, **kwargs):
                return {}

        with self.assertRaises(TypeError) as raised:
            M.require_chat_port(OldSignature())
        self.assertIn("complete_cancellable", str(raised.exception))

    def test_a_positional_cancellation_argument_is_refused(self):
        class PositionalHook:
            model = "positional"

            def complete(self, messages, tools, run_id, **kwargs):
                return {}

            def complete_cancellable(self, messages, tools, run_id, cancelled=None, **kwargs):
                return {}

        with self.assertRaises(TypeError) as raised:
            M.require_chat_port(PositionalHook())
        self.assertIn("keyword-only", str(raised.exception))

    def test_an_uninitialised_provider_subclass_cannot_satisfy_the_port(self):
        """The exact `61603fe` regression: inheritance without the constructor."""

        class Borrowed(M.ChatModel):
            def __init__(self):
                self.model = "borrowed"

            def complete(self, messages, tools, run_id, **kwargs):
                return {}

        borrowed = Borrowed()
        self.assertTrue(callable(getattr(borrowed, "complete_cancellable", None)))
        with self.assertRaises(TypeError) as raised:
            M.require_chat_port(borrowed)
        self.assertIn("__init__ never ran", str(raised.exception))

    def test_audited_chat_refuses_a_delegate_that_cannot_be_cancelled(self):
        class OnlyComplete:
            model = "only-complete"

            def complete(self, messages, tools, run_id, **kwargs):
                return {}

        with self.assertRaises(TypeError):
            AuditedChat(OnlyComplete(), _Ledger())

    def test_a_conforming_double_is_accepted(self):
        self.assertIs(M.require_chat_port(_ProductDouble()).model, "product-double")


class AuditedChatNeverDegradesCancellation(_ChatModelCase):
    def test_the_wrapper_forwards_the_hook_instead_of_falling_back(self):
        seen = {}

        class Recording:
            model = "recording"

            def complete(self, messages, tools, run_id, **kwargs):
                seen["plain"] = True
                return {}

            def complete_cancellable(self, messages, tools, run_id, *, cancelled, **kwargs):
                seen["hook"] = cancelled
                return {"content": "ok"}

        ledger = _Ledger()
        chat = AuditedChat(Recording(), ledger)
        hook = (lambda: False)
        chat.complete_cancellable(MESSAGES, None, "run", cancelled=hook)
        self.assertIs(seen.get("hook"), hook)
        self.assertNotIn("plain", seen)
        self.assertEqual([name for name, _ in ledger.events], ["provider_egress"])

    def test_a_dispatched_cancellation_keeps_unknown_usage_and_releases_the_slot(self):
        """Cancelling after dispatch must stay non-retryable and must not book a cost."""
        started, release = threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", "0")))
                started.set()
                release.wait(5)
                try:
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(b"{}")
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._shutdown, server, thread)
        self.addCleanup(release.set)

        ledger_path = Path(self.directory.name) / "dispatched.jsonl"
        guard = M.ProviderRuntimeGuard(max_concurrent_requests=1)
        chat = AuditedChat(M.ChatModel(
            M.SpendMeter(ledger=ledger_path),
            env={"AGENT_OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                 "AGENT_OPENAI_API_KEY": "key",
                 "AGENT_OPENAI_MODEL": "qwen3.8-flash"},
            runtime_guard=guard), _Ledger())

        cancelled, outcome = threading.Event(), []

        def call():
            try:
                chat.complete_cancellable(MESSAGES, None, "run",
                                          cancelled=cancelled.is_set, timeout_s=5)
            except Exception as exc:  # noqa: BLE001 - the assertion is the point
                outcome.append(exc)

        worker = threading.Thread(target=call)
        worker.start()
        self.assertTrue(started.wait(2))
        cancelled.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertEqual(outcome[0].code, "provider_cancelled")
        self.assertFalse(outcome[0].retryable)
        self.assertEqual(guard.status()["in_flight"], 0)
        rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
        self.assertEqual(len(rows), 1, "a cancelled dispatch is one row, never a retry")
        self.assertEqual(rows[0]["accounting_status"], "usage_unavailable")
        self.assertEqual(rows[0]["error_code"], "provider_cancelled")


if __name__ == "__main__":
    unittest.main()
