"""The slim loop's own parts over synthetic pages and a synthetic API; no model, no browser."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.goal.data import DataRefused
from web_operation_agent.privacy import SessionHandoffPolicy
from web_operation_agent.schemas.browser import ElementRef, PageObservation
from web_operation_agent_yudao.native_server import YudaoHostConfig
from web_operation_agent_yudao.slim_loop import (ActorKnowledge, SlimTurnExecutor, _fields, _page_lines,
                                                 _today, page_text)


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
        raise AssertionError(path)


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
        turn = {"reads": [], "marked": "", "trace": [], "baseline": {},
                "handoffs": SessionHandoffPolicy.extending(()), "shown": []}
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
        sent_back = [m for m in executor._messages if m["role"] == "user" and m["content"].startswith("验收没有通过")]
        self.assertEqual(len(sent_back), 1)
        self.assertIn("没有标出提交按钮", sent_back[0]["content"])

    def test_the_turn_is_refused_when_its_model_steps_run_out(self):
        _executor, state, text = self.loop(*[reply(call("search_menu", keyword="x")) for _ in range(3)], steps=3)
        self.assertEqual(state.terminal_reason, "agent_refused")
        self.assertIn("步骤用完", text)

    def test_a_model_that_stops_calling_tools_is_reminded_then_refused(self):
        _executor, state, text = self.loop(*[reply(content="我想想") for _ in range(3)])
        self.assertEqual((text, state.terminal_reason), ("我想想", "agent_refused"))

    def test_nothing_named_after_finish_in_the_same_reply_is_run(self):
        self.loop(reply(call("finish", status="cannot", answer="做不到"), call("click", index=10)))
        self.assertEqual(self.ran, [])

    def test_after_a_step_that_changes_the_page_the_rest_of_the_reply_is_not_run(self):
        def tool(s, name, args, turn):
            self.ran.append(name)
            return "已点击。", name == "click"
        executor, _state, _text = self.loop(
            reply(call("click", index=10), call("click", index=11)),
            reply(call("finish", status="cannot", answer="做不到")), tool=tool)
        self.assertEqual(self.ran, ["click"])
        self.assertTrue(any(m["role"] == "tool" and m["content"].startswith("未执行") for m in executor._messages))


class HostConfigTests(unittest.TestCase):
    def test_slim_is_refused_without_the_reviewed_guard(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            YudaoHostConfig(api_base="http://yudao.invalid", public_origin="https://yudao.invalid",
                            state_dir=Path(tmp), executor_mode="slim")


if __name__ == "__main__":
    unittest.main()
