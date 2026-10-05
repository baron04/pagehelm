"""A plain HTML contacts search qualifies the neutral contract without Vue/source."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from web_operation_agent.goal.page_filters import PageFilterDraft, PageFilterField, apply_page_filters
from web_operation_agent.goal.ports import PagePlan
from web_operation_agent.goal.spec import Filter, GoalSpec
from web_operation_agent.goal.executor import _ask_what_it_can
from web_operation_agent.schemas import PageObservation, ElementRef, ActionReceipt
from web_operation_agent.tests.business.test_scoped_forms import session_for


class PlainContactSearch:
    def __init__(self):
        self.calls = []
        self.revision = 'instance-1'
        self.complete = True
        self.guard = True
        self.denied = False
    def prepare_page_filters(self, plan, observation, *, context):
        return PageFilterDraft('/contacts', 'form:search', self.revision,
            (PageFilterField('Name', 'Ada'),), 'Search', '/contacts/query', (('name', 'Ada'),))
    def observation(self, epoch, *, query=None):
        return PageObservation(1, epoch, 'https://contacts.test/contacts', 'Contacts', [], [
            ElementRef('field', 'textbox', 'Name', 'input', epoch,
                {'form_scope':'form:search', 'form_instance':'instance-1', 'field_label':'Name'}),
            ElementRef('search', 'button', 'Search', 'button', epoch,
                {'form_scope':'form:search', 'form_instance':'instance-1'})],
            render_state='ready', page_route='/contacts', page_query=query)
    def execute(self, action, args, **kwargs):
        self.calls.append((action,args))
        query = {'path':'/contacts/query','parameters':{'name':'Ada'}} if action == 'click' and self.complete else None
        return ActionReceipt(1,str(len(self.calls)),action,args,True,
            write_guard_held=self.guard, observation=self.observation(len(self.calls)+1,query=query))


class PageFilterContractTests(unittest.TestCase):
    def setup_flow(self, app=None):
        app = app or PlainContactSearch()
        session = session_for(app.execute)
        state = SimpleNamespace(obs=app.observation(1), step=0, page_actions=0,
            approvals=session._approvals, writes=session._writes, browser=session.browser,
            ledger=session.ledger, budget=SimpleNamespace(check=lambda:30),
            receipt=SimpleNamespace(stage=lambda _:__import__('contextlib').nullcontext()),
            terminal_reason='',detail='')
        state.approvals.context=lambda *a: None
        state.writes.expect_decision=lambda *a: None
        executor = SimpleNamespace(goal_port=app, _wait_ready=lambda *a,**k:True)
        plan = PagePlan('/contacts','Contacts',expected_path='/contacts',conditions=(Filter('Name',('Ada',)),))
        return app,executor,state,plan
    def test_applies_inputs_then_read_query_and_requires_applied_read(self):
        for complete in (True,False):
            app,executor,state,plan=self.setup_flow()
            app.complete=complete
            self.assertEqual(apply_page_filters(executor,state,plan),complete)
            self.assertEqual([a for a,_ in app.calls],['fill','click'])
            self.assertEqual(app.calls[0][1]['value'],'Ada')
            self.assertTrue(app.calls[0][1]['commit'])
    def test_approval_denial_guard_failure_and_entry_drift_stop_before_search(self):
        for failure in ('approval','guard','drift'):
            app,executor,state,plan=self.setup_flow()
            def confirm(*a,**k):
                if failure=='drift':app.revision='instance-2'
                return ('not_confirmed','declined') if failure=='approval' else ('','')
            state.approvals.confirm_operation=confirm
            app.guard=failure!='guard'
            self.assertFalse(apply_page_filters(executor,state,plan))
            self.assertNotIn('click',[a for a,_ in app.calls])
    def test_range_peer_identity_and_commit_modes_are_bound_before_dispatch(self):
        app,executor,state,plan=self.setup_flow()
        draft=PageFilterDraft('/contacts','form:search','instance-1',(
            PageFilterField('Period','2026-10-01','combobox',name='Start',commit=False),
            PageFilterField('Period','2026-10-31','combobox',name='End',commit=False)),
            'Search','/contacts/query',(('name','Ada'),))
        app.prepare_page_filters=lambda *a,**k:draft
        old_observation=app.observation
        def observation(epoch,**kw):
            obs=old_observation(epoch,**kw)
            attrs={'form_scope':'form:search','form_instance':'instance-1',
                   'aria-haspopup':'dialog','aria-controls':'calendar'}
            return replace(obs,elements=[
                ElementRef('start','combobox','Start','input',epoch,{**attrs,'field_label':'Period'}),
                ElementRef('end','combobox','End','input',epoch,attrs),obs.elements[-1]])
        app.observation=observation;state.obs=observation(1)
        self.assertTrue(apply_page_filters(executor,state,plan))
        self.assertEqual([a for a,_ in app.calls],['fill','fill','click'])
        self.assertFalse(app.calls[0][1]['commit'])
        self.assertFalse(app.calls[1][1]['commit'])
        for mismatch in ('calendar-other',''):
            app.calls.clear()
            state.obs=observation(1)
            state.obs.elements[1].attrs['aria-controls']=mismatch
            self.assertFalse(apply_page_filters(executor,state,plan))
            self.assertEqual(app.calls,[])

    def test_repair_cannot_replace_applied_filter_with_chat_list_or_drop_exclusion(self):
        from web_operation_agent.goal.repair import lost_requirements
        from web_operation_agent.goal.ground import bind
        from web_operation_agent.goal.ports import Candidate
        before=GoalSpec.from_json({'deliverables':[{'kind':'page','entity':'contacts',
            'page':{'conditions':[{'field':'Name','values':['v1'],'op':'!='}]}}]})
        for after in ({'kind':'list','entity':'contacts'},
                      {'kind':'page','page':{'conditions':[{'field':'Name','values':['Ada']}]}}):
            self.assertTrue(lost_requirements(before,GoalSpec.from_json({'deliverables':[after]})))
        bound,_,_=bind(before,(Candidate('record','contacts','Ada',id='v1'),))
        self.assertEqual(bound.deliverables[0].page.conditions,(Filter('Name',('Ada',),'!='),))

    def test_conditions_roundtrip_and_missing_optional_capability_never_dispatch(self):
        spec=GoalSpec.from_json({'deliverables':[{'kind':'page','entity':'contacts','page':{
            'key':'/contacts','conditions':[{'field':'Name','values':['Ada']}]} }]})
        self.assertEqual(GoalSpec.from_json(spec.to_json()),spec)
        asked,left=_ask_what_it_can(spec,frozenset())
        self.assertEqual(asked.deliverables[0].page.conditions,())
        self.assertEqual(left,(('facets',),))
        with self.assertRaises(ValueError):
            GoalSpec.from_json({'deliverables':[{'kind':'page','page':{'conditions':[{}]*7}}]})
