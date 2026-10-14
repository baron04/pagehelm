"""Release review and exact query/paging/form readback, without provider calls."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.business.draft_steps import DraftStepAdmission
from web_operation_agent.execution.action_admission import ActionAdmission, ActionAdmissionRefused
from web_operation_agent.schemas import ElementRef, ActionReceipt
from web_operation_agent.transport.protocol import _remote_args_echo
from web_operation_agent_yudao.model_page_policy import NativePageReview, NativeModelReadActions, NativeDraftSteps
from . import test_read_actions as read_fixture


def review_document():
    return {"schema_version": 1, "release": "synthetic-v1", "review": "synthetic-explicit-submit-review",
        "handlers": [{"kind": kind, "route": "/crm/customer", "evidence": "reviewed-source.vue", "sha256": "a"*64,
                         "controls": ["新增", "编辑"] if kind == "form_entry" else ["添加一行"] if kind == "lines" else []}
                     for kind in ("navigation", "query", "paging", "fields", "lines", "form_entry")]}


class ReviewTests(unittest.TestCase):
    def test_release_exact_routes_and_capability_declarations(self):
        review = NativePageReview.load(review_document(), "synthetic-v1")
        self.assertTrue(review.permits("fields", "/crm/customer"))
        self.assertFalse(review.permits("fields", "/crm/customer/unknown"))
        self.assertFalse(review.permits("tabs", "/crm/customer"))
        self.assertEqual(frozenset({"fill", "lines", "facets", "paging", "handoffs"}), review.capabilities)
        self.assertEqual(64, len(review.digest))

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
