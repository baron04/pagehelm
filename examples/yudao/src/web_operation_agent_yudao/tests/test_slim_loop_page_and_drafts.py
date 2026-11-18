"""Page text projection, handoff gating, status questions, and draft choices tests."""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.model_loop_contracts import (
    AnswerTarget, LoopProcedure, LoopToolResult, ModelLoopProfile, PageActionOutcome, VerificationResult,
)
from web_operation_agent.schemas import ActionReceipt
from web_operation_agent.business.approvals import ApprovalWatch
from web_operation_agent.business.errors import BusinessToolAuthenticationError
from web_operation_agent.business.prefill_conversion import MutationPrefillOffer
from web_operation_agent.business.runtime import BusinessToolResult, BusinessToolRuntime
from web_operation_agent.composition.business_operations import RegisteredOperations
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.index import ApplicationIndex, IndexConvention, IndexEntity, IndexPage
from web_operation_agent.goal.skill_writes import SkillWrites
from web_operation_agent.privacy import SessionHandoffPolicy
from web_operation_agent.schemas.browser import ElementRef, PageObservation
from web_operation_agent.state.conversation import ConversationState, SQLiteConversationState
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent_yudao.host.native_server import YudaoHostConfig
from web_operation_agent_yudao.rules.skill_packages import packaged_skills
from web_operation_agent_yudao.reads.api_content import BEGIN, END
from web_operation_agent_yudao.reads.answer_numbers import untraced
from web_operation_agent_yudao.executor.page_prompts import (
    SYSTEM, TOOLS, VERIFY_SYSTEM, SlimTurnContext,
    _business_handoff_ready, _commits, _fields, _lines, _native_input_text,
    _own_list_facts, _own_list_path, _page_lines, _scroll_anchor, _shown,
    _tabs_not_shown, _today, conventions_text, page_text, system_text,
)
from web_operation_agent_yudao.executor.procedures import (
    _draft_only_request, _unrequested_draft_fields,
)
from web_operation_agent_yudao.executor.slim_loop import (
    ActorKnowledge, QueryParametersError, SlimTurnExecutor, TableReadTurnExecutor,
    YudaoLoopProcedures, _clean_user_answer, _is_allowed_direct_reply,
)
from web_operation_agent_yudao.writes.write_intent import WriteIntent
from web_operation_agent_yudao.tests import test_native_writes as native
from web_operation_agent_yudao.writes.writes import native_tools
from web_operation_agent_yudao.tests.slim_loop_support import (
    CONTENT, DEALS, DOCS, LEDGER, LEDGER_NAMES, Budget, Channel, Client, Ledger, Script, State,
    call, element, observation, reply,
)

