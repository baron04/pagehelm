"""A goal-directed turn, end to end, with no model and no application behind it."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.descriptor import (APPLICATION_ADAPTER_VERSION,
                                                        ApplicationDescriptor)
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnMessage, AgentTurnStatus
from web_operation_agent.goal import executor as executor_module
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.goal.index import (ApplicationIndex, IndexEntity, IndexField, IndexPage,
                                             IndexRule)
from web_operation_agent.goal.ports import OPTIONAL, Aggregate, Fact, PagePlan, Resolution, Resolved
from web_operation_agent.goal.spec import DECLARE_GOAL_TOOL, GoalSpec
from web_operation_agent.goal.narrow import neighbourhood
from web_operation_agent.goal.understand import understand
from web_operation_agent.schemas.browser import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.tests.execution.product_support import _Channel
from web_operation_agent.tests.support.application import _InvoiceAdapter
from web_operation_agent.tests.support.business import _Chat


_RULE = IndexRule(id="r1", kind="visibility", about="Create Invoice",
                  text='Button "Create Invoice" opens sale.advance_payment; hidden when state != sale',
                  citation="addons/sale/views/sale_order_views.xml:266-271@18.0 sha256:" + "a" * 64,
                  subject="330")
#: The window the control opens brought its own refusal, on the control's operation.
_WINDOW_RULE = IndexRule(id="r2", kind="refusal", about="Create Invoice › _check_amount",
                         text="Create Invoice › _check_amount refuses with \"must be positive\" when amount <= 0",
                         citation="addons/sale/wizard/advance.py:125-130@18.0 sha256:" + "c" * 64,
                         subject="330")
_OTHER_RULE = IndexRule(id="r3", kind="meaning", about="Expiration", text="Field Expiration: validity",
                        citation="addons/sale/models/sale_order.py:120-124@18.0 sha256:" + "d" * 64)


def _index() -> ApplicationIndex:
    return ApplicationIndex(
        entities=(IndexEntity(
            key="order", label="Sales Order", aliases=("销售订单",),
            fields=(IndexField(key="partner", label="Customer", kind="link", relation="partner"),
                    IndexField(key="total", label="Total", kind="money"),
                    IndexField(key="state", label="Status", kind="choice",
                               choices=("Quotation", "Sales Order"))),
            sections=("Order Lines", "Other Info"), actions=("Confirm", "Send by Email"),
            rules=(_RULE, _WINDOW_RULE, _OTHER_RULE)),),
        pages=(IndexPage(key="p1", label="Orders", breadcrumb="Sales / Orders",
                         entity="order", views=("list", "kanban"),
                         filters=("My Orders",), groups=("Salesperson",),
                         operations=("create",)),),
        source_digest="abc")


class _Port:
    """A resolver whose answers are scripted per deliverable kind."""

    def __init__(self, resolved: list[Resolved], then: list[Resolved] | None = None,
                 offers: frozenset[str] = OPTIONAL):
        self.resolved = resolved
        #: What the second resolution of a turn answers, when a repair is expected.
        self.then = then
        self.offers = offers
        self.specs: list[GoalSpec] = []

    def capabilities(self):
        return self.offers

    def locate_reference(self, text, *, context=None):
        return (("order", "S00006"),) if text == "S00006" else ()

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


class PageReadinessTests(unittest.TestCase):
    def test_ready_route_without_rendered_page_evidence_does_not_settle(self):
        executor = GoalTurnExecutor.__new__(GoalTurnExecutor)
        state = SimpleNamespace(obs=_obs(1, "https://app.test/orders"))
        waits = []

        def dispatch(s, action, args):
            waits.append((action, args))
            s.obs = _obs(s.obs.epoch + 1, s.obs.url,
                         buttons=("New",) if s.obs.elements else ())
            return object()

        executor._dispatch = dispatch
        self.assertFalse(executor._wait_ready(state, "/orders", record=False))
        self.assertTrue(waits)
        state.obs = _obs(1, "https://app.test/orders", buttons=("New",))
        self.assertTrue(executor._wait_ready(state, "/orders", record=False))


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
    threads: the first from the turn's own thread, the second from a pool. So that the
    script means the same thing on every run, the second waits until the turn's thread
    has asked for the same messages, and takes the next entry after it."""

    def __init__(self, script):
        super().__init__(script)
        self._owner = threading.get_ident()
        self._asked = threading.Condition()
        self._owner_messages = []

    def complete(self, messages, tools, _run_id, **_kwargs):
        with self._asked:
            if threading.get_ident() == self._owner:
                self._owner_messages.append(messages)
                self._asked.notify_all()
            else:
                self._asked.wait_for(lambda: messages in self._owner_messages, timeout=5)
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
             resolved_then=None, offers=OPTIONAL, staged=False, readings=2, port=None):
        channel = _Channel(page_url="http://127.0.0.1:8080/home", decision=decision)
        chat = _AgreeingChat(script)
        port = port or _Port(resolved, resolved_then, offers)
        executor = GoalTurnExecutor(
            channel=channel,
            config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=chat, application=bind_application(_Adapter()), goal_port=port,
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run", staged=staged,
            readings=readings)
        execution = executor.execute({"message_id": "m1", "text": text,
                                      "page_url": "http://127.0.0.1:8080/home"})
        events = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        return execution, channel, chat, port, events

    def test_warm_declarations_save_three_calls_but_resolve_changed_business_data(self):
        scope = ("declare_scope", {"parts": [{"kind": "fact", "entity": "order"}]})
        goal = _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])
        chat = _AgreeingChat([scope, goal, goal] * 3)
        channel = _Channel(page_url="http://127.0.0.1:8080/home", decision="approved")
        port = _Port([Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Old Customer"),))])
        executor = GoalTurnExecutor(
            channel=channel, config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=chat, application=bind_application(_Adapter()), goal_port=port,
            control=ReadOnlyControl(), run_id_factory=lambda m: "run-" + m.message_id, staged=True)
        for i in range(4):
            result = executor.execute({"message_id": f"m{i}", "text": "S00006 的客户是谁？",
                                       "page_url": "http://127.0.0.1:8080/home"})
            self.assertIn("Old Customer", result.user_answer)
        self.assertEqual(len(chat.messages), 9)  # History stabilizes; fourth turn uses three hits.
        reads_before = len(port.specs)
        port.resolved = [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "New Customer"),))]
        result = executor.execute({"message_id": "m4", "text": "S00006 的客户是谁？",
                                   "page_url": "http://127.0.0.1:8080/home"})
        self.assertIn("New Customer", result.user_answer)
        self.assertEqual(len(chat.messages), 9)
        self.assertGreater(len(port.specs), reads_before)
        events = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        hits = [event for event in events if event.get("cache_hit")]
        self.assertEqual(len(hits), 6)
        self.assertTrue(all(event["attempts"] == 0 for event in hits))

    def test_a_fact_and_a_page_are_answered_from_one_model_call(self):
        execution, channel, chat, port, events = self._run(
            [("declare_goal", {"deliverables": [
                {"kind": "fact", "entity": "order", "record": "S00006",
                 "fields": ["Customer"]},
                {"kind": "page", "entity": "order", "record": "S00006",
                 "section": "Other Info"}]})],
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
        self.assertEqual(channel.approvals[0].kind, "operate")
        kinds = [e["event"] for e in events]
        for name in ("goal_index_ready", "goal_declared", "goal_resolved",
                     "operation_confirmation_requested", "goal_page_settled",
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
        self.assertEqual((repaired["unresolved_before"], repaired["unresolved_after"]), ([1, 0], [0, 0]))

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
            [as_fact, as_list, as_fact],
            [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            text="S00006 的客户是谁？")
        self.assertEqual(len(chat.messages), 3)
        self.assertIn("a second reading of the same request declared", chat.messages[2][1]["content"])
        self.assertIn("deliverable 1 kind", chat.messages[2][1]["content"])
        # Before asking a third time, both readings were resolved quietly and found to
        # lead apart (a list here is not the fact the port answers); the turn then acts
        # on the third reading alone.
        self.assertEqual(port.specs[-1].deliverables[0].kind, "fact")
        self.assertFalse(any(e["event"] in ("goal_readings_resolve_alike", "goal_readings_one_finds")
                             for e in events))
        self.assertEqual(sum(e["event"] == "goal_resolved" for e in events), 1)
        reading = next(e for e in events if e["event"] == "goal_second_reading")
        self.assertFalse(reading["agrees"])
        self.assertTrue(any(e.get("reconciled") for e in events if e["event"] == "goal_declared"))
        self.assertIn("Lumber Inc", execution.user_answer)

    def test_readings_that_lead_apart_are_settled_by_most_independent_readings(self):
        class _ByField(_Port):
            def resolve(self, spec, *, context=None, current_path=""):
                self.specs.append(spec)
                return Resolution(deliverables=tuple(
                    Resolved(kind="fact", facts=(Fact("S00006", f, f"{f} of S00006"),))
                    for d in spec.deliverables for f in d.fields[:1]), reads=())
        customer = _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])
        total = _declaration(kind="fact", entity="order", record="S00006", fields=["Total"])
        execution, _channel, chat, _port, events = self._run(
            [customer, total, total, total], [], text="S00006 的金额是多少？",
            port=_ByField([]))
        voted = next(e for e in events if e["event"] == "goal_readings_voted")
        self.assertEqual((voted["votes"], sorted(voted["groups"])), (4, [1, 3]))
        self.assertTrue(any(e["event"] == "goal_readings_majority" for e in events))
        self.assertFalse(any("a second reading of the same request declared" in m[1]["content"]
                             for m in chat.messages))
        self.assertIn("Total of S00006", execution.user_answer)

    def test_a_tied_vote_takes_the_answer_a_question_offers_as_a_choice(self):
        from web_operation_agent.goal.executor import _answer_among, _answer_key
        opened = Resolution(deliverables=(Resolved(kind="page", page=PagePlan(
            url="/plans/9", label="期数 1", expected_path="/plans/9")),))
        titled = Resolution(deliverables=(Resolved(kind="page", page=PagePlan(
            url="/plans/9", label="第 1 期", expected_path="/plans/9")),))
        asked = Resolution(deliverables=(Resolved(kind="page", problem="several",
                                                  candidates=("期数 2", "期数 1")),))
        # The same page opened is one answer, however it is labelled.
        self.assertEqual(_answer_key(opened), _answer_key(titled))
        self.assertTrue(_answer_among(opened, asked))
        other = Resolution(deliverables=(Resolved(kind="page", page=PagePlan(
            url="/plans/3", label="期数 3", expected_path="/plans/3")),))
        self.assertFalse(_answer_among(other, asked))

    def test_two_readings_worded_apart_that_resolve_alike_need_no_third(self):
        # One reading names the field in Chinese, the other in English; both resolve to
        # the same answer, so no third reading is asked for.
        in_chinese = _declaration(kind="fact", entity="order", record="S00006", fields=["客户"])
        in_english = _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])
        execution, _channel, chat, port, events = self._run(
            [in_chinese, in_english],
            [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            text="S00006 的客户是谁？")
        self.assertEqual(len(chat.messages), 2)
        self.assertTrue(any(e["event"] == "goal_readings_resolve_alike" for e in events))
        self.assertFalse(any(e.get("reconciled") for e in events if e["event"] == "goal_declared"))
        self.assertIn("Lumber Inc", execution.user_answer)

    def test_a_port_without_an_optional_capability_gets_a_plain_answer_not_a_call(self):
        # A port offering only the required level: the fact is resolved by it, the tab
        # is not asked of it, and the reply says what this application does not offer.
        execution, _channel, _chat, port, events = self._run(
            [("declare_goal", {"deliverables": [
                {"kind": "fact", "entity": "order", "record": "S00006", "fields": ["Customer"]},
                {"kind": "page", "entity": "order", "record": "S00006", "section": "Other Info"}]})],
            [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            offers=frozenset())
        self.assertIn("Lumber Inc", execution.user_answer)
        self.assertIn("这个应用没有提供页签", execution.user_answer)
        self.assertNotIn("does not offer", execution.user_answer)
        self.assertEqual([d.kind for d in port.specs[0].deliverables], ["fact"])
        missing = next(e for e in events if e["event"] == "goal_capability_missing")
        self.assertEqual(missing["missing"], [["sections"]])
        # Nothing was left to open, so the page was not even looked at.
        self.assertEqual(_Browser.instances[0].actions, [])

    def test_a_change_where_the_assistant_only_reads_is_refused_as_read_only(self):
        execution, _channel, _chat, port, _events = self._run(
            [("declare_goal", {"write_intent": True, "deliverables": [
                {"kind": "handoff", "entity": "order", "record": "S00006", "action": "Confirm"}]})],
            [], text="把 S00006 确认掉", offers=frozenset())
        self.assertIn("只读", execution.user_answer)
        self.assertNotIn("没有提供", execution.user_answer)
        self.assertEqual(port.specs, [])

    def test_an_ordinal_the_request_writes_lets_the_pick_stand(self):
        from web_operation_agent.goal.executor import _ORDINAL
        for text in ("打开合同的第二期回款计划", "第3张订单", "the 2nd plan"):
            self.assertIsNotNone(_ORDINAL.search(text), text)
        self.assertIsNone(_ORDINAL.search("打开合同的回款计划详情"))

    def test_a_repair_that_helps_may_be_followed_by_one_more(self):
        class _Steps(_Port):
            def __init__(self, steps):
                super().__init__([])
                self.steps = steps

            def resolve(self, spec, *, context=None, current_path=""):
                self.specs.append(spec)
                return Resolution(deliverables=(self.steps[min(len(self.specs), len(self.steps)) - 1],),
                                  reads=())
        port = _Steps([
            Resolved(kind="fact", problem="no Vendor matches 'Acme'", missing="Acme",
                     repair="'Acme' is Contact record 'Acme Ltd'"),
            Resolved(kind="fact", facts=(Fact("Acme Ltd", "Phone", "(empty)"),), empty=("Phone",),
                     repair="Phone holds no value; Mobile holds one"),
            Resolved(kind="fact", facts=(Fact("Acme Ltd", "Phone", "(empty)"),
                                         Fact("Acme Ltd", "Mobile", "139"),), empty=("Phone",)),
        ])
        execution, _channel, _chat, _port, events = self._run(
            [_declaration(kind="fact", entity="order", record="Acme", fields=["Phone"]),
             _declaration(kind="fact", entity="order", record="Acme Ltd", fields=["Phone"]),
             _declaration(kind="fact", entity="order", record="Acme Ltd", fields=["Phone", "Mobile"])],
            [], text="Acme 的电话是多少？", readings=1, port=port)
        rounds = [e for e in events if e["event"] == "goal_repaired"]
        self.assertEqual([r["accepted"] for r in rounds], [True, True])
        self.assertEqual(rounds[0]["unresolved_after"], [0, 1])
        self.assertIn("139", execution.user_answer)

    def test_a_repair_keeps_what_the_person_asked_for(self):
        # "Open that order's detail" fits two: the repair may not turn it into a list.
        execution, _channel, _chat, _port, events = self._run(
            [("declare_goal", {"deliverables": [{"kind": "page", "entity": "order", "record": "Acme"}]}),
             ("declare_goal", {"deliverables": [{"kind": "list", "entity": "order", "metric": {
                 "op": "count", "filters": [{"field": "Customer", "values": ["Acme"]}]}}]})],
            [Resolved(kind="page", problem="'Acme' matches several", candidates=("S00002", "S00003"),
                      repair="add a condition the request gives")],
            resolved_then=[Resolved(kind="list", aggregate=Aggregate(
                subject="Orders", count=2, records=("S00002", "S00003")))],
            text="打开 Acme 那张订单的详情", readings=1)
        repaired = next(e for e in events if e["event"] == "goal_repaired")
        self.assertFalse(repaired["accepted"])
        self.assertIn("你指的是哪一个", execution.user_answer)

    def test_without_page_search_the_words_searched_for_name_the_record(self):
        _execution, _channel, _chat, port, _events = self._run(
            [("declare_goal", {"deliverables": [
                {"kind": "page", "entity": "order", "page": {"key": "p1", "search": "S00006"}}]})],
            [Resolved(kind="page", page=PagePlan(url="/orders/6", label="S00006",
                                                 expected_path="/orders/6"))],
            offers=OPTIONAL - {"search"})
        asked = port.specs[0].deliverables[0]
        self.assertEqual((asked.record, asked.page.search), ("S00006", ""))

    def test_without_page_search_the_name_of_what_is_listed_is_dropped(self):
        _execution, _channel, _chat, port, _events = self._run(
            [("declare_goal", {"deliverables": [
                {"kind": "list", "entity": "order", "page": {"key": "p1", "search": "销售订单"}}]})],
            [Resolved(kind="list", aggregate=Aggregate(subject="Orders", count=0, records=()))],
            offers=OPTIONAL - {"search"})
        asked = port.specs[0].deliverables[0]
        self.assertEqual((asked.record, asked.page.search), ("", ""))

    def test_without_page_search_words_a_condition_already_holds_are_dropped(self):
        _execution, _channel, _chat, port, _events = self._run(
            [("declare_goal", {"deliverables": [
                {"kind": "list", "entity": "order", "metric": {
                    "op": "count", "filters": [{"field": "Customer", "values": ["Acme Ltd"]}]},
                 "page": {"key": "p1", "search": "Acme Ltd"}}]})],
            [Resolved(kind="list", aggregate=Aggregate(subject="Orders", count=0, records=()))],
            offers=OPTIONAL - {"search"})
        asked = port.specs[0].deliverables[0]
        self.assertEqual((asked.record, asked.page.search), ("", ""))

    def test_a_repair_is_read_with_the_entity_where_the_name_was_found(self):
        class _TwoEntities(_Port):
            def index(self, *, context=None):
                base = _index()
                return replace(base, entities=base.entities + (
                    IndexEntity(key="vendor", label="Vendor", aliases=("供应商",)),),
                    pages=base.pages + (IndexPage(key="p9", label="Vendors",
                                                  breadcrumb="Purchase / Vendors", entity="vendor"),))
        port = _TwoEntities(
            [Resolved(kind="fact", problem="no Sales Order matches 'Acme'", missing="Acme",
                      elsewhere=("vendor",),
                      repair="'Acme' is Vendor record 'Acme Ltd' (entity vendor)")],
            then=[Resolved(kind="fact", facts=(Fact("Acme Ltd", "Phone", "123"),))])
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "Acme"}]}),
                   _declaration(kind="fact", entity="order", record="Acme", fields=["Total"]),
                   _declaration(kind="fact", entity="vendor", record="Acme Ltd", fields=["Phone"])]
        execution, _channel, chat, port, events = self._run(
            scripts, [], text="Acme 的电话是多少？", staged=True, port=port, readings=1)
        self.assertIn("123", execution.user_answer)
        # The first details were read without the vendor; the repair with it.
        self.assertNotIn("vendor: Vendor", chat.messages[1][1]["content"])
        self.assertIn("vendor: Vendor", chat.messages[-1][1]["content"])

    def test_a_repair_may_not_answer_from_another_kind_than_the_one_the_person_named(self):
        class _TwoEntities(_Port):
            def index(self, *, context=None):
                base = _index()
                return replace(base, entities=base.entities + (
                    IndexEntity(key="vendor", label="Vendor", aliases=("供应商",)),),
                    pages=base.pages + (IndexPage(key="p9", label="Vendors",
                                                  breadcrumb="Purchase / Vendors", entity="vendor"),))
        port = _TwoEntities(
            [Resolved(kind="fact", problem="no Sales Order matches 'Acme'", missing="Acme",
                      elsewhere=("vendor",),
                      repair="'Acme' is Vendor record 'Acme Ltd' (entity vendor)")],
            then=[Resolved(kind="fact", facts=(Fact("Acme Ltd", "Phone", "123"),))])
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "Acme"}]}),
                   _declaration(kind="fact", entity="order", record="Acme", fields=["Total"]),
                   _declaration(kind="fact", entity="vendor", record="Acme Ltd", fields=["Phone"])]
        execution, _channel, _chat, _port, events = self._run(
            scripts, [], text="Acme 这张销售订单的金额是多少？", staged=True, port=port, readings=1)
        self.assertNotIn("123", execution.user_answer)
        self.assertIn("没有找到名为「Acme」", execution.user_answer)

    def test_a_repair_that_still_finds_nothing_keeps_the_name_not_found(self):
        port = _Port(
            [Resolved(kind="fact", problem="no Sales Order matches 'Acme'", missing="Acme",
                      repair="no Sales Order is named 'Acme'")],
            then=[Resolved(kind="fact", problem="no Sales Order matches these conditions",
                           repair="re-read the request")])
        execution, _channel, _chat, _port, events = self._run(
            [_declaration(kind="fact", entity="order", record="Acme", fields=["Total"]),
             ("declare_goal", {"deliverables": [{"kind": "fact", "entity": "order", "metric": {
                 "op": "count", "filters": [{"field": "Customer", "values": ["Acme"], "op": "contains"}]}}]})],
            [], text="Acme 的订单金额是多少？", port=port, readings=1)
        self.assertEqual([e["accepted"] for e in events if e["event"] == "goal_repaired"], [False])
        self.assertIn("没有找到名为「Acme」", execution.user_answer)

    def test_the_staged_path_locates_references_chooses_a_scope_then_declares_details(self):
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "S00006"}]}),
                   _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])]
        execution, _channel, chat, port, events = self._run(
            scripts, [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            text="S00006 的客户是谁？", staged=True)
        self.assertIn("Lumber Inc", execution.user_answer)
        # The scope once, then the details read twice in parallel; the readings agree.
        self.assertEqual(chat.tool_names, [{"declare_scope"}, {"declare_goal"}, {"declare_goal"}])
        first, second = chat.messages[0][1]["content"], chat.messages[1][1]["content"]
        self.assertTrue(first.startswith("ENTITIES"))
        self.assertIn("named in request: S00006", first)
        self.assertIn("KNOWN REFERENCES", first)
        self.assertIn("APPLICATION INDEX", second)
        self.assertIn("SCOPE", second)
        # The request opens the message and closes it.
        self.assertTrue(second.startswith("REQUEST\nS00006 的客户是谁？"))
        self.assertTrue(second.endswith("REQUEST (again)\nS00006 的客户是谁？"))
        self.assertIn('"about":"S00006"', second)
        kinds = [e["event"] for e in events]
        for name in ("goal_references", "goal_scope_declared", "goal_neighbourhood", "goal_declared",
                     "goal_second_reading"):
            self.assertIn(name, kinds)
        self.assertEqual(port.specs[0].deliverables[0].record, "S00006")

    def test_names_the_scope_copies_are_located_and_join_the_known_references(self):
        # A customer written in Chinese has no reference shape; the first step copies it
        # and the port finds the record it names on the entities the parts are about.
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order",
                                                 "names": ["厦门海湾食品有限公司", " "]}]}),
                   _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])]
        asked = []

        class _NamesPort(_Port):
            def locate_names(self, names, *, entities=(), context=None):
                asked.append((names, entities))
                return tuple((n, "partner", n) for n in names if n.startswith("厦门"))

        port = _NamesPort([Resolved(kind="fact", facts=(Fact("S00006", "Customer", "厦门海湾食品有限公司"),))])
        execution, _channel, chat, _port, events = self._run(
            scripts, [], text="厦门海湾食品有限公司的订单客户是谁？", staged=True, port=port)
        self.assertEqual(asked, [(("厦门海湾食品有限公司",), ("order",))])
        named = next(e for e in events if e["event"] == "goal_names")
        self.assertEqual(named["found"], [{"text": "厦门海湾食品有限公司", "entity": "partner",
                                           "name": "厦门海湾食品有限公司"}])
        self.assertIn('"entity":"partner"', chat.messages[1][1]["content"])

    def test_a_staged_scope_with_no_parts_ends_the_turn_honestly(self):
        execution, _channel, chat, port, _events = self._run(
            [("declare_scope", {"parts": [], "unsupported": "索引里没有银行数据"})],
            [], text="Acme 昨天在银行到账多少？", staged=True)
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertIn("索引里没有银行数据", execution.user_answer)
        self.assertEqual(len(chat.messages), 1)
        self.assertEqual(port.specs, [])

    def test_an_empty_second_step_after_a_found_scope_is_asked_once_more(self):
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "S00006"}]}),
                   ("declare_goal", {"deliverables": [], "unsupported": "the message contains only the index"}),
                   _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])]
        execution, _channel, chat, _port, events = self._run(
            scripts, [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            text="S00006 的客户是谁？", staged=True, readings=1)
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("Lumber Inc", execution.user_answer)
        self.assertIn("the request asks for: fact on order (S00006)", chat.messages[2][1]["content"])
        self.assertTrue(any(e.get("redeclared") for e in events if e["event"] == "goal_declared"))

    def test_candidate_values_are_shown_and_their_ids_bound_before_resolving(self):
        from web_operation_agent.goal.ports import Candidate

        class _ValuesPort(_Port):
            def candidates(self, *, entities, known, dates, context=None):
                return (Candidate(kind="record", entity="order", display="S00006",
                                  detail="Customer Lumber Inc"),)

        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "Lumber"}]}),
                   _declaration(kind="fact", entity="order", record="v1", fields=["Customer"])]
        channel = _Channel(page_url="http://127.0.0.1:8080/home", decision="approved")
        chat = _AgreeingChat(scripts)
        port = _ValuesPort([Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))])
        executor = GoalTurnExecutor(
            channel=channel, config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=chat, application=bind_application(_Adapter()), goal_port=port,
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run", staged=True)
        execution = executor.execute({"message_id": "m1", "text": "Lumber 那张订单的客户？",
                                      "page_url": "http://127.0.0.1:8080/home"})
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("v1 record order: S00006 — Customer Lumber Inc", chat.messages[1][1]["content"])
        # Both readings wrote the id; the port was asked for the exact name.
        self.assertEqual(port.specs[0].deliverables[0].record, "S00006")
        events = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        self.assertEqual(next(e for e in events if e["event"] == "goal_values")["count"], 1)
        self.assertTrue(any(e.get("values_bound") == 1 for e in events if e["event"] == "goal_declared"))

    def test_a_declaration_that_leaves_out_what_was_asked_is_sent_back_once(self):
        from web_operation_agent.goal.executor import _coverage_gaps
        page_only = GoalSpec.from_json({"deliverables": [
            {"kind": "page", "entity": "order", "record": "S00006", "section": "Order Lines"}]})
        gaps = _coverage_gaps(page_only, [{"kind": "list", "entity": "line"}], "S00006 的明细列一下", "2026-09-26")
        self.assertEqual(len(gaps), 1)
        self.assertIn("asks for values (list on line)", gaps[0])
        dropped = GoalSpec.from_json({"deliverables": [
            {"kind": "fact", "entity": "order", "fields": ["Total"],
             "metric": {"op": "count", "filters": [{"field": "Customer", "values": ["Gemini"]}]}}]})
        gaps = _coverage_gaps(dropped, [], "Gemini 那笔 1582 美元、8 月 29 日的订单", "2026-09-26")
        self.assertEqual([g.split(" but ")[0] for g in gaps],
                         ["the request says '1582'", "the request names the day 2026-08-29"])
        reads_only = GoalSpec.from_json({"deliverables": [
            {"kind": "list", "entity": "product", "metric": {"op": "count", "filters": [
                {"field": "Sales Price", "values": ["2100"]}]}}]})
        gaps = _coverage_gaps(reads_only, [{"kind": "page", "entity": "product"}],
                              "I want the details for the product priced at $2,100.", "2026-09-26")
        self.assertIn("asks to open a page (page on product)", gaps[0])
        kept = GoalSpec.from_json({"deliverables": [
            {"kind": "fact", "entity": "order", "fields": ["Total"],
             "metric": {"op": "count", "filters": [{"field": "Total", "values": ["1,582.00"]},
                                                    {"field": "Order Date", "values": ["2026-08-29"]}]}}]})
        self.assertEqual(_coverage_gaps(kept, [], "Gemini 那笔 1582 美元、8 月 29 日的订单", "2026-09-26"), ())
        # Through a turn: the page-only reading is sent back and the list replaces it.
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "S00006"}]}),
                   _declaration(kind="page", entity="order", record="S00006", section="Order Lines"),
                   _declaration(kind="page", entity="order", record="S00006", section="Order Lines"),
                   _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])]
        execution, _channel, chat, port, events = self._run(
            scripts, [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            text="S00006 的客户是谁？", staged=True)
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertTrue(any(e["event"] == "goal_coverage_gaps" for e in events))
        self.assertEqual(port.specs[0].deliverables[0].kind, "fact")

    def test_several_matching_records_are_asked_about_and_the_reply_continues(self):
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "Ready Mat"}]}),
                   _declaration(kind="fact", entity="order", fields=["Total"],
                                metric={"op": "count", "filters": [{"field": "Customer", "values": ["Ready Mat"]}]})]
        channel = _Channel(page_url="http://127.0.0.1:8080/home", decision="approved")
        chat = _AgreeingChat(scripts)
        port = _Port([Resolved(kind="fact", problem="several records match",
                               candidates=("S00002", "S00003"))])
        from web_operation_agent.state.conversation import ConversationState
        state = ConversationState()
        executor = GoalTurnExecutor(
            channel=channel, config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=chat, application=bind_application(_Adapter()), goal_port=port,
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run", staged=True,
            conversation_state=state)
        first = executor.execute({"message_id": "m1", "text": "Ready Mat 那份报价的总额？",
                                  "page_url": "http://127.0.0.1:8080/home"})
        self.assertEqual(first.status, AgentTurnStatus.NEEDS_INPUT)
        self.assertIn("你指的是哪一个？", first.user_answer)
        self.assertIn("1. S00002", first.user_answer)
        self.assertIn("2. S00003", first.user_answer)
        self.assertEqual([c["record_ref"] for c in state.pending_clarification["choices"]],
                         ["S00002", "S00003"])
        self.assertEqual(executor._continued(AgentTurnMessage(message_id="m2", text="2",
                                                              page_url="http://127.0.0.1:8080/home")),
                         ("Ready Mat 那份报价的总额？（指的是 S00003）", "S00003"))
        self.assertIsNone(executor._continued(AgentTurnMessage(message_id="m2", text="换个问题：S00001 呢",
                                                               page_url="http://127.0.0.1:8080/home")))

    def test_a_follow_up_is_read_with_the_earlier_turns(self):
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "S00006"}]}),
                   _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"]),
                   ("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "S00006"}]}),
                   _declaration(kind="fact", entity="order", record="S00006", fields=["Total"])]
        channel = _Channel(page_url="http://127.0.0.1:8080/home", decision="approved")
        chat = _Chat(scripts)
        port = _Port([Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
                     [Resolved(kind="fact", facts=(Fact("S00006", "Total", "750.00 USD"),))])
        from web_operation_agent.state.conversation import ConversationState
        executor = GoalTurnExecutor(
            channel=channel, config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=chat, application=bind_application(_Adapter()), goal_port=port,
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run", staged=True,
            readings=1, conversation_state=ConversationState())
        executor.execute({"message_id": "m1", "text": "S00006 的客户是谁？",
                          "page_url": "http://127.0.0.1:8080/home"})
        second = executor.execute({"message_id": "m2", "text": "它的总金额呢？",
                                   "page_url": "http://127.0.0.1:8080/home"})
        self.assertIn("750.00 USD", second.user_answer)
        # The first turn had nothing before it; both steps of the second read the first.
        self.assertNotIn("EARLIER IN THIS CONVERSATION", chat.messages[0][1]["content"])
        for step in (chat.messages[2], chat.messages[3]):
            content = step[1]["content"]
            self.assertIn("EARLIER IN THIS CONVERSATION", content)
            self.assertIn("person: S00006 的客户是谁？", content)
            self.assertIn("Lumber Inc", content)
            self.assertTrue(content.rstrip().endswith("它的总金额呢？"))
        # The record the earlier request named is located for the follow-up.
        events = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        located = [e["references"] for e in events if e["event"] == "goal_references"]
        self.assertEqual(located[-1], [{"text": "S00006", "entity": "order", "name": "S00006"}])

    def test_scope_terms_in_the_applications_language_are_kept_per_part(self):
        from web_operation_agent.goal.spec import scope_from_json
        parts, _write, _why, _about = scope_from_json({"parts": [
            {"kind": "fact", "entity": "order", "rules": True,
             "terms": ["archived analytic account", " post ", "", 7, "x", "y"]}]})
        self.assertEqual(parts[0]["terms"], ["archived analytic account", "post", "x"])
        self.assertNotIn("terms", scope_from_json({"parts": [{"kind": "fact", "entity": "order"}]})[0][0])
        named = scope_from_json({"parts": [{"kind": "fact", "entity": "order",
                                            "names": ["  厦门海湾 ", "", 3, "A", "B", "C", "D"]}]})[0][0]
        self.assertEqual(named["names"], ["厦门海湾", "A", "B", "C"])

    def test_a_data_question_is_declared_without_the_rules_catalogue(self):
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "S00006"}]}),
                   _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])]
        execution, _channel, chat, _port, _events = self._run(
            scripts, [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            text="S00006 的客户是谁？", staged=True)
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertNotIn("RULES", chat.messages[1][1]["content"])

    def test_rules_render_only_in_a_focused_index(self):
        full = _index()
        self.assertNotIn("RULES", full.render())
        focused = neighbourhood(full, ("order",))
        text = focused.render()
        self.assertIn("RULES of the deployment's source", text)
        self.assertIn("Sales Order · visibility: r1 Create Invoice", text)
        # The catalogue names rules; their text and citations stay on the server.
        self.assertNotIn("hidden when", text)
        self.assertNotIn("sha256", text)

    def test_a_rule_question_is_answered_from_the_index_with_its_citation(self):
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "S00001", "rules": True}]}),
                   _declaration(kind="fact", entity="order", record="S00001", rules=["r1"])]
        execution, _channel, _chat, port, events = self._run(
            scripts, [], text="S00001 上怎么没有开票按钮？", staged=True)
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("hidden when state != sale", execution.user_answer)
        self.assertIn("依据（部署版本源码中的规则，不是当前数据）", execution.user_answer)
        self.assertIn("addons/sale/views/sale_order_views.xml:266-271@18.0 sha256:" + "a" * 64,
                      execution.user_answer)
        # The port was not asked: the rule is the index's, and the citation the server's.
        self.assertEqual(port.specs, [])
        self.assertEqual([a for a, _ in _Browser.instances[0].actions], [])
        # The rules of the same operation come along -- what the window the control
        # opens refuses -- each with its citation; an unrelated rule does not.
        self.assertIn("must be positive", execution.user_answer)
        self.assertIn("addons/sale/wizard/advance.py:125-130@18.0 sha256:" + "c" * 64, execution.user_answer)
        self.assertNotIn("validity", execution.user_answer)

    def test_a_rule_question_the_source_does_not_answer_is_refused_in_the_persons_words(self):
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "", "rules": True}]}),
                   ("declare_goal", {"deliverables": [], "unsupported": "no approval rule"})]
        execution, _channel, _chat, _port, _events = self._run(
            scripts, [], text="报价超过十万美元要两名经理批准吗？", staged=True)
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertIn("部署源码里没有找到关于「报价超过十万美元要两名经理批准吗？」的规则。", execution.user_answer)
        self.assertIn("no approval rule", execution.user_answer)

    def test_a_declaration_naming_many_rules_is_clipped_not_refused(self):
        scripts = [_declaration(kind="fact", entity="order", rules=[f"r{n}" for n in (1, 2, 3, 1, 2, 3, 1, 2, 3, 1)])]
        execution, _channel, _chat, _port, _events = self._run(scripts, [], text="开票按钮的规则？")
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("hidden when state != sale", execution.user_answer)

    def test_a_rule_written_in_two_views_is_said_once_and_cited_twice(self):
        twin = IndexRule(id="r4", kind="visibility", about="Create Invoice", text=_RULE.text,
                         citation="addons/sale/views/sale_order_views.xml:527-530@18.0 sha256:" + "f" * 64)
        index = _index()
        index = ApplicationIndex(entities=(replace(index.entities[0], rules=index.entities[0].rules + (twin,)),),
                                 pages=index.pages, source_digest="abc")

        class _TwinPort(_Port):
            def index(self, *, context=None):
                return index

        scripts = [_declaration(kind="fact", entity="order", rules=["r1"])]
        channel = _Channel(page_url="http://127.0.0.1:8080/home", decision="approved")
        executor = GoalTurnExecutor(
            channel=channel, config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=_AgreeingChat(scripts), application=bind_application(_Adapter()), goal_port=_TwinPort([]),
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run", staged=False)
        answer = executor.execute({"message_id": "m1", "text": "开票按钮何时出现？",
                                   "page_url": "http://127.0.0.1:8080/home"}).user_answer
        self.assertEqual(answer.count("hidden when state != sale"), 1)
        self.assertIn("sale_order_views.xml:266-271@18.0", answer)
        self.assertIn("sale_order_views.xml:527-530@18.0", answer)

    def test_an_unknown_rule_id_is_a_repairable_problem(self):
        scripts = [_declaration(kind="fact", entity="order", record="S00001", rules=["r9"]),
                   _declaration(kind="fact", entity="order", record="S00001", rules=["r9"]),
                   _declaration(kind="fact", entity="order", record="S00001", rules=["r1"])]
        execution, _channel, chat, _port, _events = self._run(scripts, [], text="S00001 上怎么没有开票按钮？")
        self.assertIn("hidden when state != sale", execution.user_answer)
        self.assertIn("rules must be ids shown under RULES", chat.messages[2][1]["content"])

    def test_a_handoff_highlights_and_never_presses(self):
        execution, _channel, _chat, _port, events = self._run(
            [_declaration(kind="handoff", entity="order", record="S00005",
                          action="Send by Email")],
            [Resolved(kind="handoff", page=PagePlan(url="/orders/5", label="S00005",
                                                    expected_path="/orders/5",
                                                    highlight="Send by Email"))],
            text="把 S00005 发邮件给客户")
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertEqual(execution.agent.success_claim, "not_claimed")
        self.assertIn("标出「Send by Email」", execution.user_answer)
        self.assertIn("请你核对后自行点击", execution.user_answer)
        self.assertNotRegex(execution.user_answer, r"不能|无法|做不到|未能")
        browser = _Browser.instances[0]
        self.assertNotIn("click", [a for a, _ in browser.actions])
        self.assertIn("highlight", [a for a, _ in browser.actions])
        self.assertIn("page_handoff_ready", [e["event"] for e in events])

    class _MenuBrowser(_Browser):
        """A record page with two collapsed menus, and a navigation item that happens to
        be called what the handoff asks for."""
        MENUS = (("Filters", ("Last week",)), ("Actions", ("Archive", "Delete")))

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.opened: set[int] = set()

        def _observation(self):
            obs = super()._observation()
            # A navigation item and a tag's remove link that share the name.
            extra = [ElementRef(ref="n0", role="menuitem", name="Delete", tag="a",
                                epoch=self.epoch),
                     ElementRef(ref="k0", role="link", name="Delete", tag="a",
                                epoch=self.epoch, attrs={"href": "#"})]
            for i, (name, items) in enumerate(self.MENUS):
                open_ = i in self.opened
                extra.append(ElementRef(
                    ref=f"m{i}", role="button", name=name, tag="button", epoch=self.epoch,
                    attrs={"aria-expanded": "true"} if open_ else {"revealable": "true"}))
                if open_:
                    extra += [ElementRef(ref=f"i{i}_{j}", role="menuitem", name=item,
                                         tag="span", epoch=self.epoch)
                              for j, item in enumerate(items)]
            return replace(obs, elements=list(obs.elements) + extra)

        def execute(self, action, args=None, expect_navigation=False):
            if action == "reveal":
                self.opened.add(int(args["ref"][1:]))
            return super().execute(action, args, expect_navigation)

    def test_a_control_in_a_collapsed_menu_is_found_by_opening_it(self):
        with patch.object(executor_module, "RemoteBrowserClient", self._MenuBrowser):
            execution, _channel, _chat, _port, events = self._run(
                [_declaration(kind="handoff", entity="order", record="S00005",
                              action="Delete")],
                [Resolved(kind="handoff", page=PagePlan(url="/orders/5", label="S00005",
                                                        expected_path="/orders/5",
                                                        highlight="Delete"))],
                text="删除 S00005")
        browser = self._MenuBrowser.instances[-1]
        acted = [(a, args.get("ref")) for a, args in browser.actions
                 if a in ("reveal", "highlight", "click")]
        # The navigation item is never marked; the menus open in page order; the item that
        # appeared is marked and nothing is pressed.
        self.assertEqual(acted, [("reveal", "m0"), ("reveal", "m1"), ("highlight", "i1_1")])
        self.assertIn("展开「Actions」菜单并标出「Delete」", execution.user_answer)
        self.assertIn("请你核对后自行点击", execution.user_answer)
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertEqual([e["menu"] for e in events if e["event"] == "goal_menu_revealed"],
                         ["Filters", "Actions"])

    def test_a_control_in_no_menu_is_reported_missing_after_a_bounded_search(self):
        class _ManyMenus(self._MenuBrowser):
            MENUS = tuple((f"Menu {i}", ("Nothing",)) for i in range(5))

        with patch.object(executor_module, "RemoteBrowserClient", _ManyMenus):
            execution, *_ = self._run(
                [_declaration(kind="handoff", entity="order", record="S00005",
                              action="Delete")],
                [Resolved(kind="handoff", page=PagePlan(url="/orders/5", label="S00005",
                                                        expected_path="/orders/5",
                                                        highlight="Delete"))],
                text="删除 S00005")
        browser = _ManyMenus.instances[-1]
        self.assertEqual([a for a, _ in browser.actions].count("reveal"),
                         executor_module._MAX_REVEALS)
        self.assertNotIn("highlight", [a for a, _ in browser.actions])
        self.assertIn("页面上没有「Delete」这个控件", execution.user_answer)
        self.assertIn("没有执行任何操作", execution.user_answer)
        self.assertNotIn("已打开", execution.user_answer)

    def test_a_page_declared_with_a_control_is_handed_over_with_that_control_marked(self):
        port_resolved = [Resolved(kind="handoff", page=PagePlan(
            url="/orders/5", label="S00005", expected_path="/orders/5",
            highlight="Send by Email"))]
        execution, _channel, _chat, port, events = self._run(
            [_declaration(kind="page", entity="order", record="S00005", action="Send by Email")],
            port_resolved, text="S00005 的发邮件按钮帮我标出来")
        self.assertEqual(port.specs[0].deliverables[0].kind, "handoff")
        self.assertIn("标出「Send by Email」", execution.user_answer)
        self.assertEqual([e["controls"] for e in events if e["event"] == "goal_page_became_handoff"],
                         [["Send by Email"]])

    def test_a_control_the_request_names_stays_when_a_reading_drops_it(self):
        # The first step named the control the request writes; one of the two readings
        # came back without it. The request's own words put it back before the readings
        # are compared, so they agree and no third reading is asked for.
        scope = ("declare_scope", {"parts": [{"kind": "page", "entity": "order", "about": "S00006",
                                              "terms": ["Send by Email"]}]})
        with_control = _declaration(kind="page", entity="order", record="S00006",
                                    action="Send by Email")
        without = _declaration(kind="page", entity="order", record="S00006")
        execution, _channel, chat, port, events = self._run(
            [scope, with_control, without],
            [Resolved(kind="handoff", page=PagePlan(url="/orders/6", label="S00006",
                                                    expected_path="/orders/6",
                                                    highlight="Send by Email"))],
            text="S00006 的 Send by Email 给我指出来", staged=True)
        self.assertEqual(len(chat.messages), 3)
        self.assertEqual((port.specs[-1].deliverables[0].kind, port.specs[-1].deliverables[0].action),
                         ("handoff", "Send by Email"))
        self.assertIn("标出「Send by Email」", execution.user_answer)
        self.assertEqual([e["controls"] for e in events if e["event"] == "goal_control_named"],
                         [["Send by Email"]])
        self.assertFalse(any(e.get("reconciled") for e in events if e["event"] == "goal_declared"))

    def test_a_control_the_first_step_named_but_the_request_did_not_write_is_not_added(self):
        # The first step's term is the model's guess when the request never writes that
        # word; a page stays a page.
        scope = ("declare_scope", {"parts": [{"kind": "page", "entity": "order", "about": "S00006",
                                              "terms": ["Confirm"]}]})
        execution, _channel, _chat, port, events = self._run(
            [scope, _declaration(kind="page", entity="order", record="S00006")],
            [Resolved(kind="page", page=PagePlan(url="/orders/6", label="S00006",
                                                 expected_path="/orders/6"))],
            text="把 S00006 打开", staged=True, readings=1)
        self.assertEqual(port.specs[0].deliverables[0].kind, "page")
        self.assertFalse(any(e["event"] == "goal_control_named" for e in events))
        self.assertTrue(executor_module._names_verbatim("找到 Send 按钮", "Send"))
        self.assertFalse(executor_module._names_verbatim("the Sender field", "Send"))

    def test_readings_that_disagree_on_whether_the_data_exists_end_in_the_refusal(self):
        # One reading maps the request to a neighbouring field; the other says the
        # application holds no such thing. Whichever arrives first, the turn says what
        # is missing, asks no third reading and reads nothing.
        neighbour = _declaration(kind="fact", entity="order", record="S00006", fields=["Total"])
        missing = ("declare_goal", {"deliverables": [],
                                    "unsupported": "订单上没有银行清算批次号这个字段"})
        execution, _channel, chat, port, events = self._run(
            [neighbour, missing], [Resolved(kind="fact", facts=(Fact("S00006", "Total", "10"),))],
            text="S00006 的银行清算批次号是多少？")
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertIn("银行清算批次号", execution.user_answer)
        self.assertEqual(len(chat.messages), 2)
        self.assertEqual([s for s in port.specs if s.deliverables], [])
        self.assertFalse(any(e.get("reconciled") for e in events if e["event"] == "goal_declared"))

    def test_a_question_about_rules_is_not_refused_for_a_reading_that_saw_no_data(self):
        scope = ("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "rules": True,
                                              "about": "Create Invoice"}]})
        ruled = _declaration(kind="fact", entity="order", rules=["r1"])
        missing = ("declare_goal", {"deliverables": [], "unsupported": "库存实体不在索引里"})
        execution, _channel, _chat, _port, events = self._run(
            [scope, ruled, missing], [], text="Create Invoice 按钮什么时候不显示？", staged=True)
        self.assertFalse(any(e["event"] == "goal_readings_disagree_on_existence" for e in events))
        self.assertIn("Create Invoice", execution.user_answer)

    def test_an_empty_second_step_that_says_what_is_missing_is_not_asked_again(self):
        # Both steps say the application does not hold it: the first found the order the
        # request names, and noted that the thing asked of it is not recorded.
        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "S00006"}],
                                      "unsupported": "签收人身份证号不在索引里"}),
                   ("declare_goal", {"deliverables": [], "unsupported": "订单上没有签收人身份证号"})]
        execution, _channel, chat, port, events = self._run(
            scripts, [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            text="S00006 的签收人身份证号？", staged=True, readings=1)
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertIn("签收人身份证号", execution.user_answer)
        self.assertEqual(len(chat.messages), 2)
        self.assertFalse(any(e.get("redeclared") for e in events if e["event"] == "goal_declared"))

    def test_an_operation_the_account_may_not_perform_is_said_and_not_asked_about(self):
        execution, *_ = self._run(
            [_declaration(kind="handoff", entity="order", record="S00005", action="Delete")],
            [Resolved(kind="handoff", problem="this account may not delete Sales Order records",
                      denied="delete")],
            text="把 S00005 删除", readings=1)
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        # Named as the person names them: the entity's Chinese name.
        self.assertIn("你的账号没有删除销售订单的权限", execution.user_answer)
        self.assertNotIn("你指的是哪一个", execution.user_answer)
        # A request asking one thing is quoted, and a creation is of records of a kind.
        execution, *_ = self._run(
            [_declaration(kind="handoff", entity="order", action="New", value="Solar Lamp")],
            [Resolved(kind="handoff", problem="this account may not create Product records",
                      denied="create")],
            text="新建一个叫 Solar Lamp 的产品，卖价定 49。", readings=1)
        self.assertIn("「新建一个叫 Solar Lamp 的产品，卖价定 49。」没有执行：你的账号没有新建销售订单的权限",
                      execution.user_answer)

    def test_a_value_of_one_record_listed_over_several_is_asked_about(self):
        # The first step read the request as one record's value; the declaration listed
        # the records a description fits, and two do: which one is asked, not both shown.
        scope = ("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "about": "Ready Mat"}]})
        listed = _declaration(kind="list", entity="order", fields=["Total"], metric={
            "op": "count", "filters": [{"field": "Customer", "values": ["Ready Mat"]}]})
        two = Aggregate(subject="Quotations · Ready Mat", count=2, records=("S00003", "S00002"))
        execution, *_ = self._run([scope, listed], [Resolved(kind="list", aggregate=two)],
                                  text="Ready Mat 的报价单金额是多少？", staged=True, readings=1)
        self.assertEqual(execution.status, AgentTurnStatus.NEEDS_INPUT)
        self.assertIn("1. S00003", execution.user_answer)
        # Read as a list from the first step, the records are listed.
        scope = ("declare_scope", {"parts": [{"kind": "list", "entity": "order", "about": "Ready Mat"}]})
        execution, *_ = self._run([scope, listed], [Resolved(kind="list", aggregate=two)],
                                  text="Ready Mat 的报价单金额是多少？", staged=True, readings=1)
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)

    def test_a_count_over_the_entity_the_request_names_drops_a_narrowing_page(self):
        index = ApplicationIndex(
            entities=(IndexEntity(key="partner", label="Contact", aliases=("联系人",)),),
            pages=(IndexPage(key="p2", label="Customers", breadcrumb="Sales / Customers",
                             entity="partner", aliases=("客户",), scope=("Customers",)),),
            source_digest="abc")
        spec = GoalSpec.from_json({"deliverables": [{
            "kind": "aggregate", "entity": "partner", "metric": {"op": "count"},
            "page": {"key": "p2", "filters_on": ["Companies"]}}]})
        # "联系人里": every contact, not the customers the page shows on its own; the
        # filter the declaration switched on stays.
        dropped, pages = executor_module._entity_over_page(spec, "联系人里标记为公司的有几个？", index)
        self.assertEqual(pages, ("p2",))
        self.assertEqual((dropped.deliverables[0].page.key,
                          dropped.deliverables[0].page.filters_on), ("", ("Companies",)))
        # "客户里": the page is what the request names.
        self.assertEqual(executor_module._entity_over_page(spec, "客户里标记为公司的有几个？", index),
                         (spec, ()))
        # Opening the page is left alone.
        opened = GoalSpec.from_json({"deliverables": [{"kind": "page", "entity": "partner",
                                                       "page": {"key": "p2"}}]})
        self.assertEqual(executor_module._entity_over_page(opened, "打开联系人", index), (opened, ()))

    def test_a_refusal_over_a_field_no_page_shows_is_read_once_more_with_it(self):
        base = _index()
        order = replace(base.entities[0], held=(IndexField(key="weight", label="Weight",
                                                           kind="number", aliases=("重量",)),))
        index = replace(base, entities=(order,))

        class _HeldPort(_Port):
            def index(self, *, context=None):
                return index
        scope = ("declare_scope", {"parts": [{"kind": "list", "entity": "order", "about": "重量"}]})
        missing = ("declare_goal", {"deliverables": [], "unsupported": "订单没有重量字段"})
        weighed = _declaration(kind="list", entity="order", fields=["Weight"], metric={
            "op": "count", "filters": [{"field": "Weight", "values": ["1"], "op": ">"}]})
        listed = Resolved(kind="list", aggregate=Aggregate(subject="Orders · Weight > 1", count=1,
                                                           records=("S00006",)))
        channel = _Channel(page_url="http://127.0.0.1:8080/home", decision="approved")
        chat = _AgreeingChat([scope, missing, missing, weighed])
        port = _HeldPort([listed])
        executor = GoalTurnExecutor(
            channel=channel, config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=chat, application=bind_application(_Adapter()), goal_port=port,
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run", staged=True)
        execution = executor.execute({"message_id": "m1", "text": "重量超过 1 公斤的订单有哪些？",
                                      "page_url": "http://127.0.0.1:8080/home"})
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("S00006", execution.user_answer)
        # The field was shown only to the last reading.
        self.assertNotIn("Weight[重量]", chat.messages[1][1]["content"])
        self.assertIn("held, not shown on its pages: Weight[重量]", chat.messages[-1][1]["content"])
        events = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        self.assertEqual([e["fields"] for e in events if e["event"] == "goal_held_fields_offered"],
                         [["Sales Order.Weight"]])

    def test_a_controls_rules_asked_for_on_a_named_record_point_at_the_control(self):
        from web_operation_agent.goal.narrow import KnownReference
        index = _index()
        known = (KnownReference(text="S00006", entity="order", name="S00006"),)
        ruled = GoalSpec.from_json({"deliverables": [{"kind": "fact", "entity": "order",
                                                      "record": "S00006", "fields": ["Confirm"],
                                                      "rules": ["r1"]}]})
        # "S00006 的 Confirm 按钮在哪": that record's control is pointed at.
        pointed, controls = executor_module._control_on_a_record(
            ruled, "其实 S00006 还能做，\"Confirm\"按钮在哪？", index, known)
        self.assertEqual(controls, ("Confirm",))
        self.assertEqual((pointed.deliverables[0].kind, pointed.deliverables[0].action,
                          pointed.deliverables[0].rules), ("handoff", "Confirm", ()))
        # A control's rules asked in general name no record, and stay a rules question.
        general = GoalSpec.from_json({"deliverables": [{"kind": "fact", "entity": "order",
                                                        "fields": ["Confirm"], "rules": ["r1"]}]})
        self.assertEqual(executor_module._control_on_a_record(
            general, "Confirm 按钮什么时候出现？", index, known), (general, ()))
        # A record the request does not name by its reference is not taken for one.
        self.assertEqual(executor_module._control_on_a_record(
            ruled, "报价单上的 Confirm 按钮什么时候出现？", index, ()), (ruled, ()))

    def test_a_control_not_on_the_page_is_explained_by_its_source_rule(self):
        # The page shows Confirm and Send by Email; Create Invoice has a visibility rule.
        plan = PagePlan(url="/orders/6", label="S00006", expected_path="/orders/6",
                        highlight="Create Invoice")
        execution, *_ = self._run(
            [_declaration(kind="handoff", entity="order", record="S00006",
                          action="Create Invoice")],
            [Resolved(kind="handoff", page=plan)], text="S00006 开发票，把 Create Invoice 标出来")
        self.assertIn("页面上没有显示「Create Invoice」。部署源码的规则：", execution.user_answer)
        self.assertIn("hidden when state != sale", execution.user_answer)
        self.assertIn("sale_order_views.xml:266-271", execution.user_answer)
        self.assertNotIn("可能是你的账号", execution.user_answer)

    def test_a_request_the_assistant_cannot_do_is_answered_in_the_persons_language(self):
        cannot = executor_module._cannot
        self.assertEqual(cannot("查一下签收人的身份证号", "应用里没有签收人身份证号", True),
                         "这个请求助手做不到。（应用里没有签收人身份证号）")
        # A reason the model wrote in another language is not shown; the request is.
        self.assertEqual(cannot("把客户发票在地图上标出来", "The index has no map view.", True),
                         "「把客户发票在地图上标出来」这个请求助手做不到。")
        self.assertEqual(cannot("Show invoices on a map", "no map view", False),
                         "The assistant cannot do this request. (no map view)")

    def test_a_control_one_reading_declared_and_the_first_step_named_is_kept(self):
        # "确认按钮指给我": the request writes the control in Chinese; one reading
        # declared Confirm, the other only the record; the first step named Confirm.
        scope = ("declare_scope", {"parts": [{"kind": "page", "entity": "order", "about": "S00006",
                                              "terms": ["Confirm"]}]})
        page_only = _declaration(kind="page", entity="order", record="S00006")
        handoff = ("declare_goal", {"deliverables": [{"kind": "handoff", "entity": "order",
                                                      "record": "S00006", "action": "Confirm"}],
                                    "write_intent": True})
        execution, _channel, chat, port, events = self._run(
            [scope, page_only, handoff],
            [Resolved(kind="handoff", page=PagePlan(url="/orders/6", label="S00006",
                                                    expected_path="/orders/6", highlight="Confirm"))],
            text="S00006 客户同意了，确认按钮指给我，我来点。", staged=True)
        self.assertEqual(len(chat.messages), 3)
        self.assertEqual((port.specs[-1].deliverables[0].kind, port.specs[-1].deliverables[0].action),
                         ("handoff", "Confirm"))
        self.assertIn("标出「Confirm」", execution.user_answer)

    def test_a_handoff_that_names_rules_still_points_at_its_control(self):
        declared = [("declare_goal", {"deliverables": [{"kind": "handoff", "entity": "order",
                                                        "record": "S00006", "action": "Confirm",
                                                        "rules": ["r1"]}], "write_intent": True})]
        execution, _channel, _chat, port, _events = self._run(
            declared, [Resolved(kind="handoff", page=PagePlan(
                url="/orders/6", label="S00006", expected_path="/orders/6", highlight="Confirm"))],
            text="S00006 的 Confirm 按钮在哪？", readings=1)
        self.assertEqual(port.specs[-1].deliverables[0].kind, "handoff")
        self.assertIn("标出「Confirm」", execution.user_answer)

    def test_a_named_control_handed_over_is_not_sent_back_for_values(self):
        # The first step read "where is New" as a question; the request writes the
        # page's New, and handing it over is the answer.
        from web_operation_agent.goal.executor import _coverage_gaps, _hands_over_a_named_control
        spec = GoalSpec.from_json({"deliverables": [{"kind": "handoff", "entity": "order",
                                                     "action": "New", "page": {"key": "p1"}}]})
        parts = [{"kind": "fact", "entity": "order", "rules": True, "terms": ["New"]}]
        text = "给客户开 order，New 在哪里？"
        self.assertTrue(_hands_over_a_named_control(spec, parts, text))
        self.assertEqual(_coverage_gaps(spec, parts, text, "2026-09-26", named=True), ())
        self.assertTrue(_coverage_gaps(spec, parts, text, "2026-09-26"))

    def test_a_word_that_names_a_page_is_not_taken_for_the_control_of_that_name(self):
        from web_operation_agent.goal.executor import _named_controls
        index = replace(_index(), entities=(replace(_index().entities[0], actions=("Orders", "Confirm")),))
        spec = GoalSpec.from_json({"deliverables": [{"kind": "page", "entity": "order", "record": "S00006"}]})
        parts = [{"kind": "page", "entity": "order", "terms": ["Orders"]}]
        kept, named = _named_controls(spec, parts, "把 S00006 那张 orders 打开", index)
        self.assertEqual((kept.deliverables[0].kind, named), ("page", ()))

    def test_a_list_page_whose_new_the_request_names_is_a_create_handoff(self):
        scope = ("declare_scope", {"parts": [{"kind": "page", "entity": "order", "about": "",
                                              "terms": ["New", "Orders"]}]})
        execution, _channel, _chat, port, events = self._run(
            [scope, _declaration(kind="page", entity="order", page={"key": "p1"})],
            [Resolved(kind="handoff", page=PagePlan(url="/orders", label="Orders",
                                                    expected_path="/orders", highlight="Confirm"))],
            text="给客户开 order，New 在哪里？", staged=True, readings=1)
        self.assertEqual((port.specs[0].deliverables[0].kind, port.specs[0].deliverables[0].action),
                         ("handoff", "New"))
        self.assertIn("标出「Confirm」", execution.user_answer)
        self.assertEqual([e["controls"] for e in events if e["event"] == "goal_control_named"],
                         [["New"]])

    def test_the_conventions_a_request_names_longest_word_first_narrower_first(self):
        from web_operation_agent.goal.index import IndexConvention, conventions_named
        every = IndexConvention(terms=("订单", "单子"), meaning="order every status", entity="order")
        confirmed = IndexConvention(terms=("销售订单", "成交"), meaning="order confirmed", entity="order",
                                    conditions=(("Status", ("Sales Order",)),))
        quotes = IndexConvention(terms=("报价",), meaning="order not confirmed", entity="order",
                                 conditions=(("Status", ("Quotation",)),))
        words = IndexConvention(terms=("发票",), meaning="invoice, words only")
        table = (every, confirmed, quotes, words)
        self.assertEqual(conventions_named(table, "Gemini 的订单有几张"), (every,))
        # "订单" inside "销售订单" is the longer word's.
        self.assertEqual(conventions_named(table, "Gemini 的销售订单有几张"), (confirmed,))
        # Every order yields to the narrower word on the same entity.
        self.assertEqual(conventions_named(table, "Joel 的订单和报价都列出来"), (quotes,))
        self.assertEqual(conventions_named(table, "成交的单子总额"), (confirmed,))
        # Words-only conventions are the model's to read, not the resolver's.
        self.assertEqual(conventions_named(table, "这张发票的金额"), ())
        with self.assertRaises(ValueError):
            IndexConvention(terms=("x",), meaning="m", conditions=(("Status", ("A",)),))

    def test_an_entity_a_convention_word_names_joins_the_neighbourhood(self):
        from web_operation_agent.goal.index import IndexConvention

        class _TwoEntities(_Port):
            def index(self, *, context=None):
                base = _index()
                partner = IndexEntity(key="partner", label="Contact",
                                      fields=(IndexField(key="name", label="Name", kind="text"),))
                return replace(base, entities=base.entities + (partner,), conventions=(
                    IndexConvention(terms=("成交",), meaning="order confirmed", entity="order",
                                    conditions=(("Status", ("Sales Order",)),)),))

        scripts = [("declare_scope", {"parts": [{"kind": "fact", "entity": "partner", "about": "Gemini"}]}),
                   _declaration(kind="aggregate", entity="order", metric={"op": "count"})]
        channel = _Channel(page_url="http://127.0.0.1:8080/home", decision="approved")
        port = _TwoEntities([Resolved(kind="aggregate", aggregate=Aggregate(subject="Orders", count=3))])
        executor = GoalTurnExecutor(
            channel=channel, config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=_AgreeingChat(scripts), application=bind_application(_Adapter()), goal_port=port,
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run", staged=True, readings=1)
        executor.execute({"message_id": "m1", "text": "Gemini 九月成交了几单？",
                          "page_url": "http://127.0.0.1:8080/home"})
        events = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        neighbourhood = next(e for e in events if e["event"] == "goal_neighbourhood")
        self.assertIn("order", neighbourhood["entities"])
        self.assertEqual(port.specs[-1].conventions[0].terms, ("成交",))
        # A record the request names by its number brings its entity in too.
        self.ledger.write_text("")
        scripts = [("declare_scope", {"parts": [{"kind": "page", "entity": "partner", "about": "S00006"}]}),
                   _declaration(kind="page", entity="order", record="S00006")]
        executor = GoalTurnExecutor(
            channel=_Channel(page_url="http://127.0.0.1:8080/home", decision="approved"),
            config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=_AgreeingChat(scripts), application=bind_application(_Adapter()),
            goal_port=_TwoEntities([Resolved(kind="page", page=PagePlan(
                url="/orders/6", label="S00006", expected_path="/orders/6"))]),
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run", staged=True, readings=1)
        executor.execute({"message_id": "m2", "text": "把 S00006 打开",
                          "page_url": "http://127.0.0.1:8080/home"})
        events = [json.loads(line) for line in self.ledger.read_text().splitlines() if line]
        neighbourhood = next(e for e in events if e["event"] == "goal_neighbourhood")
        self.assertIn("order", neighbourhood["entities"])

    def test_a_sum_is_a_total_only_when_both_readings_say_so(self):
        one = Aggregate(subject="Invoices · Acme · September", count=2,
                        sums=(("Total", "61,000.00 USD"),), records=("INV/2", "INV/3"))
        said = lambda total: ("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
            "metric": {"op": "sum", "field": "Total", "total": total,
                       "filters": [{"field": "Customer", "values": ["Acme"]}]}}]})
        execution, _channel, chat, _port, _events = self._run(
            [said(False), said(True)], [Resolved(kind="aggregate", aggregate=one)],
            text="Acme 九月的发票金额是多少？")
        # One reading alone called it a total: the flag is dropped, the readings agree,
        # and the one-amount rule asks which invoice.
        self.assertEqual(len(chat.messages), 2)
        self.assertNotIn("61,000.00", execution.user_answer)

    def test_an_empty_first_reading_that_says_why_meets_the_second(self):
        missing = ("declare_goal", {"deliverables": [], "unsupported": "订单上没有签收人身份证号"})
        neighbour = _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])
        execution, _channel, chat, port, events = self._run(
            [missing, neighbour], [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            text="S00006 的签收人身份证号？")
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertIn("签收人身份证号", execution.user_answer)
        self.assertEqual([s for s in port.specs if s.deliverables], [])
        # A second reading that reaches an entity the first was not shown replaces it.
        elsewhere = _declaration(kind="fact", entity="invoice", record="INV/1", fields=["Total"])
        execution, _channel, chat, port, events = self._run(
            [missing, elsewhere], [Resolved(kind="fact", facts=(Fact("INV/1", "Total", "10"),))],
            text="S00006 的签收人身份证号？")
        self.assertTrue(any(e.get("taken") == "second" for e in events
                            if e["event"] == "goal_readings_disagree_on_existence"))

    def test_a_read_left_unanswered_counts_as_unresolved_even_with_a_page_beside_it(self):
        from web_operation_agent.goal.executor import _unresolved
        page = PagePlan(url="/orders", label="Orders", expected_path="/orders")
        spec = GoalSpec.from_json({"deliverables": [{"kind": "list", "entity": "order"}]})
        unanswered = Resolution(deliverables=(Resolved(kind="list", problem="no match", page=page),))
        self.assertEqual(_unresolved(spec, unanswered), (1, 0))
        opened = GoalSpec.from_json({"deliverables": [{"kind": "page", "entity": "order"}]})
        self.assertEqual(_unresolved(opened, Resolution(deliverables=(
            Resolved(kind="page", problem="a filter was missing", page=page),))), (0, 0))

    def test_a_request_to_delete_a_described_record_is_a_delete(self):
        from web_operation_agent.goal.executor import _as_handoffs
        spec = GoalSpec.from_json({"deliverables": [{"kind": "handoff", "entity": "order", "action": "Cancel",
            "metric": {"op": "count", "filters": [{"field": "Customer", "values": ["Ready Mat"]}]}}]})
        handed, controls = _as_handoffs(spec, "把 Ready Mat 的草稿报价删除。")
        self.assertEqual(handed.deliverables[0].action, "Delete")
        self.assertEqual(controls, ("Delete",))
        # A handoff with no action named is a Delete too; a denial settles the turn.
        bare = GoalSpec.from_json({"deliverables": [{"kind": "handoff", "entity": "order",
            "metric": {"op": "count", "filters": [{"field": "Customer", "values": ["Ready Mat"]}]}}]})
        self.assertEqual(_as_handoffs(bare, "把 Ready Mat 的草稿报价删除。")[0].deliverables[0].action, "Delete")
        from web_operation_agent.goal.executor import _finds, _unresolved
        denied = Resolution(deliverables=(Resolved(kind="handoff", problem="may not", denied="delete"),))
        self.assertTrue(_finds(denied))
        self.assertEqual(_unresolved(bare, denied), (0, 0))

    def test_a_first_step_that_cannot_be_read_falls_back_to_one_declaration(self):
        scripts = [("declare_scope", {"parts": 5}), ("declare_scope", {"parts": 5}),
                   _declaration(kind="fact", entity="order", record="S00006", fields=["Customer"])]
        execution, _channel, chat, _port, events = self._run(
            scripts, [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            text="S00006 的客户是谁？", staged=True, readings=1)
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("Lumber Inc", execution.user_answer)
        self.assertTrue(any(e["event"] == "goal_scope_unread" for e in events))
        self.assertIn("parts must be an array, not int", next(
            e["detail"] for e in events if e["event"] == "goal_declaration_invalid"))
        self.assertIn("APPLICATION INDEX", chat.messages[-1][1]["content"])

    def test_the_first_step_may_write_its_parts_as_a_string(self):
        from web_operation_agent.goal.spec import scope_from_json
        parts, *_ = scope_from_json({"parts": json.dumps([{"kind": "list", "entity": "order"}])})
        self.assertEqual(parts[0]["entity"], "order")

    def test_a_record_picked_among_a_named_customers_several_is_asked_about(self):
        # "Open Joel's order": the model picked one of Joel's two orders from the values
        # offered and declared no condition; Joel is the condition the request gives.
        class _JoelPort(_Port):
            def locate_reference(self, text, *, context=None):
                return (("partner", "Joel Willis"),) if text == "Joel" else ()

            def resolve(self, spec, *, context=None, current_path=""):
                self.specs.append(spec)
                d = spec.deliverables[0]
                if not d.record:
                    return Resolution(deliverables=(Resolved(
                        kind=d.kind, problem="several", candidates=("S00019", "S00020")),), reads=())
                return Resolution(deliverables=(Resolved(kind="page", page=PagePlan(
                    url="/orders/20", label="S00020", expected_path="/orders/20")),), reads=())

        scripts = [("declare_scope", {"parts": [{"kind": "page", "entity": "order", "about": "Joel 的那张单"}]}),
                   _declaration(kind="page", entity="order", record="S00020")]
        channel = _Channel(page_url="http://127.0.0.1:8080/home", decision="approved")
        port = _JoelPort([])
        executor = GoalTurnExecutor(
            channel=channel, config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=_AgreeingChat(scripts), application=bind_application(_Adapter()), goal_port=port,
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run", staged=True, readings=1)
        execution = executor.execute({"message_id": "m1", "text": "Joel 的那张单打开。",
                                      "page_url": "http://127.0.0.1:8080/home"})
        self.assertEqual(execution.status, AgentTurnStatus.NEEDS_INPUT)
        self.assertIn("S00019", execution.user_answer)
        self.assertIn("S00020", execution.user_answer)
        asked = [spec.deliverables[0] for spec in port.specs if not spec.deliverables[0].record]
        self.assertEqual(asked[0].metric.filters[0].field, "Customer")
        self.assertEqual(asked[0].metric.filters[0].values, ("Joel Willis",))
        # With an amount in the request, the one the model picked may be the one it names.
        port = _JoelPort([])
        executor = GoalTurnExecutor(
            channel=_Channel(page_url="http://127.0.0.1:8080/home", decision="approved"),
            config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=_AgreeingChat(list(scripts)), application=bind_application(_Adapter()), goal_port=port,
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run", staged=True, readings=1)
        execution = executor.execute({"message_id": "m2", "text": "Joel 那张两千九的单打开。",
                                      "page_url": "http://127.0.0.1:8080/home"})
        self.assertNotEqual(execution.status, AgentTurnStatus.NEEDS_INPUT)
        self.assertEqual([spec for spec in port.specs if not spec.deliverables[0].record], [])

    def test_of_two_readings_the_one_that_finds_what_it_describes_is_taken(self):
        customer = ("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
            "metric": {"op": "sum", "field": "Total", "filters": [{"field": "Customer", "values": ["Joel"]}]}}]})
        salesperson = ("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
            "metric": {"op": "sum", "field": "Total", "filters": [{"field": "Salesperson", "values": ["Joel"]}]}}]})

        class _ByField(_Port):
            def resolve(self, spec, *, context=None, current_path=""):
                self.specs.append(spec)
                field = spec.deliverables[0].metric.filters[0].field
                count = 2 if field == "Customer" else 0
                return Resolution(deliverables=(Resolved(kind="aggregate", aggregate=Aggregate(
                    subject=f"Orders · {field} = Joel", count=count,
                    sums=(("Total", "4,687.50 USD" if count else "0.00"),))),), reads=())

        channel = _Channel(page_url="http://127.0.0.1:8080/home", decision="approved")
        chat = _AgreeingChat([customer, salesperson])
        port = _ByField([])
        executor = GoalTurnExecutor(
            channel=channel, config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=chat, application=bind_application(_Adapter()), goal_port=port,
            control=ReadOnlyControl(), run_id_factory=lambda _m: "goal-run", staged=False)
        execution = executor.execute({"message_id": "m1", "text": "Joel 名下两张单总额多少？",
                                      "page_url": "http://127.0.0.1:8080/home"})
        events = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        self.assertEqual(len(chat.messages), 2)
        self.assertIn("4,687.50", execution.user_answer)
        self.assertTrue(any(e["event"] == "goal_readings_one_finds" for e in events))
        self.assertEqual(port.specs[-1].deliverables[0].metric.filters[0].field, "Customer")

    def test_a_sum_declared_as_the_total_of_all_is_not_asked_about(self):
        both = Aggregate(subject="Credit Notes · September", count=3,
                         sums=(("Total", "92,750.00 USD"),))
        declared = [("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
            "metric": {"op": "sum", "field": "Total", "total": True,
                       "filters": [{"field": "Customer", "values": ["Acme"]}]}}]})]
        execution, *_ = self._run(declared, [Resolved(kind="aggregate", aggregate=both)],
                                  text="九月的 customer credits 加总多少？", readings=1)
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("92,750.00", execution.user_answer)

    def test_one_amount_over_several_records_asks_which_and_a_total_does_not(self):
        both = Aggregate(subject="Invoices · Partner = Acme", count=2,
                         sums=(("Total", "61,000.00 USD"),), records=("INV/2026/00002", "INV/2026/00003"))
        declared = [("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
            "metric": {"op": "sum", "field": "Total", "filters": [{"field": "Customer", "values": ["Acme"]}]}}]})]
        execution, *_ = self._run(declared, [Resolved(kind="aggregate", aggregate=both)],
                                  text="Acme 九月的发票金额是多少？")
        self.assertEqual(execution.status, AgentTurnStatus.NEEDS_INPUT)
        self.assertIn("INV/2026/00002", execution.user_answer)
        self.assertIn("INV/2026/00003", execution.user_answer)
        self.assertNotIn("61,000", execution.user_answer)
        total = replace(both, records=())  # a port's sum carries no record names
        execution, *_ = self._run(declared, [Resolved(kind="aggregate", aggregate=total)],
                                  text="Acme 九月的发票总额是多少？")
        self.assertIn("61,000", execution.user_answer)

    def test_that_one_record_listed_twice_asks_which(self):
        two = Aggregate(subject="Credit Notes · Partner = Azure", count=2,
                        records=("RINV/2026/00001", "RINV/2026/00002"))
        execution, *_ = self._run(
            [("declare_goal", {"deliverables": [{"kind": "list", "entity": "order",
                "metric": {"op": "count", "filters": [{"field": "Customer", "values": ["Azure"]}]}}]})],
            [Resolved(kind="list", aggregate=two)], text="Azure 八月的那张退款打开。")
        self.assertEqual(execution.status, AgentTurnStatus.NEEDS_INPUT)
        self.assertIn("RINV/2026/00001", execution.user_answer)
        # A plural request lists them.
        execution, *_ = self._run(
            [("declare_goal", {"deliverables": [{"kind": "list", "entity": "order",
                "metric": {"op": "count", "filters": [{"field": "Customer", "values": ["Azure"]}]}}]})],
            [Resolved(kind="list", aggregate=two)], text="Azure 八月的退款都有哪些？")
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        # Listed one to a line and numbered, so a reply can say "the second one".
        self.assertIn("共 2 条：\n1. RINV/2026/00001\n2. RINV/2026/00002", execution.user_answer)
        one = replace(two, count=1, records=("RINV/2026/00001",))
        execution, *_ = self._run(
            [("declare_goal", {"deliverables": [{"kind": "list", "entity": "order",
                "metric": {"op": "count", "filters": [{"field": "Customer", "values": ["Azure"]}]}}]})],
            [Resolved(kind="list", aggregate=one)], text="Azure 八月的退款都有哪些？")
        self.assertIn("共 1 条：RINV/2026/00001", execution.user_answer)

    def test_a_name_several_records_hold_is_asked_about_whichever_the_model_picked(self):
        # The request writes "Office Chair"; the model picked one of the two records that
        # hold it, by the code the candidate values carry. The pick is the model's.
        priced = [Resolved(kind="fact", facts=(Fact("[P7] Office Chair", "Sales Price", "70.00"),))]
        held = [Resolved(kind="fact", problem="'Office Chair' is ambiguous",
                         candidates=("[P7] Office Chair", "[P8] Office Chair Black"))]
        picked = [_declaration(kind="fact", entity="order", record="[P7] Office Chair",
                               fields=["Sales Price"])]
        execution, _channel, _chat, port, events = self._run(
            picked, priced, resolved_then=held, text="Office Chair 的标价是多少？")
        self.assertEqual(execution.status, AgentTurnStatus.NEEDS_INPUT)
        self.assertIn("Office Chair Black", execution.user_answer)
        self.assertEqual(port.specs[-1].deliverables[0].record, "Office Chair")
        self.assertEqual([e["count"] for e in events if e["event"] == "goal_record_not_chosen"], [2])
        # Passing over the record the words name exactly for a longer one, the model read
        # what the request says to tell them apart ("黑色"): the pick stands.
        black = [_declaration(kind="fact", entity="order", record="[P8] Office Chair Black",
                              fields=["Sales Price"])]
        execution, *_ = self._run(black, priced, resolved_then=held,
                                  text="黑色 Office Chair 的标价是多少？")
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)

    def test_a_record_the_request_never_names_is_not_chosen_for_the_person(self):
        declared = [("declare_goal", {"deliverables": [{"kind": "page", "entity": "order",
            "record": "RINV/2026/00002",
            "metric": {"op": "count", "filters": [{"field": "Customer", "values": ["Azure"]}]}}]})]
        opened = [Resolved(kind="page", page=PagePlan(url="/orders/9", label="RINV/2026/00002",
                                                      expected_path="/orders/9"))]
        both = [Resolved(kind="page", problem="Credit Notes · Customer = Azure matches several",
                         candidates=("RINV/2026/00002", "RINV/2026/00001"))]
        execution, _channel, _chat, port, events = self._run(
            declared, opened, resolved_then=both, text="Azure 八月的那张退款打开。")
        self.assertEqual(execution.status, AgentTurnStatus.NEEDS_INPUT)
        self.assertIn("RINV/2026/00001", execution.user_answer)
        self.assertIn("RINV/2026/00002", execution.user_answer)
        # The check looks with the request's own conditions and without the chosen record.
        self.assertEqual(port.specs[-1].deliverables[0].record, "")
        self.assertEqual([e["count"] for e in events if e["event"] == "goal_record_not_chosen"], [2])
        # A record the request names stands, and is not checked.
        execution, _channel, _chat, port, _events = self._run(
            declared, opened, resolved_then=both, text="Azure 的 RINV/2026/00002 打开。")
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertEqual(len(port.specs), 1)
        # Conditions that fit several records, none of them the one declared, are about
        # something else (the lines of that record); the record stands.
        lines = [Resolved(kind="page", problem="several",
                          candidates=("RINV/2026/00002 Large Meeting Table",
                                      "INV/2026/00001 Large Meeting Table"))]
        execution, *_ = self._run(declared, opened, resolved_then=lines,
                                  text="Azure 那张两万的退款打开。")
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        # A record the request's own day picked out before resolving (the day then left
        # the conditions) stands: what is left of the conditions is not the request.
        from types import SimpleNamespace
        from web_operation_agent.goal.spec import GoalSpec
        port = _Port(both)
        state = SimpleNamespace(
            message=SimpleNamespace(text="Acme 九月二号那笔 credit note 金额多少？"),
            references=(), warranted=("RINV/2026/00002",), ledger=SimpleNamespace(append=lambda *a, **k: None),
            approvals=SimpleNamespace(context=lambda *a: None), obs=SimpleNamespace(url="http://x/"), step=0)
        spec = GoalSpec.from_json(declared[0][1])
        kept = GoalTurnExecutor._not_chosen_for(SimpleNamespace(goal_port=port), state, spec,
                                                Resolution(deliverables=tuple(opened), reads=()))
        self.assertEqual(kept.deliverables, tuple(opened))
        self.assertEqual(port.specs, [])

    def test_a_refusal_raised_on_several_entities_is_answered_in_each(self):
        from types import SimpleNamespace
        from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexRule
        from web_operation_agent.goal.spec import Deliverable
        message = "Prepayment percentage must be a valid percentage."
        on_template = IndexRule(id="r1", kind="refusal", about="_check_prepayment_percent",
                                text=f'_check_prepayment_percent refuses with "{message}" when template.p',
                                citation="addons/sale/models/template.py:1-4@r sha256:a")
        on_order = IndexRule(id="r2", kind="refusal", about="_check_prepayment_percent",
                             text=f'_check_prepayment_percent refuses with "{message}" when order.p',
                             citation="addons/sale/models/sale_order.py:9-12@r sha256:b")
        other = IndexRule(id="r3", kind="refusal", about="action_confirm",
                          text='action_confirm refuses with "Some other message entirely." when x',
                          citation="addons/sale/models/sale_order.py:20-22@r sha256:c")
        index = ApplicationIndex(entities=(
            IndexEntity(key="sale.order.template", label="Quotation Template", rules=(on_template,)),
            IndexEntity(key="sale.order", label="Sales Order", rules=(on_order, other))))
        state = SimpleNamespace(full_index=index, index=index, chinese=False)
        answer = GoalTurnExecutor._rules_answer(None, state, Deliverable(kind="fact", rules=("r1",)))
        # The template was named; the order raises the same refusal and is answered too,
        # and a different refusal of the order is not.
        self.assertEqual([f.subject for f in answer.facts],
                         ["Quotation Template · _check_prepayment_percent",
                          "Sales Order · _check_prepayment_percent"])
        self.assertEqual(len(answer.citations), 2)
        # Placeholders named differently, and what follows the first sentence, do not
        # make it another refusal.
        payment = IndexRule(id="r4", kind="refusal", about="action_post", text=(
            'action_post refuses with "To pay with %(method_name)s, the account must be '
            'validated. Go to %(partner)s." when x'))
        wizard = IndexRule(id="r5", kind="refusal", about="Pay › _create_payments", text=(
            'Pay › _create_payments refuses with "To pay with %(payment_method)s, the account '
            'must be validated. Go to the partner." when y'))
        index = ApplicationIndex(entities=(IndexEntity(key="payment", label="Payment", rules=(payment,)),
                                           IndexEntity(key="move", label="Invoice", rules=(wizard,))))
        state = SimpleNamespace(full_index=index, index=index, chinese=False)
        answer = GoalTurnExecutor._rules_answer(None, state, Deliverable(kind="fact", rules=("r4",)))
        self.assertEqual([f.subject for f in answer.facts],
                         ["Payment · action_post", "Invoice · Pay › _create_payments"])

    def test_a_list_page_for_a_request_to_create_one_is_a_create_handoff(self):
        new = [Resolved(kind="handoff", page=PagePlan(url="/orders", label="Orders",
                                                      expected_path="/orders", highlight="Confirm"))]
        execution, _channel, _chat, port, _events = self._run(
            [_declaration(kind="page", page={"key": "p1"})], new,
            text="我要开一张新的订单，先指一下入口。")
        self.assertEqual((port.specs[0].deliverables[0].kind, port.specs[0].deliverables[0].action),
                         ("handoff", "create"))
        self.assertIn("标出「Confirm」", execution.user_answer)
        # "新建的订单" names orders already made; the page stays a page.
        execution, _channel, _chat, port, _events = self._run(
            [_declaration(kind="page", page={"key": "p1"})],
            [Resolved(kind="page", page=PagePlan(url="/orders", label="Orders", expected_path="/orders"))],
            text="打开新建的订单列表")
        self.assertEqual(port.specs[0].deliverables[0].kind, "page")

    def test_a_request_to_delete_is_never_handed_over_as_another_operation(self):
        with patch.object(executor_module, "RemoteBrowserClient", self._MenuBrowser):
            execution, _channel, _chat, port, _events = self._run(
                [_declaration(kind="handoff", entity="order", record="S00005", action="Archive")],
                [Resolved(kind="handoff", page=PagePlan(url="/orders/5", label="S00005",
                                                        expected_path="/orders/5",
                                                        highlight="Delete"))],
                text="帮我删掉 S00005")
        self.assertEqual(port.specs[-1].deliverables[0].action, "Delete")
        self.assertIn("标出「Delete」", execution.user_answer)
        self.assertNotIn("Archive", execution.user_answer)

    def test_an_amount_the_request_states_is_a_magnitude(self):
        from web_operation_agent.goal.executor import _magnitudes
        spec = GoalSpec.from_json({"deliverables": [{"kind": "list", "entity": "order", "metric": {
            "op": "count", "filters": [{"field": "Total", "values": ["-30000"], "op": "<="},
                                       {"field": "Customer", "values": ["Acme"]}]}}]})
        fixed, fields = _magnitudes(spec, "Acme 三万以上的退款是哪几张？")
        self.assertEqual(fields, ("Total",))
        total = fixed.deliverables[0].metric.filters[0]
        self.assertEqual((total.values, total.op), (("30000",), ">="))
        # A request that writes a negative amount keeps it.
        self.assertEqual(_magnitudes(spec, "余额低于 -30000 的有哪些")[1], ())

    def test_write_intent_cannot_be_settled_by_opening_a_page(self):
        execution, _channel, _chat, _port, events = self._run(
            [("declare_goal", {"deliverables": [{"kind": "page", "page": {"key": "p1"}}],
                               "write_intent": True})],
            [Resolved(kind="page", page=PagePlan(
                url="/orders", label="Sales / Orders", expected_path="/orders"))],
            text="把这张订单确认掉")
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertEqual(execution.agent.success_claim, "not_claimed")
        self.assertIn("尚未执行你请求的更改", execution.user_answer)
        self.assertIn("已打开", execution.user_answer)
        self.assertIn("goal_write_intent_unfulfilled", [e["event"] for e in events])

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
        self.assertIn("没有找到页签「Notes」", execution.user_answer)
        self.assertNotIn("section Notes", execution.user_answer)
        # The audit keeps its own wording.
        settled = [json.loads(line) for line in self.ledger.read_text().splitlines()
                   if '"goal_page_settled"' in line]
        self.assertEqual(settled[-1]["unmet"], ["section Notes"])

    def test_one_deliverable_cannot_complete_a_two_part_request(self):
        execution, *_ = self._run(
            [("declare_goal", {"deliverables": [
                {"kind": "fact", "entity": "order", "record": "S00006",
                 "fields": ["Customer"]},
                {"kind": "fact", "entity": "order", "record": "S00006",
                 "fields": ["Payment Terms"]}]})],
            [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),)),
             Resolved(kind="fact", problem="Payment Terms is not on this record")])
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertEqual(execution.agent.success_claim, "not_claimed")
        self.assertIn("Lumber Inc", execution.user_answer)
        self.assertIn("无法完成这一项", execution.user_answer)
        # The resolver's reason is written for the declarer and stays in the audit.
        self.assertNotIn("Payment Terms is not on this record", execution.user_answer)

    def test_adapter_cannot_drop_a_declared_deliverable(self):
        execution, *_ = self._run(
            [("declare_goal", {"deliverables": [
                {"kind": "fact", "entity": "order", "record": "S00006",
                 "fields": ["Customer"]},
                {"kind": "page", "page": {"key": "p1"}}]})],
            [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))])
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertNotIn("Lumber Inc", execution.user_answer)

    def test_changed_url_without_stable_render_is_not_arrival(self):
        class _NeverReadyBrowser(_Browser):
            def _observation(self):
                return replace(super()._observation(), render_state="pending")

        with (patch.object(executor_module, "RemoteBrowserClient", _NeverReadyBrowser),
              patch.object(executor_module, "_READY_WAITS", 2)):
            execution, *_ = self._run(
                [_declaration(kind="page", page={"key": "p1"})],
                [Resolved(kind="page", page=PagePlan(
                    url="/orders", label="Sales / Orders", expected_path="/orders"))],
                text="打开订单")
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertIn("未完成", execution.user_answer)

    def test_interrupted_render_observations_do_not_count_as_consecutive(self):
        class _FlappingBrowser(_Browser):
            def _observation(self):
                state = "pending" if self.epoch % 2 == 0 else "ready"
                return replace(super()._observation(), render_state=state)

        with (patch.object(executor_module, "RemoteBrowserClient", _FlappingBrowser),
              patch.object(executor_module, "_READY_WAITS", 4)):
            execution, *_ = self._run(
                [_declaration(kind="page", page={"key": "p1"})],
                [Resolved(kind="page", page=PagePlan(
                    url="/orders", label="Sales / Orders", expected_path="/orders"))],
                text="打开订单")
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)

    def test_a_fact_answered_by_listing_its_lines_finishes_the_turn(self):
        execution, *_ = self._run(
            [_declaration(kind="fact", entity="order", fields=["Total"],
                          metric={"op": "count", "filters": [{"field": "Customer", "values": ["Acme"]}]})],
            [Resolved(kind="fact", aggregate=Aggregate(
                subject="Lines · Product ~ Desk", count=1,
                records=("S00016 - Desk — Unit Price: 275.00",)))],
            text="S00016 的书桌单价？")
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("Unit Price: 275.00", execution.user_answer)

    def test_a_record_that_does_not_exist_is_said_to_be_missing(self):
        execution, *_ = self._run(
            [_declaration(kind="fact", entity="order", record="S00998", fields=["Customer"])],
            [Resolved(kind="fact", problem="no Sales Order matches 'S00998'", missing="S00998")],
            text="S00998 的客户是谁？")
        self.assertIn("没有找到名为「S00998」的记录", execution.user_answer)
        self.assertNotIn("no Sales Order matches", execution.user_answer)
        # Only the name was looked up; the reply does not say nothing of the kind exists.
        self.assertNotIn("不存在", execution.user_answer)

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

    def test_a_listed_record_with_a_line_break_reads_as_one_line(self):
        execution, *_ = self._run(
            [_declaration(kind="list", entity="order", page={"key": "p1"})],
            [Resolved(kind="list", aggregate=Aggregate(
                subject="Lines", count=1,
                records=("[D1] Four Person Desk\nFour person workstation — Unit Price: 2,350.00",)))],
            text="名称含 Desk 的行")
        self.assertIn("[D1] Four Person Desk Four person workstation — Unit Price: 2,350.00",
                      execution.user_answer)

    def test_declined_leave_to_work_the_page_ends_the_turn_without_acting(self):
        execution, *_ = self._run(
            [_declaration(kind="page", page={"key": "p1"})],
            [Resolved(kind="page", page=PagePlan(url="/orders", label="Sales / Orders",
                                                 expected_path="/orders"))],
            decision="denied", text="打开订单")
        self.assertEqual(execution.status, AgentTurnStatus.CANCELLED)
        self.assertEqual(execution.terminal_reason, "operation_not_confirmed")
        # Looking at the page is not acting on it; nothing else was dispatched.
        self.assertEqual([a for a, _ in _Browser.instances[0].actions], ["observe"])

    def test_nothing_declared_is_an_honest_refusal(self):
        execution, *_ = self._run(
            [("declare_goal", {"deliverables": [], "unsupported": "no such thing"})], [],
            text="帮我订个外卖")
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertIn("助手做不到", execution.user_answer)
        self.assertEqual(_Browser.instances[0].actions, [])

    def test_a_change_asked_of_a_read_only_assistant_names_the_request(self):
        execution, *_ = self._run(
            [("declare_goal", {"deliverables": [
                {"kind": "handoff", "entity": "order", "record": "S00006", "action": "Delete"}],
                "write_intent": True})],
            [], text="把 S00006 删掉。", offers=OPTIONAL - {"handoffs"})
        self.assertIn("「把 S00006 删掉。」没有执行", execution.user_answer)
        self.assertIn("只读", execution.user_answer)

    def test_a_question_about_the_assistant_is_answered_not_refused(self):
        for staged, script in (
                (True, [("declare_scope", {"parts": [], "about_assistant": True,
                                           "unsupported": "这是一个关于助手能力的通用问题"})]),
                (False, [("declare_goal", {"deliverables": [], "about_assistant": True})])):
            with self.subTest(staged=staged):
                _Browser.instances.clear()
                execution, _channel, chat, port, events = self._run(
                    script, [], text="你能帮我做什么？", staged=staged,
                    offers=frozenset({"handoffs"}))
                self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
                self.assertIn("我可以在这个应用里帮你", execution.user_answer)
                # What the application has, as its menus name its areas.
                self.assertIn("这个应用里你能用到的模块有：Sales（共 1 个）", execution.user_answer)
                self.assertIn("引导操作", execution.user_answer)
                # Only what the adapter declares: no value filling, no rules offered.
                self.assertNotIn("帮你填好", execution.user_answer)
                self.assertNotIn("解释规则", execution.user_answer)
                self.assertNotIn("做不到", execution.user_answer)
                self.assertNotIn("通用问题", execution.user_answer)
                self.assertEqual(port.specs, [])
                self.assertEqual([a for a, _ in _Browser.instances[0].actions
                                  if a != "observe"], [])

    def test_about_the_assistant_does_not_replace_a_declared_part(self):
        execution, *_ = self._run(
            [("declare_goal", {"deliverables": [
                {"kind": "fact", "entity": "order", "record": "S00006", "fields": ["Customer"]}],
                "about_assistant": True})],
            [Resolved(kind="fact", facts=(Fact("S00006", "Customer", "Lumber Inc"),))],
            text="S00006 的客户是谁？")
        self.assertIn("Lumber Inc", execution.user_answer)
        self.assertNotIn("我可以在这个应用里帮你", execution.user_answer)

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
        with self.assertRaisesRegex(ValueError, "four entries"):
            GoalSpec.from_json({"deliverables": [{"kind": "page"}] * 5})
        with self.assertRaisesRegex(ValueError, "exceeds"):
            GoalSpec.from_json({"deliverables": [{"kind": "fact",
                                                  "record": "x" * 121}]})
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


