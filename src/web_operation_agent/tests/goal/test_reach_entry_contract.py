"""Where the page loop is used: a form no opener is named for, a record that has no address."""
from contextlib import nullcontext
import json
from types import SimpleNamespace
import unittest

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.forms import ApplicationFormEntry, ApplicationFormOfferPort
from web_operation_agent.business.runtime import FormDraft, FormDraftField
from web_operation_agent.business.tools.contracts import validate_form_scope
from web_operation_agent.execution.form_tools import apply_form_draft
from web_operation_agent.goal.executor import GoalTurnExecutor, _PageOutcome, _achieved, _render
from web_operation_agent.goal.forms import _found_entry, form_failure_message
from web_operation_agent.goal.ports import FormPlan, PagePlan, Resolved
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation

READ = {"path": "/list", "query_digest": "0" * 64, "query_fields": {}, "revision": 1,
        "page": 1, "page_size": 10, "rows": 1, "total": 1}


def listing(epoch, *, rows=3, content="苏州备件仓 杭州总仓", form=""):
    elements = [ElementRef("e1", "button", "搜索", "button", epoch, {"page_control": "query"}),
                ElementRef("e2", "button", "编辑", "button", epoch),
                ElementRef("e3", "button", "更多", "button", epoch, {"revealable": "true"})]
    if form:
        elements.append(ElementRef("e9", "textbox", "名称", "input", epoch,
                                   {"field_label": "名称", "form_scope": "dialog:d", "form_instance": form}))
    return PageObservation(schema_version=1, epoch=epoch, url="https://app.test/list", title="仓库",
                           landmarks=[], elements=elements, content=content, render_state="ready",
                           page_read={**READ, "rows": rows, "total": rows})


def steps(*items):
    replies = [{"tool_calls": [{"id": "s", "function": {"name": "page_step", "arguments": json.dumps(
        {"why": "test", **item}, ensure_ascii=False)}}]} for item in items]
    asked = []

    def complete(messages, tools, run_id, **kwargs):
        asked.append(messages[1]["content"])
        return replies.pop(0)
    return SimpleNamespace(complete_cancellable=complete, asked=asked)


class Executor:
    """Dispatches to a scripted page: each click shows the next of `pages`."""
    page_reach_steps = 4
    page_reach_policy = None

    def __init__(self, chat, pages):
        self.pages, self.calls, self.events = list(pages), [], []
        self.s = SimpleNamespace(
            terminal_reason="", obs=listing(1), step=0, chat=chat, run_id="r", cancelled=lambda: False,
            budget=SimpleNamespace(remaining=lambda: 30.0),
            receipt=SimpleNamespace(stage=lambda name: nullcontext()),
            approvals=SimpleNamespace(context=lambda step, name: name),
            ledger=SimpleNamespace(append=lambda event, **kw: self.events.append((event, kw))))

    def _handoffs(self):
        return None

    def _dispatch(self, s, action, args, **kwargs):
        self.calls.append(action)
        if action != "wait_for" and self.pages:
            s.obs = self.pages.pop(0)(s.obs.epoch + 1)
        return ActionReceipt(1, "c" + str(len(self.calls)), action, dict(args), True, observation=s.obs)


class OfferPort:
    """Admits anything but 搜索; accepts once a form instance named `wanted` is shown."""

    def __init__(self, wanted="right"):
        self.wanted, self.offered, self.discarded = wanted, [], 0

    def plan_entry(self, reference, observation, *, context):
        raise BusinessToolRequestError("this page shows no form opener for the request")

    def offer_entry(self, reference, observation, control, *, context):
        if control.name == "搜索":
            raise BusinessToolRequestError("this control cannot open the requested form")
        self.offered.append(control.name)
        return ApplicationFormEntry(reference, control)

    def accept_entry(self, entry, before, receipt, observation, *, context):
        if not any(e.attrs.get("form_instance") == self.wanted for e in observation.elements):
            raise BusinessToolRequestError("the opener did not show exactly one new form")
        return entry.reference + "#bound"

    def discard_entry(self, entry, *, context):
        self.discarded += 1


PLAN = PagePlan(url="/list", label="仓库", form=FormPlan("ref", (("k", "v"),), open_entry=True,
                                                         title="仓库", record="苏州备件仓"))