class BusinessHandoffTests(unittest.TestCase):
    def test_user_status_handoff_binds_a_unique_actor_read_record_and_current_query(self):
        procedure=YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        procedure.goal_port=NS(data=NS(_client=NS()))
        procedure.knowledge=NS(_load=lambda:None,_validate_params=lambda *a:None)
        record={"id":7,"username":"account7","nickname":"合成用户","status":0}
        matches=[NS(id="7",name="合成用户")]
        digest=hashlib.sha256(b'["account7"]').hexdigest()
        control=element(12,"switch","状态开关：合成用户（account7）",id="woa-user-status-7",
                        **{"aria-checked":"true"})
        field=element(13,"textbox","用户名称","input",page_control="query",query_field="username",value="account7")
        read={"path":"/admin-api/system/user/page","query_digest":"a"*64,"query_fields":{"username":digest},
              "page":1,"page_size":10,"rows":1,"total":1,"revision":2}
        obs=replace(observation(),url="https://synthetic.test/system/user",page_route="/system/user",
                    render_state="ready",elements=[control,field],page_read=read,
                    page_query={"path":read["path"],"parameters":{}})
        state=NS(obs=obs,operation=None,page=NS(checkpoint=lambda:None))
        listed={"total":1,"list":[record]}
        bound=NS(_invoke=lambda *a,**k:listed if a[1]=="system/user/page" else record,
                 find=lambda *a,**k:matches)
        procedure.goal_port.data._client.get=lambda *a,**k:record
        with patch("web_operation_agent_yudao.reads.bound_data_reads.BoundDataReads",return_value=bound):
            self.assertTrue(procedure._user_status_target(state,control,"停用合成用户"))
            self.assertFalse(_business_handoff_ready(obs,control,"停用合成用户"))
            self.assertTrue(_business_handoff_ready(obs,control,"停用合成用户",status_target=True))
            self.assertFalse(procedure._user_status_target(state,control,"停用另一个用户"))
            matches.append(NS(id="8",name="合成用户"))
            self.assertFalse(procedure._user_status_target(state,control,"停用合成用户"))
            self.assertTrue(procedure._user_status_target(state,control,"停用用户编号7"))
            phone="13600000007"
            mobile=replace(field,attrs={**field.attrs,"query_field":"mobile","value":phone})
            phone_digest=hashlib.sha256(json.dumps([phone]).encode()).hexdigest()
            state.obs=replace(obs,elements=[control,mobile],page_read={**read,"query_fields":{"mobile":phone_digest}})
            self.assertTrue(procedure._user_status_target(state,control,"停用用户编号7"))
            empty=replace(field,ref="e14",attrs={**field.attrs,"value":""})
            empty_digest=hashlib.sha256(b'[""]').hexdigest()
            state.obs=replace(obs,elements=[control,mobile,empty],page_read={**read,"query_fields":{
                "mobile":phone_digest,"username":empty_digest}})
            self.assertTrue(procedure._user_status_target(state,control,"停用用户编号7"))
            state.obs=replace(state.obs,elements=[control,mobile])
            self.assertFalse(procedure._user_status_target(state,control,"停用用户编号7"))
            state.obs=replace(obs,elements=[control,mobile],page_read={**read,"query_fields":{"mobile":phone_digest}})
            listed["list"]=[{**record,"id":8}]
            self.assertFalse(procedure._user_status_target(state,control,"停用用户编号7"))
            listed["list"]=[record]
            state.obs=replace(obs,page_read={**read,"total":2,"rows":2})
            self.assertFalse(procedure._user_status_target(state,control,"停用用户编号7"))
            state.obs=replace(obs,page_read={**read,"query_fields":{"username":"b"*64}})
            self.assertFalse(procedure._user_status_target(state,control,"停用用户编号7"))
            state.obs=obs;record["status"]=1
            self.assertFalse(procedure._user_status_target(state,control,"启用用户编号7"))
            off=replace(control,attrs={**control.attrs,"aria-checked":"false"})
            self.assertTrue(procedure._user_status_target(state,off,"启用用户编号7"))

    def test_list_button_requires_separate_same_actor_target_verification(self):
        button = element(12, "button", "审批", "button")
        page = replace(observation(), elements=[button], page_route="/orders", render_state="ready")
        self.assertFalse(_business_handoff_ready(page, button, "审批最早的那张"))
        self.assertTrue(_business_handoff_ready(page, button, "审批最早的那张", list_target=True))
        self.assertFalse(_business_handoff_ready(replace(page, render_state="pending"), button,
                                                "审批最早的那张", list_target=True))
        self.assertFalse(_business_handoff_ready(page, replace(button, tag="span"), "审批最早的那张"))
        self.assertTrue(_business_handoff_ready(page, replace(button, tag="span"),
                                               "审批最早的那张", list_target=True))

    def test_record_button_requires_a_separately_verified_target(self):
        button = element(12, "button", "审核", "button")
        detail = replace(observation(), elements=[button], url="https://synthetic.test/orders/7",
                         page_route="/orders/7", record_route="/orders/7", render_state="ready")
        self.assertFalse(_business_handoff_ready(detail, button, "审核订单"))
        self.assertTrue(_business_handoff_ready(detail, button, "审核编号7的订单",
                                                detail_target=True))
        self.assertFalse(_business_handoff_ready(replace(detail, render_state="pending"), button,
                                                 "审核编号7的订单", detail_target=True))
        draft = replace(button, name="提交", attrs={"form_scope": "dialog:new"})
        self.assertTrue(_business_handoff_ready(replace(observation(), elements=[draft]), draft,
                                                "填表并标出提交按钮"))

    def test_detail_button_binds_actor_read_name_and_rejects_ambiguous_names(self):
        path = "/orders/detail/7"
        page = replace(observation(), url="https://synthetic.test" + path,
                       page_route=path, record_route=path, render_state="ready")
        data = NS(_ui=NS(records={"/orders": "/orders/detail/{id}"}),
                  _entities={"orders": NS(has_get=True, get_path="orders/get")},
                  _client=NS(get=lambda *a, **k: None),
                  _display=lambda _entity, record: record["name"])
        port = NS(data=data, index=lambda **kw: NS(pages=[NS(key="/orders", entity="orders")]))
        procedure = YudaoLoopProcedures(NS(), port, "https://synthetic.test")
        state = NS(obs=page, operation=object(), page=NS(checkpoint=lambda: None))
        detail = {"id": 7, "name": "甲订单"}
        matches = [NS(id="7", name="甲订单")]
        bound = NS(_invoke=lambda *a, **kw: detail,
                   record_address=lambda *a: path,
                   find=lambda *a, **kw: tuple(matches))
        with patch("web_operation_agent_yudao.reads.bound_data_reads.BoundDataReads", return_value=bound):
            self.assertTrue(procedure._detail_handoff_target(state, "审核甲订单"))
            self.assertFalse(procedure._detail_handoff_target(state, "审核乙订单"))
            matches.append(NS(id="8", name="甲订单"))
            self.assertFalse(procedure._detail_handoff_target(state, "审核甲订单"))
            self.assertTrue(procedure._detail_handoff_target(state, "审核甲订单，编号7"))
            self.assertFalse(procedure._detail_handoff_target(state, "审核甲订单，编号70"))
            detail["id"] = 8
            self.assertFalse(procedure._detail_handoff_target(state, "审核甲订单，编号7"))
            detail["id"] = 7
            state.obs = replace(page, record_route="/orders/detail/8")
            self.assertFalse(procedure._detail_handoff_target(state, "审核甲订单，编号7"))
            state.obs = page
            def expired(*_args, **_kwargs):
                raise BusinessToolAuthenticationError("session expired")
            bound._invoke = expired
            with self.assertRaises(BusinessToolAuthenticationError):
                procedure._detail_handoff_target(state, "审核甲订单，编号7")


