"""Scoped, adapter-issued label bindings and manual submit targets."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
import time

from web_operation_agent.business.forms import (
    ApplicationFormDescriptor, ApplicationFormField, ApplicationFormTools)
from web_operation_agent.business.errors import BusinessToolContractError, BusinessToolRequestError
from web_operation_agent.business.runtime import FormDraft, FormDraftField
from web_operation_agent.execution.form_tools import apply_form_draft, prefill_form
from web_operation_agent.execution.budget import TurnBudget
from web_operation_agent.execution.runstate import RunResult, _Turn
from web_operation_agent.modeling.spend import BudgetExhausted
from web_operation_agent.schemas import ElementRef, PageObservation, ActionReceipt


SCOPE = "dialog:editor"


def observation(epoch=1, *, scope=SCOPE, duplicate=False, named=False):
    fields = [ElementRef("e1", "textbox", "Name", "input", epoch,
               {"field_label": "Name", "form_scope": scope,
                **({"field": "other"} if named else {})}),
              ElementRef("e2", "button", "Confirm", "button", epoch,
                         {"form_scope": scope})]
    # The same label and submit text on a different form must not match.
    fields += [ElementRef("e3", "textbox", "Name", "input", epoch,
                         {"field_label": "Name", "form_scope": "dialog:other"}),
               ElementRef("e4", "button", "Confirm", "button", epoch,
                          {"form_scope": "dialog:other"})]
    if duplicate:
        fields.append(ElementRef("e5", "textbox", "Name", "input", epoch,
                                 {"field_label": "Name", "form_scope": scope}))
    return PageObservation(schema_version=1, epoch=epoch,
        url="https://app.test/records", title="Editor", landmarks=[],
        elements=fields, content="current dialog")


class LabelForms:
    required_scopes = frozenset()

    def describe_form(self, obs, *, context=None):
        return ApplicationFormDescriptor("create-v1", "New record", "/records",
            (ApplicationFormField("name", "Name", binding="label"),),
            scope="current actor; create mode", control_scope=SCOPE, submit_label="Confirm")

    def prepare_form(self, form, values, *, context=None):
        return FormDraft(form.route, tuple(FormDraftField(item.field.reference,
            item.field.label, item.value, item.control, binding=item.field.binding)
            for item in values), control_scope=form.control_scope, submit_label=form.submit_label)


def session_for(execute):
    return SimpleNamespace(
        _form_draft_verification=None,
        _cancel_requested=lambda: False, _browser_attempts=0, page_actions=0,
        _approvals=SimpleNamespace(confirm_operation=lambda *a, **k: ("", ""),
                                   settle_operation=lambda: None),
        _writes=SimpleNamespace(write_outcome=lambda *a, **k: {"state": "not_written"},
                                settle_write=lambda write: ("", "")),
        ledger=SimpleNamespace(append=lambda *a, **k: None),
        browser=SimpleNamespace(execute=execute, execute_with_budget=lambda action, args,
                                **kwargs: execute(action, args)))


class ScopedForms(unittest.TestCase):
    def tools(self, port=None):
        return ApplicationFormTools(port or LabelForms(), application_id="fixture",
                                    binding_secret=b"s" * 32)

    def test_exact_scoped_label_and_submit_are_retained_on_the_server(self):
        tools, obs = self.tools(), observation()
        described = tools.describe(obs, {})
        self.assertEqual(len(described["fields"]), 1)
        draft, prepared = tools.prepare(obs, {"form_id": described["form_id"],
            "values": [{"field_id": described["fields"][0]["field_id"], "value": "Draft"}]})
        self.assertTrue(prepared["draft_ready"])
        self.assertEqual((draft.control_scope, draft.submit_label, draft.fields[0].binding),
                         (SCOPE, "Confirm", "label"))

    def test_explicit_name_conflict_and_duplicate_label_are_not_available(self):
        for obs in (observation(named=True), observation(duplicate=True),
                    observation(scope="dialog:replaced")):
            with self.subTest(obs=obs.content_digest):
                self.assertEqual(self.tools().describe(obs, {})["fields"], [])

    def test_label_bindings_and_submit_target_require_a_scope(self):
        for kwargs in ({"submit_label": "Confirm"},
                       {"fields": (FormDraftField("name", "Name", "Draft", binding="label"),)}):
            options = {"route": "/records", "fields": (FormDraftField("name", "Name", "Draft"),)}
            options.update(kwargs)
            with self.assertRaisesRegex(ValueError, "require.*scope"):
                FormDraft(**options)

    def test_pending_or_blocked_form_is_not_described(self):
        for state in ("pending", "blocked"):
            obs = observation()
            obs.render_state = state
            with self.assertRaisesRegex(BusinessToolRequestError, "not ready"):
                self.tools().describe(obs, {})

    def test_adapter_cannot_substitute_labels_or_scopes_after_description(self):
        class Corrupt(LabelForms):
            def prepare_form(self, form, values, *, context=None):
                draft = super().prepare_form(form, values, context=context)
                return replace(draft, submit_label="Delete")
        tools, obs = self.tools(Corrupt()), observation()
        described = tools.describe(obs, {})
        with self.assertRaisesRegex(BusinessToolContractError, "exceeds"):
            tools.prepare(obs, {"form_id": described["form_id"], "values": [{
                "field_id": described["fields"][0]["field_id"], "value": "Draft"}]})

    def test_applier_only_fills_then_hands_over_the_exact_submit_target(self):
        calls = []
        def execute(action, args):
            calls.append((action, args))
            return ActionReceipt(schema_version=1, command_id="cmd-" + str(len(calls)),
                                 action=action, args_echo={}, ok=True,
                                 observation=observation(len(calls) + 1))
        session = session_for(execute)
        draft = LabelForms().prepare_form(LabelForms().describe_form(observation()),
            (SimpleNamespace(field=ApplicationFormField("name", "Name", binding="label"),
                             value="Draft", control="textbox"),))
        _, reason, _ = apply_form_draft(session, draft, observation(), 1)
        self.assertEqual(reason, "")
        self.assertEqual([action for action, _ in calls], ["fill", "handoff_form"])
        self.assertEqual(calls[-1][1]["submit"], {"ref": "e2", "label": "Confirm", "scope": SCOPE})
        self.assertEqual(calls[-1][1]["fields"][0]["label"], "Name")

    def test_loading_after_input_is_bounded_and_never_retypes_the_field(self):
        draft = FormDraft("/records", (FormDraftField("name", "Name", "Draft", binding="label"),),
                          control_scope=SCOPE, submit_label="Confirm")
        for remains_pending in (False, True):
            with self.subTest(remains_pending=remains_pending):
                calls = []
                def execute(action, args):
                    calls.append(action)
                    obs = observation(len(calls) + 1)
                    obs.render_state = "pending" if action == "fill" or remains_pending else "ready"
                    return ActionReceipt(schema_version=1, command_id=str(len(calls)), action=action,
                                         args_echo={}, ok=True, observation=obs)
                _, reason, _ = apply_form_draft(session_for(execute), draft, observation(), 1)
                self.assertEqual(calls.count("fill"), 1)
                self.assertEqual(calls.count("wait_for"), 3 if remains_pending else 1)
                self.assertEqual(reason, "agent_refused" if remains_pending else "")
                self.assertEqual("handoff_form" in calls, not remains_pending)

    def test_shared_budget_stops_after_approval_input_wait_and_late_handoff(self):
        draft = FormDraft("/records", (FormDraftField("name", "Name", "Draft", binding="label"),),
                          control_scope=SCOPE, submit_label="Confirm")
        for stop_at in ("approval", "fill", "wait_for", "handoff_form"):
            with self.subTest(stop_at=stop_at):
                clock, calls, events = [0.0], [], []
                def execute(action, args):
                    calls.append(action)
                    if action == stop_at:
                        clock[0] = 2
                    obs = observation(len(calls) + 1)
                    if stop_at == "wait_for" and action == "fill":
                        obs.render_state = "pending"
                    return ActionReceipt(schema_version=1, command_id=str(len(calls)),
                        action=action, args_echo={}, ok=True, observation=obs)
                session = session_for(execute)
                session.ledger.append = lambda event, **kwargs: events.append((event, kwargs))
                if stop_at == "approval":
                    def approve(*args, **kwargs):
                        clock[0] = 2
                        return "", ""
                    session._approvals.confirm_operation = approve
                budget = TurnBudget(steps=4, seconds=1, clock=lambda: clock[0])
                obs, reason, _ = apply_form_draft(session, draft, observation(), 1, budget=budget)
                self.assertEqual(reason, "wall_clock_budget")
                expected = {"approval": [], "fill": ["fill"],
                            "wait_for": ["fill", "wait_for"],
                            "handoff_form": ["fill", "handoff_form"]}
                self.assertEqual(calls, expected[stop_at])
                self.assertEqual(obs.epoch, len(calls) + 1)
                self.assertEqual([e[1]["tool"] for e in events if e[0] == "form_prefill_action"], calls)
                self.assertEqual(budget.projection()["model_requests_used"], 0)

    def test_cancellation_after_approval_dispatches_nothing(self):
        calls, cancelled = [], [False]
        session = session_for(lambda *args: calls.append(args))
        def approve(*args, **kwargs):
            cancelled[0] = True
            return "", ""
        session._approvals.confirm_operation = approve
        budget = TurnBudget(steps=4, seconds=10, cancelled=lambda: cancelled[0])
        draft = FormDraft("/records", (FormDraftField("name", "Name", "Draft", binding="label"),),
                          control_scope=SCOPE)
        _, reason, _ = apply_form_draft(session, draft, observation(), 1, budget=budget)
        self.assertEqual(reason, "user_cancelled")
        self.assertEqual(calls, [])

    def test_shared_token_exhaustion_prevents_the_form_substage(self):
        calls = []
        budget = TurnBudget(steps=4, seconds=10)
        budget.fail(BudgetExhausted("token_budget", "synthetic limit"))
        draft = FormDraft("/records", (FormDraftField("name", "Name", "Draft", binding="label"),),
                          control_scope=SCOPE)
        _, reason, _ = apply_form_draft(session_for(lambda *a: calls.append(a)), draft,
                                        observation(), 1, budget=budget)
        self.assertEqual(reason, "token_budget")
        self.assertEqual(calls, [])

    def test_product_prefill_inherits_elapsed_turn_time_and_consumes_the_draft(self):
        session = session_for(lambda *a: self.fail("expired draft must not dispatch"))
        session._form_draft = FormDraft("/records", (FormDraftField(
            "name", "Name", "Draft", binding="label"),), control_scope=SCOPE)
        session.wall_budget_s = 1
        session._approvals.waited_ms = 0
        session.task = SimpleNamespace(prompt="填写表单")
        def finish(result, started, reason, detail, **kwargs):
            result.terminal_reason = reason
            return result
        session._finish = finish
        result = RunResult("run", "task", "U", "")
        turn = _Turn(result, time.time() - 2, [], observation(), step=1)
        outcome = prefill_form(session, turn, {}, "prefill_form", {})
        self.assertEqual(outcome.finished.terminal_reason, "wall_clock_budget")
        self.assertIsNone(session._form_draft)
        self.assertIn("助手没有执行提交", result.agent_said)

    def prepared(self, port=None):
        tools = self.tools(port)
        described = tools.describe(observation(), {})
        draft, _ = tools.prepare(observation(), {"form_id": described["form_id"],
            "values": [{"field_id": described["fields"][0]["field_id"], "value": "Draft"}]})
        return draft, tools.take_verification(draft)

    def test_verification_is_consumed_and_never_serialized_with_values(self):
        tools = self.tools()
        described = tools.describe(observation(), {})
        draft, _ = tools.prepare(observation(), {"form_id": described["form_id"],
            "values": [{"field_id": described["fields"][0]["field_id"], "value": "Draft"}]})
        check = tools.take_verification(draft)
        with self.assertRaises(BusinessToolContractError):
            tools.take_verification(draft)
        obs = observation(2)
        obs.elements[0].attrs["value"] = "Draft"
        check.check(draft, obs)  # Entered values and epochs may change.
        self.assertNotIn("verification", draft.to_json())
        with self.assertRaises(BusinessToolRequestError):
            check.check(replace(draft, fields=(replace(draft.fields[0], value="Other"),)), obs)

    def test_server_current_form_binding_rejects_wrong_entry_fields_values_and_persistence(self):
        for reference, values in (("other", (("name", "Draft"),)),
                                  ("create-v1", (("absent", "Draft"),)),
                                  ("create-v1", (("name", "Draft"), ("name", "Other"))),
                                  ("create-v1", (("name", "x"*4001),))):
            with self.subTest(reference=reference, fields=len(values)), self.assertRaises(BusinessToolRequestError):
                self.tools().prepare_current(observation(), reference=reference, values=values)
        class Saving(LabelForms):
            def describe_form(self, obs, *, context=None):
                return replace(super().describe_form(obs), persistence="application_saves_on_leave",
                               effect="Change name", targets=("Record",), submit_label="")
        with self.assertRaises(BusinessToolRequestError):
            self.tools(Saving()).prepare_current(observation(), reference="create-v1", values=(("name", "Draft"),))
        missing_button = observation()
        missing_button.elements = [e for e in missing_button.elements if e.ref != "e2"]
        with self.assertRaises(BusinessToolRequestError):
            self.tools().prepare_current(missing_button, reference="create-v1", values=(("name", "Draft"),))

    def test_same_url_entry_change_during_approval_or_after_input_stops_the_flow(self):
        class Changing(LabelForms):
            revision = 1
            def describe_form(self, obs, *, context=None):
                return replace(super().describe_form(obs), reference=f"entry-{self.revision}")
        for change_at in ("approval", "fill", "handoff_form"):
            with self.subTest(change_at=change_at):
                port, calls, events = Changing(), [], []
                draft, check = self.prepared(port)
                def execute(action, args):
                    calls.append(action)
                    if action == change_at:
                        port.revision += 1
                    return ActionReceipt(schema_version=1, command_id=str(len(calls)),
                        action=action, args_echo={}, ok=True, observation=observation(len(calls)+1))
                session = session_for(execute)
                session.ledger.append = lambda event, **kwargs: events.append((event, kwargs))
                if change_at == "approval":
                    def approve(*a, **kw):
                        port.revision += 1
                        return "", ""
                    session._approvals.confirm_operation = approve
                _, reason, _ = apply_form_draft(session, draft, observation(), 1,
                    verify=lambda obs: check.check(draft, obs))
                self.assertEqual(reason, "agent_refused")
                expected = {"approval": [], "fill": ["fill"], "handoff_form": ["fill", "handoff_form"]}
                self.assertEqual(calls, expected[change_at])
                self.assertTrue(any(event == "form_entry_verification_refused" for event, _ in events))

    def test_loading_only_waits_and_revalidates_after_ready_without_retyping(self):
        draft, check = self.prepared()
        calls, verified_epochs = [], []
        def execute(action, args):
            calls.append(action)
            obs = observation(len(calls)+1)
            obs.render_state = "pending" if action == "fill" else "ready"
            return ActionReceipt(schema_version=1, command_id=str(len(calls)), action=action,
                                 args_echo={}, ok=True, observation=obs)
        def verify(obs):
            self.assertNotEqual(obs.render_state, "pending")
            verified_epochs.append(obs.epoch)
            check.check(draft, obs)
        _, reason, _ = apply_form_draft(session_for(execute), draft, observation(), 1, verify=verify)
        self.assertEqual(reason, "")
        self.assertEqual(calls, ["fill", "wait_for", "handoff_form"])
        self.assertIn(3, verified_epochs)

    def test_permission_loss_is_terminal_and_preserves_the_typed_audit_code(self):
        from web_operation_agent.business.errors import BusinessToolPermissionError
        draft, _ = self.prepared()
        calls, events = [], []
        session = session_for(lambda *args: calls.append(args))
        session.ledger.append = lambda event, **kw: events.append((event, kw))
        def refuse(obs):
            raise BusinessToolPermissionError("synthetic permission removed")
        _, reason, _ = apply_form_draft(session, draft, observation(), 1, verify=refuse)
        self.assertEqual((reason, calls), ("permission_denied", []))
        self.assertEqual(events[-1][1]["error_code"], "business_permission_denied")

    def test_malformed_verification_verdict_never_dispatches(self):
        draft, _ = self.prepared()
        for verdict in (False, True, {}, "yes"):
            with self.subTest(verdict=verdict):
                calls = []
                _, reason, _ = apply_form_draft(session_for(lambda *a: calls.append(a)),
                    draft, observation(), 1, verify=lambda obs: verdict)
                self.assertEqual((reason, calls), ("agent_refused", []))


if __name__ == "__main__":
    unittest.main()
