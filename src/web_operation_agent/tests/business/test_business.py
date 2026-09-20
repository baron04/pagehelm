"""Business Skill loading and read-only runtime-tool transitions."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.business.runtime import (
    APPLICATION_SAVES_ON_LEAVE,
    BusinessMutationBlockedError, BusinessMutationIndeterminate,
    BusinessMutationRefusedError, BusinessMutationSpec,
    BusinessSelectionOption,
    BusinessSkill, Citation, FormDraft, FormDraftField,
    BusinessToolContractError, BusinessToolDispatchError, BusinessToolError,
    BusinessToolIdentityError,
    BusinessToolPermissionError, BusinessToolRequestError, BusinessToolResult,
    BusinessToolRuntime, BusinessToolUnavailableError,
)
from web_operation_agent.composition.ports import ResultPostconditionVerifier
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.answer.evidence import Origin, QueryEvidence, RecordEvidence
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.business.mutation import MemoryMutationJournal, MutationJournalError
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.execution.session import (CAPABILITY_GAP_TERMINAL,
                                        ServerAgentSession, TaskSpec)
from web_operation_agent.tests.chat_port_contract import CancellableChatDouble


SCHEMA = {"type": "function", "function": {
    "name": "query_companies", "description": "Read companies.",
    "parameters": {"type": "object", "properties": {},
                   "additionalProperties": False}}}
WRITE_SCHEMA = {"type": "function", "function": {
    "name": "create_company", "description": "Create one company.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string"}}, "required": ["name"],
        "additionalProperties": False}}}


class _Executor:
    @staticmethod
    def tool_schemas():
        return (SCHEMA,)

    @staticmethod
    def tool_effects():
        return {"query_companies": "read"}

    @staticmethod
    def execute(name, args, *, context=None):
        if name != "query_companies" or args:
            raise BusinessToolError("unexpected call")
        return BusinessToolResult(
            content={"companies": [{"name": "Acme", "domain": "acme.test",
                                     "ui_path": "/object/company/company-1"}],
                     "matched_count": 1, "source_total": 1,
                     "scope": "current_delegated_workspace",
                     "observed_at": "2026-09-08T05:00:00+00:00",
                     "completeness": {"source_total_known": True,
                                      "source_complete": True,
                                      "result_truncated": False}},
            audit={"application": "fixture", "operation": name,
                   "oauth_actor": "delegated_user", "returned_count": 1,
                   "scope": "current_delegated_workspace",
                   "source_complete": True},
            allowed_routes=("/object/company/company-1",))


class _ProjectedExecutor(_Executor):
    @staticmethod
    def execute(name, args, *, context=None):
        result = _Executor.execute(name, args, context=context)
        return BusinessToolResult(
            content=result.content, audit=result.audit,
            allowed_routes=result.allowed_routes,
            query_answer=StructuredQueryAnswer(
                collection="companies", fields=("name", "domain"),
                records=(("Acme", "acme.test"),), source="fixture API",
                scope="current_delegated_workspace",
                observed_at="2026-09-08T05:00:00+00:00",
                source_complete=True, result_truncated=False,
                matched_count=1, source_total=1, records_scanned=1))


def _skill(path: Path) -> BusinessSkill:
    path.write_text(json.dumps({
        "schema_version": 1,
        "skill_id": "fixture.companies-query",
        "title": "Query companies",
        "intent": "Read current companies.",
        "triggers": ["companies", "公司"],
        "instructions": ["Call query_companies and trust its current result."],
        "tool_names": ["query_companies"],
        "provenance": ["src:fixture@v1:companies.json#companies"],
        "read_only": True,
    }) + "\n")
    return BusinessSkill.load(path)


def _write_skill(path: Path) -> BusinessSkill:
    path.write_text(json.dumps({
        "schema_version": 1,
        "skill_id": "fixture.companies-create",
        "title": "Create company",
        "intent": "Create one current company.",
        "triggers": ["create company", "创建公司"],
        "instructions": ["Propose one company and wait for approval."],
        "tool_names": ["create_company"],
        "provenance": ["src:fixture@v1:companies.json#create"],
        "read_only": False,
    }) + "\n")
    return BusinessSkill.load(path)


class _MutationExecutor:
    def __init__(self, *, indeterminate=False):
        self.executions = 0
        self.indeterminate = indeterminate

    @staticmethod
    def tool_schemas():
        return (WRITE_SCHEMA,)

    @staticmethod
    def tool_effects():
        return {"create_company": "write"}

    @staticmethod
    def execute(_name, _args, *, context=None):
        raise AssertionError("write tools must not use the read dispatch path")

    @staticmethod
    def prepare_mutation(name, args, *, context=None):
        if name != "create_company" or set(args) != {"name"}:
            raise BusinessToolError("invalid create request")
        return BusinessMutationSpec(
            args={"name": str(args["name"]).strip()},
            effect="Create one company", targets=(str(args["name"]).strip(),))

    def execute_mutation(self, name, args, *, context=None):
        self.executions += 1
        self.context = context
        if self.indeterminate:
            raise BusinessMutationIndeterminate("fixture acknowledgement was lost")
        return BusinessToolResult(
            content={"created": True, "name": args["name"],
                     "ui_path": "/object/company/company-2"},
            audit={"operation": name, "records_created": 1},
            allowed_routes=("/object/company/company-2",))


def _operation_context(*, scopes=("companies:read", "companies:write"),
                       operation_id="op-1", application_id="fixture"):
    now = int(time.time() * 1000)
    principal = PrincipalContext(
        1, application_id, "tenant-1", "workspace-1", "actor-1", "delegation-1",
        now - 1_000, now + 60_000, tuple(scopes))
    return OperationContext.create(
        principal, session_id="session-1", turn_id="message-1",
        operation_id=operation_id, deadline_ms=now + 30_000, now_ms=now)


def _observation(epoch: int, url: str = "http://app.test/companies") -> PageObservation:
    return PageObservation(
        schema_version=1, epoch=epoch, url=url,
        title="Companies", landmarks=["Companies"], elements=[],
        content="Companies page")


class _Browser:
    def __init__(self):
        self.epoch = 1
        self.actions = []
        self.url = "http://app.test/companies"

    def observe(self):
        return _observation(self.epoch, self.url)

    def execute(self, action, args=None, expect_navigation=False):
        self.actions.append(action)
        before = self.url
        if action == "goto":
            self.url = "http://app.test" + str((args or {}).get("url") or "")
        self.epoch += 1
        return ActionReceipt(
            schema_version=1, command_id=f"c{len(self.actions)}", action=action,
            args_echo=dict(args or {}), ok=True,
            url_before=before,
            url_after=self.url, navigated=self.url != before,
            epoch_before=self.epoch - 1, epoch_after=self.epoch,
            observation=_observation(self.epoch, self.url))

    @staticmethod
    def console():
        return []


class _Chat(CancellableChatDouble):
    def __init__(self, script):
        self.script = list(script)
        self.tool_names = []
        self.messages = []

        class _Meter:
            @staticmethod
            def report():
                return {"provider_calls": 0, "cost_cny": 0.0}

        self.meter = _Meter()

    def complete(self, messages, tools, _run_id, **_kwargs):
        self.messages.append([dict(message) for message in messages])
        self.tool_names.append({tool["function"]["name"] for tool in tools})
        name, args = self.script.pop(0)
        if name is None:
            return {"content": args["content"]}
        return {"tool_calls": [{"id": str(len(self.messages)), "function": {
            "name": name, "arguments": json.dumps(args)}}]}


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


class SkillCitations(unittest.TestCase):
    """`provenance` is what makes a hand-written playbook checkable, so it has a grammar.

    Before this it was validated as "a non-empty list of strings", and four spellings had
    accumulated across nine shipped artifacts. Nothing could resolve any of them, so a
    citation that had rotted looked exactly like one that never pointed anywhere.
    """

    def _artifact(self, provenance, path: Path) -> Path:
        path.write_text(json.dumps({
            "schema_version": 1, "skill_id": "fixture.companies-query",
            "title": "Query companies", "intent": "Read current companies.",
            "triggers": ["companies"], "instructions": ["Call query_companies."],
            "tool_names": ["query_companies"], "provenance": provenance,
            "read_only": True,
        }) + "\n")
        return path

    def test_a_citation_round_trips_through_its_parts(self):
        for text in ("src:odoo@18.0:addons/sale/views/sale_order_views.xml#action_orders",
                     "src:twenty@v2.39.0:packages/twenty-server/src/x.ts",
                     "api:odoo@18.0:/jsonrpc#object.execute_kw",
                     "api:twenty@v2.39.0:/rest/open-api/core#/paths/~1companies/post"):
            with self.subTest(text=text):
                self.assertEqual(Citation.parse(text).text, text)

    def test_every_spelling_this_repository_used_to_ship_is_now_refused(self):
        """The point of the grammar is that the old entries do not survive it."""
        for legacy in ("odoo:18.0:addons/sale/views/sale_order_views.xml#action_orders",
                       "twenty/v2.39.0:packages/twenty-docs/x.mdx",
                       "minimal-demo://contacts/v1",
                       "Odoo external API: /jsonrpc object.execute_kw",
                       "Twenty self-hosted /rest/companies API"):
            with self.subTest(legacy=legacy):
                with self.assertRaises(ValueError):
                    Citation.parse(legacy)

    def test_a_source_citation_cannot_escape_the_repository(self):
        for escape in ("src:odoo@18.0:/etc/passwd",
                       "src:odoo@18.0:addons/../../etc/passwd"):
            with self.subTest(escape=escape):
                with self.assertRaisesRegex(ValueError, "relative path inside"):
                    Citation.parse(escape)

    def test_an_api_citation_must_name_a_path_on_the_origin(self):
        with self.assertRaisesRegex(ValueError, "path on the application's origin"):
            Citation.parse("api:odoo@18.0:jsonrpc")

    def test_a_skill_whose_provenance_does_not_parse_does_not_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._artifact(["fixture:companies"], Path(tmp) / "skill.json")
            with self.assertRaisesRegex(ValueError, "provenance entry must read"):
                BusinessSkill.load(path)

    def test_a_repeated_citation_does_not_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._artifact(["src:fixture@v1:a.json#x", "src:fixture@v1:a.json#x"],
                                  Path(tmp) / "skill.json")
            with self.assertRaisesRegex(ValueError, "unique"):
                BusinessSkill.load(path)

    def test_every_shipped_skill_artifact_parses(self):
        """The nine artifacts in `examples/` are the thing this grammar had to fit.

        Read as files, never imported: the core suite must still run with no application
        distribution installed, which is the boundary `check_core_boundary.py` guards. From
        an installed wheel there are no examples to read and this has nothing to say.
        """
        root = Path(__file__).resolve().parents[4] / "examples"
        if not root.is_dir():
            self.skipTest("running outside the checkout; no example artifacts to read")
        artifacts = [path for path in root.rglob("skills/*.json")
                     if "/build/" not in str(path)]
        self.assertGreaterEqual(len(artifacts), 9, "the example Skills moved")
        for path in artifacts:
            with self.subTest(path=path.name):
                skill = BusinessSkill.load(path)
                kinds = {Citation.parse(entry).kind for entry in skill.provenance}
                self.assertTrue(kinds <= {"src", "api"})


class BusinessRuntimeValidation(unittest.TestCase):
    def test_complete_ambiguous_navigation_query_becomes_a_server_choice(self):
        class AmbiguousExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                origin = Origin(kind="typed", reference="fixture", observed_at="now",
                                scope="current workspace")
                return BusinessToolResult(
                    content={"companies": [
                        {"name": "Acme", "domain": "acme.test"},
                        {"name": "Azure", "domain": "azure.test"},
                    ]},
                    audit={"application": "fixture", "operation": name,
                           "source_complete": True},
                    query_answer=StructuredQueryAnswer(
                        collection="companies", fields=("name", "domain"),
                        records=(("Acme", "acme.test"), ("Azure", "azure.test")),
                        source="fixture API", scope="current workspace",
                        observed_at="now", source_complete=True,
                        result_truncated=False, matched_count=2,
                        source_total=2, records_scanned=2),
                    evidence=(
                        QueryEvidence(
                            evidence_id="query#1", origin=origin, collection="companies",
                            conditions=("name contains A",), matched_count=2,
                            records_scanned=2, covers_whole_scope=True,
                            looked_for="name contains A"),
                        RecordEvidence(
                            evidence_id="record#1", origin=origin, record_id="company/1",
                            field_name="name", field_meaning="company name",
                            field_was_read=True, value="Acme", value_type="text"),
                        RecordEvidence(
                            evidence_id="record#2", origin=origin, record_id="company/2",
                            field_name="name", field_meaning="company name",
                            field_was_read=True, value="Azure", value_type="text"),
                    ),
                    selection_options=(
                        BusinessSelectionOption("company/1", "Acme — acme.test"),
                        BusinessSelectionOption("company/2", "Azure — azure.test"),
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query.json")
            runtime = BusinessToolRuntime(
                (skill,), AmbiguousExecutor(),
                route_skill_hints=((r"^/companies$", skill.skill_id),))
            chat = _Chat([("query_companies", {})])
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="打开 A", entry_path="/companies",
                              supported_scope="Read and open current companies."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(root / "session.jsonl", "ambiguous-navigation"),
                business_tools=runtime, deterministic_read_navigation=True,
                allow_clarification=True,
            )
            session.entry_observation = _observation(1)
            session.plan()

            result = session.run()

            self.assertEqual(result.terminal_reason, "needs_input")
            self.assertEqual(result.agent_said, "找到多个匹配项，请选择要打开的记录：")
            self.assertEqual(
                [item["label"] for item in result.clarification["choices"]],
                ["Acme — acme.test", "Azure — azure.test"])
            self.assertEqual(len(chat.messages), 1, "the server must not ask the model to copy choices")
            event = session.ledger.of_kind("clarification_requested")[-1]
            self.assertEqual(event["deterministic"], "complete_navigation_query")

    def test_complete_navigation_query_beyond_the_choice_limit_goes_back_to_the_model(self):
        """A complete read with more matches than one question can offer must not end the turn.

        The server choice is only a shortcut for a set it can show whole. Nine matches cannot
        be one ask_user question, so the model decides how to narrow the request.
        """
        names = [f"A{index}" for index in range(1, 10)]

        class ManyExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                origin = Origin(kind="typed", reference="fixture", observed_at="now",
                                scope="current workspace")
                return BusinessToolResult(
                    content={"companies": [{"name": item} for item in names]},
                    audit={"application": "fixture", "operation": name,
                           "source_complete": True},
                    query_answer=StructuredQueryAnswer(
                        collection="companies", fields=("name",),
                        records=tuple((item,) for item in names),
                        source="fixture API", scope="current workspace",
                        observed_at="now", source_complete=True,
                        result_truncated=False, matched_count=len(names),
                        source_total=len(names), records_scanned=len(names)),
                    evidence=(
                        QueryEvidence(
                            evidence_id="query#1", origin=origin, collection="companies",
                            conditions=("name contains A",), matched_count=len(names),
                            records_scanned=len(names), covers_whole_scope=True,
                            looked_for="name contains A"),
                        *(RecordEvidence(
                            evidence_id=f"record#{index}", origin=origin,
                            record_id=f"company/{index}", field_name="name",
                            field_meaning="company name", field_was_read=True,
                            value=item, value_type="text")
                          for index, item in enumerate(names, 1)),
                    ),
                    selection_options=tuple(
                        BusinessSelectionOption(f"company/{index}", item)
                        for index, item in enumerate(names, 1)),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query.json")
            runtime = BusinessToolRuntime(
                (skill,), ManyExecutor(),
                route_skill_hints=((r"^/companies$", skill.skill_id),))
            chat = _Chat([
                ("query_companies", {}),
                ("ask_user", {"question": "请说明公司名称。", "response_kind": "free_text",
                              "choice_ids": []}),
            ])
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="打开 A", entry_path="/companies",
                              supported_scope="Read and open current companies."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(root / "session.jsonl", "many-navigation"),
                business_tools=runtime, deterministic_read_navigation=True,
                allow_clarification=True,
            )
            session.entry_observation = _observation(1)
            session.plan()

            result = session.run()

            self.assertEqual(result.terminal_reason, "needs_input")
            self.assertEqual(result.clarification["response_kind"], "free_text")
            self.assertEqual(len(chat.messages), 2, "the model must see the complete read")
            self.assertEqual(
                session.ledger.of_kind("clarification_candidates_ready")[-1]["count"],
                len(names))
            self.assertFalse(session.ledger.of_kind("turn_execution_failed"))
            self.assertNotIn(
                "deterministic", session.ledger.of_kind("clarification_requested")[-1])

    def test_a_complete_read_that_looked_for_nothing_is_not_a_choice_question(self):
        """Several records are an ambiguity only when the person's own words matched them.

        "打开金额最高的销售订单" names a rule, not a record. The read it drives carries no
        name, so every order matches, and the server answered with a list of all five and
        asked which one to open -- the choice the person had already made. A read that
        looked for nothing goes back to the model, which can compute the one it names.
        """
        rows = (("Acme", "1000.00"), ("Azure", "287.50"))

        class WholeCollectionExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                origin = Origin(kind="typed", reference="fixture", observed_at="now",
                                scope="current workspace")
                return BusinessToolResult(
                    content={"companies": [{"name": item, "total": total}
                                           for item, total in rows]},
                    audit={"application": "fixture", "operation": name,
                           "source_complete": True},
                    query_answer=StructuredQueryAnswer(
                        collection="companies", fields=("name", "total"),
                        records=rows, source="fixture API", scope="current workspace",
                        observed_at="now", source_complete=True, result_truncated=False,
                        matched_count=len(rows), source_total=len(rows),
                        records_scanned=len(rows)),
                    evidence=(
                        QueryEvidence(
                            evidence_id="query#1", origin=origin, collection="companies",
                            conditions=("no filter",), matched_count=len(rows),
                            records_scanned=len(rows), covers_whole_scope=True),
                        *(RecordEvidence(
                            evidence_id=f"record#{index}", origin=origin,
                            record_id=f"company/{index}", field_name="name",
                            field_meaning="company name", field_was_read=True,
                            value=item, value_type="text")
                          for index, (item, _total) in enumerate(rows, 1)),
                    ),
                    selection_options=tuple(
                        BusinessSelectionOption(f"company/{index}", f"{item} — {total}")
                        for index, (item, total) in enumerate(rows, 1)),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query.json")
            runtime = BusinessToolRuntime(
                (skill,), WholeCollectionExecutor(),
                route_skill_hints=((r"^/companies$", skill.skill_id),))
            chat = _Chat([
                ("query_companies", {}),
                ("ask_user", {"question": "按哪个口径算最高？", "response_kind": "free_text",
                              "choice_ids": []}),
            ])
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="打开金额最高的公司",
                              entry_path="/companies",
                              supported_scope="Read and open current companies."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(root / "session.jsonl", "unnamed-navigation"),
                business_tools=runtime, deterministic_read_navigation=True,
                allow_clarification=True,
            )
            session.entry_observation = _observation(1)
            session.plan()

            result = session.run()

            self.assertEqual(result.terminal_reason, "needs_input")
            self.assertEqual(len(chat.messages), 2, "the model must see the complete read")
            self.assertNotIn(
                "deterministic", session.ledger.of_kind("clarification_requested")[-1])
            self.assertNotIn("请选择要打开的记录", result.agent_said)

    def test_server_policy_text_cannot_manufacture_a_skill_match(self):
        """Route the person's words; keep the server's policy out of intent matching.

        This is the exact shape used by the Odoo demo.  Its form-draft Skill has a
        ``do not submit`` trigger while the product scope says ``Do not submit page
        forms``.  Before this regression, that server-authored sentence won over the
        trusted customer-page hint and every entity-only open request loaded the draft
        tool instead of the customer query.
        """
        class CatalogExecutor:
            @staticmethod
            def tool_schemas():
                return (SCHEMA, {"type": "function", "function": {
                    "name": "prepare_company_form", "description": "Prepare a form.",
                    "parameters": {"type": "object", "properties": {},
                                   "additionalProperties": False}}})

            @staticmethod
            def tool_effects():
                return {"query_companies": "read", "prepare_company_form": "read"}

            @staticmethod
            def execute(*_args, **_kwargs):
                raise AssertionError("routing the turn must not dispatch a tool")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = _skill(root / "query.json")
            draft_path = root / "draft.json"
            draft_path.write_text(json.dumps({
                "schema_version": 1,
                "skill_id": "fixture.companies-draft",
                "title": "Prepare company form",
                "intent": "Prepare one form without submitting.",
                "triggers": ["do not submit", "不要提交"],
                "instructions": ["Prepare the form and leave submission to the user."],
                "tool_names": ["prepare_company_form"],
                "provenance": ["src:fixture@v1:companies.json#draft"],
                "read_only": True,
            }) + "\n")
            draft = BusinessSkill.load(draft_path)
            runtime = BusinessToolRuntime(
                (query, draft), CatalogExecutor(),
                route_skill_hints=((r"^/companies$", query.skill_id),))
            ledger = SessionLedger(root / "session.jsonl", "policy-routing")

            session = ServerAgentSession(
                task=TaskSpec(
                    task_ref="T", prompt="打开 azur", entry_path="/companies",
                    supported_scope=("Read current records. Do not submit page forms. "
                                     "Open only an exact API route.")),
                chat=_Chat([]), browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, business_tools=runtime,
                business_mutation_approval=lambda *_args, **_kwargs: None,
                deterministic_read_navigation=True,
            )

            self.assertEqual(session._task_business_skill_ids, {query.skill_id})
            self.assertEqual(session._auto_loaded_business_skill_ids, {query.skill_id})
            event = ledger.of_kind("business_skill_candidates")[-1]
            self.assertTrue(event["route_context_used"])
            self.assertEqual(event["trigger_matches"], [])
            self.assertEqual(event["task_digest"], hashlib.sha256(
                "打开 azur".encode("utf-8")).hexdigest())

    def test_a_clarification_reply_inherits_only_the_unresolved_user_goal(self):
        class CatalogExecutor:
            @staticmethod
            def tool_schemas():
                return (SCHEMA, WRITE_SCHEMA)

            @staticmethod
            def tool_effects():
                return {"query_companies": "read", "create_company": "write"}

            @staticmethod
            def execute(*_args, **_kwargs):
                raise AssertionError("routing the turn must not dispatch a tool")

        context = {
            "schema_version": 1,
            "prior_turns": [{
                "message_id": "message-1", "user": "打开 A",
                "assistant": "请给出完整名称。", "status": "needs_input",
            }],
            "pending_clarification": {
                "schema_version": 1, "clarification_id": "clarify-1",
                "task_id": "task-1", "question": "请给出完整名称。",
                "response_kind": "free_text", "choices": [],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = _skill(root / "query.json")
            create_path = root / "create.json"
            create_path.write_text(json.dumps({
                "schema_version": 1,
                "skill_id": "fixture.companies-create",
                "title": "Create company",
                "intent": "Create one company.",
                "triggers": ["create company", "创建公司"],
                "instructions": ["Query duplicates, then propose one company."],
                "tool_names": ["query_companies", "create_company"],
                "provenance": ["src:fixture@v1:companies.json#create"],
                "read_only": False,
            }) + "\n")
            create = BusinessSkill.load(create_path)
            runtime = BusinessToolRuntime(
                (query, create), CatalogExecutor(),
                route_skill_hints=((r"^/companies$", query.skill_id),))

            continued = ServerAgentSession(
                task=TaskSpec(task_ref="continued", prompt="Azure Interior",
                              entry_path="/companies", supported_scope="read and open"),
                chat=_Chat([]), browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(root / "continued.jsonl", "continued"),
                business_tools=runtime,
                business_mutation_approval=lambda *_args, **_kwargs: None,
                deterministic_read_navigation=True,
                conversation_context=context,
            )
            self.assertEqual(continued._task_business_skill_ids, {query.skill_id})
            self.assertEqual(continued._auto_loaded_business_skill_ids, {query.skill_id})
            self.assertTrue(continued._navigation_only)
            routed = continued.ledger.of_kind("business_skill_candidates")[-1]
            self.assertTrue(routed["clarification_context_used"])
            self.assertTrue(routed["route_context_used"])

            replaced = ServerAgentSession(
                task=TaskSpec(task_ref="replaced", prompt="创建公司 Zephyr",
                              entry_path="/companies", supported_scope="create one"),
                chat=_Chat([]), browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(root / "replaced.jsonl", "replaced"),
                business_tools=runtime,
                business_mutation_approval=lambda *_args, **_kwargs: None,
                deterministic_read_navigation=True,
                conversation_context=context,
            )
            self.assertEqual(replaced._task_business_skill_ids, {create.skill_id})
            self.assertFalse(replaced._navigation_only)
            rerouted = replaced.ledger.of_kind("business_skill_candidates")[-1]
            self.assertFalse(rerouted.get("clarification_context_used", False))

    def test_read_failures_have_stable_machine_classification(self):
        class FailingExecutor(_Executor):
            @staticmethod
            def execute(*_args, **_kwargs):
                raise OSError("private transport detail")

        class InvalidResultExecutor(_Executor):
            @staticmethod
            def execute(*_args, **_kwargs):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "query-skill.json")
            loaded = {skill.skill_id}
            runtime = BusinessToolRuntime((skill,), _Executor(), application_id="fixture")
            with self.assertRaises(BusinessToolRequestError) as request:
                runtime.execute("query_companies", {}, set())
            self.assertEqual(request.exception.code, "business_request")
            with self.assertRaises(BusinessToolIdentityError) as identity:
                runtime.execute(
                    "query_companies", {}, loaded,
                    context=_operation_context(application_id="another-app"))
            self.assertEqual(identity.exception.code, "business_identity")
            unavailable_runtime = BusinessToolRuntime((skill,), FailingExecutor())
            with self.assertRaises(BusinessToolUnavailableError) as unavailable:
                unavailable_runtime.execute("query_companies", {}, loaded)
            self.assertEqual(unavailable.exception.code, "business_unavailable")
            contract_runtime = BusinessToolRuntime((skill,), InvalidResultExecutor())
            with self.assertRaises(BusinessToolContractError) as contract:
                contract_runtime.execute("query_companies", {}, loaded)
            self.assertEqual(contract.exception.code, "business_contract")

    def test_bound_tool_requires_application_and_declared_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "query-skill.json")
            runtime = BusinessToolRuntime(
                (skill,), _Executor(), application_id="fixture",
                required_scopes={"query_companies": "companies:read"})
            loaded = {skill.skill_id}
            result = runtime.execute(
                "query_companies", {}, loaded,
                context=_operation_context(scopes=("companies:read",)))
            self.assertEqual(result.content["matched_count"], 1)
            with self.assertRaises(BusinessToolPermissionError) as refused:
                runtime.execute(
                    "query_companies", {}, loaded,
                    context=_operation_context(scopes=("companies:write",)))
            self.assertEqual(refused.exception.required_scope, "companies:read")
            with self.assertRaisesRegex(BusinessToolError, "another application"):
                runtime.execute(
                    "query_companies", {}, loaded,
                    context=_operation_context(application_id="another-app"))

    def test_mutation_proposal_cannot_cross_operation_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime(
                (skill,), executor, application_id="fixture",
                required_scopes={"create_company": "companies:write"})
            loaded = {skill.skill_id}
            first = _operation_context(operation_id="op-1")
            second = _operation_context(operation_id="op-2")
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded, context=first)
            self.assertEqual(
                proposal.operation_context_digest, first.binding_digest)
            with self.assertRaisesRegex(BusinessToolError, "another operation context"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, loaded, context=second)
            self.assertEqual(executor.executions, 0)

    def test_skill_can_load_from_bytes_with_a_logical_artifact_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            from_path = _skill(path)
            raw = path.read_bytes()
            from_bytes = BusinessSkill.from_bytes(
                raw, artifact_uri="memory://fixture/companies-query.json")

            self.assertEqual(from_bytes.loaded_card(), from_path.loaded_card())
            self.assertEqual(from_bytes.artifact_sha256, from_path.artifact_sha256)
            self.assertEqual(
                from_bytes.artifact_uri, "memory://fixture/companies-query.json")
            self.assertEqual(from_bytes.artifact_path, from_bytes.artifact_uri)
            with self.assertRaisesRegex(TypeError, "must be bytes"):
                BusinessSkill.from_bytes(  # type: ignore[arg-type]
                    raw.decode(), artifact_uri="memory://fixture/skill.json")
            with self.assertRaisesRegex(ValueError, "artifact_uri"):
                BusinessSkill.from_bytes(raw, artifact_uri="")

    def test_tool_is_unavailable_until_relevant_skill_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            relevant = runtime.relevant("Which companies are in this workspace?")
            self.assertEqual([item.skill_id for item in relevant], [skill.skill_id])
            self.assertEqual(runtime.schemas_for(set()), [])
            with self.assertRaisesRegex(BusinessToolError, "load the matching"):
                runtime.execute("query_companies", {}, set())
            loaded, error = runtime.load(skill.skill_id, {skill.skill_id})
            self.assertEqual(error, "")
            self.assertEqual(loaded, skill)
            self.assertEqual(
                [item["function"]["name"] for item in runtime.schemas_for({skill.skill_id})],
                ["query_companies"])

    def test_empty_trigger_match_uses_a_bounded_read_only_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            selection = runtime.select(
                "Show the current workspace overview", allow_mutations=False)

            self.assertTrue(selection.fallback_used)
            self.assertEqual(selection.trigger_matches, ())
            self.assertEqual(selection.candidate_ids, (skill.skill_id,))

    def test_skill_instruction_cannot_reference_an_unavailable_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "skill_id": "fixture.invalid-tool",
                "title": "Invalid tool reference",
                "intent": "Prove instruction and callable surfaces stay aligned.",
                "triggers": ["invalid"],
                "instructions": ["Call delete_everything before answering."],
                "tool_names": ["query_companies"],
                "provenance": ["src:fixture@v1:invalid.json#invalid"],
                "read_only": True,
            }))
            with self.assertRaisesRegex(ValueError, "unavailable.*delete_everything"):
                BusinessToolRuntime((BusinessSkill.load(path),), _Executor())

    def test_result_rejects_credential_shaped_fields(self):
        with self.assertRaisesRegex(ValueError, "credential-shaped"):
            BusinessToolResult(content={"access_token": "leak"}, audit={})

    def test_result_routes_are_safe_and_must_be_visible_to_the_model(self):
        with self.assertRaisesRegex(ValueError, "safe relative"):
            BusinessToolResult(
                content={"ui_path": "https://other.test/record/1"}, audit={},
                allowed_routes=("https://other.test/record/1",))
        with self.assertRaisesRegex(ValueError, "safe relative"):
            BusinessToolResult(
                content={"ui_path": "/\\other.test/record/1"}, audit={},
                allowed_routes=("/\\other.test/record/1",))
        with self.assertRaisesRegex(ValueError, "must appear"):
            BusinessToolResult(
                content={"companies": []}, audit={},
                allowed_routes=("/object/company/company-1",))

    def test_skill_and_executor_must_declare_the_same_tools(self):
        class EmptyExecutor:
            @staticmethod
            def tool_schemas():
                return ()

            @staticmethod
            def tool_effects():
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "same tools"):
                BusinessToolRuntime((_skill(Path(tmp) / "skill.json"),), EmptyExecutor())

    def test_custom_mutation_journal_must_declare_restart_behavior(self):
        class UnclassifiedJournal:
            def register(self, *_args, **_kwargs):
                pass

            def claim(self, *_args, **_kwargs):
                pass

            def finish(self, *_args, **_kwargs):
                pass

            def get(self, *_args, **_kwargs):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(TypeError, "MutationJournalDurability"):
                BusinessToolRuntime(
                    (_skill(Path(tmp) / "skill.json"),), _Executor(),
                    mutation_journal=UnclassifiedJournal())

    def test_read_only_skill_cannot_expose_a_write_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            data = json.loads(Path(_skill(path).artifact_path).read_text())
            data["tool_names"] = ["create_company"]
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "read-only"):
                BusinessToolRuntime((BusinessSkill.load(path),), _MutationExecutor())

    def test_separate_runtimes_share_normalized_business_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            journal = MemoryMutationJournal()
            executors = [_MutationExecutor(), _MutationExecutor()]
            runtimes = [BusinessToolRuntime((skill,), executor, mutation_journal=journal)
                        for executor in executors]
            proposals = [runtime.prepare_mutation(
                "create_company", {"name": name}, {skill.skill_id})
                for runtime, name in zip(runtimes, (" Acme ", "Acme"))]
            self.assertNotEqual(proposals[0].content_digest, proposals[1].content_digest)
            journal.claim(proposals[0].content_digest, "")
            with self.assertRaisesRegex(BusinessMutationBlockedError, "business key") as blocked:
                runtimes[1].execute_mutation(
                    proposals[1], proposals[1].content_digest, {skill.skill_id})
            self.assertEqual(blocked.exception.reason, "business_key_unresolved")
            self.assertEqual(executors[1].executions, 0)

    def test_mutation_requires_exact_approval_and_is_single_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            loaded = {skill.skill_id}
            with self.assertRaisesRegex(BusinessToolError, "explicit approval"):
                runtime.execute("create_company", {"name": " Acme "}, loaded)
            proposal = runtime.prepare_mutation(
                "create_company", {"name": " Acme "}, loaded)
            self.assertEqual(proposal.args, {"name": "Acme"})
            with self.assertRaisesRegex(BusinessToolError, "does not match"):
                runtime.execute_mutation(proposal, "0" * 64, loaded)
            self.assertEqual(executor.executions, 0)
            result = runtime.execute_mutation(
                proposal, proposal.content_digest, loaded)
            self.assertTrue(result.content["created"])
            self.assertEqual(executor.executions, 1)
            self.assertEqual(
                journal.get(proposal.content_digest).state, "completed")
            self.assertEqual(
                journal.get(proposal.content_digest).result_digest,
                result.content_digest)
            with self.assertRaisesRegex(BusinessMutationBlockedError, "already completed") as reused:
                runtime.execute_mutation(proposal, proposal.content_digest, loaded)
            self.assertEqual(reused.exception.reason, "already_consumed")
            self.assertEqual(executor.executions, 1)

    def test_indeterminate_mutation_is_consumed_and_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor(indeterminate=True)
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, {skill.skill_id})
            with self.assertRaises(BusinessMutationIndeterminate):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, {skill.skill_id})
            self.assertEqual(
                journal.get(proposal.content_digest).state, "indeterminate")
            with self.assertRaisesRegex(BusinessToolError, "already indeterminate"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, {skill.skill_id})
            self.assertEqual(executor.executions, 1)

    def test_unverified_adapter_postcondition_keeps_acknowledged_write_indeterminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal,
                postcondition_verifier=ResultPostconditionVerifier((
                    ("create_company", ("content.readback_verified",)),
                )))
            loaded = {skill.skill_id}
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded)

            with self.assertRaisesRegex(
                    BusinessMutationIndeterminate, "not verified"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, loaded)

            self.assertEqual(executor.executions, 1)
            self.assertEqual(
                journal.get(proposal.content_digest).state, "indeterminate")

    def test_missing_durable_completion_turns_success_into_indeterminate(self):
        class BrokenFinishJournal(MemoryMutationJournal):
            def finish(self, proposal_digest, state, *, result_digest="", now_ms=None):
                if state == "completed":
                    raise MutationJournalError("disk unavailable")
                return super().finish(
                    proposal_digest, state, result_digest=result_digest,
                    now_ms=now_ms)

        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            journal = BrokenFinishJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, {skill.skill_id})
            with self.assertRaisesRegex(
                    BusinessMutationIndeterminate, "durable outcome"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, {skill.skill_id})
            self.assertEqual(executor.executions, 1)
            self.assertEqual(
                journal.get(proposal.content_digest).state, "dispatching")


class BusinessReActTransition(unittest.TestCase):
    def test_a_capability_question_cannot_turn_into_a_business_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            chat = _Chat([("finish", {"done": True,
                                      "why": "我可以查询当前公司。"})])
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="你可以做什么？",
                              supported_scope="Read current records."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(Path(tmp) / "session.jsonl", "capabilities"),
                business_tools=runtime, step_budget=2, wall_budget_s=30)
            session.entry_observation = _observation(1)
            session.plan()

            result = session.run()

            self.assertEqual(result.agent_said, "我可以查询当前公司。")
            self.assertEqual(chat.tool_names, [{"finish"}])
            self.assertIn("Query companies", chat.messages[0][1]["content"])

    def test_assistant_history_does_not_change_the_user_text_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "assistant-history")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open the second one.",
                              supported_scope="Read the current page only."),
                chat=_Chat([]), browser=_Browser(),
                control=ReadOnlyControl(),
                ledger=ledger,
                business_tools=runtime,
                conversation_context={
                    "schema_version": 1,
                    "prior_turns": [{
                        "message_id": "m1",
                        "user": "Show the available records.",
                        "assistant": "The companies include Acme and Example.",
                        "status": "finished",
                    }],
                },
            )

            self.assertEqual(session._task_business_skill_ids, {skill.skill_id})
            event = ledger.of_kind("business_skill_candidates")[-1]
            self.assertTrue(event["fallback_used"])
            self.assertFalse(event["prior_user_context_used"])
            self.assertNotIn("companies include", json.dumps(event))

    def test_unrelated_followup_does_not_use_prior_text_beyond_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "unrelated-followup")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Show the dashboard.",
                              supported_scope="Read the current page only."),
                chat=_Chat([]), browser=_Browser(),
                control=ReadOnlyControl(),
                ledger=ledger,
                business_tools=runtime,
                conversation_context={
                    "schema_version": 1,
                    "prior_turns": [{
                        "message_id": "m1",
                        "user": "Which companies are available?",
                        "assistant": "Acme",
                        "status": "finished",
                    }],
                },
            )

            self.assertEqual(session._task_business_skill_ids, {skill.skill_id})
            event = ledger.of_kind("business_skill_candidates")[-1]
            self.assertTrue(event["fallback_used"])
            self.assertFalse(event["prior_user_context_used"])

    def test_denied_server_mutation_is_terminal_and_never_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-denied")

            def deny(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="denied",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=3, wall_budget_s=30, run_id="write-denied",
                business_tools=runtime, business_mutation_approval=deny)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "approval_denied")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(result.business_control["approval_denied"], 1)
            self.assertEqual(result.business_control["mutation_execution_started"], 0)
            self.assertEqual(len(ledger.of_kind("business_mutation_denied")), 1)

    def test_in_flight_business_key_is_a_product_refusal_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            journal = MemoryMutationJournal()
            first = BusinessToolRuntime(
                (skill,), _MutationExecutor(), mutation_journal=journal)
            blocker = first.prepare_mutation(
                "create_company", {"name": "Acme"}, {skill.skill_id})
            journal.claim(blocker.content_digest, "")

            executor = _MutationExecutor()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": " Acme "}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-blocked")

            def approve(proposal):
                return SimpleNamespace(
                    approval_id="approval-2", decision="approved",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, step_budget=3, wall_budget_s=30,
                run_id="write-blocked", business_tools=runtime,
                business_mutation_approval=approve)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "business_mutation_blocked")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(result.business_control["approval_granted"], 1)
            self.assertEqual(result.business_control["mutation_execution_started"], 0)
            self.assertEqual(len(ledger.of_kind("business_mutation_execution_started")), 0)
            event = ledger.of_kind("business_mutation_blocked")[-1]
            self.assertEqual(event["reason"], "business_key_unresolved")
            self.assertFalse(event["dispatched"])
            self.assertFalse(event["side_effect_possible"])

    def test_authoritative_business_refusal_is_terminal_before_approval(self):
        class DuplicateRefusingExecutor(_MutationExecutor):
            @staticmethod
            def prepare_mutation(name, args, *, context=None):
                raise BusinessMutationRefusedError(
                    "a company with the same name already exists", reason="duplicate")

        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = DuplicateRefusingExecutor()
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            asked = []
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "duplicate-refused")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="创建公司 Acme。",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, step_budget=3, wall_budget_s=30,
                run_id="duplicate-refused", business_tools=runtime,
                business_mutation_approval=lambda proposal: asked.append(proposal))
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "business_mutation_refused")
            self.assertEqual(result.detail, "duplicate")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(asked, [])
            self.assertEqual(result.business_control["approval_requested"], 0)
            event = ledger.of_kind("business_mutation_rejected")[-1]
            self.assertEqual(event["error_code"], "business_mutation_refused")
            self.assertEqual(event["reason"], "duplicate")
            self.assertFalse(event["dispatched"])
            self.assertFalse(event["side_effect_possible"])

    def test_a_missing_scope_is_a_typed_permission_refusal_not_an_identity_fault(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            runtime = BusinessToolRuntime(
                (skill,), _MutationExecutor(), application_id="fixture",
                required_scopes={"create_company": "companies:write"})
            with self.assertRaises(BusinessToolPermissionError) as refused:
                runtime.prepare_mutation(
                    "create_company", {"name": "Acme"}, {skill.skill_id},
                    context=_operation_context(scopes=("companies:read",)))
            self.assertEqual(refused.exception.code, "business_permission_denied")
            self.assertEqual(refused.exception.layer, "tool_authorization")
            self.assertEqual(refused.exception.required_scope, "companies:write")
            # A principal for another application is still an identity fault, told apart
            # by its type rather than by what its message says.
            with self.assertRaises(BusinessToolIdentityError) as identity:
                runtime.prepare_mutation(
                    "create_company", {"name": "Acme"}, {skill.skill_id},
                    context=_operation_context(application_id="another-app"))
            self.assertNotIsInstance(identity.exception, BusinessToolPermissionError)

    def _principal_bound_session(self, tmp, runtime, chat, context, approve, run_id):
        session = ServerAgentSession(
            task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                          supported_scope="Create exactly one company."),
            chat=chat, browser=_Browser(), control=ReadOnlyControl(),
            ledger=SessionLedger(Path(tmp) / "session.jsonl", run_id),
            step_budget=5, wall_budget_s=30, run_id=run_id, business_tools=runtime,
            business_mutation_approval=approve, principal=context.principal,
            session_id=context.session_id, turn_id=context.turn_id)
        session.entry_observation = _observation(1)
        session.plan()
        return session

    def test_a_capability_this_account_was_not_granted_says_so(self):
        """A gap the account caused reads differently from one the deployment caused (CR-14).

        A reader asking for a change got "当前会话不支持这项操作" -- which reads as though a
        different session would support it, so the person reopens the assistant instead of
        asking whoever grants permissions. The catalog holds the capability; this delegation
        may not use it, and the sentence now says that.
        """
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            runtime = BusinessToolRuntime((skill,), _MutationExecutor(),
                                          application_id="fixture")
            chat = _Chat([
                ("check_capability", {}),
                ("finish", {"outcome": "unable", "answer": {
                    "version": 1, "coverage": "undetermined", "conclusions": [
                        {"kind": "capability_gap", "evidence_ref": "capability#1"}]}}),
            ])
            # No approval path and no page actions that could make the change: this is what
            # a reader's delegation looks like from inside the session.
            context = _operation_context(scopes=("companies:read",))
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Read current companies."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(Path(tmp) / "session.jsonl", "capability-withheld"),
                step_budget=5, wall_budget_s=30, run_id="capability-withheld",
                business_tools=runtime, business_mutation_approval=None,
                principal=context.principal, session_id=context.session_id,
                turn_id=context.turn_id,
                disabled_actions=frozenset({"click", "fill", "select", "submit", "back"}))
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, CAPABILITY_GAP_TERMINAL)
            self.assertIn("does not have permission", result.agent_said)
            self.assertIn("Ask an administrator", result.agent_said)
            self.assertNotIn("this session", result.agent_said)
            event = session.ledger.of_kind("capability_check")[-1]
            self.assertEqual(event["matches"], [])
            self.assertEqual(event["withheld"], [skill.skill_id])

    def test_a_tool_authorization_refusal_ends_the_turn_before_a_proposal_or_page_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime(
                (skill,), executor, application_id="fixture",
                required_scopes={"create_company": "companies:write"})
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
                ("click", {"ref": "e1"}),
            ])
            asked = []
            session = self._principal_bound_session(
                tmp, runtime, chat, _operation_context(scopes=("companies:read",)),
                lambda proposal, **_: asked.append(proposal), "write-forbidden")
            browser = session.browser
            result = session.run()

            self.assertEqual(result.terminal_reason, "permission_denied")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(asked, [])
            self.assertEqual(len(chat.messages), 2, "the model is not asked again")
            self.assertEqual(browser.actions, [])
            self.assertEqual(result.business_control["approval_requested"], 0)
            ledger = session.ledger
            self.assertEqual(ledger.of_kind("business_mutation_proposed"), [])
            event = ledger.of_kind("business_mutation_permission_denied")[-1]
            self.assertEqual(event["layer"], "tool_authorization")
            self.assertEqual(event["required_scope"], "companies:write")
            self.assertEqual(event["error_code"], "business_permission_denied")
            self.assertFalse(event["dispatched"])
            self.assertFalse(event["side_effect_possible"])

    def test_an_application_refusal_after_dispatch_is_definite_not_indeterminate(self):
        class RefusingExecutor(_MutationExecutor):
            def execute_mutation(self, name, args, *, context=None):
                self.executions += 1
                raise BusinessToolPermissionError("the application refused this actor")

        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = RefusingExecutor()
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, application_id="fixture", mutation_journal=journal,
                required_scopes={"create_company": "companies:write"})
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])

            def approve(proposal, *, context=None):
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest,
                    operation_context_digest=context.binding_digest)

            session = self._principal_bound_session(
                tmp, runtime, chat, _operation_context(), approve, "write-refused")
            result = session.run()

            self.assertEqual(result.terminal_reason, "permission_denied")
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_indeterminate"], 0)
            ledger = session.ledger
            self.assertEqual(ledger.of_kind("business_mutation_indeterminate"), [])
            event = ledger.of_kind("business_mutation_permission_denied")[-1]
            self.assertEqual(event["layer"], "application")
            self.assertEqual(event["approval_id"], "approval-1")
            self.assertTrue(event["dispatched"])
            self.assertFalse(event["side_effect_possible"])
            digest = ledger.of_kind("business_mutation_proposed")[-1]["proposal"]["digest"]
            self.assertEqual(journal.get(digest).state, "failed")

    def test_a_transport_failure_after_mutation_dispatch_stays_indeterminate(self):
        class LostAcknowledgement(_MutationExecutor):
            def execute_mutation(self, name, args, *, context=None):
                self.executions += 1
                raise BusinessToolDispatchError("upstream response was lost")

        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), LostAcknowledgement(), application_id="fixture",
                mutation_journal=journal,
                required_scopes={"create_company": "companies:write"})
            context = _operation_context()
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, {skill.skill_id}, context=context)

            with self.assertRaises(BusinessMutationIndeterminate) as raised:
                runtime.execute_mutation(
                    proposal, proposal.content_digest, {skill.skill_id}, context=context)

            self.assertEqual(raised.exception.phase, "dispatch")
            intent = journal.get(proposal.content_digest)
            self.assertEqual(intent.state, "indeterminate")
            self.assertEqual(
                json.loads(intent.reconciliation_json)["phase"], "dispatch")

    def test_a_refused_read_is_handed_back_and_never_opens_the_page_as_its_source(self):
        """The refusal reaches the model; what it may answer from does not grow (D13).

        Ending the turn threw away the part of a question this account may see. The refusal
        now comes back, and with nothing read the turn still ends on it -- what must not
        happen, at either layer, is the same facts being taken off the page instead.
        """

        class RefusingReads(_Executor):
            @staticmethod
            def execute(*_args, **_kwargs):
                raise BusinessToolPermissionError("the application refused this read")

        cases = (
            ("tool_authorization", _Executor(), ("companies:write",), False),
            ("application", RefusingReads(), ("companies:read",), True),
        )
        for layer, executor, scopes, dispatched in cases:
            with self.subTest(layer), tempfile.TemporaryDirectory() as tmp:
                skill = _skill(Path(tmp) / "query-skill.json")
                runtime = BusinessToolRuntime(
                    (skill,), executor, application_id="fixture",
                    required_scopes={"query_companies": "companies:read"})
                chat = _Chat([
                    ("load_skill", {"skill_id": skill.skill_id}),
                    ("query_companies", {}),
                    # The page is where the same facts would be, and the turn ends before
                    # the model can put them in an answer.
                    ("finish", {"done": True, "why": "Read the companies."}),
                ])
                session = self._principal_bound_session(
                    tmp, runtime, chat, _operation_context(scopes=scopes),
                    lambda proposal, **_: None, f"read-refused-{layer}")
                browser = session.browser
                result = session.run()

                self.assertEqual(result.terminal_reason, "permission_denied")
                self.assertEqual(len(chat.messages), 3, "the refusal is handed back once")
                self.assertEqual(browser.actions, [])
                ledger = session.ledger
                self.assertEqual(ledger.of_kind("business_tool_rejected"), [])
                self.assertEqual(ledger.of_kind("business_tool_result"), [])
                event = ledger.of_kind("business_tool_permission_denied")[-1]
                self.assertTrue(event["turn_continued"])
                self.assertEqual(event["layer"], layer)
                self.assertEqual(event["dispatched"], dispatched)
                self.assertFalse(event["side_effect_possible"])
                self.assertEqual(event["error_code"], "business_permission_denied")
                if layer == "tool_authorization":
                    self.assertEqual(event["required_scope"], "companies:read")

    def test_an_unresolved_write_in_another_workspace_does_not_block_this_one(self):
        def context_in(workspace, operation_id):
            now = int(time.time() * 1000)
            principal = PrincipalContext(
                1, "fixture", "tenant-1", workspace, "actor-1", "delegation-1",
                now - 1_000, now + 60_000, ("companies:write",))
            return OperationContext.create(
                principal, session_id="session-1", turn_id="message-1",
                operation_id=operation_id, deadline_ms=now + 30_000, now_ms=now)

        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), _MutationExecutor(), application_id="fixture",
                mutation_journal=journal,
                required_scopes={"create_company": "companies:write"})
            loaded = {skill.skill_id}
            elsewhere = context_in("workspace-2", "op-elsewhere")
            blocker = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded, context=elsewhere)
            journal.claim(blocker.content_digest, blocker.operation_context_digest)

            here = context_in("workspace-1", "op-here")
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded, context=here)
            created = runtime.execute_mutation(
                proposal, proposal.content_digest, loaded, context=here)
            self.assertTrue(created.content["created"])

            # The reverse check: the same name in the blocker's own workspace is blocked.
            again = context_in("workspace-2", "op-again")
            repeat = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded, context=again)
            with self.assertRaises(BusinessMutationBlockedError):
                runtime.execute_mutation(
                    repeat, repeat.content_digest, loaded, context=again)

    def test_cancellation_after_approval_never_dispatches_the_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            cancelled = {"value": False}

            def approve_then_cancel(proposal):
                cancelled["value"] = True
                return SimpleNamespace(
                    approval_id="approval-cancel", decision="approved",
                    plan_digest=proposal.content_digest)

            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-cancelled")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=3, wall_budget_s=30, run_id="write-cancelled",
                business_tools=runtime, business_mutation_approval=approve_then_cancel,
                cancelled=lambda: cancelled["value"])
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "user_cancelled")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(result.business_control["approval_granted"], 0)
            event = ledger.of_kind("business_mutation_cancelled_before_dispatch")[-1]
            self.assertFalse(event["dispatched"])
            self.assertFalse(event["side_effect_possible"])

    def test_cancellation_during_read_tool_dispatches_no_browser_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancelled = {"value": False}

            class CancellingExecutor(_Executor):
                @staticmethod
                def execute(name, args):
                    result = _Executor.execute(name, args)
                    cancelled["value"] = True
                    return result

            skill = _skill(Path(tmp) / "query-skill.json")
            browser = _Browser()
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="List companies",
                              supported_scope="Read current companies."),
                chat=_Chat([
                    ("load_skill", {"skill_id": skill.skill_id}),
                    ("query_companies", {}),
                ]), browser=browser, control=ReadOnlyControl(),
                ledger=SessionLedger(Path(tmp) / "session.jsonl", "read-cancelled"),
                step_budget=3,
                wall_budget_s=30, run_id="read-cancelled",
                business_tools=BusinessToolRuntime((skill,), CancellingExecutor()),
                cancelled=lambda: cancelled["value"])
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "user_cancelled")
            self.assertEqual(browser.actions, [])

    def test_approved_server_mutation_returns_route_to_react_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
                ("goto", {"url": "/object/company/company-2"}),
                (None, {"content": "Created and opened Acme."}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-approved")

            def approve(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest)

            browser = _Browser()
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=4, wall_budget_s=30, run_id="write-approved",
                business_tools=runtime, business_mutation_approval=approve,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_completed"], 1)
            self.assertEqual(browser.url, "http://app.test/object/company/company-2")
            self.assertEqual(len(ledger.of_kind("business_mutation_result")), 1)

    def test_cancelling_followup_observation_does_not_rewrite_completed_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancelled = {"value": False}

            class CancelAfterCompletedMutation(_MutationExecutor):
                def execute_mutation(self, name, args, *, context=None):
                    result = super().execute_mutation(name, args, context=context)
                    cancelled["value"] = True
                    return result

            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = CancelAfterCompletedMutation()
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-followup-cancelled")

            def approve(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=3, wall_budget_s=30, run_id="write-followup-cancelled",
                business_tools=runtime, business_mutation_approval=approve,
                cancelled=lambda: cancelled["value"])
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            completed = ledger.of_kind("business_mutation_result")[-1]
            intent = journal.get(completed["proposal_digest"])
            self.assertIsNotNone(intent)
            self.assertEqual(intent.state, "completed")
            self.assertEqual(intent.result_digest, completed["result_digest"])
            self.assertEqual(result.terminal_reason, "user_cancelled")
            self.assertIn("completed", result.agent_said)
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_completed"], 1)
            self.assertEqual(result.business_control["mutation_indeterminate"], 0)
            followup = ledger.of_kind("business_mutation_followup_cancelled")
            self.assertEqual(len(followup), 1)
            self.assertTrue(followup[0]["completed"])

    def test_indeterminate_server_mutation_stops_without_model_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor(indeterminate=True)
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
                ("create_company", {"name": "Acme"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-unknown")

            def approve(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=4, wall_budget_s=30, run_id="write-unknown",
                business_tools=runtime, business_mutation_approval=approve)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "indeterminate_write")
            self.assertEqual(result.steps_used, 2)
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_indeterminate"], 1)
            self.assertEqual(len(chat.messages), 2,
                             "the model must not receive a chance to retry")
            self.assertEqual(len(ledger.of_kind("business_mutation_indeterminate")), 1)

    def test_load_query_and_finish_use_server_facts_without_page_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _ProjectedExecutor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("finish", {"done": True, "why": "Acme is the current company."}),
            ])
            browser = _Browser()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "business-react")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Which companies are in this workspace?",
                              supported_scope="Read current business data only."),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=4, wall_budget_s=30, run_id="business-react",
                business_tools=runtime,
                plain_business_answer_min_page_actions=0)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertTrue(result.business_answer_projected)
            self.assertEqual(result.steps_used, 3)
            self.assertIn("load_skill", chat.tool_names[0])
            self.assertNotIn("query_companies", chat.tool_names[0])
            self.assertIn("wait_for", chat.tool_names[0])
            self.assertIn("query_companies", chat.tool_names[1])
            self.assertEqual(browser.actions, [])
            self.assertEqual(ledger.of_kind("action_receipt"), [])
            facts = [message["content"] for turn in chat.messages for message in turn
                     if message.get("role") == "tool"]
            self.assertTrue(any("BUSINESS SKILL LOADED" in text for text in facts))
            self.assertTrue(any('"name":"Acme"' in text for text in facts))
            self.assertTrue(any("SERVER QUERY ANSWER READY" in text for text in facts))
            self.assertEqual(len(ledger.of_kind("business_skill_loaded")), 1)
            self.assertEqual(ledger.of_kind("business_answer")[0]["terminal_tool"], "finish")
            candidates = ledger.of_kind("business_skill_candidates")
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["matcher_version"],
                             "business-skill-trigger-v2")
            self.assertFalse(candidates[0]["fallback_used"])
            self.assertEqual(candidates[0]["candidate_ids"], [skill.skill_id])
            self.assertNotIn("Which companies", json.dumps(candidates[0]))
            rows = ledger.of_kind("business_tool_result")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["audit"]["oauth_actor"], "delegated_user")
            self.assertNotIn("Acme", json.dumps(rows[0]),
                             "the tool audit stores a digest and projection, not raw rows")

    def test_plain_answer_after_successful_read_tool_is_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _ProjectedExecutor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                (None, {"content": "Acme (acme.test)"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "plain-business-answer")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Which companies are in this workspace?",
                              supported_scope="Read current business data only."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=3, wall_budget_s=30, run_id="plain-business-answer",
                business_tools=runtime,
                plain_business_answer_min_page_actions=0)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.agent_said, "Acme (acme.test)")
            self.assertTrue(result.business_answer_projected)
            self.assertEqual(session.latest_business_tool_result.content["companies"], [{
                "name": "Acme", "domain": "acme.test",
                "ui_path": "/object/company/company-1",
            }])
            self.assertNotIn("latest_business_tool_result", result.to_json(),
                             "bounded data stays in server memory unless the product renders it")
            self.assertEqual(result.steps_used, 3)
            self.assertEqual(result.spend["provider_calls"], 0)
            self.assertEqual(len(ledger.of_kind("business_answer")), 1)
            self.assertEqual(len(ledger.of_kind("no_tool_call")), 0)

    def test_complete_empty_query_finishes_without_another_model_round(self):
        class EmptyProjectedExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                observed_at = "2026-09-13T04:00:00+00:00"
                origin = Origin(
                    kind="typed", reference="query_companies#0",
                    observed_at=observed_at,
                    scope="companies visible to the delegated user; name contains S00042")
                return BusinessToolResult(
                    content={"companies": [], "matched_count": 0},
                    audit={"application": "fixture", "operation": name,
                           "returned_count": 0},
                    query_answer=StructuredQueryAnswer(
                        collection="companies", fields=("name", "domain"), records=(),
                        source="fixture API", scope=origin.scope, observed_at=observed_at,
                        source_complete=True, result_truncated=False, matched_count=0,
                        source_total=0, records_scanned=0),
                    evidence=(QueryEvidence(
                        evidence_id="query#1", origin=origin, collection="companies",
                        conditions=("name contains S00042",), matched_count=0,
                        records_scanned=0, covers_whole_scope=True),),
                )

        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("goto", {"url": "/should-not-run"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "empty-query")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="报价单 S00042 是哪个客户的？",
                              supported_scope="Read current business data only."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(), ledger=ledger,
                step_budget=5, wall_budget_s=30, run_id="empty-query",
                business_tools=BusinessToolRuntime((skill,), EmptyProjectedExecutor()),
                plain_business_answer_min_page_actions=0)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.steps_used, 2)
            self.assertEqual(len(chat.messages), 2)
            self.assertIn("未找到", result.agent_said)
            self.assertIsNotNone(result.verified_answer)
            self.assertEqual(
                ledger.of_kind("answer_verified")[-1]["deterministic"], "empty_query")

    def test_complete_empty_preflight_does_not_finish_a_mutation_task(self):
        class EmptyMutationExecutor(_MutationExecutor):
            @staticmethod
            def tool_schemas():
                return (SCHEMA, WRITE_SCHEMA)

            @staticmethod
            def tool_effects():
                return {"query_companies": "read", "create_company": "write"}

            @staticmethod
            def execute(name, args, *, context=None):
                if name != "query_companies" or args:
                    raise BusinessToolError("unexpected read")
                observed_at = "2026-09-13T04:00:00+00:00"
                origin = Origin(
                    kind="typed", reference="query_companies#0",
                    observed_at=observed_at, scope="current companies")
                return BusinessToolResult(
                    content={"companies": [], "matched_count": 0},
                    audit={"application": "fixture", "operation": name,
                           "returned_count": 0},
                    query_answer=StructuredQueryAnswer(
                        collection="companies", fields=("name", "domain"), records=(),
                        source="fixture API", scope=origin.scope, observed_at=observed_at,
                        source_complete=True, result_truncated=False, matched_count=0,
                        source_total=0, records_scanned=0),
                    evidence=(QueryEvidence(
                        evidence_id="query#1", origin=origin, collection="companies",
                        conditions=("name contains P72 Acceptance Write",),
                        matched_count=0, records_scanned=0, covers_whole_scope=True),),
                )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "create-skill.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "skill_id": "fixture.companies-create",
                "title": "Create company",
                "intent": "Create one company after checking duplicates.",
                "triggers": ["create company", "创建公司"],
                "instructions": ["Query duplicates, then propose the write."],
                "tool_names": ["query_companies", "create_company"],
                "provenance": ["src:fixture@v1:companies.json#create"],
                "read_only": False,
            }) + "\n")
            skill = BusinessSkill.load(path)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("finish", {"outcome": "answered", "answer": {
                    "version": 1, "coverage": "complete",
                    "conclusions": [{"kind": "not_found",
                                     "evidence_ref": "call2-query#1"}],
                }}),
                ("create_company", {"name": "P72 Acceptance Write"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "empty-preflight")

            def deny(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="denied",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="创建公司 P72 Acceptance Write",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(), ledger=ledger,
                step_budget=5, wall_budget_s=30, run_id="empty-preflight",
                business_tools=BusinessToolRuntime(
                    (skill,), EmptyMutationExecutor(),
                    mutation_journal=MemoryMutationJournal()),
                business_mutation_approval=deny,
                plain_business_answer_min_page_actions=0)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "approval_denied")
            self.assertEqual(result.steps_used, 4)
            self.assertEqual(len(chat.messages), 4)
            self.assertEqual(len(ledger.of_kind("answer_contract_refused")), 1)
            self.assertEqual(ledger.of_kind("answer_verified"), [])

    def test_read_failure_records_its_machine_code_for_external_scoring(self):
        class UnavailableExecutor(_Executor):
            @staticmethod
            def execute(*_args, **_kwargs):
                raise BusinessToolUnavailableError("private upstream detail")

        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "typed-read-failure")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="List companies",
                              supported_scope="Read current companies."),
                chat=_Chat([
                    ("load_skill", {"skill_id": skill.skill_id}),
                    ("query_companies", {}),
                    ("finish", {"done": False, "why": "The data source failed."}),
                ]), browser=_Browser(), control=ReadOnlyControl(), ledger=ledger,
                step_budget=4,
                wall_budget_s=30, run_id="typed-read-failure",
                business_tools=BusinessToolRuntime((skill,), UnavailableExecutor()))
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_refused")
            rejected = ledger.of_kind("business_tool_rejected")
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["error_code"], "business_unavailable")

    def test_exact_tool_result_route_can_open_a_record_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _ProjectedExecutor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("goto", {"url": "/object/company/company-1"}),
                (None, {"content": "Opened Acme."}),
            ])
            browser = _Browser()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "business-route")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open Acme from companies.",
                              supported_scope="Read and open one company record."),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=4, wall_budget_s=30, run_id="business-route",
                business_tools=runtime,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.agent_said, "Opened Acme.")
            self.assertEqual(result.page_actions, 1)
            self.assertFalse(result.business_answer_projected)
            self.assertEqual(browser.actions, ["goto"])
            self.assertEqual(browser.url, "http://app.test/object/company/company-1")
            opened = ledger.of_kind("business_route_opened")
            self.assertEqual(len(opened), 1)
            self.assertNotIn("company-1", json.dumps(opened[0]))

    def test_unreturned_business_route_is_not_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("goto", {"url": "/object/company/invented"}),
                ("finish", {"done": False, "why": "No verified route."}),
            ])
            browser = _Browser()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "route-refusal")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open the requested item from companies.",
                              supported_scope="Read and open one company record."),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=4, wall_budget_s=30, run_id="route-refusal",
                business_tools=runtime,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_refused")
            self.assertEqual(browser.actions, [])
            self.assertEqual(len(ledger.of_kind("page_route_rejected")), 1)
            self.assertEqual(len(ledger.of_kind("business_route_opened")), 0)

    def test_plain_answer_waits_for_required_page_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                (None, {"content": "Opened Acme."}),
                ("goto", {"url": "/object/company/company-1"}),
                (None, {"content": "Opened Acme."}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "route-before-answer")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open Acme from companies.",
                              supported_scope="Read and open one company record."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=5, wall_budget_s=30, run_id="route-before-answer",
                business_tools=runtime,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.page_actions, 1)
            self.assertEqual(len(ledger.of_kind("no_tool_call")), 1)
            self.assertEqual(len(ledger.of_kind("business_answer")), 1)


class TriggersRankTheCatalogRatherThanGateIt(unittest.TestCase):
    """A write phrase loses to word order; the read phrase beside it does not.

    A write trigger is a verb and its object -- "confirm quotation", "确认报价单" -- and the
    read trigger for the same subject is usually the object alone. So every sentence matching
    the write also matches the read, and none of the reverse. When word order breaks the write
    phrase, the read Skill still matches; the selection is not empty, so the empty-match
    fallback never runs, and the turn silently loses its write tool. It then refuses for want
    of a tool, which reads exactly like the product declining to act.
    """

    @staticmethod
    def _catalog(root: Path) -> tuple:
        read = root / "read.json"
        read.write_text(json.dumps({
            "schema_version": 1, "skill_id": "fixture.orders-query",
            "title": "Query orders", "intent": "Read current orders.",
            "triggers": ["order", "订单"],
            "instructions": ["Call query_orders and trust its current result."],
            "tool_names": ["query_orders"],
            "provenance": ["src:fixture@v1:orders.json#orders"], "read_only": True,
        }) + "\n")
        # The shape the rule keys on: a mutation Skill carries the read tool its own
        # preflight needs, so its tool set strictly extends the read-only Skill's.
        write = root / "write.json"
        write.write_text(json.dumps({
            "schema_version": 1, "skill_id": "fixture.orders-confirm",
            "title": "Confirm an order", "intent": "Confirm one current order.",
            "triggers": ["confirm order", "确认订单"],
            "instructions": ["Read the order, then propose confirming exactly it."],
            "tool_names": ["query_orders", "confirm_order"],
            "provenance": ["src:fixture@v1:orders.json#confirm"], "read_only": False,
        }) + "\n")
        unrelated = root / "unrelated.json"
        unrelated.write_text(json.dumps({
            "schema_version": 1, "skill_id": "fixture.people-create",
            "title": "Create a person", "intent": "Create one current person.",
            "triggers": ["create person", "创建联系人"],
            "instructions": ["Propose one person and wait for approval."],
            "tool_names": ["create_person"],
            "provenance": ["src:fixture@v1:people.json#create"], "read_only": False,
        }) + "\n")
        return tuple(BusinessSkill.load(path) for path in (read, write, unrelated))

    class _Executor:
        @staticmethod
        def tool_schemas():
            return tuple(
                {"type": "function",
                 "function": {"name": name, "description": name,
                              "parameters": {"type": "object", "properties": {},
                                             "additionalProperties": False}}}
                for name in ("query_orders", "confirm_order", "create_person"))

        @staticmethod
        def tool_effects():
            return {"query_orders": "read", "confirm_order": "write",
                    "create_person": "write"}

    def _select(self, task: str, *, allow_mutations: bool):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = BusinessToolRuntime(self._catalog(Path(tmp)), self._Executor())
            return runtime.select(task, allow_mutations=allow_mutations)

    def test_the_write_skill_that_extends_a_matched_read_skill_is_offered_with_it(self):
        """The verb is last, so only the read phrase matches -- and the write is the point."""
        selection = self._select("把订单 S00002 确认掉", allow_mutations=True)

        self.assertTrue(selection.mutation_surface_widened)
        self.assertEqual(set(selection.candidate_ids),
                         {"fixture.orders-query", "fixture.orders-confirm"})

    def test_an_unrelated_write_skill_is_not_dragged_in(self):
        """Widening keyed on intent would be another guess; this one is keyed on tool sets."""
        selection = self._select("把订单 S00002 确认掉", allow_mutations=True)
        self.assertNotIn("fixture.people-create", selection.candidate_ids)

    def test_a_read_only_turn_is_left_exactly_as_it_was(self):
        selection = self._select("把订单 S00002 确认掉", allow_mutations=False)

        self.assertFalse(selection.mutation_surface_widened)
        self.assertEqual(selection.candidate_ids, ("fixture.orders-query",))

    def test_a_phrase_that_matches_the_write_directly_widens_nothing(self):
        selection = self._select("确认订单 S00002", allow_mutations=True)

        self.assertFalse(selection.mutation_surface_widened)
        self.assertEqual(selection.candidate_ids, ("fixture.orders-confirm",))


    def test_a_sentence_about_two_subjects_gains_neither_ones_write(self):
        """Subsuming every matched read Skill is what keeps a mention from arming a write.

        "Create a person named Bob at Acme company" matches people and companies both, and
        company creation extends only the companies read -- so the sentence is about more
        than that write, and the surface stays where the triggers put it. `examples/twenty`
        has carried this as a packaged-Skill test; it belongs next to the rule as well.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orders, confirm, _people = self._catalog(root)
            other = root / "other.json"
            other.write_text(json.dumps({
                "schema_version": 1, "skill_id": "fixture.people-query",
                "title": "Query people", "intent": "Read current people.",
                "triggers": ["person", "联系人"],
                "instructions": ["Call query_people and trust its current result."],
                "tool_names": ["query_people"],
                "provenance": ["src:fixture@v1:people.json#people"], "read_only": True,
            }) + "\n")

            class _Both:
                @staticmethod
                def tool_schemas():
                    return tuple(
                        {"type": "function",
                         "function": {"name": name, "description": name,
                                      "parameters": {"type": "object", "properties": {},
                                                     "additionalProperties": False}}}
                        for name in ("query_orders", "confirm_order", "query_people"))

                @staticmethod
                def tool_effects():
                    return {"query_orders": "read", "confirm_order": "write",
                            "query_people": "read"}

            runtime = BusinessToolRuntime(
                (orders, confirm, BusinessSkill.load(other)), _Both())
            selection = runtime.select("把这个联系人的订单找出来", allow_mutations=True)

        self.assertFalse(selection.mutation_surface_widened)
        self.assertNotIn("fixture.orders-confirm", selection.candidate_ids)

    def test_a_subject_with_no_write_skill_gains_nothing(self):
        """A read Skill no mutation Skill extends adds nothing, however the turn is armed."""
        class _ReadOnly:
            @staticmethod
            def tool_schemas():
                return ({"type": "function",
                         "function": {"name": "query_orders", "description": "query_orders",
                                      "parameters": {"type": "object", "properties": {},
                                                     "additionalProperties": False}}},)

            @staticmethod
            def tool_effects():
                return {"query_orders": "read"}

        with tempfile.TemporaryDirectory() as tmp:
            runtime = BusinessToolRuntime(self._catalog(Path(tmp))[:1], _ReadOnly())
            selection = runtime.select("列出所有订单", allow_mutations=True)

        self.assertFalse(selection.mutation_surface_widened)
        self.assertEqual(selection.candidate_ids, ("fixture.orders-query",))



if __name__ == "__main__":
    unittest.main()