class PageTextTests(unittest.TestCase):
    def test_applied_query_values_require_matching_read_digests_and_keep_selected_tabs(self):
        value = "梁"
        digest = hashlib.sha256(json.dumps([value], ensure_ascii=False,
                                          separators=(",", ":")).encode()).hexdigest()
        read = {"path": "/admin-api/crm/contact/page", "query_digest": "a" * 64,
                "query_fields": {"name": digest}, "revision": 2,
                "page": 1, "page_size": 10, "total": 0, "rows": 0}
        controls = [element(12, "textbox", "姓名", "input", page_control="query",
                            query_field="name", field_label="姓名", value=value),
                    element(13, "tab", "我负责的", **{"aria-selected": "true"}),
                    element(14, "tab", "我参与的", **{"aria-selected": "false"})]
        obs = replace(observation(), elements=controls, page_read=read,
                      page_query={"path": read["path"], "parameters": {}})
        text = page_text(obs, handoffs=SessionHandoffPolicy.extending(()))
        self.assertIn("已核验的列表查询条件: 姓名=梁", text)
        self.assertIn("选中的页签/视图: 我负责的", text)
        changed = replace(obs, page_read={**read, "query_fields": {"name": "b" * 64}})
        self.assertNotIn("已核验的列表查询条件:", page_text(changed, handoffs=SessionHandoffPolicy.extending(())))

    def test_hydrated_button_names_survive_removing_the_only_text_child(self):
        for name in ("新增", "展开", "password=hidden"):
            obs = observation()
            obs.elements = [element(1, "button", name, "button"), element(2, "span", name, "span")]
            obs.content = f"[1]<button type=button>\n\t[2]<span>{name} />"
            text = page_text(obs, handoffs=SessionHandoffPolicy.extending(()))
            self.assertNotIn("[2]", text)
            self.assertIn("[1]<button type=button>", text)
            if name.startswith("password"):
                self.assertNotIn("hidden", text)
            else:
                self.assertIn("[1]<button type=button> " + name, text)

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

    def test_duplicate_label_span_is_not_offered_as_a_control_but_independent_controls_remain(self):
        obs = observation()
        obs.elements = [element(1, "button", "编辑", "button"), element(2, "span", "编辑", "span"),
                        element(3, "button", "编辑", "button"), element(4, "span", "编辑", "span")]
        obs.content = "[1]<button>编辑\n\t[2]<span>编辑 />\n\t[3]<button>编辑 />\n[4]<span>编辑 />"
        text = page_text(obs, handoffs=SessionHandoffPolicy.extending(()))
        self.assertNotIn("[2]", text)
        for number in (1, 3, 4):
            self.assertIn(f"[{number}]", text)
        procedures = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        self.assertIsNone(procedures._element(NS(obs=obs), 2))
        self.assertEqual("e1", procedures._element(NS(obs=obs), 1).ref)

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

    def test_a_cached_pages_refresh_is_not_stated_as_this_pages_list(self):
        own = "/admin-api/erp/account/page"
        foreign = "/admin-api/crm/customer/page"
        read = {"path": foreign, "query_digest": "0" * 64, "query_fields": {},
                "revision": 3, "page": 1, "page_size": 10, "total": 240, "rows": 10}
        query = {"path": foreign, "parameters": {"pageNo": "1", "sceneType": "1"}}
        obs = replace(observation(), url="https://synthetic.test/erp/finance/account",
                      page_read=read, page_query=query)
        text = page_text(obs, handoffs=SessionHandoffPolicy.extending(()), list_path=own)
        self.assertNotIn("共 240 条", text)
        self.assertNotIn("页面生效的查询", text)
        # With no map answer the runtime's own table binding stands alone.
        text = page_text(obs, handoffs=SessionHandoffPolicy.extending(()))
        self.assertIn("共 240 条", text)
        # Only the page's own endpoint is stated as its list and applied query.
        obs = replace(obs, page_read={**read, "path": own},
                      page_query={**query, "path": own})
        text = page_text(obs, handoffs=SessionHandoffPolicy.extending(()), list_path=own)
        self.assertIn("共 240 条", text)
        self.assertIn("页面生效的查询", text)

    def test_an_unreadable_map_verifies_no_list_at_all(self):
        read = {"path": "/admin-api/erp/account/page", "query_digest": "0" * 64,
                "query_fields": {}, "revision": 1, "page": 1, "page_size": 10,
                "total": 2, "rows": 2}
        obs = replace(observation(), url="https://synthetic.test/erp/finance/account",
                      page_read=read)
        text = page_text(obs, handoffs=SessionHandoffPolicy.extending(()), list_path="")
        self.assertNotIn("共 2 条", text)

    def test_the_pages_own_list_path_comes_from_the_actor_map(self):
        obs = replace(observation(), url="https://synthetic.test/erp/finance/account")
        resolve = lambda route: "/admin-api/erp/account/page" if route == "/erp/finance/account" else None
        self.assertEqual(_own_list_path(NS(data=NS(page_list_path=resolve)), obs),
                         "/admin-api/erp/account/page")
        self.assertIsNone(_own_list_path(NS(), obs))
        self.assertIsNone(_own_list_path(NS(data=NS()), obs))

        def broken(route):
            raise DataRefused("map unreadable")
        self.assertEqual(_own_list_path(NS(data=NS(page_list_path=broken)), obs), "")
        read = {"path": "/admin-api/erp/account/page", "query_digest": "0" * 64,
                "query_fields": {}, "revision": 1, "page": 1, "page_size": 10,
                "total": 2, "rows": 2}
        self.assertEqual(_own_list_facts(replace(obs, page_read=read), ""), (None, None))

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

    def test_what_a_look_showed_is_the_same_at_any_epoch_and_differs_when_the_page_does(self):
        first, again = observation(epoch=1), observation(epoch=2)
        self.assertNotEqual(first.content_digest, again.content_digest)   # the digest covers the epoch
        self.assertEqual(_shown(first), _shown(again))
        self.assertNotIn('"epoch"', _shown(again))
        self.assertNotEqual(_shown(first), _shown(observation(epoch=2, customer="Beta")))
        self.assertNotEqual(_shown(first), _shown(observation(epoch=2, messages=["已保存"])))
        self.assertEqual(again.to_json()["elements"][0]["epoch"], 2)      # and reading it changed nothing
        # The controller stars what is new since its last look; a look later the same page has none.
        starred = observation(epoch=3, content=CONTENT.replace("\t[10]<button", "\t*[10]<button"))
        self.assertIn("*[10]", starred.content)
        self.assertEqual(_shown(starred), _shown(first))
        self.assertNotEqual(_shown(first), _shown(observation(content=CONTENT.replace("Order 1", "Order *[1]"))))

    def test_fields_are_read_by_their_labels(self):
        self.assertEqual(_fields(observation()), {"Customer": "Acme"})

    def test_only_an_element_the_page_text_listed_may_be_acted_on(self):
        class State:
            obs = observation()
        self.assertIsNone(YudaoLoopProcedures._element(None, State, 3))    # a menu entry
        self.assertIsNone(YudaoLoopProcedures._element(None, State, 99))   # nothing there
        self.assertEqual(YudaoLoopProcedures._element(None, State, 10).name, "Search")



