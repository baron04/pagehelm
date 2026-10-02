"""Scoped, adapter-issued label bindings and manual submit targets."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from web_operation_agent.business.forms import (
    ApplicationFormDescriptor, ApplicationFormField, ApplicationFormTools)
from web_operation_agent.business.errors import BusinessToolContractError, BusinessToolRequestError
from web_operation_agent.business.runtime import FormDraft, FormDraftField
from web_operation_agent.execution.form_tools import apply_form_draft
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
        _cancel_requested=lambda: False, _browser_attempts=0, page_actions=0,
        _approvals=SimpleNamespace(confirm_operation=lambda *a, **k: ("", ""),
                                   settle_operation=lambda: None),
        _writes=SimpleNamespace(write_outcome=lambda *a, **k: {"state": "not_written"},
                                settle_write=lambda write: ("", "")),
        ledger=SimpleNamespace(append=lambda *a, **k: None),
        browser=SimpleNamespace(execute=execute))


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


if __name__ == "__main__":
    unittest.main()
