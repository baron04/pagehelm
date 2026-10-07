"""No models or accounts: live binding retains reviewed identity and persistence."""
from dataclasses import replace
import time
import unittest

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.forms import (ApplicationFormTools, ApplicationFormDescriptor,
    ApplicationFormEntry, ApplicationFormField, FormChoice)
from web_operation_agent.business.runtime import FormDraft, FormDraftField
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent_yudao.live_forms import (FenceEvidence, LiveFormPort, LiveRequest,
                                                  field_key)
from web_operation_agent_yudao.page_guard import GuardRefused, NativePageGuard

ACTOR, OWNER = "a" * 64, "c" * 64
DIALOG = {"form_scope": "dialog_describedby:el-1", "form_instance": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
QUERY = {"form_scope": "form_instance:q", "form_instance": "q"}


def control(ref, role, name, epoch, label="", **attrs):
    return ElementRef(ref, role, name, "input", epoch,
                      {**DIALOG, **({"field_label": label} if label else {}), **attrs})



class ReviewedEntry:
    """Synthetic review boundary; actual native verifiers are integration-tested separately."""
    goal_entries = (("contact-create", "crm/contact", "/crm/contact"),)
    goal_updates = True
    present = True
    prepared = None

    def __init__(self):
        self.form = ApplicationFormDescriptor("reviewed-contact", "联系人", "/crm/contact", (
            ApplicationFormField("name", "联系人姓名", binding="label"),
            ApplicationFormField("customer", "客户名称", kind="choice", controls=("combobox",),
                choices=(FormChoice("17", "晨岚机电"),), binding="label"),
            ApplicationFormField("mobile", "手机", binding="label"),
            ApplicationFormField("next", "下次联系时间", kind="datetime",
                controls=("combobox",), binding="label")), scope="new record",
            control_scope=DIALOG["form_scope"], submit_label="确 定")

    def plan_entry(self, reference, observation, *, context):
        if reference != "contact-create":
            raise BusinessToolRequestError("the reviewed target has no matching opener")
        matches = [e for e in observation.elements if e.name == "新增" and not e.attrs.get("disabled")]
        if len(matches) != 1:
            raise BusinessToolRequestError("the reviewed opener is ambiguous")
        return ApplicationFormEntry(reference, matches[0])

    def accept_entry(self, entry, before, receipt, observation, *, context):
        if any(e.attrs.get("form_scope") != self.form.control_scope for e in observation.elements):
            raise BusinessToolRequestError("another entity editor opened")
        return self.form.reference

    def discard_entry(self, entry, *, context):
        pass

    def describe_form(self, observation, *, context):
        return self.form if self.present and context.turn_id == "turn" else None

    def prepare_form(self, form, values, *, context):
        if form != self.form or not self.present:
            raise BusinessToolRequestError("the reviewed form changed")
        self.prepared = values
        from datetime import datetime
        return FormDraft(form.route, tuple(FormDraftField(item.field.reference, item.field.label,
            datetime.fromisoformat(item.value).isoformat(sep=" ", timespec="seconds")
                if item.field.kind == "datetime" else item.value,
            item.control, binding="label",
            option_label=next((c.label for c in item.field.choices if c.value == item.value), ""))
            for item in values), control_scope=form.control_scope, submit_label=form.submit_label)

class LiveFormTests(unittest.TestCase):
    def setUp(self):
        self.guard = NativePageGuard(frozenset(), writing_gets=frozenset())
        self.guard.begin(ACTOR, OWNER)
        self.reviewed = ReviewedEntry()
        self.port = LiveFormPort(FenceEvidence(self.guard, lambda: ACTOR), self.reviewed)
        now = self.now = int(time.time() * 1000)
        principal = PrincipalContext(1, "yudao", "1", "10", "2", "delegation", now - 100, now + 60000,
                                     ("yudao.read",))
        self.context = OperationContext.create(principal, session_id="session", turn_id="turn",
                                               operation_id="entry", deadline_ms=now + 30000)
        self.list = PageObservation(1, 1, "https://synthetic.test/crm/contact", "Contacts", [], [
            ElementRef("q1", "button", "搜索", "button", 1, QUERY),
            ElementRef("q2", "button", "新增", "button", 1, QUERY),
            ElementRef("r1", "button", "编辑", "button", 1)], render_state="ready",
            page_route="/crm/contact")
        self.form = replace(self.list, epoch=3, elements=[
            control("f1", "textbox", "请输入姓名", 3, "联系人姓名"),
            control("f2", "combobox", "请选择客户", 3, "客户名称"),
            control("f3", "textbox", "请输入手机", 3, "手机"),
            control("f4", "combobox", "选择下次联系时间", 3, "下次联系时间"),
            control("f5", "textbox", "", 3, "备注"), control("f6", "textbox", "", 3, "备注"),
            control("f7", "button", "确 定", 3), control("f8", "button", "取 消", 3)])

    def request(self, *pairs, mode="create", openers=(), aliases=None):
        names = tuple((field_key(label), (label, *(aliases or {}).get(label, ()))) for label, _ in pairs)
        values = tuple((field_key(label), value) for label, value in pairs)
        request = LiveRequest(f"live:{mode}:crm/contact:new:x", mode, "联系人", names, values, openers,
                              entity="crm/contact", route="/crm/contact")
        self.port.expect(request)
        return request

    def open(self, request, before=None, after=None):
        before, after = before or self.list, after or self.form
        entry = self.port.plan_entry(request.reference, before, context=self.context)
        receipt = ActionReceipt(1, "open", "click", {"ref": entry.control.ref, "epoch": before.epoch}, True,
                                write_guard_held=True, observation=after)
        return entry, self.port.accept_entry(entry, before, receipt, after, context=self.context)

    def refuse(self, method="POST", path="/admin-api/crm/contact/create"):
        with self.assertRaises(GuardRefused):
            with self.guard.request(ACTOR, method, path):
                self.fail("the fence admitted a write")

    def test_opener_comes_from_review_not_request_supplied_search(self):
        request = self.request(("手机", "1"), openers=("搜索",))
        entry, reference = self.open(request)
        self.assertEqual(entry.control.ref, "q2")
        self.assertTrue(reference.endswith("#" + DIALOG["form_instance"]))

    def test_unreviewed_editor_cannot_schedule_delayed_autosave(self):
        # No refusals yet is no proof about callbacks after the fence ends.
        self.reviewed.goal_entries = ()
        request = self.request(("手机", "1"))
        with self.assertRaises(BusinessToolRequestError):
            self.port.plan_entry(request.reference, self.list, context=self.context)
        self.guard.end(ACTOR, OWNER)
        self.assertIsNone(self.port._planned)
        self.assertIsNone(self.port._bound)
        self.assertIsNone(self.reviewed.prepared)

    def test_customer_target_cannot_use_contact_editor_or_another_record(self):
        request = LiveRequest("r", "update", "客户", ((field_key("手机"), ("手机",)),),
            ((field_key("手机"), "1"),), entity="crm/customer",
            route="/crm/customer/detail/7", record_id="7")
        self.port.expect(request)
        before = replace(self.list, url="https://synthetic.test/crm/customer/detail/7",
                         page_route="/crm/customer/detail/7", record_route="/crm/customer/detail/7")
        with self.assertRaises(BusinessToolRequestError):
            self.port.plan_entry("r", before, context=self.context)
        self.assertFalse(self.port.entry_reference(replace(request, record_id="8")))

    def test_field_numbers_names_and_prefixes_are_not_semantic_aliases(self):
        request = self.request(("客户名称", "x"), ("所属手机", "1"))
        self.open(request)
        self.assertEqual(self.port._match(self.port._bound, ["客户编号", "手机"]), {})
        self.assertEqual(self.port._match(self.port._bound, ["客户姓名", "手机号码"]), {})
        self.port._bound.request = replace(request, fields=(("first", ("手机",)), ("second", ("手机",))))
        self.assertEqual(self.port._match(self.port._bound, ["手机"]), {})
        self.port._bound.request = replace(request, fields=(("first", ("手机", "邮箱")),))
        self.assertEqual(self.port._match(self.port._bound, ["手机", "邮箱"]), {})

    def test_only_the_planned_opener_is_admitted_and_one_new_dialog_is_accepted(self):
        policy = self.port.click_policy()
        request = self.request(("手机", "1"))
        self.assertTrue(policy.decide(self.list, self.list.elements[1]))
        entry = self.port.plan_entry(request.reference, self.list, context=self.context)
        self.assertEqual(policy.decide(self.list, entry.control), "")
        self.assertTrue(policy.decide(self.list, self.list.elements[2]))
        self.assertTrue(policy.decide(replace(self.list, url="https://synthetic.test/other"), entry.control))
        receipt = ActionReceipt(1, "open", "click", {"ref": "q2", "epoch": 1}, True,
                                write_guard_held=True, observation=self.form)
        two = replace(self.form, elements=[*self.form.elements, ElementRef(
            "x1", "textbox", "", "input", 3, {"form_scope": "dialog:other", "form_instance": "b" * 8})])
        for after in (two, replace(self.form, elements=[]), replace(self.form, render_state="pending"),
                      replace(self.form, url="https://synthetic.test/crm/contact/detail/1")):
            entry = self.port.plan_entry(request.reference, self.list, context=self.context)
            with self.assertRaises(BusinessToolRequestError):
                self.port.accept_entry(entry, self.list, receipt, after, context=self.context)
        # A dialog that was already there is not the one the opener showed.
        with self.assertRaises(BusinessToolRequestError):
            self.port.plan_entry(request.reference, self.form, context=self.context)
        self.assertTrue(policy.decide(self.list, entry.control))

    def test_requested_labels_bind_by_tier_and_the_loosest_one_is_reported(self):
        request = self.request(("联系人姓名", "林沐"), ("客户", "晨岚机电"), ("手机号", "13800001001"),
                               ("下次联系时间", "2026-10-09 10:00"), ("备注", "x"), ("邮箱", "a@x.invalid"),
                               aliases={"客户": ("客户名称",), "手机号": ("手机",)})
        _, reference = self.open(request)
        form = self.port.describe_form(self.form, context=self.context)
        self.assertEqual(form.reference, reference)
        self.assertEqual((form.control_scope, form.submit_label), (DIALOG["form_scope"], "确 定"))
        self.assertEqual(set(self.port.matched), {("联系人姓名", "联系人姓名", "exact"), ("客户", "客户名称", "alias"),
                                                   ("手机号", "手机", "alias"),
                                                   ("下次联系时间", "下次联系时间", "exact")})
        # A label two controls answer to is nobody's; a label the form lacks stays unmatched.
        self.assertEqual(self.port.unmatched, ("备注", "邮箱"))
        fields = {f.label: f for f in form.fields}
        self.assertNotIn("备注", fields)
        self.assertEqual({k: (f.kind, f.binding) for k, f in fields.items()},
                         {"联系人姓名": ("text", "label"), "客户名称": ("choice", "label"),
                          "手机": ("text", "label"), "下次联系时间": ("datetime", "label")})
        self.assertEqual([(c.value, c.label) for c in fields["客户名称"].choices], [("17", "晨岚机电")])

    def test_draft_is_label_bound_and_only_carries_what_was_asked(self):
        request = self.request(("联系人姓名", "林沐"), ("客户", "晨岚机电"), ("下次联系时间", "2026-10-09T10:00"),
                               aliases={"客户": ("客户名称",)})
        _, reference = self.open(request)
        tools = ApplicationFormTools(self.port, application_id="yudao")
        draft, _ = tools.prepare_current(self.form, reference=reference, values=tuple(
            (key, value) for key, value in request.values), context=self.context)
        self.assertEqual([(f.label, f.value, f.option_label, f.binding) for f in draft.fields],
                         [("联系人姓名", "林沐", "", "label"), ("客户名称", "17", "晨岚机电", "label"),
                          ("下次联系时间", "2026-10-09 10:00:00", "", "label")])
        self.assertEqual((draft.control_scope, draft.submit_label), (DIALOG["form_scope"], "确 定"))
        # The application may hide a field once a choice is made; the form that was
        # prepared stays the one described while its submit control is still there.
        fewer = replace(self.form, epoch=4, elements=[replace(e, epoch=4) for e in self.form.elements
                                                      if e.ref not in ("f3", "f4")])
        self.assertIsNotNone(self.port.describe_form(fewer, context=self.context))
        gone = replace(fewer, elements=[e for e in fewer.elements if e.ref != "f7"])
        self.assertIsNone(self.port.describe_form(gone, context=self.context))

    def test_a_refused_write_since_the_opener_stops_the_draft(self):
        request = self.request(("手机", "13800001001"))
        self.refuse()   # before the opener was planned: not this form's doing
        _, reference = self.open(request)
        self.assertIsNotNone(self.port.describe_form(self.form, context=self.context))
        self.refuse("PUT", "/admin-api/crm/contact/update")
        with self.assertRaises(BusinessToolRequestError) as stopped:
            self.port.describe_form(self.form, context=self.context)
        self.assertIn("page fence refused", str(stopped.exception))
        form = self.port._bound.described
        with self.assertRaises(BusinessToolRequestError):
            self.port.prepare_form(form, (), context=self.context)

    def test_another_turn_instance_or_missing_submit_describes_nothing(self):
        _, reference = self.open(self.request(("手机", "1")))
        other = OperationContext.create(self.context.principal, session_id="session", turn_id="later",
                                        operation_id="entry", deadline_ms=self.now + 30000)
        self.assertIsNone(self.port.describe_form(self.form, context=other))
        self.assertIsNone(self.port.describe_form(self.form, context=None))
        replaced = replace(self.form, elements=[replace(e, attrs={**e.attrs, "form_instance": "b" * 8})
                                                for e in self.form.elements])
        self.assertIsNone(self.port.describe_form(replaced, context=self.context))
        unsure = replace(self.form, elements=[*self.form.elements, control("f9", "button", "确 定", 3)])
        self.assertIsNone(self.port.describe_form(unsure, context=self.context))
        with self.assertRaises(TypeError):
            LiveFormPort(object(), self.reviewed)
        with self.assertRaises(ValueError):
            self.port.expect(LiveRequest("r", "delete", "", (), ()))


if __name__ == "__main__":
    unittest.main()