class HalfSupportedRequestTests(unittest.TestCase):
    """A request whose optional addition the port does not offer is still done as far as
    it can be, and a refusal that knows what the application asked for says it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "audit.jsonl"
        _Browser.instances.clear()
        self.patcher = patch.object(executor_module, "RemoteBrowserClient", _Browser)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _run(self, script, resolved, *, offers, text):
        channel = _Channel(page_url="http://127.0.0.1:8080/home")
        executor = GoalTurnExecutor(
            channel=channel,
            config=AgentTurnConfig(supported_scope="read", ledger_path=self.ledger),
            chat=_AgreeingChat(script), application=bind_application(_Adapter()),
            goal_port=_Port(resolved, offers=offers), control=ReadOnlyControl(),
            run_id_factory=lambda _m: "goal-run")
        execution = executor.execute({"message_id": "m1", "text": text,
                                      "page_url": "http://127.0.0.1:8080/home"})
        events = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        return execution, events

    def test_the_page_is_opened_when_the_port_offers_no_filters_or_groups(self):
        # The request asks for the list grouped as well; the port offers no facets. The
        # page is what the person asked to see, so it is opened and the reply says plainly
        # what this application does not offer -- the turn is not refused.
        execution, events = self._run(
            [("declare_goal", {"deliverables": [
                {"kind": "page", "entity": "order", "record": "S00006",
                 "page": {"group_by": "Status"}}]})],
            [Resolved(kind="page", page=PagePlan(url="/orders/6", label="订单 S00006",
                                                 expected_path="/orders/6"))],
            offers=frozenset(), text="打开订单 S00006，按下单状态分组给我看")
        self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
        self.assertIn("已打开：订单 S00006", execution.user_answer)
        self.assertIn("这个应用没有提供页面筛选与分组", execution.user_answer)
        asked = next(e for e in events if e["event"] == "goal_resolved")
        self.assertEqual(asked["left_out"], [["facets"]])
        self.assertNotIn("goal_capability_missing", [e["event"] for e in events])

    def test_a_read_keeps_the_condition_the_port_cannot_apply(self):
        # A count whose filter the port cannot apply is not answered about every record
        # instead: it stays a part this application cannot do.
        execution, events = self._run(
            [("declare_goal", {"deliverables": [
                {"kind": "list", "entity": "order",
                 "metric": {"op": "count", "page_filters": ["To pay"]}}]})],
            [], offers=frozenset(), text="有多少订单是已确认的？")
        self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
        self.assertIn("这个应用没有提供页面筛选与分组", execution.user_answer)
        self.assertEqual(next(e for e in events if e["event"] == "goal_capability_missing")["missing"],
                         [["facets"]])

    def test_a_refused_read_says_what_the_application_asked_for(self):
        from web_operation_agent.goal.executor import _not_done
        reason = "请求参数不正确:请选择时间范围"
        self.assertEqual(
            _not_done(Resolved(kind="list", problem="the application refused a read: x",
                               refused=reason), True),
            f"这一项没有完成：{reason}。")
        # A reason not written in the person's language is not put in front of them.
        self.assertEqual(_not_done(Resolved(kind="list", problem="x", refused="pick a period"),
                                   True), "无法完成这一项。")
        self.assertEqual(_not_done(Resolved(kind="list", problem="x", refused="pick a period"),
                                   False), "This part was not done: pick a period.")
