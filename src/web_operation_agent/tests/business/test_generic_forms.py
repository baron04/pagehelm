from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from web_operation_agent.answer.evidence import Origin, SourceEvidence, TurnEvidence
from web_operation_agent.business.forms import (
    ApplicationFormDescriptor,
    ApplicationFormField,
    ApplicationFormTools,
    FormChoice,
)
from web_operation_agent.business.runtime import FormDraft, FormDraftField
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.errors import (
    BusinessToolContractError,
    BusinessToolRequestError,
)
from web_operation_agent.schemas import (SCHEMA_VERSIONS, ElementRef, PageObservation,
                                         SourceMap, SourceRelation)
from web_operation_agent.composition.ports import DeclaredRoutePort
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.execution.form_tools import _source_bases
from web_operation_agent.execution.prompts import AGENT_SYSTEM, FORM_SYSTEM
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.business import _Browser, _Chat


def _observation(*, epoch: int = 1, include_duplicate: bool = False) -> PageObservation:
    elements = [
        ElementRef("e1", "textbox", "Company", "input", epoch, {"field": "company_name"}),
        ElementRef("e2", "select", "State", "select", epoch, {"field": "state"}),
        # The application did not declare this observed field writable.
        ElementRef("e3", "textbox", "Internal note", "input", epoch,
                   {"field": "internal_note"}),
    ]
    if include_duplicate:
        elements.append(ElementRef(
            "e4", "textbox", "Company copy", "input", epoch,
            {"field": "company_name"}))
    return PageObservation(
        schema_version=1, epoch=epoch, url="https://app.test/companies/new",
        title="New company", landmarks=[], elements=elements,
        content="current form")


class _Forms:
    required_scopes = frozenset()

    def __init__(self):
        self.revision = 1
        self.exceed = False

    def describe_form(self, observation, *, context=None):
        fields = (
            ApplicationFormField(
                "company_name", "Company name", controls=("textbox", "combobox"),
                required=True),
            ApplicationFormField(
                "state", "State", kind="choice", controls=("select",),
                choices=(FormChoice("draft", "Draft"), FormChoice("active", "Active"))),
            # Metadata alone is insufficient: this field is not in the page observation.
            ApplicationFormField("email", "Email", kind="email"),
        )
        if self.revision == 2:
            fields = fields[1:]
        return ApplicationFormDescriptor(
            reference=f"company-create-v{self.revision}", title="New company",
            route="/companies/new", fields=fields,
            scope="fields writable by the current actor")

    def prepare_form(self, form, values, *, context=None):
        fields = tuple(FormDraftField(
            item.field.reference, item.field.label, item.value, item.control)
            for item in values)
        if self.exceed:
            fields += (FormDraftField("email", "Email", "invented@example.test"),)
        return FormDraft(route=form.route, fields=fields)


