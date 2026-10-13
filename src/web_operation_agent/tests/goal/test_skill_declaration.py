"""Malformed model arguments stop before any resource read or business preparation."""
import json
import unittest
from dataclasses import replace

from web_operation_agent.goal.index import ApplicationIndex, IndexConvention, IndexEntity, IndexField
from web_operation_agent.goal.skill_plan import declare_plan, plan_tools, _parse_plan
from web_operation_agent.goal.spec import DECLARE_GOAL_TOOL
from web_operation_agent.goal.understand import understand_scope
from web_operation_agent.skills import SkillContext, SkillRegistry
from web_operation_agent.skills.artifacts import SkillArtifact
from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.tools.contracts import ToolOperation
from web_operation_agent.business.tools.registry import ToolDefinition
from web_operation_agent.tests.support.skill_chat import SkillDeclarationChat as _Chat
from web_operation_agent.tests.support.business import _Chat as RawChat


class SkillDeclarationTests(unittest.TestCase):
    def test_raw_protocol_requires_execute_marker_and_bound_inputs(self):
        valid = {"request_intent": "execute", "inputs": {
            "name": {"source": 0, "text": "Synthetic Harbor"},
            "email": {"source": 0, "text": "harbor@example.test"}}}
        malformed = [
            {"inputs": valid["inputs"]},
            {**valid, "request_intent": "prepare"},
            {**valid, "request_intent": False},
            {**valid, "extra": "untrusted"},
            {**valid, "inputs": {"name": "Synthetic Harbor", "email": "harbor@example.test"}},
            {**valid, "inputs": {**valid["inputs"], "name": {"source": 0, "text": "Invented Harbor"}}},
        ]
        for args in malformed:
            with self.subTest(args=args):
                chat = RawChat([("create_contact", args)] * 2)
                with self.assertRaises(BusinessToolRequestError):
                    self.declare_creation(chat)
                self.assertEqual(len(chat.messages), 2)
        result = self.declare_creation(RawChat([[('create_contact', valid), ('finish_skill_task', {})]]))
        self.assertEqual(result["steps"][0]["inputs"], {
            "name": "Synthetic Harbor", "email": "harbor@example.test"})

    def test_declaration_metadata_does_not_reserve_installed_business_field_names(self):
        context, definition = self.creation_context()
        schema = json.loads(definition.input_json)
        schema["properties"]["request_intent"] = {"type": "string"}
        definition = replace(definition, input_json=json.dumps(schema))
        original = definition.schema
        offered = plan_tools((definition,))[0]["function"]["parameters"]
        self.assertEqual(offered["properties"]["request_intent"]["enum"], ["execute"])
        self.assertEqual(offered["properties"]["inputs"]["properties"]["request_intent"], {"type": "string"})
        inputs = {"name": {"source": 0, "text": "Synthetic Harbor"},
            "email": {"source": 0, "text": "harbor@example.test"}, "request_intent": "business value"}
        plan = _parse_plan({"tool_calls": [{"function": {"name": "create_contact", "arguments": {
            "request_intent": "execute", "inputs": inputs}}}]}, context, (definition,),
            ("Create Synthetic Harbor with harbor@example.test",))
        self.assertEqual(plan["steps"][0]["inputs"]["request_intent"], "business value")
        self.assertEqual(definition.schema, original)

    def test_negated_explanation_can_finish_without_operation_proposals(self):
        context, definition = self.creation_context()
        for request in ("Don't create Synthetic Harbor with harbor@example.test; just explain the requirements.",
                        "不要新建Synthetic Harbor，邮箱harbor@example.test；只说明所需资料。"):
            with self.subTest(request=request):
                chat = RawChat([("cite_skill_knowledge", {"skill_id": context.loaded[0].skill_id,
                    "span": "Use supplied names and emails."})])
                plan = declare_plan(chat, request=request, index=ApplicationIndex(), context=context,
                    definitions=(), run_id="non-execution", remaining=lambda: 10, cancelled=lambda: False,
                    scope={"parts": [{"kind": "fact", "rules": True}], "write_intent": False})
                self.assertFalse(plan["steps"])
                self.assertNotIn("create_contact", chat.tool_names[0])

    def creation_context(self):
        artifact = SkillArtifact.from_files("fixture", {
            "SKILL.md": b"---\nname: contact\ndescription: Maintain synthetic contacts.\n---\nUse supplied names and emails.\n",
            "agents/woa.toml": b'schema_version = 1\nwrite_operations = ["create_contact"]\n'})
        context = SkillContext(SkillRegistry((artifact,)))
        context.load(artifact.skill_id)

        class Handler:
            def prepare(self, *_args, **_kwargs):
                raise AssertionError("declaration must not prepare writes")
            dispatch = prepare

        definition = ToolDefinition("create_contact", "Create a synthetic contact", json.dumps({
            "type": "object", "properties": {"name": {"type": "string", "minLength": 1},
                "email": {"type": "string", "minLength": 3}},
            "required": ["name", "email"], "additionalProperties": False}),
            "write", "fixture.write", ToolOperation("contact", "create"), Handler(), literal_inputs=("name", "email"))
        return context, definition

    def declare_creation(self, chat, *, request="Create Synthetic Harbor with harbor@example.test", **kwargs):
        context, definition = self.creation_context()
        return declare_plan(chat, request=request,
            index=ApplicationIndex(), context=context, definitions=(definition,), run_id="creation",
            remaining=lambda: 10, cancelled=lambda: False, **kwargs)

    def declare_field_disagreement(self, chat, *, fields=("name",), request=None, email_required=True):
        context, definition = self.creation_context()
        if not email_required:
            schema = json.loads(definition.input_json)
            schema["required"] = ["name"]
            definition = replace(definition, input_json=json.dumps(schema))
        definition = replace(definition, input_fields=(("name", "contact", "name"),
                                                       ("email", "contact", "email")))
        return declare_plan(chat, request=request or "Create Synthetic Harbor with harbor@example.test",
            index=ApplicationIndex(), context=context, definitions=(definition,), run_id="field-review",
            remaining=lambda: 10, cancelled=lambda: False,
            scope={"write_intent": True, "parts": [{"kind": "handoff", "entity": "contact",
                "write_fields": [{"entity": "contact", "fields": list(fields)}]}]})

    def test_scope_omission_cannot_force_correct_proposal_to_drop_requested_field(self):
        proposal = ("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"})
        review = ("review_skill_fields", {"decisions": [{"field_id": 0, "requested": True}]})
        chat = _Chat([proposal, [review, proposal]])
        result = self.declare_field_disagreement(chat)
        self.assertEqual(result["steps"][0]["inputs"]["email"], "harbor@example.test")
        self.assertEqual(len(chat.messages), 2)
        self.assertNotIn("review_skill_fields", chat.tool_names[0])
        self.assertIn("review_skill_fields", chat.tool_names[1])
        feedback = chat.messages[-1][-1]["content"]
        self.assertIn('"field": "email"', feedback)
        self.assertIn('"in_scope": false', feedback)
        self.assertIn("two fallible readings", feedback)

    def test_replacing_proposal_without_review_cannot_bypass_disagreement(self):
        proposal = ("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"})
        chat = _Chat([proposal, ("create_contact", {"name": "Synthetic Harbor"})])
        with self.assertRaisesRegex(BusinessToolRequestError, "deleting fields cannot bypass review"):
            self.declare_field_disagreement(chat, email_required=False)
        self.assertEqual(len(chat.messages), 2)

    def test_reviewed_requested_optional_field_cannot_be_removed_from_proposal(self):
        chat = _Chat([("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"}),
            [("review_skill_fields", {"decisions": [{"field_id": 0, "requested": True}]}),
             ("create_contact", {"name": "Synthetic Harbor"})]])
        with self.assertRaises(BusinessToolRequestError):
            self.declare_field_disagreement(chat, email_required=False)

    def test_scope_only_error_retains_validated_inputs_without_regeneration(self):
        proposal = ("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"})
        chat = _Chat([proposal, ("review_skill_fields", {"decisions": [
            {"field_id": 0, "requested": True}]})])
        result = self.declare_field_disagreement(chat)
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["inputs"], {"name": "Synthetic Harbor", "email": "harbor@example.test"})
        self.assertEqual(len(chat.messages), 2)

    def test_confirmed_omission_requires_replacement_and_cannot_reuse_incomplete_inputs(self):
        chat = _Chat([("create_contact", {"name": "Synthetic Harbor"}),
            ("review_skill_fields", {"decisions": [{"field_id": 0, "requested": True}]}),
            ("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"})])
        result = self.declare_field_disagreement(chat, fields=("name", "email"), email_required=False)
        self.assertEqual(len(chat.messages), 3)
        self.assertIn("email", result["steps"][0]["inputs"])
        self.assertNotIn("review_skill_fields", chat.tool_names[-1])

    def test_rejected_extra_input_requires_replacement_without_silently_changing_effect(self):
        chat = _Chat([("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"}),
            ("review_skill_fields", {"decisions": [{"field_id": 0, "requested": False}]}),
            ("create_contact", {"name": "Synthetic Harbor"})])
        result = self.declare_field_disagreement(chat, email_required=False)
        self.assertEqual(len(chat.messages), 3)
        self.assertEqual(result["steps"][0]["inputs"], {"name": "Synthetic Harbor"})

    def test_field_review_cannot_drop_an_unrelated_or_unsupported_obligation(self):
        proposal = ("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"})
        # The scope has a genuinely requested field the tool does not support.
        chat = _Chat([proposal, [("review_skill_fields", {"decisions": [
            {"field_id": 0, "requested": True}]}), proposal]])
        with self.assertRaises(BusinessToolRequestError):
            self.declare_field_disagreement(chat, fields=("name", "email", "phone"),
                request="Create Synthetic Harbor with harbor@example.test and phone 5550104")
        self.assertEqual(len(chat.messages), 2)

    def test_unrequested_scope_field_can_be_explicitly_rejected_or_task_can_stop(self):
        proposal = ("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"})
        chat = _Chat([proposal, [("review_skill_fields", {"decisions": [
            {"field_id": 0, "requested": False}]}), proposal]])
        result = self.declare_field_disagreement(chat, fields=("name", "email", "phone"))
        self.assertEqual(len(result["steps"]), 1)
        for terminal in (("report_skill_limit", {"unsupported": "Requested phone is not supported."}),
                         ("ask_for_skill_inputs", {"question": "Which field should be changed?"})):
            with self.subTest(terminal=terminal[0]):
                result = self.declare_field_disagreement(_Chat([proposal, terminal]))
                self.assertFalse(result["steps"])

    def test_invalid_input_gets_one_correction_before_any_business_preparation(self):
        chat = _Chat([("create_contact", {"name": "Synthetic Harbor", "email": ""}),
                      [("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"}), ("finish_skill_task", {})]])
        errors = []
        result = self.declare_creation(chat, declaration_invalid=lambda *args: errors.append(args))
        self.assertEqual(result["steps"][0]["inputs"]["email"], "harbor@example.test")
        self.assertEqual(len(chat.messages), 2)
        self.assertEqual(errors, [("BusinessToolRequestError", "input.email exceeds its text bounds", True)])
        self.assertIn("No business operation has run", chat.messages[-1][-1]["content"])

    def test_second_invalid_input_stops_without_further_model_calls(self):
        chat = _Chat([("create_contact", {"name": "Synthetic Harbor", "email": ""})] * 2)
        with self.assertRaises(BusinessToolRequestError):
            self.declare_creation(chat)
        self.assertEqual(len(chat.messages), 2)

    def test_schema_correction_receives_the_failed_call_and_its_tool_rejection(self):
        chat = _Chat([("create_contact", {"name": "Synthetic Harbor"}),
            [("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"}),
             ("finish_skill_task", {})]])
        result = self.declare_creation(chat)
        retry = chat.messages[1]
        previous, = [message for message in retry if message.get("tool_calls")]
        call, = previous["tool_calls"]
        self.assertEqual(previous["role"], "assistant")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"inputs": {"name": {
            "source": 0, "text": "Synthetic Harbor"}}, "request_intent": "execute"})
        response, = [message for message in retry if message["role"] == "tool"]
        self.assertEqual(response["tool_call_id"], call["id"])
        rejected = json.loads(response["content"])
        self.assertFalse(rejected["accepted_proposal"])
        self.assertFalse(rejected["dispatched"])
        self.assertIn("missing required fields: email", rejected["validation_error"])
        self.assertEqual(result["steps"][0]["inputs"]["email"], "harbor@example.test")

    def test_invented_value_is_rejected_and_correction_uses_original_request(self):
        chat = _Chat([("create_contact", {"name": "Synthetic Harbor", "email": "invented@example.test"}),
            [("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"}), ("finish_skill_task", {})]])
        errors = []
        result = self.declare_creation(chat, declaration_invalid=lambda *args: errors.append(args))
        self.assertEqual(result["steps"][0]["inputs"]["email"], "harbor@example.test")
        self.assertIn("must copy a value supplied", errors[0][1])
        self.assertIn("REQUEST\nCreate Synthetic Harbor with harbor@example.test", chat.messages[-1][-1]["content"])

    def test_execution_request_cannot_be_settled_by_a_creation_handoff(self):
        chat = _Chat([("declare_goal", {"deliverables": [{"kind": "handoff", "entity": "contact",
            "action": "create"}], "write_intent": True}),
            ("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"})])
        errors = []
        result = self.declare_creation(chat, scope={"parts": [{"kind": "handoff", "entity": "contact"}],
            "write_intent": True}, declaration_invalid=lambda *args: errors.append(args))
        self.assertEqual(result["intent"], "execute")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["operation"], "create_contact")
        self.assertEqual(len(chat.messages), 2)
        self.assertIn("execution requires a native operation", errors[0][1])
        self.assertNotIn("declare_goal", chat.tool_names[0])

    def test_write_only_request_offers_native_operations_and_no_prepare_instructions(self):
        chat = _Chat([("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"})])
        result = self.declare_creation(chat, scope={"parts": [{"kind": "handoff", "entity": "contact"}],
            "write_intent": True})
        self.assertEqual(result["intent"], "execute")
        self.assertEqual(len(chat.messages), 1)
        self.assertIn("create_contact", chat.tool_names[0])
        self.assertNotIn("declare_goal", chat.tool_names[0])
        self.assertNotIn("For prepare use declare_goal handoffs", chat.messages[0][0]["content"])

    def test_terminal_capability_decision_is_reconciled_once_before_any_effect(self):
        chat = _Chat([("report_skill_limit", {"unsupported": "Names and email are not available."}),
                      ("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"})])
        rechecks = []
        result = self.declare_creation(chat, scope={"parts": [{"kind": "handoff", "entity": "contact"}],
            "write_intent": True}, capability_rechecked=lambda: rechecks.append(True))
        self.assertEqual(result["steps"][0]["inputs"], {
            "name": "Synthetic Harbor", "email": "harbor@example.test"})
        self.assertEqual(len(chat.messages), 2)
        self.assertEqual(rechecks, [True])
        feedback, = [m for m in chat.messages[1] if m["role"] == "tool"]
        self.assertFalse(json.loads(feedback["content"])["dispatched"])
        self.assertIn("routing summary does not prove support", chat.messages[1][-1]["content"])

    def test_confirmed_capability_limit_discards_effects_and_does_not_loop(self):
        limit = ("report_skill_limit", {"unsupported": "Owner assignment is unavailable."})
        for known_limit in ("", "Owner assignment is unavailable."):
            with self.subTest(known_limit=known_limit):
                chat = _Chat([limit] * (1 if known_limit else 2))
                result = self.declare_creation(chat, scope={
                    "parts": [{"kind": "handoff", "entity": "contact"}], "write_intent": True,
                    "unsupported": known_limit})
                self.assertEqual(result["steps"], [])
                self.assertEqual(result["unsupported"], "Owner assignment is unavailable.")
                self.assertEqual(len(chat.messages), 1 if known_limit else 2)

    def test_capability_recheck_and_invalid_input_share_one_correction_budget(self):
        limit = ("report_skill_limit", {"unsupported": "Names and email are unavailable."})
        invalid = ("create_contact", {"name": "Synthetic Harbor"})
        for script in ([limit, invalid], [invalid, limit]):
            with self.subTest(script=script):
                chat = _Chat(script)
                options = {"scope": {"parts": [{"kind": "handoff", "entity": "contact"}],
                                     "write_intent": True}}
                if script[0] == limit:
                    with self.assertRaises(BusinessToolRequestError):
                        self.declare_creation(chat, **options)
                else:
                    self.assertEqual(self.declare_creation(chat, **options)["steps"], [])
                self.assertEqual(len(chat.messages), 2)

    def test_write_only_context_preserves_actor_and_conventions_without_page_vocabulary(self):
        context, definition = self.creation_context()
        index = ApplicationIndex(actor="Synthetic Actor", entities=(IndexEntity("contact", "Contact",
            fields=(IndexField("other", "Unrelated readable value", "text"),)),),
            conventions=(IndexConvention(("client",), "A client is a contact in this deployment."),))
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                calls = [("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"})]
                parts = [{"kind": "handoff", "entity": "contact"}]
                if mixed:
                    calls.append(("declare_goal", {"deliverables": [{"kind": "fact", "entity": "contact",
                        "record": "Synthetic Harbor", "fields": ["email"]}], "write_intent": False}))
                    parts.append({"kind": "fact", "entity": "contact"})
                chat = _Chat([calls])
                declare_plan(chat, request="Create Synthetic Harbor with harbor@example.test",
                    index=index, context=context, definitions=(definition,), run_id="context-proof",
                    remaining=lambda: 10, cancelled=lambda: False,
                    current_values="v1 record contact: Synthetic Existing", scope={"parts": parts, "write_intent": True})
                content = chat.messages[0][1]["content"]
                for required in ("Synthetic Actor", "A client is a contact", "Synthetic Existing"):
                    self.assertIn(required, content)
                self.assertEqual("Unrelated readable value" in content, mixed)

    def test_mixed_execution_goal_schema_cannot_advertise_write_handoffs(self):
        _, definition = self.creation_context()
        original = json.dumps(DECLARE_GOAL_TOOL, sort_keys=True)
        tools = plan_tools((definition,), execution=True)
        goal = next(tool for tool in tools if tool["function"]["name"] == "declare_goal")
        properties = goal["function"]["parameters"]["properties"]
        self.assertEqual(properties["write_intent"]["enum"], [False])
        self.assertEqual(properties["deliverables"]["items"]["properties"]["kind"]["enum"],
            ["fact", "aggregate", "list", "page"])
        prepared = next(tool for tool in plan_tools((definition,)) if tool["function"]["name"] == "declare_goal")
        self.assertIn("handoff", prepared["function"]["parameters"]["properties"]["deliverables"]["items"]["properties"]["kind"]["enum"])
        self.assertEqual(json.dumps(DECLARE_GOAL_TOOL, sort_keys=True), original)

    def test_execution_handoff_is_rejected_even_with_false_write_intent(self):
        handoff = ("declare_goal", {"deliverables": [{"kind": "handoff", "entity": "contact",
            "action": "create"}], "write_intent": False})
        chat = _Chat([handoff, handoff])
        with self.assertRaisesRegex(ValueError, "execution requires a native operation"):
            self.declare_creation(chat, scope={"parts": [{"kind": "handoff", "entity": "contact"}],
                "write_intent": True})
        self.assertEqual(len(chat.messages), 2)

    def test_prepare_request_retains_handoff_without_proposing_a_write(self):
        handoff = ("declare_goal", {"deliverables": [{"kind": "handoff", "entity": "contact",
            "action": "create"}], "write_intent": True})
        chat = _Chat([handoff, handoff])
        result = self.declare_creation(chat, scope={"parts": [{"kind": "handoff", "entity": "contact"}],
            "write_intent": False}, request="Prepare Synthetic Harbor with harbor@example.test; I will submit it.")
        self.assertEqual(result["intent"], "prepare")
        self.assertEqual(result["steps"][0]["kind"], "goal")
        self.assertIn("declare_goal", chat.tool_names[0])
        self.assertEqual(len(chat.messages), 2)

    def test_missed_execution_handoff_rechecked_before_effects_and_preserves_all_inputs(self):
        handoff = ("declare_goal", {"deliverables": [{"kind": "handoff", "entity": "contact",
            "action": "create"}], "write_intent": False})
        chat = _Chat([handoff, ("create_contact", {
            "name": "Synthetic Harbor", "email": "harbor@example.test"})])
        rechecks = []
        result = self.declare_creation(chat, scope={"parts": [{"kind": "handoff", "entity": "contact"}],
            "write_intent": False}, intent_rechecked=lambda: rechecks.append(True))
        self.assertEqual(result["intent"], "execute")
        self.assertEqual(result["steps"][0]["inputs"], {
            "name": "Synthetic Harbor", "email": "harbor@example.test"})
        self.assertEqual(rechecks, [True])
        feedback = json.loads(chat.messages[1][-2]["content"])
        self.assertFalse(feedback["accepted_proposal"])
        self.assertFalse(feedback["dispatched"])

    def test_intent_recheck_shares_correction_budget_and_drops_missing_input_draft(self):
        handoff = ("declare_goal", {"deliverables": [{"kind": "handoff", "entity": "contact",
            "action": "create"}], "write_intent": False})
        for next_reply in (("create_contact", {"name": "Synthetic Harbor", "email": ""}),
                           ("ask_for_skill_inputs", {"question": "Which email?"})):
            chat = _Chat([handoff, next_reply])
            if next_reply[0] == "create_contact":
                with self.assertRaises(BusinessToolRequestError):
                    self.declare_creation(chat, scope={"parts": [{"kind": "handoff", "entity": "contact"}],
                        "write_intent": False})
            else:
                result = self.declare_creation(chat, scope={"parts": [{"kind": "handoff", "entity": "contact"}],
                    "write_intent": False})
                self.assertEqual(result["steps"], [])
                self.assertEqual(result["question"], "Which email?")
            self.assertEqual(len(chat.messages), 2)

    def test_mixed_request_rejects_execution_handoff_before_recording_pending_steps(self):
        chat = _Chat([("declare_goal", {"deliverables": [{"kind": "handoff", "entity": "contact",
            "action": "create"}], "write_intent": True}),
            ("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"}),
            ("declare_goal", {"deliverables": [{"kind": "fact", "entity": "contact",
                "record": "Synthetic Harbor", "fields": ["email"]}], "write_intent": False})])
        result = self.declare_creation(chat, scope={"parts": [{"kind": "handoff", "entity": "contact"},
            {"kind": "fact", "entity": "contact"}], "write_intent": True})
        self.assertEqual([step["kind"] for step in result["steps"]], ["operation", "goal"])
        self.assertEqual(len(chat.messages), 3)

    def test_missing_information_returns_question_and_discards_incomplete_effects(self):
        chat = _Chat([[("create_contact", {"name": "Synthetic Harbor", "email": ""}), ("ask_for_skill_inputs", {"question": "What is the email?"})]])
        result = self.declare_creation(chat)
        self.assertEqual(result["steps"], [])
        self.assertEqual(result["question"], "What is the email?")
        self.assertEqual(len(chat.messages), 1)

    def test_process_explanation_uses_installed_passage_without_offering_record_goals(self):
        context, _ = self.creation_context()
        skill = context.loaded[0]
        chat = _Chat([("cite_skill_knowledge", {"skill_id": skill.skill_id,
                                              "span": "Use supplied names and emails."})])
        result = declare_plan(chat, request="Explain how to create a synthetic contact",
            index=ApplicationIndex(), context=context, definitions=(), run_id="explain",
            remaining=lambda: 10, cancelled=lambda: False,
            scope={"parts": [{"kind": "fact", "rules": True}], "write_intent": False})
        self.assertEqual(result["intent"], "explain")
        self.assertFalse(result["steps"])
        self.assertEqual(result["knowledge"][0]["span"], "Use supplied names and emails.")
        self.assertEqual(len(chat.messages), 1)
        self.assertNotIn("declare_goal", chat.tool_names[0])

    def test_duplicate_operation_proposals_stop_before_any_preparation(self):
        duplicate = [("create_contact", {"name": "Synthetic Harbor", "email": "harbor@example.test"})] * 2
        chat = _Chat([duplicate, duplicate])
        with self.assertRaisesRegex(ValueError, "proposed more than once"):
            self.declare_creation(chat)
        self.assertEqual(len(chat.messages), 2)

    def test_literal_input_can_come_from_user_history_but_not_prior_assistant_prose(self):
        for source in ("user", "assistant"):
            with self.subTest(source=source):
                chat = _Chat([[("create_contact", {"name": "Synthetic Harbor", "email": {"source": 1, "text": "old@example.test"}}),
                              ("finish_skill_task", {})]] * 2)
                history = {"prior_turns": [{source: "Use old@example.test"}]}
                if source == "user":
                    result = self.declare_creation(chat, conversation=history)
                    self.assertEqual(result["steps"][0]["inputs"]["email"], "old@example.test")
                else:
                    with self.assertRaises(BusinessToolRequestError):
                        self.declare_creation(chat, conversation=history)

    def test_quoted_name_truncation_requires_correction_before_preparation(self):
        context, definition = self.creation_context()
        chat = _Chat([("create_contact", {"name": "Synthetic Cedar", "email": "cedar@example.test"}),
            ("create_contact", {"name": "Synthetic Cedar Research", "email": "cedar@example.test"})])
        invalid = []
        result = declare_plan(chat, request='Create “Synthetic Cedar Research” with cedar@example.test.',
            index=ApplicationIndex(), context=context, definitions=(definition,), run_id="quoted-copy",
            remaining=lambda: 10, cancelled=lambda: False,
            scope={"parts": [{"kind": "handoff", "entity": "contact"}], "write_intent": True},
            declaration_invalid=lambda *args: invalid.append(args))
        self.assertEqual(result["steps"][0]["inputs"]["name"], "Synthetic Cedar Research")
        self.assertEqual(len(chat.messages), 2)
        self.assertEqual(len(invalid), 1)
        self.assertIn("in full", invalid[0][1])

    def test_partial_quoted_literal_and_case_change_stop_after_one_correction(self):
        cases = (("“", "”", "Synthetic Cedar"), ('"', '"', "Cedar Research"),
                 ("'", "'", "Synthetic Cedar"), ("「", "」", "Synthetic Cedar"),
                 ("『", "』", "Cedar Research"), ("«", "»", "Synthetic Cedar"),
                 ("‘", "’", "Synthetic Cedar"), ("“", "”", "synthetic cedar research"))
        for opening, closing, shortened in cases:
            with self.subTest(opening=opening, value=shortened):
                context, definition = self.creation_context()
                chat = _Chat([("create_contact", {"name": shortened, "email": "cedar@example.test"})] * 2)
                with self.assertRaisesRegex(BusinessToolRequestError, "in full"):
                    declare_plan(chat, request=f'Create {opening}Synthetic Cedar Research{closing} with cedar@example.test.',
                        index=ApplicationIndex(), context=context, definitions=(definition,), run_id="partial-copy",
                        remaining=lambda: 10, cancelled=lambda: False)
                self.assertEqual(len(chat.messages), 2)

    def test_full_multiline_literal_and_apostrophe_name_remain_available(self):
        for name in ("Synthetic Cedar\nResearch", "Synthetic O'Connor"):
            with self.subTest(name=name):
                context, definition = self.creation_context()
                chat = _Chat([[("create_contact", {"name": name, "email": "cedar@example.test"}),
                    ("finish_skill_task", {})]])
                result = declare_plan(chat, request=f'Create “{name}” with cedar@example.test.',
                    index=ApplicationIndex(), context=context, definitions=(definition,), run_id="full-copy",
                    remaining=lambda: 10, cancelled=lambda: False)
                self.assertEqual(result["steps"][0]["inputs"]["name"], name)

    def test_unquoted_token_truncation_cannot_supply_a_literal(self):
        for name in ("SyntheticCedar", "CedarResearch"):
            with self.subTest(name=name):
                full_name = "SyntheticCedarResearch"
                context, definition = self.creation_context()
                chat = _Chat([("create_contact", {"name": name, "email": "cedar@example.test"})] * 2)
                with self.assertRaisesRegex(BusinessToolRequestError, "in full"):
                    declare_plan(chat, request=f'Create {full_name} with cedar@example.test.',
                        index=ApplicationIndex(), context=context, definitions=(definition,), run_id="token-copy",
                        remaining=lambda: 10, cancelled=lambda: False)

    def test_unquoted_complete_name_and_email_tokens_remain_available(self):
        context, definition = self.creation_context()
        chat = _Chat([[("create_contact", {"name": "SyntheticCedarResearch", "email": "cedar@example.test"}),
            ("finish_skill_task", {})]])
        result = declare_plan(chat, request='Create SyntheticCedarResearch with cedar@example.test.',
            index=ApplicationIndex(), context=context, definitions=(definition,), run_id="complete-token",
            remaining=lambda: 10, cancelled=lambda: False)
        self.assertEqual(result["steps"][0]["inputs"]["name"], "SyntheticCedarResearch")

    def test_unsegmented_request_does_not_make_complete_name_or_email_invalid(self):
        context, definition = self.creation_context()
        chat = _Chat([[("create_contact", {"name": "合成杉木研究中心", "email": "cedar@example.test"}),
            ("finish_skill_task", {})]])
        result = declare_plan(chat, request='请创建合成杉木研究中心，邮箱cedar@example.test。',
            index=ApplicationIndex(), context=context, definitions=(definition,), run_id="unsegmented-copy",
            remaining=lambda: 10, cancelled=lambda: False)
        self.assertEqual(result["steps"][0]["inputs"]["name"], "合成杉木研究中心")

    def test_source_scope_keeps_business_parts_without_selecting_skills(self):
        declaration = {"kind_0": "fact", "entity_0": "contact", "about_0": "current contact",
            "kind_1": "source", "about_1": "contact workflow", "terms_1": ["workflow"]}
        chat = _Chat([("declare_scope", declaration)])
        parts, _, _, _, projection = understand_scope(chat, prompt="Read the contact and explain its workflow",
            summaries="contact: Contact", run_id="source-skill", repository_source=True)
        self.assertEqual(parts[0]["entity"], "contact")
        self.assertNotIn("skill_ids", projection)
        self.assertEqual(projection["source_requests"][0]["question"], "contact workflow")
        self.assertEqual(projection["attempts"], 1)

    def test_application_calendar_reaches_the_declaration_that_prepares_the_write(self):
        chat = _Chat([("report_skill_limit", {"unsupported": "This application does not support assignment."})])
        declare_plan(chat, request="Explain tomorrow's workflow", index=ApplicationIndex(),
            context=SkillContext(SkillRegistry(())), definitions=(), run_id="calendar",
            remaining=lambda: 10, cancelled=lambda: False,
            today="2031-02-28", time_zone="Pacific/Auckland",
            current_values="v1 record contact: Synthetic Calendar (ID=1)",
            scope={"parts": [{"kind": "handoff", "entity": "contact"}],
                "unsupported": "This application does not support assignment."})
        content = chat.messages[0][1]["content"]
        self.assertIn("2031-02-28", content)
        self.assertIn("Pacific/Auckland", content)
        self.assertIn("Synthetic Calendar (ID=1)", content)
        self.assertIn("This application does not support assignment.", content)

    def test_resource_arguments_fail_closed_with_a_typed_declaration_error(self):
        for arguments in ([], None, "string", 42, {"skill_id": []},
                          {"skill_id": "fixture.workflow", "path": None},
                          {"skill_id": "fixture.workflow", "path": "references/a.md", "extra": "x"}):
            with self.subTest(argument_type=type(arguments).__name__):
                with self.assertRaises(ValueError):
                    declare_plan(_Chat([("read_skill_resource", arguments)]),
                        request="Explain the synthetic workflow", index=ApplicationIndex(),
                        context=SkillContext(SkillRegistry(())), definitions=(), run_id="invalid-resource",
                        remaining=lambda: 10, cancelled=lambda: False)

    def test_no_attachment_means_no_resource_tool_for_a_capability_limit(self):
        context, definition = self.creation_context()
        chat = RawChat([("report_skill_limit", {"unsupported": "Phone changes are unavailable."})])
        result = declare_plan(chat, request="Explain unsupported phone changes", index=ApplicationIndex(),
            context=context, definitions=(definition,), run_id="no-resource",
            remaining=lambda: 10, cancelled=lambda: False)
        self.assertNotIn("read_skill_resource", chat.tool_names[0])
        self.assertEqual(result["steps"], [])
        self.assertEqual(len(chat.messages), 1)

    def test_progressive_resource_schema_uses_loaded_text_attachments_only(self):
        context, _ = self.creation_context()
        guide = SkillArtifact.from_files("fixture", {
            "SKILL.md": b"---\nname: guide\ndescription: Explain team rules.\n---\nRead references/team.md.\n",
            "references/team.md": b"Team means the current actor's team.",
            "assets/image.png": b"synthetic image"})
        context = SkillContext(SkillRegistry((*context.loaded, guide)))
        for artifact in context.registry.artifacts:
            context.load(artifact.skill_id)
        class ResourceChat(RawChat):
            def complete(self, messages, tools, *args, **kwargs):
                self.offered_tools = tools
                return super().complete(messages, tools, *args, **kwargs)
        chat = ResourceChat([("read_skill_resource", {"skill_id": guide.skill_id, "path": "references/team.md"}),
            ("cite_skill_knowledge", {"skill_id": guide.skill_id, "resource": "references/team.md",
                "span": "Team means the current actor's team."})])
        read = []
        result = declare_plan(chat, request="Explain team meaning", index=ApplicationIndex(),
            context=context, definitions=(), run_id="resource",
            remaining=lambda: 10, cancelled=lambda: False,
            resource_loaded=lambda *args: read.append(args),
            scope={"parts": [{"kind": "fact", "rules": True}], "write_intent": False})
        resource = next(t for t in chat.offered_tools if t["function"]["name"] == "read_skill_resource")
        properties = resource["function"]["parameters"]["properties"]
        self.assertEqual(properties["skill_id"]["enum"], [guide.skill_id])
        self.assertEqual(properties["path"]["enum"], ["references/team.md"])
        self.assertEqual(len(read), 1)
        self.assertTrue(result["knowledge"])
