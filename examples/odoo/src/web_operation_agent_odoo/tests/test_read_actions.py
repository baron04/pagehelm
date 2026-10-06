"""Standard effect admission and postcondition failures, independent of labels."""
import copy
import time
import unittest

from web_operation_agent.execution.action_admission import (
    ActionEffect, ActionIntent, ActionAdmissionRefused, ActionResultPending,
)
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent.state.identity import OperationContext, PrincipalContext, IdentityAssertion
from web_operation_agent_odoo.read_actions import OdooReadActions
from .test_generic_reads import _catalog


def context(actor='7'):
    now = int(time.time()*1000)
    p=PrincipalContext(schema_version=1,application_id='odoo',tenant_id='fixture',workspace_id='2',
        actor_id=actor,delegation_id='d',issued_at=now-1000,expires_at=now+60000,
        scopes=('generic_records:read',))
    return OperationContext.create(p,session_id='s',turn_id='t',operation_id='a',deadline_ms=now+30000)


class Client:
    base_url='http://odoo.test'
    def __init__(self): self.payload=_catalog();self.actor='7'
    def resolve_delegated_identity(self):
        p=context(self.actor).principal
        return IdentityAssertion(schema_version=1,source='delegated_api',application_id=p.application_id,
            tenant_id=p.tenant_id,workspace_id=p.workspace_id,actor_id=p.actor_id,
            issued_at=p.issued_at,expires_at=p.expires_at)
    def call(self, model, method, args, kwargs=None):
        if method=='catalog':return copy.deepcopy(self.payload)
        if method=='get_view':return {'arch': (
            '<search><field name="name"/><filter name="active" string="Active" domain="[]"/></search>'
            if kwargs['view_type']=='search' else '<form><notebook><page string="Details"/></notebook></form>')}
        raise AssertionError(method)


def observation(page=1, revision=1, path='/odoo/inventory/transfers', elements=()):
    return PageObservation(schema_version=1, epoch=revision, url='http://odoo.test'+path,
        title='Transfers',render_state='ready',landmarks=[],elements=list(elements),page_route=path,active_view='List',
        page_read={'path':path,'query_digest':'a'*64,'query_fields':{},'revision':revision,
                   'page':page,'page_size':20,'total':40,'rows':20})


