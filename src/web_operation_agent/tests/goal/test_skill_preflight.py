"""Whole-task admission and one shared correction stop before any write."""
import unittest
from dataclasses import replace
from types import SimpleNamespace

from web_operation_agent.business.effects import RequestedEffect, EffectProjection, canonical
from web_operation_agent.business.entities import ENTITY_READ_SCHEMAS, QUERY_ENTITY_RECORDS_SCHEMA, RESOLVE_RECORD_REFERENCE_SCHEMA
from web_operation_agent.business.errors import (BusinessToolRequestError, BusinessToolContractError,
    BusinessToolPermissionError, BusinessToolReferenceError)
from web_operation_agent.goal.complete_task import declare_single_task
from web_operation_agent.goal.skill_preflight import CorrectionBudget, PreparationError, preflight_plan
from web_operation_agent.goal.index import ApplicationIndex
from web_operation_agent.skills import SkillArtifact, SkillContext, SkillRegistry
from web_operation_agent.tests.goal import test_complete_task as fixture


class SkillPreflightTests(unittest.TestCase):
    def setUp(self):
        base = fixture.CompleteTaskTests()
        base.setUp()
        self.base, self.definition = base, base.definition

    def operation(self, values=None):
        return {"kind": "operation", "operation": self.definition.name,
                "skill_id": self.base.context.loaded[0].skill_id,
                "inputs": values or self.base.values}

    def projection(self, values):
        return SimpleNamespace(effect_projection=EffectProjection(
            self.definition.effect_contract.project_inputs(values), required_receipts=("record",)))

    def test_later_prepare_failure_is_found_before_the_caller_can_approve(self):
        calls = []
        plan = {"steps": [self.operation(), self.operation({**self.base.values, "email": "later@example.test"})]}
        def prepare(position, step):
            calls.append(position)
            if position == 1:
                raise BusinessToolRequestError("synthetic later precondition")
            return self.projection(step["inputs"])
        with self.assertRaises(PreparationError) as raised:
            preflight_plan(plan, (self.definition,), prepare=prepare)
        self.assertEqual(raised.exception.position, 1)
        self.assertEqual(calls, [0, 1])

    def test_dependent_prepare_is_deferred_but_forward_dependency_is_rejected(self):
        definition = replace(self.definition, literal_inputs=("email",), reference_inputs=("name",))
        steps = [self.operation(), self.operation({"name": "@result:0", "email": "later@example.test"})]
        calls = []
        preflight_plan({"steps": steps}, (definition,),
            prepare=lambda position, step: calls.append(position) or self.projection(step["inputs"]))
        self.assertEqual(calls, [0])
        with self.assertRaises(PreparationError):
            preflight_plan({"steps": [steps[1]]}, (definition,), prepare=lambda *args: self.fail("prepared forward reference"))

    def test_unavailable_page_capability_is_checked_before_preparation(self):
        plan = {"steps": [{"kind": "goal", "goal": {"deliverables": [{"kind": "page",
                "entity": "contacts", "section": "Lines"}]}}, self.operation()]}
        def unavailable(goal):
            raise BusinessToolRequestError("sections is unavailable")
        with self.assertRaises(BusinessToolRequestError):
            preflight_plan(plan, (self.definition,), check_goal=unavailable,
                prepare=lambda *args: self.fail("prepared after known capability gap"))

    def declare(self, chat, preflight, budget=None):
        return declare_single_task(chat, request=self.base.request, index=self.base.index,
            context=self.base.context, definitions=(self.definition,), run_id="preflight",
            remaining=lambda: 10, cancelled=lambda: False, correction_budget=budget,
            preflight=preflight)

    def test_prepare_correction_replaces_only_failed_operation_and_preserves_other_steps(self):
        wrong = {**self.base.values, "email": "other@example.test"}
        self.base.request += ' Then create a second contact with other@example.test.'
        first = self.base.draft(self.base.values)["tool_calls"][0]
        second = self.base.draft(wrong)["tool_calls"][0]
        second["id"] = "second"
        finish = fixture.wire(("finish_skill_task", {}))["tool_calls"][0]
        finish["id"] = "finish"
        chat = self.base.sequence([{"tool_calls": [first, second, finish]},
                                  {"tool_calls": [second]}])
        seen = []
        def check(plan):
            seen.append(plan)
            if len(seen) == 1:
                raise PreparationError(1, BusinessToolRequestError("refresh synthetic preparation"))
        budget = CorrectionBudget()
        result = self.declare(chat, check, budget)
        self.assertEqual(result["steps"][0]["inputs"], self.base.values)
        self.assertEqual(result["steps"][1]["inputs"], wrong)
        self.assertTrue(budget.used)
        self.assertEqual(len(chat.messages), 2)

    def test_schema_and_prepare_share_one_correction_budget(self):
        chat = self.base.sequence([self.base.draft({**self.base.values, "name": "Invented"}),
                                  self.base.draft(self.base.values)])
        def reject(plan):
            raise PreparationError(0, BusinessToolRequestError("prepare failed after schema correction"))
        with self.assertRaises(PreparationError):
            self.declare(chat, reject)
        self.assertEqual(len(chat.messages), 2)

    def test_permission_failure_never_requests_model_correction(self):
        chat = self.base.sequence([self.base.draft(self.base.values)])
        def denied(plan):
            raise BusinessToolPermissionError("synthetic denied")
        with self.assertRaises(BusinessToolPermissionError):
            self.declare(chat, denied)
        self.assertEqual(len(chat.messages), 1)

    def lookup(self):
        return {"type": "function", "function": {"name": "lookup_records",
            "description": "Read current actor-visible reference candidates.", "parameters": {
                "type": "object", "properties": {"name": {"type": "string"}},
                "required": ["name"], "additionalProperties": False}}}

    def test_current_read_results_return_to_the_same_agent_before_the_proposal(self):
        chat = self.base.sequence([fixture.wire(("lookup_records", {"name": "Synthetic Harbor"})),
                                  self.base.draft(self.base.values)])
        reads = []
        def lookup(name, args):
            reads.append((name, args))
            return {"candidates": ["Synthetic Harbor Research"], "complete": True}
        plan = self.base.declare(chat, single=True, read_tools=(self.lookup(),), read_tool=lookup)
        self.assertEqual(reads, [("lookup_records", {"name": "Synthetic Harbor"})])
        self.assertFalse(plan["correction_used"])
        self.assertEqual(len(plan["steps"]), 1)
        feedback = next(message for message in chat.messages[1] if message["role"] == "tool")
        self.assertIn('"candidates":["Synthetic Harbor Research"]', feedback["content"])
        self.assertIn('"business_dispatched":false', feedback["content"])

    def test_mixed_reads_defer_every_proposal_until_the_same_agent_observes_facts(self):
        # Even an invalid operation and finish in the read reply are not accepted
        # or prepared. The fresh proposal is the only one that enters preflight.
        chat = self.base.sequence([fixture.wire(
            ("lookup_records", {"name": "Synthetic Harbor"}),
            ("create_contact", {"inputs": {"name": "Invented"}}),
            ("finish_skill_task", {})), self.base.draft(self.base.values)])
        seen, prepared = [], []
        result = self.base.declare(chat, single=True, read_tools=(self.lookup(),),
            read_tool=lambda n, a: seen.append((n, a)) or {"candidates": ["Synthetic Harbor Research"]},
            preflight=lambda plan: prepared.append(plan))
        self.assertEqual(seen, [("lookup_records", {"name": "Synthetic Harbor"})])
        self.assertEqual(len(prepared), 1)
        self.assertEqual([s["inputs"] for s in result["steps"]], [self.base.values])
        self.assertFalse(result["correction_used"])
        feedback = [m for m in chat.messages[1] if m["role"] == "tool"]
        self.assertIn('"current_facts"', feedback[0]["content"])
        self.assertTrue(all('"proposal_deferred":true' in m["content"] for m in feedback[1:]))

    def test_mixed_reads_keep_prior_accepted_proposals_without_duplicating_them(self):
        chat = self.base.sequence([self.base.draft(self.base.values, finish=False),
            fixture.wire(("lookup_records", {"name": "Synthetic Harbor"}),
                         ("finish_skill_task", {})), fixture.wire(("finish_skill_task", {}))])
        result = self.base.declare(chat, single=True, read_tools=(self.lookup(),),
            read_tool=lambda *a: {"candidates": []})
        self.assertEqual(len(result["steps"]), 1)
        self.assertFalse(result["correction_used"])

    def test_resources_and_actor_reads_share_a_reply_without_becoming_proposals(self):
        guide = SkillArtifact.from_files("fixture", {
            "SKILL.md": b"---\nname: guide\ndescription: Synthetic reference guide.\n---\nRead references/guide.md.\n",
            "references/guide.md": b"Use the current actor's candidates."})
        self.base.context = SkillContext(SkillRegistry((*self.base.context.registry.artifacts, guide)))
        for artifact in self.base.context.registry.artifacts:
            self.base.context.load(artifact.skill_id)
        chat = self.base.sequence([fixture.wire(
            ("read_skill_resource", {"skill_id": guide.skill_id, "path": "references/guide.md"}),
            ("lookup_records", {"name": "Synthetic Harbor"})), self.base.draft(self.base.values)])
        loaded = []
        result = self.base.declare(chat, single=True, read_tools=(self.lookup(),),
            read_tool=lambda *a: {"candidates": []},
            resource_loaded=lambda s, p, t: loaded.append((p, t)))
        self.assertEqual(loaded, [("references/guide.md", "Use the current actor's candidates.")])
        self.assertFalse(result["correction_used"])

    def test_mixed_reads_reject_unknown_calls_before_any_read_runs(self):
        reply = fixture.wire(("lookup_records", {"name": "Synthetic Harbor"}), ("unknown_write", {}))
        chat = self.base.sequence([reply, reply])
        with self.assertRaises(BusinessToolRequestError):
            self.base.declare(chat, single=True, read_tools=(self.lookup(),),
                              read_tool=lambda *a: self.fail("read ran before batch validation"))
        self.assertEqual(len(chat.messages), 2)

    def test_deferred_proposals_do_not_increase_the_schema_correction_budget(self):
        mixed = fixture.wire(("lookup_records", {"name": "Synthetic Harbor"}),
                             ("finish_skill_task", {}))
        invalid = self.base.draft({**self.base.values, "name": "Invented"})
        chat = self.base.sequence([mixed, invalid, invalid])
        with self.assertRaises(BusinessToolRequestError):
            self.base.declare(chat, single=True, read_tools=(self.lookup(),), read_tool=lambda *a: {})
        self.assertEqual(len(chat.messages), 3)

    def test_read_permission_failure_does_not_enter_the_model_correction_path(self):
        chat = self.base.sequence([fixture.wire(("lookup_records", {"name": "Synthetic Harbor"}))])
        def denied(*args):
            raise BusinessToolPermissionError("synthetic read denied")
        with self.assertRaises(BusinessToolPermissionError):
            self.base.declare(chat, single=True, read_tools=(self.lookup(),), read_tool=denied)
        self.assertEqual(len(chat.messages), 1)

    def test_all_read_arguments_are_checked_before_any_read_runs(self):
        invalid = fixture.wire(("lookup_records", {"name": "Synthetic Harbor"}),
                               ("lookup_records", {"name": 42}))
        chat = self.base.sequence([invalid, self.base.draft(self.base.values)])
        result = self.base.declare(chat, single=True, read_tools=(self.lookup(),),
            read_tool=lambda *a: self.fail("first read ran before second call validation"))
        self.assertTrue(result["correction_used"])
        self.assertEqual(len(chat.messages), 2)

    def test_read_correction_names_missing_schema_keys_without_echoing_input(self):
        chat = self.base.sequence([fixture.wire(("lookup_records", {})), self.base.draft(self.base.values)])
        result = self.base.declare(chat, single=True, read_tools=(self.lookup(),),
            read_tool=lambda *a: self.fail("invalid read input must not dispatch"))
        self.assertTrue(result["correction_used"])
        self.assertIn("missing keys: name", chat.messages[1][-1]["content"])

    def test_generic_query_schema_keeps_dynamic_filter_values_for_the_application(self):
        args = {"entity_id": "entity_" + "a" * 32, "field_ids": ["field_" + "b" * 32],
                "filters": [{"field_id": "field_" + "b" * 32, "operator": "contains", "value": "Synthetic"}]}
        name = QUERY_ENTITY_RECORDS_SCHEMA["function"]["name"]
        chat = self.base.sequence([fixture.wire((name, args)), self.base.draft(self.base.values)])
        seen = []
        result = self.base.declare(chat, single=True, read_tools=(QUERY_ENTITY_RECORDS_SCHEMA,),
            read_tool=lambda tool, inputs: seen.append((tool, inputs)) or {"records": []})
        self.assertEqual(seen, [(name, args)])
        self.assertFalse(result["correction_used"])

    def test_generic_read_patterns_unique_fields_and_selector_union_are_checked_before_reads(self):
        query = QUERY_ENTITY_RECORDS_SCHEMA["function"]["name"]
        reference = RESOLVE_RECORD_REFERENCE_SCHEMA["function"]["name"]
        valid = (query, {"entity_id": "entity_" + "a" * 32, "field_ids": []})
        invalid = [
            (query, {"entity_id": "invented-entity-id", "field_ids": []}),
            (query, {"entity_id": "entity_" + "a" * 32, "field_ids": ["field_" + "b" * 32] * 2}),
            (reference, {"reference": "Synthetic", "entity_query": "Contacts", "entity_id": "entity_" + "a" * 32}),
        ]
        for call in invalid:
            with self.subTest(call=call):
                chat = self.base.sequence([fixture.wire(valid, call), self.base.draft(self.base.values)])
                result = self.base.declare(chat, single=True,
                    read_tools=(QUERY_ENTITY_RECORDS_SCHEMA, RESOLVE_RECORD_REFERENCE_SCHEMA),
                    read_tool=lambda *a: self.fail("a read ran before the complete batch was validated"))
                self.assertTrue(result["correction_used"])
                self.assertEqual(len(chat.messages), 2)

    def test_read_schema_cannot_fetch_a_remote_reference_or_enter_model_correction(self):
        tool = self.lookup()
        tool["function"]["parameters"] = {"$ref": "https://example.invalid/private-schema"}
        chat = self.base.sequence([fixture.wire(("lookup_records", {"name": "Synthetic"}))])
        with self.assertRaises(BusinessToolContractError):
            self.base.declare(chat, single=True, read_tools=(tool,),
                read_tool=lambda *a: self.fail("an unresolved schema must not execute a read"))
        self.assertEqual(len(chat.messages), 1)

    def test_same_agent_discovers_describes_and_queries_before_proposing(self):
        entity, field = 'entity_' + 'a' * 32, 'field_' + 'b' * 32
        calls = [('resolve_record_reference', {'entity_query': 'Contacts', 'reference': 'Synthetic'}),
                 ('describe_entity', {'entity_id': entity}),
                 ('query_entity_records', {'entity_id': entity, 'field_ids': [field]})]
        facts = {'resolve_record_reference': {'candidates': [{'entity_id': entity, 'record_label': 'Synthetic'}]},
                 'describe_entity': {'entity_id': entity, 'fields': [{'field_id': field, 'label': 'Email'}]},
                 'query_entity_records': {'records': []}}
        chat = self.base.sequence([*(fixture.wire(call) for call in calls), self.base.draft(self.base.values)])
        seen = []
        result = self.base.declare(chat, single=True,
            read_tools=(*ENTITY_READ_SCHEMAS, RESOLVE_RECORD_REFERENCE_SCHEMA),
            read_tool=lambda name, args: seen.append((name, args)) or facts[name])
        self.assertEqual(seen, calls)
        self.assertFalse(result['correction_used'])
        self.assertEqual(len(result['steps']), 1)

    def test_unobserved_query_in_a_discovery_batch_runs_no_read(self):
        chat = self.base.sequence([fixture.wire(
            ('discover_entities', {'query': 'Contacts'}),
            ('query_entity_records', {'entity_id': 'entity_' + 'a' * 32, 'field_ids': []})),
            self.base.draft(self.base.values)])
        result = self.base.declare(chat, single=True,
            read_tools=(*ENTITY_READ_SCHEMAS, RESOLVE_RECORD_REFERENCE_SCHEMA),
            read_tool=lambda *a: self.fail('a read ran before identities were available for the whole batch'))
        self.assertTrue(result['correction_used'])

    def test_prepare_reference_feedback_can_read_then_replace_without_moving_dependencies(self):
        self.definition = replace(self.definition, reference_inputs=("name",), literal_inputs=("email",))
        values = [self.base.values, {**self.base.values, "name": "Synthetic Second"},
                  {**self.base.values, "name": "@result:1"}]
        def proposal(inputs):
            return (self.definition.name, {"request_intent": "execute", "inputs": {
                "name": inputs["name"], "email": {"source": 0, "text": inputs["email"]}}})
        resolved = {**values[1], "name": "Synthetic Second (owner B)"}
        chat = self.base.sequence([fixture.wire(*(proposal(v) for v in values), ("finish_skill_task", {})),
            fixture.wire(("lookup_records", {"name": "Synthetic Second"})), fixture.wire(proposal(resolved))])
        seen, reads = [], []
        def prepare(position, step):
            seen.append((position, step["inputs"]["name"]))
            if step["inputs"]["name"] == "Synthetic Second":
                raise BusinessToolReferenceError("synthetic ambiguous target", entity="contacts",
                    choices=("Synthetic Second (owner A)", "Synthetic Second (owner B)"))
            return self.projection(step["inputs"])
        def check(plan):
            preflight_plan(plan, (self.definition,), prepare=prepare, recover_references=True)
        result = declare_single_task(chat, request=self.base.request, index=self.base.index,
            context=self.base.context, definitions=(self.definition,), run_id="reference-recovery",
            remaining=lambda: 10, cancelled=lambda: False, preflight=check,
            read_tools=(self.lookup(),), read_tool=lambda name, args:
                reads.append((name, args)) or {"candidates": [resolved["name"]]})
        self.assertEqual(reads, [("lookup_records", {"name": "Synthetic Second"})])
        self.assertEqual([s["inputs"] for s in result["steps"]], [values[0], resolved, values[2]])
        self.assertEqual(seen, [(0, values[0]["name"]), (1, values[1]["name"]),
                                (0, values[0]["name"]), (1, resolved["name"])])
        self.assertTrue(result["correction_used"])
        self.assertIn('"code":"business_reference"', chat.messages[1][-1]["content"])
        self.assertIn('"current_facts"', chat.messages[2][-1]["content"])

    def test_read_failure_keeps_prior_results_and_marks_remaining_calls_unrun(self):
        chat = self.base.sequence([fixture.wire(*[("lookup_records", {"name": str(i)}) for i in range(3)]),
                                  self.base.draft(self.base.values)])
        executed = []
        def lookup(name, args):
            executed.append(args["name"])
            if args["name"] == "1":
                raise BusinessToolRequestError("synthetic invalid lookup condition")
            return {"candidates": ["Synthetic Harbor Research"]}
        result = self.base.declare(chat, single=True, read_tools=(self.lookup(),), read_tool=lookup)
        self.assertEqual(executed, ["0", "1"])
        feedback = [m for m in chat.messages[1] if m["role"] == "tool"]
        self.assertEqual(len(feedback), 3)
        self.assertIn('"current_facts"', feedback[0]["content"])
        self.assertIn('"code":"business_request"', feedback[1]["content"])
        self.assertIn('"not_run":true', feedback[2]["content"])
        self.assertTrue(result["correction_used"])

    def test_reference_still_ambiguous_after_one_correction_preserves_clarification_type(self):
        chat = self.base.sequence([self.base.draft(self.base.values),
            fixture.wire(("lookup_records", {"name": "Synthetic Harbor"})),
            {"tool_calls": [self.base.draft(self.base.values)["tool_calls"][0]]}])
        error = BusinessToolReferenceError("synthetic ambiguous target", entity="contacts",
            choices=("Synthetic Harbor (owner A)", "Synthetic Harbor (owner B)"))
        def check(plan):
            raise PreparationError(0, error)
        with self.assertRaises(BusinessToolReferenceError) as raised:
            self.base.declare(chat, single=True, preflight=check, read_tools=(self.lookup(),),
                              read_tool=lambda *a: {"candidates": list(error.choices)})
        self.assertIs(raised.exception, error)
        self.assertEqual(len(chat.messages), 3)

    def test_reference_recovery_is_opt_in_and_never_masks_permission_denial(self):
        error = BusinessToolReferenceError("synthetic ambiguous target", entity="contacts")
        def ambiguous(*args):
            raise error
        with self.assertRaises(BusinessToolReferenceError) as raised:
            preflight_plan({"steps": [self.operation()]}, (self.definition,), prepare=ambiguous)
        self.assertIs(raised.exception, error)
        with self.assertRaises(PreparationError) as raised:
            preflight_plan({"steps": [self.operation()]}, (self.definition,), prepare=ambiguous,
                           recover_references=True)
        self.assertIs(raised.exception.cause, error)
        def denied(*args):
            raise BusinessToolPermissionError("synthetic permission denied")
        with self.assertRaises(BusinessToolPermissionError):
            preflight_plan({"steps": [self.operation()]}, (self.definition,), prepare=denied,
                           recover_references=True)
