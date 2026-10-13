"""Caller-required tool responses survive the real HTTP and admission boundary."""
import json
from types import SimpleNamespace

from web_operation_agent.modeling import client as M
from web_operation_agent.modeling.chat import KeyedLimitedChat, KeyedProviderAdmission
from web_operation_agent.modeling.egress import AuditedChat
from web_operation_agent.tests.modeling.test_provider_transport import _ProviderCase, _Server, MESSAGES

TOOLS = [{"type": "function", "function": {"name": "declare_task", "description": "Declare only.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]
CALLS = [{"id": "one", "type": "function", "function": {"name": "declare_task", "arguments": "{}"}},
         {"id": "two", "type": "function", "function": {"name": "declare_task", "arguments": "{}"}}]
TOOL_REPLY = json.dumps({"choices": [{"message": {"role": "assistant", "content": None,
    "tool_calls": CALLS}}], "usage": {"prompt_tokens": 2, "completion_tokens": 2}}).encode()


class ToolChoiceTests(_ProviderCase):
    def test_required_survives_audit_and_admission_and_preserves_multiple_calls(self):
        server = _Server(reply=TOOL_REPLY)
        chat = AuditedChat(KeyedLimitedChat(self.chat(server), KeyedProviderAdmission(1), "actor"),
            SimpleNamespace(append=lambda *_args, **_kwargs: None))
        reply = chat.complete_cancellable(MESSAGES, TOOLS, "required", cancelled=lambda: False,
            options={"tool_choice": "required"}, timeout_s=5)
        self.assertEqual(server.bodies[0]["tool_choice"], "required")
        self.assertEqual(reply["tool_calls"], CALLS)
        self.assertEqual(len(self.rows()), 1)

    def test_ordinary_call_still_uses_auto_after_a_required_call(self):
        server = _Server(reply=TOOL_REPLY)
        chat = self.chat(server)
        chat.complete(MESSAGES, TOOLS, "required", options={"tool_choice": "required"})
        chat.complete(MESSAGES, TOOLS, "ordinary")
        self.assertEqual([b["tool_choice"] for b in server.bodies], ["required", "auto"])

    def test_invalid_mode_and_required_without_tools_never_dispatch(self):
        server = _Server()
        chat = self.chat(server)
        for tools, value in ((None, "required"), (TOOLS, "anything"), (TOOLS, {})):
            with self.subTest(value=value), self.assertRaises(ValueError):
                chat.complete(MESSAGES, tools, "invalid", options={"tool_choice": value})
        self.assertEqual(server.requests, 0)
        self.assertFalse(self.ledger.exists())

    def test_required_prose_response_is_terminal_and_usage_is_kept(self):
        server = _Server()
        chat = self.chat(server)
        with self.assertRaises(M.ProviderError) as caught:
            chat.complete(MESSAGES, TOOLS, "required", options={"tool_choice": "required"})
        self.assertEqual(caught.exception.code, "provider_tool_response")
        self.assertTrue(caught.exception.dispatched)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(server.requests, 1)
        self.assertEqual(len(self.rows()), 1)
