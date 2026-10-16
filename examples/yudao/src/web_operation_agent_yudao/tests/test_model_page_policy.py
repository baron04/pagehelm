"""Release review and exact query/paging/form readback, without provider calls."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.business.draft_steps import DraftStepAdmission
from web_operation_agent.execution.action_admission import ActionAdmission, ActionAdmissionRefused, ActionResultPending
from web_operation_agent.schemas import ElementRef, ActionReceipt
from web_operation_agent.transport.protocol import _remote_args_echo
from web_operation_agent_yudao.model_page_policy import (NativePageReview, NativeModelReadActions,
    NativeDraftSteps, _unsafe_button)
from web_operation_agent_yudao.deployment_validation import validate_native_deployment
from . import test_read_actions as read_fixture


def review_document():
    return {"schema_version": 1, "release": "synthetic-v1", "review": "synthetic-explicit-submit-review",
        "handlers": [{"kind": kind, "route": "/crm/customer", "evidence": "reviewed-source.vue", "sha256": "a"*64,
                         "controls": ["新增", "编辑"] if kind == "form_entry" else ["添加一行"] if kind == "lines" else []}
                     for kind in ("navigation", "query", "paging", "fields", "lines", "form_entry")]}


def deployment_review():
    path = Path(__file__).resolve().parents[3] / "deploy/cloud/native-policy/pages.json"
    document = json.loads(path.read_text())
    return NativePageReview.load(document, document["release"])


class ReviewTests(unittest.TestCase):
    def test_pinned_yudao_review_covers_actor_menu_and_blocks_unsafe_controls(self):
        policy = Path(__file__).resolve().parents[3] / "deploy/cloud/native-policy/pages.json"
        review = NativePageReview.load(json.loads(policy.read_text()), "v2026.09")
        for route in ("/erp/purchase/supplier", "/erp/stock/warehouse", "/erp/product/product"):
            for kind in ("navigation", "query", "paging", "form_entry", "fields"):
                self.assertTrue(review.permits(kind, route), (kind, route))
            self.assertTrue(review.permits("form_entry", route, "新增"))
            self.assertTrue(review.permits("form_entry", route))
        for route in ("/erp/sale/order", "/erp/purchase/order"):
            self.assertTrue(review.permits("query", route))
            self.assertTrue(review.permits("paging", route))
            self.assertTrue(review.permits("form_entry", route, "详情"))
        self.assertEqual(len(review.menu_routes), 506)
        self.assertEqual(review.labels_on("/erp/sale/order")["totalPrice"], "含税金额")
        self.assertTrue(review.reviewed_control("lines", "/erp/sale/order", "+ 添加销售产品"))
        self.assertFalse(review.reviewed_control("lines", "/erp/sale/order", "保存"))
        self.assertTrue(review.permits("query", "/system/user"))
        self.assertFalse(review.permits("query", "/not-in-actor-menu"))
        self.assertTrue(_unsafe_button(ElementRef("e1", "button", "批量删除", "button", 1)))
        self.assertTrue(_unsafe_button(ElementRef("e1", "button", "", "button", 1,
                                              {"action_icon": "ep:download"})))
        document = json.loads(policy.read_text())
        document["generated"]["excluded"].append(["/erp/stock/warehouse", "form_entry"])
        document["generated"]["excluded"].append(["/crm/customer/detail/{id}", "fields"])
        narrowed = NativePageReview.load(document, "v2026.09")
        self.assertFalse(narrowed.permits("form_entry", "/erp/stock/warehouse"))
        self.assertFalse(narrowed.permits("fields", "/crm/customer/detail/42"))
        self.assertFalse(review.permits("fields", "/erp/stock/warehouse/unknown"))

    def test_wallet_balance_is_in_the_generated_menu_review(self):
        review = deployment_review()
        route = "/pay/wallet/wallet-balance"
        self.assertIn(route, review.menu_routes)
        self.assertTrue(review.permits("navigation", route))
        self.assertFalse(review.permits("navigation", route + "/detail/1"))

    def test_release_exact_routes_and_capability_declarations(self):
        review = NativePageReview.load(review_document(), "synthetic-v1")
        self.assertTrue(review.permits("fields", "/crm/customer"))
        self.assertFalse(review.permits("fields", "/crm/customer/unknown"))
        self.assertFalse(review.permits("tabs", "/crm/customer"))
        self.assertEqual(frozenset({"fill", "lines", "facets", "paging", "handoffs"}), review.capabilities)
        self.assertEqual(64, len(review.digest))

    def test_generated_page_catalog_is_bound_to_the_deployed_method_fence(self):
        native = Path(__file__).resolve().parents[3] / "deploy/cloud/native-policy"
        with tempfile.TemporaryDirectory() as folder:
            changed = Path(folder) / "fence.json"
            changed.write_text((native / "fence.json").read_text() + "\n")
            config = NS(native_read_policy=changed, native_page_policy=native / "pages.json",
                        native_live_forms=True, source_release="v2026.09")
            with self.assertRaisesRegex(ValueError, "different method fence"):
                validate_native_deployment(config)
            config.native_read_policy = native / "fence.json"
            self.assertEqual(506, len(validate_native_deployment(config).page_review.menu_routes))

    def test_unknown_duplicate_or_wrong_release_is_not_a_grant(self):
        for change in (lambda d: d.update(release="wrong"),
                       lambda d: d["handlers"].append(d["handlers"][0]),
                       lambda d: d["handlers"][0].update(kind="submit"),
                       lambda d: d["handlers"][0].update(route="/**")):
            doc = review_document(); change(doc)
            with self.assertRaises(ValueError): NativePageReview.load(doc, "synthetic-v1")


class QueryTests(unittest.TestCase):
    page = read_fixture.NativeReadActionTests.page
    def setUp(self):
        read_fixture.NativeReadActionTests.setUp(self)
        review = NativePageReview.load(review_document(), "synthetic-v1")
        data = self.policy.data
        self.policy = NativeModelReadActions("https://fixture.test", data, {},
                                             NS(outline_index=lambda **kw: data.index(context=kw["context"])), review)
        self.runtime = ActionAdmission(self.policy, "yudao")
        self.proof = {"path": "/admin-api/crm/customer/page", "query_digest": "b"*64, "query_fields": {},
                      "revision": 1, "page": 1, "page_size": 10, "rows": 10, "total": 30}

    def input(self, epoch, value=""):
        return ElementRef("e1", "textbox", "名称", "input", epoch,
            {"page_control": "query", "query_field": "name", "form_scope": "form:query", "form_instance": "live",
             "field_label": "名称", "value": value})

    def test_record_navigation_reads_exact_detail_without_compiling_line_tables(self):
        self.policy.record_routes = {"/crm/customer":"/crm/customer/detail/{id}"}
        data = self.policy.data
        self.policy.goal_port = NS(outline_index=lambda **kw: data.outline_view(context=kw["context"])[0])
        document = review_document()
        document["handlers"].append({"kind":"navigation", "route":"/crm/customer/detail/{id}",
            "evidence":"reviewed-source.vue", "sha256":"b"*64, "controls":[]})
        self.policy.page_review = NativePageReview.load(document, "synthetic-v1")
        data.outline_view(context=self.context)  # exclude the catalog's own first permission read
        from unittest.mock import patch
        with patch.object(data, "schema", side_effect=AssertionError("line tables not needed")), \
                patch.object(self.client, "_request", side_effect=self.request) as requests:
            self.transact(self.page(), self.page(2, path="/crm/customer/detail/5"),
                          "goto", {"url":"https://fixture.test/crm/customer/detail/5"})
        detail = [c for c in requests.call_args_list if c.args[1]=="crm/customer/get"]
        self.assertEqual(2, len(detail))
        self.assertTrue(all(str(c.kwargs["params"]["id"])=="5" for c in detail))
        assertions = [c for c in requests.call_args_list if c.args[1]=="system/auth/get-permission-info"]
        self.assertEqual(2, len(assertions))  # fresh actor assertion before and after dispatch

    def transact(self, before, after, action, args):
        intent, permit = self.runtime.prepare(action, args, before, context=self.context, checkpoint=lambda: None)
        self.runtime.consume(intent, permit, before, context=self.context, checkpoint=lambda: None)
        receipt = ActionReceipt(1, "command", action, _remote_args_echo({**args, "epoch": before.epoch}), True,
            observation=after, url_before=before.url, url_after=after.url, write_guard_held=True)
        self.runtime.verify(intent, permit, receipt, context=self.context, checkpoint=lambda: None)

    def test_query_requires_exact_applied_value_digest(self):
        before = replace(self.page(elements=(self.input(1),), path="/crm/customer"), page_read=self.proof)
        after = replace(before, epoch=2, elements=[self.input(2, "Synthetic")])
        self.transact(before, after, "fill", {"ref": "e1", "value": "Synthetic", "commit": True})
        button = ElementRef("e2", "button", "搜索", "button", 2,
                            {"page_control": "query", "form_scope": "form:query", "form_instance": "live"})
        before = replace(after, elements=[self.input(2, "Synthetic"), button])
        self.runtime = ActionAdmission(self.policy, "yudao")
        proof = {**self.proof, "revision": 2, "query_fields": {"name": hashlib.sha256(b'["Synthetic"]').hexdigest()}}
        after = replace(before, epoch=3, elements=[self.input(3, "Synthetic"), replace(button, epoch=3)], page_read=proof)
        self.transact(before, after, "click", {"ref": "e2"})
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.transact(before, replace(after, page_read={**proof, "query_fields": {}}), "click", {"ref": "e2"})

    def test_query_reset_reads_back_live_empty_field_and_preserves_hidden_scene(self):
        digest = lambda value: hashlib.sha256(json.dumps([value], ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest()
        button = ElementRef("e2", "button", "重置", "button", 1,
                            {"page_control": "query", "form_scope": "form:query", "form_instance": "live"})
        before_fields = {"name": digest("Synthetic"), "sceneType": digest("1")}
        before = replace(self.page(elements=(self.input(1, "Synthetic"), button), path="/crm/customer"),
                         page_read={**self.proof, "query_fields": before_fields})
        after_fields = {"name": digest(""), "sceneType": digest("1")}
        empty_input = self.input(2)
        empty_input = replace(empty_input, attrs={k: v for k, v in empty_input.attrs.items() if k != "value"})
        after = replace(before, epoch=2, elements=[empty_input, replace(button, epoch=2)],
                        page_read={**self.proof, "revision": 2, "query_fields": after_fields})
        self.transact(before, after, "click", {"ref": "e2"})
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.transact(before, replace(after, page_read={**after.page_read,
                "query_fields": before_fields}), "click", {"ref": "e2"})

    def test_live_query_choice_binds_label_native_code_and_applied_read_without_source(self):
        attrs = {"page_control":"query", "query_field":"productId", "form_scope":"form:query",
                 "form_instance":"live", "field_label":"Product", "aria-haspopup":"listbox",
                 "aria-controls":"owned-options"}
        choice = ElementRef("e1", "combobox", "Product", "input", 1, attrs)
        before = replace(self.page(elements=(choice,), path="/crm/customer"), page_read=self.proof)
        selected = replace(choice, epoch=2, attrs={**attrs, "selected_option":"Synthetic", "query_value":"7"})
        after = replace(before, epoch=2, elements=[selected])
        self.transact(before, after, "select", {"ref":"e1", "value":"Synthetic", "option_label":"Synthetic"})
        button = ElementRef("e2", "button", "搜索", "button", 2,
                            {"page_control":"query", "form_scope":"form:query", "form_instance":"live"})
        before = replace(after, elements=[selected, button])
        proof = {**self.proof, "revision":2,
                 "query_fields":{"productId":hashlib.sha256(b'["7"]').hexdigest()}}
        after = replace(before, epoch=3, elements=[replace(selected, epoch=3), replace(button, epoch=3)], page_read=proof)
        self.runtime = ActionAdmission(self.policy, "yudao")
        self.transact(before, after, "click", {"ref":"e2"})
        for native in (None, "private-text"):
            self.runtime = ActionAdmission(self.policy, "yudao")
            initial = replace(before, epoch=1, elements=[choice])
            selected_attrs = {**attrs, "selected_option":"Synthetic"}
            if native is not None: selected_attrs["query_value"] = native
            changed = replace(initial, epoch=2, elements=[replace(choice, epoch=2, attrs=selected_attrs)])
            with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
                self.transact(initial, changed, "select", {"ref":"e1", "value":"Synthetic", "option_label":"Synthetic"})

    def test_query_toggle_uses_live_form_instance_and_readback(self):
        attrs = {"page_control":"query", "form_scope":"form:query", "form_instance":"live"}
        button = ElementRef("e1", "button", "展开", "button", 1, attrs)
        before = replace(self.page(elements=(button,), path="/crm/customer"), page_read=self.proof)
        after = replace(before, epoch=2, elements=[replace(button, epoch=2, name="收起")])
        self.transact(before, after, "click", {"ref":"e1"})
        for changed in (replace(after, page_read={**self.proof,"revision":2}),
                        replace(after, elements=[replace(button, epoch=2, name="收起", attrs={**attrs,"form_instance":"other"})])):
            self.runtime = ActionAdmission(self.policy, "yudao")
            with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
                self.transact(before, changed, "click", {"ref":"e1"})

    def test_query_row_export_and_delete_are_refused_before_dispatch(self):
        attrs = {"page_control": "query", "form_scope": "form:query", "form_instance": "live"}
        for name, icon in (("导出", "ep:download"), ("批量删除", "ep:delete"),
                           ("", "ep:download")):
            with self.subTest(name=name, icon=icon):
                button = ElementRef("e1", "button", name, "button", 1,
                                    {**attrs, "action_icon": icon})
                before = replace(self.page(elements=(button,), path="/crm/customer"), page_read=self.proof)
                with self.assertRaisesRegex(ActionAdmissionRefused, "unreviewed"):
                    self.transact(before, replace(before, epoch=2, elements=[replace(button, epoch=2)]),
                                  "click", {"ref": "e1"})

    def test_menu_authorization_binds_reviewed_destination_from_any_page(self):
        menu = ElementRef("e1", "menuitem", "Customers", "li", 1,
            {"navigation_kind":"page", "navigation_path":"/crm/customer", "href":"/crm/customer"})
        before = self.page(elements=(menu,))
        after = self.page(2, path="/crm/customer")
        self.transact(before, after, "click", {"ref":"e1"})
        self.runtime = ActionAdmission(self.policy, "yudao")
        unknown = replace(menu, attrs={**menu.attrs,"navigation_path":"/unreviewed","href":"/unreviewed"})
        with self.assertRaisesRegex(ActionAdmissionRefused, "unreviewed"):
            self.transact(replace(before, elements=[unknown]), after, "click", {"ref":"e1"})

    def test_wallet_balance_navigation_verifies_even_when_the_list_is_empty(self):
        route = "/pay/wallet/wallet-balance"
        self.policy.page_review = deployment_review()
        self.policy.goal_port = NS(outline_index=lambda **kw: NS(pages=[NS(key=route)]))
        before = self.page()
        after = replace(self.page(2, path=route), page_read={**self.proof,
            "path": "/admin-api/pay/wallet/page", "total": 0, "rows": 0})
        self.transact(before, after, "goto", {"url": "https://fixture.test" + route})
        self.policy.goal_port = NS(outline_index=lambda **kw: NS(pages=[]))
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unreviewed"):
            self.transact(before, after, "goto", {"url": "https://fixture.test" + route})

    def test_wallet_navigation_leaves_blocked_source_but_still_requires_review_and_arrival(self):
        route = "/pay/wallet/wallet-balance"
        self.policy.page_review = deployment_review()
        self.policy.goal_port = NS(outline_index=lambda **kw: NS(pages=[NS(key=route)]))
        source = replace(self.page(path="/erp/sale/order"), render_state="blocked", page_route="")
        arrived = self.page(2, path=route)
        self.transact(source, arrived, "goto", {"url": "https://fixture.test" + route})
        self.runtime = ActionAdmission(self.policy, "yudao")
        for rejected_source in (replace(source, render_state="pending"),
                                replace(source, render_state="ready"),
                                replace(source, page_route="/another")):
            with self.subTest(state=rejected_source.render_state, route=rejected_source.page_route), \
                    self.assertRaisesRegex(ActionAdmissionRefused, "invalid_target"):
                self.transact(rejected_source, arrived, "goto", {"url": "https://fixture.test" + route})
        with self.assertRaisesRegex(ActionAdmissionRefused, "unreviewed"):
            self.transact(source, arrived, "goto", {"url": "https://fixture.test/system/secret"})
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaises(ActionResultPending):
            self.transact(source, replace(arrived, page_route=""), "goto",
                          {"url": "https://fixture.test" + route})
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.transact(source, replace(arrived, render_state="blocked"), "goto",
                          {"url": "https://fixture.test" + route})

    def test_meeting_room_field_admits_without_offscreen_submit(self):
        from web_operation_agent_yudao.live_forms import FenceEvidence, LiveFormPort
        from web_operation_agent_yudao.page_guard import NativePageGuard
        actor, owner = "a" * 64, "c" * 64
        guard = NativePageGuard(frozenset(), writing_gets=frozenset())
        guard.begin(actor, owner)
        forms = LiveFormPort(FenceEvidence(guard, lambda: actor))
        attrs = {"form_scope": "dialog_describedby:meeting-room",
                 "form_instance": "live", "form_lifetime": "dialog-one"}
        name = ElementRef("e1", "textbox", "请输入名称", "input", 1,
                          {**attrs, "field_label": "会议室名称"})
        location = ElementRef("e2", "textbox", "请输入位置", "input", 1,
                              {**attrs, "field_label": "会议室位置"})
        before = self.page(path="/oa/meetingroom/room", elements=(name, location))
        admission = DraftStepAdmission(NativeDraftSteps(
            self.policy.data, forms, deployment_review()), "yudao")
        intent, permit = admission.prepare("fill", {"ref": "e1", "value": "云台厅"}, before,
                                           context=self.context, checkpoint=lambda: None)
        self.assertEqual(permit.step, "enter_value")
        self.assertEqual(permit.values, (("会议室名称", "云台厅"),))
        admission.consume(intent, permit, before, context=self.context, checkpoint=lambda: None)

    def test_paging_option_requires_owned_popup_and_fresh_matching_read(self):
        owner = ElementRef("e1", "combobox", "每页", "input", 1,
                           {"page_control": "page_size", "aria-controls": "owned-list"})
        option = ElementRef("e2", "option", "20 条/页", "li", 1, {"listbox_owner": "owned-list"})
        before = replace(self.page(path="/crm/customer", elements=(owner, option)), page_read=self.proof)
        after = replace(before, epoch=2, elements=[replace(owner, epoch=2)],
                        page_read={**self.proof, "revision": 2, "page_size": 20, "rows": 20})
        self.transact(before, after, "click", {"ref": "e2"})
        self.runtime = ActionAdmission(self.policy, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "unreviewed"):
            self.transact(replace(before, elements=[owner, replace(option, attrs={})]), after, "click", {"ref": "e2"})

    def test_date_pair_binds_commit_and_exact_endpoint_and_never_submit(self):
        scope = {"form_scope": "dialog:editor", "form_instance": "live", "aria-haspopup": "dialog", "aria-controls": "calendar"}
        first = ElementRef("e1", "combobox", "开始", "input", 1, {**scope, "field_label": "期间", "value": ""})
        second = ElementRef("e2", "combobox", "结束", "input", 1, {**scope, "value": ""})
        before = self.page(path="/crm/customer", elements=(first, second))
        forms = NS(_evidence=NS(mark=lambda: 0, since=lambda mark: ()))
        port = NativeDraftSteps(self.policy.data, forms, self.policy.page_review)
        admission = DraftStepAdmission(port, "yudao")
        with self.assertRaisesRegex(ActionAdmissionRefused, "invalid_target"):
            admission.prepare("fill", {"ref": "e1", "value": "2026-10-20", "commit": True}, before,
                              context=self.context, checkpoint=lambda: None)
        args = {"ref": "e1", "value": "2026-10-20", "commit": False}
        intent, permit = admission.prepare("fill", args, before, context=self.context, checkpoint=lambda: None)
        admission.consume(intent, permit, before, context=self.context, checkpoint=lambda: None)
        after = replace(before, epoch=2, elements=[replace(first, epoch=2, attrs={**first.attrs, "value": args["value"]}), replace(second, epoch=2)])
        receipt = ActionReceipt(1, "draft", "fill", _remote_args_echo({**args, "epoch": 1}), True,
                               observation=after, url_before=before.url, url_after=after.url, write_guard_held=True)
        admission.verify(intent, permit, receipt, context=self.context, checkpoint=lambda: None)
        swapped = replace(after, elements=[replace(first, epoch=2), replace(second, epoch=2, attrs={**second.attrs, "value": args["value"]})])
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            admission.verify(intent, permit, replace(receipt, observation=swapped), context=self.context, checkpoint=lambda: None)
        with self.assertRaises(ActionAdmissionRefused):
            admission.prepare("submit", {"ref": "e1"}, before, context=self.context, checkpoint=lambda: None)

    def test_line_numeric_readback_accepts_formatting_but_rejects_changed_value(self):
        field = ElementRef("e1", "spinbutton", "数量", "input", 1,
            {"form_scope": "dialog:editor", "form_instance": "live", "title": "明细第1行·数量", "value": "1.000"})
        before = self.page(path="/crm/customer", elements=(field,))
        forms = NS(_evidence=NS(mark=lambda: 0, since=lambda mark: ()))
        port = NativeDraftSteps(self.policy.data, forms, self.policy.page_review)
        admission = DraftStepAdmission(port, "yudao")
        args = {"ref": "e1", "value": "2", "commit": True}
        intent, permit = admission.prepare("fill", args, before, context=self.context, checkpoint=lambda: None)
        admission.consume(intent, permit, before, context=self.context, checkpoint=lambda: None)
        for value, valid in (("2.000", True), ("2.001", False), ("NaN", False)):
            with self.subTest(value=value):
                after = replace(before, epoch=2, elements=[replace(field, epoch=2, attrs={**field.attrs, "value": value})])
                receipt = ActionReceipt(1, "draft", "fill", _remote_args_echo({**args, "epoch": 1}), True,
                    observation=after, url_before=before.url, url_after=after.url, write_guard_held=True)
                if valid:
                    admission.verify(intent, permit, receipt, context=self.context, checkpoint=lambda: None)
                else:
                    with self.assertRaises(ActionAdmissionRefused):
                        admission.verify(intent, permit, receipt, context=self.context, checkpoint=lambda: None)

    def test_exact_reviewed_line_button_can_add_one_transient_row(self):
        document = review_document()
        next(item for item in document["handlers"] if item["kind"] == "lines")["controls"] = ["+ 添加销售产品"]
        review = NativePageReview.load(document, "synthetic-v1")
        forms = NS(_evidence=NS(mark=lambda: 0, since=lambda mark: ()))
        admission = DraftStepAdmission(NativeDraftSteps(self.policy.data, forms, review), "yudao")
        attrs = {"form_scope": "dialog:editor", "form_instance": "live", "form_lifetime": "dialog-1"}
        button = ElementRef("e1", "button", "+ 添加销售产品", "button", 1, attrs)
        first = ElementRef("e2", "textbox", "产品", "input", 1,
                           {**attrs, "title": "明细第1行·产品名称"})
        before = self.page(path="/crm/customer", elements=(button, first))
        intent, permit = admission.prepare("click", {"ref": "e1"}, before,
                                           context=self.context, checkpoint=lambda: None)
        self.assertEqual(permit.step, "add_line")
        admission.consume(intent, permit, before, context=self.context, checkpoint=lambda: None)
        second = ElementRef("e3", "textbox", "产品", "input", 2,
                            {**attrs, "title": "明细第2行·产品名称"})
        after = replace(before, epoch=2, elements=[replace(button, epoch=2),
                                                    replace(first, epoch=2), second])
        receipt = ActionReceipt(1, "draft", "click", {"ref": "e1", "epoch": 1}, True,
                                observation=after, url_before=before.url, url_after=after.url,
                                write_guard_held=True)
        admission.verify(intent, permit, receipt, context=self.context, checkpoint=lambda: None)
        for name in ("保存", "删除", "未经审核的按钮"):
            with self.subTest(name=name), self.assertRaises(ActionAdmissionRefused):
                unreviewed = replace(button, name=name)
                admission.prepare("click", {"ref": "e1"}, replace(before, elements=[unreviewed, first]),
                                  context=self.context, checkpoint=lambda: None)

    def test_no_effect_choice_requires_unchanged_entire_bound_form_and_write_evidence(self):
        attrs = {"form_scope":"dialog:editor", "form_instance":"live", "title":"明细第1行·产品名称",
                 "aria-controls":"products-1", "aria-haspopup":"listbox", "aria-expanded":"false"}
        choice = ElementRef("e1", "combobox", "产品名称", "input", 1, attrs)
        other = ElementRef("e2", "textbox", "备注", "input", 1,
                           {"form_scope":"dialog:editor", "form_instance":"live", "field_label":"备注", "value":"untouched"})
        before = self.page(path="/crm/customer", elements=(choice, other))
        evidence = NS(mark=lambda:0, since=lambda mark:())
        admission = DraftStepAdmission(NativeDraftSteps(self.policy.data, NS(_evidence=evidence), self.policy.page_review), "yudao")
        args = {"ref":"e1", "value":"Missing", "option_label":"Missing"}
        intent, permit = admission.prepare("select", args, before, context=self.context, checkpoint=lambda:None)
        admission.consume(intent, permit, before, context=self.context, checkpoint=lambda:None)
        after = replace(before, epoch=2, elements=[replace(choice, epoch=2), replace(other, epoch=2)])
        receipt = ActionReceipt(1, "choice", "select", _remote_args_echo({**args, "epoch":1}), False,
            error_code="option_unavailable", no_effect=True, candidate_labels=("Exact option",),
            observation=after, url_before=before.url, url_after=after.url, write_guard_held=True)
        admission.verify_no_effect(intent, permit, receipt, context=self.context, checkpoint=lambda:None)
        for changed in (replace(after, elements=[replace(choice, epoch=2), replace(other, epoch=2,
                           attrs={**other.attrs,"value":"changed"})]),
                        replace(after, elements=[replace(choice, epoch=2, attrs={**attrs,"form_instance":"replaced"}), replace(other, epoch=2)])):
            with self.assertRaises(ActionAdmissionRefused):
                admission.verify_no_effect(intent, permit, replace(receipt, observation=changed), context=self.context, checkpoint=lambda:None)
        evidence.since = lambda mark: ("write",)
        with self.assertRaises(ActionAdmissionRefused):
            admission.verify_no_effect(intent, permit, receipt, context=self.context, checkpoint=lambda:None)

    def test_opening_a_line_choice_binds_its_title_form_and_owned_listbox(self):
        attrs = {"form_scope":"dialog:editor", "form_instance":"live", "title":"明细第1行·产品名称",
                 "aria-controls":"products-1", "aria-expanded":"false"}
        field = ElementRef("e1", "combobox", "产品名称", "input", 1, attrs)
        before = self.page(path="/crm/customer", elements=(field,))
        forms = NS(_evidence=NS(mark=lambda:0, since=lambda mark:()))
        admission = DraftStepAdmission(NativeDraftSteps(self.policy.data, forms, self.policy.page_review), "yudao")
        intent, permit = admission.prepare("click", {"ref":"e1"}, before,
            context=self.context, checkpoint=lambda:None)
        admission.consume(intent, permit, before, context=self.context, checkpoint=lambda:None)
        opened = replace(field, ref="e7", epoch=2, attrs={**attrs,"aria-expanded":"true"})
        for elements, valid in (([opened], True),
                ([replace(opened, attrs={**opened.attrs,"aria-controls":"another"})], False),
                ([replace(opened, attrs={**opened.attrs,"form_instance":"replaced"})], False),
                ([replace(opened, attrs={**opened.attrs,"title":"明细第2行·产品名称"})], False),
                ([opened, replace(opened, ref="e8")], False)):
            with self.subTest(elements=elements):
                after = replace(before, epoch=2, elements=elements)
                receipt = ActionReceipt(1,"draft","click",{"ref":"e1","epoch":1},True,
                    observation=after,url_before=before.url,url_after=after.url,write_guard_held=True)
                if valid:
                    admission.verify(intent,permit,receipt,context=self.context,checkpoint=lambda:None)
                else:
                    with self.assertRaises(ActionAdmissionRefused):
                        admission.verify(intent,permit,receipt,context=self.context,checkpoint=lambda:None)
