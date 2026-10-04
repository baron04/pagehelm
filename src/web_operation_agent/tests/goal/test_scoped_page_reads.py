"""Scoped reads refresh the same Agent state; read-only fields remain declarable."""
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest

from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.goal.data import EntitySchema, FieldSchema, Schema
from web_operation_agent.goal.resolve.indexing import index_from_schema
from web_operation_agent.goal.ports import Resolution, Resolved
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent.schemas import PageObservation


class ScopedPageReadTests(unittest.TestCase):
    def test_fresh_read_updates_agent_state_before_resolution_and_failure_blocks_data(self):
        executor=GoalTurnExecutor.__new__(GoalTurnExecutor)
        executor.config=AgentTurnConfig("Read reports",Path("unused"),observe_before_reads=True)
        executor.goal_port=SimpleNamespace(requires_page_reading=lambda spec,**kw:True)
        before=PageObservation(1,1,"https://app.test/report","",[],[])
        after=replace(before,epoch=2,page_route="/report",page_query={"path":"/api/report","parameters":{"month":"2026-09"}})
        state=SimpleNamespace(obs=before,budget=SimpleNamespace(check=lambda:1),receipt=SimpleNamespace(stage=lambda _:nullcontext()))
        spec=GoalSpec.from_json({"deliverables":[{"kind":"fact","entity":"report","fields":["Balance"]}]})
        seen=[]
        def dispatch(s,action,args):
            self.assertEqual(("observe",{}),(action,args))
            s.obs=after
            return SimpleNamespace(ok=True)
        executor._dispatch=dispatch
        executor._resolve_declared=lambda s,spec,**kw: seen.append(s.obs) or Resolution((Resolved("fact"),),())
        executor._resolve(state,spec)
        self.assertEqual([after],seen)
        executor._dispatch=lambda *args:None
        failed=executor._resolve(state,spec)
        self.assertEqual("unavailable",failed.deliverables[0].read_failure)
        self.assertEqual([after],seen)
        executor.goal_port.requires_page_reading=lambda *args,**kw:False
        executor._dispatch=lambda *args: (_ for _ in ()).throw(AssertionError("ordinary API read needs no page refresh"))
        executor._resolve(state,spec)

    def test_read_only_schema_fields_survive_missing_dynamic_ui_columns_without_becoming_writable(self):
        fields=(FieldSchema("name","Name","text",listed=True,on_form=False),
                FieldSchema("balance","Balance","money",listed=False,on_form=False))
        readonly=EntitySchema("report","Report",fields=fields,operations=frozenset({"read"}))
        self.assertEqual(["name","balance"],[f.key for f in index_from_schema(Schema((readonly,))).entity("report").fields])
        ordinary=replace(readonly,operations=frozenset({"read","write"}))
        self.assertEqual(["name"],[f.key for f in index_from_schema(Schema((ordinary,))).entity("report").fields])
        self.assertTrue(all(not f.on_form for f in readonly.fields))
