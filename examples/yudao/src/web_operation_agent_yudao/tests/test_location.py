"""Location batches preserve ordinary admission and never enter business editors."""
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from web_operation_agent.execution.model_loop_contracts import LoopToolResult
from web_operation_agent.execution.action_admission import ActionAdmissionRefused
from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent.schemas import ActionReceipt
from web_operation_agent.business.errors import BusinessToolIdentityError
from web_operation_agent_yudao.reads.location import LocationProcedures, location_schemas
from web_operation_agent_yudao.executor.slim_loop import TOOLS, _requirements
from web_operation_agent_yudao.executor.slim_loop import YudaoLoopProcedures
from web_operation_agent_yudao.tests.slim_loop_support import element, observation
from web_operation_agent_yudao.tests import test_read_actions as read_fixture


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.owner = LocationProcedures.__new__(LocationProcedures)
        self.owner._act = Mock(return_value=None)
        self.owner._settled = Mock(return_value="still")
        self.owner._fill_fields = Mock(return_value=LoopToolResult("verified", kind="page"))
        self.owner.goal_port = NS(data=NS(_ui=NS(records={"/orders":"/orders/detail/{id}"})))
        self.turn = {"touched": [], "reads": [], "trace": [], "marked": ""}
        self.s = NS(obs=observation(page_route="/orders", render_state="ready"), terminal_reason="",
                    operation=NS(session_id="session", turn_id="turn"), page=NS(checkpoint=lambda:None))

    def query_page(self, *, business=False):
        self.s.obs = replace(self.s.obs, elements=[
            element(1, "textbox", "客户", "input", field_label="客户",
                    page_control="form" if business else "query"),
            element(2, "button", "搜索", "button", page_control="query")])

    def test_filter_batch_rejects_business_editor_before_any_fill(self):
        self.query_page(business=True)
        result = self.owner._tool(self.s, "filter_page", {"fields":[{"label":"客户","value":"Other"}]}, self.turn)
        self.assertEqual(result.state,"unverified")
        self.owner._fill_fields.assert_not_called()

    def test_filter_uses_existing_batch_and_search_verification(self):
        self.query_page()
        fields = [{"label": "客户", "value": "Acme"}]
        result = self.owner._tool(self.s, "filter_page", {"fields": fields}, self.turn)
        self.assertEqual(result.state, "completed")
        self.owner._fill_fields.assert_called_once_with(self.s, fields, "搜索", self.turn,
            query_only=True,locate=self.owner._locate_query_control,choose=self.owner._choose_query)

    def row(self, ref, identifier, name="Acme", table="1"):
        return element(ref, "button", "详情", "button", record_id=identifier,
                       record_table=table, record_values=json.dumps([["客户", name]]))

    def test_recognizes_business_value_but_never_picks_first_duplicate_row(self):
        self.s.obs = replace(self.s.obs, elements=[self.row(1, "17"), self.row(2, "18")])
        args = {"column": "客户", "value": "Acme"}
        self.assertEqual(self.owner._tool(self.s, "open_record", args, self.turn).state, "refused")
        self.owner._act.assert_not_called()
        self.owner._tool(self.s, "open_record", {**args, "record_id": "18"}, self.turn)
        self.owner._act.assert_called_once_with(self.s, "click", {"ref":"e2"})

    def test_same_id_in_two_tables_is_ambiguous_and_dangerous_action_never_dispatched(self):
        self.s.obs = replace(self.s.obs, elements=[self.row(1, "17"), self.row(2, "17", table="2")])
        self.assertEqual(self.owner._tool(self.s, "open_record", {"record_id": "17"}, self.turn).state, "refused")
        self.s.obs = replace(self.s.obs, elements=[self.row(1, "17")])
        self.assertEqual(self.owner._tool(self.s, "open_record", {"record_id": "17", "button": "删除"}, self.turn).state, "refused")
        self.owner._act.assert_not_called()

    def test_refused_or_uncertain_action_is_not_retried_or_claimed_complete(self):
        failure = LoopToolResult("uncertain", kind="page", state="unverified", dispatch="uncertain")
        self.owner._act.return_value = failure
        self.s.obs = replace(self.s.obs, elements=[self.row(1, "17")])
        self.assertIs(self.owner._tool(self.s, "open_record", {"record_id": "17"}, self.turn), failure)
        self.owner._act.assert_called_once()
        self.assertEqual(self.owner._settled.call_count, 2)  # no post-dispatch wait/retry

    def test_name_link_still_requires_the_expected_native_record_arrival(self):
        link = replace(self.row(1,"17"), tag="a", role="link", name="Acme")
        self.s.obs = replace(self.s.obs,elements=[link])
        result = self.owner._tool(self.s,"open_record",{"column":"客户","value":"Acme"},self.turn)
        self.assertEqual(result.state,"unverified")
        self.owner._act.assert_called_once_with(self.s,"click",{"ref":"e1"})
        def arrive(*_args, **_kw):
            self.s.obs = replace(self.s.obs, url="https://synthetic.test/orders/detail/17",
                                 page_route="/orders/detail/17", record_route="/orders/detail/17",content="Acme details")
        self.owner._act.side_effect = arrive
        result = self.owner._tool(self.s,"open_record",{"column":"客户","value":"Acme"},self.turn)
        self.assertEqual(result.state,"completed")

    def test_row_change_before_click_never_dispatches(self):
        self.s.obs=replace(self.s.obs,elements=[self.row(1,"17")])
        count=0
        def fresh(s,**_kw):
            nonlocal count
            count+=1
            if count==2:s.obs=replace(s.obs,elements=[self.row(1,"18")])
            return "still"
        self.owner._settled.side_effect=fresh
        result=self.owner._tool(self.s,"open_record",{"record_id":"17"},self.turn)
        self.assertEqual(result.error_code,"stale_binding")
        self.owner._act.assert_not_called()

    def paging_observations(self, *, owner="sizes", expanded="true", changed_read=False):
        proof = {"path":"/admin-api/orders/page", "query_digest":"b"*64, "query_fields":{},
                 "revision":1, "page":1, "page_size":10, "rows":10, "total":240}
        control = element(1, "combobox", "每页条数", "input", page_control="page_size",
                          **{"aria-controls":"sizes", "aria-expanded":"false"})
        before = replace(self.s.obs, epoch=1, elements=[control], page_read=proof)
        after = replace(before, epoch=2, elements=[
            replace(control, epoch=2, attrs={**control.attrs, "aria-expanded":expanded}),
            replace(element(2, "option", "20条/页", "li", listbox_owner=owner), epoch=2)],
            page_read={**proof, "revision":2} if changed_read else proof)
        return before, after

    def test_paging_open_unverified_can_only_continue_on_fresh_exact_owned_options(self):
        before, after = self.paging_observations()
        self.s.obs = before
        self.owner._paging_choice = ("session", "turn", "20条/页")
        self.owner._settled.side_effect = lambda s, **_kw: setattr(s, "obs", after) or "still"
        failure = LoopToolResult("intermediate", kind="page", state="unverified", dispatch="acknowledged")
        with patch.object(YudaoLoopProcedures, "_act", return_value=failure) as dispatch:
            self.assertIsNone(LocationProcedures._act(self.owner, self.s, "click", {"ref":"e1"}))
            dispatch.assert_called_once()  # popup is observed, never clicked again
        self.assertEqual([e.ref for e in self.owner._paging_options(self.s)], ["e2"])

    def test_unrelated_closed_changed_or_uncertain_paging_popup_is_not_promoted(self):
        for options in ({"owner":"other"}, {"expanded":"false"}, {"changed_read":True}):
            with self.subTest(options=options):
                before, after = self.paging_observations(**options)
                self.s.obs = before
                self.owner._paging_choice = ("session", "turn", "20条/页")
                self.owner._settled.side_effect = lambda s, **_kw: setattr(s, "obs", after) or "still"
                failure = LoopToolResult("intermediate", kind="page", state="unverified", dispatch="acknowledged")
                with patch.object(YudaoLoopProcedures, "_act", return_value=failure):
                    self.assertIs(LocationProcedures._act(self.owner, self.s, "click", {"ref":"e1"}), failure)
        before, after = self.paging_observations()
        self.s.obs = before
        for state, dispatch in (("unverified","uncertain"), ("refused","not_dispatched")):
            failure = LoopToolResult("stop", kind="page", state=state, dispatch=dispatch)
            self.owner._settled.reset_mock()
            with patch.object(YudaoLoopProcedures, "_act", return_value=failure):
                self.assertIs(LocationProcedures._act(self.owner, self.s, "click", {"ref":"e1"}), failure)
            self.owner._settled.assert_not_called()

    def test_navigation_and_paging_keep_capabilities_and_all_ordinary_tools(self):
        self.assertEqual(_requirements("filter_page", {"tab": "全部"}, self.s.obs), {"facets", "sections"})
        self.assertEqual(_requirements("page_number", {}, self.s.obs), {"paging"})
        self.assertTrue({tool["function"]["name"] for tool in TOOLS}.issubset(
            {tool["function"]["name"] for tool in location_schemas(TOOLS)}))

    def test_query_values_and_dates_are_not_rejected_by_user_source_matching(self):
        self.query_page()
        for value in ("Unrequested", "2099-10-01 ~ 2099-10-31", "", "New dictionary label"):
            with self.subTest(value=value):
                fields=[{"label":"客户","value":value}]
                result=self.owner._tool(self.s,"filter_page",{"fields":fields},self.turn)
                self.assertEqual(result.state,"completed")
                self.owner._fill_fields.assert_called_with(self.s,fields,"搜索",self.turn,
                    query_only=True,locate=self.owner._locate_query_control,choose=self.owner._choose_query)

    def test_filter_locates_query_in_the_list_region_and_rereads_before_resolving(self):
        self.s.obs=replace(self.s.obs,content='[5]<div data-scrollable="top=500 bottom=0">List\n\t[6]<input />',elements=[
            element(5,"div","List","div"),element(6,"combobox","20条/页","input",page_control="page_size")])
        def reveal(*_args,**_kwargs):
            self.query_page()
            return "still"
        self.owner._settled.side_effect=reveal
        result=self.owner._tool(self.s,"filter_page",{"fields":[{"label":"客户","value":"Other"}]},self.turn)
        self.assertEqual(result.state,"completed")
        self.owner._act.assert_called_once_with(self.s,"scroll",{"direction":"up","pages":3,"ref":"e6"})
        self.owner._fill_fields.assert_called_once()

    def test_query_locator_does_not_follow_a_changed_user_page(self):
        self.s.obs=replace(self.s.obs,content='[5]<div data-scrollable="top=500 bottom=0">List\n\t[6]<input />',elements=[
            element(5,"div","List","div"),element(6,"combobox","20条/页","input",page_control="page_size")])
        def changed(*_args,**_kwargs):
            self.s.obs=replace(self.s.obs,url="https://synthetic.test/other")
            return "still"
        self.owner._settled.side_effect=changed
        result=self.owner._tool(self.s,"filter_page",{"fields":[{"label":"客户","value":"Other"}]},self.turn)
        self.assertEqual(result.error_code,"stale_binding")
        self.owner._fill_fields.assert_not_called()

    def test_query_locator_uses_visible_bound_rows_when_query_and_pager_are_both_offscreen(self):
        self.s.obs=replace(self.s.obs,content='[5]<div data-scrollable="top=500 bottom=500">List\n\t[6]<a>Record</a>',
            elements=[element(5,"div","List","div"),self.row(6,"17")])
        self.owner._settled.side_effect=lambda *_args,**_kwargs:self.query_page() or "still"
        result=self.owner._tool(self.s,"filter_page",{"fields":[{"label":"客户","value":"Acme"}]},self.turn)
        self.assertEqual(result.state,"completed")
        self.owner._act.assert_called_once_with(self.s,"scroll",{"direction":"up","pages":3,"ref":"e6"})

    def test_query_locator_rereads_once_before_declaring_an_unobserved_query_region(self):
        self.owner._settled.side_effect=lambda *_args,**_kwargs:self.query_page() or "still"
        result=self.owner._tool(self.s,"filter_page",{"fields":[{"label":"客户","value":"Acme"}]},self.turn)
        self.assertEqual(result.state,"completed")
        self.owner._act.assert_not_called()

    def test_missing_query_diagnostic_is_private_bounded_and_cannot_change_the_result(self):
        self.s.obs=replace(self.s.obs,elements=[element(1,"textbox","Secret record","input",
            field_label="客户",value="private-value")])
        self.s.audit=Mock()
        before=self.s.obs.to_json()
        result=self.owner._locate_query_control(self.s,"客户")
        self.assertEqual(result.error_code,"target_unobserved")
        event,facts=self.s.audit.call_args.args[0],self.s.audit.call_args.kwargs
        self.assertEqual(event,"location_query_unobserved")
        self.assertTrue(facts["controls"][0]["matches_label"])
        self.assertFalse(facts["controls"][0]["query"])
        self.assertNotIn("private-value",json.dumps(facts))
        self.assertNotIn("Secret record",json.dumps(facts))
        self.assertEqual(self.s.obs.to_json(),before)
        self.s.audit.side_effect=RuntimeError("diagnostic collector unavailable")
        after=self.owner._locate_query_control(self.s,"客户")
        self.assertEqual((after.state,after.error_code,after.feedback),(result.state,result.error_code,result.feedback))

    def test_ordinary_fallback_uses_the_same_shared_boundary_as_control(self):
        self.query_page(business=True)
        for name,args in (("input",{"index":1,"text":"Other"}),
                          ("select",{"index":1,"option":"Other"}),
                          ("fill_fields",{"fields":[{"label":"客户","value":"Other"}]}),
                          ("goto",{"path":"/x","open":"新增"})):
            with patch.object(YudaoLoopProcedures,"_tool",return_value=LoopToolResult("shared",kind="page")) as shared:
                result=self.owner._tool(self.s,name,args,self.turn)
                self.assertEqual(result.feedback,"shared")
                shared.assert_called_once_with(self.s,name,args,self.turn)

    def test_query_click_and_fill_no_longer_have_a_second_value_source_gate(self):
        self.query_page()
        with patch.object(YudaoLoopProcedures,"_act",return_value=None) as shared:
            LocationProcedures._act(self.owner,self.s,"fill",{"ref":"e1","value":"Other"})
            self.s.obs=replace(self.s.obs,elements=[element(3,"option","Other","li")])
            LocationProcedures._act(self.owner,self.s,"click",{"ref":"e3"})
            self.assertEqual(shared.call_count,2)

    def query_choices(self):
        combo=element(1,"combobox","Status","input",page_control="query",field_label="Status",
            form_scope="form:query",form_instance="mounted-form",query_field="status",
            **{"aria-controls":"choices","aria-haspopup":"listbox","aria-expanded":"false"})
        expanded=replace(combo,ref="e4",attrs={**combo.attrs,"aria-expanded":"true"})
        option=element(7,"option","Available","li",listbox_owner="choices")
        return combo,expanded,option

    def test_query_selection_clicks_the_fresh_owner_and_exact_owned_option(self):
        combo,expanded,option=self.query_choices()
        self.s.obs=replace(self.s.obs,elements=[combo])
        self.owner._select=Mock()
        def click(_s,_action,args):
            if args["ref"]=="e1":self.s.obs=replace(self.s.obs,elements=[expanded,option])
            else:self.s.obs=replace(self.s.obs,elements=[replace(expanded,ref="e9",
                attrs={**expanded.attrs,"aria-expanded":"false","selected_option":"Available"})])
        self.owner._act.side_effect=click
        self.assertIsNone(self.owner._choose_query(self.s,combo,"Available"))
        self.assertEqual([c.args[1:] for c in self.owner._act.call_args_list],
                         [("click",{"ref":"e1"}),("click",{"ref":"e7"})])
        self.owner._select.assert_not_called()

    def test_query_selection_never_picks_foreign_duplicate_disabled_or_changed_options(self):
        combo,expanded,option=self.query_choices()
        for controls in ([expanded,replace(option,attrs={"listbox_owner":"other"})],
                [expanded,option,replace(option,ref="e8")],
                [expanded,replace(option,attrs={**option.attrs,"aria-disabled":"true"})],
                [replace(expanded,attrs={**expanded.attrs,"form_instance":"replaced"}),option]):
            with self.subTest(controls=controls):
                self.s.obs=replace(self.s.obs,elements=[combo])
                self.owner._act.reset_mock()
                self.owner._act.side_effect=lambda *_:setattr(self.s,"obs",replace(self.s.obs,elements=controls))
                result=self.owner._choose_query(self.s,combo,"Available")
                self.assertEqual(result.state,"unverified")
                self.assertEqual(self.owner._act.call_count,1)

    def test_query_selection_does_not_repeat_an_uncertain_option_click(self):
        combo,expanded,option=self.query_choices()
        self.s.obs=replace(self.s.obs,elements=[expanded,option])
        failure=LoopToolResult("unknown",kind="page",state="unverified",dispatch="uncertain")
        self.owner._act.return_value=failure
        self.assertIs(self.owner._choose_query(self.s,expanded,"Available"),failure)
        self.owner._act.assert_called_once_with(self.s,"click",{"ref":"e7"})

    def test_unknown_query_structure_retains_the_ordinary_select_fallback(self):
        combo,_,_=self.query_choices()
        self.owner._select=Mock(return_value=None)
        unknown=replace(combo,attrs={"page_control":"query"})
        self.assertIsNone(self.owner._choose_query(self.s,unknown,"Available"))
        self.owner._select.assert_called_once_with(self.s,unknown,"Available")
        self.owner._act.assert_not_called()


