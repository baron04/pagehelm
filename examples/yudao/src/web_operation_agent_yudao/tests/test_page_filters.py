"""Query source is evidence; actor index, review and live form remain mandatory."""
from dataclasses import replace
from datetime import date
import hashlib
from types import SimpleNamespace
import unittest

from web_operation_agent.business.errors import BusinessToolRequestError,BusinessToolIdentityError
from web_operation_agent.goal.index import ApplicationIndex,IndexEntity,IndexField,IndexPage
from web_operation_agent.goal.ports import PagePlan
from web_operation_agent.goal.spec import Filter
from web_operation_agent.schemas import ElementRef
from web_operation_agent_yudao.page_filters import NativePageFilterReview,NativePageFilters
from web_operation_agent_yudao.tests.test_form_entries import EntryTests


class NativePageFilterTests(unittest.TestCase):
    setUp=EntryTests.setUp
    def query(self):
        component='ui/src/views/erp/sale/order/index.vue';api='ui/src/api/erp/sale/order/index.ts'
        source='<template><el-form :model="queryParams"><el-form-item label="订单时间" prop="orderTime">' \
            '<el-date-picker v-model="queryParams.orderTime" type="daterange" value-format="YYYY-MM-DD HH:mm:ss" ' \
            'start-placeholder="开始日期" end-placeholder="结束日期" /></el-form-item></el-form></template>'
        for path,text in ((component,source),(api,'reviewed synthetic GET')):
            p=self.tree.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        payload={'schema_version':1,'release':self.tree.release,'entity':'orders','route':'/orders',
            'component':component,'endpoint':'orders/page','hashes':{p:hashlib.sha256(self.tree.read_text(p).encode()).hexdigest() for p in (component,api)}}
        review=NativePageFilterReview.load(payload,self.tree,['/admin-api/orders/page'])
        index=ApplicationIndex(entities=(IndexEntity('orders','Orders',fields=(IndexField('orderTime','订单时间','date'),)),),
            pages=(IndexPage('/orders','Orders','Orders',entity='orders'),))
        data=SimpleNamespace(_client=self.client,index=lambda **k:index)
        port=NativePageFilters(data,self.tree,(review,),date(2026,10,3))
        attrs={'form_scope':'form:query','form_instance':'instance'}
        obs=replace(self.before,url='https://synthetic.test/orders',record_route='',page_route='/orders',
            elements=[ElementRef('q','button','搜索','button',1,attrs)])
        plan=PagePlan('/orders','Orders',expected_path='/orders',conditions=(Filter('订单时间',('2026-10',)),))
        return port,plan,obs,payload
    def test_calendar_endpoints_bound_source_actor_and_current_query_form(self):
        port,plan,obs,payload=self.query()
        draft=port.prepare(plan,obs,context=self.context)
        self.assertEqual([f.value for f in draft.fields],['2026-10-01','2026-10-31'])
        self.assertEqual([f.commit for f in draft.fields],[False,False])
        self.assertEqual(dict(draft.parameters),{'orderTimeStart':'2026-10-01 00:00:00','orderTimeEnd':'2026-10-31 23:59:59'})
        for changed in (replace(obs,page_route=''),replace(obs,render_state='pending'),replace(obs,elements=[])):
            with self.assertRaises(BusinessToolRequestError):port.prepare(plan,changed,context=self.context)
        self.client.actor=3
        with self.assertRaises(BusinessToolIdentityError):port.prepare(plan,obs,context=self.context)
        self.client.actor=2
        (self.tree.root/payload['component']).write_text('<template>changed</template>')
        with self.assertRaises(BusinessToolRequestError):port.prepare(plan,obs,context=self.context)
    def test_source_alone_unknown_fields_and_unbounded_ranges_cannot_issue(self):
        port,plan,obs,payload=self.query()
        with self.assertRaises(ValueError):NativePageFilterReview.load(payload,self.tree,[])
        for condition in (Filter('Missing',('x',)),Filter('订单时间',()),Filter('订单时间',('overdue',)),Filter('订单时间',('2026-10',),'!=')):
            with self.assertRaises(BusinessToolRequestError):
                port.prepare(replace(plan,conditions=(condition,)),obs,context=self.context)
