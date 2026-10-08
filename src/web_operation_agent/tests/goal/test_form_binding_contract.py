"""Requested fields tied to an open form's own labels; what the form lacks is named, not guessed."""
from contextlib import nullcontext
import json
from types import SimpleNamespace
import unittest

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.forms import ApplicationFormBindingPort
from web_operation_agent.execution.form_tools import apply_form_draft
from web_operation_agent.business.runtime import FormDraft, FormDraftField
from web_operation_agent.goal.bind_fields import choose_labels
from web_operation_agent.goal.executor import _PageOutcome, _achieved, _render
from web_operation_agent.goal.forms import _shown_fields
from web_operation_agent.goal.ports import FormPlan, PagePlan, Resolved
from web_operation_agent.modeling.provider import ProviderError
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation


class Chat:
    """Answers bind_form_fields with what the test scripted, and keeps what it was asked."""

    def __init__(self, bindings=None, error=None):
        self.bindings, self.error, self.asked = bindings, error, []

    def complete_cancellable(self, messages, tools, run_id, **kwargs):
        self.asked.append((messages, tools))
        if self.error is not None:
            raise self.error
        return {"tool_calls": [{"id": "c1", "function": {
            "name": "bind_form_fields",
            "arguments": json.dumps({"bindings": self.bindings}, ensure_ascii=False)}}]}

    complete = complete_cancellable


class Port:
    """A form that shows Name, Category and Price; the request asked by other names."""
    required_scopes = frozenset()

    def __init__(self, missing, free):
        self.missing, self.free, self.bound = list(missing), list(free), []

    def unbound_fields(self, reference, observation, *, context):
        return tuple(self.missing), tuple(self.free)

    def bind_fields(self, reference, bindings, *, context):
        self.bound.append(bindings)
        for key, label in bindings:
            self.missing = [item for item in self.missing if item[0] != key]
            self.free = [item for item in self.free if item[0] != label]


def state(chat):
    events = []
    return SimpleNamespace(
        approvals=SimpleNamespace(context=lambda step, name: object()), step=3, obs=object(),
        receipt=SimpleNamespace(stage=lambda name: nullcontext()), chat=chat, run_id="r",
        cancelled=lambda: False, budget=SimpleNamespace(remaining=lambda: 30.0),
        ledger=SimpleNamespace(append=lambda event, **kw: events.append((event, kw)))), events


def executor(port):
    return SimpleNamespace(application=SimpleNamespace(ports=SimpleNamespace(forms=port)))


VALUES = (("k-name", "缠绕膜"), ("k-category", "包装材料"), ("k-lines", "胶带 10卷"))


class ChooseLabels(unittest.TestCase):
    def test_only_offered_names_and_labels_are_kept_and_each_label_once(self):
        chat = Chat([{"field": "产品分类", "label": "分类"}, {"field": "产品信息", "label": "分类"},
                     {"field": "没问过", "label": "条码"}, {"field": "产品信息", "label": "不存在"}, "junk"])
        chosen = choose_labels(chat, requested=(("产品分类", "包装材料"), ("产品信息", "胶带 10卷")),
                               labels=(("分类", "combobox"), ("条码", "textbox")), run_id="r",
                               cancelled=lambda: False)
        self.assertEqual(chosen, {"产品分类": "分类"})
        tool = chat.asked[0][1][0]["function"]["parameters"]["properties"]["bindings"]["items"]["properties"]
        self.assertEqual(tool["field"]["enum"], ["产品分类", "产品信息"])
        self.assertEqual(tool["label"]["enum"], ["分类", "条码"])
        self.assertIn("UNTRUSTED_FORM_LABELS", chat.asked[0][0][1]["content"])

    def test_nothing_to_ask_asks_nothing(self):
        chat = Chat([])
        self.assertEqual(choose_labels(chat, requested=(), labels=(("分类", "combobox"),), run_id="r"), {})
        self.assertEqual(choose_labels(chat, requested=(("产品分类", "x"),), labels=(), run_id="r"), {})
        self.assertEqual(chat.asked, [])


