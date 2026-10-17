"""The per-turn record keeps what reached the model and nothing the egress check refused."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from web_operation_agent.execution.turn_trace import TurnTrace, prune_turn_traces
from web_operation_agent.modeling.egress import AuditedChat, ProviderEgressError
from web_operation_agent.privacy import SessionHandoffPolicy
from web_operation_agent.tests.support.chat_port_contract import CancellableChatDouble


class _Ledger:
    def __init__(self):
        self.events = []

    def append(self, event, **fields):
        self.events.append((event, fields))


class _Reply(CancellableChatDouble):
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def complete(self, messages, tools, run_id, **kwargs):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


TOOLS = [{"type": "function", "function": {"name": "finish", "parameters": {}}}]


class TurnTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.handoffs = SessionHandoffPolicy.extending(("ticket",))

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, trace, **overrides):
        fields = dict(run_id="run-1", turn_id="m-1", prompt="Grace Hopper email?",
                      start_route="/contacts", terminal_reason="agent_finished",
                      wall_seconds=1.5, answer="grace@example.test")
        fields.update(overrides)
        return trace.write(**fields)

    def test_requests_are_kept_once_per_message_with_tools_and_reply(self):
        trace = TurnTrace(self.root / "turns", self.handoffs)
        chat = trace.chat(_Reply({"content": "", "tool_calls": [{"id": "c1", "function": {
            "name": "finish", "arguments": "{}"}}]}, {"content": "done"}))
        system = {"role": "system", "content": "rules"}
        chat.complete_cancellable([system, {"role": "user", "content": "q"}], TOOLS, "run-1",
                                  cancelled=lambda: False)
        chat.complete([system, {"role": "user", "content": "q"}, {"role": "tool", "content": "row"}],
                      None, "run-1")
        record = json.loads(self._write(trace).read_text())

        self.assertEqual([system, {"role": "user", "content": "q"}, {"role": "tool", "content": "row"}],
                         record["messages"])
        self.assertEqual([[0, 1], [0, 1, 2]], [call["messages"] for call in record["calls"]])
        self.assertEqual(["finish"], record["calls"][0]["tools"])
        self.assertEqual("finish", record["calls"][0]["reply"]["tool_calls"][0]["function"]["name"])
        self.assertEqual("done", record["calls"][1]["reply"]["content"])
        self.assertEqual(("agent_finished", "/contacts", "Grace Hopper email?"),
                         (record["terminal_reason"], record["start_route"], record["prompt"]))

    def test_a_refused_request_keeps_only_its_error(self):
        trace = TurnTrace(self.root / "turns", self.handoffs)
        chat = trace.chat(_Reply(RuntimeError("provider refused")))
        with self.assertRaises(RuntimeError):
            chat.complete([{"role": "user", "content": "ticket=abc123"}], None, "run-1")
        record = json.loads(self._write(trace).read_text())
        self.assertEqual([{"error": "RuntimeError", "seconds": record["calls"][0]["seconds"]}],
                         record["calls"])
        self.assertNotIn("abc123", json.dumps(record))

    def test_a_request_the_egress_check_blocks_never_reaches_the_record(self):
        trace = TurnTrace(self.root / "turns", self.handoffs)
        delegate = _Reply({"content": "never"})
        chat = AuditedChat(trace.chat(delegate), _Ledger())
        with self.assertRaises(ProviderEgressError):
            chat.complete([{"role": "user", "content": "password=hunter2"}], None, "run-1")
        self.assertEqual((0, []), (delegate.calls, trace.calls))

    def test_credentials_in_the_turn_fields_are_redacted(self):
        trace = TurnTrace(self.root / "turns", self.handoffs)
        text = json.dumps(json.loads(self._write(
            trace, prompt="open ?ticket=abc123 then password=hunter2",
            start_route="/contacts?ticket=abc123", answer="token=s3cr3tvalue",
            steps=("filled password=hunter2",)).read_text()))
        for secret in ("abc123", "hunter2", "s3cr3tvalue"):
            self.assertNotIn(secret, text)

    def test_a_record_still_holding_a_credential_keeps_only_the_outcome(self):
        trace = TurnTrace(self.root / "turns", self.handoffs)
        trace.record([{"role": "tool", "content": {"password": "hunter2"}}], None, {"content": "x"}, 0.1)
        record = json.loads(self._write(trace).read_text())
        self.assertEqual("credential_form", record["withheld"])
        self.assertNotIn("hunter2", json.dumps(record))
        self.assertEqual({"schema_version", "run_id", "turn_id", "terminal_reason", "wall_seconds",
                          "withheld"}, set(record))

    def test_records_are_numbered_in_order_and_private(self):
        first = self._write(TurnTrace(self.root / "turns", self.handoffs))
        second = self._write(TurnTrace(self.root / "turns", self.handoffs), run_id="run-2")
        self.assertEqual(("0001.json", "0002.json"), (first.name, second.name))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(second).st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(second.parent).st_mode))

    def test_prune_removes_only_old_turn_records(self):
        session = self.root / "runs" / "20261001-000000-abcd"
        (session / "turns").mkdir(parents=True)
        old, new = session / "turns" / "0001.json", session / "turns" / "0002.json"
        audit, other = session / "audit.jsonl", session / "turns" / "notes.json"
        for path in (old, new, audit, other):
            path.write_text("{}")
        long_ago = time.time() - 20 * 86400
        for path in (old, audit, other):
            os.utime(path, (long_ago, long_ago))
        self.assertEqual(1, prune_turn_traces(self.root / "runs", 14))
        self.assertEqual((False, True, True, True),
                         (old.exists(), new.exists(), audit.exists(), other.exists()))


if __name__ == "__main__":
    unittest.main()
