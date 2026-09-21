from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent.composition.ports import DeclaredRoutePort
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
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
            self.assertEqual(browser.actions, ["fill"])
            self.assertNotIn("prefill_form", chat.tool_names[0])
            self.assertIn("describe_form", chat.tool_names[0])
            self.assertIn("prepare_form", chat.tool_names[0])
            self.assertIn("prefill_form", chat.tool_names[2])
            self.assertEqual(ledger.of_kind("business_skill_loaded"), [])
            self.assertEqual(len(ledger.of_kind("application_form_prepared")), 1)
            self.assertEqual(len(ledger.of_kind("form_prefill_completed")), 1)
            self.assertNotIn("submit", browser.actions)

if __name__ == "__main__":
    unittest.main()
