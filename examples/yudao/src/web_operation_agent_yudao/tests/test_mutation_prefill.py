"""The native third choice is limited to an exact actor-visible contact draft."""
from dataclasses import replace
import time
import unittest

from web_operation_agent.business.tools.contracts import BusinessMutationProposal
from web_operation_agent.execution.draft_handoff import field_identity
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent_yudao.mutation_prefill import (
    YudaoMutationPrefill, _customer_choice_settled)


class _Client:
    def __init__(self):
        self.rows = [{"id": 20, "name": "合成星河"}]

    def get(self, path, **kwargs):
        if path == "crm/customer/simple-list":
            return self.rows
        raise AssertionError(path)


class _Review:
    def permits(self, kind, path, control=""):
        return path == "/crm/contact" and kind in {"form_entry", "fields"}


def _context():
    now = int(time.time() * 1000)
    actor = PrincipalContext(1, "yudao", "1", "7", "9", "synthetic", now - 1000,
                             now + 60000, ("yudao.read", "yudao.write"))
    return OperationContext.create(actor, session_id="s", turn_id="t", operation_id="o",
                                   deadline_ms=now + 30000)


def _observation(route="/crm/contact"):
    return PageObservation(1, 1, "http://synthetic.invalid" + route, "CRM", [],
                           [ElementRef("e1", "button", "新增", "button", 1, {})],
                           "synthetic", render_state="ready", page_route=route)


def _proposal(context, **changes):
    body = {"name": "周思远", "mobile": "13911113332", "post": "项目协调员",
            "customerId": 20, "ownerUserId": 9}
    body.update(changes)
    return BusinessMutationProposal("crm_contact_create", {
        "body": body, "before": {}, "entity": "crm/contact", "actor": "9",
        "operation": "crm_contact_create", "choices": [],
        "relations": [{"entity": "crm/customer", "id": 20, "name": "合成星河"}]},
        "新建CRM 联系人资料", ("周思远",), "1" * 32,
        operation_context_digest=context.binding_digest)


def _form(customer="", owner="当前账号", parent=None):
    elements = [ElementRef("e1", "combobox", "客户名称", "input", 1,
                {"form_scope": "dialog", "form_instance": "one", "field_label": "客户名称",
                 "selected_option": customer}),
                ElementRef("e2", "combobox", "负责人", "input", 1,
                {"form_scope": "dialog", "form_instance": "one", "field_label": "负责人",
                 "selected_option": owner})]
    if parent is not None:
        elements.append(ElementRef("e3", "combobox", "直属上级", "input", 1,
            {"form_scope": "dialog", "form_instance": "one", "field_label": "直属上级",
             "selected_option": parent}))
    return PageObservation(1, 1, "http://synthetic.invalid/crm/contact", "CRM", [],
                           elements, "synthetic", render_state="ready", page_route="/crm/contact")


class YudaoMutationPrefillTests(unittest.TestCase):
    def setUp(self):
        self.context = _context()
        self.client = _Client()
        self.port = YudaoMutationPrefill(self.client, object(), _Review())

    def test_exact_actor_customer_and_visible_entry_offer(self):
        proposal = _proposal(self.context)
        offer = self.port.offer(proposal, context=self.context,
                                observation=_observation())
        self.assertEqual(offer.target, "/crm/contact#crm/customer/20")
        self.assertEqual(dict(offer.fields)["customer"], "合成星河")
        self.assertEqual(dict(offer.fields)["ownerUserId"], "9")

    def test_wrong_page_or_ambiguous_customer_never_offers(self):
        proposal = _proposal(self.context)
        self.assertIsNone(self.port.offer(proposal, context=self.context,
                                          observation=_observation("/crm/customer")))
        self.client.rows.append({"id": 21, "name": "合成星河"})
        self.assertIsNone(self.port.offer(proposal, context=self.context,
                                          observation=_observation()))

    def test_unmapped_effect_and_wrong_actor_never_offer(self):
        self.assertIsNone(self.port.offer(_proposal(self.context, email="new@example.test"),
                                          context=self.context, observation=_observation()))
        self.assertIsNone(self.port.offer(_proposal(self.context, ownerUserId=8),
                                          context=self.context, observation=_observation()))
        self.assertIsNone(self.port.offer(replace(_proposal(self.context),
                                                  targets=("其他联系人",)),
                                          context=self.context, observation=_observation()))

    def test_customer_choice_may_reveal_only_an_empty_parent(self):
        before = _form()
        key = field_identity(before.elements[0])
        self.assertTrue(_customer_choice_settled(before, _form("合成星河", parent=""),
                                                 key, "合成星河"))
        self.assertFalse(_customer_choice_settled(before, _form("合成星河", parent="林舟"),
                                                  key, "合成星河"))
        self.assertFalse(_customer_choice_settled(before, _form("合成星河", owner="其他账号",
                                                         parent=""), key, "合成星河"))


if __name__ == "__main__":
    unittest.main()
