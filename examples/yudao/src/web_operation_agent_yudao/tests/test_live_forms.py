"""No models or accounts: a form the page shows is bound by its labels, under the fence."""
from dataclasses import replace
import time
import unittest

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent_yudao.live_forms import (FenceEvidence, FenceReachPolicy, LiveFormPort,
                                                  LiveRequest, field_key)
from web_operation_agent_yudao.page_guard import GuardRefused, NativePageGuard

ACTOR, OWNER = "a" * 64, "c" * 64
DIALOG = {"form_scope": "dialog_describedby:el-1", "form_instance": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
QUERY = {"form_scope": "form_instance:q", "form_instance": "q"}


def control(ref, role, name, epoch, label="", **attrs):
    return ElementRef(ref, role, name, "input", epoch,
                      {**DIALOG, **({"field_label": label} if label else {}), **attrs})


class LiveFormTests(unittest.TestCase):
    def setUp(self):
        self.guard = NativePageGuard(frozenset(), writing_gets=frozenset())
        self.guard.begin(ACTOR, OWNER)
        self.port = LiveFormPort(FenceEvidence(self.guard, lambda: ACTOR))
        now = self.now = int(time.time() * 1000)
        principal = PrincipalContext(1, "yudao", "1", "10", "2", "delegation", now - 100, now + 60000,
                                     ("yudao.read",))
        self.context = OperationContext.create(principal, session_id="session", turn_id="turn",
                                               operation_id="entry", deadline_ms=now + 30000)
        self.list = PageObservation(1, 1, "https://synthetic.test/crm/contact", "Contacts", [], [
            ElementRef("q1", "button", "搜索", "button", 1, QUERY),
            ElementRef("q2", "button", "新增", "button", 1, QUERY),
            ElementRef("q3", "textbox", "请输入姓名", "input", 1, {**QUERY, "field_label": "姓名"}),
            ElementRef("r1", "button", "编辑", "button", 1)], render_state="ready",
            page_route="/crm/contact")
        self.form = replace(self.list, epoch=3, elements=[
            control("f1", "textbox", "请输入姓名", 3, "联系人姓名", value="沈秋"),
            control("f3", "textbox", "请输入手机", 3, "手机"),
            control("f5", "textbox", "", 3, "备注"),
            control("f7", "button", "确 定", 3), control("f8", "button", "取 消", 3)])

    def request(self, *pairs, mode="create", openers=(), record=""):
        names = tuple((field_key(label), (label,)) for label, _ in pairs)
        values = tuple((field_key(label), value) for label, value in pairs)
        request = LiveRequest(f"live:{mode}:crm/contact:x", mode, "联系人", names, values, openers, record)
        self.port.expect(request)
        return request

    def receipt(self, entry, before, after):
        return ActionReceipt(1, "open", "click", {"ref": entry.control.ref, "epoch": before.epoch}, True,
                             write_guard_held=True, observation=after)

    def open(self, request, before=None, after=None):
        before, after = before or self.list, after or self.form
        entry = self.port.plan_entry(request.reference, before, context=self.context)
        return entry, self.port.accept_entry(entry, before, self.receipt(entry, before, after), after,
                                             context=self.context)

    def refuse_a_write(self):
        with self.assertRaises(GuardRefused):
            with self.guard.request(ACTOR, "POST", "/admin-api/crm/contact/create"):
                self.fail("the fence admitted a write")

    def test_the_pages_one_create_button_opens_exactly_one_new_dialog(self):
        entry, reference = self.open(self.request(("手机", "13800000000")))
        self.assertEqual(entry.control.ref, "q2")
        self.assertTrue(reference.endswith("#" + DIALOG["form_instance"]))

    def test_two_matching_openers_or_an_open_dialog_are_refused(self):
        request = self.request(("手机", "1"))
        twice = replace(self.list, elements=[*self.list.elements,
                                             ElementRef("q9", "button", "新增", "button", 1, QUERY)])
        with self.assertRaisesRegex(BusinessToolRequestError, "more than one"):
            self.port.plan_entry(request.reference, twice, context=self.context)
        with self.assertRaisesRegex(BusinessToolRequestError, "another dialog"):
            self.port.plan_entry(request.reference, self.form, context=self.context)

    def test_an_opener_that_shows_no_new_form_is_not_accepted(self):
        request = self.request(("手机", "1"))
        entry = self.port.plan_entry(request.reference, self.list, context=self.context)
        with self.assertRaisesRegex(BusinessToolRequestError, "exactly one new form"):
            self.port.accept_entry(entry, self.list, self.receipt(entry, self.list, self.list),
                                   replace(self.list, epoch=2, elements=[replace(e, epoch=2) for e in self.list.elements]),
                                   context=self.context)

    def test_a_control_the_server_found_may_open_the_form_but_never_an_input_or_a_submit(self):
        request = self.request(("手机", "1"), mode="update", record="沈秋")
        row = self.list.elements[3]
        entry = self.port.offer_entry(request.reference, self.list, row, context=self.context)
        self.assertEqual(entry.control, row)
        with self.assertRaisesRegex(BusinessToolRequestError, "cannot open"):
            self.port.offer_entry(request.reference, self.list, self.list.elements[2], context=self.context)
        with self.assertRaisesRegex(BusinessToolRequestError, "cannot open"):
            self.port.offer_entry(request.reference, self.form, self.form.elements[3], context=self.context)

    def test_an_editor_found_on_the_page_must_show_the_record_that_was_meant(self):
        request = self.request(("手机", "1"), mode="update", record="沈秋")
        row = self.list.elements[3]
        entry = self.port.offer_entry(request.reference, self.list, row, context=self.context)
        self.port.accept_entry(entry, self.list, self.receipt(entry, self.list, self.form), self.form,
                               context=self.context)
        # A name several records share arrives with what tells it apart; the form shows the name.
        qualified = self.request(("手机", "1"), mode="update", record="沈秋（采购）（杭州云帆科技有限公司）")
        entry = self.port.offer_entry(qualified.reference, self.list, row, context=self.context)
        self.port.accept_entry(entry, self.list, self.receipt(entry, self.list, self.form), self.form,
                               context=self.context)
        other = self.request(("手机", "1"), mode="update", record="另一个人")
        entry = self.port.offer_entry(other.reference, self.list, row, context=self.context)
        with self.assertRaisesRegex(BusinessToolRequestError, "does not show the requested record"):
            self.port.accept_entry(entry, self.list, self.receipt(entry, self.list, self.form), self.form,
                                   context=self.context)

    def test_requested_fields_bind_by_label_and_a_field_the_form_lacks_is_named(self):
        _, reference = self.open(self.request(("手机", "13800000000"), ("微信", "wx")))
        form = self.port.describe_form(self.form, context=self.context)
        self.assertEqual(form.reference, reference)
        self.assertEqual(form.submit_label, "确 定")
        self.assertEqual(self.port.unmatched, ("微信",))
        self.assertEqual([m[:2] for m in self.port.matched], [("手机", "手机")])
        missing, free = self.port.unbound_fields(reference, self.form, context=self.context)
        self.assertEqual([name for _, name in missing], ["微信"])
        self.assertIn("备注", [label for label, _ in free])

    def test_a_field_can_be_bound_only_to_a_label_the_form_offered(self):
        _, reference = self.open(self.request(("手机", "1"), ("说明", "回访")))
        missing, free = self.port.unbound_fields(reference, self.form, context=self.context)
        key = missing[0][0]
        with self.assertRaises(BusinessToolRequestError):
            self.port.bind_fields(reference, ((key, "不存在的标签"),), context=self.context)
        self.port.bind_fields(reference, ((key, "备注"),), context=self.context)
        self.port.describe_form(self.form, context=self.context)
        self.assertEqual(self.port.unmatched, ())
        self.assertIn(("说明", "备注"), [m[:2] for m in self.port.matched])

    def test_a_write_the_form_attempted_since_its_opener_stops_the_draft(self):
        self.open(self.request(("手机", "1")))
        self.assertIsNotNone(self.port.describe_form(self.form, context=self.context))
        self.refuse_a_write()
        with self.assertRaisesRegex(BusinessToolRequestError, "tried to save"):
            self.port.describe_form(self.form, context=self.context)

    def test_another_turn_or_a_replaced_form_describes_nothing(self):
        self.open(self.request(("手机", "1")))
        later = OperationContext.create(self.context.principal, session_id="session", turn_id="next",
                                        operation_id="entry", deadline_ms=self.now + 30000)
        self.assertIsNone(self.port.describe_form(self.form, context=later))
        replaced = replace(self.form, elements=[replace(e, attrs={**e.attrs, "form_instance": "b" * 8})
                                                for e in self.form.elements])
        self.assertIsNone(self.port.describe_form(replaced, context=self.context))

    def test_an_open_form_is_taken_as_it_stands_for_the_fields_asked(self):
        reference, values = self.port.adopt_open(self.form, (("手机", "13900000000"),), context=self.context)
        self.assertTrue(reference.startswith("live:open:" + DIALOG["form_instance"]))
        self.assertEqual(values, ((field_key("手机"), "13900000000"),))
        self.assertIsNotNone(self.port.describe_form(self.form, context=self.context))
        with self.assertRaises(BusinessToolRequestError):
            self.port.adopt_open(self.list, (("手机", "1"),), context=self.context)

    def test_the_page_loop_may_press_what_a_person_presses_but_not_inputs_or_a_dialogs_submit(self):
        policy = FenceReachPolicy()
        # An empty answer admits the press; anything else is the reason it is refused.
        self.assertEqual(policy.decide(self.list, self.list.elements[3]), "")
        self.assertTrue(policy.decide(self.list, self.list.elements[2]))
        self.assertTrue(policy.decide(self.form, self.form.elements[3]))
        self.assertEqual(policy.decide(self.form, self.form.elements[4]), "")


if __name__ == "__main__":
    unittest.main()
