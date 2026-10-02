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
    NativeEntryFormPort, NativeEntryClickPolicy, REVIEWED_FILES)
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

    def test_source_replacement_and_incomplete_review_refuse(self):
        with self.assertRaises(ValueError):
            NativeFormReview.load({**self.payload,"hashes":{}},self.tree)
        self.tree.resolve(REVIEWED_FILES[2]).write_text("<template>changed dialog</template>")
        with self.assertRaises(BusinessToolRequestError):
            self.port.plan_entry("customer-update:12",self.before,context=self.context)

    def test_form_goals_keep_facts_and_refuse_unsupported_fields(self):
        data = object()
        port = NativeFormsGoalPort(data,self.verifier.source)
        index = ApplicationIndex(entities=(IndexEntity("customers","Customers", fields=(IndexField("remark","合成备注","text"),)),))
        spec = GoalSpec.from_json({"write_intent":True,"deliverables":[
            {"kind":"fact","entity":"customers","record":"Synthetic customer","fields":["合成备注"]},
            {"kind":"handoff","entity":"customers","record":"Synthetic customer","action":"合成备注","value":"Draft"}]})
        page = PagePlan(self.route,"Synthetic customer",expected_path=self.route)
        result = Resolution((Resolved("fact",facts=(Fact("Synthetic customer","合成备注","Old"),)),Resolved("page",page=page)))
        with patch.object(DataGoalPort,"resolve",return_value=result), patch.object(port,"index",return_value=index):
            resolved = port.resolve(spec,context=self.context)
            self.assertEqual(resolved.deliverables[0],result.deliverables[0])
            self.assertTrue(resolved.deliverables[1].page.form.open_entry)
            wrong = replace(spec,deliverables=(spec.deliverables[0],replace(spec.deliverables[1],action="Unsupported")))
            self.assertTrue(port.resolve(wrong,context=self.context).deliverables[1].problem)


if __name__ == "__main__":
    unittest.main()
