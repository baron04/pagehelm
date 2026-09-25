"""A goal-directed turn, end to end, with no model and no application behind it."""
from __future__ import annotations

import json
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.descriptor import (APPLICATION_ADAPTER_VERSION,
                                                        ApplicationDescriptor)
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnStatus
from web_operation_agent.goal import executor as executor_module
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexField, IndexPage
from web_operation_agent.goal.ports import Aggregate, Fact, PagePlan, Resolution, Resolved
from web_operation_agent.goal.spec import DECLARE_GOAL_TOOL, GoalSpec
from web_operation_agent.goal.understand import understand
from web_operation_agent.schemas.browser import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.tests.execution.product_support import _Channel
from web_operation_agent.tests.support.application import _InvoiceAdapter
from web_operation_agent.tests.support.business import _Chat


def _index() -> ApplicationIndex:
    return ApplicationIndex(
        entities=(IndexEntity(
            key="order", label="Sales Order", aliases=("销售订单",),
            fields=(IndexField(key="partner", label="Customer", kind="link", relation="partner"),
                    IndexField(key="total", label="Total", kind="money"),
                    IndexField(key="state", label="Status", kind="choice",
                               choices=("Quotation", "Sales Order"))),
            sections=("Order Lines", "Other Info"), actions=("Confirm", "Send by Email")),),
        pages=(IndexPage(key="p1", label="Orders", breadcrumb="Sales / Orders",
                         entity="order", views=("list", "kanban"),
                         filters=("My Orders",), groups=("Salesperson",)),),
        source_digest="abc")


class _Port:
    """A resolver whose answers are scripted per deliverable kind."""

    def __init__(self, resolved: list[Resolved], then: list[Resolved] | None = None):
        self.resolved = resolved
        #: What the second resolution of a turn answers, when a repair is expected.
        self.then = then
        self.specs: list[GoalSpec] = []

    def index(self, *, context=None):
        return _index()

    def resolve(self, spec, *, context=None, current_path=""):
        self.specs.append(spec)
        resolved = self.then if (self.then is not None and len(self.specs) > 1) else self.resolved
        return Resolution(deliverables=tuple(resolved), reads=(("read", "order"),))


def _obs(epoch: int, url: str, *, tabs=(), selected=(), facets=None, candidates=None,
         buttons=(), render_state="ready", fields=None, suggestions=()) -> PageObservation:
    elements = [ElementRef(ref=f"t{i}", role="tab", name=name, tag="a", epoch=epoch)
                for i, name in enumerate(tabs)]
    elements += [ElementRef(ref=f"b{i}", role="button", name=name, tag="button", epoch=epoch)
                 for i, name in enumerate(buttons)]
    # A field's label comes before its input, as on a real form; a value must go into
    # the input, never the label.
    elements += [ElementRef(ref=f"l{i}", role="label", name=name, tag="label", epoch=epoch)
                 for i, name in enumerate(fields or {})]
    # Inputs carry the application's field identifier and no accessible name of their
    # own, as a component form renders them.
    elements += [ElementRef(ref=f"f{i}", role="textbox", name="", tag="input", epoch=epoch,
                            attrs={"field": name.lower(), **({"value": value} if value else {})})
                 for i, (name, value) in enumerate((fields or {}).items())]
    elements += [ElementRef(ref=f"s{i}", role="option", name=name, tag="li", epoch=epoch)
                 for i, name in enumerate(suggestions)]
    return PageObservation(schema_version=1, epoch=epoch, url=url, title="App",
                           landmarks=["main"], elements=elements, content="page",
                           render_state=render_state, selected_controls=list(selected),
                           page_facets=facets, facet_candidates=candidates)


