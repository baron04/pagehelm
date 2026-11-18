"""Native framework facts do not replace exact effect/result/actor binding."""
from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from web_operation_agent.execution.action_admission import (
    ActionAdmission, ActionAdmissionRefused, ActionPermit, ActionEffect, ActionResultPending)
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.business.errors import BusinessToolIdentityError
from web_operation_agent_yudao.reads.client import YudaoClient
from web_operation_agent_yudao.goal_compat.goal import YudaoGoalData
from web_operation_agent_yudao.reads.read_actions import NativeReadActions
from .test_goal import _Client, DOCS
from copy import deepcopy


class NativeReadActionTests(unittest.TestCase):
    def setUp(self):
        fixture = _Client()
        self.client = YudaoClient.for_token("https://fixture.test", "synthetic-token", "1")
        self.user = {"id": 1, "deptId": 10}
        def request(method, path, **kwargs):
            self.assertEqual("GET", method)
            if path == "crm/customer/get":
                identifier = int(kwargs["params"]["id"])
                result = {"id": identifier, "name": "Synthetic detail"}
            else:
                result = fixture.get(path, **(kwargs.get("params") or {}))
            if path == "system/auth/get-permission-info":
                result = {**result, "user": self.user}
            return 0, {"code": 0, "data": result}
        self.addCleanup(patch.stopall)
        patch.object(self.client, "_request", side_effect=request).start()
        self.fixture = fixture
        docs = deepcopy(DOCS)
        docs["paths"]["/admin-api/crm/customer/get"] = docs["paths"]["/admin-api/crm/contract/get"]
        self.request = request
        data = YudaoGoalData(self.client, docs)
        self.policy = NativeReadActions("https://fixture.test", data,
            {"/crm/customer": "/crm/customer/detail/{id}"}, review="synthetic-handler-effect-review",
            list_tab_routes=("/crm/customer",))
        self.runtime = ActionAdmission(self.policy, "yudao")
        now = int(time.time() * 1000)
        principal = PrincipalContext(1, "yudao", "1", "10", "1", "delegation", now-1000,
                                     now+60_000, ("yudao.read",))
        self.context = OperationContext.create(principal, session_id="session", turn_id="turn",
                                              operation_id="action", deadline_ms=now+50_000)

    def page(self, epoch=1, *, path="/index", elements=(), selected=()):
        return PageObservation(1, epoch, "https://fixture.test" + path, "Synthetic", [], list(elements),
            render_state="ready", page_route=path,
            record_route=path if "/detail/" in path else "", selected_controls=list(selected))

    def settle(self, before, after, action="click", args=None):
        args = args or {"ref": "e1"}
        intent, permit = self.runtime.prepare(action, args, before,
            context=self.context, checkpoint=lambda: None)
        self.runtime.consume(intent, permit, before, context=self.context, checkpoint=lambda: None)
        echo = {**args, **({"epoch": before.epoch} if "ref" in args else {})}
        receipt = ActionReceipt(1, "command", action, echo, True, url_before=before.url,
            url_after=after.url, observation=after, write_guard_held=True)
        self.runtime.verify(intent, permit, receipt, context=self.context, checkpoint=lambda: None)

    def test_actor_catalog_menu_and_destination_render_are_both_required(self):
        item = ElementRef("e1", "menuitem", "客户", "li", 1,
            {"navigation_kind": "page", "navigation_path": "/crm/customer", "href": "/crm/customer"})
        self.settle(self.page(elements=(item,)), self.page(2, path="/crm/customer"))
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.settle(self.page(elements=(item,)), self.page(2, path="/crm/contract"))
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaises(ActionAdmissionRefused):
            self.settle(self.page(), self.page(2), "goto", {"url": "/system/secret"})

    def test_tab_requires_selected_same_id_and_panel_on_same_rendered_record(self):
        attrs = {"id": "tab-payments", "aria-controls": "pane-payments", "aria-selected": "false"}
        tab = ElementRef("e1", "tab", "回款", "div", 1, attrs)
        before = self.page(path="/crm/customer/detail/1", elements=(tab,))
        chosen = replace(tab, epoch=2, attrs={**attrs, "aria-selected": "true", "panel_ready": "true"})
        after = self.page(2, path="/crm/customer/detail/1", elements=(chosen,), selected=())
        self.settle(before, after)
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.settle(before, replace(after, record_route="/crm/customer/detail/2"))
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.settle(before, replace(after, elements=[replace(chosen, attrs=attrs)], selected_controls=["回款"]))
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.settle(before, replace(after, elements=[replace(chosen,
                attrs={**chosen.attrs, "panel_ready": "false"})]))

    def test_list_tab_requires_selected_tab_and_fresh_changed_list_read(self):
        attrs = {"id": "tab-2", "aria-controls": "pane-2", "aria-selected": "false"}
        tab = ElementRef("e1", "tab", "我参与的", "div", 1, attrs)
        read = {"path": "/admin-api/crm/customer/page", "query_digest": "a" * 64,
                "query_fields": {}, "revision": 1, "page": 1, "page_size": 10,
                "total": 20, "rows": 10}
        before = replace(self.page(path="/crm/customer", elements=(tab,)), page_read=read,
            page_query={"path": read["path"], "parameters": {"sceneType": "1"}})
        chosen = replace(tab, epoch=2, attrs={**attrs, "aria-selected": "true", "panel_ready": "true"})
        after = replace(self.page(2, path="/crm/customer", elements=(chosen,)),
            page_read={**read, "revision": 2, "query_digest": "b" * 64},
            page_query={"path": read["path"], "parameters": {"sceneType": "2"}})
        self.settle(before, after)
        self.policy.list_tab_routes = frozenset()
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unreviewed"):
            self.runtime.prepare("click", {"ref": "e1"}, before,
                                 context=self.context, checkpoint=lambda: None)
        self.policy.list_tab_routes = frozenset(("/crm/customer",))
        for pending in (replace(after, page_read=None),
                        replace(after, page_read={**after.page_read, "revision": 1})):
            self.runtime = ActionAdmission(self.policy, "yudao")
            with self.subTest(pending=pending), self.assertRaises(ActionResultPending):
                self.settle(before, pending)
        for changed in (replace(after, page_read={**after.page_read, "query_digest": read["query_digest"]}),
                        replace(after, page_read={**after.page_read, "path": "/admin-api/other/page"}),
                        replace(after, elements=[replace(chosen, attrs={**chosen.attrs, "panel_ready": "false"})]),
                        replace(after, elements=[replace(chosen, attrs={**chosen.attrs, "aria-selected": "false"})])):
            self.runtime = ActionAdmission(self.policy, "yudao")
            with self.subTest(changed=changed), self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
                self.settle(before, changed)
        for unqualified in (replace(before, page_read=None), replace(before, page_query=None),
                            replace(before, page_route="/other"), replace(before, url="https://fixture.test/other")):
            self.runtime = ActionAdmission(self.policy, "yudao")
            with self.subTest(unqualified=unqualified), self.assertRaises(ActionAdmissionRefused):
                self.runtime.prepare("click", {"ref": "e1"}, unqualified,
                                     context=self.context, checkpoint=lambda: None)

    def test_list_tab_waits_on_one_click_until_its_read_settles(self):
        tab = ElementRef("e1", "tab", "我参与的", "div", 1,
            {"id": "tab-2", "aria-controls": "pane-2", "aria-selected": "false"})
        read = {"path": "/admin-api/crm/customer/page", "query_digest": "a" * 64,
                "query_fields": {}, "revision": 1, "page": 1, "page_size": 10,
                "total": 20, "rows": 10}
        before = replace(self.page(path="/crm/customer", elements=(tab,)), page_read=read,
            page_query={"path": read["path"], "parameters": {"sceneType": "1"}})
        chosen = replace(tab, epoch=2, attrs={**tab.attrs,
            "aria-selected": "true", "panel_ready": "true"})
        pending = self.page(2, path="/crm/customer", elements=(chosen,))
        final = replace(self.page(3, path="/crm/customer", elements=(replace(chosen, epoch=3),)),
            page_read={**read, "revision": 2, "query_digest": "b" * 64},
            page_query={"path": read["path"], "parameters": {"sceneType": "2"}})
        args = {"ref": "e1"}
        intent, permit = self.runtime.prepare("click", args, before,
            context=self.context, checkpoint=lambda: None)
        self.runtime.consume(intent, permit, before, context=self.context, checkpoint=lambda: None)
        receipt = ActionReceipt(1, "command", "click", {**args, "epoch": before.epoch}, True,
            url_before=before.url, url_after=pending.url, observation=pending, write_guard_held=True)
        with self.assertRaises(ActionResultPending):
            self.runtime.verify(intent, permit, receipt,
                context=self.context, checkpoint=lambda: None)
        settlement = ActionReceipt(1, "observe", "wait_for", {"ms": 150}, True,
            url_before=final.url, url_after=final.url, observation=final, write_guard_held=True)
        self.runtime.verify(intent, permit, receipt, settlement=settlement,
            context=self.context, checkpoint=lambda: None)
        self.assertTrue(self.runtime.query_verified(final, command_id="command"))
        with self.assertRaisesRegex(ActionAdmissionRefused, "already_consumed"):
            self.runtime.consume(intent, permit, before, context=self.context, checkpoint=lambda: None)

    def test_identity_or_credential_drift_cannot_verify_acknowledged_action(self):
        intent, permit = self.runtime.prepare("goto", {"url": "/crm/customer"}, self.page(),
            context=self.context, checkpoint=lambda: None)
        self.runtime.consume(intent, permit, self.page(), context=self.context, checkpoint=lambda: None)
        after = self.page(2, path="/crm/customer")
        receipt = ActionReceipt(1, "command", "goto", {"url": "/crm/customer"}, True,
            url_before=self.page().url, url_after=after.url, observation=after, write_guard_held=True)
        self.client.renew("synthetic-renewed")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.runtime.verify(intent, permit, receipt, context=self.context, checkpoint=lambda: None)
        self.user = {"id": 2, "deptId": 10}
        with self.assertRaises(BusinessToolIdentityError):
            self.runtime.prepare("goto", {"url": "/crm/customer"}, self.page(),
                context=self.context, checkpoint=lambda: None)

    def test_unreviewed_handlers_are_not_enabled_by_roles_or_http_fence(self):
        for role, name in (("button", "下一页"), ("button", "提交"), ("textbox", "名称")):
            element = ElementRef("e1", role, name, "button", 1)
            with self.subTest(role=role), self.assertRaises(ActionAdmissionRefused):
                self.runtime.prepare("click", {"ref": "e1"}, self.page(elements=(element,)),
                    context=self.context, checkpoint=lambda: None)

    def test_form_subreads_stop_when_delegated_credential_renews(self):
        invoked = []
        def changed(check):
            invoked.append('first')
            self.client.renew('synthetic-renewed')
            check()
            invoked.append('second')
        with self.assertRaises(BusinessToolIdentityError):
            self.policy._form_read(lambda:None,changed)
        self.assertEqual(invoked,['first'])

    def test_reviewed_editor_path_checks_actor_without_loading_business_catalog(self):
        # This isolates the effect-policy routing dependency. Real opener review and
        # field binding are qualified in test_form_open_actions and native Chromium.
        self.policy.form_open_actions = SimpleNamespace(
            matches=lambda intent: intent.action == 'click',
            admit=lambda intent, **kw: ActionPermit(intent.binding_digest,
                ActionEffect.NAVIGATION,'synthetic-reviewed-editor','exact-editor'),
            verify=lambda *args, **kw: True)
        opener=ElementRef('e1','button','Open reviewed editor','button',1)
        with patch.object(self.policy.data,'index',side_effect=AssertionError('catalog unavailable')):
            self.settle(self.page(elements=(opener,)),self.page(2))
        self.runtime=ActionAdmission(self.policy,'yudao')
        self.user={'id':2,'deptId':10}
        with self.assertRaises(BusinessToolIdentityError):
            self.runtime.prepare('click',{'ref':'e1'},self.page(elements=(opener,)),
                                 context=self.context,checkpoint=lambda:None)

    def test_exact_record_get_and_render_proof_bind_navigation(self):
        for identifier in (5, 6):
            self.runtime = ActionAdmission(self.policy, "yudao")
            route = f"/crm/customer/detail/{identifier}"
            self.settle(self.page(), self.page(2, path=route), "goto", {"url": route})
        for changed in (self.page(2, path="/crm/customer"),
                        replace(self.page(2, path="/crm/customer/detail/5"), record_route=""),
                        self.page(2, path="/crm/customer/detail/6")):
            self.runtime = ActionAdmission(self.policy, "yudao")
            with self.subTest(changed=changed.url), self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
                self.settle(self.page(), changed, "goto", {"url": "/crm/customer/detail/5"})

    def test_denied_detail_does_not_fall_back_to_readable_list(self):
        from web_operation_agent.goal.data import DataRefused
        def denied(method, path, **kw):
            if path == "crm/customer/get":
                raise DataRefused("synthetic denied detail", permission_denied=True)
            return self.request(method, path, **kw)
        with patch.object(self.client, "_request", side_effect=denied), self.assertRaises(DataRefused):
            self.runtime.prepare("goto", {"url": "/crm/customer/detail/5"}, self.page(),
                                 context=self.context, checkpoint=lambda: None)
        self.assertNotIn("crm/customer/page", [p for p, _ in self.fixture.calls])

    def test_record_route_must_be_unique_and_match_returned_identifier(self):
        self.policy.record_routes["/crm/contract"] = "/crm/customer/detail/{id}"
        with self.assertRaises(ActionAdmissionRefused):
            self.runtime.prepare("goto", {"url": "/crm/customer/detail/5"}, self.page(),
                                 context=self.context, checkpoint=lambda: None)
        del self.policy.record_routes["/crm/contract"]
        def wrong(method, path, **kw):
            if path == "crm/customer/get":
                return 0, {"code": 0, "data": {"id": 6, "name": "Wrong record"}}
            return self.request(method, path, **kw)
        with patch.object(self.client, "_request", side_effect=wrong), self.assertRaises(ActionAdmissionRefused):
            self.runtime.prepare("goto", {"url": "/crm/customer/detail/5"}, self.page(),
                                 context=self.context, checkpoint=lambda: None)

    def test_route_mounting_waits_on_the_consumed_receipt_without_replaying_navigation(self):
        before=self.page()
        args={"url":"/crm/customer"}
        intent,permit=self.runtime.prepare("goto",args,before,context=self.context,checkpoint=lambda:None)
        self.runtime.consume(intent,permit,before,context=self.context,checkpoint=lambda:None)
        pending=replace(self.page(2,path="/crm/customer"),page_route="")
        receipt=ActionReceipt(1,"command","goto",args,True,url_before=before.url,
            url_after=pending.url,observation=pending,write_guard_held=True)
        with self.assertRaises(ActionResultPending):
            self.runtime.verify(intent,permit,receipt,context=self.context,checkpoint=lambda:None)
        fresh=self.page(3,path="/crm/customer")
        settlement=ActionReceipt(1,"observe","wait_for",{"ms":300},True,
            url_before=fresh.url,url_after=fresh.url,observation=fresh,write_guard_held=True)
        self.runtime.verify(intent,permit,receipt,settlement=settlement,
            context=self.context,checkpoint=lambda:None)
        with self.assertRaisesRegex(ActionAdmissionRefused,"already_consumed"):
            self.runtime.consume(intent,permit,before,context=self.context,checkpoint=lambda:None)
