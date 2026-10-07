"""A plain list with pagination qualifies paging without any application or model."""
from dataclasses import replace
from types import SimpleNamespace
import contextlib
import unittest

from web_operation_agent.goal.executor import _ask_what_it_can, _coverage_gaps, _dropped_quantities
from web_operation_agent.goal.paging import apply_paging
from web_operation_agent.goal.ports import PagePlan
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.tests.business.test_scoped_forms import session_for


class PlainList:
    """Three pages of contacts; the page reports each list read it completes."""
    def __init__(self):
        self.calls, self.page, self.size, self.revision, self.broken = [], 1, 20, 1, ""
    def observation(self, epoch):
        controls = [ElementRef(f"p{n}", "li", str(n), "li", epoch,
                               {"page_control": "pagination", "page_number": str(n), "page_size": str(self.size)})
                    for n in (1, 2, 3) if n != self.page]
        controls.append(ElementRef("size", "div", "", "div", epoch,
                                   {"page_control": "page_size", "page_size": str(self.size)}))
        controls += [ElementRef(f"o{n}", "option", f"{n} / page", "li", epoch) for n in (20, 50)]
        read = {"path": "/contacts/query", "query_digest": "d" * 64,
                "query_fields": {"city": "c" * 64} if self.broken == "narrowing" else {},
                "revision": self.revision, "page": self.page, "page_size": self.size, "total": 61,
                "rows": min(self.size, 61 - (self.page - 1) * self.size)}
        return PageObservation(1, epoch, "https://contacts.test/contacts", "Contacts", [], controls,
                               render_state="ready", page_route="/contacts", page_read=read)
    def execute(self, action, args, **kwargs):
        self.calls.append((action, args["ref"]))
        if self.broken != "ignored":
            if args["ref"].startswith("p"):
                self.page = int(args["ref"][1:])
            elif args["ref"].startswith("o"):
                self.size, self.page = int(args["ref"][1:]), 1
            if args["ref"] != "size":
                self.revision += 1
        return ActionReceipt(1, str(len(self.calls)), action, args, True, write_guard_held=True,
                             observation=self.observation(len(self.calls) + 1))


class PagingContractTests(unittest.TestCase):
    def flow(self, **paging):
        app = PlainList()
        session = session_for(app.execute)
        state = SimpleNamespace(obs=app.observation(1), step=0, page_actions=0, approvals=session._approvals,
                                writes=session._writes, browser=session.browser, ledger=session.ledger,
                                budget=SimpleNamespace(check=lambda: 30),
                                receipt=SimpleNamespace(stage=lambda _: contextlib.nullcontext()),
                                terminal_reason="", detail="")
        state.writes.expect_decision = lambda *a: None
        executor = SimpleNamespace(_wait_ready=lambda *a, **k: True, _dispatch=lambda *a, **k: None)
        return app, executor, state, PagePlan("/contacts", "Contacts", expected_path="/contacts", **paging)

    def test_a_page_and_a_size_are_settled_only_by_a_later_read_that_reports_them(self):
        for paging, expected, pressed in (({"page_step": 1}, "2/20", ["p2"]), ({"page_number": 3}, "3/20", ["p3"]),
                                          ({"page_size": 50}, "1/50", ["size", "o50"]),
                                          ({"page_size": 50, "page_number": 2}, "2/50", ["size", "o50", "p2"]),
                                          ({"page_number": 1}, "1/20", []), ({"page_size": 20}, "1/20", [])):
            app, executor, state, plan = self.flow(**paging)
            self.assertEqual(apply_paging(executor, state, plan), expected, paging)
            self.assertEqual([ref for _, ref in app.calls], pressed)

    def test_no_control_no_read_or_a_changed_list_is_a_paging_not_done(self):
        for paging, broken in (({"page_number": 9}, ""), ({"page_step": -1}, ""), ({"page_size": 30}, ""),
                               ({"page_step": 1}, "ignored")):
            app, executor, state, plan = self.flow(**paging)
            app.broken = broken
            self.assertEqual(apply_paging(executor, state, plan), "", (paging, broken))
        app, executor, state, plan = self.flow(page_step=1)
        state.obs = replace(state.obs, page_read=None)
        self.assertEqual(apply_paging(executor, state, plan), "")
        self.assertEqual(app.calls, [])
        app, executor, state, plan = self.flow(page_step=1)
        app.execute = (lambda inner: lambda *a, **k: (setattr(app, "broken", "narrowing"), inner(*a, **k))[1])(app.execute)
        state.browser = session_for(app.execute).browser
        self.assertEqual(apply_paging(executor, state, plan), "")

    def test_paging_is_declared_round_trips_and_is_left_out_where_not_offered(self):
        spec = GoalSpec.from_json({"deliverables": [{"kind": "page", "entity": "contacts", "page": {
            "key": "/contacts", "page_step": 1, "page_size": "50"}}]})
        self.assertEqual((spec.deliverables[0].page.page_step, spec.deliverables[0].page.page_size), (1, 50))
        self.assertEqual(GoalSpec.from_json(spec.to_json()), spec)
        asked, left = _ask_what_it_can(spec, frozenset())
        self.assertFalse(asked.deliverables[0].page.paged)
        self.assertEqual(left, (("paging",),))
        self.assertEqual(_ask_what_it_can(spec, frozenset({"paging"}))[1], ((),))
        for bad in ({"page_number": 2, "page_step": 1}, {"page_step": 2}, {"page_size": -1}, {"page_number": "x"}):
            with self.assertRaises(ValueError):
                GoalSpec.from_json({"deliverables": [{"kind": "page", "page": {"key": "/contacts", **bad}}]})

    def test_a_quantity_the_declaration_carries_nowhere_is_named(self):
        def spec(**extra):
            return GoalSpec.from_json({"deliverables": [{"kind": "page", "entity": "contacts",
                                                         "page": {"key": "/contacts", **extra}}]})
        self.assertEqual(_dropped_quantities(spec(), "客户太多了，每页显示50条"), ("50条",))
        self.assertEqual(_dropped_quantities(spec(page_size=50), "客户太多了，每页显示50条"), ())
        self.assertTrue(any("50条" in gap for gap in _coverage_gaps(spec(), [], "每页显示50条", "2026-10-03")))
        order = GoalSpec.from_json({"deliverables": [{"kind": "handoff", "entity": "orders", "action": "create",
            "field_values": [{"field": "Supplier", "value": "海川"}, {"field": "Date", "value": "2026-10-02"},
                             {"field": "Start", "value": "2026-10-06 09:00:00"}, {"field": "Price", "value": "2,000.00"}]}]})
        self.assertEqual(_dropped_quantities(order, "供应商海川，10月2日下单，安全帽100顶"), ("100顶",))
        # Days, clock times, amounts written another way, codes and names are not quantities left out.
        for text in ("10月2日下单", "6日9点开始", "金额2000元", "订单SC20261003005的状态", "传感器T20的详情",
                     "手机13800001001", "下一页"):
            self.assertEqual(_dropped_quantities(order, text), (), text)


if __name__ == "__main__":
    unittest.main()
