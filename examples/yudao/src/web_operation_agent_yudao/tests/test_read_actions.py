"""Native framework facts do not replace exact effect/result/actor binding."""
from dataclasses import replace
import time
import unittest
from unittest.mock import patch

from web_operation_agent.execution.action_admission import ActionAdmission, ActionAdmissionRefused
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.business.errors import BusinessToolIdentityError
from web_operation_agent_yudao.client import YudaoClient
from web_operation_agent_yudao.goal import YudaoGoalData
from web_operation_agent_yudao.read_actions import NativeReadActions
from .test_goal import _Client, DOCS


class NativeReadActionTests(unittest.TestCase):
    def setUp(self):
        fixture = _Client()
        self.client = YudaoClient.for_token("https://fixture.test", "synthetic-token", "1")
        self.user = {"id": 1, "deptId": 10}
        def request(method, path, **kwargs):
            self.assertEqual("GET", method)
            result = fixture.get(path, **(kwargs.get("params") or {}))
            if path == "system/auth/get-permission-info":
                result = {**result, "user": self.user}
            return 0, {"code": 0, "data": result}
        self.addCleanup(patch.stopall)
        patch.object(self.client, "_request", side_effect=request).start()
        data = YudaoGoalData(self.client, DOCS)
        self.policy = NativeReadActions("https://fixture.test", data,
            {"/crm/customer": "/crm/customer/detail/{id}"}, review="synthetic-handler-effect-review")
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
        chosen = replace(tab, epoch=2, attrs={**attrs, "aria-selected": "true"})
        after = self.page(2, path="/crm/customer/detail/1", elements=(chosen,), selected=("回款",))
        self.settle(before, after)
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.settle(before, replace(after, record_route="/crm/customer/detail/2"))

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