class _Browser:
    """A page that knows tabs, facets and buttons, and records what was pressed."""

    instances: list = []

    def __init__(self, channel, *, timeout_s, turn_id=""):
        self.channel = channel
        self.actions: list[tuple[str, dict]] = []
        self.url = channel.latest_observation_envelope.observation.url
        self.epoch = channel.latest_observation_envelope.page_epoch
        self.tabs = ("Order Lines", "Other Info")
        self.selected: list[str] = []
        self.facets: list[dict] = [{"kind": "filter", "name": "My Orders"}]
        self.buttons = ("Confirm", "Send by Email")
        #: Form fields by label, with what they currently show.
        self.fields: dict[str, str] = {"Expiration": "", "Customer": "Gemini Furniture"}
        self.suggestions: tuple[str, ...] = ()
        self.__class__.instances.append(self)

    def start(self):
        return {"ok": True}

    def _observation(self):
        candidates = [{"ref": "fc_1", "kind": "filter", "name": "My Orders",
                       "active": any(f["name"] == "My Orders" for f in self.facets)},
                      {"ref": "fc_2", "kind": "group", "name": "Salesperson",
                       "active": any(f["name"] == "Salesperson" for f in self.facets)}]
        return _obs(self.epoch, self.url, tabs=self.tabs, selected=self.selected,
                    facets=list(self.facets), candidates=candidates, buttons=self.buttons,
                    fields=self.fields, suggestions=self.suggestions)

    def execute(self, action, args=None, expect_navigation=False):
        args = dict(args or {})
        self.actions.append((action, args))
        before = self.url
        if action == "goto":
            self.url = "http://127.0.0.1:8080" + args["url"]
            self.selected = []
        elif action == "click":
            ref = args["ref"]
            if ref.startswith("t"):
                self.selected = [self.tabs[int(ref[1:])]]
            elif ref.startswith("s"):
                # Choosing a suggestion puts that record's name into the link field.
                self.fields["Customer"] = self.suggestions[int(ref[1:])]
                self.suggestions = ()
        elif action == "fill":
            label = list(self.fields)[int(args["ref"][1:])]
            if label == "Customer":
                # A link field offers the records whose names contain the typed text.
                self.suggestions = tuple(n for n in ("Lumber Inc", "Azure Interior")
                                         if args["value"].casefold() in n.casefold())
            else:
                self.fields[label] = args["value"]
        elif action == "apply_facet":
            name = {"fc_1": "My Orders", "fc_2": "Salesperson"}[args["ref"]]
            if args["active"]:
                self.facets.append({"kind": "filter" if name == "My Orders" else "group", "name": name})
            else:
                self.facets = [f for f in self.facets if f["name"] != name]
        self.epoch += 1
        return ActionReceipt(schema_version=1, command_id=f"c{len(self.actions)}",
                             action=action, args_echo=args, ok=True, url_before=before,
                             url_after=self.url, navigated=self.url != before,
                             epoch_before=self.epoch - 1, epoch_after=self.epoch,
                             observation=self._observation())

    @staticmethod
    def console():
        return []


class _Adapter(_InvoiceAdapter):
    def __init__(self):
        super().__init__()
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION, application_id="ledger-demo",
            display_name="Ledger Demo", origin="http://127.0.0.1:8080",
            navigation_mode="spa", page_controller_module="http://127.0.0.1:8080/pc.js",
            catalog_sha256=self.business_tools.catalog_sha256)


def _declaration(**deliverable) -> tuple:
    return ("declare_goal", {"deliverables": [deliverable]})


class _AgreeingChat(_Chat):
    """A scripted chat whose second reading repeats the first, as a steady model would,
    unless the script provides a different one. The two readings arrive from two
    threads, so the script is handed out under a lock."""

    def __init__(self, script):
        super().__init__(script)
        self._lock = threading.Lock()

    def complete(self, messages, tools, _run_id, **_kwargs):
        with self._lock:
            if len(self.script) == 1:
                self.script.append(self.script[0])
            return super().complete(messages, tools, _run_id, **_kwargs)


class GoalTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "audit.jsonl"
        _Browser.instances.clear()
        self.patcher = patch.object(executor_module, "RemoteBrowserClient", _Browser)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _run(self, script, resolved, *, decision="approved", text="S00006 的客户是谁？看看其他信息",
             resolved_then=None):
        channel = _Channel(page_url="http://127.0.0.1:8080/home", decision=decision)
        chat = _AgreeingChat(script)
        port = _Port(resolved, resolved_then)
        executor = GoalTurnExecutor(
            channel=channel,
            config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=chat, application=bind_application(_Adapter()), goal_port=port,
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run")
        execution = executor.execute({"message_id": "m1", "text": text,
                                      "page_url": "http://127.0.0.1:8080/home"})
        events = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        return execution, channel, chat, port, events

    def test_a_fact_and_a_page_are_answered_from_one_model_call(self):
        execution, channel, chat, port, events = self._run(
            [_declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])],
            [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),)),
             Resolved(kind="page", page=PagePlan(url="/orders/6", label="S00006 · Other Info",
                                                 expected_path="/orders/6",
                                                 section="Other Info"))])
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("Lumber Inc", execution.user_answer)
        self.assertIn("已打开：S00006 · Other Info", execution.user_answer)
        # One reading, and a second that agreed with it; nothing more.
        self.assertEqual(len(chat.messages), 2)
        self.assertEqual(chat.tool_names, [{"declare_goal"}, {"declare_goal"}])
        self.assertIn("APPLICATION INDEX", chat.messages[0][1]["content"])
        browser = _Browser.instances[0]
        self.assertEqual([a for a, _ in browser.actions if a not in ("wait_for", "observe")],
                         ["goto", "click"])
        self.assertEqual(browser.selected, ["Other Info"])
        self.assertEqual(len(channel.approvals), 1)
        self.assertEqual(channel.approvals[0].kind, "navigation")
        kinds = [e["event"] for e in events]
        for name in ("goal_index_ready", "goal_declared", "goal_resolved",
                     "navigation_confirmation_requested", "goal_page_settled",
                     "run_finished", "product_turn_finished"):
            self.assertIn(name, kinds)
        self.assertEqual(port.specs[0].deliverables[0].record, "S00006")

    def test_a_repairable_problem_gets_one_second_declaration(self):
        port_first = Resolved(kind="fact", problem="no field 'Validity' on Order",
                              repair="'Validity' is not a field of Order; its fields are: Expiration")
        port_second = Resolved(kind="fact", facts=(Fact("S00001", "Expiration", "2026-10-31"),))
        first = _declaration(kind="fact", entity="order", record="S00001", fields=["Validity"])
        scripts = [first, first,
                   _declaration(kind="fact", entity="order", record="S00001", fields=["Expiration"])]
        execution, _channel, chat, port, events = self._run(
            scripts, [port_first], text="S00001 有效期到哪天？", resolved_then=[port_second])
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("2026-10-31", execution.user_answer)
        self.assertEqual(len(chat.messages), 3)
        self.assertIn("PROBLEMS", chat.messages[2][1]["content"])
        self.assertIn("Expiration", chat.messages[2][1]["content"])
        self.assertEqual(len(port.specs), 2)
        repaired = next(e for e in events if e["event"] == "goal_repaired")
        self.assertTrue(repaired["accepted"])
        self.assertEqual((repaired["unresolved_before"], repaired["unresolved_after"]), (1, 0))

    def test_a_second_declaration_that_is_no_better_is_not_taken(self):
        problem = Resolved(kind="fact", problem="no Order matches 'X'", repair="no Order is named 'X'")
        first = _declaration(kind="fact", entity="order", record="X", fields=["Customer"])
        scripts = [first, first,
                   _declaration(kind="fact", entity="order", record="Y", fields=["Customer"])]
        execution, _channel, chat, port, events = self._run(
            scripts, [problem], text="X 的客户是谁？", resolved_then=[problem])
        self.assertEqual(len(chat.messages), 3)
        self.assertEqual(port.specs[0].deliverables[0].record, "X")
        repaired = next(e for e in events if e["event"] == "goal_repaired")
        self.assertFalse(repaired["accepted"])
        self.assertIn("无法完成", execution.user_answer)

    def test_a_change_asked_for_without_a_handoff_is_sent_back_once(self):
        first = ("declare_goal", {"deliverables": [{"kind": "page", "entity": "order",
                                                    "record": "S00005"}],
                                  "write_intent": True})
        scripts = [first, first,
                   _declaration(kind="handoff", entity="order", record="S00005",
                                action="Send by Email")]
        page = Resolved(kind="page", page=PagePlan(url="/orders/5", label="S00005",
                                                   expected_path="/orders/5"))
        handoff = Resolved(kind="handoff", page=PagePlan(url="/orders/5", label="S00005",
                                                         expected_path="/orders/5",
                                                         highlight="Send by Email"))
        execution, _channel, chat, _port, events = self._run(
            scripts, [page], text="把 S00005 发邮件给客户", resolved_then=[handoff])
        self.assertIn("write_intent", chat.messages[2][1]["content"])
        self.assertIn("标出「Send by Email」", execution.user_answer)
        self.assertTrue(next(e for e in events if e["event"] == "goal_repaired")["accepted"])

    def test_a_handoff_with_a_value_fills_the_field_and_leaves_saving_to_the_person(self):
        execution, _channel, _chat, _port, events = self._run(
            [_declaration(kind="handoff", entity="order", record="S00005",
                          action="Expiration", value="2026-10-31")],
            [Resolved(kind="handoff", page=PagePlan(url="/orders/5", label="S00005",
                                                    expected_path="/orders/5",
                                                    highlight="Expiration", fill="2026-10-31",
                                                    fill_kind="text", fill_label="2026-10-31",
                                                    fill_field="expiration"))],
            text="S00005 的有效期延长到十月底")
        browser = _Browser.instances[0]
        self.assertEqual(browser.fields["Expiration"], "2026-10-31")
        self.assertEqual([a for a, _ in browser.actions if a not in ("wait_for", "goto", "observe")],
                         ["fill", "highlight"])
        self.assertNotIn("submit", [a for a, _ in browser.actions])
        self.assertIn("已在「Expiration」填入「2026-10-31」并标出", execution.user_answer)
        self.assertIn("自行保存", execution.user_answer)
        self.assertNotIn("未回显", execution.user_answer)
        filled = next(e for e in events if e["event"] == "goal_field_filled")
        self.assertTrue(filled["verified"])

    def test_a_linked_record_is_typed_then_chosen_among_the_fields_own_suggestions(self):
        execution, _channel, _chat, _port, _events = self._run(
            [_declaration(kind="handoff", entity="order", record="S00005",
                          action="Customer", value="Azure Interior")],
            [Resolved(kind="handoff", page=PagePlan(url="/orders/5", label="S00005",
                                                    expected_path="/orders/5",
                                                    highlight="Customer", fill="Azure Interior",
                                                    fill_kind="link", fill_label="Azure Interior",
                                                    fill_field="customer"))],
            text="S00005 的客户换成 Azure Interior")
        browser = _Browser.instances[0]
        self.assertEqual(browser.fields["Customer"], "Azure Interior")
        self.assertEqual([a for a, _ in browser.actions if a not in ("wait_for", "goto", "observe")],
                         ["fill", "click", "highlight"])
        self.assertIn("填入「Azure Interior」", execution.user_answer)

    def test_a_value_that_cannot_be_entered_is_said_and_left_to_the_person(self):
        execution, _channel, _chat, _port, _events = self._run(
            [_declaration(kind="handoff", entity="order", record="S00005",
                          action="Customer", value="Nobody")],
            [Resolved(kind="handoff", page=PagePlan(url="/orders/5", label="S00005",
                                                    expected_path="/orders/5",
                                                    highlight="Customer", fill="Nobody",
                                                    fill_kind="link", fill_label="Nobody",
                                                    fill_field="customer"))],
            text="S00005 的客户换成 Nobody")
        self.assertEqual(_Browser.instances[0].fields["Customer"], "Gemini Furniture")
        self.assertIn("没能填入「Nobody」", execution.user_answer)
        self.assertIn("标出「Customer」", execution.user_answer)

    def test_two_readings_that_differ_are_decided_by_a_third(self):
        as_list = _declaration(kind="list", entity="order", record="S00006", fields=["Customer"])
        as_fact = _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])
        execution, _channel, chat, port, events = self._run(
            [as_list, as_fact, as_fact],
            [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            text="S00006 的客户是谁？")
        self.assertEqual(len(chat.messages), 3)
        self.assertIn("a second reading of the same request declared", chat.messages[2][1]["content"])
        self.assertIn("deliverable 1 kind", chat.messages[2][1]["content"])
        self.assertEqual(port.specs[0].deliverables[0].kind, "fact")
        self.assertEqual(len(port.specs), 1)
        reading = next(e for e in events if e["event"] == "goal_second_reading")
        self.assertFalse(reading["agrees"])
        self.assertTrue(any(e.get("reconciled") for e in events if e["event"] == "goal_declared"))
        self.assertIn("Lumber Inc", execution.user_answer)

    def test_a_handoff_highlights_and_never_presses(self):
        execution, _channel, _chat, _port, events = self._run(
            [_declaration(kind="handoff", entity="order", record="S00005",
                          action="Send by Email")],
            [Resolved(kind="handoff", page=PagePlan(url="/orders/5", label="S00005",
                                                    expected_path="/orders/5",
                                                    highlight="Send by Email"))],
            text="把 S00005 发邮件给客户")
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("标出「Send by Email」", execution.user_answer)
        self.assertIn("请你核对后自行点击", execution.user_answer)
        self.assertNotRegex(execution.user_answer, r"不能|无法|做不到|未能")
        browser = _Browser.instances[0]
        self.assertNotIn("click", [a for a, _ in browser.actions])
        self.assertIn("highlight", [a for a, _ in browser.actions])
        self.assertIn("page_handoff_ready", [e["event"] for e in events])

    def test_facets_are_applied_by_name_on_the_live_page(self):
        execution, *_ = self._run(
            [_declaration(kind="page", page={"key": "p1", "filters_off": ["My Orders"],
                                              "group_by": "Salesperson"})],
            [Resolved(kind="page", page=PagePlan(url="/orders", label="Sales / Orders",
                                                 expected_path="/orders",
                                                 facets_on=("Salesperson",),
                                                 facets_off=("My Orders",)))],
            text="按销售员看所有人的订单")
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        browser = _Browser.instances[0]
        self.assertEqual([f["name"] for f in browser.facets], ["Salesperson"])
        self.assertIn("已移除筛选：My Orders", execution.user_answer)

    def test_an_unmet_page_part_is_reported_not_claimed(self):
        execution, *_ = self._run(
            [_declaration(kind="page", entity="order", record="S00006", section="Notes")],
            [Resolved(kind="page", page=PagePlan(url="/orders/6", label="S00006 · Notes",
                                                 expected_path="/orders/6", section="Notes"))])
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertIn("未完成", execution.user_answer)
        self.assertIn("section Notes", execution.user_answer)

    def test_an_aggregate_is_rendered_with_its_scope(self):
        execution, *_ = self._run(
            [_declaration(kind="aggregate", entity="order", page={"key": "p1"},
                          metric={"op": "sum", "field": "Total"})],
            [Resolved(kind="aggregate", aggregate=Aggregate(
                subject="Orders · Customer = Acme", count=3,
                sums=(("Total", "92,750.00 USD"),)))],
            text="Acme 有几张订单？合计多少")
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("3 条", execution.user_answer)
        self.assertIn("92,750.00 USD", execution.user_answer)
        self.assertEqual(_Browser.instances[0].actions, [])

    def test_a_declined_page_switch_ends_the_turn_without_acting(self):
        execution, *_ = self._run(
            [_declaration(kind="page", page={"key": "p1"})],
            [Resolved(kind="page", page=PagePlan(url="/orders", label="Sales / Orders",
                                                 expected_path="/orders"))],
            decision="denied", text="打开订单")
        self.assertEqual(execution.status, AgentTurnStatus.CANCELLED)
        self.assertEqual(execution.terminal_reason, "navigation_not_confirmed")
        # Looking at the page is not acting on it; nothing else was dispatched.
        self.assertEqual([a for a, _ in _Browser.instances[0].actions], ["observe"])

    def test_nothing_declared_is_an_honest_refusal(self):
        execution, *_ = self._run(
            [("declare_goal", {"deliverables": [], "unsupported": "no such thing"})], [],
            text="帮我订个外卖")
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertIn("超出了助手能做的范围", execution.user_answer)
        self.assertEqual(_Browser.instances[0].actions, [])

    def test_a_reply_without_a_declaration_is_refused_not_guessed(self):
        execution, *_ = self._run([(None, {"content": "Sure, let me look."}),
                                   (None, {"content": "Still just looking."})], [])
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertEqual(_Browser.instances[0].actions, [])


class DeclarationTests(unittest.TestCase):
    def test_a_declaration_is_bounded_and_typed(self):
        spec = GoalSpec.from_json({"deliverables": [
            {"kind": "aggregate", "entity": "order", "page": {"key": "p1"},
             "metric": {"op": "count", "filters": [{"field": "Status", "value": "Sales Order"}],
                        "group_by": "Customer", "top": 1}},
            {"kind": "page", "entity": "order", "record": " S00006 ", "via": "Customer"}],
            "write_intent": False})
        self.assertEqual(spec.deliverables[0].metric.filters[0].values, ("Sales Order",))
        self.assertEqual(spec.deliverables[1].record, "S00006")
        self.assertEqual(spec.to_json()["deliverables"][1]["via"], "Customer")
        with self.assertRaises(ValueError):
            GoalSpec.from_json({"deliverables": [{"kind": "delete"}]})
        with self.assertRaises(ValueError):
            GoalSpec.from_json({"deliverables": [{"kind": "aggregate",
                                                  "metric": {"op": "count", "top": 99}}]})
        self.assertEqual(DECLARE_GOAL_TOOL["function"]["name"], "declare_goal")

    def test_the_index_renders_one_line_per_entity_and_page(self):
        text = _index().render()
        self.assertIn("order: Sales Order [销售订单] | fields: Customer->partner, Total(money), "
                      "Status(Quotation/Sales Order) | sections: Order Lines, Other Info | "
                      "actions: Confirm, Send by Email", text)
        self.assertIn("p1: Sales / Orders | entity order | views: list, kanban | "
                      "filters: My Orders | groups: Salesperson", text)
        self.assertEqual(_index().page("Sales / Orders"), _index().pages[0])
        self.assertEqual(_index().entity("销售订单").key, "order")
        self.assertEqual(_index().entities[0].field_by_label("status").key, "state")

    def test_understand_accepts_a_tool_call_or_json_content(self):
        chat = _Chat([("declare_goal", {"deliverables": [{"kind": "fact", "entity": "order",
                                                          "record": "S1", "fields": ["Total"]}]})])
        spec, projection = understand(chat, prompt="S1 多少钱", index=_index(), run_id="r")
        self.assertEqual(spec.deliverables[0].fields, ("Total",))
        self.assertEqual(projection["source"], "tool_call")
        chat = _Chat([(None, {"content": 'Here: {"deliverables": [{"kind": "page", '
                                         '"page": {"key": "p1"}}]}'})])
        spec, projection = understand(chat, prompt="打开订单", index=_index(), run_id="r")
        self.assertEqual(spec.deliverables[0].page.key, "p1")
        self.assertEqual(projection["source"], "content")
        with self.assertRaises(ValueError):
            understand(_Chat([(None, {"content": "no idea"}), (None, {"content": "none"})]),
                       prompt="?", index=_index(), run_id="r")
        chat = _Chat([(None, {"content": "hmm"}),
                      ("declare_goal", {"deliverables": {"kind": "page", "page": {"key": "p1"}}})])
        spec, projection = understand(chat, prompt="打开订单", index=_index(), run_id="r")
        self.assertEqual(spec.deliverables[0].page.key, "p1")
        self.assertEqual(projection["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
