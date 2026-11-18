"""Explicit deployment closure overrides all page grants and the offered contract."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
import json
import unittest
from unittest.mock import patch
from web_operation_agent.execution.action_admission import ActionAdmissionRefused
from web_operation_agent_yudao.executor.model_page_policy import NativePageReview
from web_operation_agent_yudao.host.native_server import YudaoHostConfig
from web_operation_agent_yudao.executor.slim_loop import TOOLS, scoped_page_tools, page_scope_notice, _requirements
from . import test_model_page_policy as policy_fixture

class NativeScopeTests(unittest.TestCase):
    def test_disabled_handlers_override_menu_and_exact_grants_and_capabilities(self):
        explicit=policy_fixture.review_document()['handlers']
        original=NativePageReview.load(policy_fixture.tiered_document(handlers=explicit),'synthetic-v2')
        closed=replace(original,disabled_handlers=frozenset({'query','paging','fields','lines'}))
        for route in ('/crm/customer','/system/user'):
            for family in closed.disabled_handlers:
                self.assertFalse(closed.permits(family,route))
        self.assertTrue(closed.permits('navigation','/crm/customer'))
        self.assertTrue(closed.permits('tabs','/system/user'))
        self.assertTrue(closed.permits('form_entry','/crm/customer','新增'))
        self.assertFalse(closed.capabilities&{'facets','paging','fill','lines'})
        self.assertNotEqual(closed.digest,original.digest)
        self.assertTrue(original.permits('query','/crm/customer'))

    def test_explicit_deployment_entries_cannot_restore_disabled_query_or_paging(self):
        from .test_model_page_policy import deployment_review
        closed=replace(deployment_review(),disabled_handlers=frozenset({'query','paging'}))
        entries=[(kind,route) for kind,route,*_ in closed.scopes if kind in closed.disabled_handlers]
        self.assertTrue(entries)
        for kind,route in entries:self.assertFalse(closed.permits(kind,route.replace('{id}','1')))
        self.assertFalse({'facets','paging'}&closed.capabilities)
        self.assertIn('fill',closed.capabilities)

    def test_actual_query_admission_refuses_before_any_native_read(self):
        fixture=policy_fixture.QueryTests();fixture.setUp()
        fixture.policy.page_review=replace(fixture.policy.page_review,disabled_handlers=frozenset({'query'}))
        before=fixture.page(path='/crm/customer',elements=(fixture.input(1),))
        with self.assertRaises(ActionAdmissionRefused):
            fixture.runtime.prepare('fill',{'ref':'e1','value':'any'},before,context=fixture.context,checkpoint=lambda:None)

    def test_tool_contract_omits_disabled_options_without_mutating_default_tools(self):
        original=json.dumps(TOOLS,ensure_ascii=False)
        review=NS(disabled_handlers=frozenset({'query','lines'}))
        offered={s['function']['name']:s for s in scoped_page_tools(review)}
        fields=offered['fill_fields']['function']['parameters']['properties']
        self.assertNotIn('then_click',fields);self.assertNotIn('add_lines',fields)
        self.assertIn('then_highlight',fields);self.assertIn('call_api',offered)
        self.assertIn('页面筛选',page_scope_notice(review))
        closed=NS(disabled_handlers=frozenset({'query','paging','fields','lines'}))
        names={s['function']['name'] for s in scoped_page_tools(closed)}
        self.assertFalse(names&{'input','select','fill_fields'})
        self.assertTrue({'goto','call_api','find_record','highlight'}<=names)
        self.assertFalse(_requirements('goto',{'open':'新增'},None,closed))
        self.assertEqual(json.dumps(TOOLS,ensure_ascii=False),original)

    def test_configuration_is_immutable_and_cli_forwards_repeated_closures(self):
        base=dict(api_base='http://synthetic.invalid',public_origin='http://synthetic.invalid',state_dir=Path('/synthetic/unused'))
        for invalid in (['query'],frozenset({'navigation'}),frozenset({'typo'})):
            with self.assertRaises(ValueError):YudaoHostConfig(**base,native_page_disabled_handlers=invalid)
        from web_operation_agent_yudao.host import native_server as native
        class Composed(Exception):pass
        captured=[]
        def compose(config):captured.append(config);raise Composed()
        with patch.dict(native.os.environ,{'AGENT_OPENAI_API_KEY':'synthetic-unused','AGENT_OPENAI_MODEL':'scripted/no-model'},clear=True),\
             patch('web_operation_agent_yudao.host.deployment_validation.validate_native_deployment'),\
             patch('web_operation_agent.modeling.spend.price_for',return_value={}),\
             patch.object(native,'YudaoHost',side_effect=compose):
            with self.assertRaises(Composed):native.main(['--api-base',base['api_base'],'--public-origin',base['public_origin'],
                '--state-dir',str(base['state_dir']),'--native-page-disable','query','--native-page-disable','paging',
                '--no-native-edit-prefill'])
        self.assertEqual(captured[0].native_page_disabled_handlers,frozenset({'query','paging'}))
        self.assertFalse(captured[0].native_edit_prefill)

class EditModeClosureTests(unittest.TestCase):
    def test_create_must_have_consistent_fresh_mode_and_edit_entry_stays_available(self):
        from web_operation_agent.business.draft_steps import DraftStepAdmission
        from web_operation_agent_yudao.executor.model_page_policy import NativeDraftSteps
        from web_operation_agent.schemas import ElementRef,ActionReceipt
        from web_operation_agent.transport.protocol import _remote_args_echo
        fixture=policy_fixture.QueryTests();fixture.setUp()
        review=replace(fixture.policy.page_review,allow_edit_prefill=False)
        forms=NS(_evidence=NS(mark=lambda:0,since=lambda mark:()))
        port=NativeDraftSteps(fixture.policy.data,forms,review)
        base={'form_scope':'dialog:editor','form_instance':'live','title':'明细第1行·数量','value':'1'}
        def field(extra):return ElementRef('e1','spinbutton','数量','input',1,{**base,**extra})
        for mode in ({},{'form_mode':'update','form_record_id':'7'},{'form_mode':'create','form_record_id':'7'}):
            before=fixture.page(path='/crm/customer',elements=(field(mode),))
            with self.assertRaises(ActionAdmissionRefused):
                DraftStepAdmission(port,'yudao').prepare('fill',{'ref':'e1','value':'2'},before,
                    context=fixture.context,checkpoint=lambda:None)
        shown=field({'form_mode':'create','form_record_id':''})
        before=fixture.page(path='/crm/customer',elements=(shown,))
        admission=DraftStepAdmission(port,'yudao');args={'ref':'e1','value':'2'}
        intent,permit=admission.prepare('fill',args,before,context=fixture.context,checkpoint=lambda:None)
        admission.consume(intent,permit,before,context=fixture.context,checkpoint=lambda:None)
        after=replace(before,epoch=2,elements=[replace(shown,epoch=2,attrs={**shown.attrs,'value':'2'})])
        receipt=ActionReceipt(1,'draft','fill',_remote_args_echo({**args,'epoch':1}),True,observation=after,
            url_before=before.url,url_after=after.url,write_guard_held=True)
        admission.verify(intent,permit,receipt,context=fixture.context,checkpoint=lambda:None)
        changed=replace(after,elements=[replace(after.elements[0],attrs={**shown.attrs,'value':'2','form_mode':'update','form_record_id':'7'})])
        with self.assertRaises(ActionAdmissionRefused):
            admission.verify(intent,permit,replace(receipt,observation=changed),context=fixture.context,checkpoint=lambda:None)
        opener=ElementRef('e1','button','编辑','button',1,{})
        entry=fixture.page(path='/crm/customer',elements=(opener,))
        _,permit=DraftStepAdmission(port,'yudao').prepare('click',{'ref':'e1'},entry,context=fixture.context,checkpoint=lambda:None)
        self.assertEqual(permit.step,'open_entry')

    def test_edit_mode_closure_changes_review_digest_and_model_notice(self):
        review=NativePageReview.load(policy_fixture.review_document(),'synthetic-v1')
        closed=replace(review,allow_edit_prefill=False)
        self.assertNotEqual(review.digest,closed.digest)
        self.assertIn('fill',closed.capabilities)  # reviewed create forms remain offered
        self.assertIn('编辑或模式未知',page_scope_notice(closed))
        with self.assertRaises(ValueError):replace(closed,allow_edit_prefill='false')
