from copy import deepcopy
import unittest
from unittest.mock import patch

from web_operation_agent.goal.data import (EVERYTHING, All, Any, Compare, DataRefused,
                                           Native, Not, all_of)
from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent.goal.scoped import ScopedData
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent_yudao.goal_compat.goal import YudaoGoalData
from web_operation_agent_yudao.rules.ui_source import ListView, UiSource
from .test_goal import _Client, _array_list, _prop, DOCS as CRM_DOCS


PATH = "books/balance/list"
ENTITY = "books/balance"
PAGE = "/books/balance"
DOCS = _array_list(PATH,"Balance",{
    "account":_prop("string","Account"),"period":_prop("string","Period"),
    "kind":_prop("string","Row kind"),"debit":_prop("number","Debit"),
    "credit":_prop("number","Credit"),"balance":_prop("number","Balance")})
DOCS["paths"]["/admin-api/"+PATH]["get"]["parameters"] = [
    {"in":"query","name":name,"required":True,"schema":{"type":kind}}
    for name,kind in (("ledgerId","integer"),("month","string"))]
DOCS["paths"]["/admin-api/"+PATH]["get"]["parameters"].append(
    {"in":"query","name":"rowKind","schema":{"type":"string"}})


class Client(_Client):
    read_generation = 0
    def __init__(self):
        super().__init__()
        self.records = [{"account":"Cash","period":"2026-09","kind":"Monthly",
                         "debit":120,"credit":40,"balance":80},
                        {"account":"Cash","period":"2026-09","kind":"Yearly",
                         "debit":240,"credit":80,"balance":160}]
    def get(self,path,**params):
        if path=="system/auth/get-permission-info":
            return {"menus":[{"name":"Balance","path":PAGE,"component":"books/balance/index"}],
                    "permissions":["books:balance:create","books:balance:update"]}
        if path==PATH:
            self.calls.append((path,params))
            if not {"ledgerId","month"}<=params.keys():
                raise DataRefused("required scope missing")
            return [dict(r,period=params["month"],debit=r["debit"]*params["ledgerId"])
                    for r in self.records if not params.get("rowKind") or r["kind"]==params["rowKind"]]
        return super().get(path,**params)