class StatusQuestionTests(unittest.TestCase):
    def test_status_questions_keep_page_tools_and_prompt_encodes_four_allowed_five_must_check(self):
        from web_operation_agent_yudao.executor.page_prompts import SYSTEM_RULES
        self.assertIn("四允许、五必查", SYSTEM_RULES)
        self.assertIn("ev_", SYSTEM_RULES)
        self.assertIn("cannot", SYSTEM_RULES)
        schemas = [schema for schema in TOOLS if schema["function"]["name"] in
                   {"find_record", "call_api", "goto", "click", "finish"}]
        executor = SlimTurnExecutor.__new__(SlimTurnExecutor)
        executor.turn_context = SlimTurnContext(current_request="新程办公的销售订单现在是什么状态？")
        with patch.object(ModelDrivenTurnExecutor, "_tools", return_value=schemas):
            offered = executor._tools({})
        self.assertEqual([schema["function"]["name"] for schema in offered],
                         ["find_record", "goto", "click", "call_api", "finish"])

    def test_four_allowed_vs_disguised_business_routing(self):
        self.assertTrue(_is_allowed_direct_reply("你好", "你好，请问有什么可以帮您？"))
        self.assertTrue(_is_allowed_direct_reply("你能做什么？", "我可以协助查询业务数据、定位页面或准备表单。"))
        self.assertTrue(_is_allowed_direct_reply("把下面这段话翻译成英文：Hello", "你好"))
        self.assertTrue(_is_allowed_direct_reply("125 * 4 等于多少？", "500"))
        self.assertFalse(_is_allowed_direct_reply("新程办公的销售订单现在是什么状态？", "已完成"))
        self.assertFalse(_is_allowed_direct_reply("你好，帮我看一下当前仓库库存还有多少？", "还有 10 件"))
        self.assertFalse(_is_allowed_direct_reply("请概括一下客户权限配置规则", "所有员工都有权限"))