class LocationRowClickAdmissionTests(unittest.TestCase):
    def setUp(self):
        from . import test_read_only_window_actions as fixture
        self.fixture=fixture.ReadOnlyWindowActionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.owner=LocationProcedures.__new__(LocationProcedures)
        self.owner._settled=Mock(return_value="still")
        self.owner._act=self.admitted_click
        row=element(1,"link","Acme","a",record_id="17",record_table="table-1",
            record_values=json.dumps([["客户名称","Acme"]]),href="javascript:void(0)")
        self.s=NS(obs=self.fixture.page(path="/crm/customer",elements=[row]),
            operation=self.fixture.context,page=NS(checkpoint=lambda:None))
        self.dispatched=[]
        self.latest=None

    def admitted_click(self,s,action,args,**_kwargs):
        before=s.obs
        try:
            intent,permit=self.fixture.runtime.prepare(action,args,before,
                context=s.operation,checkpoint=s.page.checkpoint)
            self.fixture.runtime.consume(intent,permit,self.latest or before,
                context=s.operation,checkpoint=s.page.checkpoint)
        except ActionAdmissionRefused as error:
            return LoopToolResult("not dispatched",kind="page",state="refused",error_code=error.code)
        self.dispatched.append((action,args))
        after=replace(self.fixture.page(before.epoch+1,path="/crm/customer/detail/17"),content="Acme details")
        receipt=ActionReceipt(1,"detail-click",action,{**args,"epoch":before.epoch},True,
            url_before=before.url,url_after=after.url,observation=after,write_guard_held=True)
        self.fixture.runtime.verify(intent,permit,receipt,context=s.operation,checkpoint=s.page.checkpoint)
        s.obs=after
        return None

    def test_native_record_link_click_needs_no_declared_address_but_uses_shared_actor_admission(self):
        result=self.owner._open_record(self.s,{"column":"客户名称","value":"Acme"})
        self.assertEqual(result.state,"completed")
        self.assertEqual(self.dispatched,[("click",{"ref":"e1"})])

    def test_refusing_policy_or_replaced_node_never_dispatches(self):
        self.latest=replace(self.s.obs,elements=[replace(self.s.obs.elements[0],attrs={
            **self.s.obs.elements[0].attrs,"record_id":"18"})])
        result=self.owner._open_record(self.s,{"record_id":"17","column":"客户名称","value":"Acme"})
        self.assertEqual(result.error_code,"stale_binding")
        self.assertFalse(self.dispatched)

    def test_actor_mismatch_does_not_turn_into_a_callback_click(self):
        self.fixture.user={"id":2,"deptId":10}
        with self.assertRaises(BusinessToolIdentityError):
            self.owner._open_record(self.s,{"record_id":"17","column":"客户名称","value":"Acme"})
        self.assertFalse(self.dispatched)
