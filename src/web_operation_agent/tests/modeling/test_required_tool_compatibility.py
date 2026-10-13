"""Unsupported wire choice cannot weaken the caller's required declaration."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.modeling import client as M


TOOL = {"type": "function", "function": {"name": "declare_result", "parameters": {
    "type": "object", "properties": {}, "additionalProperties": False}}}
DECLARATION = {"tool_calls": [{"id": "synthetic-call", "type": "function",
    "function": {"name": "declare_result", "arguments": "{}"}}]}


class RequiredToolCompatibilityTests(unittest.TestCase):
    def run_call(self, message, *, host="token-plan.cn-beijing.maas.aliyuncs.com", cancellable=False):
        with tempfile.TemporaryDirectory() as directory:
            chat = M.ChatModel(M.SpendMeter(ledger=Path(directory)/"spend.jsonl"), env={
                "AGENT_OPENAI_MODEL": "deepseek-v4.1-flash", "AGENT_OPENAI_API_KEY": "synthetic-key",
                "AGENT_OPENAI_BASE_URL": "https://" + host + "/compatible-mode/v1"})
            bodies = []
            payload = json.dumps({"usage": {"prompt_tokens": 3, "completion_tokens": 2},
                "choices": [{"finish_reason": "tool_calls", "message": message}]}).encode()
            def respond(request, **kwargs):
                bodies.append(json.loads(request.data))
                return io.BytesIO(payload)
            def respond_cancellable(url, data, *args, **kwargs):
                bodies.append(json.loads(data))
                return payload
            with patch.object(M.urllib.request, "urlopen", side_effect=respond), \
                    patch.object(chat, "_cancellable_post", side_effect=respond_cancellable):
                if cancellable:
                    reply = chat.complete_cancellable([{"role": "user", "content": "Declare a synthetic result."}],
                        [TOOL], "synthetic-run", options={"tool_choice": "required"}, cancelled=lambda: False)
                else:
                    reply = chat.complete([{"role": "user", "content": "Declare a synthetic result."}],
                        [TOOL], "synthetic-run", options={"tool_choice": "required"})
            self.assertEqual(len(bodies), 1)
            return reply, bodies[0]

    def test_known_endpoint_uses_auto_but_returns_the_required_tool_declaration(self):
        for cancellable in (False, True):
            with self.subTest(cancellable=cancellable):
                reply, body = self.run_call(DECLARATION, cancellable=cancellable)
                self.assertEqual(body["tool_choice"], "auto")
                self.assertIs(body["enable_thinking"], False)
                self.assertEqual(body["tools"], [TOOL])
                self.assertEqual(reply["tool_calls"], DECLARATION["tool_calls"])

    def test_text_only_response_is_a_terminal_protocol_error_without_retry(self):
        for cancellable in (False, True):
            with self.subTest(cancellable=cancellable), self.assertRaises(M.ProviderError) as caught:
                self.run_call({"content": "done"}, cancellable=cancellable)
            self.assertEqual(caught.exception.code, "provider_tool_response")
            self.assertFalse(caught.exception.retryable)

    def test_other_endpoints_keep_the_requested_required_wire(self):
        for host in ("api.deepseek.com", "dashscope.aliyuncs.com", "token-plan.cn-beijing.maas.aliyuncs.com.example.test"):
            with self.subTest(host=host):
                _, body = self.run_call(DECLARATION, host=host)
                self.assertEqual(body["tool_choice"], "required")
