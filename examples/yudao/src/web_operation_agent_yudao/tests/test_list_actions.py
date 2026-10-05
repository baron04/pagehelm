"""Query drafts are distinct from successful actor-bound applied reads."""
from dataclasses import replace
import hashlib
import unittest

from web_operation_agent.execution.action_admission import ActionIntent, ActionAdmissionRefused
from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexPage
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent_yudao.list_actions import NativeListActions, PAGINATION_COMPONENT, _hash
from web_operation_agent_yudao.page_filters import NativePageFilterReview
from . import test_form_entries as entries_fixture


SOURCE = '''<template><el-form :model="queryParams"><el-form-item label="编号" prop="no">
<el-input v-model="queryParams.no" @keyup.enter="searchRows" /></el-form-item>
<el-form-item><el-button @click="searchRows">查找</el-button></el-form-item></el-form>
<Pagination :total="total" v-model:page="queryParams.pageNo" v-model:limit="queryParams.pageSize"
@pagination="loadRows" /></template><script setup>
const loadRows = async () => {loading.value = true
try {const data = await OrdersApi.page(queryParams);list.value = data.list;total.value = data.total}
finally {loading.value = false}}
const searchRows = () => {queryParams.pageNo = 1;loadRows()}
</script>'''


class NativeListActionTests(unittest.TestCase):
    setUp = entries_fixture.EntryTests.setUp

    def integration(self, source=SOURCE):
        component, api = 'ui/src/views/orders/index.vue', 'ui/src/api/orders/index.ts'
        for path, text in ((component, source), (api, 'reviewed GET'),
                           (PAGINATION_COMPONENT, 'reviewed standard paging emitter')):
            file = self.tree.root/path; file.parent.mkdir(parents=True, exist_ok=True); file.write_text(text)
        payload = dict(schema_version=1, release=self.tree.release, entity='orders', route='/orders',
            component=component, endpoint='orders/page', hashes={p:hashlib.sha256(self.tree.read_text(p).encode()).hexdigest()
            for p in (component,api,PAGINATION_COMPONENT)})
        review = NativePageFilterReview.load(payload,self.tree,['/admin-api/orders/page'])
        self.index = ApplicationIndex(entities=(IndexEntity('orders','Orders'),),
                                     pages=(IndexPage('/orders','Orders','Orders',entity='orders'),))
        self.policy = NativeListActions(self.tree,(review,))
        attrs = dict(form_scope='form:query',form_instance='synthetic-instance',page_control='query')
        self.before = PageObservation(1,1,'https://synthetic.test/orders','Orders',[],[
            ElementRef('field','textbox','编号','input',1,{**attrs,'field_label':'编号','query_field':'no'}),
            ElementRef('search','button','查找','button',1,attrs),
            ElementRef('page','button','2','li',1,dict(page_control='pagination',page_number='2',page_size='10'))],
            render_state='ready',page_route='/orders',page_read=dict(path='/admin-api/orders/page',
                query_digest=_hash([]),query_fields={},revision=1,page=1,page_size=10,total=25,rows=10))

    def admit(self, action, args, before=None):
        before = before or self.before
        intent = ActionIntent.create(action,args,before,self.context)
        permit = self.policy.admit(intent,self.index,context=self.context,checkpoint=lambda:None)
        return intent,permit

    def verify(self, pair, after):
        intent,permit = pair
        return self.policy.verify(permit,intent,after,index=self.index,context=self.context,checkpoint=lambda:None)

    def test_query_input_and_search_require_distinct_postconditions(self):
        self.integration()
        pair = self.admit('fill',{'ref':'field','value':'SO-SYNTHETIC'})
        after = replace(self.before,epoch=2,elements=[replace(e,epoch=2,attrs={**e.attrs,
            **({'value':'SO-SYNTHETIC'} if e.ref=='field' else {})}) for e in self.before.elements])
        self.assertTrue(self.verify(pair,after))
        pair = self.admit('click',{'ref':'search'},after)
        self.assertFalse(self.verify(pair,after))
        applied = replace(after,epoch=3,elements=[replace(e,epoch=3) for e in after.elements],page_read={**after.page_read,'revision':2,
            'query_fields':{'no':_hash(['SO-SYNTHETIC'])},'query_digest':_hash([['no','SO-SYNTHETIC']])})
        self.assertTrue(self.verify(pair,applied))
        self.assertFalse(self.verify(pair,replace(applied,page_read={**applied.page_read,'query_fields':{}})))

    def test_paging_preserves_applied_filters_and_requires_a_new_successful_read(self):
        self.integration()
        self.before.page_read.update(query_fields={'status':_hash(['20'])},query_digest=_hash([['status','20']]))
        pair = self.admit('click',{'ref':'page'})
        self.assertFalse(self.verify(pair,self.before))
        after = replace(self.before,epoch=2,elements=[replace(e,epoch=2) for e in self.before.elements],
                        page_read={**self.before.page_read,'revision':2,'page':2})
        self.assertTrue(self.verify(pair,after))
        for proof in (None,{**after.page_read,'page':1},{**after.page_read,'query_fields':{}},
                      {**after.page_read,'revision':1}):
            with self.subTest(proof=proof):self.assertFalse(self.verify(pair,replace(after,page_read=proof)))

    def test_custom_query_handlers_source_drift_and_unindexed_pages_fail_closed(self):
        for changed,action,args in ((SOURCE.replace('queryParams.pageNo = 1;','save();queryParams.pageNo = 1;'),'click',{'ref':'search'}),
                (SOURCE.replace('queryParams.no"','queryParams.no" @input="save"'),'fill',{'ref':'field','value':'synthetic'}),
                (SOURCE.replace('data.list','data.unverified'),'click',{'ref':'search'})):
            self.integration(changed)
            with self.assertRaises(ActionAdmissionRefused):self.admit(action,args)
        self.integration()
        (self.tree.root/'ui/src/views/orders/index.vue').write_text('changed')
        with self.assertRaises(BusinessToolRequestError):self.admit('click',{'ref':'page'})

    def test_unknown_buttons_dialogs_and_out_of_range_paging_are_not_enabled(self):
        self.integration()
        for changed in (replace(self.before,elements=[replace(e,name='提交') if e.ref=='search' else e for e in self.before.elements]),
                        replace(self.before,elements=[replace(e,attrs={**e.attrs,'form_scope':'dialog:business'})
                            if e.ref=='search' else e for e in self.before.elements])):
            with self.assertRaises(ActionAdmissionRefused):self.admit('click',{'ref':'search'},changed)
        changed = replace(self.before,elements=[replace(e,attrs={**e.attrs,'page_number':'99'})
            if e.ref=='page' else e for e in self.before.elements])
        with self.assertRaises(ActionAdmissionRefused):self.admit('click',{'ref':'page'},changed)

    def test_current_actor_catalog_removal_invalidates_an_acknowledged_query(self):
        self.integration()
        pair = self.admit('click',{'ref':'page'})
        after = replace(self.before,epoch=2,elements=[replace(e,epoch=2) for e in self.before.elements],
                        page_read={**self.before.page_read,'revision':2,'page':2})
        self.assertTrue(self.verify(pair,after))
        self.index = replace(self.index,pages=())
        self.assertFalse(self.verify(pair,after))

    def test_identically_named_unbound_form_controls_are_not_query_controls(self):
        self.integration()
        for action,args,ref in (('fill',{'ref':'field','value':'synthetic'},'field'),
                               ('click',{'ref':'search'},'search')):
            changed = replace(self.before,elements=[replace(e,attrs={k:v for k,v in e.attrs.items()
                if k!='page_control'}) if e.ref==ref else e for e in self.before.elements])
            with self.subTest(action=action),self.assertRaises(ActionAdmissionRefused):self.admit(action,args,changed)
        changed = replace(self.before,elements=[replace(e,attrs={**e.attrs,'query_field':'another'})
            if e.ref=='field' else e for e in self.before.elements])
        with self.assertRaises(ActionAdmissionRefused):self.admit('fill',{'ref':'field','value':'synthetic'},changed)
