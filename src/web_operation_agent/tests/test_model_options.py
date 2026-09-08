"""Per-model request options.

Some providers bound answer and reasoning tokens separately. Reasoning allowances therefore
live in policy data keyed by model name rather than in model-specific code branches.
"""
import json
import unittest
from pathlib import Path

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

    def complete(self, messages, tools, run_id, max_tokens=None, options=None):
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
        c.complete([], None, "r", options={"model": "elsewhere"})
        self.assertEqual(c.body["model"], "qwen3.8-flash")


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
        import inspect
        from web_operation_agent.session import ServerAgentSession
        src = inspect.getsource(ServerAgentSession.run)
        self.assertIn("relogin=receipt.relogin", src)
