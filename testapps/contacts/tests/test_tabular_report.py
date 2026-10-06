"""A source-free REST-derived table proves the neutral scoped DataPort path."""
from collections import Counter
import json
import time
import unittest

from web_operation_agent.goal.data import (EVERYTHING, All, Native, Compare, DataRefused,
    EntitySchema, FieldSchema, Row, Schema, matches)
from web_operation_agent.goal.resolve.indexing import index_from_schema
from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent.goal.scoped import ScopedData
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent.goal.tabular import table_rows
from web_operation_agent.state.identity import OperationContext
from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.app import ACTORS_BY_ID, DEMO_SESSION_COOKIE, MinimalContactsApplication
from testapps.contacts.goal import ContactsGoalPort


class DomainReport:
    """Read-only domain totals from the current actor's real REST collection.

    No ORM, source parser, native ledger parameters or record IDs are involved.
    The application-owned Native scope names one domain and one actor context.
    """
    def __init__(self, adapter, context):
        self.contacts=ContactsGoalPort(adapter)
        self.context=context
        self._schema=Schema(entities=(EntitySchema("domains","Email domains",fields=(
            FieldSchema("domain","Domain","text",listed=True),
            FieldSchema("count","Contacts","number",listed=True)),operations=frozenset({"read"})),))

    def schema(self):
        self.contacts._require_actor(self.context)
        return self._schema

    def index(self, *, context=None):
        return index_from_schema(self.schema())

    def capabilities(self):
        return frozenset()

    def time_zone(self):
        return "UTC"

    def name_of(self,display):
        return display

    def scope(self,domain):
        return Native((self.context.binding_digest,domain),label=domain)

    def rows(self,entity,where,*,fields=(),order=(),limit=80):
        self.schema()
        if entity!="domains":
            raise DataRefused("unknown report")
        scopes=[]
        def local(c):
            if isinstance(c,Native):
                if (not isinstance(c.payload,tuple) or len(c.payload)!=2
                        or c.payload[0]!=self.context.binding_digest):
                    raise DataRefused("foreign report scope")
                scopes.append(c.payload[1])
                return EVERYTHING
            if isinstance(c,All):
                return All(tuple(local(x) for x in c.items))
            return c
        cond=local(where)
        if len(set(scopes))!=1:
            raise DataRefused("a single domain scope is required")
        domain=scopes[0]
        count=Counter(row['email'].rsplit('@',1)[-1]
                      for row in self.contacts._executor._contact_rows())
        # Only real REST rows visible to this actor contribute to the table.
        raw=[{"domain":domain,"count":count[domain]}] if domain in count else []
        binding=json.dumps([self.context.binding_digest,domain])
        result=[Row(r.reference,r.values["domain"],r.values,
                    {k:str(v) for k,v in r.values.items()})
                for r in table_rows(raw,binding=binding,max_rows=4000)]
        return tuple(Row(r.id,r.display,{k:v for k,v in r.values.items() if k in fields},
                         {k:v for k,v in r.shown.items() if k in fields})
                     for r in result if matches(cond,r))[:limit]

    def count(self,entity,where):
        return len(self.rows(entity,where,fields=("domain","count")))

    def total(self,entity,where,field):
        return sum(r.values[field] for r in self.rows(entity,where,fields=(field,))),""

    def groups(self,*args,**kwargs):
        raise DataRefused("this report does not offer grouping")

    def find(self,*args,**kwargs):
        raise AssertionError("scoped record lookup must stay inside the report")

    def read(self,*args,**kwargs):
        raise AssertionError("scoped row reads must stay inside the report")

    def page_address(self,*args,**kwargs):
        raise DataRefused("this derived report has no separate page")

    def record_address(self,*args,**kwargs):
        raise DataRefused("report rows have no record form")