class ShownFields(unittest.TestCase):
    def test_a_port_without_binding_is_prepared_as_planned(self):
        s, events = state(Chat([]))
        self.assertEqual(_shown_fields(executor(object()), s, "ref", VALUES), (VALUES, ()))
        self.assertEqual(events, [])

    def test_a_chosen_label_is_bound_and_what_the_form_lacks_is_left_out_by_name(self):
        port = Port([("k-category", "产品分类"), ("k-lines", "产品信息")],
                    [("分类", "combobox"), ("条码", "textbox")])
        self.assertIsInstance(port, ApplicationFormBindingPort)
        chat = Chat([{"field": "产品分类", "label": "分类"}])
        s, events = state(chat)
        values, unfilled = _shown_fields(executor(port), s, "ref", VALUES)
        self.assertEqual(values, VALUES[:2])
        self.assertEqual(unfilled, ("产品信息",))
        self.assertEqual(port.bound, [(("k-category", "分类"),)])
        asked = chat.asked[0][0][1]["content"]
        self.assertIn("- 产品分类: 包装材料", asked)
        self.assertIn("- 产品信息: 胶带 10卷", asked)
        self.assertNotIn("缠绕膜", asked)   # a field the form already shows is not asked about
        self.assertEqual(events[0], ("goal_form_fields_bound",
                                     {"step": 3, "asked": 2, "labels": 2, "bound": 1}))

    def test_no_free_label_or_a_failed_call_leaves_the_fields_out_without_binding(self):
        for port, chat in ((Port([("k-lines", "产品信息")], []), Chat([])),
                           (Port([("k-lines", "产品信息")], [("条码", "textbox")]),
                            Chat(error=ProviderError("down"))),
                           (Port([("k-lines", "产品信息")], [("条码", "textbox")]), Chat("not a list"))):
            with self.subTest(free=port.free, error=chat.error):
                s, _ = state(chat)
                values, unfilled = _shown_fields(executor(port), s, "ref", VALUES)
                self.assertEqual(values, (VALUES[0], VALUES[1]))
                self.assertEqual(unfilled, ("产品信息",))
                self.assertEqual(port.bound, [])

    def test_a_port_that_refuses_is_not_recovered_here(self):
        class Refusing(Port):
            def unbound_fields(self, reference, observation, *, context):
                raise BusinessToolRequestError("the live form is absent or changed")
        s, _ = state(Chat([]))
        with self.assertRaises(BusinessToolRequestError):
            _shown_fields(executor(Refusing([], [])), s, "ref", VALUES)


class PartialHandoffReply(unittest.TestCase):
    PLAN = PagePlan(url="/p", label="产品", form=FormPlan("ref", (("k", "v"),)))

    def test_a_form_with_a_field_left_out_is_handed_over_but_is_not_the_whole_request(self):
        resolved = Resolved(kind="handoff", page=self.PLAN)
        outcome = _PageOutcome(form_requested=True, form_complete=True, form_fields=("名称", "分类"),
                               form_submit="确 定", form_input_dispatched=True,
                               form_unfilled=("产品信息",))
        self.assertFalse(_achieved(resolved, outcome))
        text = "\n".join(_render(resolved, outcome, True))
        self.assertIn("已预填：名称、分类", text)
        self.assertIn("没有找到「产品信息」对应的字段", text)
        english = "\n".join(_render(resolved, outcome, False))
        self.assertIn("no field for “产品信息”", english)
        outcome.form_unfilled = ()
        self.assertTrue(_achieved(resolved, outcome))
        self.assertNotIn("未填写", "\n".join(_render(resolved, outcome, True)))


SCOPE = "dialog:editor"


def page(epoch, *, covered=False, value=""):
    elements = [ElementRef("e1", "combobox", "选择日期", "input", epoch,
                           {"field_label": "日期", "form_scope": SCOPE, "value": value})]
    if not covered:
        elements.append(ElementRef("e2", "button", "确 定", "button", epoch, {"form_scope": SCOPE}))
    return PageObservation(schema_version=1, epoch=epoch, url="https://app.test/records",
                           title="Editor", landmarks=[], elements=elements, render_state="ready")


class LookAgainAfterAPopup(unittest.TestCase):
    """A calendar still closing over the form hides its submit control for a moment."""
    DRAFT = FormDraft("/records", (FormDraftField("date", "日期", "2026-11-05", "combobox",
                                                  binding="label"),),
                      control_scope=SCOPE, submit_label="确 定")

    def run_draft(self, covered_for):
        calls, events = [], []

        def execute(action, args):
            calls.append(action)
            epoch = len(calls) + 1
            covered = action in ("fill", "wait_for") and calls.count("wait_for") < covered_for
            return ActionReceipt(1, "c" + str(epoch), action, {}, True,
                                 observation=page(epoch, covered=covered, value="2026-11-05"))

        def verify(observation):
            if not any(e.role == "button" and e.name == "确 定" for e in observation.elements):
                raise BusinessToolRequestError("the application changed the prepared form entry")

        session = SimpleNamespace(
            _cancel_requested=lambda: False, _browser_attempts=0, page_actions=0,
            _approvals=SimpleNamespace(confirm_operation=lambda *a, **k: ("", ""),
                                       settle_operation=lambda: None),
            _writes=SimpleNamespace(write_outcome=lambda *a, **k: {"state": "not_written"},
                                    settle_write=lambda write: ("", "")),
            ledger=SimpleNamespace(append=lambda event, **kw: events.append(event)),
            browser=SimpleNamespace(execute=execute))
        _, reason, detail = apply_form_draft(session, self.DRAFT, page(1), 1, verify=verify)
        return reason, detail, calls, events

    def test_the_form_is_verified_once_the_popup_has_closed(self):
        for waits in (1, 2):
            with self.subTest(waits=waits):
                reason, _, calls, events = self.run_draft(waits)
                self.assertEqual(reason, "")
                self.assertEqual(calls, ["fill"] + ["wait_for"] * waits + ["handoff_form"])
                self.assertEqual(calls.count("fill"), 1)   # looking again never enters the value again
                self.assertNotIn("form_entry_verification_refused", events)

    def test_a_form_that_stays_changed_is_still_refused_and_recorded_once(self):
        reason, detail, calls, events = self.run_draft(9)
        self.assertEqual(reason, "agent_refused")
        self.assertIn("changed the prepared form", detail)
        self.assertEqual(calls, ["fill", "wait_for", "wait_for"])
        self.assertEqual(events.count("form_entry_verification_refused"), 1)

    def test_a_form_already_verified_waits_for_nothing(self):
        reason, _, calls, _ = self.run_draft(0)
        self.assertEqual((reason, calls), ("", ["fill", "handoff_form"]))


