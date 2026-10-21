"""A model call that failed in a way a repeat may recover is repeated once, on the record.

The provider client makes no hidden retries: its circuit breaker counts every failure. The
turn is where a retry is visible, so the loop repeats a transport failure, a rate limit or
an unavailable provider once, writes a `provider_retry` event, and otherwise ends the turn
exactly as before.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.execution import loop
from web_operation_agent.modeling.provider import ProviderError
from web_operation_agent.tests.support.execution import (_Browser, _Chat, _Control, _obs,
                                                         _session)


class _Failing(_Chat):
    """Fail the calls named in `failures` (1-based) with the given code, answer the rest."""

    def __init__(self, script, failures: dict[int, str], *, dispatched=True):
        super().__init__(script)
        self.failures = dict(failures)
        self.dispatched = dispatched
        self.calls = 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        code = self.failures.get(self.calls)
        if code is not None:
            raise ProviderError(f"synthetic {code}", code=code,
                                retryable=code != "provider_authentication",
                                request_id="b" * 32, dispatched=self.dispatched)
        return super().complete(*args, **kwargs)


class ProviderRetry(unittest.TestCase):
    def setUp(self):
        # The real pauses protect a live provider, not this contract; what matters here is
        # that the retry happens, is recorded, and stays inside its caps.
        self._pauses = (loop.PROVIDER_RETRY_PAUSE_RATE_LIMITED_S,
                        loop.PROVIDER_RETRY_PAUSE_DEFAULT_S)
        loop.PROVIDER_RETRY_PAUSE_RATE_LIMITED_S = 0.0
        loop.PROVIDER_RETRY_PAUSE_DEFAULT_S = 0.0

    def tearDown(self):
        (loop.PROVIDER_RETRY_PAUSE_RATE_LIMITED_S,
         loop.PROVIDER_RETRY_PAUSE_DEFAULT_S) = self._pauses

    def _run(self, chat, **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(tmp, chat, _Browser([_obs()]), _Control([[]]))
            for key, value in overrides.items():
                setattr(session, key, value)
            result = session.run()
            rows = [json.loads(line) for line in
                    (Path(tmp) / "l.jsonl").read_text().splitlines()]
        return result, [row for row in rows
                        if row.get("event") in {"provider_retry", "provider_failed"}]

    def test_a_transient_failure_is_retried_once_on_the_record(self):
        for code in sorted(loop.RETRYABLE_PROVIDER_CODES):
            with self.subTest(code=code):
                chat = _Failing([("finish", {"done": False, "why": "stop"})], {1: code})
                result, events = self._run(chat)
                self.assertEqual(chat.calls, 2)
                self.assertNotEqual(result.terminal_reason, "provider_error")
                self.assertEqual([e["event"] for e in events], ["provider_retry"])
                self.assertEqual(events[0]["provider_error_code"], code)
                self.assertEqual(events[0]["accounting_status"], "usage_unavailable")

    def test_a_second_consecutive_failure_ends_the_turn(self):
        chat = _Failing([], {1: "provider_unavailable", 2: "provider_unavailable"})
        result, events = self._run(chat)
        self.assertEqual(chat.calls, 2)
        self.assertEqual(result.terminal_reason, "provider_error")
        self.assertEqual([e["event"] for e in events], ["provider_retry", "provider_failed"])

    def test_retries_are_capped_per_turn(self):
        script = [("observe", {}), ("observe", {}), ("finish", {"done": False, "why": "x"})]
        chat = _Failing(script, {1: "provider_transport", 3: "provider_transport",
                                 5: "provider_transport"})
        result, events = self._run(chat)
        self.assertEqual(result.terminal_reason, "provider_error")
        self.assertEqual([e["event"] for e in events].count("provider_retry"),
                         loop.MAX_PROVIDER_RETRIES_PER_TURN)

    def test_other_failures_are_not_retried(self):
        for code in ("provider_authentication", "provider_http", "provider_circuit_open",
                     "provider_response"):
            with self.subTest(code=code):
                chat = _Failing([], {1: code})
                result, events = self._run(chat)
                self.assertEqual(chat.calls, 1)
                self.assertEqual(result.terminal_reason, "provider_error")
                self.assertNotIn("provider_retry", [e["event"] for e in events])

    def test_no_retry_without_enough_wall_budget_left(self):
        chat = _Failing([], {1: "provider_unavailable"})
        result, events = self._run(chat, wall_budget_s=loop.PROVIDER_RETRY_MIN_REMAINING_S - 1)
        self.assertEqual(chat.calls, 1)
        self.assertNotIn("provider_retry", [e["event"] for e in events])

    def test_a_never_dispatched_failure_is_recorded_as_not_dispatched(self):
        chat = _Failing([("finish", {"done": False, "why": "stop"})],
                        {1: "provider_transport"}, dispatched=False)
        _result, events = self._run(chat)
        self.assertEqual(events[0]["accounting_status"], "not_dispatched")


if __name__ == "__main__":
    unittest.main()