class FoundEntry(unittest.TestCase):
    def test_the_port_is_offered_each_press_and_accepts_the_form_that_opened(self):
        port = OfferPort()
        self.assertIsInstance(port, ApplicationFormOfferPort)
        chat = steps({"action": "click", "element": 1}, {"action": "click", "element": 2})
        executor = Executor(chat, [lambda epoch: listing(epoch, form="right")])
        self.assertEqual(_found_entry(executor, executor.s, port, PLAN, "ref"), "ref#bound")
        self.assertEqual(port.offered, ["编辑"])              # 搜索 was refused before any press
        self.assertEqual(executor.calls.count("click"), 1)
        self.assertEqual(port.discarded, 1)
        self.assertIn('edits the 仓库 record named "苏州备件仓"', chat.asked[0])
        self.assertIn("NAMES\n- 苏州备件仓", chat.asked[0])
        self.assertIn("refused: this control cannot be pressed for this", chat.asked[1])

    def test_a_press_that_opens_no_form_goes_on_and_a_wrong_form_ends_the_search_unfilled(self):
        port = OfferPort()
        chat = steps({"action": "click", "element": 2}, {"action": "click", "element": 2})
        executor = Executor(chat, [lambda epoch: listing(epoch, content="a tab switched"),
                                   lambda epoch: listing(epoch, form="another-record")])
        self.assertEqual(_found_entry(executor, executor.s, port, PLAN, "ref"), "")
        self.assertEqual(port.discarded, 2)
        reached = [kw for event, kw in executor.events if event == "goal_form_entry_reached"][0]
        self.assertFalse(reached["arrived"])
        self.assertIn("not the one asked for", reached["why"])

    def test_a_new_form_has_no_record_to_name(self):
        chat = steps({"action": "stop"})
        executor = Executor(chat, [])
        plan = PagePlan(url="/list", label="请假", form=FormPlan("ref", (("k", "v"),), open_entry=True, title="请假"))
        self.assertEqual(_found_entry(executor, executor.s, OfferPort(), plan, "ref"), "")
        self.assertIn("creates a new 请假", chat.asked[0])
        self.assertNotIn("NAMES", chat.asked[0])
        self.assertIn("没有在页面上找到打开这张表单的入口", form_failure_message("agent_refused", "form_entry_not_found", True))


class ShowRecord(unittest.TestCase):
    def show(self, chat, pages, *, steps_allowed=4, policy=True):
        executor = Executor(chat, pages)
        executor.page_reach_steps = steps_allowed
        executor.page_reach_policy = SimpleNamespace(decide=lambda obs, element: "") if policy else None
        return GoalTurnExecutor._show_record(executor, executor.s, "苏州备件仓"), executor

    @staticmethod
    def view(epoch, *, content, path="/list", elements=8):
        return PageObservation(schema_version=1, epoch=epoch, url="https://app.test" + path, title="详情",
                               landmarks=[], render_state="ready", content=content,
                               elements=[ElementRef(f"e{i}", "button", "x", "button", epoch) for i in range(elements)])

    def test_another_page_or_a_dialog_over_the_list_that_carries_the_name_is_the_record(self):
        for path in ("/detail/7", "/list"):
            with self.subTest(path=path):
                shown, executor = self.show(steps({"action": "click", "element": 2}),
                                            [lambda epoch: self.view(epoch, content="名称 苏州备件仓", path=path)])
                self.assertEqual(shown, "shown")
                self.assertEqual(executor.calls.count("click"), 1)

    def test_the_list_itself_never_counts_and_a_view_without_the_name_needs_the_one_row(self):
        shown, _ = self.show(steps({"action": "click", "element": 2}, {"action": "stop"}),
                             [lambda epoch: listing(epoch)])
        self.assertFalse(shown)                    # the name is on the list, the list is still in front
        shown, _ = self.show(steps({"action": "click", "element": 2}, {"action": "stop"}),
                             [lambda epoch: self.view(epoch, content="disabled fields")])
        self.assertFalse(shown)                    # three rows were shown: which record this is is unknown
        shown, _ = self.show(steps({"action": "click", "element": 1}, {"action": "click", "element": 2}),
                             [lambda epoch: listing(epoch, rows=1, content="苏州备件仓"),
                              lambda epoch: self.view(epoch, content="disabled fields")])
        self.assertTrue(shown)                     # its row was the only one when its view was opened

    def test_a_confirmation_box_over_the_one_row_is_not_the_record(self):
        shown, _ = self.show(steps({"action": "click", "element": 1}, {"action": "click", "element": 2},
                                   {"action": "stop"}),
                             [lambda epoch: listing(epoch, rows=1, content="苏州备件仓"),
                              lambda epoch: self.view(epoch, content="确定删除吗", elements=2)])
        self.assertFalse(shown)

    def test_without_steps_or_a_policy_nothing_is_pressed(self):
        shown, executor = self.show(steps(), [], steps_allowed=0)
        self.assertEqual((shown, executor.calls), ("", []))
        shown, executor = self.show(steps({"action": "click", "element": 2}, {"action": "stop"}), [], policy=False)
        self.assertEqual((shown, executor.calls.count("click")), ("", 0))

    def test_its_own_page_in_front_with_the_applications_error_is_said_as_that(self):
        def errored(epoch):
            view = self.view(epoch, content="名称 苏州备件仓", path="/detail/7")
            view.render_state = "blocked"
            return view
        shown, _ = self.show(steps({"action": "click", "element": 2}, {"action": "stop"}), [errored])
        self.assertEqual(shown, "blocked")
        resolved = Resolved(kind="page", page=PagePlan(url="/list", label="仓库", missing_record="苏州备件仓"))
        from web_operation_agent.goal.executor import _Unmet
        outcome = _PageOutcome(record_shown="苏州备件仓", unmet=[_Unmet("error_dialog")])
        self.assertFalse(_achieved(resolved, outcome))
        self.assertEqual(_render(resolved, outcome, True),
                         ["已打开「苏州备件仓」的页面，但应用弹出了错误提示，页面内容可能不完整。"])

    def test_a_record_shown_settles_the_deliverable_and_is_said_plainly(self):
        resolved = Resolved(kind="page", page=PagePlan(url="/list", label="仓库", missing_record="苏州备件仓"))
        outcome = _PageOutcome(record_shown="苏州备件仓")
        self.assertTrue(_achieved(resolved, outcome))
        self.assertEqual(_render(resolved, outcome, True), ["已打开「苏州备件仓」的详情。"])
        self.assertFalse(_achieved(resolved, _PageOutcome()))