class DraftChoiceTests(unittest.TestCase):
    def setUp(self):
        self.procedures = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        self.procedures.knowledge = NS(handoffs=SessionHandoffPolicy.extending(()))
        self.procedures._settled = lambda *a, **kw: None
        self.attrs = {"form_scope":"dialog:create", "form_instance":"live", "field_label":"产品",
                      "aria-haspopup":"listbox", "aria-controls":"owned", "aria-expanded":"false"}
        self.control = element(1, "combobox", "产品", "input", **self.attrs)
        self.state = NS(obs=observation())
        self.state.obs.elements = [self.control]
        self.sent = []
        self.changed = [replace(self.control, ref="e7", epoch=2,
                                attrs={**self.attrs, "aria-expanded":"true"}),
                        replace(element(8, "option", "PLC 控制器 P5", "li", listbox_owner="owned"), epoch=2)]
        def act(s, action, args, **kw):
            self.sent.append((action, args))
            if action == "click":
                s.obs = replace(s.obs, epoch=2, elements=self.changed)
            return ""
        self.procedures._act = act

    def test_offscreen_label_uses_owned_runtime_search_and_returns_choices_on_explicit_failure(self):
        act = self.procedures._act
        def unavailable(s, action, args, **kw):
            act(s, action, args, **kw)
            if action == "select":
                return LoopToolResult("没有唯一的精确标签", kind="page", state="unverified",
                                      dispatch="acknowledged", error_code="option_unavailable")
        self.procedures._act = unavailable
        text = self.procedures._select(self.state, self.control, "PLC控制器P5").feedback
        self.assertEqual(["select"], [a for a, _ in self.sent])
        self.assertEqual("PLC控制器P5", self.sent[-1][1]["option_label"])
        self.assertIn("没有唯一的精确标签", text)

    def test_exact_label_uses_one_runtime_selection_without_server_preopening(self):
        self.assertEqual("", self.procedures._select(self.state, self.control, "PLC 控制器 P5"))
        self.assertEqual([("select", {"ref":"e1", "value":"PLC 控制器 P5",
                                     "option_label":"PLC 控制器 P5"})], self.sent)

    def test_actor_enum_label_reaches_selection_and_handoff_with_its_exact_typography(self):
        self.control = replace(self.control, attrs={**self.attrs, "field_label":"客户级别"})
        self.state.obs = replace(self.state.obs, elements=[self.control], content="[1]<input />")
        self.state.terminal_reason = ""
        self.procedures.goal_port = NS(data=NS(choice_label=lambda route, label, value:
            "A （重点客户）" if label == "客户级别" else value))
        expected = []
        def handoff(_state, _before, fields, _button, **_kw):
            expected.extend(fields)
            return LoopToolResult("已交接", kind="page", marked="确定")
        self.procedures.turn_context = SlimTurnContext(executor_turn={})
        with patch("web_operation_agent_yudao.executor.procedures.handoff_draft", handoff):
            self.procedures._fill_fields(self.state, [{"label":"客户级别", "value":"A（重点客户）"}],
                "", {"touched":[],"trace":[]}, then_highlight="确定")
        self.assertEqual(self.sent, [("select", {"ref":"e1", "value":"A （重点客户）",
                                               "option_label":"A （重点客户）"})])
        self.assertEqual(expected[0][1], "A （重点客户）")

    def test_partial_form_failure_keeps_typed_dispatch_and_names_the_unverified_field(self):
        self.state.obs = replace(self.state.obs, elements=[self.control], content="[1]<input />")
        self.state.terminal_reason = ""
        def failed(s, _action, _args, **_kw):
            s.terminal_reason = "agent_refused"
            return LoopToolResult("选项未确认，不会自动重发。", kind="page", state="unverified",
                                  dispatch="acknowledged", error_code="unverified",
                                  terminal_reason="agent_refused", terminal_answer="选项未确认，不会自动重发。")
        self.procedures._act = failed
        result = self.procedures._fill_fields(self.state, [{"label":"产品","value":"缺少的精确型号"}],
            "", {"touched":[],"trace":[]})
        self.assertEqual((result.state,result.dispatch,result.error_code),
                         ("unverified","acknowledged","unverified"))
        self.assertIn("产品：选项未确认", result.feedback)
        self.assertIn("后续字段未继续处理", result.feedback)
        self.assertNotEqual(result.feedback, "操作没有完成。")
        self.assertEqual(result.feedback, result.terminal_answer)

    def test_live_datetime_precision_normalizes_a_day_before_dispatch_and_readback(self):
        from web_operation_agent_yudao.executor.page_prompts import _native_input_text
        for precision, wanted in (("datetime","2026-10-22 00:00:00"),("date","2026-10-22"),("","2026-10-22")):
            with self.subTest(precision=precision):
                date = element(1,"combobox","下次联系时间","input",form_input_kind=precision,
                    field_label="下次联系时间",**{"aria-haspopup":"dialog"})
                self.state.obs = replace(self.state.obs,elements=[date],content="[1]<input />")
                self.state.terminal_reason = ""
                self.sent.clear()
                self.procedures._fill_fields(self.state,[{"label":"下次联系时间","value":"2026-10-22"}],
                    "",{"touched":[],"trace":[]})
                self.assertEqual(wanted,self.sent[0][1]["value"])
                self.assertEqual("2026-02-30",_native_input_text(date,"2026-02-30"))
                self.assertEqual("2026-10-22 15:30:00",_native_input_text(date,"2026-10-22 15:30:00"))

    def test_prior_viewport_options_do_not_replace_runtime_owned_selection(self):
        option = self.changed[1]
        for options in ([replace(option, attrs={"listbox_owner":"foreign"})],
                        [replace(option, attrs={**option.attrs, "aria-disabled":"true"})],
                        [option, replace(option, ref="e9")],
                        [option, replace(option, ref="e9", attrs={**option.attrs, "aria-disabled":"true"})]):
            with self.subTest(options=options):
                self.sent.clear()
                self.changed = [self.changed[0], *options]
                self.procedures._select(self.state, self.control, "PLC 控制器 P5")
                self.assertEqual(["select"], [a for a, _ in self.sent])

    def test_missing_form_or_list_ownership_never_receives_selection(self):
        control = replace(self.control, attrs={**self.attrs, "form_instance":""})
        self.assertEqual("invalid_target", self.procedures._select(self.state, control, "PLC 控制器 P5").error_code)
        self.assertEqual([], self.sent)