class UnofferedChoice(unittest.TestCase):
    """A choice its own list does not offer is left unchosen; the rest of the form is entered."""
    DRAFT = FormDraft("/records", (
        FormDraftField("kind", "跟进类型", "电话", "combobox", binding="label", option_label="电话"),
        FormDraftField("date", "日期", "2026-11-05", "combobox", binding="label")),
        control_scope=SCOPE, submit_label="确 定")

    @staticmethod
    def form(epoch, value=""):
        return PageObservation(schema_version=1, epoch=epoch, url="https://app.test/records", title="Editor",
                               landmarks=[], render_state="ready", elements=[
            ElementRef("e0", "combobox", "请选择跟进类型", "input", epoch,
                       {"field_label": "跟进类型", "form_scope": SCOPE}),
            ElementRef("e1", "combobox", "选择日期", "input", epoch,
                       {"field_label": "日期", "form_scope": SCOPE, "value": value}),
            ElementRef("e2", "button", "确 定", "button", epoch, {"form_scope": SCOPE})])

    def run_draft(self, *, unoffered, code="option_unavailable", draft=None):
        calls = []

        def execute(action, args):
            calls.append((action, args))
            epoch = len(calls) + 1
            missing = action == "select"
            return ActionReceipt(1, "c" + str(epoch), action, {}, not missing,
                                 error_code=code if missing else "",
                                 observation=self.form(epoch, "2026-11-05" if len(calls) > 2 else ""))
        session = SimpleNamespace(
            _cancel_requested=lambda: False, _browser_attempts=0, page_actions=0,
            _approvals=SimpleNamespace(confirm_operation=lambda *a, **k: ("", ""), settle_operation=lambda: None),
            _writes=SimpleNamespace(write_outcome=lambda *a, **k: {"state": "not_written"},
                                    settle_write=lambda write: ("", "")),
            ledger=SimpleNamespace(append=lambda *a, **k: None), browser=SimpleNamespace(execute=execute))
        _, reason, detail = apply_form_draft(session, draft or self.DRAFT, self.form(1), 1, unoffered=unoffered)
        return reason, detail, calls

    def test_the_rest_is_entered_and_handed_over_without_the_unoffered_choice(self):
        unoffered = []
        reason, _, calls = self.run_draft(unoffered=unoffered)
        self.assertEqual(reason, "")
        self.assertEqual([a for a, _ in calls], ["select", "wait_for", "fill", "handoff_form"])
        self.assertEqual(unoffered, [("跟进类型", "电话")])
        self.assertEqual([f["field"] for f in calls[-1][1]["fields"]], ["date"])

    def test_without_the_list_or_for_any_other_failure_the_draft_ends_as_before(self):
        reason, detail, calls = self.run_draft(unoffered=None)
        self.assertEqual((reason, [a for a, _ in calls]), ("agent_refused", ["select"]))
        unoffered = []
        reason, _, calls = self.run_draft(unoffered=unoffered, code="page_error")
        self.assertEqual((reason, [a for a, _ in calls], unoffered), ("agent_refused", ["select"], []))

    def test_a_form_that_offered_none_of_the_choices_is_not_handed_over(self):
        unoffered = []
        only = FormDraft("/records", self.DRAFT.fields[:1], control_scope=SCOPE, submit_label="确 定")
        reason, detail, calls = self.run_draft(unoffered=unoffered, draft=only)
        self.assertEqual(reason, "agent_refused")
        self.assertNotIn("handoff_form", [a for a, _ in calls])
        self.assertEqual(unoffered, [("跟进类型", "电话")])

    def test_the_reply_names_the_choice_and_the_turn_is_not_finished(self):
        resolved = Resolved(kind="handoff", page=PartialHandoffReply.PLAN)
        outcome = _PageOutcome(form_requested=True, form_complete=True, form_fields=("日期",),
                               form_submit="确 定", form_input_dispatched=True,
                               form_unoffered=(("跟进类型", "电话"),))
        self.assertFalse(_achieved(resolved, outcome))
        text = "\n".join(_render(resolved, outcome, True))
        self.assertIn("已预填：日期", text)
        self.assertIn("「跟进类型」的选项里没有「电话」", text)


if __name__ == "__main__":
    unittest.main()
