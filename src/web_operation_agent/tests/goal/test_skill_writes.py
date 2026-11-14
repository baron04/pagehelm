"""A model-driven turn's Skill writes over a synthetic application; no model, no browser."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from web_operation_agent.business.approvals import ApprovalWatch
from web_operation_agent.business.effects import EffectProjection, RequestedEffect, canonical
from web_operation_agent.business.errors import (BusinessMutationIndeterminate,
                                                 BusinessMutationRefusedError,
                                                 BusinessToolPermissionError,
                                                 BusinessToolReferenceError)
from web_operation_agent.business.runtime import BusinessToolRuntime
from web_operation_agent.business.tools.contracts import (BusinessMutationSpec, BusinessToolResult,
                                                          ToolOperation)
from web_operation_agent.business.tools.registry import ToolDefinition, ToolRegistry
from web_operation_agent.composition.business_operations import RegisteredOperations
from web_operation_agent.goal.skill_writes import SkillWrites
from web_operation_agent.schemas.approval_details import ApprovalDetail
from web_operation_agent.skills import SkillArtifact, SkillRegistry
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.state.ledger import SessionLedger

SKILL = "notes.note-revision"
REQUEST = "Revise Cedar: the body becomes Reviewed in May."


class Notes:
    """Named notes whose body one operation revises; also that operation's effect contract."""

    def __init__(self):
        self.records = {"Cedar": "draft", "Birch": "draft"}
        self.prepared, self.dispatched = [], []
        self.prepare_error = self.dispatch_error = None
        self.drift = ""

    def project_inputs(self, inputs):
        return RequestedEffect("note", "update", "note", inputs["target"],
                               canonical({"body": inputs["body"]}))

    def prepare(self, args, *, context=None):
        self.prepared.append(dict(args))
        if self.prepare_error is not None:
            raise self.prepare_error
        if args["target"] not in self.records:
            raise BusinessToolReferenceError("note is missing or ambiguous", entity="note",
                                             choices=tuple(sorted(self.records)))
        change = self.project_inputs({**args, "body": args["body"] + self.drift})
        return BusinessMutationSpec(
            dict(args), "Revise note", (args["target"],),
            details=(ApprovalDetail("change", "body", f"{self.records[args['target']]} → {args['body']}"),),
            effect_projection=EffectProjection(change, required_receipts=("record",)))

    def dispatch(self, args, *, context=None):
        self.dispatched.append(dict(args))
        if self.dispatch_error is not None:
            raise self.dispatch_error
        self.records[args["target"]] = args["body"]
        return BusinessToolResult(
            {"verified": True, "entity": "note", "record_id": args["target"],
             "reference": args["target"], "effects": [{"effect": "record", "verified": True}]},
            {"verified": True})


class Person:
    """Answers each approval as scripted and keeps what was shown."""

    def __init__(self, decision="approved"):
        self.decision, self.shown = decision, []

    def __call__(self, proposal, *, context=None):
        self.shown.append(proposal)
        return NS(decision=self.decision, plan_digest=proposal.content_digest,
                  operation_context_digest=context.binding_digest, approval_id=f"a{len(self.shown)}")


class SkillWritesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notes, self.person, self.invalidated = Notes(), Person(), []
        tools = ToolRegistry((ToolDefinition("revise_note", "Revise the body of one existing note", json.dumps({
            "type": "object", "properties": {"target": {"type": "string", "minLength": 1},
                                             "body": {"type": "string", "maxLength": 2000}},
            "required": ["target", "body"], "additionalProperties": False}),
            "write", "notes.write", ToolOperation("note", "update"), self.notes,
            reference_inputs=("target",), literal_inputs=("body",), effect_contract=self.notes),))
        skills = SkillRegistry((SkillArtifact.from_files("notes", {
            "SKILL.md": b"---\nname: note-revision\ndescription: Revise an existing note.\n---\n# Revise\nAsk when the note is ambiguous.\n",
            "agents/woa.toml": b'schema_version = 1\nwrite_operations = ["revise_note"]\n',
            "references/meaning.md": b"A note belongs to its team."}),
            SkillArtifact.from_files("notes", {
                "SKILL.md": b"---\nname: note-glossary\ndescription: What the words mean.\n---\nKnowledge only.\n"})))
        runtime = BusinessToolRuntime.from_registry(tools, application_id="notes",
                                                    skill_registry=skills, require_skill_for_writes=True)
        self.application = NS(business_tools=runtime, ports=NS(
            operations=RegisteredOperations(tools, skills, self.invalidated.append)))

    def writes(self, *, scopes=("notes.write",), enabled=True):
        now = int(time.time() * 1000)
        principal = PrincipalContext(1, "notes", "1", "7", "9", "synthetic", now - 1000, now + 60000, scopes)
        self.ledger = SessionLedger(Path(self.tmp.name) / "audit.jsonl", "run")
        approvals = ApprovalWatch(ledger=self.ledger, run_id="run", principal=principal,
                                  session_id="session", turn_id="turn",
                                  business_approval=self.person if enabled else None,
                                  cancelled=lambda: False, approval_hold_s=30)
        approvals.start_turn(now + 30000)
        return SkillWrites(self.application, approvals)

    def events(self):
        return [event["event"] for event in self.ledger.events]

    def propose(self, writes, body="Reviewed in May", target="Cedar", step=1):
        return writes.propose("revise_note", {"target": target, "body": body}, step=step, texts=(REQUEST,))

    def test_preparation_can_reach_real_approval_but_cannot_dispatch_at_either_boundary(self):
        from web_operation_agent.business.errors import BusinessToolRequestError
        original = self.application.ports.operations
        tools = ToolRegistry(original.tools.definitions, mutation_dispatch_enabled=False)
        runtime = BusinessToolRuntime.from_registry(tools, application_id="notes",
            skill_registry=original.skills, require_skill_for_writes=True,
            mutation_dispatch_enabled=False)
        self.application.business_tools = runtime
        self.application.ports.operations = RegisteredOperations(tools, original.skills, self.invalidated.append)
        writes = self.writes()
        writes.load(SKILL, step=0)
        outcome = self.propose(writes)
        self.assertEqual(outcome.state, "refused")
        self.assertEqual(len(self.person.shown), 1)
        self.assertTrue(self.notes.prepared)
        self.assertEqual(self.notes.dispatched, [])
        with self.assertRaises(BusinessToolRequestError):
            tools.execute_mutation("revise_note", {"target": "Cedar", "body": "bypass"})
        self.assertEqual(self.notes.dispatched, [])
        self.assertEqual(self.notes.records["Cedar"], "draft")

    def test_only_a_skill_that_offers_this_actor_an_operation_is_in_the_catalog(self):
        catalog = self.writes().catalog()
        self.assertEqual([(skill.skill_id, [d.name for d in definitions]) for skill, definitions in catalog],
                         [(SKILL, ["revise_note"])])
        self.assertEqual(self.writes(scopes=("notes.read",)).catalog(), ())
        self.assertEqual(self.writes(enabled=False).catalog(), ())

    def test_nothing_is_proposed_before_its_skill_is_loaded(self):
        writes = self.writes()
        self.assertEqual(writes.definitions(), ())
        outcome = self.propose(writes)
        self.assertEqual((outcome.state, outcome.asked), ("refused", False))
        self.assertEqual((self.notes.prepared, self.person.shown), ([], []))
        for unknown in ("notes.note-glossary", "notes.absent", "", None):
            with self.subTest(skill=unknown), self.assertRaises(ValueError):
                writes.load(unknown, step=1)
        skill = writes.load(SKILL, step=1)
        writes.load(SKILL, step=2)
        self.assertEqual(skill.name, "note-revision")
        self.assertEqual([d.name for d in writes.definitions()], ["revise_note"])
        self.assertEqual(self.events().count("business_skill_loaded"), 1)

    def test_a_loaded_skill_s_reference_is_read_and_recorded(self):
        writes = self.writes()
        with self.assertRaises(ValueError):
            writes.resource(SKILL, "references/meaning.md", step=1)
        writes.load(SKILL, step=1)
        self.assertEqual(writes.resource(SKILL, "references/meaning.md", step=1), "A note belongs to its team.")
        with self.assertRaises(ValueError):
            writes.resource(SKILL, "agents/woa.toml", step=1)
        self.assertEqual(self.events(), ["business_skill_loaded", "business_skill_resource_loaded"])

    def test_an_approved_proposal_is_dispatched_once_and_its_result_is_the_application_s(self):
        writes = self.writes()
        writes.load(SKILL, step=1)
        outcome = self.propose(writes, step=2)
        self.assertEqual((outcome.state, outcome.asked, outcome.reason), ("completed", True, ""))
        self.assertEqual(outcome.result.content["reference"], "Cedar")
        self.assertEqual(self.notes.records["Cedar"], "Reviewed in May")
        self.assertEqual(len(self.person.shown), 1)
        self.assertEqual(self.person.shown[0].details[0].text, "draft → Reviewed in May")
        self.assertEqual(self.person.shown[0].skill_id, SKILL)
        self.assertEqual(self.invalidated, [frozenset({"note"})])
        self.assertEqual(self.events(), [
            "business_skill_loaded", "skill_effect_bound", "business_mutation_proposed",
            "business_mutation_execution_started", "business_mutation_result"])
        # The same proposal again in this turn asks nobody and writes nothing.
        again = self.propose(writes, step=3)
        self.assertEqual((again.state, again.asked), ("repeated", False))
        self.assertEqual((len(self.person.shown), len(self.notes.dispatched)), (1, 1))

    def test_each_further_write_needs_its_own_approval(self):
        writes = self.writes()
        writes.load(SKILL, step=1)
        self.propose(writes, step=2)
        outcome = writes.propose("revise_note", {"target": "Birch", "body": "Reviewed in May"},
                                 step=3, texts=(REQUEST,))
        self.assertEqual(outcome.state, "completed")
        self.assertEqual(len(self.person.shown), 2)
        self.assertNotEqual(self.person.shown[0].content_digest, self.person.shown[1].content_digest)

    def test_text_the_person_did_not_supply_is_refused_before_preparation(self):
        writes = self.writes()
        writes.load(SKILL, step=1)
        for body in ("Reviewed in June", "reviewed in may", "Review"):
            with self.subTest(body=body):
                outcome = self.propose(writes, body=body)
                self.assertEqual((outcome.state, outcome.asked), ("refused", False))
        self.assertEqual((self.notes.prepared, self.person.shown), ([], []))
        self.assertIn("skill_operation_refused", self.events())

    def test_inputs_outside_the_installed_schema_are_refused_before_preparation(self):
        writes = self.writes()
        writes.load(SKILL, step=1)
        for inputs in ({"target": "Cedar"}, {"target": "Cedar", "body": "Reviewed in May", "id": 7}, "Cedar"):
            with self.subTest(inputs=inputs):
                outcome = writes.propose("revise_note", inputs, step=2, texts=(REQUEST,))
                self.assertEqual((outcome.state, outcome.asked), ("refused", False))
        self.assertEqual(self.notes.prepared, [])

    def test_a_prepared_effect_that_is_not_the_proposed_one_never_reaches_the_person(self):
        writes = self.writes()
        writes.load(SKILL, step=1)
        self.notes.drift = " and more"
        outcome = self.propose(writes)
        self.assertEqual((outcome.state, outcome.asked, outcome.reason), ("refused", False, "agent_refused"))
        self.assertEqual((self.person.shown, self.notes.dispatched), ([], []))

    def test_an_unresolved_target_returns_its_candidates_and_asks_nobody(self):
        writes = self.writes()
        writes.load(SKILL, step=1)
        outcome = self.propose(writes, target="Ced")
        self.assertEqual((outcome.state, outcome.asked), ("reference", False))
        self.assertEqual(outcome.error.choices, ("Birch", "Cedar"))
        self.assertEqual((self.person.shown, self.notes.dispatched), ([], []))

    def test_a_declined_proposal_is_not_dispatched(self):
        self.person.decision = "denied"
        writes = self.writes()
        writes.load(SKILL, step=1)
        outcome = self.propose(writes)
        self.assertEqual((outcome.state, outcome.asked, outcome.reason),
                         ("not_approved", True, "approval_denied"))
        self.assertEqual(self.notes.dispatched, [])
        self.assertEqual(self.notes.records["Cedar"], "draft")

    def test_an_uncertain_write_is_recorded_and_never_sent_again(self):
        writes = self.writes()
        writes.load(SKILL, step=1)
        self.notes.dispatch_error = BusinessMutationIndeterminate("acknowledged; readback failed",
                                                                  phase="postcondition")
        outcome = self.propose(writes)
        self.assertEqual((outcome.state, outcome.reason), ("indeterminate", "indeterminate_write"))
        self.assertEqual(len(self.notes.dispatched), 1)
        self.assertEqual(writes.approvals.business_result()["mutation_indeterminate"], 1)
        self.assertIn("business_mutation_indeterminate", self.events())
        # The unresolved key blocks the same write for as long as it is unsettled.
        self.notes.dispatch_error = None
        blocked = self.propose(writes, step=2)
        self.assertEqual((blocked.state, blocked.reason), ("refused", "business_mutation_blocked"))
        self.assertEqual(len(self.notes.dispatched), 1)

    def test_the_actor_s_refusal_and_a_change_after_approval_end_the_proposal(self):
        writes = self.writes()
        writes.load(SKILL, step=1)
        self.notes.prepare_error = BusinessToolPermissionError("the actor may not revise this note")
        denied = self.propose(writes)
        self.assertEqual((denied.state, denied.reason, denied.asked),
                         ("permission_denied", "permission_denied", False))
        self.notes.prepare_error = None
        self.notes.dispatch_error = BusinessMutationRefusedError("the note changed after approval",
                                                                 reason="precondition_changed")
        changed = self.propose(writes, step=2)
        self.assertEqual((changed.state, changed.reason, changed.asked),
                         ("refused", "business_mutation_refused", True))
        self.assertEqual(self.notes.records["Cedar"], "draft")


if __name__ == "__main__":
    unittest.main()
