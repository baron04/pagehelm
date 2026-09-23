from __future__ import annotations

import json
import unittest

from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent_odoo.demo.form_qualify import (
    FormQualificationChat,
    _tool_payload,
    form_cases,
)


class DynamicFormQualificationScript(unittest.TestCase):
    def setUp(self):
        self.case = form_cases("0123abcd")[0]
        self.chat = FormQualificationChat(
            self.case, egress_policy=ProviderEgressPolicy())
        self.tools = [{"function": {"name": name}}
                      for name in ("describe_form", "prepare_form", "prefill_form")]

    @staticmethod
    def _call(response):
        call = response["tool_calls"][0]["function"]
        return call["name"], json.loads(call["arguments"])

    def test_script_consumes_only_the_server_minted_form_and_field_ids(self):
        name, arguments = self._call(self.chat.complete([], self.tools, "run"))
        self.assertEqual(name, "describe_form")
        self.assertNotIn("form_id", arguments)
        self.assertNotIn("field_id", json.dumps(arguments))

        descriptor = {
            "form_available": True,
            "form_id": "form_" + "a" * 32,
            "fields": [
                {"label": "Name", "field_id": "form_field_" + "b" * 32,
                 "type": "text", "control": "combobox"},
                {"label": "Email", "field_id": "form_field_" + "c" * 32,
                 "type": "text", "control": "textbox"},
            ],
        }
        messages = [{"content": "CURRENT FORM DESCRIPTOR (runtime fact):\n"
                                + json.dumps(descriptor)}]
        name, arguments = self._call(self.chat.complete(messages, self.tools, "run"))
        self.assertEqual(name, "prepare_form")
        self.assertEqual(arguments["form_id"], descriptor["form_id"])
        self.assertEqual([row["field_id"] for row in arguments["values"]],
                         [row["field_id"] for row in descriptor["fields"]])

        messages.append({"content": (
            "APPLICATION FORM DRAFT READY:\n{}\n"
            "Call prefill_form with no arguments.")})
        name, arguments = self._call(self.chat.complete(messages, self.tools, "run"))
        self.assertEqual((name, arguments), ("prefill_form", {}))

    def test_a_missing_application_label_fails_instead_of_guessing_a_field(self):
        self.chat.complete([], self.tools, "run")
        messages = [{"content": "CURRENT FORM DESCRIPTOR:\n" + json.dumps({
            "form_available": True,
            "form_id": "form_" + "a" * 32,
            "fields": [],
        })}]
        name, arguments = self._call(self.chat.complete(messages, self.tools, "run"))
        self.assertEqual((name, arguments), ("wait_for", {"ms": 1_000}))
        name, _arguments = self._call(self.chat.complete(messages, self.tools, "run"))
        self.assertEqual(name, "describe_form")
        with self.assertRaisesRegex(RuntimeError, "omitted requested labels"):
            self.chat.complete(messages, self.tools, "run")

    def test_an_unrendered_form_gets_one_wait_before_it_is_declared_missing(self):
        self.chat.complete([], self.tools, "run")
        messages = [{"content": "CURRENT FORM DESCRIPTOR:\n" + json.dumps({
            "form_available": False, "fields": [],
        })}]
        self.assertEqual(self._call(self.chat.complete(messages, self.tools, "run")),
                         ("wait_for", {"ms": 1_000}))
        self.assertEqual(self._call(self.chat.complete(messages, self.tools, "run"))[0],
                         "describe_form")
        with self.assertRaisesRegex(RuntimeError, "did not expose the current form"):
            self.chat.complete(messages, self.tools, "run")

    def test_payload_parser_does_not_accept_a_non_matching_tool_result(self):
        with self.assertRaisesRegex(RuntimeError, "CURRENT FORM DESCRIPTOR"):
            _tool_payload([{"content": "some other result\n{}"}],
                          "CURRENT FORM DESCRIPTOR")


if __name__ == "__main__":
    unittest.main()
