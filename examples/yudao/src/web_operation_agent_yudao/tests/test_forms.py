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
from web_operation_agent_yudao.forms import NativeFormState, YudaoFormPort
from web_operation_agent_yudao.repository import repository_tree
from web_operation_agent_yudao.source_forms import SourceFormReader
from web_operation_agent_yudao.application import YudaoApplication
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
