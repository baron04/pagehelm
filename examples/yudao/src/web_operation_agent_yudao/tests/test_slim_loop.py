"""The slim loop's own parts over synthetic pages and a synthetic API; no model, no browser."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.index import ApplicationIndex, IndexConvention, IndexEntity, IndexPage
from web_operation_agent.privacy import SessionHandoffPolicy
from web_operation_agent.schemas.browser import ElementRef, PageObservation
from web_operation_agent_yudao.native_server import YudaoHostConfig
from web_operation_agent_yudao.slim_loop import (ActorKnowledge, SlimTurnExecutor, _commits, _fields, _lines,
                                                 _page_lines, _scroll_anchor, _today, conventions_text, page_text)


def element(number: int, role: str, name: str = "", tag: str = "div", **attrs) -> ElementRef:
    return ElementRef(f"e{number}", role, name, tag, 1, attrs)


CONTENT = "\n".join([
    "Current Page: [Orders](https://synthetic.test/orders)",
    "Page info: 1440x900px viewport",
    "[Start of page]",
    "[0]<a href=/>Synthetic />",
    '[1]<div data-scrollable="bottom=320" >Sales',
    "Orders",
    "Returns />",
    "\t[2]<li role=menuitem>Home />",
    "\t[3]<li role=menuitem>Sales />",
    "\t\t[4]<li role=menuitem>Orders />",
    "\t\t[5]<li role=menuitem>Returns />",
    "\t[6]<li role=menuitem>Stock />",
    '[7]<div data-scrollable="bottom=394" >Orders',
    "\t[8]<div  />",
    "\t[9]<label for=a>Customer />",
    "\t[10]<button type=button>Search />",
    "Order 1",
    "\t[11]<button type=button>Details />",
    "[End of page]",
])


def observation(**kw) -> PageObservation:
    elements = [element(0, "link", "Synthetic", "a", href="/"), element(1, "div", "Sales\nOrders"),
                *[element(n, "menuitem", "m", "li") for n in range(2, 7)],
                element(7, "div", "Orders"), element(8, "div", ""),
                element(9, "combobox", "Customer", "input", field_label="Customer", value="Acme"),
                element(10, "button", "Search", "button"), element(11, "button", "Details", "button")]
    return PageObservation(1, 1, "https://synthetic.test/orders", "Orders", [], elements,
                           content=CONTENT, **kw)


class PageTextTests(unittest.TestCase):
    def test_the_navigation_menu_and_the_runtime_preamble_are_left_out(self):
        lines, _hints = _page_lines(observation())
        text = "\n".join(lines)
        self.assertNotIn("Current Page", text)
        self.assertNotIn("menuitem", text)
        self.assertNotIn("[1]<div", text)
        self.assertIn("[0]<a href=/>Synthetic", text)
        self.assertIn("[10]<button type=button>Search", text)

    def test_a_bare_wrapper_goes_and_what_it_holds_stays(self):
        lines, _hints = _page_lines(observation())
        self.assertFalse(any(line.strip().startswith("[8]") for line in lines))
        self.assertTrue(any(line.strip().startswith("[11]") for line in lines))

    def test_a_region_with_more_below_is_named_with_the_way_to_see_it(self):
        _lines, hints = _page_lines(observation())
        self.assertEqual(len(hints), 1)
        self.assertIn("[7]", hints[0])
        self.assertIn("scroll(down=true, index=7)", hints[0])

    def test_the_page_states_its_list_its_applied_query_and_its_fields(self):
        read = {"path": "/admin-api/orders/page", "query_digest": "0" * 64, "query_fields": {},
                "revision": 1, "page": 2, "page_size": 10, "total": 15, "rows": 5}
        query = {"path": "/admin-api/orders/page", "parameters": {"pageNo": "2", "customerId": "7"}}
        text = page_text(observation(page_read=read, page_query=query),
                         handoffs=SessionHandoffPolicy.extending(()), marked="OK")
        self.assertIn("第 2 页，每页 10 条，本页 5 行，共 15 条", text)
        self.assertIn('"customerId": "7"', text)
        self.assertIn("Customer=Acme", text)
        self.assertIn("已标出的控件: OK", text)

    def test_the_date_told_is_the_application_s_reference_day_or_today_there(self):
        self.assertEqual(_today("2026-10-03", "Asia/Shanghai"), "2026-10-03")
        self.assertRegex(_today("", "Asia/Shanghai"), r"^\d{4}-\d{2}-\d{2}$")

    def test_a_dropdown_and_a_date_box_are_marked_as_what_they_are(self):
        lines, _hints = _page_lines(observation())
        self.assertTrue(next(line for line in lines if "[9]" in line).endswith("〔下拉〕"))
        dated = PageObservation(1, 1, "https://synthetic.test/orders", "Orders", [], [
            element(0, "combobox", "开始日期", "input", **{"aria-haspopup": "dialog"}),
            element(1, "textbox", "备注", "input")], content="[0]<input placeholder=开始日期 />\n[1]<input />")
        lines, _hints = _page_lines(dated)
        self.assertTrue(lines[0].endswith("〔日期〕"))
        self.assertEqual(lines[1], "[1]<input />")

    def test_the_first_box_of_a_date_range_waits_for_its_second(self):
        def box(number, value=""):
            attrs = {"aria-haspopup": "dialog", "aria-controls": "panel-1"}
            if value:
                attrs["value"] = value
            return element(number, "combobox", "", "input", **attrs)
        start, end, alone = box(1), box(2), element(3, "combobox", "", "input", **{
            "aria-haspopup": "dialog", "aria-controls": "panel-2"})
        text = element(4, "textbox", "", "input")
        self.assertFalse(_commits(start, [start, end, alone, text]))
        self.assertTrue(_commits(end, [start, end, alone, text]))
        self.assertTrue(_commits(alone, [start, end, alone, text]))
        self.assertTrue(_commits(text, [start, end, alone, text]))
        # Once the second box holds a value, the first is committed like any other.
        self.assertTrue(_commits(start, [start, box(2, "2026-10-31")]))

    def test_the_deployment_s_conventions_are_told_as_reviewed(self):
        told = conventions_text((IndexConvention(("客户",), "crm/customer (CRM 客户)", "crm/customer"),
                                 IndexConvention(("进销存客户", "ERP 客户"), "erp/customer")))
        self.assertIn("- 客户：crm/customer (CRM 客户)", told)
        self.assertIn("- 进销存客户 / ERP 客户：erp/customer", told)
        self.assertEqual(conventions_text(()), "")

    def test_the_menu_under_an_open_dialog_is_left_out_too(self):
        under = PageObservation(1, 1, "https://synthetic.test/orders", "Orders", [], [
            element(0, "menubar", "Home\nSales", "ul"), element(1, "button", "OK", "button")],
            content="[0]<ul role=menubar>Home\nSales\nStock />\n[1]<button>OK />")
        lines, _hints = _page_lines(under)
        self.assertEqual(lines, ["[1]<button>OK />"])

    def test_a_line_table_s_controls_are_read_by_row_and_column(self):
        lined = PageObservation(1, 1, "https://synthetic.test/orders", "Orders", [], [
            element(3, "combobox", "明细第1行·产品名称", "input", title="明细第1行·产品名称", selected_option="包装胶带"),
            element(4, "spinbutton", "明细第1行·数量", "input", title="明细第1行·数量", value="10"),
            element(5, "spinbutton", "明细第2行·数量", "input", title="明细第2行·数量"),
            element(6, "textbox", "备注", "input", title="备注", value="x")],
            content="[3]<input />\n[4]<input />\n[5]<input />\n[6]<input />")
        self.assertEqual(_lines(lined), {1: {"产品名称": "包装胶带", "数量": "10"}, 2: {"数量": ""}})
        text = page_text(lined, handoffs=SessionHandoffPolicy.extending(()))
        self.assertIn("明细行当前值: 第1行 产品名称=包装胶带, 数量=10； 第2行 数量=(空)", text)

    def test_the_two_boxes_of_a_date_range_are_told_apart(self):
        ranged = PageObservation(1, 1, "https://synthetic.test/orders", "Orders", [], [
            element(0, "combobox", "", "input", **{"aria-haspopup": "dialog", "aria-controls": "p"}),
            element(1, "combobox", "", "input", **{"aria-haspopup": "dialog", "aria-controls": "p"})],
            content="[0]<input />\n[1]<input />")
        lines, _hints = _page_lines(ranged)
        self.assertEqual(lines, ["[0]<input /> 〔日期·开始〕", "[1]<input /> 〔日期·结束〕"])

    def test_a_region_is_scrolled_from_an_element_inside_it(self):
        self.assertEqual(_scroll_anchor(observation(), "7"), "8")    # the runtime moves what is around its anchor
        self.assertEqual(_scroll_anchor(observation(), "10"), "10")  # an ordinary element anchors itself

    def test_fields_are_read_by_their_labels(self):
        self.assertEqual(_fields(observation()), {"Customer": "Acme"})

    def test_only_an_element_the_page_text_listed_may_be_acted_on(self):
        class State:
            obs = observation()
        self.assertIsNone(SlimTurnExecutor._element(None, State, 3))    # a menu entry
        self.assertIsNone(SlimTurnExecutor._element(None, State, 99))   # nothing there
        self.assertEqual(SlimTurnExecutor._element(None, State, 10).name, "Search")


DOCS = {"paths": {
    "/admin-api/orders/page": {"get": {"summary": "获得订单分页", "tags": ["管理后台 - 订单"], "parameters": [
        {"name": "customerId", "in": "query", "description": "客户编号"},
        {"name": "pageNo", "in": "query", "required": True, "description": "页码"}],
        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/Result"}}}}}}},
    "/admin-api/wallet/get": {"get": {"summary": "获得钱包", "tags": ["管理后台 - 钱包"]}},
    "/admin-api/deals/page": {"get": {"summary": "获得商机分页", "tags": ["管理后台 - 商机"]}},
    "/admin-api/leads/page": {"get": {"summary": "获得线索分页", "tags": ["管理后台 - 线索"], "parameters": [
        {"name": "sceneType", "in": "query", "description": "场景类型"},
        {"name": "pageNo", "in": "query", "required": True, "description": "页码"}]}},
    "/admin-api/orders/export-excel": {"get": {"summary": "导出订单", "tags": ["管理后台 - 订单"]}},
    "/admin-api/orders/create": {"post": {"summary": "创建订单"}},
}, "components": {"schemas": {
    "Result": {"properties": {"code": {}, "data": {"$ref": "#/components/schemas/Page"}}},
    "Page": {"properties": {"total": {}, "list": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}}}},
    "Order": {"properties": {"no": {"description": "订单号"}, "price": {"description": "金额，单位：分"}}},
}}}


class Client:
    def __init__(self):
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "system/auth/get-permission-info":
            return {"user": {"nickname": "合成用户"}, "menus": [
                {"name": "销售", "path": "/sale", "children": [
                    {"name": "订单", "path": "order", "component": "sale/order/index"},
                    {"name": "隐藏页", "path": "hidden", "component": "x", "visible": False}]}]}
        if path == "orders/page":
            if params.get("customerId") == "0":
                raise DataRefused("orders/page: 400 客户不存在")
            return {"total": 2, "list": [{"no": "S1", "price": 1000, "remark": None}, {"no": "S2", "price": 250}]}
        if path == "leads/page":
            mine = {1: [{"id": 1, "name": "甲"}, {"id": 2, "name": "乙"}], 2: [{"id": 2, "name": "乙"}, {"id": 3, "name": "丙"}],
                    3: []}.get(params.get("sceneType"), [{"id": n, "name": "别人的"} for n in range(1, 9)])
            if params.get("name"):
                mine = [row for row in mine if params["name"] in row["name"]]
            return {"total": len(mine), "list": mine if params["pageNo"] == 1 else []}
        if path == "deals/page":
            size, page = params.get("pageSize", 2), params["pageNo"]
            return {"total": len(DEALS), "list": DEALS[(page - 1) * size:page * size]}
        raise AssertionError(path)


#: 2026-10-02 08:00 and 2026-09-30 23:30 in Asia/Shanghai, as epoch milliseconds.
OCTOBER, SEPTEMBER = 1790899200000, 1790782200000
DEALS = [{"id": n, "name": f"商机{n}", "ownerUserId": 7, "totalPrice": price, "createTime": made,
          "remark": "备注" * 30}
         for n, (price, made) in enumerate([(45000.0, OCTOBER), (18000.5, SEPTEMBER), (45000.0, OCTOBER),
                                            (32000.0, SEPTEMBER), (100.0, OCTOBER)], start=1)]


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.know = ActorKnowledge(self.client, lambda: DOCS, {"/admin-api/wallet/get"})

    def test_the_menu_lists_the_actor_s_visible_pages_with_their_paths(self):
        found = self.know.search_menu("订单")
        self.assertIn("/sale/order  销售 > 订单", found)
        self.assertNotIn("隐藏页", found)
        self.assertEqual(self.know.user_name, "合成用户")

    def test_only_reads_are_searchable_and_exports_are_not(self):
        found = self.know.search_api("订单")
        self.assertIn("GET /admin-api/orders/page", found)
        self.assertIn("customerId(客户编号)", found)
        self.assertNotIn("export", found)
        self.assertNotIn("create", found)

    def test_a_read_returns_its_total_its_rows_and_what_the_fields_mean(self):
        text = self.know.call_api("/admin-api/orders/page", {"pageNo": 1})
        self.assertIn("total=2", text)
        self.assertIn('"no": "S1"', text)
        self.assertNotIn("remark", text)
        self.assertIn("price=金额，单位：分", text)
        self.assertEqual(self.client.calls[-1], ("orders/page", {"pageNo": 1}))

    def test_chosen_columns_are_read_to_the_last_page_with_their_sums_and_extremes(self):
        text = self.know.call_api("deals/page", {"pageNo": 1, "pageSize": 2}, ["name", "totalPrice"])
        self.assertEqual([c[1]["pageNo"] for c in self.client.calls if c[0] == "deals/page"], [1, 2, 3])
        self.assertIn("total=5（符合条件的总数），本次读了 5 行（已读完所有页）", text)
        self.assertIn("totalPrice（5 行）: 合计 140100.5，最大 45000，最小 100；最大的有 2 行: "
                      '{"id": 1, "name": "商机1", "totalPrice": 45000.0}；{"id": 3, "name": "商机3", "totalPrice": 45000.0}',
                      text)
        self.assertIn('{"id": 5, "name": "商机5", "totalPrice": 100.0}', text)
        self.assertNotIn("remark", text)
        self.assertNotIn("ownerUserId（", text)   # an identifier is not a quantity

    def test_a_list_divided_by_whose_records_they_are_is_read_as_everything_the_actor_sees(self):
        text = self.know.call_api("leads/page", {"pageNo": 1, "pageSize": 10})
        self.assertEqual([c[1]["sceneType"] for c in self.client.calls if c[0] == "leads/page"], [1, 2, 3])
        self.assertIn("total=3（当前账号可见的全部", text)
        self.assertNotIn("别人的", text)
        self.assertEqual(text.count('"name": "乙"'), 1)

    def test_a_record_that_exists_outside_the_actor_s_scopes_is_not_reported_as_absent(self):
        text = self.know.call_api("leads/page", {"pageNo": 1, "pageSize": 10, "name": "别人"})
        self.assertIn("total=0（当前账号可见的全部", text)
        self.assertIn("不限范围时同样条件有 8 条记录", text)
        self.assertIn("这是没有权限，不是记录不存在", text)
        absent = self.know.call_api("leads/page", {"pageNo": 1, "pageSize": 10, "name": "没有这个"})
        self.assertNotIn("不限范围", absent)

    def test_a_scope_the_person_named_is_the_only_one_read(self):
        text = self.know.call_api("leads/page", {"pageNo": 1, "pageSize": 10, "sceneType": 1})
        self.assertEqual([c[1]["sceneType"] for c in self.client.calls if c[0] == "leads/page"], [1])
        self.assertIn("total=2（符合条件的总数）", text)

    def test_a_number_that_classifies_a_row_is_not_summed(self):
        DEALS[0]["status"] = DEALS[1]["status"] = 10
        try:
            text = self.know.call_api("deals/page", {"pageNo": 1, "pageSize": 2}, ["status", "totalPrice"])
        finally:
            del DEALS[0]["status"], DEALS[1]["status"]
        self.assertNotIn("status（", text)
        self.assertIn("totalPrice（", text)

    def test_without_chosen_columns_only_the_page_asked_for_is_read(self):
        text = self.know.call_api("deals/page", {"pageNo": 1, "pageSize": 2})
        self.assertEqual([c[1]["pageNo"] for c in self.client.calls if c[0] == "deals/page"], [1])
        self.assertIn("total=5（符合条件的总数），本次读了 2 行", text)
        self.assertNotIn("已读完所有页", text)

    def test_an_instant_is_shown_as_the_application_s_clock_has_it(self):
        text = self.know.call_api("deals/page", {"pageNo": 1, "pageSize": 5}, ["createTime"])
        self.assertIn('{"id": 1, "createTime": "2026-10-02 08:00:00"}', text)
        self.assertIn('{"id": 2, "createTime": "2026-09-30 23:30:00"}', text)
        self.assertIn("createTime 按月的行数: 2026-10 3 行，2026-09 2 行", text)
        self.assertNotIn("createTime（", text)    # an instant is counted by month, not summed

    def test_a_result_adds_a_bounded_amount_to_the_conversation_and_says_what_it_left_out(self):
        text = self.know.call_api("deals/page", {"pageNo": 1, "pageSize": 100})
        self.assertIn("本次读了 5 行", text)
        DEALS.extend({"id": 100 + n, "name": "行" * 60, "totalPrice": 1.0} for n in range(200))
        try:
            long = self.know.call_api("deals/page", {"pageNo": 1, "pageSize": 100}, ["name"])
        finally:
            del DEALS[5:]
        self.assertLess(len(long), 10_600)
        self.assertRegex(long, r"只列出了前 \d+ 行，其余 \d+ 行未列出")

    def test_a_get_that_writes_is_never_called(self):
        text = self.know.call_api("/admin-api/wallet/get", {})
        self.assertIn("不允许调用", text)
        self.assertNotIn(("wallet/get", {}), self.client.calls)

    def test_a_path_the_application_does_not_publish_is_not_called(self):
        text = self.know.call_api("/admin-api/orders/create", {})
        self.assertIn("没有这个只读接口", text)
        self.assertEqual([c for c in self.client.calls if c[0] != "system/auth/get-permission-info"], [])

    def test_the_application_s_refusal_is_told_to_the_model(self):
        self.assertIn("客户不存在", self.know.call_api("orders/page", {"customerId": "0"}))


def call(name: str, **args) -> dict:
    return {"id": f"call-{name}-{len(args)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def reply(*calls, content: str = "") -> dict:
    return {"content": content, "tool_calls": list(calls)}


class Script:
    """A chat that answers from a script and keeps what it was sent."""

    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def complete_cancellable(self, messages, tools, run_id, *, cancelled, max_tokens=None,
                             options=None, timeout_s=None):
        self.seen.append(messages)
        return self.replies.pop(0)


class Ledger:
    def __init__(self):
        self.events = []

    def append(self, event, **fields):
        self.events.append((event, fields))


class Budget:
    @staticmethod
    def remaining():
        return 60.0


class State:
    def __init__(self, *replies):
        self.terminal_reason, self.detail, self.clarification = "", "", None
        self.step, self.run_id, self.obs = 0, "run-1", observation()
        self.chat, self.ledger, self.budget = Script(*replies), Ledger(), Budget()

    @staticmethod
    def cancelled():
        return False


class LoopTests(unittest.TestCase):
    def loop(self, *replies, steps: int = 6, tool=None, problems=()):
        executor = SlimTurnExecutor.__new__(SlimTurnExecutor)
        executor.model_steps, executor.verify = steps, True
        executor._messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
        self.ran, self.checked = [], []
        executor._tool = tool or (lambda s, name, args, turn: (self.ran.append(name) or "ok", False))
        pending = list(problems)

        def verify(s, prompt, answer, turn):
            self.checked.append(answer)
            return pending.pop(0) if pending else []
        executor._verify = verify
        state = State(*replies)
        self.turn = turn = {"reads": [], "marked": "", "trace": [], "touched": [],
                            "handoffs": SessionHandoffPolicy.extending(()), "shown": [], "done": set(), "stalled": 0}
        return executor, state, executor._loop(state, "q", turn, verified=False)

    def test_done_ends_the_turn_finished_with_the_model_s_answer(self):
        _executor, state, text = self.loop(reply(call("search_menu", keyword="订单")),
                                           reply(call("finish", status="done", answer="共 3 条")))
        self.assertEqual((text, state.terminal_reason), ("共 3 条", ""))
        self.assertEqual((self.ran, self.checked), (["search_menu"], ["共 3 条"]))

    def test_need_input_ends_as_a_question_the_conversation_can_hold(self):
        _executor, state, text = self.loop(reply(call("finish", status="need_input", answer="哪一条？" * 200)))
        self.assertEqual(state.terminal_reason, "needs_clarification")
        self.assertEqual(set(state.clarification), {"schema_version", "clarification_id", "task_id",
                                                    "question", "response_kind", "choices"})
        self.assertEqual((state.clarification["response_kind"], state.clarification["choices"]), ("free_text", []))
        self.assertLessEqual(len(state.clarification["question"]), 500)
        self.assertEqual(self.checked, [])  # only a claim of completion is checked
        self.assertTrue(text.startswith("哪一条？"))

    def test_cannot_ends_the_turn_refused(self):
        _executor, state, text = self.loop(reply(call("finish", status="cannot", answer="没有这条记录")))
        self.assertEqual((text, state.terminal_reason), ("没有这条记录", "agent_refused"))

    def test_a_failed_check_sends_the_turn_back_once_and_only_once(self):
        executor, state, text = self.loop(
            reply(call("finish", status="done", answer="已完成")),
            reply(call("finish", status="done", answer="已修正")),
            problems=[["没有标出提交按钮"]])
        self.assertEqual((text, state.terminal_reason), ("已修正", ""))
        self.assertEqual(self.checked, ["已完成"])
        sent_back = [m for m in executor._messages if m["role"] == "user" and m["content"].startswith("验收提出了疑点")]
        self.assertEqual(len(sent_back), 1)
        self.assertIn("没有标出提交按钮", sent_back[0]["content"])
        self.assertIn("意见不对，就保持原来的结果", sent_back[0]["content"])   # a second reading, not a ruling

    def test_the_turn_is_refused_when_its_model_steps_run_out(self):
        _executor, state, text = self.loop(*[reply(call("search_menu", keyword="x")) for _ in range(3)], steps=3)
        self.assertEqual(state.terminal_reason, "agent_refused")
        self.assertIn("步骤用完", text)

    def test_before_its_last_steps_the_turn_is_told_to_report_what_it_has(self):
        executor, _state, _text = self.loop(
            *[reply(call("search_menu", keyword="x")) for _ in range(3)],
            reply(call("finish", status="cannot", answer="查到 3 张，页面没筛出来")), steps=5)
        told = [n for n, m in enumerate(executor._messages) if m["role"] == "user" and m["content"].startswith("只剩最后两步")]
        self.assertEqual(len(told), 1)
        self.assertEqual(len([m for m in executor._messages[:told[0]] if m["role"] == "assistant"]), 3)

    def test_a_model_that_stops_calling_tools_is_reminded_then_refused(self):
        _executor, state, text = self.loop(*[reply(content="我想想") for _ in range(3)])
        self.assertEqual((text, state.terminal_reason), ("我想想", "agent_refused"))

    def test_nothing_named_after_finish_in_the_same_reply_is_run(self):
        self.loop(reply(call("finish", status="cannot", answer="做不到"), call("click", index=10)))
        self.assertEqual(self.ran, [])

    def test_a_step_repeated_from_the_same_page_is_not_run_again(self):
        executor, state, _text = self.loop(
            reply(call("click", index=10)), reply(call("click", index=10)),
            reply(call("finish", status="cannot", answer="做不到")))
        self.assertEqual(self.ran, ["click"])
        self.assertTrue(any(m["role"] == "tool" and "没有带来进展" in m["content"] for m in executor._messages))
        self.assertEqual(state.terminal_reason, "agent_refused")

    def test_a_turn_that_keeps_repeating_one_step_ends(self):
        _executor, state, text = self.loop(*[reply(call("click", index=10)) for _ in range(5)], steps=10)
        self.assertEqual(self.ran, ["click"])
        self.assertEqual(state.terminal_reason, "agent_refused")
        self.assertIn("重复", text)

    def test_the_trace_names_the_control_a_step_was_on(self):
        self.loop(reply(call("input", index=9, text="Beta")),
                  reply(call("finish", status="cannot", answer="做不到")))
        self.assertTrue(self.turn["trace"][0].startswith("input[Customer]("), self.turn["trace"])

    def test_what_the_assistant_put_into_a_control_is_kept_for_the_check(self):
        executor = SlimTurnExecutor.__new__(SlimTurnExecutor)
        executor.knowledge, sent = None, []
        executor._act = lambda s, action, args, **kw: sent.append((action, args)) or ""
        executor._settled = lambda s, **kw: None
        state, turn = State(), {"touched": []}
        executor._tool(state, "input", {"index": 9, "text": ""}, turn)
        executor._tool(state, "select", {"index": 9, "option": "Beta"}, turn)
        self.assertEqual(turn["touched"], [("Customer", ""), ("Customer", "Beta")])
        self.assertEqual([action for action, _args in sent], ["fill", "select"])
        self.assertIn("用 select(index, option)", executor._tool(state, "click", {"index": 9}, turn)[0])
        sent.pop()
        executor._tool(state, "input", {"index": 9, "text": "Delta"}, turn)   # typed into a dropdown
        self.assertEqual(sent[-1], ("select", {"ref": "e9", "value": "Delta", "option_label": "Delta"}))
        said, _changed = executor._tool(state, "scroll", {"index": 7, "down": True}, turn)
        self.assertEqual(sent[-1], ("scroll", {"direction": "down", "pages": 1, "ref": "e8"}))
        self.assertIn("内容没有变化", said)    # the page it was shown is the page it had
        executor._act = lambda s, action, args, **kw: "没有成功：stale"
        executor._tool(state, "input", {"index": 9, "text": "Gamma"}, turn)
        self.assertEqual(len(turn["touched"]), 3)   # a step that failed put nothing there

    def test_the_check_is_told_which_controls_the_assistant_set_and_nothing_else_counts_as_changed(self):
        executor = SlimTurnExecutor.__new__(SlimTurnExecutor)
        executor._messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "把地址清空"}]
        state = State({"content": '{"ok": false, "problems": ["没有标出提交按钮"]}'})
        turn = {"reads": [], "marked": "", "trace": ["input[详细地址]({})"], "touched": [("详细地址", ""), ("电话", "010")],
                "handoffs": SessionHandoffPolicy.extending(())}
        self.assertEqual(executor._verify(state, "把地址清空", "已清空", turn), ["没有标出提交按钮"])
        asked = state.chat.seen[0][1]["content"]
        self.assertIn("助手本轮填写/选择过的控件（程序记录，按先后）：详细地址←(清空)；电话←010", asked)
        self.assertIn("助手填入的这些值没有原样出现在用户原话里：电话←010", asked)
        self.assertNotIn("form_changed", asked)

    def test_earlier_api_results_leave_the_conversation_once_they_outgrow_their_share(self):
        executor = SlimTurnExecutor.__new__(SlimTurnExecutor)
        executor._messages = [{"role": "tool", "_api": True, "content": f"total={n}\n" + "行" * 5000}
                              for n in range(4)]
        executor._forget_old_observations()
        kept = [len(m["content"]) > 1000 for m in executor._messages]
        self.assertEqual(kept, [False, False, True, True])
        self.assertTrue(executor._messages[0]["content"].startswith("total=0\n（更早的接口结果已省略"))

    def test_after_a_step_that_changes_the_page_the_rest_of_the_reply_is_not_run(self):
        def tool(s, name, args, turn):
            self.ran.append(name)
            return "已点击。", name == "click"
        executor, _state, _text = self.loop(
            reply(call("click", index=10), call("click", index=11)),
            reply(call("finish", status="cannot", answer="做不到")), tool=tool)
        self.assertEqual(self.ran, ["click"])
        self.assertTrue(any(m["role"] == "tool" and m["content"].startswith("未执行") for m in executor._messages))


class FindRecordTests(unittest.TestCase):
    class Port:
        def __init__(self, hits=(), refuse=False):
            self.hits, self.refuse, self.asked = hits, refuse, []

        def locate_reference(self, text, *, context=None):
            self.asked.append(text)
            if self.refuse:
                raise DataRefused("the index is unavailable", read_failed=True)
            return self.hits

        def index(self, *, context=None):
            return ApplicationIndex(
                entities=(IndexEntity("crm/customer", "CRM 客户"), IndexEntity("erp/customer", "ERP 客户")),
                pages=(IndexPage("crm-customer", "客户管理", "CRM 系统 / 客户管理", entity="crm/customer"),))

    class Turn:
        step = 0

        class approvals:
            @staticmethod
            def context(step, tool):
                return None

        class receipt:
            @staticmethod
            def stage(name):
                import contextlib
                return contextlib.nullcontext()

    def find(self, port, name):
        executor = SlimTurnExecutor.__new__(SlimTurnExecutor)
        executor.goal_port = port
        return executor._find_record(self.Turn, name)

    def test_it_says_which_modules_hold_a_record_of_that_name_and_where(self):
        port = self.Port(hits=(("crm/customer", "晨岚机电"), ("erp/customer", "晨岚机电")))
        told = self.find(port, "晨岚机电").splitlines()
        self.assertEqual(told, ["CRM 客户（crm/customer；页面：CRM 系统 / 客户管理）：晨岚机电",
                                "ERP 客户（erp/customer；页面：无）：晨岚机电"])
        self.assertEqual(port.asked, ["晨岚机电"])

    def test_nothing_found_is_said_without_claiming_absence(self):
        told = self.find(self.Port(), "松原设备")
        self.assertIn("没有在任何模块里找到", told)
        self.assertIn("可能不存在", told)

    def test_a_refused_read_is_told_and_an_empty_name_is_not_looked_up(self):
        self.assertIn("查找失败", self.find(self.Port(refuse=True), "晨岚机电"))
        port = self.Port()
        self.assertIn("请给出", self.find(port, ""))
        self.assertEqual(port.asked, [])


class HostConfigTests(unittest.TestCase):
    def test_slim_is_refused_without_the_reviewed_guard(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            YudaoHostConfig(api_base="http://yudao.invalid", public_origin="https://yudao.invalid",
                            state_dir=Path(tmp), executor_mode="slim")


if __name__ == "__main__":
    unittest.main()
