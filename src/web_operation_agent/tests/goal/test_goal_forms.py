"""Goal form batches retain the established descriptor, approval and receipt contract."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.app import ACTORS_BY_ID, DEMO_SESSION_COOKIE, MinimalContactsApplication
from testapps.contacts.goal import ContactsGoalPort
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.forms import ApplicationFormEntry
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnStatus
from web_operation_agent.goal import executor as module
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.goal.forms import combine_forms
from web_operation_agent.goal.ports import FormPlan, PagePlan
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.tests.execution.product_support import _Channel
from web_operation_agent.tests.support.business import _Chat


class FormBrowser:
    instances = []
    fail = ""

    def __init__(self, channel, **kwargs):
        self.url = channel.latest_observation_envelope.observation.url
        self.epoch, self.values, self.actions = 1, {"name": "", "email": ""}, []
        self.instances.append(self)

    def start(self):
        return {"ok": True}

    def execute_with_budget(self, action, args, **kwargs):
        self.actions.append(action)
        if action == "goto":
            from urllib.parse import urlsplit
            parsed = urlsplit(self.url)
            self.url = parsed.scheme + "://" + parsed.netloc + args["url"]
        if action == "fill":
            self.values["name" if args["ref"] == "e1" else "email"] = args["value"]
        self.epoch += 1
        scope = "form:contact-create"
        elements = [ElementRef("e1", "textbox", "Name", "input", self.epoch,
                    {"field": "name", "form_scope": scope, "value": self.values["name"]}),
                    ElementRef("e2", "textbox", "Email", "input", self.epoch,
                    {"field": "email", "form_scope": scope, "value": self.values["email"]}),
                    ElementRef("e3", "button", "Save contact", "button", self.epoch,
                    {"form_scope": scope}),
                    ElementRef("e4", "button", "Prepare contact", "button", self.epoch)]
        obs = PageObservation(1, self.epoch, self.url, "New contact", [], elements,
                              "synthetic form", render_state="ready")
        return ActionReceipt(1, str(len(self.actions)), action, dict(args), action != self.fail,
                             error_code="navigation_timeout" if action == self.fail else "",
                             observation=obs)


class GoalFormsTests(unittest.TestCase):
    def run_goal(self, *, extras=(), values=None, decision="approved", enabled=True,
                 disabled=frozenset(), fail="", actor_id="ada-lovelace", open_entry=False):
        FormBrowser.instances.clear()
        FormBrowser.fail = fail
        self.addCleanup(setattr, FormBrowser, "fail", "")
        with MinimalContactsApplication() as app, tempfile.TemporaryDirectory() as tmp:
            actor = ACTORS_BY_ID[actor_id]
            adapter = MinimalContactsAdapter(app.origin, api_token=actor.token)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={actor.session}"), delegation_id="goal-form")
            channel = _Channel(page_url=app.origin + "/contacts/new", principal=principal,
                               decision=decision)
            values = values or (("Name", "  Ada   Byron  "), ("Email", "NEW@example.test"))
            declarations = [{"kind": "handoff", "entity": "contacts", "action": field,
                             "value": value} for field, value in values]
            chat = _Chat([("declare_goal", {"write_intent": True,
                           "deliverables": [*declarations, *extras]})])
            audit = Path(tmp) / "audit.jsonl"
            bound = bind_application(adapter)
            goal_port = ContactsGoalPort(adapter, form_prefill=True)
            if open_entry:
                delegate = bound.ports.forms
                class EntryPort:
                    required_scopes = delegate.required_scopes
                    opened = False
                    def describe_form(self, *args, **kwargs):
                        return delegate.describe_form(*args, **kwargs) if self.opened else None
                    def prepare_form(self, *args, **kwargs):
                        return delegate.prepare_form(*args, **kwargs)
                    def plan_entry(self, reference, obs, *, context):
                        return ApplicationFormEntry(reference, next(e for e in obs.elements if e.name == "Prepare contact"))
                    def accept_entry(self, entry, before, receipt, obs, *, context):
                        if (not receipt.ok or receipt.action != "click"
                                or receipt.args_echo != {"ref": entry.control.ref, "epoch": before.epoch}):
                            raise BusinessToolRequestError("the contact entry receipt failed")
                        self.opened = True
                        return "contacts-create-v1"
                    def discard_entry(self, entry, *, context):
                        return None
                class EntryGoals(ContactsGoalPort):
                    def resolve(self, *args, **kwargs):
                        result = super().resolve(*args, **kwargs)
                        return replace(result, deliverables=tuple(replace(r, page=replace(r.page,
                            form=replace(r.page.form, reference="contact-entry", open_entry=True)))
                            if r.page and r.page.form else r for r in result.deliverables))
                bound = replace(bound, ports=replace(bound.ports, forms=EntryPort()))
                goal_port = EntryGoals(adapter, form_prefill=True)
            runner = GoalTurnExecutor(channel=channel,
                config=AgentTurnConfig(supported_scope="read and prepare", require_identity=True,
                    allow_form_prefill=enabled, ledger_path=audit), chat=chat,
                application=bound, goal_port=goal_port,
                control=ReadOnlyControl(), run_id_factory=lambda _: "goal-form", readings=1,
                staged=False, disabled_page_actions=disabled)
            with patch.object(module, "RemoteBrowserClient", FormBrowser):
                result = runner.execute({"message_id": "form", "page_url": app.origin + "/contacts/new",
                                         "text": "填写 Name 和 Email，尚不提交"})
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            return result, FormBrowser.instances[-1], channel, chat, events

    def test_two_fields_are_normalized_prepared_once_and_handed_over_once(self):
        result, browser, channel, chat, events = self.run_goal()
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(browser.values, {"name": "Ada Byron", "email": "new@example.test"})
        self.assertEqual([a for a in browser.actions if a not in {"observe", "wait_for"}],
                         ["fill", "fill", "handoff_form"])
        self.assertEqual(len(chat.messages), 1)
        self.assertEqual(len(channel.approvals), 1)
        self.assertEqual(result.user_answer.count("尚未提交"), 1)
        self.assertIn("Save contact", result.user_answer)
        self.assertTrue(next(e for e in events if e["event"] == "goal_form_handoff")["complete"])

    def test_mixed_fact_is_retained_and_page_work_precedes_final_handoff(self):
        result, browser, *_ = self.run_goal(extras=(
            {"kind": "fact", "entity": "contacts", "record": "Grace Hopper", "fields": ["Email"]},
            {"kind": "page", "entity": "contacts", "page": {"key": "desk-partner"}}))
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("grace@example.test", result.user_answer)
        self.assertIn("Partner desk", result.user_answer)
        self.assertEqual(browser.actions[-1], "handoff_form")
        self.assertEqual(browser.actions.count("handoff_form"), 1)

    def test_named_rest_form_entry_consumes_one_exact_click_before_batch(self):
        result, browser, channel, chat, _ = self.run_goal(open_entry=True)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertEqual([a for a in browser.actions if a not in {"observe", "wait_for"}],
                         ["click", "fill", "fill", "handoff_form"])
        self.assertEqual(len(chat.messages), 1)
        self.assertEqual(len(channel.approvals), 1)
        for args in ({"fail":"click"}, {"disabled":frozenset({"click"})}, {"decision":"denied"}):
            with self.subTest(args=args):
                result, browser, *_ = self.run_goal(open_entry=True, **args)
                self.assertNotEqual(result.status, AgentTurnStatus.FINISHED)
                self.assertFalse({"fill", "handoff_form"} & set(browser.actions))
                self.assertLessEqual(browser.actions.count("click"), 1)

    def test_conflicting_field_values_refuse_the_entire_batch_before_input(self):
        result, browser, *_ = self.run_goal(values=(("Name", "A"), ("Name", "B")))
        self.assertNotEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertFalse({"fill", "handoff_form"} & set(browser.actions))

    def test_reader_cannot_prepare_a_writers_form(self):
        result, browser, channel, *_ = self.run_goal(actor_id="rosalind-franklin")
        self.assertNotEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertFalse({"fill", "handoff_form"} & set(browser.actions))
        self.assertEqual(channel.approvals, [])

    def test_disabled_or_denied_or_uncertain_entry_does_not_claim_a_completed_form(self):
        for args in ({"enabled": False}, {"disabled": frozenset({"fill"})},
                     {"disabled": frozenset({"handoff_form"})}, {"decision": "denied"},
                     {"fail": "fill"}, {"fail": "handoff_form"}):
            with self.subTest(args=args):
                result, browser, *_ = self.run_goal(**args)
                self.assertNotEqual(result.status, AgentTurnStatus.FINISHED)
                self.assertNotIn("已填写：", result.user_answer)
                if args.get("fail") == "fill":
                    self.assertEqual(browser.actions.count("fill"), 1)
                if not args.get("fail"):
                    self.assertNotIn("handoff_form", browser.actions)

    def test_distinct_entries_or_excessive_fields_cannot_share_a_handoff(self):
        plan = PagePlan("/records", "Form", form=FormPlan("entry", (("name", "A"),)))
        with self.assertRaises(BusinessToolRequestError):
            combine_forms([plan, replace(plan, form=FormPlan("other", (("email", "B"),)))])
        many = [replace(plan, form=FormPlan("entry", ((f"f{i}", "v"),))) for i in range(21)]
        with self.assertRaises(BusinessToolRequestError):
            combine_forms(many)


if __name__ == "__main__":
    unittest.main()
