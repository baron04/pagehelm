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
from web_operation_agent.business.errors import (BusinessToolIdentityError,
                                                BusinessToolPermissionError, BusinessToolRequestError)
from web_operation_agent.business.forms import ApplicationFormEntry
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnStatus
from web_operation_agent.goal import executor as module
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.goal.forms import combine_forms
from web_operation_agent.goal.ports import FormPlan, PagePlan, Resolution, Resolved
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.tests.execution.product_support import _Channel
from web_operation_agent.tests.support.business import _Chat


class FormBrowser:
    instances = []
    fail = ""
    submit_after_scroll = 0
    submit_requires_values = False

    def __init__(self, channel, **kwargs):
        self.url = channel.latest_observation_envelope.observation.url
        self.epoch, self.values, self.actions = 1, {"name": "", "email": ""}, []
        self.submit_states = []
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
        submit_disabled = self.submit_requires_values and not all(self.values.values())
        self.submit_states.append((action, submit_disabled))
        elements = [ElementRef("e1", "textbox", "Name", "input", self.epoch,
                    {"field": "name", "form_scope": scope, "value": self.values["name"]}),
                    ElementRef("e2", "textbox", "Email", "input", self.epoch,
                    {"field": "email", "form_scope": scope, "value": self.values["email"]}),
                    ElementRef("e3", "button", "Save contact", "button", self.epoch,
                    {"form_scope": scope, **({"disabled": "true"} if submit_disabled else {})}),
                    ElementRef("e4", "button", "Prepare contact", "button", self.epoch)]
        if self.actions.count("scroll") < self.submit_after_scroll:
            elements = [e for e in elements if e.ref != "e3"]
        obs = PageObservation(1, self.epoch, self.url, "New contact", [], elements,
                              "synthetic form", render_state="ready")
        return ActionReceipt(1, str(len(self.actions)), action, dict(args), action != self.fail,
                             error_code="navigation_timeout" if action == self.fail else "",
                             observation=obs)


