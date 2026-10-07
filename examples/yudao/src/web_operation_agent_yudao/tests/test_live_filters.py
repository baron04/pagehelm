"""No models, source or accounts: page conditions bound to the query form the page shows."""
from dataclasses import replace
from datetime import date
import hashlib
import time
from types import SimpleNamespace
import unittest

from web_operation_agent.business.errors import BusinessToolIdentityError, BusinessToolRequestError
from web_operation_agent.goal.data import Compare
from web_operation_agent.goal.ports import PagePlan
from web_operation_agent.goal.spec import Filter
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent_yudao.live_filters import LivePageFilters

FORM = {"form_scope": "form_instance:q", "form_instance": "q", "page_control": "query"}
CALENDAR = {"aria-haspopup": "dialog", "aria-controls": "cal-1"}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def query(ref, role, name, key="", label="", **attrs):
    return ElementRef(ref, role, name, "input", 1, {
        **FORM, **({"query_field": key} if key else {}), **({"field_label": label} if label else {}), **attrs})


class Resolver:
    """Stands in for the core condition compiler, which has its own tests."""
    fields = {"状态": ("status", "choice"), "客户": ("customerId", "link"), "下单时间": ("orderTime", "date"),
              "单号": ("no", "text"), "仓库": ("warehouseId", "link")}

    def view(self, entity):
        infos = {key: SimpleNamespace(kind=kind, relation="customers", choices=(
            SimpleNamespace(key=10, label="未审核"), SimpleNamespace(key=20, label="已审核")))
            for key, kind in self.fields.values()}
        return SimpleNamespace(info=infos.get) if entity == "orders" else None

    def _condition(self, view, label, values, *, op="", today=None):
        if label not in self.fields or not values:
            return None, "no such field"
        key, kind = self.fields[label]
        if kind == "date":
            if values != ("2026-10",):
                return [Compare(key, "lt", date(2026, 10, 1))], ""
            return [Compare(key, "ge", date(2026, 10, 1)), Compare(key, "lt", date(2026, 11, 1))], ""
        if kind == "choice":
            return [Compare(key, "in", ({"未审核": 10, "已审核": 20, "作废": 30}[values[0]],))], ""
        if kind == "link":
            return [Compare(key, "in", (980101,))], ""
        return [Compare(key, "contains" if op == "contains" else "eq", values[0])], ""

    def _find_record(self, relation, name):
        return (relation, "980101", "远舟工具") if name == "远舟" else None

    def _own_name(self, display):
        return display