class YudaoTableTests(unittest.TestCase):
    def test_native_page_resolution_binds_successful_query_and_never_falls_back_on_failure(self):
        from web_operation_agent.schemas import PageObservation
        from web_operation_agent_yudao.goal_compat.page_goals import NativePageGoalPort
        from dataclasses import replace
        page=PageObservation(1,1,"https://test.invalid"+PAGE,"",[],[],render_state="ready",page_route=PAGE,
                             page_query={"path":"/admin-api/"+PATH,"parameters":{"ledgerId":"2","month":"2026-09"}})
        port=NativePageGoalPort(self.data)
        readings=[]
        port.page_reader=lambda context: readings.append(context) or page
        spec=GoalSpec.from_json({"deliverables":[{"entity":ENTITY,"kind":"fact","fields":["Debit"],
            "metric":{"op":"count","filters":[{"field":"Row kind","values":["Monthly"]}]}}]})
        result=port.resolve(spec,current_path=PAGE).deliverables[0]
        self.assertEqual("240",result.facts[0].value)
        for failed in (replace(page,page_query=None),replace(page,render_state="pending"),
                       replace(page,page_route="/other"),
                       replace(page,page_query={"path":"/admin-api/other/list","parameters":{}})):
            port.page_reader=lambda context:failed
            calls=len(self.client.calls)
            result=port.resolve(spec,current_path=PAGE).deliverables[0]
            self.assertEqual("unavailable",result.read_failure)
            self.assertFalse(result.facts)
            self.assertEqual(calls,len(self.client.calls))
        port.page_reader=lambda context: (_ for _ in ()).throw(AssertionError("navigation needs no read scope"))
        result=port.resolve(GoalSpec.from_json({"deliverables":[{"kind":"page","entity":ENTITY}]}),current_path=PAGE)
        self.assertFalse(result.deliverables[0].problem)
    def test_bound_crm_scene_is_not_overwritten_by_the_three_scene_union(self):
        client=_Client()
        data=YudaoGoalData(client,deepcopy(CRM_DOCS))
        scope=data.query_scope("crm/contract",{"sceneType":1},label="Owned contracts")
        rows=data.rows("crm/contract",scope)
        self.assertEqual(1,len(rows))
        self.assertEqual([1],[p['sceneType'] for path,p in client.calls if path=="crm/contract/page"])

    def setUp(self):
        self.client=Client()
        self.data=YudaoGoalData(self.client,deepcopy(DOCS),ui=UiSource(views={
            "books/balance/index":ListView(columns=("account","period","kind","debit","credit","balance"),
                                           endpoint="/"+PATH)}))
        self.scope=self.data.query_scope(ENTITY,{"ledgerId":1,"month":"2026-09"},label="Ledger 1 September")
        self.port=DataGoalPort(ScopedData(self.data,{ENTITY:self.scope}))

    def ask(self,d,port=None):
        return (port or self.port).resolve(GoalSpec.from_json({"deliverables":[{"entity":ENTITY,**d}]})).deliverables[0]

    def test_scoped_list_and_fact_preserve_all_rows_and_values(self):
        result=self.ask({"kind":"list","fields":["Account","Period","Row kind","Debit","Credit","Balance"]})
        self.assertFalse(result.problem)
        self.assertEqual(2,result.aggregate.count)
        self.assertTrue(any("120" in str(v) and "80" in str(v) for v in result.aggregate.records))
        result=self.ask({"kind":"fact","fields":["Debit","Credit","Balance"],
                         "metric":{"op":"count","filters":[{"field":"Row kind","values":["Monthly"]}]}})
        self.assertFalse(result.problem,result)
        self.assertEqual(["120","40","80"],[f.value for f in result.facts])
        calls=[p for path,p in self.client.calls if path==PATH]
        self.assertEqual([{"ledgerId":1,"month":"2026-09"}],calls)

    def test_identical_rows_do_not_collapse_or_prove_uniqueness(self):
        self.client.records=[self.client.records[0],deepcopy(self.client.records[0])]
        result=self.ask({"kind":"list","fields":["Account","Debit"]})
        self.assertEqual(2,result.aggregate.count)
        result=self.ask({"kind":"fact","fields":["Debit"]})
        self.assertFalse(result.facts)
        self.assertTrue(result.problem or result.clarification)
        offered=self.port.data.find(ENTITY,"Cash")
        self.assertEqual(2,len(offered))
        self.assertIn("行引用=table:",offered[0].display)
        self.assertEqual((),self.port.data.find(ENTITY,"  "))
        chosen=self.ask({"kind":"fact","record":offered[0].display,"fields":["Debit"]})
        self.assertFalse(chosen.problem,chosen)
        self.assertEqual("120",chosen.facts[0].value)

    def test_scope_cache_and_handles_do_not_cross_ledger_month_actor_or_generation(self):
        first=self.data.rows(ENTITY,self.scope,fields=("debit",))
        second_scope=self.data.query_scope(ENTITY,{"ledgerId":2,"month":"2026-09"},label="Ledger 2")
        second=self.data.rows(ENTITY,second_scope,fields=("debit",))
        third_scope=self.data.query_scope(ENTITY,{"ledgerId":1,"month":"2026-08"},label="August")
        third=self.data.rows(ENTITY,third_scope,fields=("debit",))
        self.assertEqual(120,first[0].values["debit"])
        self.assertEqual(240,second[0].values["debit"])
        self.assertNotEqual(first[0].id,third[0].id)
        with self.assertRaises(DataRefused):
            ScopedData(self.data,{ENTITY:second_scope}).read(ENTITY,first[0].id,("debit",))
        foreign=YudaoGoalData(Client(),deepcopy(DOCS))
        foreign.schema()
        with self.assertRaises(DataRefused):
            foreign.count(ENTITY,self.scope)
        self.client.read_generation=1
        with self.assertRaises(DataRefused):
            self.data.count(ENTITY,self.scope)

    def test_invalid_conflicting_and_expired_scope_cannot_widen_a_read(self):
        for cond in (Native({"ledgerId":1}),Not(self.scope),Any((self.scope,EVERYTHING)),
                     all_of(self.scope,Compare("month","eq","2026-08"))):
            with self.subTest(cond=cond),self.assertRaises(DataRefused):
                self.data.count(ENTITY,cond)
        for params in ({"ledgerId":True,"month":"2026-09"}, {"ledgerId":1},
                       {"ledgerId":1,"month":"2026-09","secret":"x"}):
            with self.assertRaises(DataRefused):
                self.data.query_scope(ENTITY,params,label="invalid")
        with patch("web_operation_agent_yudao.goal_compat.goal.time.monotonic",return_value=self.scope.payload.expires+1):
            with self.assertRaises(DataRefused):
                self.data.count(ENTITY,self.scope)
        self.assertFalse([p for path,p in self.client.calls if path==PATH])

    def test_nested_scope_cannot_reuse_a_wider_optional_parameter_cache(self):
        self.assertEqual(2,self.data.count(ENTITY,self.scope))
        narrow=self.data.query_scope(ENTITY,{"ledgerId":1,"month":"2026-09","rowKind":"Monthly"},
                                     label="Monthly only")
        self.assertEqual(1,self.data.count(ENTITY,All((All((narrow,)),))))
        self.assertEqual("Monthly",[p for path,p in self.client.calls if path==PATH][-1]["rowKind"])

    def test_report_handles_never_become_record_forms_or_write_fields(self):
        schema=self.data.schema()
        self.assertFalse(schema.page(PAGE).opens_records)
        self.assertEqual(frozenset({"read"}),schema.entity(ENTITY).operations)
        self.assertTrue(all(not f.on_form for f in schema.entity(ENTITY).fields))
        row=self.data.rows(ENTITY,self.scope)[0]
        with self.assertRaises(DataRefused):
            self.data.record_address(ENTITY,row.id)
        name=row.display
        result=self.ask({"kind":"page","record":name})
        self.assertIsNone(result.page)
        result=self.ask({"kind":"handoff","record":name,"action":"Debit","value":"999"})
        self.assertEqual("write",result.denied)

    def test_malformed_or_too_large_table_is_not_an_empty_answer(self):
        for value in (None,{},[{"id":123,"account":"Cash"}],[None],
                      [{"account":"Cash","children":[{"account":"Child"}]}]):
            with patch.object(self.client,"get",side_effect=lambda path,**params:value if path==PATH else []):
                with self.assertRaises(DataRefused) as error:
                    self.data.rows(ENTITY,self.scope)
                self.assertTrue(error.exception.read_failed)
        self.data._max_rows=1
        with self.assertRaises(DataRefused):
            self.data.count(ENTITY,self.scope)