class ContactReportTests(unittest.TestCase):
    def test_same_agent_data_tool_uses_source_free_rest_report_and_real_actor(self):
        from web_operation_agent.answer.evidence import ComputationEvidence, RecordEvidence, TurnEvidence
        from web_operation_agent.goal.read_tools import DataReadTools
        from web_operation_agent.business.errors import BusinessToolIdentityError
        with MinimalContactsApplication() as app:
            data=self.bind(app,"ada-lovelace")
            def factory(context, checkpoint):
                report=DomainReport(data.contacts._adapter,context)
                return ScopedData(report,{"domains":report.scope("example.test")})
            tools=DataReadTools(factory,data.context.principal.application_id,"contacts:read")
            result=tools.execute("resolve_data",{"deliverables":[
                {"kind":"fact","entity":"domains","record":"example.test","fields":["Contacts"]},
                {"kind":"aggregate","entity":"domains","metric":{"op":"sum","field":"Contacts"}}]},
                context=data.context,checkpoint=lambda:None,user_request="How many contacts use example.test?")
            count=next(e for e in result.evidence if isinstance(e,RecordEvidence) and e.field_name=="count")
            total=next(e for e in result.evidence if isinstance(e,ComputationEvidence) and e.operation=="sum count")
            self.assertEqual(("3","3"),(count.value,total.value))
            self.assertTrue(count.record_id.startswith("domains:"))
            TurnEvidence().capture(result.evidence,namespace="rest1")
            self.assertEqual((),result.allowed_routes)
            other=self.bind(app,"rosalind-franklin")
            with self.assertRaises(BusinessToolIdentityError):
                tools.execute("inspect_data",{"query":"domains"},context=other.context,
                              checkpoint=lambda:None,user_request="domains")
            self.assertEqual(0,app.write_requests)

    def test_rest_report_uses_authenticated_document_query_without_framework_or_source(self):
        from web_operation_agent.transport import RemoteSessionRegistry
        from web_operation_agent.schemas import ClientHello, PageObservation, ObservationEnvelope
        with MinimalContactsApplication() as app:
            data=self.bind(app,"ada-lovelace")
            actor=data.context.principal
            grant,channel=RemoteSessionRegistry().create(allowed_origins=[app.origin],principal=actor,session_id=data.context.session_id)
            self.addCleanup(channel.close)
            lease=channel.accept_hello(ClientHello(1,grant.session_id,"contacts-tab","test/1","contacts-document",
                ("dom.observe",),app.origin,application_id=actor.application_id,principal_digest=actor.binding_digest))["lease_id"]
            page=PageObservation(1,1,app.origin+"/contacts","Contacts",[],[],render_state="ready",page_route="/contacts",
                page_query={"path":"/api/contacts","parameters":{"domain":"example.test"}})
            channel.accept_observation(ObservationEnvelope(1,grant.session_id,"contact-query",1,page),lease_id=lease)
            channel.accept_message({"message_id":data.context.turn_id,"text":"How many contacts use this domain?",
                                    "page_url":page.url},lease_id=lease)
            channel.wait_for_message(timeout_s=0)
            observed=channel.page_for_read(data.context).observation
            self.assertEqual("/api/contacts",observed.page_query["path"])
            scoped=ScopedData(data,{"domains":data.scope(observed.page_query["parameters"]["domain"])})
            result=DataGoalPort(scoped).resolve(GoalSpec.from_json({"deliverables":[{
                "kind":"fact","entity":"domains","record":"example.test","fields":["Contacts"]}]})).deliverables[0]
            self.assertFalse(result.problem)
            self.assertEqual("3",result.facts[0].value)
            self.assertEqual(0,app.write_requests)
    def bind(self,application,actor_id):
        actor=ACTORS_BY_ID[actor_id]
        adapter=MinimalContactsAdapter(application.origin,api_token=actor.token)
        identity=adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={actor.session}")
        principal=adapter.identity.bind(identity,delegation_id="tabular-test")
        context=OperationContext.create(principal,session_id="table-session",turn_id="table-turn",
            operation_id="table-read",deadline_ms=int(time.time()*1000)+60000)
        return DomainReport(adapter,context)

    def test_rest_report_facts_and_totals_use_the_same_core_and_neutral_handles(self):
        with MinimalContactsApplication() as app:
            data=self.bind(app,"ada-lovelace")
            scoped=ScopedData(data,{"domains":data.scope("example.test")})
            port=DataGoalPort(scoped)
            expected=sum(r["email"].endswith("@example.test")
                         for r in data.contacts._executor._contact_rows())
            result=port.resolve(GoalSpec.from_json({"deliverables":[{
                "kind":"fact","entity":"domains","record":"example.test","fields":["Contacts"]},
                {"kind":"aggregate","entity":"domains","metric":{"op":"sum","field":"Contacts"}}]}))
            self.assertFalse(any(r.problem for r in result.deliverables))
            self.assertEqual(str(expected),result.deliverables[0].facts[0].value)
            self.assertEqual((("Contacts",f"{expected:.2f}"),),result.deliverables[1].aggregate.sums)
            self.assertEqual(0,app.write_requests)
            self.assertTrue(scoped.rows("domains",EVERYTHING)[0].id.startswith("table:"))
            other=self.bind(app,"rosalind-franklin")
            second=ScopedData(other,{"domains":other.scope("example.test")})
            self.assertNotEqual(scoped.rows("domains",EVERYTHING)[0].id,second.rows("domains",EVERYTHING)[0].id)
            with self.assertRaises(DataRefused):
                second.read("domains",scoped.rows("domains",EVERYTHING)[0].id,("count",))
            with self.assertRaises(DataRefused):
                other.count("domains",data.scope("example.test"))

    def test_empty_scope_and_record_page_are_not_fabricated(self):
        with MinimalContactsApplication() as app:
            data=self.bind(app,"ada-lovelace")
            port=DataGoalPort(ScopedData(data,{"domains":data.scope("absent.invalid")}))
            count=port.resolve(GoalSpec.from_json({"deliverables":[{
                "kind":"aggregate","entity":"domains","metric":{"op":"count"}}]})).deliverables[0]
            self.assertEqual(0,count.aggregate.count)
            present=DataGoalPort(ScopedData(data,{"domains":data.scope("example.test")}))
            page=present.resolve(GoalSpec.from_json({"deliverables":[{
                "kind":"page","entity":"domains","record":"example.test"}]})).deliverables[0]
            self.assertIsNone(page.page)
            self.assertEqual(0,app.write_requests)
