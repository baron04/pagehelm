"""Business Skill loading and read-only runtime-tool transitions."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from web_operation_agent.business.runtime import (
    APPLICATION_SAVES_ON_LEAVE, BusinessSelectionOption, BusinessSkill,
    BusinessToolResult, BusinessToolRuntime, FormDraft, FormDraftField,
)
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.answer.evidence import Origin, RecordEvidence
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.schemas import ElementRef
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.tests.support.business import _Browser, _Chat


class SelectionOptionContract(unittest.TestCase):
    def test_options_require_bounded_unique_application_record_references(self):
        evidence = (RecordEvidence(
            evidence_id="record#1",
            origin=Origin(kind="typed", reference="fixture", observed_at="now",
                          scope="current workspace"),
            record_id="company/1", field_name="name", field_meaning="company name",
            field_was_read=True, value="Acme", value_type="text"),)
        result = BusinessToolResult(
            content={"companies": []}, audit={},
            evidence=evidence,
            selection_options=(BusinessSelectionOption("company/1", "Acme — London"),))
        self.assertEqual(result.selection_options[0].label, "Acme — London")
        with self.assertRaisesRegex(ValueError, "unique record references"):
            BusinessToolResult(
                content={"companies": []}, audit={},
                evidence=evidence,
                selection_options=(
                    BusinessSelectionOption("company/1", "Acme — London"),
                    BusinessSelectionOption("company/1", "Acme — Paris"),
                ))

    def test_an_option_cannot_name_a_record_absent_from_evidence(self):
        with self.assertRaisesRegex(ValueError, "established by evidence"):
            BusinessToolResult(
                content={"companies": []}, audit={},
                selection_options=(BusinessSelectionOption("company/1", "Acme"),))

    def test_a_navigation_target_must_be_an_authorized_route(self):
        result = BusinessToolResult(
            content={"ui_path": "/object/company/1"}, audit={},
            allowed_routes=("/object/company/1",),
            navigation_target="/object/company/1")
        self.assertEqual(result.navigation_target, "/object/company/1")
        with self.assertRaisesRegex(ValueError, "one of the allowed routes"):
            BusinessToolResult(
                content={"ui_path": "/object/company/1"}, audit={},
                allowed_routes=("/object/company/1",),
                navigation_target="/object/company/2")
        with self.assertRaisesRegex(ValueError, "must be a string"):
            BusinessToolResult(content={}, audit={}, navigation_target=None)


class FormDraftContract(unittest.TestCase):
    def test_a_draft_is_bound_to_an_allowed_route_and_exact_non_secret_fields(self):
        draft = FormDraft(
            route="/object/company/new",
            fields=(FormDraftField("name", "Company name", "Acme"),))
        result = BusinessToolResult(
            content={"form_ui_path": "/object/company/new",
                     "fields": [item.to_json() for item in draft.fields]}, audit={},
            allowed_routes=("/object/company/new",), form_draft=draft)
        self.assertEqual(result.form_draft.content_digest, draft.content_digest)

        with self.assertRaisesRegex(ValueError, "allowed route"):
            BusinessToolResult(content={"form_ui_path": "/object/company/new"}, audit={},
                               form_draft=draft)
        with self.assertRaisesRegex(ValueError, "credential-shaped"):
            FormDraftField("password", "Password", "secret")
        with self.assertRaisesRegex(ValueError, "unique fields"):
            FormDraft(route="/object/company/new", fields=(
                FormDraftField("name", "Name", "A"),
                FormDraftField("name", "Name", "B"),
            ))


class FormPrefillProductPath(unittest.TestCase):
    class Executor:
        @staticmethod
        def tool_schemas():
            return ({"type": "function", "function": {
                "name": "prepare_company_form", "description": "Prepare one form.",
                "parameters": {"type": "object", "properties": {},
                               "additionalProperties": False}}},)

        @staticmethod
        def tool_effects():
            return {"prepare_company_form": "read"}

        @staticmethod
        def execute(name, args, *, context=None):
            draft = FormDraft(
                route="/object/company/new",
                fields=(FormDraftField("name", "Company name", "NovaForge"),
                        FormDraftField("email", "Email", "ops@novaforge.example")))
            return BusinessToolResult(
                content={"form_ui_path": draft.route,
                         "fields": [item.to_json() for item in draft.fields]},
                audit={"operation": name, "field_count": 2},
                allowed_routes=(draft.route,), form_draft=draft)

    class Browser(_Browser):
        def execute(self, action, args=None, expect_navigation=False):
            receipt = super().execute(action, args, expect_navigation)
            receipt.observation.elements = [
                ElementRef("e1", "textbox", "Company name", "input",
                           receipt.observation.epoch, {"field": "name"}),
                ElementRef("e2", "textbox", "Email", "input",
                           receipt.observation.epoch, {"field": "email"}),
                ElementRef("e3", "button", "Save", "button",
                           receipt.observation.epoch),
            ]
            return receipt

    def test_the_model_can_request_the_current_draft_but_cannot_supply_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draft-skill.json"
            path.write_text(json.dumps({
                "schema_version": 1, "skill_id": "fixture.companies-draft",
                "title": "Draft company", "intent": "Prefill a company form.",
                "triggers": ["prefill company"],
                "instructions": [
                    "Call prepare_company_form, then call prefill_form with no arguments."],
                "tool_names": ["prepare_company_form"],
                "provenance": ["api:fixture@v1:/companies#draft"],
                "read_only": True,
            }))
            skill = BusinessSkill.load(path)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("prepare_company_form", {}),
                ("prefill_form", {}),
                ("finish", {"done": True, "why": "must not be called"}),
            ])
            browser = self.Browser()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "form-prefill")
            session = ServerAgentSession(
                task=TaskSpec("T", "prefill company, do not submit"),
                chat=chat, browser=browser, control=ReadOnlyControl(), ledger=ledger,
                business_tools=BusinessToolRuntime((skill,), self.Executor()),
                disabled_actions={"click", "fill", "select", "submit"},
                allow_form_prefill=True, step_budget=5, wall_budget_s=30)
            session.entry_observation = browser.observe()
            session.plan()

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.steps_used, 3)
            self.assertEqual(len(chat.messages), 3)
            self.assertNotIn("prefill_form", chat.tool_names[0])
            self.assertIn("prefill_form", chat.tool_names[2])
            self.assertNotIn("fill", chat.tool_names[2])
            self.assertEqual(browser.actions, ["goto", "fill", "fill"])
            self.assertNotIn("submit", browser.actions)
            self.assertIn("has not been submitted", result.agent_said)
            self.assertEqual(len(ledger.of_kind("form_prefill_completed")), 1)


    def test_a_form_that_finished_rendering_mid_fill_is_aimed_at_again(self):
        """P7.5 J2.12: the application settles its own form while we are aiming at it.

        The runtime refused the first `fill` as stale -- correctly: nothing was typed into
        a page that had moved on. The prefill treated that as a refusal and told the person
        the form could not be filled, on a page whose fields were plainly there.
        """
        class Racing(self.Browser):
            """Refuses the first fill the way the runtime does, then behaves."""

            def __init__(self):
                super().__init__()
                self.refused = 0

            def execute(self, action, args=None, expect_navigation=False):
                receipt = super().execute(action, args, expect_navigation)
                if action == "fill" and self.refused == 0:
                    self.refused += 1
                    receipt.ok = False
                    receipt.error_code = "stale_observation"
                return receipt

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draft-skill.json"
            path.write_text(json.dumps({
                "schema_version": 1, "skill_id": "fixture.companies-draft",
                "title": "Draft company", "intent": "Prefill a company form.",
                "triggers": ["prefill company"],
                "instructions": ["Call prepare_company_form, then prefill_form."],
                "tool_names": ["prepare_company_form"],
                "provenance": ["api:fixture@v1:/companies#draft"],
                "read_only": True,
            }))
            skill = BusinessSkill.load(path)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("prepare_company_form", {}),
                ("prefill_form", {}),
                ("finish", {"done": True, "why": "must not be called"}),
            ])
            browser = Racing()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "form-prefill-race")
            session = ServerAgentSession(
                task=TaskSpec("T", "prefill company, do not submit"),
                chat=chat, browser=browser, control=ReadOnlyControl(), ledger=ledger,
                business_tools=BusinessToolRuntime((skill,), self.Executor()),
                disabled_actions={"click", "fill", "select", "submit"},
                allow_form_prefill=True, step_budget=5, wall_budget_s=30)
            session.entry_observation = browser.observe()
            session.plan()

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertIn("has not been submitted", result.agent_said)
            # It looked again rather than giving up, and both fields were filled.
            self.assertEqual(browser.actions,
                             ["goto", "fill", "observe", "fill", "fill"])
            self.assertEqual(len(ledger.of_kind("form_prefill_completed")), 1)
            self.assertNotIn("submit", browser.actions)


class AFormTheApplicationSavesIsConfirmedFirst(unittest.TestCase):
    """Owner ruling (P7.5): prepare it, but ask first, in the card a write uses.

    Odoo commits an open record form when the page is left, so filling one stages a change
    to data that already exists. The person is shown what it would become before anything
    is typed, and a declined confirmation types nothing at all.
    """

    class Executor:
        @staticmethod
        def tool_schemas():
            return ({"type": "function", "function": {
                "name": "prepare_company_edit", "description": "Stage a change.",
                "parameters": {"type": "object", "properties": {}, "required": [],
                               "additionalProperties": False}}},)

        @staticmethod
        def tool_effects():
            return {"prepare_company_edit": "read"}

        @staticmethod
        def execute(name, args, *, context=None):
            draft = FormDraft(
                route="/object/company/7",
                fields=(FormDraftField("phone", "Phone", "+1 512 555 0199"),),
                persistence=APPLICATION_SAVES_ON_LEAVE,
                effect="Fill one new phone on NovaForge for you to save",
                targets=("company · NovaForge · phone: +1 512 555 0142 → +1 512 555 0199",))
            return BusinessToolResult(
                content={"form_ui_path": draft.route,
                         "fields": [item.to_json() for item in draft.fields]},
                audit={"operation": name, "field_count": 1},
                allowed_routes=(draft.route,), form_draft=draft)

    class Browser(_Browser):
        def execute(self, action, args=None, expect_navigation=False):
            receipt = super().execute(action, args, expect_navigation)
            receipt.observation.elements = [
                ElementRef("e1", "textbox", "Phone", "input",
                           receipt.observation.epoch, {"field": "phone"})]
            return receipt

    def _run(self, decision):
        asked = []

        def approval(plan, *, context=None):
            asked.append({"effect": plan.effect, "targets": list(plan.targets)})
            return SimpleNamespace(
                decision=decision, plan_digest=plan.content_digest,
                approval_id="approval-1", operation_context_digest="")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "edit-skill.json"
            path.write_text(json.dumps({
                "schema_version": 1, "skill_id": "fixture.companies-edit",
                "title": "Edit company", "intent": "Prepare a change to one company.",
                "triggers": ["edit company"],
                "instructions": ["Call prepare_company_edit, then prefill_form."],
                "tool_names": ["prepare_company_edit"],
                "provenance": ["api:fixture@v1:/companies#edit"],
                "read_only": False,
            }))
            skill = BusinessSkill.load(path)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("prepare_company_edit", {}),
                ("prefill_form", {}),
                ("finish", {"done": True, "why": "must not be called"}),
            ])
            browser = self.Browser()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "form-edit")
            session = ServerAgentSession(
                task=TaskSpec("T", "change the company phone, do not save"),
                chat=chat, browser=browser, control=ReadOnlyControl(), ledger=ledger,
                business_tools=BusinessToolRuntime((skill,), self.Executor()),
                business_mutation_approval=approval,
                disabled_actions={"click", "fill", "select", "submit"},
                allow_form_prefill=True, step_budget=5, wall_budget_s=30)
            session.entry_observation = browser.observe()
            session.plan()
            return session.run(), browser, ledger, asked

    def test_the_confirmation_names_the_change_and_only_then_is_it_typed(self):
        result, browser, ledger, asked = self._run("approved")

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(len(asked), 1)
        self.assertIn("NovaForge", asked[0]["targets"][0])
        self.assertIn("+1 512 555 0142 → +1 512 555 0199", asked[0]["targets"][0])
        self.assertEqual(browser.actions, ["goto", "fill"])
        self.assertIn("saved", result.agent_said.lower())
        self.assertEqual(len(ledger.of_kind("form_handoff_approved")), 1)

    def test_a_declined_confirmation_types_nothing(self):
        result, browser, ledger, asked = self._run("denied")

        self.assertEqual(result.terminal_reason, "approval_denied")
        self.assertEqual(len(asked), 1)
        self.assertNotIn("fill", browser.actions)
        self.assertEqual(len(ledger.of_kind("form_handoff_denied")), 1)

    def test_a_confirmation_that_expired_types_nothing(self):
        result, browser, _ledger, _asked = self._run("expired")

        self.assertEqual(result.terminal_reason, "approval_expired")
        self.assertNotIn("fill", browser.actions)
