"""The isolated profile omits handler review while retaining real fence and bindings."""
from dataclasses import replace
import unittest

from web_operation_agent.business.errors import BusinessToolIdentityError
from web_operation_agent.execution.action_admission import ActionAdmission, ActionAdmissionRefused
from web_operation_agent.schemas import ActionReceipt, ElementRef
from web_operation_agent.transport.protocol import _remote_args_echo
from web_operation_agent_yudao.host.guard_proxy import NativeGuardProxy
from web_operation_agent_yudao.host.page_guard import GuardRefused, NativePageGuard
from web_operation_agent_yudao.executor.model_page_policy import NativePageReview
from web_operation_agent_yudao.executor.read_only_window_actions import (
    ReadOnlyWindowPageActions, ReadOnlyWindowDraftSteps, ReadOnlyWindowPageReview)
from . import test_read_actions as read_fixture


class ReadOnlyWindowActionTests(unittest.TestCase):
    page = read_fixture.NativeReadActionTests.page

    def settle(self, before, after, action="click", args=None):
        args = args or {"ref":"e1"}
        intent,permit=self.runtime.prepare(action,args,before,context=self.context,checkpoint=lambda:None)
        self.runtime.consume(intent,permit,before,context=self.context,checkpoint=lambda:None)
        echo=_remote_args_echo({**args,**({"epoch":before.epoch} if "ref" in args else {})})
        receipt=ActionReceipt(1,"command",action,echo,True,url_before=before.url,url_after=after.url,
                              observation=after,write_guard_held=True)
        self.runtime.verify(intent,permit,receipt,context=self.context,checkpoint=lambda:None)

    def setUp(self):
        read_fixture.NativeReadActionTests.setUp(self)
        self.guard = NativePageGuard(frozenset(), writing_gets=frozenset({"/admin-api/records/write-get"}))
        self.proxy = NativeGuardProxy(upstream="http://127.0.0.1:9", origin="https://fixture.test",
            guard=self.guard, resolve_actor=lambda _token, _tenant: "a" * 64).start()
        self.addCleanup(self.proxy.close)
        self.proxy.ingress_base = self.proxy.url
        self.proxy.verify_ingress()
        self.guard.begin("a" * 64, "b" * 64)
        review = NativePageReview("synthetic", "synthetic-reviewed-profile", ())
        self.policy = ReadOnlyWindowPageActions("https://fixture.test", self.policy.data,
            {}, None, review, guard_proxy=self.proxy)
        self.runtime = ActionAdmission(self.policy, "yudao")

    def test_unknown_routes_handlers_and_query_controls_do_not_require_a_catalog(self):
        for action, element, args in (
                ("click", ElementRef("e1", "tab", "Unregistered tab", "div", 1, {}), {"ref":"e1"}),
                ("fill", ElementRef("e1", "textbox", "", "input", 1, {"form_scope":"form:query"}),
                 {"ref":"e1", "value":"arbitrary query"}),
                ("select", ElementRef("e1", "combobox", "Unknown query", "input", 1, {}),
                 {"ref":"e1", "value":"New choice"})):
            with self.subTest(action=action):
                self.runtime = ActionAdmission(self.policy, "yudao")
                self.settle(replace(self.page(path="/unregistered", elements=(element,)), page_route=""),
                    replace(self.page(2,path="/unregistered"),page_route=""), action, args)
        self.runtime = ActionAdmission(self.policy, "yudao")
        self.settle(self.page(), replace(self.page(2,path="/unregistered/detail/17"),page_route=""),
                    "goto", {"url":"/unregistered/detail/17"})

    def test_an_unstarted_or_unverified_guard_cannot_enable_the_profile(self):
        proxy = NativeGuardProxy(upstream="http://127.0.0.1:9", origin="https://fixture.test",
            guard=self.guard, resolve_actor=lambda *_: "a" * 64)
        self.addCleanup(proxy.close)
        with self.assertRaises(ValueError):
            ReadOnlyWindowPageActions("https://fixture.test", self.policy.data, {}, None, None, guard_proxy=proxy)
        self.assertFalse(proxy.method_fence_ready)
        proxy.start()
        with self.assertRaises(ValueError):
            ReadOnlyWindowPageActions("https://fixture.test", self.policy.data, {}, None, None, guard_proxy=proxy)
        with self.assertRaises(ValueError):
            ReadOnlyWindowPageActions("https://fixture.test", self.policy.data, {}, None, None, guard_proxy=None)

    def test_ended_window_is_not_a_verified_result_and_closed_proxy_cannot_admit(self):
        intents=[]
        for action,args,role in (("goto",{"url":"/unregistered"},"button"),
                ("click",{"ref":"e1"},"button"),
                ("fill",{"ref":"e1","value":"query"},"textbox"),
                ("select",{"ref":"e1","value":"choice"},"combobox")):
            with self.subTest(action=action):
                self.runtime=ActionAdmission(self.policy,"yudao")
                before=self.page(elements=(ElementRef("e1",role,"Query","input",1,{}),))
                intent,permit=self.runtime.prepare(action,args,before,
                    context=self.context,checkpoint=lambda:None)
                intents.append(intent)
                self.runtime.consume(intent,permit,before,context=self.context,checkpoint=lambda:None)
                after=self.page(2,path="/unregistered" if action=="goto" else "/crm/customer")
                echo=_remote_args_echo({**args,**({"epoch":before.epoch} if "ref" in args else {})})
                receipt=ActionReceipt(1,"command",action,echo,True,
                    url_before=before.url,url_after=after.url,observation=after,write_guard_held=True)
                self.guard.end("a"*64,"b"*64)
                with self.assertRaises(ActionAdmissionRefused):
                    self.runtime.verify(intent,permit,receipt,context=self.context,checkpoint=lambda:None)
                self.guard.begin("a"*64,"b"*64)
        self.proxy.close()
        for intent in intents:
            with self.subTest(closed_proxy_action=intent.action),self.assertRaises(ActionAdmissionRefused):
                self.policy.admit(intent,context=self.context,checkpoint=lambda:None)

    def test_write_controls_remain_closed_and_writes_are_blocked_by_the_real_fence(self):
        for name,role in (("保存","button"),("删除","button"),("active","switch")):
            element=ElementRef("e1",role,name,"button",1,{})
            with self.assertRaisesRegex(ActionAdmissionRefused,"unsafe_effect"):
                self.runtime.prepare("click",{"ref":"e1"},self.page(elements=(element,)),
                    context=self.context,checkpoint=lambda:None)
        for method,target in (("POST","/admin-api/records/create"),("GET","/admin-api/records/write-get")):
            with self.assertRaises(GuardRefused):
                with self.guard.request("a"*64,method,target):
                    self.fail("the isolated window forwarded a mutation")

    def test_actor_freshness_origin_and_exact_node_are_still_required(self):
        self.user={"id":2,"deptId":10}
        with self.assertRaises(BusinessToolIdentityError):
            self.runtime.prepare("goto",{"url":"/unregistered"},self.page(),
                context=self.context,checkpoint=lambda:None)
        self.user={"id":1,"deptId":10}
        with self.assertRaises(ActionAdmissionRefused):
            self.runtime.prepare("goto",{"url":"https://foreign.test/page"},self.page(),
                context=self.context,checkpoint=lambda:None)
        element=ElementRef("e1","button","Search","button",1,{})
        before=self.page(elements=(element,))
        intent,permit=self.runtime.prepare("click",{"ref":"e1"},before,
            context=self.context,checkpoint=lambda:None)
        changed=replace(before,elements=[replace(element,name="Other")])
        with self.assertRaisesRegex(ActionAdmissionRefused,"stale_binding"):
            self.runtime.consume(intent,permit,changed,context=self.context,checkpoint=lambda:None)

    def test_review_metadata_does_not_filter_isolated_tools_or_change_default_review(self):
        original=NativePageReview("synthetic","review",())
        isolated=ReadOnlyWindowPageReview.from_review(original)
        self.assertTrue(isolated.permits("query","/unknown","newField"))
        self.assertTrue(isolated.reviewed_control("query","/unknown","newField"))
        self.assertFalse(original.permits("query","/unknown","newField"))

    def test_inert_callback_links_are_live_controls_not_unreviewed_destinations(self):
        element=ElementRef("e1","link","Details","a",1,{"href":"javascript:void(0)"})
        self.settle(self.page(elements=(element,)),self.page(2,path="/unregistered/detail/17"))

    def test_query_dialog_and_readonly_clicks_do_not_enter_business_value_admission(self):
        from web_operation_agent.execution.action_admission import ActionIntent
        review=NativePageReview("synthetic","review",())
        draft=ReadOnlyWindowDraftSteps(self.policy.data,None,review)
        element=ElementRef("e1","textbox","Filter","input",1,
            {"form_scope":"dialog:query","page_control":"query"})
        intent=ActionIntent.create("fill",{"ref":"e1","value":"Any"},
            self.page(elements=(element,)),self.context)
        self.assertFalse(draft.handles(intent))
        business=replace(element,attrs={"form_scope":"dialog:business"})
        intent=ActionIntent.create("fill",{"ref":"e1","value":"Any"},
            self.page(elements=(business,)),self.context)
        self.assertTrue(draft.handles(intent))

    def test_pending_click_rebases_only_its_wait_and_still_requires_the_actual_actor_window(self):
        from web_operation_agent.tests.execution.test_product_model_loop import DispatchDiagnosticsTests
        run=DispatchDiagnosticsTests().pending_navigation
        outcome,actions,_=run(policy=self.policy,operation=self.context,origin=self.policy.origin)
        self.assertEqual(outcome.state,"completed")
        self.assertEqual(actions,["click","wait_for","wait_for"])
        self.guard.end("a"*64,"b"*64)
        outcome,actions,_=run(policy=self.policy,operation=self.context,origin=self.policy.origin)
        self.assertEqual(outcome.state,"unverified")
        self.assertEqual(actions.count("click"),1)