SCOPE = "form_instance:9f2c1a7e-0000-4000-8000-000000000001"


class DatesAndScopes(unittest.TestCase):
    def run_date(self, behaviour):
        values, calls = {"v": ""}, []

        def execute(action, args):
            calls.append((action, args["fields"][0]["value"] if action == "handoff_form" else args.get("value")))
            if action == "fill":
                given = args["value"]
                values["v"] = {"keeps": given, "completes": given + " 00:00:00" if len(given) == 10 else given,
                               "needs_time": given if len(given) > 10 else ""}[behaviour]
            epoch = len(calls) + 1
            return ActionReceipt(1, "c" + str(epoch), action, {}, True, observation=PageObservation(
                schema_version=1, epoch=epoch, url="https://app.test/leave/create", title="t", landmarks=[],
                render_state="ready", elements=[
                    ElementRef("e1", "combobox", "开始时间", "input", epoch,
                               {"field_label": "开始时间", "form_scope": SCOPE, "value": values["v"]}),
                    ElementRef("e2", "button", "确 定", "button", epoch, {"form_scope": SCOPE})]))

        session = SimpleNamespace(
            _cancel_requested=lambda: False, _browser_attempts=0, page_actions=0,
            _approvals=SimpleNamespace(confirm_operation=lambda *a, **k: ("", ""), settle_operation=lambda: None),
            _writes=SimpleNamespace(write_outcome=lambda *a, **k: {"state": "not_written"},
                                    settle_write=lambda write: ("", "")),
            ledger=SimpleNamespace(append=lambda *a, **k: None), browser=SimpleNamespace(execute=execute))
        draft = FormDraft("/leave/create", (FormDraftField("start", "开始时间", "2026-10-20", "combobox",
                                                           binding="label"),),
                          control_scope=SCOPE, submit_label="确 定")
        _, reason, _ = apply_form_draft(session, draft, execute("observe", {}).observation, 1)
        handed = [value for action, value in calls if action == "handoff_form"]
        return reason, [value for action, value in calls if action == "fill"], handed

    def test_a_form_scope_named_by_its_lifetime_is_a_scope(self):
        validate_form_scope(SCOPE, "确 定", label_binding=True)
        for bad in ("form_instance:", "form_instance:a b", "window:x"):
            with self.assertRaises(ValueError):
                validate_form_scope(bad, "确 定", label_binding=True)

    def test_a_control_that_keeps_a_time_gets_the_date_at_midnight_once(self):
        # (what was typed, the value the handoff then agrees with the page on)
        self.assertEqual(self.run_date("keeps"), ("", ["2026-10-20"], ["2026-10-20"]))
        self.assertEqual(self.run_date("completes"), ("", ["2026-10-20"], ["2026-10-20 00:00:00"]))
        self.assertEqual(self.run_date("needs_time"),
                         ("", ["2026-10-20", "2026-10-20 00:00:00"], ["2026-10-20 00:00:00"]))

    def test_form_plan_words_are_bounded(self):
        with self.assertRaises(ValueError):
            FormPlan("ref", (("k", "v"),), title="x" * 201)
        with self.assertRaises(ValueError):
            FormPlan("ref", (("k", "v"),), record=7)