class LiveFilterTests(unittest.TestCase):
    def setUp(self):
        self.client = SimpleNamespace(tenant="1", actor=2)
        self.client.get = lambda path: {"user": {"id": self.client.actor}}
        spec = SimpleNamespace(list_path="orders/page", params={
            "status": {}, "customerId": {}, "orderTime": {}, "no": {}})
        index = SimpleNamespace(page=lambda path: SimpleNamespace(entity="orders") if path == "/orders" else None)
        self.port = SimpleNamespace(data=SimpleNamespace(_client=self.client, _entities={"orders": spec}),
                                    index=lambda **k: index, _resolver=lambda index: Resolver())
        self.filters = LivePageFilters(self.port)
        now = int(time.time() * 1000)
        principal = PrincipalContext(1, "yudao", "1", "10", "2", "delegation", now - 100, now + 60000,
                                     ("yudao.read",))
        self.context = OperationContext.create(principal, session_id="session", turn_id="turn",
                                               operation_id="filters", deadline_ms=now + 30000)
        self.page = PageObservation(1, 1, "https://synthetic.test/orders", "Orders", [], [
            query("n", "textbox", "请输入单号", "no", "订单单号"),
            query("s1", "combobox", "开始日期", "orderTime", "订单时间", **CALENDAR),
            query("s2", "combobox", "结束日期", "orderTime", **CALENDAR),
            query("c", "combobox", "客户", "customerId", "客户"),
            # The same dropdown's inner input: no form facts, so it binds nothing.
            ElementRef("c2", "combobox", "请选择客户", "input", 1, {"page_control": "query",
                                                                  "query_field": "customerId"}),
            query("t", "combobox", "状态", "status", "状态"),
            query("go", "button", "搜索"), query("reset", "button", "重置")],
            render_state="ready", page_route="/orders")

    def prepare(self, *conditions, page=None):
        plan = PagePlan("/orders", "Orders", expected_path="/orders", conditions=tuple(conditions))
        return self.filters.prepare(plan, page or self.page, context=self.context)

    def test_conditions_bind_by_the_page_own_parameter_name_and_are_attested_by_digest(self):
        draft = self.prepare(Filter("下单时间", ("2026-10",)), Filter("客户", ("远舟",)),
                             Filter("状态", ("未审核",)), Filter("单号", ("XS2026",)))
        self.assertEqual((draft.route, draft.scope, draft.instance, draft.button, draft.query_path),
                         ("/orders", "form_instance:q", "q", "搜索", "/admin-api/orders/page"))
        # The page's own labels, its option labels, and ranges last.
        self.assertEqual([(f.label, f.value, f.control, f.option_label, f.name, f.commit, f.ordinal)
                          for f in draft.fields], [
            ("客户", "980101", "combobox", "远舟工具", "", True, 0),
            ("状态", "10", "combobox", "未审核", "", True, 0),
            ("订单单号", "XS2026", "textbox", "", "", True, 0),
            ("订单时间", "2026-10-01", "combobox", "", "开始日期", False, 0),
            ("订单时间", "2026-10-31", "combobox", "", "结束日期", False, 0)])
        self.assertEqual(draft.parameters, ())
        self.assertEqual(dict(draft.read_fields), {
            "customerId": (sha('["980101"]'),), "status": (sha('["10"]'),), "no": (sha('["XS2026"]'),),
            "orderTime[0]": (sha('["2026-10-01 00:00:00"]'), sha('["2026-10-01"]')),
            "orderTime[1]": (sha('["2026-10-31 23:59:59"]'), sha('["2026-10-31"]'))})

    def test_range_ends_that_share_a_placeholder_are_told_apart_by_order(self):
        same = replace(self.page, elements=[replace(e, name="自定义时间") if e.ref in ("s1", "s2") else e
                                            for e in self.page.elements])
        draft = self.prepare(Filter("下单时间", ("2026-10",)), page=same)
        self.assertEqual([(f.name, f.ordinal) for f in draft.fields], [("自定义时间", 1), ("自定义时间", 2)])

    def test_what_the_page_does_not_show_exactly_once_is_refused(self):
        def without(*refs):
            return replace(self.page, elements=[e for e in self.page.elements if e.ref not in refs])
        status = Filter("状态", ("未审核",))
        cases = [((Filter("部门", ("x",)),), None),                      # not a field of the entity
                 ((Filter("仓库", ("x",)),), None),                      # not a parameter of the list
                 ((Filter("状态", ("作废",)),), None),                   # not one of the field's choices
                 ((Filter("客户", ("无此客户",)),), None),               # names no single record
                 ((Filter("下单时间", ("overdue",)),), None),            # not a bounded range
                 ((status,), without("t")), ((status,), without("go")),
                 ((Filter("下单时间", ("2026-10",)),), without("s2")),
                 ((status, status), None), ((), None),
                 ((status,), replace(self.page, render_state="pending")),
                 ((status,), replace(self.page, page_route="/other")),
                 ((status,), replace(self.page, url="https://synthetic.test/orders?x=1")),
                 ((status,), replace(self.page, elements=[*self.page.elements, ElementRef(
                     "d", "textbox", "", "input", 1, {"form_scope": "dialog:editor"})])),
                 ((status,), replace(self.page, elements=[*self.page.elements,
                                                          query("t2", "combobox", "状态", "status", "状态")])),
                 ((status,), replace(self.page, elements=[
                     replace(e, attrs={**e.attrs, "disabled": "true"}) if e.ref == "t" else e
                     for e in self.page.elements]))]
        for conditions, page in cases:
            with self.subTest(conditions=conditions, changed=page is not None), \
                    self.assertRaises(BusinessToolRequestError):
                self.prepare(*conditions, page=page)
        self.client.actor = 3
        with self.assertRaises(BusinessToolIdentityError):
            self.prepare(status)

    def test_an_open_calendar_keeps_only_the_draft_already_issued_for_this_form(self):
        conditions = (Filter("状态", ("未审核",)), Filter("下单时间", ("2026-10",)))
        issued = self.prepare(*conditions)
        # The calendar of this form covers the status control and the query button.
        covered = replace(self.page, elements=[
            *(e for e in self.page.elements if e.ref not in ("t", "go", "reset")),
            ElementRef("cal", "button", "1", "td", 1, {"form_scope": "dialog:cal-1"})])
        self.assertIs(self.prepare(*conditions, page=covered), issued)
        with self.assertRaises(BusinessToolRequestError):
            self.prepare(Filter("状态", ("已审核",)), page=covered)
        gone = replace(covered, elements=[e for e in covered.elements
                                          if e.attrs.get("form_instance") != "q"])
        with self.assertRaises(BusinessToolRequestError):
            self.prepare(*conditions, page=gone)
        self.assertEqual(self.prepare(*conditions), issued)


if __name__ == "__main__":
    unittest.main()
