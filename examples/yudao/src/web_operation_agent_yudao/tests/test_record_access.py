"""CRM edit facts use exact application permission rows, never the DOM."""
import unittest
from web_operation_agent.business.errors import BusinessToolContractError
from web_operation_agent_yudao.record_access import contact_can_edit, customer_can_edit


class CrmRecordAccessTests(unittest.TestCase):
    def test_exact_record_actor_level_and_global_permission_are_all_required(self):
        identity = {"permissions":["crm:customer:update"]}
        row = {"bizType":2,"bizId":7,"userId":1,"level":1}
        self.assertTrue(customer_can_edit(identity,[row],"7","1"))
        self.assertTrue(customer_can_edit({"permissions":["*:*:*"]},[{**row,"level":3}],"7","1"))
        for change in ({"bizType":1},{"bizType":True},{"bizId":8},{"userId":2},
                       {"level":2},{"level":True}):
            with self.subTest(change=change):
                self.assertFalse(customer_can_edit(identity,[{**row,**change}],"7","1"))
        self.assertFalse(customer_can_edit({"permissions":[]},[row],"7","1"))
        self.assertFalse(customer_can_edit(identity,[],"7","1"))
        contact = {"bizType":3,"bizId":8,"userId":1,"level":3}
        self.assertTrue(contact_can_edit({"permissions":["crm:contact:update"]},[contact],"8","1"))
        self.assertFalse(contact_can_edit(identity,[contact],"8","1"))
        self.assertFalse(contact_can_edit({"permissions":["crm:contact:update"]},[row],"8","1"))

    def test_unavailable_or_malformed_permissions_are_not_a_denial_fact(self):
        for identity, team in ((None,[]),({},[]),({"permissions":"*:*:*"},[]),
                               ({"permissions":[]},{}),({"permissions":[]},[None])):
            with self.subTest(identity=identity,team=team), self.assertRaises(BusinessToolContractError):
                customer_can_edit(identity,team,"7","1")

    def test_navigation_feedback_reads_live_permission_under_exact_actor(self):
        import time
        from types import SimpleNamespace
        from unittest.mock import patch
        from web_operation_agent.state.identity import OperationContext, PrincipalContext
        from web_operation_agent.business.errors import BusinessToolIdentityError, BusinessToolUnavailableError
        from web_operation_agent_yudao.client import YudaoClient
        from web_operation_agent_yudao.bound_data_reads import turn_actor_reads
        from web_operation_agent_yudao.slim_loop import YudaoLoopProcedures, _CURRENT_REQUEST, _WRITE_INTENT
        from web_operation_agent_yudao.write_intent import classify_write_intent
        client = YudaoClient.for_token('http://synthetic.invalid', 'synthetic-token', '1')
        now = int(time.time()*1000)
        principal = PrincipalContext(1,'yudao','1','10','1','delegation',now-100,now+60000,('yudao.read',))
        operation = OperationContext.create(principal,session_id='s',turn_id='t',operation_id='access',deadline_ms=now+30000)
        context = SimpleNamespace(operation=operation,page=SimpleNamespace(checkpoint=lambda:None))
        owner = YudaoLoopProcedures(SimpleNamespace(client=client),SimpleNamespace(data=SimpleNamespace(_client=client)),'http://synthetic.invalid')
        row = {'bizType':2,'bizId':7,'userId':2,'level':3}
        identity = {'user':{'id':1,'deptId':10},'permissions':['crm:customer:update']}
        requests=[]
        def request(method,path,**kwargs):
            requests.append((method,path,kwargs.get('params')))
            return 0,{'code':0,'data':identity if path=='system/auth/get-permission-info' else [row]}
        with patch.object(client,'_request',side_effect=request),turn_actor_reads(client):
            denied = owner._record_access(context,'/crm/customer/detail/7')
            self.assertIn('没有此客户记录本身的编辑权限',denied)
            self.assertIn('不代表其联系人不能修改',denied)
            self.assertIn(('GET','crm/permission/list',{'bizType':2,'bizId':'7'}),requests)
            row['userId']=1
            self.assertIn('有此客户记录本身的编辑资格',owner._record_access(context,'/crm/customer/detail/7'))
            count = len(requests)
            token = _CURRENT_REQUEST.set('把晶华电子的联系人手机号换成新的')
            try:
                self.assertEqual('',owner._record_access(context,'/crm/customer/detail/7'))
                self.assertEqual(count,len(requests))
            finally:
                _CURRENT_REQUEST.reset(token)
            row.update({'bizType':3,'bizId':8})
            identity['permissions']=['crm:contact:update']
            count = len(requests)
            self.assertEqual('',owner._record_access(context,'/crm/contact/detail/8'))
            self.assertEqual(count,len(requests))
            token = _WRITE_INTENT.set(classify_write_intent('把联系人手机换成新的'))
            try:
                self.assertIn('有此联系人记录本身的编辑资格',
                              owner._record_access(context,'/crm/contact/detail/8'))
                self.assertIn(('GET','crm/permission/list',{'bizType':3,'bizId':'8'}),requests)
                row['userId']=2
                self.assertIn('没有此联系人记录本身的编辑权限',
                              owner._record_access(context,'/crm/contact/detail/8'))
            finally:
                _WRITE_INTENT.reset(token)
            identity['permissions']=None
            with self.assertRaises(BusinessToolUnavailableError):
                owner._record_access(context,'/crm/customer/detail/7')
            identity['user']['id']=2
            with self.assertRaises(BusinessToolIdentityError):
                owner._record_access(context,'/crm/customer/detail/7')
        # No new application API lookup for a different application page family.
        self.assertEqual('',owner._record_access(context,'/hrm/employee/detail/7'))