class OpenFormMeant(unittest.TestCase):
    """A change to what is being entered: the named record is the draft in the open form."""

    @staticmethod
    def observation(*, shown="雾松设计", scope="dialog:d"):
        return PageObservation(schema_version=1, epoch=3, url="https://app.test/crm/customer", title="客户",
                               landmarks=[], render_state="ready", elements=[
            ElementRef("e1", "textbox", "请输入客户名称", "input", 3,
                       {"field_label": "客户名称", "form_scope": scope, "form_instance": "i1", "value": shown}),
            ElementRef("e2", "combobox", "陈牧", "input", 3,
                       {"field_label": "负责人", "form_scope": scope, "form_instance": "i1",
                        "selected_option": "陈牧"}),
            ElementRef("e3", "button", "确 定", "button", 3, {"form_scope": scope})])

    def meant(self, declaration, resolved, *, port=None, observation=None, prefill=True):
        from web_operation_agent.goal.ports import Resolution
        from web_operation_agent.goal.spec import GoalSpec
        adopted = []

        class Port:
            def adopt_open(self, observation, fields, *, context):
                adopted.append(fields)
                return "open-ref", tuple(("k" + str(i), value) for i, (_, value) in enumerate(fields))
        events = []
        executor = SimpleNamespace(
            application=SimpleNamespace(ports=SimpleNamespace(forms=port or Port())),
            config=SimpleNamespace(allow_form_prefill=prefill))
        state = SimpleNamespace(
            obs=observation or self.observation(), step=4,
            receipt=SimpleNamespace(stage=lambda name: nullcontext()),
            approvals=SimpleNamespace(context=lambda step, name: name),
            ledger=SimpleNamespace(append=lambda event, **kw: events.append(event)))
        spec = GoalSpec.from_json({"write_intent": True, "deliverables": [declaration]})
        out = GoalTurnExecutor._open_form_meant(executor, state, spec, Resolution(deliverables=(resolved,)))
        return out.deliverables[0], adopted, events

    MISSING = Resolved(kind="handoff", problem="no customer matches", missing="雾松设计")
    EMAIL = {"kind": "handoff", "entity": "crm/customer", "record": "雾松设计",
             "field_values": [{"field": "email", "value": "ops@x.invalid"}]}

    def test_a_name_no_record_has_that_the_open_form_shows_means_that_form(self):
        for declaration in (self.EMAIL, {"kind": "handoff", "entity": "crm/customer", "record": "雾松设计",
                                         "value": "ops@x.invalid", "fields": ["email"]}):
            with self.subTest(shape=sorted(declaration)):
                resolved, adopted, events = self.meant(declaration, self.MISSING)
                self.assertEqual(adopted, [(("email", "ops@x.invalid"),)])
                self.assertEqual(resolved.problem, "")
                self.assertEqual(resolved.page.form.reference, "open-ref")
                self.assertEqual(resolved.page.form.values, (("k0", "ops@x.invalid"),))
                self.assertFalse(resolved.page.form.open_entry)       # nothing is pressed to get there
                self.assertEqual((resolved.page.url, resolved.page.expected_path), ("", "/crm/customer"))
                self.assertEqual(events, ["goal_open_form_adopted"])

    def test_a_selected_option_of_the_open_form_counts_as_shown(self):
        missing = Resolved(kind="handoff", problem="no customer matches", missing="陈牧")
        resolved, adopted, _ = self.meant({**self.EMAIL, "record": "陈牧"}, missing)
        self.assertEqual(len(adopted), 1)
        self.assertIsNotNone(resolved.page)

    def test_everything_else_is_left_as_the_resolver_found_it(self):
        cases = {
            "the form shows another name": dict(observation=self.observation(shown="别的客户")),
            "the form is not in a dialog": dict(observation=self.observation(scope="form:list-query")),
            "form prefill is not enabled": dict(prefill=False),
            "the port cannot adopt": dict(port=object()),
        }
        for name, extra in cases.items():
            with self.subTest(name):
                resolved, adopted, _ = self.meant(self.EMAIL, self.MISSING, **extra)
                self.assertEqual((resolved, adopted), (self.MISSING, []))
        found = Resolved(kind="handoff", page=PagePlan(url="/x", label="x"))
        self.assertEqual(self.meant(self.EMAIL, found)[0], found)             # the record exists
        valueless = {"kind": "handoff", "entity": "crm/customer", "record": "雾松设计", "fields": ["email"]}
        self.assertEqual(self.meant(valueless, self.MISSING)[1], [])           # nothing to enter

    def test_a_port_that_refuses_leaves_the_record_not_found(self):
        class Refusing:
            def adopt_open(self, observation, fields, *, context):
                raise BusinessToolRequestError("this page shows no single open form for these fields")
        resolved, _, events = self.meant(self.EMAIL, self.MISSING, port=Refusing())
        self.assertEqual((resolved, events), (self.MISSING, ["goal_open_form_not_adopted"]))


if __name__ == "__main__":
    unittest.main()