class GenericCurrentFormContract(unittest.TestCase):
    def setUp(self):
        self.port = _Forms()
        self.tools = ApplicationFormTools(
            self.port, application_id="fixture.forms", binding_secret=b"f" * 32)

    def test_description_is_the_page_metadata_intersection_and_hides_native_references(self):
        result = self.tools.describe(_observation(), {})

        self.assertTrue(result["form_available"])
        self.assertEqual([item["label"] for item in result["fields"]],
                         ["Company name", "State"])
        self.assertNotIn("internal_note", str(result))
        self.assertNotIn("company_name", str(result))
        self.assertNotIn("email", str(result).casefold())
        self.assertRegex(result["form_id"], r"^form_[0-9a-f]{32}$")
        self.assertTrue(all(item["field_id"].startswith("form_field_")
                            for item in result["fields"]))

    def test_ambiguous_dom_field_is_not_actionable(self):
        result = self.tools.describe(_observation(include_duplicate=True), {})

        self.assertEqual([item["label"] for item in result["fields"]], ["State"])

    def test_disabled_or_read_only_page_fields_are_not_actionable(self):
        observation = _observation()
        observation.elements[0].attrs["readonly"] = "true"
        observation.elements[1].attrs["disabled"] = "true"

        result = self.tools.describe(observation, {})

        self.assertEqual(result["fields"], [])

    def test_prepare_revalidates_opaque_ids_and_returns_only_selected_fields(self):
        observation = _observation()
        described = self.tools.describe(observation, {})
        by_label = {item["label"]: item["field_id"] for item in described["fields"]}

        draft, result = self.tools.prepare(observation, {
            "form_id": described["form_id"],
            "values": [
                {"field_id": by_label["Company name"], "value": "NovaForge"},
                {"field_id": by_label["State"], "value": "active"},
            ],
        })

        self.assertEqual([(item.field, item.value, item.control) for item in draft.fields], [
            ("company_name", "NovaForge", "textbox"),
            ("state", "active", "select"),
        ])
        self.assertEqual(result["fields"], ["Company name", "State"])
        self.assertNotIn("company_name", str(result))

    def test_page_or_metadata_change_makes_every_old_binding_stale(self):
        observation = _observation()
        described = self.tools.describe(observation, {})
        field_id = described["fields"][0]["field_id"]
        request = {"form_id": described["form_id"],
                   "values": [{"field_id": field_id, "value": "NovaForge"}]}

        with self.assertRaisesRegex(BusinessToolRequestError, "stale"):
            self.tools.prepare(_observation(epoch=2), request)
        self.port.revision = 2
        with self.assertRaisesRegex(BusinessToolRequestError, "stale"):
            self.tools.prepare(observation, request)

    def test_choice_must_be_an_adapter_declared_value(self):
        observation = _observation()
        described = self.tools.describe(observation, {})
        state = next(item for item in described["fields"] if item["label"] == "State")

        with self.assertRaisesRegex(BusinessToolRequestError, "declared choice"):
            self.tools.prepare(observation, {
                "form_id": described["form_id"],
                "values": [{"field_id": state["field_id"], "value": "deleted"}],
            })

    def test_malformed_unhashable_field_id_is_a_typed_request_refusal(self):
        observation = _observation()
        described = self.tools.describe(observation, {})
        with self.assertRaisesRegex(BusinessToolRequestError, "field_ids"):
            self.tools.prepare(observation, {
                "form_id": described["form_id"],
                "values": [{"field_id": [], "value": "NovaForge"}],
            })

    def test_adapter_cannot_add_an_unselected_native_field_to_the_draft(self):
        observation = _observation()
        described = self.tools.describe(observation, {})
        self.port.exceed = True

        with self.assertRaisesRegex(BusinessToolContractError, "exceeds"):
            self.tools.prepare(observation, {
                "form_id": described["form_id"],
                "values": [{"field_id": described["fields"][0]["field_id"],
                            "value": "NovaForge"}],
            })

    def test_source_derived_value_requires_current_release_exact_evidence(self):
        excerpt = "For a confirmed company, choose the active state."
        evidence = TurnEvidence()
        evidence.record(SourceEvidence(
            evidence_id="source#1",
            origin=Origin(kind="source", reference="lookup_source:company state",
                          observed_at="1", scope="release v1"),
            path="apps/contacts/rules.py", start_line=4, end_line=4,
            release="v1", sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
            excerpt=excerpt, summary="confirmed companies use active state"))
        session = SimpleNamespace(evidence=evidence,
                                  source_map=SimpleNamespace(release="v1"))
        value = {"field_id": "form_field_" + "a" * 32, "value": "active",
                 "source_basis": {"ref": "source#1",
                                  "support": "choose the active state"}}
        args = {"form_id": "form_" + "b" * 32, "values": [value]}

        stripped, refs = _source_bases(session, args)

        self.assertEqual(refs, ("source#1",))
        self.assertEqual(stripped["values"], [{"field_id": value["field_id"],
                                                "value": "active"}])
        self.assertIn("source_basis", args["values"][0])
        for bad_basis in (
            {"ref": "source#2", "support": "choose the active state"},
            {"ref": "source#1", "support": "choose the archived state"},
            {"ref": "source#1", "support": "short"},
        ):
            with self.subTest(bad_basis=bad_basis), self.assertRaises(
                    BusinessToolRequestError):
                _source_bases(session, {**args, "values": [{**value,
                                             "source_basis": bad_basis}]})
        session.source_map.release = "v2"
        with self.assertRaisesRegex(BusinessToolRequestError, "deployment"):
            _source_bases(session, args)

    def test_form_prompt_distinguishes_draft_from_submission(self):
        self.assertIn("A source rule may help derive a value",
                      " ".join(FORM_SYSTEM.split()))
        self.assertIn("Never submit a draft-only task", AGENT_SYSTEM)
        self.assertNotIn("then click its submit control", AGENT_SYSTEM)