class ActionFeedbackTests(unittest.TestCase):
    def test_unverified_native_choice_keeps_its_explanation_through_the_common_loop(self):
        from web_operation_agent.tests.execution.test_product_model_loop import LoopFixture, call, reply
        from web_operation_agent.execution.model_loop_contracts import PageProcedureContext
        procedures = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        procedures.knowledge = NS(handoffs=SessionHandoffPolicy.extending(()))
        procedures._settled = lambda *_a, **_kw: None
        def handler(fixture, _name, _arguments):
            return procedures._fill_fields(context,[{"label":"客户级别","value":"Z（不存在）"}],
                "",{"touched":[],"trace":[]})
        fixture = LoopFixture([reply(call("fill_fields")),reply(call("finish",status="done",answer="done"))],handler)
        control = element(1,"combobox","客户级别","input",field_label="客户级别",form_scope="dialog:create",
            form_instance="live",**{"aria-haspopup":"listbox","aria-controls":"owned"})
        fixture.state.obs = replace(observation(),elements=[control],content="[1]<input />")
        sent = []
        def execute(action, args, **_kwargs):
            sent.append(action)
            receipt = ActionReceipt(1,"choice-cmd",action,args,False,error_code="option_unavailable",
                no_effect=True,write_guard_held=True,observation=fixture.state.obs)
            fixture._tool_receipts.append(receipt)
            fixture.state.terminal_reason = "agent_refused"
            # A browser's no-effect attestation is not the application's successful
            # verification: preserve both error codes and stop without replaying.
            return PageActionOutcome("unverified","acknowledged","unverified",json.dumps(receipt.to_json()))
        bridge = NS(execute=execute,observation=lambda:fixture.state.obs,
                    stopped=lambda:bool(fixture.state.terminal_reason))
        context = PageProcedureContext(None,bridge,lambda _facts:None,lambda **_facts:None,
                                       lambda *_args:contextlib.nullcontext())
        answer = fixture.run()
        self.assertIn("客户级别：",answer)
        self.assertIn("未能确认当前表单",answer)
        self.assertIn("不会自动重发",answer)
        self.assertIn("没有点击提交按钮",answer)
        self.assertEqual(["select"],sent)
        self.assertEqual(1,len(fixture.state.chat.seen))

    def test_a_record_tab_only_uses_the_fast_ending_with_a_verified_request_target(self):
        procedures = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        procedures.knowledge = None
        tab = element(12, "tab", "联系人", "div", id="tab-contacts", **{"aria-selected":"false"})
        current = replace(observation(), url="https://synthetic.test/customers/1",
            page_route="/customers/1", record_route="/customers/1", elements=[tab],
            content='[12]<div role=tab>联系人 />')
        context = NS(obs=current)
        procedures._act = lambda *_a, **_k: None
        procedures._settled = lambda *_a, **_k: None
        for target_bound in (False, True):
            procedures._detail_handoff_target = lambda *_a: target_bound
            result = procedures._tool(context, "click", {"index":12,"answer":"已切换"}, {})
            self.assertEqual(not target_bound, result.review_completion)

    def test_refusal_reaches_agent_without_a_settlement_or_false_permission_claim(self):
        procedures = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        procedures.knowledge = None
        for code, dispatch, state in (("unreviewed", "not_dispatched", "refused"),
                                      ("invalid_target", "not_dispatched", "refused"),
                                      ("stale_binding", "not_dispatched", "refused"),
                                      ("unverified", "uncertain", "unverified")):
            with self.subTest(code=code):
                sent = []
                outcome = PageActionOutcome(state, dispatch, code)
                context = NS(obs=observation(), page=NS(execute=lambda action, args, **kw:
                             sent.append(action) or outcome))
                result = procedures._tool(context, "click", {"index": 10}, {"marked": ""})
                self.assertEqual(["click"], sent)
                self.assertEqual((state, dispatch, code), (result.state, result.dispatch, result.error_code))
                if code == "unreviewed":
                    self.assertIn("本部署未开放这个页面动作", result.feedback)
                    self.assertIn("这不代表账号无权限", result.feedback)
                    self.assertIn("尚未审核", result.feedback)
                    # Recorded turn Q052: the refusal named no other route and the turn stopped.
                    self.assertIn("find_record 取得它的详情页地址后 goto 打开", result.feedback)
                    self.assertNotIn("不要换其它入口", result.feedback)
                if code == "invalid_target":
                    self.assertIn("不能据此断定菜单未审核", result.feedback)
                if code in ("unreviewed", "invalid_target"):
                    # A1（P15 意图首跑）：页面入口被拒后，接口已读到的答案没有被作答。
                    self.assertIn("已读到的", result.feedback)
                    self.assertIn("直接作答", result.feedback)


