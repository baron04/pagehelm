"""Per-model request options.

Some providers bound answer and reasoning tokens separately. Reasoning allowances therefore
live in policy data keyed by model name rather than in model-specific code branches.
"""
import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent import model as M


class Policy(unittest.TestCase):
    def test_an_unknown_model_is_sent_nothing_extra(self):
        """An option a provider does not understand is a 400, so absence means silence."""
        self.assertEqual(M.options_for("some-model-we-have-never-run"), {})

    def test_the_default_price_model_has_no_extra_request_options(self):
        """A price-table default does not imply provider-specific request options."""
        self.assertEqual(M.options_for("MiniMax-M3"), {})

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
        raw = json.loads((Path(M.__file__).parent / "policies" / "model_options.json").read_text())
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
           "AGENT_OPENAI_MODEL_PRO": "MiniMax-M3"}

    @staticmethod
    def _response(payload):
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        response.__enter__ = lambda stream=response: stream
        response.__exit__ = lambda stream=response, *_args: None
        return response

    def test_framework_native_body_is_not_rewritten_and_full_response_is_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            meter = M.SpendMeter(ledger=Path(directory) / "spend.jsonl")
            chat = M.ChatModel(meter, env=self.ENV)
            body = {
                "model": "MiniMax-M3",
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
                {"model": "MiniMax-M3", "messages": messages, "stream": True},
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
                "model": "MiniMax-M3",
                "messages": [{"role": "user", "content": "test"}],
                "metadata": {"api_key": "must-not-leave"},
            }
            with patch.object(M.urllib.request, "urlopen") as opened:
                with self.assertRaises(ValueError):
                    chat.request_completion(body, "native-run")
            opened.assert_not_called()


class ProviderRuntimeProtection(unittest.TestCase):
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
            guard.record_failure(transient=True)
            guard.release()
        self.assertTrue(guard.status()["circuit_open"])
        self.assertEqual(guard.status()["retry_after_s"], 5)
        with self.assertRaises(M.ProviderError) as caught:
            guard.acquire(1)
        self.assertEqual(caught.exception.code, "provider_circuit_open")
        now[0] += 5
        guard.acquire(1)
        guard.record_success()
        guard.release()
        self.assertFalse(guard.status()["circuit_open"])
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
                     "AGENT_OPENAI_MODEL_PRO": "MiniMax-M3"},
                runtime_guard=guard,
            )

            def unavailable(request, timeout):
                raise M.urllib.error.HTTPError(
                    request.full_url, 503, "unavailable", {}, io.BytesIO(b"ignored"))

            with patch.object(M.urllib.request, "urlopen", side_effect=unavailable) as opened:
                for _ in range(2):
                    with self.assertRaises(M.ProviderError) as caught:
                        chat.complete([{"role": "user", "content": "test"}], None, "run")
                    self.assertEqual(caught.exception.code, "provider_http")
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
                     "AGENT_OPENAI_MODEL_PRO": "MiniMax-M3"},
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
                     "AGENT_OPENAI_MODEL_PRO": "MiniMax-M3"},
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
                     "AGENT_OPENAI_MODEL_PRO": "MiniMax-M3"},
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
        from web_operation_agent.contract import PLANNER_THINKING_FACTOR, thinking_options
        c = _Recorder("qwen3.8-flash")
        base = c.options["thinking_budget"]
        self.assertEqual(thinking_options(c, PLANNER_THINKING_FACTOR),
                         {"thinking_budget": base * PLANNER_THINKING_FACTOR})

    def test_it_grows_with_the_answer_budget(self):
        from web_operation_agent.contract import thinking_options
        c = _Recorder("qwen3.8-flash")
        first = thinking_options(c, 4 * 2 ** 0)["thinking_budget"]
        second = thinking_options(c, 4 * 2 ** 1)["thinking_budget"]
        self.assertEqual(second, 2 * first)

    def test_a_model_with_no_such_knob_is_asked_for_nothing(self):
        """Otherwise switching provider would start posting a field it never advertised."""
        from web_operation_agent.contract import thinking_options
        self.assertIsNone(thinking_options(_Recorder("MiniMax-M3"), 4))
        self.assertIsNone(thinking_options(object(), 4))


if __name__ == "__main__":
    unittest.main()


class WhatTheAgentIsToldAfterAWrite(unittest.TestCase):
    """After a write is permitted, the agent may stop and verify instead of submitting again."""

    def _rendered(self, state):
        from web_operation_agent.session import ServerAgentSession, _render_observation
        from web_operation_agent.schemas import (SCHEMA_VERSIONS, ActionReceipt, ElementRef,
                                                 PageObservation)
        obs = PageObservation(schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=1,
                              url="http://app/x", title="T", landmarks=[],
                              elements=[ElementRef("e1", "link", "L", "a", 1)], messages=[])
        rec = ActionReceipt(schema_version=SCHEMA_VERSIONS["ActionReceipt"], command_id="c",
                            action="click", args_echo={}, ok=True, url_before="a",
                            url_after="b", navigated=True)
        s = object.__new__(ServerAgentSession)
        s.write_refusals = 0
        return ServerAgentSession._render(s, obs, rec, "", {"state": state})

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
    """A run bounced back to a login page and a run quietly recovered look identical in the
    record unless the count the worker already reports is actually read."""

    def _client(self, relogins):
        from web_operation_agent.browser import ThinBrowserClient
        c = object.__new__(ThinBrowserClient)
        c.relogins = 0
        c._roundtrip = lambda msg, expect, timeout_note="": {
            "kind": "console", "lines": ["error: x"], "relogins": relogins}
        return c

    def test_the_count_reaches_the_caller(self):
        from web_operation_agent.browser import ThinBrowserClient
        c = self._client(5)
        ThinBrowserClient.console(c)
        self.assertEqual(c.relogins, 5)

    def test_a_worker_that_reports_nothing_is_zero_not_an_error(self):
        from web_operation_agent.browser import ThinBrowserClient
        c = self._client(None)
        ThinBrowserClient.console(c)
        self.assertEqual(c.relogins, 0)

    def test_the_action_ledger_carries_it_per_action(self):
        from dataclasses import replace
        from web_operation_agent.tests.test_session import (_Browser, _Chat, _Control,
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