class GenericFormProductPathNeedsNoPerFormSkill(unittest.TestCase):
    class Browser(_Browser):
        def __init__(self):
            super().__init__()
            self.url = "http://app.test/companies/new"

        def _page(self):
            return PageObservation(
                schema_version=1, epoch=self.epoch, url=self.url,
                title="New company", landmarks=[], elements=[
                    ElementRef("e1", "textbox", "Company", "input", self.epoch,
                               {"field": "company_name"}),
                    ElementRef("e2", "select", "State", "select", self.epoch,
                               {"field": "state"}),
                ], content="current company form")

        def observe(self):
            return self._page()

        def execute(self, action, args=None, expect_navigation=False):
            receipt = super().execute(action, args, expect_navigation)
            receipt.observation = self._page()
            receipt.url_after = self.url
            return receipt

    class Chat(_Chat):
        def __init__(self):
            super().__init__([])
            self.step = 0

        def complete(self, messages, tools, _run_id, **_kwargs):
            self.messages.append([dict(message) for message in messages])
            self.tool_names.append({tool["function"]["name"] for tool in tools})
            self.step += 1
            if self.step == 1:
                name, args = "describe_form", {"field_query": "company name"}
            elif self.step == 2:
                payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
                name, args = "prepare_form", {
                    "form_id": payload["form_id"],
                    "values": [{"field_id": payload["fields"][0]["field_id"],
                                "value": "NovaForge"}],
                }
            else:
                name, args = "prefill_form", {}
            return {"tool_calls": [{"id": str(self.step), "function": {
                "name": name, "arguments": json.dumps(args)}}]}

    def test_prefill_before_a_draft_exists_is_refused_without_dispatch(self):
        """prefill_form is on the tool list from the first call so the list never changes
        mid-turn; before an application-issued draft exists it must do nothing at all."""
        class EarlyChat(self.Chat):
            def complete(self, messages, tools, run_id, **kwargs):
                if not self.messages:
                    self.messages.append([dict(m) for m in messages])
                    self.tool_names.append({t["function"]["name"] for t in tools})
                    return {"tool_calls": [{"id": "early", "function": {
                        "name": "prefill_form", "arguments": "{}"}}]}
                return super().complete(messages, tools, run_id, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            browser, chat = self.Browser(), EarlyChat()
            routes = DeclaredRoutePort("fixture.forms", (r"/companies/new",))
            ledger = SessionLedger(Path(tmp) / "audit.jsonl", "early-prefill")
            session = ServerAgentSession(
                task=TaskSpec("T", "fill the company name but do not submit"),
                chat=chat, browser=browser, control=ReadOnlyControl(), ledger=ledger,
                application_routes=routes, application_forms=_Forms(),
                disabled_actions={"click", "fill", "select", "submit"},
                allow_form_prefill=True, step_budget=5, wall_budget_s=30)
            session.entry_observation = browser.observe()
            session.plan()

            result = session.run()

            self.assertIn("prefill_form", chat.tool_names[0])
            self.assertIn("No current application-issued form draft",
                          chat.messages[1][-1]["content"])
            # The refused call reached no browser; the one fill is the real draft's.
            self.assertEqual(browser.actions, ["fill", "handoff_form"])
            self.assertEqual(result.terminal_reason, "agent_finished")

    def test_describe_prepare_and_prefill_run_without_a_matching_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            browser, chat = self.Browser(), self.Chat()
            routes = DeclaredRoutePort(
                "fixture.forms", (r"/companies/new",))
            ledger = SessionLedger(Path(tmp) / "audit.jsonl", "generic-form")
            session = ServerAgentSession(
                task=TaskSpec("T", "fill the company name but do not submit"),
                chat=chat, browser=browser, control=ReadOnlyControl(), ledger=ledger,
                application_routes=routes, application_forms=_Forms(),
                disabled_actions={"click", "fill", "select", "submit"},
                allow_form_prefill=True, step_budget=4, wall_budget_s=30)
            session.entry_observation = browser.observe()
            session.plan()

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(browser.actions, ["fill", "handoff_form"])
            # Offered from the first call so the tool list never changes mid-turn; the
            # server refuses it until an application-issued draft exists.
            self.assertIn("prefill_form", chat.tool_names[0])
            self.assertIn("describe_form", chat.tool_names[0])
            self.assertIn("prepare_form", chat.tool_names[0])
            self.assertIn("prefill_form", chat.tool_names[2])
            self.assertEqual(ledger.of_kind("business_skill_loaded"), [])
            self.assertEqual(len(ledger.of_kind("application_form_prepared")), 1)
            self.assertEqual(len(ledger.of_kind("form_prefill_completed")), 1)
            self.assertNotIn("submit", browser.actions)

    def test_deployed_source_rule_can_choose_a_current_field_value_without_skill(self):
        class RuleChat(_Chat):
            def __init__(self):
                super().__init__([])
                self.step = 0

            def complete(self, messages, tools, _run_id, **_kwargs):
                self.messages.append([dict(message) for message in messages])
                self.tool_names.append({tool["function"]["name"] for tool in tools})
                self.step += 1
                if self.step == 1:
                    name, args = "lookup_source", {
                        "query": "confirmed company state rule"}
                elif self.step == 2:
                    self.test_source_result = messages[-1]["content"]
                    name, args = "describe_form", {"field_query": "State"}
                elif self.step == 3:
                    descriptor = json.loads(messages[-1]["content"].split("\n", 1)[1])
                    selected = "active" if "choose the active state" in self.test_source_result \
                        else "draft"
                    name, args = "prepare_form", {
                        "form_id": descriptor["form_id"],
                        "values": [{
                            "field_id": descriptor["fields"][0]["field_id"],
                            "value": selected,
                            "source_basis": {
                                "ref": "source#1",
                                "support": f"choose the {selected} state",
                            },
                        }],
                    }
                else:
                    name, args = "prefill_form", {}
                return {"tool_calls": [{"id": str(self.step), "function": {
                    "name": name, "arguments": json.dumps(args)}}]}

        for release, expected in (("release-active", "active"),
                                  ("release-draft", "draft")):
            with self.subTest(release=release), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source_path = root / "apps" / "contacts" / "rules.py"
                source_path.parent.mkdir(parents=True)
                source_path.write_text(
                    f"For a confirmed company, choose the {expected} state.\n",
                    encoding="utf-8")
                tree = SourceTree({
                    "repository_id": "fixture/contacts", "release": release,
                    "allow_globs": ["apps/**/*.py"], "deny_globs": [],
                }, root)
                source_map = SourceMap(
                    schema_version=SCHEMA_VERSIONS["SourceMap"],
                    attestation_id="test-release", repository_id=tree.repository_id,
                    release=release,
                    relations=[SourceRelation(
                        kind="service_entity", subject="company", object="state",
                        attrs={"help": (
                            f"For a confirmed company, choose the {expected} state.")},
                        evidence=(tree.cite("apps/contacts/rules.py", 1, 1,
                                            excerpt_chars=200),))],
                )
                browser, chat = self.Browser(), RuleChat()
                session = ServerAgentSession(
                    task=TaskSpec("T", "Set the confirmed company state in the draft"),
                    chat=chat, browser=browser, control=ReadOnlyControl(),
                    ledger=SessionLedger(root / "audit.jsonl", "source-form"),
                    application_routes=DeclaredRoutePort(
                        "fixture.forms", (r"/companies/new",)),
                    application_forms=_Forms(),
                    source=SourceCapability(source_map=source_map,
                                            runtime_source=True),
                    disabled_actions={"click", "fill", "select", "submit"},
                    allow_form_prefill=True, step_budget=5, wall_budget_s=30)
                session.entry_observation = browser.observe()
                session.plan()

                result = session.run()

                self.assertEqual(result.terminal_reason, "agent_finished",
                                 [m[-1].get("content", "") for m in chat.messages])
                self.assertIn("source#1", chat.test_source_result)
                self.assertEqual(browser.actions, ["observe", "select", "handoff_form"])
                self.assertNotIn("submit", browser.actions)
                prepared = session.ledger.of_kind("application_form_prepared")
                self.assertEqual(prepared[0]["source_refs"], ["source#1"])

if __name__ == "__main__":
    unittest.main()