class StandardReadActions(unittest.TestCase):
    def setUp(self):
        self.client=Client();self.ctx=context()
        self.policy=OdooReadActions(self.client,review='standard-framework-handlers-fixture-v1',
                                   module_digest=self.client.payload['module_digest'])
    def intent(self, name, args, obs):return ActionIntent.create(name,args,obs,self.ctx)
    def test_actor_window_route_is_admitted_without_a_named_button_review(self):
        obs=observation()
        intent=self.intent('goto',{'url':'/odoo/inventory/transfers/7'},obs)
        permit=self.policy.admit(intent,context=self.ctx,checkpoint=lambda:None)
        self.assertEqual(permit.effect,ActionEffect.NAVIGATION)
        self.assertTrue(self.policy.verify(permit,intent,None,
            observation=observation(path='/odoo/inventory/transfers/7'),context=self.ctx,checkpoint=lambda:None))
    def test_unknown_and_transient_routes_are_refused(self):
        for path in ['/odoo/inventory/backorders','/odoo/settings/users','/odoo/inventory/transfers/new',
                     '/odoo/unknown','https://other.test/odoo/inventory/transfers',
                     '/odoo/inventory/transfers?view_type']:
            with self.subTest(path=path),self.assertRaises(ActionAdmissionRefused):
                self.policy.admit(self.intent('goto',{'url':path},observation()),context=self.ctx,checkpoint=lambda:None)
    def test_paging_needs_fresh_same_query_same_size_and_actual_page(self):
        el=ElementRef('e1','button','any language','button',1,
            {'page_control':'pagination','page_number':'2','page_size':'20'})
        obs=observation(elements=(el,));intent=self.intent('click',{'ref':'e1'},obs)
        permit=self.policy.admit(intent,context=self.ctx,checkpoint=lambda:None)
        with self.assertRaises(ActionResultPending):
            self.policy.verify(permit,intent,None,observation=obs,context=self.ctx,checkpoint=lambda:None)
        after=observation(page=2,revision=2)
        self.assertTrue(self.policy.verify(permit,intent,None,observation=after,context=self.ctx,checkpoint=lambda:None))
        after.page_read['query_digest']='b'*64
        self.assertFalse(self.policy.verify(permit,intent,None,observation=after,context=self.ctx,checkpoint=lambda:None))
    def test_a_button_called_next_does_not_grant_paging(self):
        el=ElementRef('e1','button','Next','button',1,{})
        with self.assertRaises(ActionAdmissionRefused):
            self.policy.admit(self.intent('click',{'ref':'e1'},observation(elements=(el,))),
                              context=self.ctx,checkpoint=lambda:None)
    def test_notebook_tab_requires_actor_view_and_selected_same_record(self):
        path='/odoo/inventory/transfers/7'
        tab=ElementRef('e1','tab','Details','a',1,{})
        before=observation(path=path,elements=(tab,))
        before.active_view='Form';before.page_read=None;before.record_route=path
        intent=self.intent('click',{'ref':'e1'},before)
        permit=self.policy.admit(intent,context=self.ctx,checkpoint=lambda:None)
        self.assertEqual(permit.effect,ActionEffect.READ_ONLY)
        after=observation(path=path,revision=2,elements=(
            ElementRef('e1','tab','Details','a',2,{'selected':'true'}),))
        after.active_view='Form';after.page_read=None;after.record_route=path
        self.assertTrue(self.policy.verify(permit,intent,None,observation=after,
                         context=self.ctx,checkpoint=lambda:None))
        after.record_route='/odoo/inventory/transfers/8'
        self.assertFalse(self.policy.verify(permit,intent,None,observation=after,
                          context=self.ctx,checkpoint=lambda:None))
        for name in ('Unreviewed', 'Details'):
            before.elements=[ElementRef('e1','tab',name,'a',1,{})]
            if name=='Details':before.elements.append(ElementRef('e2','tab',name,'a',1,{}))
            with self.subTest(name=name),self.assertRaises(ActionAdmissionRefused):
                self.policy.admit(self.intent('click',{'ref':'e1'},before),
                                  context=self.ctx,checkpoint=lambda:None)
    def test_module_review_drift_is_refused(self):
        self.client.payload['module_digest']='sha256:'+'9'*64
        with self.assertRaises(ActionAdmissionRefused):
            self.policy.admit(self.intent('goto',{'url':'/odoo/inventory/transfers'},observation()),
                              context=self.ctx,checkpoint=lambda:None)
    def test_delegated_actor_drift_is_refused(self):
        from web_operation_agent.business.errors import BusinessToolIdentityError
        self.client.actor='8'
        with self.assertRaises(BusinessToolIdentityError):
            self.policy.admit(self.intent('goto',{'url':'/odoo/inventory/transfers'},observation()),
                              context=self.ctx,checkpoint=lambda:None)
    def test_departed_or_not_rendered_page_cannot_verify_navigation(self):
        obs=observation();intent=self.intent('goto',{'url':'/odoo/inventory/transfers/7'},obs)
        permit=self.policy.admit(intent,context=self.ctx,checkpoint=lambda:None)
        with self.assertRaises(ActionResultPending):
            self.policy.verify(permit,intent,None,observation=obs,context=self.ctx,checkpoint=lambda:None)
        after=observation(path='/odoo/inventory/transfers/7');after.page_route=None
        with self.assertRaises(ActionResultPending):
            self.policy.verify(permit,intent,None,observation=after,context=self.ctx,checkpoint=lambda:None)

    def test_search_text_is_local_input_not_an_applied_search(self):
        el=ElementRef('e1','textbox','Search','input',1,{'page_control':'query','query_field':'search_name'})
        before=observation(elements=(el,))
        intent=self.intent('fill',{'ref':'e1','value':'Blue'},before)
        permit=self.policy.admit(intent,context=self.ctx,checkpoint=lambda:None)
        self.assertEqual(permit.effect,ActionEffect.READ_ONLY)
        after=observation(revision=2,elements=(ElementRef('e1','textbox','Search','input',2,
            {**el.attrs,'value':'Blue'}),))
        self.assertTrue(self.policy.verify(permit,intent,None,observation=after,context=self.ctx,checkpoint=lambda:None))
        self.assertNotIn('search_name',after.page_read['query_fields'])
        after.page_read['path']='/odoo/other'
        self.assertFalse(self.policy.verify(permit,intent,None,observation=after,context=self.ctx,checkpoint=lambda:None))
        before.page_read['path']='/odoo/other'
        with self.assertRaises(ActionAdmissionRefused):
            self.policy.admit(self.intent('fill',{'ref':'e1','value':'Blue'},before),
                              context=self.ctx,checkpoint=lambda:None)
        before.page_read['path']=before.page_route
        for attrs in ({**el.attrs,'form_scope':'dialog:editor'}, {**el.attrs,'query_field':'search_unread'}):
            before.elements=[ElementRef('e1','textbox','Search','input',1,attrs)]
            with self.assertRaises(ActionAdmissionRefused):
                self.policy.admit(self.intent('fill',{'ref':'e1','value':'Blue'},before),context=self.ctx,checkpoint=lambda:None)

    def test_search_choice_requires_fresh_exact_applied_query_and_preserves_prior_conditions(self):
        elements=(ElementRef('e1','textbox','Search','input',1,
            {'page_control':'query','query_field':'search_name','value':'Blue'}),
            ElementRef('e2','link','Search Name for Blue','a',1,
            {'page_control':'query','query_field':'search_name'}))
        before=observation(elements=elements);before.page_read['query_fields']={'filter_active':self.policy._value_digest('false')}
        intent=self.intent('click',{'ref':'e2'},before)
        permit=self.policy.admit(intent,context=self.ctx,checkpoint=lambda:None)
        with self.assertRaises(ActionResultPending):
            self.policy.verify(permit,intent,None,observation=before,context=self.ctx,checkpoint=lambda:None)
        after=observation(revision=2)
        after.page_read['query_fields']={'search_name':self.policy._value_digest('Blue'),'filter_active':self.policy._value_digest('false')}
        self.assertTrue(self.policy.verify(permit,intent,None,observation=after,context=self.ctx,checkpoint=lambda:None))
        for fields in ({'search_name':self.policy._value_digest('Red'),'filter_active':self.policy._value_digest('false')}, {'search_name':self.policy._value_digest('Blue'),'filter_active':self.policy._value_digest('true')}):
            after.page_read['query_fields']=fields
            self.assertFalse(self.policy.verify(permit,intent,None,observation=after,context=self.ctx,checkpoint=lambda:None))

    def test_filter_and_menu_have_distinct_verified_postconditions(self):
        toggle=ElementRef('e1','button','Toggle Search Panel','button',1,
            {'page_control':'query','query_field':'search_menu','aria-expanded':'false'})
        before=observation(elements=(toggle,))
        intent=self.intent('click',{'ref':'e1'},before)
        permit=self.policy.admit(intent,context=self.ctx,checkpoint=lambda:None)
        after=observation(revision=2,elements=(ElementRef('e1','button',toggle.name,'button',2,
            {**toggle.attrs,'aria-expanded':'true'}),))
        self.assertTrue(self.policy.verify(permit,intent,None,observation=after,context=self.ctx,checkpoint=lambda:None))
        filter=ElementRef('e2','menuitemcheckbox','Active','span',1,
            {'page_control':'query','query_field':'filter_active'})
        before.elements=[filter];before.page_read['query_fields']={'filter_active':self.policy._value_digest('false')}
        intent=self.intent('click',{'ref':'e2'},before)
        permit=self.policy.admit(intent,context=self.ctx,checkpoint=lambda:None)
        after=observation(revision=2);after.page_read['query_fields']={'filter_active':self.policy._value_digest('true')}
        self.assertTrue(self.policy.verify(permit,intent,None,observation=after,context=self.ctx,checkpoint=lambda:None))
        after.page_read['query_fields']={}
        self.assertFalse(self.policy.verify(permit,intent,None,observation=after,context=self.ctx,checkpoint=lambda:None))
        before.elements=[ElementRef('e2','button','Add Custom Filter','button',1,filter.attrs)]
        with self.assertRaises(ActionAdmissionRefused):
            self.policy.admit(self.intent('click',{'ref':'e2'},before),context=self.ctx,checkpoint=lambda:None)