class GoalFormsTests(unittest.TestCase):
    def run_goal(self, *, extras=(), values=None, decision="approved", enabled=True,
                 disabled=frozenset(), fail="", actor_id="ada-lovelace", open_entry=False, batch=False, submit_after_scroll=0, batch_action="", prompt="填写 Name 和 Email，尚不提交", repair_label=False, submit_requires_values=False, preparation_error=None):
        FormBrowser.instances.clear()
        FormBrowser.fail = fail
        FormBrowser.submit_after_scroll = submit_after_scroll
        FormBrowser.submit_requires_values = submit_requires_values
        self.addCleanup(setattr, FormBrowser, "submit_after_scroll", 0)
        self.addCleanup(setattr, FormBrowser, "submit_requires_values", False)
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
            if batch:
                declarations = [{"kind": "handoff", "entity": "contacts", "action": batch_action, "field_values":
                                 [{"field": label, "value": value} for label, value in values]}]
                chat = _Chat([("declare_goal", {"write_intent": True,
                               "deliverables": [*declarations, *extras]})])
            audit = Path(tmp) / "audit.jsonl"
            bound = bind_application(adapter)
            if preparation_error is not None:
                delegate = bound.ports.forms
                class RefusedForms:
                    required_scopes = delegate.required_scopes
                    def describe_form(self, *args, **kwargs):
                        return delegate.describe_form(*args, **kwargs)
                    def prepare_form(self, *args, **kwargs):
                        raise preparation_error
                bound = replace(bound, ports=replace(bound.ports, forms=RefusedForms()))
            goal_port = ContactsGoalPort(adapter, form_prefill=True)
            if repair_label:
                old_label = "Wrong mail" if repair_label == "literal" else "Electronic mail"
                class RepairGoals(ContactsGoalPort):
                    def index(self, **kwargs):
                        index = super().index(**kwargs)
                        return replace(index, entities=tuple(replace(e, fields=tuple(
                            replace(f, aliases=("Electronic mail",)) if f.key == "email" else f
                            for f in e.fields)) for e in index.entities))
                    def resolve(self, spec, **kwargs):
                        if any(label == old_label for d in spec.deliverables
                               for label, _ in d.field_values):
                            return Resolution(deliverables=(Resolved(kind="handoff",
                                problem="the form needs its current field label",
                                repair="Electronic mail is indexed as Email on this form"),))
                        return super().resolve(spec, **kwargs)
                goal_port = RepairGoals(adapter, form_prefill=True)
                final = {"write_intent": True, "deliverables": declarations}
                first = {"write_intent": True, "deliverables": [{**d, "field_values": [
                    {**v, "field": old_label} if v["field"] == "Email" else v
                    for v in d["field_values"]]} for d in declarations]}
                chat = _Chat([("declare_goal", first), ("declare_goal", final)])
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
                                         "text": prompt})
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            return result, FormBrowser.instances[-1], channel, chat, events

    def test_missing_value_asks_before_any_approval_or_page_dispatch(self):
        result, browser, channel, _, events = self.run_goal(batch=True, values=(("Email", ""),),
                                                          prompt="给联系人补个 Email")
        self.assertEqual(result.status, AgentTurnStatus.NEEDS_INPUT)
        self.assertEqual(result.to_json()["clarification"]["response_kind"], "free_text")
        self.assertIn("Email", result.user_answer)
        self.assertNotIn("fill", browser.actions)
        self.assertNotIn("click", browser.actions)
        self.assertFalse(any(e['event'] == 'operation_control_requested' for e in events))

    def test_alias_repair_reaches_verified_form_and_still_requires_approval(self):
        for decision in ("approved", "denied"):
            with self.subTest(decision=decision):
                result, browser, channel, chat, events = self.run_goal(
                    batch=True, repair_label=True, decision=decision)
                self.assertTrue(next(e for e in events if e["event"] == "goal_repaired")["accepted"])
                self.assertEqual(len(chat.messages), 2)
                self.assertEqual(len(channel.approvals), 1)
                self.assertNotIn("submit", browser.actions)
                if decision == "approved":
                    self.assertEqual(result.status, AgentTurnStatus.FINISHED)
                    self.assertEqual(browser.values, {"name": "Ada Byron", "email": "new@example.test"})
                    self.assertEqual(browser.actions.count("handoff_form"), 1)
                else:
                    self.assertEqual(result.status, AgentTurnStatus.CANCELLED)
                    self.assertNotIn("fill", browser.actions)

    def test_unindexed_label_repair_requires_literal_assignments_and_fresh_approval(self):
        for decision in ("approved", "denied"):
            result, browser, channel, _, events = self.run_goal(batch=True, repair_label="literal",
                values=(("Name", "Ada Byron"), ("Email", "new@example.test")),
                decision=decision, prompt="Name to Ada Byron, Email to new@example.test; leave saving to me.")
            self.assertTrue(next(e for e in events if e["event"] == "goal_repaired")["accepted"])
            self.assertEqual(len(channel.approvals), 1)
            self.assertNotIn("submit", browser.actions)
            if decision == "approved":
                self.assertEqual(result.status, AgentTurnStatus.FINISHED)
                self.assertEqual(browser.values, {"name": "Ada Byron", "email": "new@example.test"})
            else:
                self.assertEqual(result.status, AgentTurnStatus.CANCELLED)
                self.assertNotIn("fill", browser.actions)

    def test_explicit_clear_still_obeys_application_required_fields(self):
        result, browser, _, _, _ = self.run_goal(batch=True, values=(("Email", ""),),
                                                prompt="清空 Email")
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertNotIn("fill", browser.actions)
        self.assertNotIn("submit", browser.actions)
        self.assertNotIn("请提供", result.user_answer)

    def test_batch_cannot_ignore_an_unreviewed_business_action(self):
        result, browser, _, _, _ = self.run_goal(batch=True, batch_action="Delete")
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertNotIn("fill", browser.actions)
        self.assertNotIn("handoff_form", browser.actions)

    def test_declined_batch_reports_no_input_but_uncertain_fill_keeps_its_warning(self):
        result, browser, _, _, _ = self.run_goal(batch=True, decision="denied")
        self.assertEqual(result.status, AgentTurnStatus.CANCELLED)
        self.assertNotIn("fill", browser.actions)
        self.assertIn("没有填写或提交", result.user_answer)
        self.assertIn("未获得你对这次操作的同意", result.user_answer)
        self.assertNotIn("部分字段可能", result.user_answer)
        result, browser, _, _, events = self.run_goal(batch=True, fail="fill")
        self.assertEqual(browser.actions.count("fill"), 1)
        self.assertIn("部分字段可能", result.user_answer)
        self.assertTrue(any(e['event']=='goal_form_handoff' and e['input_dispatched'] for e in events))

    def test_typed_preparation_refusals_explain_the_cause_without_exposing_exception_text(self):
        for error_type, copy in ((BusinessToolPermissionError, "没有这项表单操作的权限"),
                                 (BusinessToolIdentityError, "无法核验当前登录身份"),
                                 (BusinessToolRequestError, "未通过应用校验")):
            with self.subTest(error=error_type):
                result, browser, _, _, events = self.run_goal(batch=True,
                    preparation_error=error_type("internal /private/source credential-token"))
                self.assertEqual(result.status, AgentTurnStatus.REFUSED)
                self.assertIn(copy, result.user_answer)
                self.assertIn("没有填写或提交", result.user_answer)
                self.assertNotIn("/private/source", result.user_answer)
                self.assertNotIn("credential-token", result.user_answer)
                self.assertFalse({"fill", "select", "handoff_form"} & set(browser.actions))
                self.assertTrue(any(e["event"] == "goal_form_preparation_refused"
                                    and e["error_code"] == error_type.code for e in events))

    def test_missing_submit_explains_why_and_never_enters_values(self):
        result, browser, _, _, _ = self.run_goal(batch=True, submit_after_scroll=99)
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertIn("没有找到可核验的提交按钮", result.user_answer)
        self.assertFalse({"fill", "handoff_form"} & set(browser.actions))

    def test_unobserved_submit_is_scrolled_before_values_and_still_verified(self):
        result, browser, _, _, _ = self.run_goal(batch=True, submit_after_scroll=1)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(browser.actions.count("scroll"), 1)
        self.assertLess(browser.actions.index("scroll"), browser.actions.index("fill"))
        self.assertEqual(browser.actions.count("handoff_form"), 1)

    def test_contacts_prefill_can_enable_an_initially_disabled_submit(self):
        result, browser, channel, _, events = self.run_goal(batch=True, submit_requires_values=True)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertTrue(browser.submit_states[0][1])
        self.assertEqual(browser.submit_states[-1], ("handoff_form", False))
        self.assertEqual(browser.values, {"name": "Ada Byron", "email": "new@example.test"})
        self.assertEqual(len(channel.approvals), 1)
        self.assertEqual(browser.actions.count("handoff_form"), 1)
        self.assertFalse({"click", "submit"} & set(browser.actions))
        self.assertTrue(any(e['event'] == 'goal_form_handoff' and e['complete'] for e in events))

    def test_contacts_partial_prefill_hands_over_even_when_submit_remains_disabled(self):
        result, browser, channel, _, events = self.run_goal(batch=True,
            values=(("Name", "Ada Byron"),), submit_requires_values=True,
            prompt="把 Name 填成 Ada Byron，剩下我来填")
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(browser.values, {"name": "Ada Byron", "email": ""})
        self.assertEqual(browser.submit_states[-1], ("handoff_form", True))
        self.assertEqual(len(channel.approvals), 1)
        self.assertEqual(browser.actions.count("fill"), 1)
        self.assertEqual(browser.actions.count("handoff_form"), 1)
        self.assertFalse({"click", "submit"} & set(browser.actions))
        self.assertIn("已预填：Name", result.user_answer)
        self.assertIn("完善其余必填内容", result.user_answer)
        self.assertTrue(any(e['event'] == 'goal_form_handoff' and e['complete'] for e in events))

    def test_submit_scroll_is_bounded_and_never_fills_without_observed_submit(self):
        result, browser, _, _, events = self.run_goal(batch=True, submit_after_scroll=4)
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertEqual(browser.actions.count("scroll"), 3)
        self.assertNotIn("fill", browser.actions)
        self.assertNotIn("handoff_form", browser.actions)
        self.assertTrue(any(e["event"] == "goal_form_preparation_refused" for e in events))

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

    def test_one_form_slot_keeps_all_fields_and_three_other_targets(self):
        result, browser, channel, chat, _ = self.run_goal(batch=True, extras=(
            {"kind": "fact", "entity": "contacts", "record": "Grace Hopper", "fields": ["Email"]},
            {"kind": "aggregate", "entity": "contacts", "metric": {"op": "count"}},
            {"kind": "page", "entity": "contacts", "page": {"key": "desk-partner"}}))
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(browser.values, {"name": "Ada Byron", "email": "new@example.test"})
        self.assertEqual(len(chat.messages), 1)
        self.assertEqual(browser.actions.count("handoff_form"), 1)
        self.assertIn("grace@example.test", result.user_answer)

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
                self.assertNotIn("已预填：", result.user_answer)
                if args.get("fail") == "fill":
                    self.assertEqual(browser.actions.count("fill"), 1)
                if not args.get("fail"):
                    self.assertNotIn("handoff_form", browser.actions)
                if args.get("disabled"):
                    self.assertIn("当前部署未启用这项表单操作", result.user_answer)

    def test_distinct_entries_or_excessive_fields_cannot_share_a_handoff(self):
        plan = PagePlan("/records", "Form", form=FormPlan("entry", (("name", "A"),)))
        with self.assertRaises(BusinessToolRequestError):
            combine_forms([plan, replace(plan, form=FormPlan("other", (("email", "B"),)))])
        many = [replace(plan, form=FormPlan("entry", ((f"f{i}", "v"),))) for i in range(21)]
        with self.assertRaises(BusinessToolRequestError):
            combine_forms(many)


if __name__ == "__main__":
    unittest.main()
