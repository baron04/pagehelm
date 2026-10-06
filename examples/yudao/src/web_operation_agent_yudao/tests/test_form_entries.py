from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from web_operation_agent.business.errors import BusinessToolRequestError, BusinessToolPermissionError, BusinessToolIdentityError
from web_operation_agent.business.forms import ApplicationFormTools
from web_operation_agent.goal.ports import PagePlan, Resolved, Resolution, Fact
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexField
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent.schemas import ElementRef, PageObservation, ActionReceipt
from web_operation_agent.state.identity import PrincipalContext, OperationContext
from web_operation_agent_yudao.form_entries import (NativeFormReview, NativeCustomerEntryVerifier,
    NativeEntryFormPort, NativeEntryClickPolicy, REVIEWED_FILES, CREATE_ENTRIES,
    NativeCreateReview, NativeCreateEntryVerifier, NativeEntryVerifiers)
from web_operation_agent_yudao.form_goals import NativeFormsGoalPort
from web_operation_agent_yudao.repository import repository_tree


class Client:
    tenant = "1"
    level = 3
    actor = 2
    permission = "crm:customer:update"
    def get(self, path, **params):
        if path == "system/auth/get-permission-info":
            return {"user": {"id": self.actor}, "permissions": [self.permission]}
        if path == "crm/permission/list":
            return [{"userId": 2, "level": self.level, "bizType": 2, "bizId": int(params["bizId"])}]
        if path == "crm/customer/get":
            return {"id": int(params["id"])}
        raise AssertionError(path)


class EntryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for path in REVIEWED_FILES:
            p = root/path; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('<template><el-form :model="formData"><el-form-item label="合成备注" prop="remark">'
                         '<el-input v-model="formData.remark" /></el-form-item></el-form></template>')
        self.tree = repository_tree(root, "synthetic-entry")
        self.payload = {"schema_version": 1, "release": self.tree.release,
            "hashes": {p: hashlib.sha256(self.tree.read_text(p).encode()).hexdigest() for p in REVIEWED_FILES}}
        self.review = NativeFormReview.load(self.payload, self.tree)
        self.client = Client()
        self.verifier = NativeCustomerEntryVerifier(self.tree, self.review, self.client)
        self.port = NativeEntryFormPort(self.verifier)
        now = int(time.time()*1000)
        principal = PrincipalContext(1, "yudao", "1", "10", "2", "delegation", now-100, now+60000, ("yudao.read",))
        self.context = OperationContext.create(principal, session_id="session", turn_id="turn",
            operation_id="entry", deadline_ms=now+30000)
        self.route = "/crm/customer/detail/12"
        self.before = PageObservation(1, 1, "https://synthetic.test"+self.route, "Customer", [],
            [ElementRef("e1", "button", "编辑", "button", 1)], render_state="ready", record_route=self.route)
        self.after = replace(self.before, epoch=3, elements=[
            ElementRef("e2", "textbox", "", "input", 3, {"field_label":"合成备注", "form_scope":"dialog:editor",
                "form_instance":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}),
            ElementRef("e3", "button", "确 定", "button", 3, {"form_scope":"dialog:editor",
                "form_instance":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"})])
        self.receipt = ActionReceipt(1, "open", "click", {"ref":"e1", "epoch":1}, True,
            write_guard_held=True, observation=self.after)

    def accept(self):
        entry = self.port.plan_entry("customer-update:12", self.before, context=self.context)
        return self.port.accept_entry(entry, self.before, self.receipt, self.after, context=self.context)

    def test_exact_receipt_live_authority_and_current_instance_issue_a_form(self):
        reference = self.accept()
        tools = ApplicationFormTools(self.port, application_id="yudao")
        draft, _ = tools.prepare_current(self.after, reference=reference, values=(("remark","Synthetic draft"),), context=self.context)
        self.assertEqual(draft.fields[0].value, "Synthetic draft")
        self.assertTrue(self.verifier.validate(self.verifier._current, context=self.context))
        changed = replace(self.after, epoch=4, elements=[replace(e, epoch=4, attrs={**e.attrs,
            "form_instance":"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}) for e in self.after.elements])
        with self.assertRaises(BusinessToolRequestError):
            tools.take_verification(draft).check(draft, changed, context=self.context)
        self.assertIsNone(self.verifier._current)

    def test_permissions_are_rechecked_and_missing_scope_never_issues(self):
        for attr, value in (("level",2), ("level",True), ("permission",{}),
                            ("permission","crm:customer:query"), ("actor",3)):
            with self.subTest(attr=attr):
                old = getattr(self.client,attr); setattr(self.client,attr,value)
                with self.assertRaises((BusinessToolPermissionError, BusinessToolIdentityError)):
                    self.port.plan_entry("customer-update:12", self.before, context=self.context)
                setattr(self.client,attr,old)
        self.accept(); self.client.level=2
        with self.assertRaises(BusinessToolPermissionError):
            self.port.describe_form(self.after,context=self.context)
        self.assertIsNone(self.verifier._current)

    def test_wrong_or_uncertain_receipt_cannot_be_replayed(self):
        for args in ({"ok":False}, {"write_guard_held":False}, {"args_echo":{"ref":"other","epoch":1}},
                     {"navigated":True}, {"action":"observe"}):
            with self.subTest(args=args):
                entry = self.port.plan_entry("customer-update:12", self.before, context=self.context)
                with self.assertRaises(BusinessToolRequestError):
                    self.port.accept_entry(entry,self.before,replace(self.receipt,**args),self.after,context=self.context)
                with self.assertRaises(BusinessToolRequestError):
                    self.port.accept_entry(entry,self.before,self.receipt,self.after,context=self.context)

    def test_only_exact_reviewed_button_is_allowed(self):
        policy = NativeEntryClickPolicy(self.verifier,None)
        self.assertTrue(policy.decide(self.before,self.before.elements[0]))
        self.port.plan_entry("customer-update:12",self.before,context=self.context)
        self.assertEqual(policy.decide(self.before,self.before.elements[0]),"")
        self.port.discard_entry(self.verifier._planned[0],context=self.context)
        self.assertTrue(policy.decide(self.before,self.before.elements[0]))
        self.port.plan_entry("customer-update:12",self.before,context=self.context)
        self.assertTrue(policy.decide(self.before,replace(self.before.elements[0],name="提交")))
        with self.assertRaises(BusinessToolRequestError):
            self.port.plan_entry("customer-update:13",self.before,context=self.context)
        with self.assertRaises(BusinessToolRequestError):
            self.port.plan_entry("customer-update:12",self.after,context=self.context)

    def test_discovery_requires_rendered_record_unique_enabled_control_and_live_authority(self):
        ready = replace(self.before, page_route=self.route)
        rows = self.port.form_entries(ready, context=self.context)
        self.assertEqual([(r.reference, r.label, r.kind) for r in rows],
                         [('customer-update:12', '编辑', 'update')])
        for changed in (self.before, replace(ready, record_route=''),
                        replace(ready, render_state='pending'),
                        replace(ready, elements=[ready.elements[0], replace(ready.elements[0], ref='other')]),
                        replace(ready, elements=[replace(ready.elements[0], attrs={'disabled': 'true'})]),
                        replace(self.after, page_route=self.route),
                        replace(ready, url=ready.url + '?id=12')):
            with self.subTest(page=changed):
                self.assertEqual(self.port.form_entries(changed, context=self.context), ())
        self.client.level = 2
        with self.assertRaises(BusinessToolPermissionError):
            self.port.form_entries(ready, context=self.context)

    def test_missing_instance_conflicting_scope_and_wrong_record_refuse(self):
        for change in ({"form_instance":""}, {"form_scope":"dialog:other"}):
            with self.subTest(change=change):
                entry = self.port.plan_entry("customer-update:12",self.before,context=self.context)
                changed = replace(self.after,elements=[replace(self.after.elements[0],attrs={**self.after.elements[0].attrs,**change}),self.after.elements[1]])
                with self.assertRaises(BusinessToolRequestError):
                    self.port.accept_entry(entry,self.before,self.receipt,changed,context=self.context)
        entry = self.port.plan_entry("customer-update:12",self.before,context=self.context)
        with self.assertRaises(BusinessToolRequestError):
            self.port.accept_entry(entry,self.before,self.receipt,replace(self.after,record_route="/crm/customer/detail/13"),context=self.context)

    def test_calendar_navigation_does_not_rebind_the_editor_but_another_editor_is_ambiguous(self):
        popup = ElementRef("e4", "button", "Previous month", "button", 3,
                           {"form_scope": "dialog:calendar", "form_instance": "b" * 36})
        observation = replace(self.after, elements=[*self.after.elements, popup])
        self.assertEqual(self.verifier._scope(observation),
                         ("dialog:editor", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        for other in (replace(popup, name="确 定"),
                      replace(popup, role="textbox", attrs={**popup.attrs, "field_label": "Other field"}),
                      replace(popup, role="spinbutton", attrs={**popup.attrs, "field_label": "Other amount"})):
            with self.subTest(other=other):
                with self.assertRaises(BusinessToolRequestError):
                    self.verifier._scope(replace(observation, elements=[*self.after.elements, other]))

    def test_source_replacement_and_incomplete_review_refuse(self):
        with self.assertRaises(ValueError):
            NativeFormReview.load({**self.payload,"hashes":{}},self.tree)
        self.tree.resolve(REVIEWED_FILES[2]).write_text("<template>changed dialog</template>")
        with self.assertRaises(BusinessToolRequestError):
            self.port.plan_entry("customer-update:12",self.before,context=self.context)

    def test_form_goals_keep_facts_and_refuse_unsupported_fields(self):
        data = object()
        port = NativeFormsGoalPort(data,self.verifier.source)
        index = ApplicationIndex(entities=(IndexEntity("crm/customer","Customers", fields=(IndexField("remark","合成备注","text"),)),))
        spec = GoalSpec.from_json({"write_intent":True,"deliverables":[
            {"kind":"fact","entity":"crm/customer","record":"Synthetic customer","fields":["合成备注"]},
            {"kind":"handoff","entity":"crm/customer","record":"Synthetic customer","action":"合成备注","value":"Draft"}]})
        page = PagePlan(self.route,"Synthetic customer",expected_path=self.route)
        result = Resolution((Resolved("fact",facts=(Fact("Synthetic customer","合成备注","Old"),)),Resolved("page",page=page)))
        with patch.object(DataGoalPort,"resolve",return_value=result), patch.object(port,"index",return_value=index):
            resolved = port.resolve(spec,context=self.context)
            self.assertEqual(resolved.deliverables[0],result.deliverables[0])
            self.assertTrue(resolved.deliverables[1].page.form.open_entry)
            self.assertTrue(resolved.deliverables[1].page.requires_page_route)
            batch = replace(spec,deliverables=(spec.deliverables[0],replace(spec.deliverables[1],
                action="",value="",field_values=(("合成备注","Draft"),))))
            self.assertEqual(port.resolve(batch,context=self.context).deliverables[1].page.form.values,
                             (("remark","Draft"),))
            for action in ("edit", "编辑", "修改"):
                edited = replace(batch, deliverables=(batch.deliverables[0], replace(batch.deliverables[1], action=action)))
                self.assertEqual(port.resolve(edited, context=self.context).deliverables[1].page.form.values, (("remark", "Draft"),))
            for action in ("删除", "提交", "Delete", "合成备注"):
                forbidden = replace(batch, deliverables=(batch.deliverables[0], replace(batch.deliverables[1], action=action)))
                self.assertTrue(port.resolve(forbidden, context=self.context).deliverables[1].problem)
            wrong = replace(spec,deliverables=(spec.deliverables[0],replace(spec.deliverables[1],action="Unsupported")))
            self.assertTrue(port.resolve(wrong,context=self.context).deliverables[1].problem)


class DefinedCreationEntryTests(unittest.TestCase):
    setUp = EntryTests.setUp

    def defined(self):
        definition = {"entity": "synthetic/record", "route": "/synthetic/records",
            "label": "新建记录", "permission": "synthetic:record:create",
            "parent_component": "ui/src/views/synthetic/record/index.vue",
            "form_component": "ui/src/views/synthetic/record/RecordForm.vue"}
        for path in (definition["parent_component"], definition["form_component"]):
            target = self.tree.root / path; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('<template><el-form :model="formData">'
                '<el-form-item label="金额" prop="price"><el-input-number v-model="formData.price" :min="0" :precision="2"/></el-form-item>'
                '<el-form-item label="日期" prop="day"><el-date-picker v-model="formData.day" type="date" value-format="x"/></el-form-item>'
                '</el-form></template>')
        paths = (definition["parent_component"], definition["form_component"], REVIEWED_FILES[2])
        payload = {"schema_version": 1, "release": self.tree.release, "reference": "synthetic-create",
            "entry": definition, "hashes": {p: hashlib.sha256(self.tree.read_text(p).encode()).hexdigest() for p in paths}}
        review = NativeCreateReview.load(payload, self.tree)
        client = Client(); client.permission = definition["permission"]
        verifier = NativeCreateEntryVerifier(self.tree, review, client)
        before = replace(self.before, url="https://synthetic.test" + definition["route"],
            record_route="", page_route=definition["route"],
            elements=[replace(self.before.elements[0], name=definition["label"])])
        attrs = self.after.elements[0].attrs
        after = replace(self.after, url=before.url, record_route="", page_route=definition["route"],
            elements=[ElementRef("amount", "spinbutton", "", "input", 3, {**attrs, "field_label":"金额"}),
                ElementRef("day", "textbox", "", "input", 3, {**attrs, "field_label":"日期"}), self.after.elements[1]])
        return payload, client, verifier, before, after

    def test_deployment_defined_entry_binds_scalar_fields_without_a_code_registry(self):
        payload, client, verifier, before, after = self.defined()
        port = NativeEntryFormPort(verifier)
        self.assertEqual([r.reference for r in port.form_entries(before, context=self.context)], ["synthetic-create"])
        entry = port.plan_entry("synthetic-create", before, context=self.context)
        ref = port.accept_entry(entry, before, self.receipt, after, context=self.context)
        draft, _ = ApplicationFormTools(port, application_id="yudao").prepare_current(after,
            reference=ref, values=(("price", "1250.5"), ("day", "2026-10-08")), context=self.context)
        self.assertEqual([f.value for f in draft.fields], ["1250.50", "2026-10-08"])
        client.permission = "synthetic:record:query"
        with self.assertRaises(BusinessToolPermissionError):
            port.describe_form(after, context=self.context)

    def test_definition_cannot_borrow_builtin_authority_or_escape_reviewed_source(self):
        payload, _, _, _, _ = self.defined()
        for key, value in (("form_component", "../private.vue"), ("route", "https://other.test/records"),
                           ("permission", "*:*:*"), ("entity", ""), ("label", "\n新建")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                NativeCreateReview.load({**payload, "entry": {**payload["entry"], key:value}}, self.tree)
        with self.assertRaises(ValueError):
            NativeCreateReview.load({**payload, "reference":"customer-create"}, self.tree)
        with self.assertRaises(ValueError):
            NativeCreateReview.load({k:v for k,v in payload.items() if k!="entry"}, self.tree)
        path = self.tree.root / payload["entry"]["form_component"]
        path.write_text("changed source")
        with self.assertRaises(ValueError):
            NativeCreateReview.load(payload, self.tree)

    def user_opened(self):
        payload, client, _, _, observation = self.defined()
        path = self.tree.root / payload["entry"]["form_component"]
        path.write_text(path.read_text().replace('label="金额"', 'v-if="formType === \'create\'" label="金额"'))
        payload["hashes"][payload["entry"]["form_component"]] = hashlib.sha256(path.read_bytes()).hexdigest()
        verifier = NativeCreateEntryVerifier(self.tree, NativeCreateReview.load(payload, self.tree), client)
        return payload, client, verifier, observation

    def test_user_opened_defined_creation_uses_mode_evidence_without_an_opener_receipt(self):
        _, _, verifier, observation = self.user_opened()
        combined = NativeEntryVerifiers(verifier)
        port = NativeEntryFormPort(combined)
        form = port.current_goal_form("synthetic/record", observation, context=self.context)
        self.assertIsNotNone(form)
        self.assertIsNone(verifier._planned)
        self.assertEqual((verifier._current.mode, verifier._current.record_id), ("create", ""))
        self.assertEqual(combined.active, verifier)
        tools = ApplicationFormTools(port, application_id="yudao")
        draft, _ = tools.prepare_current(observation, reference=form.reference,
            values=(("price", "12.5"), ("day", "2026-10-08")), context=self.context)
        self.assertEqual([f.value for f in draft.fields], ["12.50", "2026-10-08"])
        self.assertEqual(draft.persistence, "transient_until_submit")
        self.assertEqual(draft.effect, "")
        tools.take_verification(draft).check(draft, replace(observation, epoch=4,
            elements=[replace(e, epoch=4) for e in observation.elements]), context=self.context)

    def test_adoption_requires_rendered_route_unique_enabled_create_marker_and_live_authority(self):
        _, _, _, observation = self.user_opened()
        for changed in (replace(observation, page_route=""), replace(observation, record_route=self.route),
                        replace(observation, url=observation.url + "?id=12"),
                        replace(observation, elements=observation.elements[1:]),
                        replace(observation, elements=[replace(observation.elements[0], ref="duplicate"), *observation.elements]),
                        replace(observation, elements=[replace(observation.elements[0],
                            attrs={**observation.elements[0].attrs, "disabled": "true"}), *observation.elements[1:]])):
            with self.subTest(observation=changed):
                _, _, verifier, _ = self.user_opened()
                self.assertIsNone(verifier.adopt_current(changed, context=self.context))
        for attribute, value in (("permission", "synthetic:record:query"), ("actor", 3), ("tenant", "2")):
            with self.subTest(attribute=attribute):
                _, client, verifier, observation = self.user_opened()
                setattr(client, attribute, value)
                with self.assertRaises((BusinessToolPermissionError, BusinessToolIdentityError)):
                    verifier.adopt_current(observation, context=self.context)

    def test_unconditional_update_or_unknown_mode_cannot_admit_a_user_opened_creation(self):
        for condition in ("", 'v-if="formType === \'update\'" ', 'v-if="canCreate" ',
                          'v-if="formType === \'create\'" @change="persist" '):
            with self.subTest(condition=condition):
                payload, client, verifier, _, observation = self.defined()
                path = self.tree.root / payload["entry"]["form_component"]
                path.write_text(path.read_text().replace('label="金额"', condition + 'label="金额"'))
                payload["hashes"][payload["entry"]["form_component"]] = hashlib.sha256(path.read_bytes()).hexdigest()
                verifier = NativeCreateEntryVerifier(self.tree, NativeCreateReview.load(payload, self.tree), client)
                self.assertIsNone(verifier.adopt_current(observation, context=self.context))

    def test_user_opened_creation_cannot_use_changed_reviewed_source(self):
        payload, _, verifier, observation = self.user_opened()
        path = self.tree.root / payload["entry"]["form_component"]
        path.write_text(path.read_text().replace(':precision="2"', ':precision="3"'))
        with self.assertRaises(BusinessToolRequestError):
            verifier.adopt_current(observation, context=self.context)
        self.assertIsNone(verifier._current)

    def test_adopted_form_cannot_drift_or_readopt_after_mode_route_or_instance_loss(self):
        _, _, _, observation = self.user_opened()
        changes = (replace(observation, elements=observation.elements[1:]),
                   replace(observation, url=observation.url + "?id=12"),
                   replace(observation, elements=[replace(e, attrs={**e.attrs,
                       "form_instance": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}) for e in observation.elements]))
        for changed in changes:
            with self.subTest(changed=changed):
                _, _, verifier, original = self.user_opened()
                port = NativeEntryFormPort(verifier)
                form = port.describe_form(original, context=self.context)
                tools = ApplicationFormTools(port, application_id="yudao")
                draft, _ = tools.prepare_current(original, reference=form.reference,
                    values=(("price", "12.5"),), context=self.context)
                with self.assertRaises(BusinessToolRequestError):
                    tools.take_verification(draft).check(draft, changed, context=self.context)
                self.assertIsNone(verifier.current(original, context=self.context))
                self.assertIsNone(verifier.adopt_current(original, context=self.context))
                self.assertIsNotNone(verifier.current(original,
                    context=replace(self.context, turn_id="fresh-user-turn")))

    def test_two_matching_creation_policies_cannot_choose_an_editor(self):
        payload, client, first, observation = self.user_opened()
        review = NativeCreateReview.load({**payload, "reference": "other-create"}, self.tree)
        second = NativeCreateEntryVerifier(self.tree, review, client)
        combined = NativeEntryVerifiers(first, second)
        self.assertIsNone(combined.current(observation, context=self.context))
        self.assertIsNone(combined.active)

    def test_another_user_turn_can_adopt_a_different_policy_but_the_bound_turn_cannot(self):
        payload, client, first, observation = self.user_opened()
        definition = {**payload["entry"], "route": "/synthetic/other-records"}
        second = NativeCreateEntryVerifier(self.tree, NativeCreateReview.load(
            {**payload, "reference": "other-create", "entry": definition}, self.tree), client)
        combined = NativeEntryVerifiers(first, second)
        self.assertIsNotNone(combined.current(observation, context=self.context))
        other = replace(observation, url="https://synthetic.test" + definition["route"],
            page_route=definition["route"], elements=[replace(e, attrs={**e.attrs,
                "form_instance": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}) for e in observation.elements])
        self.assertIsNone(combined.current(other, context=self.context))
        self.assertIsNone(combined.current(observation, context=self.context))
        state = combined.current(other, context=replace(self.context, turn_id="fresh-user-turn"))
        self.assertEqual(state.route, definition["route"])
        self.assertEqual(combined.active, second)


class CreationEntryTests(unittest.TestCase):
    setUp = EntryTests.setUp

    def creation(self, reference="customer-create"):
        entry = CREATE_ENTRIES[reference]
        for relative in (entry[4], entry[5]):
            path = self.tree.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('<template><el-form :model="formData"><el-form-item label="合成备注" prop="remark">'
                            '<el-input v-model="formData.remark" /></el-form-item></el-form></template>')
        files = (entry[4], entry[5], REVIEWED_FILES[2])
        if reference == "contact-create":
            files += (REVIEWED_FILES[0],)
        payload = {"schema_version": 1, "release": self.tree.release, "reference": reference,
                   "hashes": {p: hashlib.sha256(self.tree.read_text(p).encode()).hexdigest() for p in files}}
        review = NativeCreateReview.load(payload, self.tree)
        client = Client(); client.permission = entry[3]
        verifier = NativeCreateEntryVerifier(self.tree, review, client)
        before = replace(self.before, url="https://synthetic.test" + entry[1], record_route="",
                         elements=[replace(self.before.elements[0], name=entry[2])])
        after = replace(self.after, url=before.url, record_route="")
        return payload, client, verifier, before, after

    def test_user_opened_contact_creation_is_bound_to_parent_actor_and_instance(self):
        _, client, verifier, _, _ = self.creation("contact-create")
        component = CREATE_ENTRIES["contact-create"][5]
        path = self.tree.root / component
        path.write_text('<template><el-form :model="formData">' + ''.join(
            f'<el-form-item label="{label}" prop="{field}"><el-input v-model="formData.{field}" /></el-form-item>'
            for field, label in (("name", "联系人姓名"), ("mobile", "手机"))) + '</el-form></template>')
        files = dict(verifier.review.hashes)
        files[component] = hashlib.sha256(path.read_bytes()).hexdigest()
        review = NativeCreateReview.load({"schema_version": 1, "release": self.tree.release,
            "reference": "contact-create", "hashes": files}, self.tree)
        verifier = NativeCreateEntryVerifier(self.tree, review, client)
        port = NativeEntryFormPort(NativeEntryVerifiers(verifier))
        attrs = self.after.elements[0].attrs
        elements = [ElementRef(f"f{i}", role, "", "input", 3,
            {**attrs, "field_label": label, **({"disabled": "true"} if disabled else {})})
            for i, (label, role, disabled) in enumerate((("联系人姓名", "textbox", False),
                ("手机", "textbox", False), ("客户名称", "combobox", True), ("负责人", "combobox", False)))]
        obs = replace(self.after, selected_controls=["联系人"], elements=elements + [self.after.elements[1]])
        form = port.current_goal_form("crm/contact", obs, context=self.context)
        self.assertIsNotNone(form)
        tools = ApplicationFormTools(port, application_id="yudao")
        draft, _ = tools.prepare_current(obs, reference=form.reference,
            values=(("name", "Synthetic"), ("mobile", "00000000001")), context=self.context)
        self.assertEqual([f.value for f in draft.fields], ["Synthetic", "00000000001"])
        check = tools.take_verification(draft)
        changed = replace(obs, elements=[replace(e, attrs={**e.attrs,
            "form_instance": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}) for e in obs.elements])
        with self.assertRaises(BusinessToolRequestError):
            check.check(draft, changed, context=self.context)
        verifier._current = None
        second = replace(self.context, turn_id="next-turn")
        self.assertIsNotNone(port.current_goal_form("crm/contact", obs, context=second))
        self.assertNotEqual(port.describe_form(obs, context=second).reference, form.reference)
        for changed in (replace(obs, record_route="/crm/customer/detail/13"),
                        replace(obs, elements=[replace(e, attrs={**e.attrs, "disabled": "true"})
                            if e.attrs.get("field_label") == "负责人" else e for e in obs.elements])):
            verifier._current = None
            self.assertIsNone(verifier.adopt_current(changed, context=self.context))
        client.permission = "crm:contact:query"
        with self.assertRaises(BusinessToolPermissionError):
            verifier.adopt_current(obs, context=self.context)

    def test_reviewed_contact_opener_on_parent_discovers_and_binds_only_child_editor(self):
        _, client, _, _, _ = self.creation("contact-create")
        component = CREATE_ENTRIES["contact-create"][5]
        path = self.tree.root / component
        path.write_text('<template><el-form :model="formData">' + ''.join(
            f'<el-form-item label="{label}" prop="{field}"><el-input v-model="formData.{field}" /></el-form-item>'
            for field, label in (("name", "联系人姓名"), ("mobile", "手机"))) + '</el-form></template>')
        files = (CREATE_ENTRIES["contact-create"][4], component, REVIEWED_FILES[2], REVIEWED_FILES[0])
        review = NativeCreateReview.load({"schema_version": 1, "release": self.tree.release,
            "reference": "contact-create", "hashes": {p: hashlib.sha256(self.tree.read_text(p).encode()).hexdigest()
                for p in files}}, self.tree)
        verifier = NativeCreateEntryVerifier(self.tree, review, client)
        port = NativeEntryFormPort(NativeEntryVerifiers(verifier))
        before = replace(self.before, page_route=self.route,
            elements=[replace(self.before.elements[0], name="创建联系人")])
        self.assertEqual([(r.reference, r.kind) for r in port.form_entries(before, context=self.context)],
                         [("contact-create", "create")])
        attrs = self.after.elements[0].attrs
        after = replace(self.after, page_route=self.route, elements=[
            ElementRef("name", "textbox", "", "input", 3, {**attrs, "field_label": "联系人姓名"}),
            ElementRef("mobile", "textbox", "", "input", 3, {**attrs, "field_label": "手机"}),
            ElementRef("owner", "combobox", "", "input", 3, {**attrs, "field_label": "负责人"}),
            self.after.elements[1]])
        entry = port.plan_entry("contact-create", before, context=self.context)
        reference = port.accept_entry(entry, before, self.receipt, after, context=self.context)
        self.assertEqual(port.describe_form(after, context=self.context).reference, reference)
        tools = ApplicationFormTools(port, application_id="yudao")
        draft, _ = tools.prepare_current(after, reference=reference,
            values=(("name", "Synthetic child"), ("mobile", "00000000001")), context=self.context)
        self.assertEqual([f.field for f in draft.fields], ["name", "mobile"])
        changed = replace(after, url="https://synthetic.test/crm/customer/detail/13",
            page_route="/crm/customer/detail/13", record_route="/crm/customer/detail/13")
        self.assertIsNone(port.describe_form(changed, context=self.context))
        self.assertIsNone(port.describe_form(after, context=self.context))
        for invalid in (replace(before, record_route=""), replace(before, page_route=""),
                replace(before, url=before.url + "?id=12"),
                replace(before, elements=[before.elements[0], replace(before.elements[0], ref="duplicate")])):
            self.assertEqual(port.form_entries(invalid, context=self.context), ())
        verifier = NativeCreateEntryVerifier(self.tree, review, client)
        entry = verifier.plan_entry("contact-create", before, context=self.context)
        with self.assertRaises(BusinessToolRequestError):
            verifier.accept_entry(entry, before, self.receipt, self.after, context=self.context)
        client.actor = 3
        with self.assertRaises(BusinessToolIdentityError):
            verifier.plan_entry("contact-create", before, context=self.context)

    def test_separate_creation_policies_bind_exact_receipt_and_no_record(self):
        for reference in CREATE_ENTRIES:
            with self.subTest(reference=reference):
                _, client, verifier, before, after = self.creation(reference)
                port = NativeEntryFormPort(verifier)
                entry = port.plan_entry(reference, before, context=self.context)
                ref = port.accept_entry(entry, before, self.receipt, after, context=self.context)
                draft, _ = ApplicationFormTools(port, application_id="yudao").prepare_current(
                    after, reference=ref, values=(("remark", "Draft"),), context=self.context)
                self.assertEqual(draft.fields[0].value, "Draft")
                self.assertEqual(verifier._current.mode, "create")
                self.assertEqual(verifier._current.record_id, "")
                client.permission = "crm:customer:update"
                with self.assertRaises(BusinessToolPermissionError):
                    verifier.validate(verifier._current, context=self.context)

    def test_creation_discovery_is_limited_to_its_reviewed_rendered_collection(self):
        _, client, verifier, before, after = self.creation()
        port = NativeEntryFormPort(verifier)
        ready = replace(before, page_route='/crm/customer')
        rows = port.form_entries(ready, context=self.context)
        self.assertEqual([(r.reference, r.kind) for r in rows], [('customer-create', 'create')])
        query_scoped = replace(ready, elements=[replace(e, attrs={"form_scope":"form_instance:query"})
                                               for e in ready.elements])
        self.assertEqual(port.form_entries(query_scoped, context=self.context), rows)
        self.assertEqual(port.plan_entry("customer-create", query_scoped, context=self.context).reference,
                         "customer-create")
        for changed in (before, replace(ready, page_route='/pms/pm/project/list'),
                        replace(ready, record_route=self.route),
                        replace(after, page_route='/crm/customer')):
            with self.subTest(page=changed):
                self.assertEqual(port.form_entries(changed, context=self.context), ())
        client.permission = 'crm:customer:update'
        with self.assertRaises(BusinessToolPermissionError):
            port.form_entries(ready, context=self.context)

    def test_project_tabs_are_reviewed_and_bound_through_the_entry_lifetime(self):
        _, client, verifier, before, after = self.creation("project-create")
        route = "/pms/pm/project/list"
        port = NativeEntryFormPort(verifier)
        ready = replace(before, url=before.url + "?tabs=participate", page_route=route)
        self.assertEqual(len(port.form_entries(ready, context=self.context)), 1)
        for suffix in ("?tabs=owner", "?tabs=all"):
            self.assertEqual(len(port.form_entries(replace(ready, url=before.url + suffix), context=self.context)), 1)
        for suffix in ("?tabs=unknown", "?tabs=participate&tabs=all", "?tabs=participate&id=1", "?tabs=created", "#other"):
            changed = replace(ready, url=before.url + suffix)
            self.assertEqual(port.form_entries(changed, context=self.context), ())
            with self.assertRaises(BusinessToolRequestError):
                port.plan_entry("project-create", changed, context=self.context)
        entry = port.plan_entry("project-create", ready, context=self.context)
        shown = replace(after, url=ready.url)
        reference = port.accept_entry(entry, ready, self.receipt, shown, context=self.context)
        draft, _ = ApplicationFormTools(port, application_id="yudao").prepare_current(
            shown, reference=reference, values=(("remark", "Draft"),), context=self.context)
        self.assertEqual(draft.route, route + "?tabs=participate")
        self.assertIsNone(verifier.current(replace(shown, url=before.url + "?tabs=all"), context=self.context))

    def test_user_opened_project_shape_binds_create_radio_and_local_datetime_fields(self):
        payload, client, _, _, observation = self.creation("project-create")
        component = CREATE_ENTRIES["project-create"][5]
        path = self.tree.root / component
        path.write_text('''<template><el-form :model="formData">
          <el-form-item v-if="formType === 'create'" label="项目类型" prop="type">
            <el-radio-group v-model="formData.type"><el-radio :value="1">普通项目</el-radio><el-radio :value="2">敏捷项目</el-radio></el-radio-group>
          </el-form-item>
          <el-form-item label="项目名称" prop="name"><el-input v-model="formData.name" maxlength="31"/></el-form-item>
          <el-form-item label="开始时间" prop="startTime"><el-date-picker v-model="formData.startTime" type="datetime" value-format="x"/></el-form-item>
          <el-form-item label="截止时间" prop="endTime"><el-date-picker v-model="formData.endTime" type="datetime" value-format="x"/></el-form-item>
        </el-form></template>''')
        payload["hashes"][component] = hashlib.sha256(path.read_bytes()).hexdigest()
        verifier = NativeCreateEntryVerifier(self.tree, NativeCreateReview.load(payload, self.tree), client)
        attrs = observation.elements[0].attrs
        observation = replace(observation, url=observation.url + "?tabs=participate", page_route="/pms/pm/project/list",
            elements=[ElementRef(ref, role, "", "div" if role == "radiogroup" else "input", 3,
                {**attrs, "field_label": label}) for ref, role, label in
                (("type", "radiogroup", "项目类型"), ("name", "textbox", "项目名称"),
                 ("start", "combobox", "开始时间"), ("end", "combobox", "截止时间"))] + [observation.elements[1]])
        port = NativeEntryFormPort(NativeEntryVerifiers(verifier))
        form = port.current_goal_form("pms/pm/project", observation, context=self.context)
        self.assertEqual([f.kind for f in form.fields], ["choice", "text", "datetime", "datetime"])
        tools = ApplicationFormTools(port, application_id="yudao")
        draft, _ = tools.prepare_current(observation, reference=form.reference, values=(("name", "合成项目"),
            ("startTime", "2026-10-08T09:30"), ("endTime", "2026-10-09T18:00")), context=self.context)
        self.assertEqual([f.value for f in draft.fields], ["合成项目", "2026-10-08 09:30:00", "2026-10-09 18:00:00"])
        check = tools.take_verification(draft)
        check.check(draft, replace(observation, epoch=4,
            elements=[replace(e, epoch=4) for e in observation.elements]), context=self.context)
        # A selected collection tab remains part of the exact entry, even though all
        # three tabs have the same reviewed creation opener.
        with self.assertRaises(BusinessToolRequestError):
            check.check(draft, replace(observation, url=observation.url.replace("participate", "all")), context=self.context)
        self.assertIsNone(port.describe_form(observation, context=self.context))

    def test_creation_cannot_borrow_update_review_or_other_entry_authority(self):
        with self.assertRaises(TypeError):
            NativeCreateEntryVerifier(self.tree, self.review, self.client)
        payload, client, verifier, before, after = self.creation()
        for reference in ([], {}, "project-create"):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                NativeCreateReview.load({**payload, "reference": reference}, self.tree)
        for obs in (replace(before, record_route="/crm/customer/detail/12"),
                    replace(before, url=before.url + "?id=12"), after):
            with self.assertRaises(BusinessToolRequestError):
                verifier.plan_entry("customer-create", obs, context=self.context)
        client.actor = 3
        with self.assertRaises(BusinessToolIdentityError):
            verifier.plan_entry("customer-create", before, context=self.context)
        client.actor = 2
        entry = verifier.plan_entry("customer-create", before, context=self.context)
        with self.assertRaises(BusinessToolRequestError):
            verifier.accept_entry(entry, before, replace(self.receipt, ok=False), after, context=self.context)
        with self.assertRaises(BusinessToolRequestError):
            verifier.accept_entry(entry, before, self.receipt, after, context=self.context)
        self.tree.resolve(CREATE_ENTRIES["customer-create"][4]).write_text("changed opener")
        with self.assertRaises(BusinessToolRequestError):
            verifier.plan_entry("customer-create", before, context=self.context)

    def test_composite_rejects_duplicate_or_differently_bound_source_policies(self):
        _, _, verifier, before, after = self.creation()
        combined = NativeEntryVerifiers(self.verifier, verifier)
        self.assertTrue(combined.goal_updates)
        self.assertFalse(NativeEntryFormPort(verifier).goal_updates)
        entry = combined.plan_entry("customer-create", before, context=self.context)
        combined.accept_entry(entry, before, self.receipt, after, context=self.context)
        self.assertEqual(combined.current(after, context=self.context).mode, "create")
        with self.assertRaises(ValueError):
            NativeEntryVerifiers(verifier, verifier)
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        other_tree = repository_tree(Path(tmp.name), "other-release")
        other = NativeCustomerEntryVerifier(other_tree, self.review, self.client)
        with self.assertRaises(ValueError):
            NativeEntryVerifiers(verifier, other)

    def test_creation_batch_requires_explicit_creation_and_never_selects_existing_record(self):
        _, _, verifier, _, _ = self.creation()
        port = NativeFormsGoalPort(object(), verifier.source, entries=verifier.goal_entries, updates=False)
        index = ApplicationIndex(entities=(IndexEntity("crm/customer", "Customers",
                    fields=(IndexField("remark", "合成备注", "text"),)),))
        spec = GoalSpec.from_json({"write_intent": True, "deliverables": [{"kind": "handoff",
            "entity": "crm/customer", "action": "create", "field_values": [{"field": "合成备注", "value": "Draft"}]}]})
        page = PagePlan("/crm/customer", "Customers", expected_path="/crm/customer")
        result = Resolution((Resolved("page", page=page),))
        with patch.object(DataGoalPort, "resolve", return_value=result), patch.object(port, "index", return_value=index):
            resolved = port.resolve(spec, context=self.context)
            self.assertEqual(resolved.deliverables[0].page.form.reference, "customer-create")
            source_path = self.tree.root / CREATE_ENTRIES["customer-create"][5]
            original = source_path.read_text()
            source_path.write_text(original.replace('<el-form-item label=', '<el-form-item v-if="formType === \'create\'" label='))
            self.assertEqual(port.resolve(spec, context=self.context).deliverables[0].page.form.reference, "customer-create")
            source_path.write_text(original.replace('<el-form-item label=', '<el-form-item v-if="canEdit" label='))
            self.assertTrue(port.resolve(spec, context=self.context).deliverables[0].problem)
            source_path.write_text(original)
            unknown = replace(spec, deliverables=(replace(spec.deliverables[0], action=""),))
            self.assertTrue(port.resolve(unknown, context=self.context).deliverables[0].problem)


if __name__ == "__main__":
    unittest.main()
