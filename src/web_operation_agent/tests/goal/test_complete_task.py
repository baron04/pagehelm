"""Current single-proposal and optional anchored contracts, with scripted models."""
import copy
import json
import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace

from web_operation_agent.business.effects import RequestedEffect, EffectProjection, canonical
from web_operation_agent.business.errors import BusinessToolRequestError, BusinessMutationIndeterminate
from web_operation_agent.goal.complete_task import declare_single_task, declare_complete_task, compare, parse_intent
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexField
from web_operation_agent.goal.skill_plan import plan_tools
from web_operation_agent.tests.goal import test_skill_declaration as declaration_fixture


def effect(values, *, entity="contacts", action="create", target_entity="", target="", preserve=()):
    # The anchored reading carries supplied facts, never field identities.
    return {"action": action, "target": target, "facts": [
        {"kind": "boolean" if type(v) is bool else "number" if type(v) in (int, float)
         else "clear" if v is None else "text", "value": v if isinstance(v, str)
         else "" if v is None else json.dumps(v)} for v in values.values()]}


def intent(*effects, mode="execute", goals=(), question="", unsupported=""):
    return {"mode": mode, "effects": list(effects), "outcomes": [
        {**d, "after_effect": item["after_effect"]}
        for item in goals for d in item["goal"]["deliverables"]],
        "missing_input": question, "capability_limit": unsupported}


def wire(*calls):
    return {"tool_calls": [{"id": str(i), "function": {"name": n, "arguments": json.dumps(a)}}
                           for i, (n, a) in enumerate(calls)]}


class ScriptedReadings:
    def __init__(self, expected, draft, replacement=None):
        self.expected, self.draft, self.replacement = expected, draft, replacement
        self.replies = iter(replacement or [])
        self.messages = []
        self.lock = threading.Lock()

    def complete_cancellable(self, messages, tools, *_args, **_kwargs):
        names = {t["function"]["name"] for t in tools}
        with self.lock:
            self.messages.append((copy.deepcopy(messages), names))
        if messages[0]["content"].startswith("Reconcile"):
            if self.replacement is None:
                raise AssertionError("unexpected reconciliation")
            return next(self.replies)
        if names == {"declare_skill_intent"}:
            return wire(("declare_skill_intent", self.expected))
        return self.draft