class SettledTests(unittest.TestCase):
    """How long the page is given after a step, by what the looks at it show."""

    def settle(self, first, *later, gone_after=None, prior=(), **kw):
        executor = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        epochs = iter(range(1, 99))

        def look(content, state="ready", url="https://synthetic.test/orders", route=None):
            # Every look has a new epoch, on itself and on each element, as every observation
            # of a live page does; and what is new since the look before is starred once. The
            # application reports the page it rendered: by default the one at the address.
            epoch = next(epochs)
            starred = "\t*" if epoch % 2 else "\t"
            route = url.split("synthetic.test", 1)[1] if route is None else route
            return NS(content=content, render_state=state, url=url, page_route=route, to_json=lambda: {
                "epoch": epoch, "url": url, "render_state": state,
                "content": f"[1]<div />\n{starred}[2]<button>{content} />",
                "elements": [{"ref": "e2", "role": "button", "name": content, "epoch": epoch}]})
        coming, self.waited = [look(*item) for item in later], []
        state = NS(obs=look(*first))
        history = (*[look(*item) for item in prior], state.obs) if prior else ()

        def dispatch(s, action, args, **_kw):
            self.waited.append((action, args["ms"]))
            if gone_after is not None and len(self.waited) > gone_after:
                return PageActionOutcome("unavailable", "not_dispatched", "turn_stopped")
            s.obs = coming.pop(0)
            return NS(state="completed")
        state.page = NS(action_observations=lambda: history, execute=lambda action, args: dispatch(state, action, args))
        self.outcome = executor._settled(state, **kw)
        return state.obs.content

    def test_a_page_that_is_already_still_costs_one_short_look(self):
        self.assertEqual(self.settle(("a",), ("a",)), "a")
        self.assertEqual((self.waited, self.outcome), ([("wait_for", 150)], "still"))

    def test_verification_settlement_is_reused_without_another_wait(self):
        self.assertEqual(self.settle(("a",), prior=(("a", "pending"), ("a",))), "a")
        self.assertEqual((self.waited, self.outcome), ([], "still"))

    def test_pending_verification_does_not_count_as_a_stable_look(self):
        self.assertEqual(self.settle(("a",), ("a",), prior=(("a", "pending"),)), "a")
        self.assertEqual((self.waited, self.outcome), ([("wait_for", 150)], "still"))

    def test_a_page_that_changed_is_looked_at_until_two_looks_agree(self):
        self.assertEqual(self.settle(("a",), ("b",), ("b",)), "b")
        self.assertEqual(self.waited, [("wait_for", 150)] * 2)

    def test_a_page_that_keeps_changing_is_taken_as_it_is(self):
        self.assertEqual(self.settle(("a",), ("b",), ("c",), ("d",)), "c")
        self.assertEqual((len(self.waited), self.outcome), (2, "moving"))

    def test_what_the_page_reads_is_waited_for_and_then_it_must_be_still(self):
        self.assertEqual(self.settle(("a", "pending"), ("a", "pending"), ("b",), ("b",)), "b")
        self.assertEqual(self.waited, [("wait_for", 400), ("wait_for", 400), ("wait_for", 150)])

    def test_a_read_that_starts_late_is_waited_for_too(self):
        self.assertEqual(self.settle(("a",), ("a", "pending"), ("b",), ("b",)), "b")
        self.assertEqual(self.waited, [("wait_for", 150), ("wait_for", 400), ("wait_for", 150)])

    def test_the_looks_before_a_late_read_do_not_count_for_what_it_brought(self):
        self.assertEqual(self.settle(("a",), ("b",), ("c", "pending"), ("d",), ("e",), ("e",)), "e")
        self.assertEqual(self.waited, [("wait_for", 150)] * 2 + [("wait_for", 400)] + [("wait_for", 150)] * 2)
        self.assertEqual(self.outcome, "still")

    def test_a_page_that_was_opened_is_waited_for_until_the_browser_is_there(self):
        there, old = "https://synthetic.test/crm/customer", "https://synthetic.test/orders"
        self.assertEqual(self.settle(("a", "ready", old), ("a", "ready", old), ("b", "ready", there),
                                     ("b", "ready", there), path="/crm/customer"), "b")
        self.assertEqual(self.waited, [("wait_for", 400), ("wait_for", 400), ("wait_for", 150)])

    def test_an_opened_page_has_arrived_when_the_application_rendered_it_not_when_the_address_changed(self):
        there, old = "https://synthetic.test/crm/customer", "https://synthetic.test/orders"
        self.assertEqual(self.settle(("a", "ready", there, "/orders"), ("a", "ready", there, ""),
                                     ("b", "ready", there), ("b", "ready", there), path="/crm/customer"), "b")
        self.assertEqual(self.waited, [("wait_for", 400), ("wait_for", 400), ("wait_for", 150)])
        self.assertEqual(self.outcome, "still")

    def test_an_opened_page_that_reports_an_error_is_not_waited_out(self):
        there = "https://synthetic.test/crm/customer"
        self.settle(("a", "blocked", there), ("a", "ready", there), path="/crm/customer")
        self.assertEqual((self.waited, self.outcome), ([], "blocked"))

    def test_a_page_whose_rendering_is_not_reported_is_not_taken_as_opened(self):
        there = "https://synthetic.test/crm/customer"
        for state, route in (("pending", None), ("unknown", None), ("ready", "/orders")):
            with self.subTest(state=state, route=route):
                self.settle(*[("a", state, there, route)] * 5, path="/crm/customer", waits=2)
                self.assertEqual((self.waited, self.outcome), ([("wait_for", 400)] * 4, "waiting"))

    def test_waiting_for_what_the_page_reads_is_bounded(self):
        self.settle(("a", "pending"), *[("a", "pending")] * 9, waits=2)
        self.assertEqual((self.waited, self.outcome), ([("wait_for", 400)] * 4, "waiting"))

    def test_a_turn_that_ended_while_waiting_is_left_alone(self):
        self.settle(("a",), ("b",), ("b",), gone_after=0)
        self.assertEqual((len(self.waited), self.outcome), (1, "gone"))




if __name__ == "__main__":
    unittest.main()
