from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.egress import (AuditedChat, ProviderEgressError,
                                         ProviderEgressPolicy)
from web_operation_agent.ledger import SessionLedger


class ProviderEgressPolicyTests(unittest.TestCase):
    def test_report_classifies_data_without_copying_payload(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": (
                "goal\nPRIOR CONVERSATION CONTEXT\nprior turn\n"
                "BEGIN_UNTRUSTED_PAGE_OBSERVATION\n"
                "customer: Synthetic Acme\nEND_UNTRUSTED_PAGE_OBSERVATION")},
            {"role": "tool", "name": "query_companies", "tool_call_id": "c1",
             "content": "Synthetic Acme"},
        ]
        report = ProviderEgressPolicy().inspect(messages, [{"type": "function"}])
        projection = report.audit_projection()

        self.assertEqual(projection["message_count"], 3)
        self.assertGreater(projection["input_bytes"], 0)
        self.assertEqual(projection["tool_count"], 1)
        self.assertEqual(projection["roles"], {"system": 1, "tool": 1, "user": 1})
        self.assertIn("application_page_observation", projection["data_classes"])
        self.assertIn("application_conversation_history", projection["data_classes"])
        self.assertIn("application_tool_result", projection["data_classes"])
        self.assertFalse(projection["contains_raw_payload"])
        self.assertNotIn("Synthetic Acme", json.dumps(projection))

    def test_credentials_nonfinite_values_and_size_fail_before_dispatch(self):
        policy = ProviderEgressPolicy(max_request_bytes=1_024)
        unsafe = [
            [{"role": "user", "content": "access_token=secret-value"}],
            [{"role": "user", "content": "ok", "metadata": {"score": float("nan")}}],
            [{"role": "user", "content": "x" * 2_000}],
        ]
        for messages in unsafe:
            with self.subTest(messages=messages), self.assertRaises(ProviderEgressError):
                policy.inspect(messages, None)

    def test_message_and_tool_shapes_are_bounded(self):
        policy = ProviderEgressPolicy(max_messages=2, max_tools=1)
        with self.assertRaisesRegex(ProviderEgressError, "message count"):
            policy.inspect([{"role": "user", "content": "x"}] * 3, None)
        with self.assertRaisesRegex(ProviderEgressError, "unsupported"):
            policy.inspect([{"role": "developer", "content": "x"}], None)
        with self.assertRaisesRegex(ProviderEgressError, "tool count"):
            policy.inspect([{"role": "user", "content": "x"}], [{}, {}])


class AuditedChatTests(unittest.TestCase):
    def test_audit_contains_only_projection_and_precedes_delegate(self):
        class Delegate:
            meter = object()

            def __init__(self):
                self.calls = 0

            def complete(self, messages, tools, run_id, **kwargs):
                self.calls += 1
                return {"content": "done"}

        delegate = Delegate()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            ledger = SessionLedger(path, "egress-test")
            chat = AuditedChat(delegate, ledger)
            result = chat.complete(
                [{"role": "user", "content": "Synthetic customer record"}],
                None,
                "run-1",
            )
            disk = path.read_text(encoding="utf-8")

        self.assertEqual(result, {"content": "done"})
        self.assertEqual(delegate.calls, 1)
        self.assertIs(chat.meter, delegate.meter)
        self.assertIn('"event": "provider_egress"', disk)
        self.assertNotIn("Synthetic customer record", disk)

    def test_blocked_request_never_reaches_delegate(self):
        class Delegate:
            def __init__(self):
                self.calls = 0

            def complete(self, *_args, **_kwargs):
                self.calls += 1
                return {}

        delegate = Delegate()
        with tempfile.TemporaryDirectory() as tmp:
            chat = AuditedChat(
                delegate, SessionLedger(Path(tmp) / "events.jsonl", "egress-test"))
            with self.assertRaises(ProviderEgressError):
                chat.complete(
                    [{"role": "user", "content": "password=do-not-send"}], None, "run")
        self.assertEqual(delegate.calls, 0)


if __name__ == "__main__":
    unittest.main()
