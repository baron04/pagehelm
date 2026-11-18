"""Source scalars require a current server attestation; none authorize a write."""
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.forms import ApplicationFormTools
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent.state.identity import PrincipalContext, OperationContext
from web_operation_agent_yudao.writes.forms import NativeFormState, YudaoFormPort
from web_operation_agent_yudao.rules.repository import repository_tree
from web_operation_agent_yudao.writes.source_forms import SourceFormReader
from web_operation_agent_yudao.host.application import YudaoApplication
from .test_source_forms import _SOURCE, _PATH
from .test_goal import DOCS, _Client


class Verifier:
    def __init__(self, state):
        self.state = state
        self.allowed = True

    def current(self, observation, *, context):
        return self.state if self.allowed else None

    def validate(self, state, *, context):
        return self.allowed and state == self.state


class FormsTests(unittest.TestCase):
    def test_radio_choice_is_bound_to_verified_mode_and_exact_declared_value(self):
        self.path.write_text(_SOURCE.replace('<el-input v-model="formData.name" maxlength="31" clearable />',
            '<el-radio-group v-model="formData.name"><el-radio :value="false">私有</el-radio><el-radio :value="true">公开</el-radio></el-radio-group>')
            .replace('label="合成名称"', 'v-if="formType === \'create\'" label="合成名称"'))
        self.verifier.state = replace(self.state, source_digest=self.reader.read(_PATH).binding_digest)
        self.obs.elements[0] = replace(self.obs.elements[0], role='radiogroup', tag='div')
        self.assertNotIn('合成名称', [f['label'] for f in self.describe()['fields']])
        self.verifier.state = replace(self.verifier.state, mode='create', record_id='')
        result = self.describe()
        draft, _ = self.tools.prepare(self.obs, self.args(result, 'true'), context=self.context)
        self.assertEqual((draft.fields[0].control, draft.fields[0].value, draft.fields[0].option_label),
                         ('radiogroup', 'true', '公开'))
        self.path.write_text(self.path.read_text().replace("formType === 'create'", 'canEdit'))
        self.verifier.state = replace(self.verifier.state, source_digest=self.reader.read(_PATH).binding_digest)
        self.assertNotIn('合成名称', [f['label'] for f in self.describe()['fields']])

    def test_literal_choices_use_opaque_field_ids_and_reject_source_replacement(self):
        self.path.write_text(_SOURCE.replace('<el-input v-model="formData.name" maxlength="31" clearable />',
            '<el-select v-model="formData.name"><el-option label="合成负责人" value="17"/></el-select>'))
        self.verifier.state = replace(self.state, source_digest=self.reader.read(_PATH).binding_digest)
        self.obs.elements[0] = replace(self.obs.elements[0], role="combobox")
        result = self.describe()
        self.assertEqual(result['fields'][0]['choices'], [{'value':'17','label':'合成负责人'}])
        draft, _ = self.tools.prepare(self.obs, self.args(result, '17'), context=self.context)
        self.assertEqual((draft.fields[0].value, draft.fields[0].option_label), ('17','合成负责人'))
        result = self.describe()
        self.path.write_text(self.path.read_text().replace('value="17"','value="18"'))
        with self.assertRaises(BusinessToolRequestError):
            self.tools.prepare(self.obs, self.args(result, '17'), context=self.context)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / _PATH
        self.path.parent.mkdir(parents=True)
        self.path.write_text(_SOURCE)
        self.tree = repository_tree(self.root, "synthetic-form-release")
        self.reader = SourceFormReader(self.tree)
        now = int(time.time() * 1000)
        self.principal = PrincipalContext(1, "yudao", "1", "10", "2", "delegate", now-100, now+60000, ("yudao.read",))
        self.context = OperationContext.create(self.principal, session_id="session", turn_id="turn",
                                               operation_id="describe", deadline_ms=now+30000)
        self.state = NativeFormState(self.principal.actor_authority_digest, "session", "turn", "entry-1",
            "update", "12", "/records", "dialog:editor", _PATH,
            self.reader.read(_PATH).binding_digest, "合成编辑", "确定")
        self.verifier = Verifier(self.state)
        self.port = YudaoFormPort(self.reader, self.verifier)
        self.tools = ApplicationFormTools(self.port, application_id="yudao")
        self.obs = PageObservation(1, 1, "https://synthetic.test/records", "Editor", [], [
            ElementRef("e1", "textbox", "合成名称", "input", 1,
                       {"field_label": "合成名称", "form_scope": "dialog:editor"}),
            ElementRef("e2", "textbox", "合成描述", "textarea", 1,
                       {"field_label": "合成描述", "form_scope": "dialog:editor"}),
            ElementRef("e3", "button", "确定", "button", 1, {"form_scope": "dialog:editor"})],
            "fixture", render_state="ready")

    def describe(self):
        return self.tools.describe(self.obs, {}, context=self.context)

    def args(self, result, value="合成内容"):
        return {"form_id": result["form_id"], "values": [{
            "field_id": next(f["field_id"] for f in result["fields"] if f["label"] == "合成名称"),
            "value": value}]}

    def test_scalar_facts_use_the_existing_opaque_contract_without_a_site_catalog(self):
        with patch.object(self.tree, "files", side_effect=AssertionError("no eager catalog")):
            result = self.describe()
            draft, reply = self.tools.prepare(self.obs, self.args(result), context=self.context)
        self.assertEqual({f["label"] for f in result["fields"]}, {"合成名称", "合成描述"})
        self.assertEqual(result["scope"], "update; record 12")
        self.assertNotIn(_PATH, str(result))
        self.assertEqual((draft.fields[0].field, draft.fields[0].value), ("name", "合成内容"))
        self.assertEqual((draft.control_scope, draft.submit_label), ("dialog:editor", "确定"))
        self.assertEqual(reply["submission"], "user_only")

    def numeric_date_form(self):
        self.path.write_text(_SOURCE.replace('<el-input v-model="formData.name" maxlength="31" clearable />',
            '<el-input-number v-model="formData.name" :min="0.01" :max="1000" :precision="2" />')
            .replace('<el-input type="textarea" v-model="formData.description" maxlength="500" />',
                     '<el-date-picker v-model="formData.description" type="date" value-format="x" />'))
        self.verifier.state = replace(self.state, source_digest=self.reader.read(_PATH).binding_digest)
        self.obs.elements[0] = replace(self.obs.elements[0], role="spinbutton")

    def test_numeric_and_date_drafts_validate_native_constraints_without_rounding(self):
        self.numeric_date_form()
        result = self.describe()
        fields = {f["label"]: f for f in result["fields"]}
        self.assertEqual(fields["合成名称"]["type"], "number")
        self.assertEqual(fields["合成描述"]["type"], "date")
        draft, reply = self.tools.prepare(self.obs, {"form_id": result["form_id"], "values": [
            {"field_id": fields["合成名称"]["field_id"], "value": "12.5"},
            {"field_id": fields["合成描述"]["field_id"], "value": "2026-10-05"},
        ]}, context=self.context)
        self.assertEqual([(f.control, f.value) for f in draft.fields],
                         [("spinbutton", "12.50"), ("textbox", "2026-10-05")])
        self.assertEqual(reply["submission"], "user_only")

    def test_invalid_numeric_values_and_calendar_days_are_refused_before_typing(self):
        self.numeric_date_form()
        for label, value in ([("合成名称", v) for v in
                             ("0", "1000.01", "12.501", "NaN", "1e100", "9007199254740992")]
        + [("合成描述", "2026-02-30")]):
            with self.subTest(value=value):
                result = self.describe()
                field = next(f for f in result["fields"] if f["label"] == label)
                with self.assertRaises(BusinessToolRequestError):
                    self.tools.prepare(self.obs, {"form_id": result["form_id"], "values": [
                        {"field_id": field["field_id"], "value": value}]}, context=self.context)

    def datetime_form(self):
        self.path.write_text(_SOURCE.replace('<el-input v-model="formData.name" maxlength="31" clearable />',
            '<el-date-picker v-model="formData.name" type="datetime" value-format="x" />'))
        self.verifier.state = replace(self.state, source_digest=self.reader.read(_PATH).binding_digest)
        self.obs.elements[0] = replace(self.obs.elements[0], role="combobox")

    def test_datetime_draft_uses_local_display_without_epoch_or_precision_loss(self):
        self.datetime_form()
        for value, expected in (("2026-10-08T09:30", "2026-10-08 09:30:00"),
                                ("2026-10-08 18:45:27", "2026-10-08 18:45:27"),
                                ("2026-10-08T09:30:00.000", "2026-10-08 09:30:00")):
            with self.subTest(value=value):
                result = self.describe()
                self.assertEqual(result["fields"][0]["type"], "datetime")
                draft, reply = self.tools.prepare(self.obs, self.args(result, value), context=self.context)
                self.assertEqual((draft.fields[0].control, draft.fields[0].value), ("combobox", expected))
                self.assertEqual(reply["submission"], "user_only")
                check = self.tools.take_verification(draft)
                after = replace(self.obs, epoch=2, elements=[replace(e, epoch=2) for e in self.obs.elements])
                check.check(draft, after, context=self.context)

    def test_datetime_requires_explicit_naive_time_and_rejects_precision_loss(self):
        self.datetime_form()
        for value in ("2026-10-08", "2026-10-08T09:30Z", "2026-10-08T09:30+08:00",
                      "2026-10-08T09:30:00.001", "2026-02-30T09:30", "2026-10-08T24:00",
                      "1791413400000", "2026-10-08T09:30/2026-10-09T18:00"):
            with self.subTest(value=value), self.assertRaises(BusinessToolRequestError):
                self.tools.prepare(self.obs, self.args(self.describe(), value), context=self.context)

    def test_datetime_readonly_and_ambiguous_live_controls_never_issue_a_draft(self):
        self.datetime_form()
        original = self.obs.elements[0]
        for controls in ([replace(original, attrs={**original.attrs, "readonly": "true"})],
                         [original, replace(original, ref="other")]):
            with self.subTest(controls=controls):
                self.obs.elements = [*controls, *self.obs.elements[1:]]
                self.assertNotIn("合成名称", [f["label"] for f in self.describe()["fields"]])
                self.obs.elements = [original, *self.obs.elements[len(controls):]]

    def test_no_live_verifier_or_wrong_actor_session_turn_route_has_no_form(self):
        self.verifier.allowed = False
        self.assertFalse(self.describe()["form_available"])
        self.verifier.allowed = True
        for changes in ({"actor_digest": "a"*64}, {"session_id": "other"}, {"turn_id": "other"},
                        {"route": "/other"}, {"source_digest": "b"*64}):
            with self.subTest(changes=changes):
                self.verifier.state = replace(self.state, **changes)
                self.assertFalse(self.describe()["form_available"])

    def test_unknown_pending_and_blocked_pages_never_offer_a_form(self):
        for state in ("unknown", "pending", "blocked"):
            self.obs.render_state = state
            with self.subTest(state=state):
                if state == "unknown":
                    self.assertFalse(self.describe()["form_available"])
                else:
                    with self.assertRaises(BusinessToolRequestError):
                        self.describe()

    def test_malformed_verifier_acknowledgement_does_not_admit_a_form(self):
        for acknowledgement in (None, 1, "yes", {"allowed": True}):
            with self.subTest(acknowledgement=acknowledgement):
                self.verifier.validate = lambda *a, **kw: acknowledgement
                self.assertFalse(self.describe()["form_available"])

    def test_same_url_mode_record_or_reopened_entry_invalidates_opaque_ids(self):
        for changes in ({"entry_id": "entry-2"}, {"record_id": "13"},
                        {"mode": "create", "record_id": ""}, {"control_scope": "dialog:other"}):
            with self.subTest(changes=changes):
                self.verifier.state = self.state
                result = self.describe()
                self.verifier.state = replace(self.state, **changes)
                with self.assertRaises(BusinessToolRequestError):
                    self.tools.prepare(self.obs, self.args(result), context=self.context)

    def test_prepared_verification_rechecks_mode_record_and_entry_after_typing(self):
        for changes in ({"entry_id": "entry-2"}, {"record_id": "13"},
                        {"mode": "create", "record_id": ""}):
            with self.subTest(changes=changes):
                self.verifier.state = self.state
                draft, _ = self.tools.prepare(self.obs, self.args(self.describe()), context=self.context)
                check = self.tools.take_verification(draft)
                after = replace(self.obs, epoch=2,
                                elements=[replace(e, epoch=2) for e in self.obs.elements])
                check.check(draft, after, context=self.context)
                self.verifier.state = replace(self.state, **changes)
                with self.assertRaises(BusinessToolRequestError):
                    check.check(draft, after, context=self.context)

    def test_prepared_verification_rejects_a_different_actor_session_or_turn(self):
        from web_operation_agent.business.errors import BusinessToolIdentityError
        draft, _ = self.tools.prepare(self.obs, self.args(self.describe()), context=self.context)
        check = self.tools.take_verification(draft)
        for context in (None, replace(self.context, session_id="other"),
                        replace(self.context, turn_id="other"),
                        replace(self.context, principal=replace(self.principal, actor_id="other"))):
            with self.subTest(context_none=context is None), self.assertRaises(BusinessToolIdentityError):
                check.check(draft, self.obs, context=context)

    def test_permission_loss_or_entry_switch_during_prepare_is_not_a_draft(self):
        result = self.describe()
        original = self.verifier.validate
        calls = [0]
        def validate(state, *, context):
            calls[0] += 1
            return original(state, context=context) and calls[0] == 1
        self.verifier.validate = validate
        with self.assertRaises(BusinessToolRequestError):
            self.tools.prepare(self.obs, self.args(result), context=self.context)

    def test_current_source_replacement_or_deletion_cannot_reuse_reviewed_admission(self):
        result = self.describe()
        self.path.write_text(_SOURCE.replace('maxlength="31"', 'maxlength="32"'))
        with self.assertRaises(BusinessToolRequestError):
            self.tools.prepare(self.obs, self.args(result), context=self.context)
        self.path.unlink()
        self.assertFalse(self.describe()["form_available"])

    def test_native_maxlength_counts_utf16_units_and_refuses_before_typing(self):
        for value in ("x"*32, "😀"*16, "\ud800"):
            with self.subTest(value_length=len(value)), self.assertRaises(BusinessToolRequestError):
                self.tools.prepare(self.obs, self.args(self.describe(), value), context=self.context)
        draft, _ = self.tools.prepare(self.obs, self.args(self.describe(), "😀"*15+"x"), context=self.context)
        self.assertEqual(draft.fields[0].value, "😀"*15+"x")

    def test_goal_handoff_uses_source_labels_and_the_same_live_entry_verification(self):
        from types import SimpleNamespace
        from web_operation_agent.execution.budget import TurnBudget
        from web_operation_agent.execution.receipt import TurnReceipt
        from web_operation_agent.goal.forms import handoff_form
        from web_operation_agent.goal.ports import FormPlan, PagePlan
        from web_operation_agent.schemas import ActionReceipt
        calls, entered = [], {}
        def execute(action, args, **kwargs):
            calls.append(action)
            if action == "fill":
                entered[args["ref"]] = args["value"]
            if action == "handoff_form":
                self.assertEqual(len(args["fields"]), 2)
                self.assertEqual(args["submit"]["scope"], self.state.control_scope)
                self.assertEqual(args["submit"]["label"], "确定")
                for field in args["fields"]:
                    self.assertEqual(entered[field["ref"]], field["value"])
            epoch = len(calls)+1
            after = replace(self.obs, epoch=epoch,
                elements=[replace(e, epoch=epoch, attrs={**e.attrs,
                    **({"value": entered[e.ref]} if e.ref in entered else {})}) for e in self.obs.elements])
            return ActionReceipt(1, str(len(calls)), action, {}, True, observation=after)
        class Port:
            def capabilities(self):
                return frozenset({"fill", "handoffs"})
        state = SimpleNamespace(obs=self.obs, cancelled=lambda: False, step=1, page_actions=0,
            budget=TurnBudget(steps=1, seconds=10), receipt=TurnReceipt(),
            approvals=SimpleNamespace(context=lambda *a: self.context,
                confirm_operation=lambda *a, **k: ("", ""), settle_operation=lambda: None),
            ledger=SimpleNamespace(append=lambda *a, **kw: None),
            writes=SimpleNamespace(expect_decision=lambda *a: None,
                write_outcome=lambda *a, **kw: {"state": "not_written"}, settle_write=lambda *a: ("", "")),
            browser=SimpleNamespace(execute_with_budget=execute))
        form = self.port.describe_form(self.obs, context=self.context)
        executor = SimpleNamespace(config=SimpleNamespace(allow_form_prefill=True),
            disabled_page_actions=frozenset(), goal_port=Port(),
            application=SimpleNamespace(ports=SimpleNamespace(forms=self.port),
                                         descriptor=SimpleNamespace(application_id="yudao")))
        plan = PagePlan("/records", "合成表单", form=FormPlan(form.reference,
            (("name", "合成名称值"), ("description", "合成描述值"))))
        result = handoff_form(executor, state, plan)
        self.assertTrue(result.complete)
        self.assertEqual(calls, ["fill", "fill", "handoff_form"])
        self.assertEqual(result.fields, ("合成名称", "合成描述"))
        self.assertEqual(state.budget.used, 0)
        self.verifier.state = replace(self.state, entry_id="other")
        self.assertFalse(handoff_form(executor, state, plan).complete)
        self.assertEqual(len(calls), 3)

    def test_handlers_and_conditional_fields_are_not_silently_admitted(self):
        self.path.write_text(_SOURCE.replace('v-model="formData.name"', 'v-model="formData.name" @change="save"'))
        self.verifier.state = replace(self.state, source_digest=self.reader.read(_PATH).binding_digest)
        self.assertEqual([f["label"] for f in self.describe()["fields"]], ["合成描述"])

    def test_port_requires_explicit_integration_and_keeps_default_native_forms_disabled(self):
        app = YudaoApplication("https://synthetic.test", None, DOCS, client=_Client(), conventions=None)
        self.assertIsNone(app.ports.forms.describe_form(self.obs))
        with self.assertRaises(ValueError):
            YudaoApplication("https://synthetic.test", None, DOCS, client=_Client(), conventions=None, forms=self.port)
        app = YudaoApplication("https://synthetic.test", None, DOCS, client=_Client(), conventions=None,
                               forms=self.port, page_write_guard="/reviewed-guard")
        self.assertIs(app.ports.forms, self.port)

    def test_update_requires_an_exact_record_and_create_cannot_carry_a_record(self):
        for changes in ({"record_id": ""}, {"record_id": "0"}, {"record_id": "１２"},
                        {"record_id": "1"*21}, {"mode": "create"}, {"mode": "delete"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.state, **changes)


if __name__ == "__main__":
    unittest.main()
