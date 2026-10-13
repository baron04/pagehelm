"""Input source binding is deterministic and cannot read or prepare effects."""
from copy import deepcopy
import json
import unittest

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.tools.contracts import ToolOperation
from web_operation_agent.business.tools.registry import ToolDefinition
from web_operation_agent.goal.skill_literals import (bind_input_sources, require_literal_inputs,
                                                      user_texts)


class LiteralBindingTests(unittest.TestCase):
    def definition(self):
        class NoEffects:
            def prepare(self, *args, **kwargs):
                raise AssertionError("source binding must not prepare effects")
            dispatch = prepare
        return ToolDefinition("create_contact", "Create a contact", json.dumps({
            "type": "object", "properties": {"name": {"type": "string", "minLength": 1},
                "email": {"type": "string", "minLength": 3}},
            "required": ["name", "email"], "additionalProperties": False}),
            "write", "synthetic.write", ToolOperation("contact", "create"), NoEffects(),
            literal_inputs=("name", "email"))

    def bind(self, name, request="请创建合成杉木研究中心，邮箱cedar@example.test。", **kwargs):
        return bind_input_sources({"name": name, "email": {"source": 0, "text": "cedar@example.test"}},
                                 self.definition(), user_texts(request, **kwargs))

    def test_complete_unquoted_source_binds_once_without_a_model(self):
        result = self.bind({"source": 0, "text": "合成杉木研究中心"})
        self.assertEqual(result, {"name": "合成杉木研究中心", "email": "cedar@example.test"})

    def test_invented_values_case_changes_and_partial_quotes_stop(self):
        for name in ("Invented", "Synthetic Cedar", "Cedar Research", "synthetic cedar research"):
            with self.subTest(name=name), self.assertRaises(BusinessToolRequestError):
                self.bind({"source": 0, "text": name}, 'Create “Synthetic Cedar Research” with cedar@example.test.')

    def test_partial_ascii_identifiers_stop_and_complete_identifiers_bind(self):
        for name in ("SyntheticCedar", "CedarResearch"):
            with self.subTest(name=name), self.assertRaises(BusinessToolRequestError):
                self.bind({"source": 0, "text": name}, 'Create SyntheticCedarResearch with cedar@example.test.')
        self.assertEqual(self.bind({"source": 0, "text": "SyntheticCedarResearch"},
            'Create SyntheticCedarResearch with cedar@example.test.')["name"], "SyntheticCedarResearch")

    def test_plain_literal_inputs_are_found_in_the_person_s_own_messages(self):
        definition = self.definition()
        texts = ("Name it Original Cedar Research.", "Create it with cedar@example.test.")
        require_literal_inputs({"name": "Original Cedar Research", "email": "cedar@example.test"},
                               definition, texts)
        # An explicit clearing and a field the proposal leaves out are the schema's to judge.
        require_literal_inputs({"name": ""}, definition, texts)
        for name in ("Invented Cedar", "original cedar research", "Cedar Resear"):
            with self.subTest(name=name), self.assertRaises(BusinessToolRequestError):
                require_literal_inputs({"name": name, "email": "cedar@example.test"}, definition, texts)
        with self.assertRaises(BusinessToolRequestError):
            require_literal_inputs({"name": "Original Cedar Research", "email": "cedar@example.test"},
                                   definition, ("Create it.",))

    def test_malformed_or_missing_source_identity_never_binds(self):
        for source in ("合成杉木研究中心", {}, {"source": 0}, {"source": True, "text": "合成杉木研究中心"},
                       {"source": -1, "text": "合成杉木研究中心"}, {"source": 1, "text": "合成杉木研究中心"},
                       {"source": 0, "text": None}, {"source": 0, "text": "合成杉木研究中心", "extra": "x"}):
            with self.subTest(source=source), self.assertRaises(BusinessToolRequestError):
                self.bind(source)

    def test_history_source_cannot_select_assistant_text_or_a_different_user_turn(self):
        history = {"prior_turns": [{"user": "Name it Original Cedar Research.", "assistant": "Invented Cedar"}]}
        result = self.bind({"source": 1, "text": "Original Cedar Research"},
                          "Create it with cedar@example.test.", conversation=history)
        self.assertEqual(result["name"], "Original Cedar Research")
        for source, name in ((0, "Original Cedar Research"), (1, "Invented Cedar")):
            with self.subTest(source=source), self.assertRaises(BusinessToolRequestError):
                self.bind({"source": source, "text": name}, "Create it with cedar@example.test.", conversation=history)

    def test_multiline_text_is_data_and_retains_punctuation(self):
        name = 'Synthetic Cedar\nResearch "A"'
        result = self.bind({"source": 0, "text": name}, f'Create “{name}” with cedar@example.test.')
        self.assertEqual(result["name"], name)

    def test_binding_never_mutates_inputs_and_installed_schema_still_checks_fields(self):
        args = {"name": {"source": 0, "text": "合成杉木研究中心"},
                "email": {"source": 0, "text": "cedar@example.test"}}
        original = deepcopy(args)
        bind_input_sources(args, self.definition(), user_texts("请创建合成杉木研究中心，邮箱cedar@example.test。"))
        self.assertEqual(args, original)
        for malformed in ({"name": args["name"]}, {**args, "unexpected": "x"}):
            with self.subTest(malformed=malformed), self.assertRaises(BusinessToolRequestError):
                bind_input_sources(malformed, self.definition(), user_texts("请创建合成杉木研究中心，邮箱cedar@example.test。"))

    def test_nested_updates_bind_only_declared_fields_and_keep_references(self):
        definition = ToolDefinition("update_contact", "Update contact", json.dumps({
            "type": "object", "properties": {"target": {"type": "string"}, "changes": {
                "type": "object", "properties": {"note": {"type": "string"}}, "additionalProperties": False}},
            "required": ["target", "changes"], "additionalProperties": False}),
            "write", "synthetic.write", ToolOperation("contact", "update"), self.definition().handler,
            reference_inputs=("target",), literal_inputs=("changes.note",))
        note = '第一行\n不要创建其他记录；保留 "原文"。'
        result = bind_input_sources({"target": "@result:0", "changes": {"note": {"source": 0, "text": note}}},
                                    definition, (f'Update its note:\n{note}',))
        self.assertEqual(result, {"target": "@result:0", "changes": {"note": note}})