class CompleteTaskTests(unittest.TestCase):
    def setUp(self):
        self.context, base = declaration_fixture.SkillDeclarationTests().creation_context()
        class Projector:
            def project_inputs(self, inputs):
                return RequestedEffect("contacts", "create", values_json=canonical(inputs))
        self.definition = replace(base, effect_contract=Projector())
        self.index = ApplicationIndex(entities=(IndexEntity("contacts", "Contacts", fields=(
            IndexField("name", "Name", "text"), IndexField("email", "Email", "text"))),))
        self.values = {"name": "Synthetic Harbor Research", "email": "harbor@example.test"}
        self.request = "Create Synthetic Harbor Research with harbor@example.test."

    def draft(self, values, *, finish=True):
        calls = [("create_contact", {"request_intent": "execute", "inputs": {
            k: {"source": 0, "text": v} for k, v in values.items()}})]
        return wire(*calls, *([("finish_skill_task", {})] if finish else []))

    def declare(self, chat, *, single=False, scope=None, **kwargs):
        declarer = declare_single_task if single else declare_complete_task
        return declarer(chat, request=self.request, index=self.index, context=self.context,
            definitions=(self.definition,), run_id="synthetic", remaining=lambda: 10,
            cancelled=lambda: False, scope=scope or {"parts": [{"kind": "handoff", "entity": "contacts"}]}, **kwargs)

    def sequence(self, replies):
        class Chat:
            def __init__(self):
                self.replies, self.messages = iter(replies), []
            def complete_cancellable(self, messages, *_args, **_kwargs):
                self.messages.append(copy.deepcopy(messages))
                return next(self.replies)
        return Chat()

    def test_single_locks_one_valid_proposal_without_independent_reader(self):
        chat = self.sequence([self.draft(self.values)])
        plan = self.declare(chat, single=True)
        self.assertEqual(plan["locked_effects"][0].values, self.values)
        self.assertEqual(len(chat.messages), 1)
        self.assertFalse(plan["reconciled"])

    def test_single_rejects_execution_goal_and_keeps_the_accepted_operation(self):
        goal = wire(("declare_goal", {"write_intent": True, "deliverables": [{
            "kind": "handoff", "entity": "contacts", "action": "create"}]}))
        chat = self.sequence([self.draft(self.values, finish=False), goal,
                              wire(("finish_skill_task", {}))])
        prepared = []
        plan = self.declare(chat, single=True, preflight=lambda p: prepared.append(p))
        self.assertEqual([step["kind"] for step in plan["steps"]], ["operation"])
        self.assertEqual(plan["steps"][0]["inputs"], self.values)
        self.assertTrue(plan["correction_used"])
        self.assertEqual(len(prepared), 1)
        self.assertIn("declare_goal cannot request execution", chat.messages[2][-1]["content"])

    def test_single_nonexecuting_form_preparation_remains_a_goal(self):
        self.request = "Prepare a contact creation form."
        goal = wire(("declare_goal", {"write_intent": False, "deliverables": [{
            "kind": "handoff", "entity": "contacts", "action": "create"}]}),
            ("finish_skill_task", {}))
        plan = self.declare(self.sequence([goal]), single=True)
        self.assertEqual(plan["intent"], "prepare")
        self.assertEqual([step["kind"] for step in plan["steps"]], ["goal"])
        self.assertEqual(plan["locked_effects"], [])

    def test_single_explicit_preservation_enters_the_locked_application_effect(self):
        self.definition = replace(self.definition, preservation_fields=("phone",), preserves_unmentioned=True)
        reply = self.draft(self.values)
        args = json.loads(reply["tool_calls"][0]["function"]["arguments"])
        args["preserve"] = ["phone", "*"]
        reply["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
        plan = self.declare(self.sequence([reply]), single=True)
        self.assertEqual(plan["locked_effects"][0].preserve, ("phone", "*"))

    def test_preservation_is_off_unless_the_application_registers_it(self):
        schema = plan_tools((self.definition,), execution=True)[0]["function"]["parameters"]
        self.assertNotIn("preserve", schema["properties"])
        reply = self.draft(self.values)
        args = json.loads(reply["tool_calls"][0]["function"]["arguments"])
        args["preserve"] = ["*"]
        reply["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
        with self.assertRaises(BusinessToolRequestError):
            self.declare(self.sequence([reply, reply]), single=True)

    def test_registered_preservation_uses_identities_and_rejects_display_labels(self):
        self.definition = replace(self.definition, preservation_fields=("phone",))
        schema = plan_tools((self.definition,), execution=True)[0]["function"]["parameters"]
        self.assertEqual(schema["properties"]["preserve"]["items"]["enum"], ["phone"])
        reply = self.draft(self.values)
        args = json.loads(reply["tool_calls"][0]["function"]["arguments"])
        args["preserve"] = ["Phone number"]
        reply["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
        with self.assertRaises(BusinessToolRequestError):
            self.declare(self.sequence([reply, reply]), single=True)

    def test_preservation_metadata_requires_bounded_identities_and_an_effect_contract(self):
        for fields in (["phone"], ("phone", "phone"), ([],), ("Phone number",), ("*",)):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(self.definition, preservation_fields=fields)
        with self.assertRaises(ValueError):
            replace(self.definition, effect_contract=None, preserves_unmentioned=True)
        with self.assertRaises(ValueError):
            replace(self.definition, preserves_unmentioned=1)

    def test_single_cannot_lock_a_field_as_both_changed_and_preserved(self):
        self.definition = replace(self.definition, preservation_fields=("name",))
        reply = self.draft(self.values)
        args = json.loads(reply["tool_calls"][0]["function"]["arguments"])
        args["preserve"] = ["name"]
        reply["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
        with self.assertRaises(ValueError):
            self.declare(self.sequence([reply]), single=True)

    def test_single_corrects_rejected_literal_in_original_roles_once(self):
        chat = self.sequence([self.draft({**self.values, "name": "Invented"}), self.draft(self.values)])
        plan = self.declare(chat, single=True)
        self.assertEqual(plan["steps"][0]["inputs"], self.values)
        self.assertIn('accepted_proposal', canonical(chat.messages[-1]))
        self.assertIn('Invented', canonical(chat.messages[-1]))
        self.assertEqual(len(chat.messages), 2)

    def test_single_repeated_invalid_proposal_stops(self):
        wrong = self.draft({**self.values, "name": "Invented"})
        chat = self.sequence([wrong, wrong])
        with self.assertRaises(BusinessToolRequestError):
            self.declare(chat, single=True)
        self.assertEqual(len(chat.messages), 2)

    def test_single_empty_finish_is_not_complete(self):
        chat = self.sequence([wire(("finish_skill_task", {}))] * 2)
        with self.assertRaisesRegex(ValueError, "empty proposal"):
            self.declare(chat, single=True)
        self.assertEqual(len(chat.messages), 2)

    def test_consultation_empty_finish_is_a_validation_failure_not_implicit_yield(self):
        chat = self.sequence([wire(("finish_skill_task", {}))] * 2)
        events = []
        with self.assertRaisesRegex(ValueError, "empty proposal"):
            self.declare(chat, single=True, allow_yield=True,
                declaration_invalid=lambda *args: events.append(args))
        self.assertEqual([event[2] for event in events], [True, False])
        self.assertEqual(len(chat.messages), 2)

    def test_single_scope_categories_do_not_invent_additional_request_outcomes(self):
        for extra in ("fact", "aggregate", "list", "page", "handoff"):
            chat = self.sequence([self.draft(self.values)])
            plan = self.declare(chat, single=True, scope={"write_intent": True,
                "parts": [{"kind": "handoff"}, {"kind": extra}]})
            self.assertEqual([s["kind"] for s in plan["steps"]], ["operation"])
            self.assertEqual(len(chat.messages), 1)

    def test_single_full_request_can_propose_post_write_read_absent_from_scope(self):
        self.request += " After creation, read this contact's Email."
        goal = {"deliverables": [{"kind": "fact", "entity": "contacts", "record": "@result:0", "fields": ["Email"]}]}
        chat = self.sequence([self.draft(self.values, finish=False),
            wire(("declare_goal", goal), ("finish_skill_task", {}))])
        plan = self.declare(chat, single=True, scope={"parts": [{"kind": "handoff"}]})
        self.assertEqual([s["kind"] for s in plan["steps"]], ["operation", "goal"])
        self.assertIn(self.request, canonical(chat.messages[-1]))

    def test_single_duplicate_native_proposals_still_fail_before_preparation(self):
        first = self.draft(self.values, finish=False)
        duplicate = self.draft(self.values)
        chat = self.sequence([first, duplicate, duplicate])
        with self.assertRaisesRegex(ValueError, "more than once"):
            self.declare(chat, single=True)
        self.assertEqual(len(chat.messages), 3)

    def test_single_correction_retains_earlier_accepted_write(self):
        goal = {"deliverables": [{"kind": "fact", "entity": "contacts", "record": "@result:0", "fields": ["Email"]}]}
        chat = self.sequence([self.draft(self.values, finish=False),
            wire(("declare_goal", {"deliverables": "invalid"})), wire(("declare_goal", goal), ("finish_skill_task", {}))])
        plan = self.declare(chat, single=True, scope={"parts": [{"kind": "handoff"}, {"kind": "fact"}]})
        self.assertEqual([s["kind"] for s in plan["steps"]], ["operation", "goal"])
        self.assertEqual(len(plan["locked_effects"]), 1)
        self.assertEqual(len(chat.messages), 3)
        self.assertIn('recorded_proposal', canonical(chat.messages[-1]))

    def test_single_clarification_discards_accumulated_unexecuted_draft(self):
        chat = self.sequence([self.draft(self.values, finish=False),
            wire(("ask_for_skill_inputs", {"question": "Which requested record?"}))])
        plan = self.declare(chat, single=True, scope={"parts": [{"kind": "handoff"}, {"kind": "fact"}]})
        self.assertEqual(plan["steps"], [])
        self.assertEqual(plan["locked_effects"], [])
        self.assertTrue(plan["question"])

    def test_anchored_reads_independently_without_tool_or_field_vocabulary(self):
        chat = ScriptedReadings(intent(effect(self.values)), self.draft(self.values))
        plan = self.declare(chat)
        self.assertEqual(plan["locked_effects"][0].values, self.values)
        self.assertEqual(len(chat.messages), 2)
        reader = next(m for m, tools in chat.messages if tools == {"declare_skill_intent"})
        self.assertIn('LOADED BUSINESS KNOWLEDGE', canonical(reader))
        self.assertNotIn('create_contact', canonical(reader))
        self.assertNotIn('APPLICATION INDEX', canonical(reader))

    def test_anchored_one_reconciliation_restores_full_literal(self):
        expected = intent(effect(self.values))
        chat = ScriptedReadings(expected, self.draft({**self.values, "name": "Synthetic Harbor"}),
            [wire(("declare_skill_intent", expected)), self.draft(self.values)])
        plan = self.declare(chat)
        self.assertEqual(plan["steps"][0]["inputs"], self.values)
        self.assertTrue(plan["reconciled"])
        self.assertEqual(len(chat.messages), 4)

    def test_anchored_confirmed_omission_stops(self):
        self.request += " Phone 5550104."
        expected = intent(effect({**self.values, "phone": "5550104"}))
        chat = ScriptedReadings(expected, self.draft(self.values),
            [wire(("declare_skill_intent", expected)), self.draft(self.values)])
        with self.assertRaisesRegex(ValueError, "supplied value not proposed"):
            self.declare(chat)
        self.assertEqual(len(chat.messages), 4)

    def test_anchored_malformed_reader_corrects_without_redrafting(self):
        expected = intent(effect(self.values))
        chat = ScriptedReadings({**expected, "write_intent": True}, self.draft(self.values),
            [wire(("declare_skill_intent", expected))])
        plan = self.declare(chat)
        self.assertEqual(plan["steps"][0]["inputs"], self.values)
        self.assertEqual(len(chat.messages), 3)
        self.assertIn('rejected_declarations', canonical(chat.messages[-1][0]))

    def test_anchor_resolves_candidate_ids_and_rejects_invented_literal(self):
        expected = intent({"action": "update", "target": "v1", "facts": [
            {"kind": "reference", "value": "v1"}, {"kind": "text", "value": "Exact Value"}]})
        parsed = parse_intent(wire(("declare_skill_intent", expected)), ("Set Exact Value",),
            (SimpleNamespace(id="v1", display="Synthetic Harbor (owner A, ID 7)"),))
        self.assertEqual(parsed["effects"][0]["target"], "Synthetic Harbor (owner A, ID 7)")
        with self.assertRaisesRegex(ValueError, "exact text"):
            parse_intent(wire(("declare_skill_intent", expected)), ("No such value",),
                (SimpleNamespace(id="v1", display="Synthetic Harbor (owner A, ID 7)"),))

    def test_anchored_reads_require_their_original_dependency(self):
        goal = {"deliverables": [{"kind": "fact", "entity": "contacts", "record": "@result:0", "fields": ["Email"]}]}
        expected = parse_intent(wire(("declare_skill_intent", intent(effect(self.values),
            goals=[{"after_effect": 1, "goal": goal}]))), (self.request,))
        operation = {"kind": "operation", "operation": self.definition.name, "inputs": self.values}
        with self.assertRaisesRegex(ValueError, "read/page"):
            compare(expected, {"steps": [operation]}, (self.definition,))
        compare(expected, {"steps": [operation, {"kind": "goal", "goal": goal}]}, (self.definition,))

    def test_preparation_cannot_substitute_execution(self):
        expected = parse_intent(wire(("declare_skill_intent", intent(mode="prepare"))))
        with self.assertRaisesRegex(ValueError, "does not execute"):
            compare(expected, {"steps": [{"kind": "operation", "operation": self.definition.name,
                "inputs": self.values}]}, (self.definition,))

    def test_prepared_effect_preservation_and_all_receipts_are_required(self):
        requested = RequestedEffect("contacts", "update", "contacts", "Harbor", canonical({"remark": ""}), ("email",))
        projection = EffectProjection(replace(requested, preserve=()), preserved_json=canonical({"email": "h@example.test"}),
            required_receipts=("record", "unrelated_field_preservation"))
        projection.require(requested)
        with self.assertRaises(BusinessToolRequestError):
            replace(projection, preserved_json="{}").require(requested)
        all_others = replace(requested, preserve=("*",))
        with self.assertRaises(BusinessToolRequestError):
            projection.require(all_others)
        replace(projection, preserves_unmentioned=True).require(all_others)
        with self.assertRaises(BusinessToolRequestError):
            projection.require(replace(requested, values_json=canonical({"remark": "extra"})))
        partial = {"verified": True, "record_id": "7", "effects": [{"effect": "record", "verified": True}]}
        with self.assertRaises(BusinessMutationIndeterminate) as caught:
            projection.require_receipt(partial)
        self.assertEqual(caught.exception.reconciliation["record_id"], "7")
        projection.require_receipt({**partial, "effects": [*partial["effects"],
            {"effect": "unrelated_field_preservation", "verified": True}]})


if __name__ == "__main__":
    unittest.main()
