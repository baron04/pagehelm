"""Complete native relation effects, permissions and acknowledged partial outcomes."""
from unittest import TestCase
from unittest.mock import patch

from web_operation_agent.business.errors import (BusinessToolPermissionError,
    BusinessToolRequestError, BusinessMutationIndeterminate)
from web_operation_agent_yudao.tests.test_native_writes import Client, Data, context
from web_operation_agent_yudao.writes import native_tools


class FollowUpRelations(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.records[("crm/contact", 11)] = {"id": 11, "name": "合成第二联系人", "contactNextTime": 1}
        self.client.records[("crm/business", 40)] = {"id": 40, "name": "合成商机", "contactNextTime": 2}
        self.data = Data(self.client)
        self.tools = native_tools(self.client, self.data)
        self.inputs = {"entity": "crm/customer", "target": "合成星河", "type": "电话",
            "content": "合成关联记录", "nextTime": "2026-10-13 10:00:00",
            "contacts": ["合成第二联系人", "林舟"], "businesses": ["合成商机"]}

    def test_full_sets_and_every_linked_effect_are_approved_and_verified(self):
        spec = self.tools.prepare_mutation("crm_follow_up_create", self.inputs, context=context())
        expected = self.tools.get("crm_follow_up_create").effect_contract.project_inputs(self.inputs)
        spec.effect_projection.require(expected)
        self.assertEqual(spec.args["body"]["contactIds"], [11, 10])
        self.assertEqual(spec.args["body"]["businessIds"], [40])
        self.assertIn("related_follow_up", spec.effect_projection.required_receipts)
        self.assertTrue(any("合成第二联系人" in d.text and "林舟" in d.text for d in spec.details))
        receipt = self.tools.execute_mutation("crm_follow_up_create", spec.args, context=context())
        spec.effect_projection.require_receipt(receipt.content)
        self.assertEqual(len(self.client.writes), 1)
        for entity, id in (("crm/contact", 10), ("crm/contact", 11), ("crm/business", 40)):
            self.assertEqual(self.client.records[(entity, id)]["contactNextTime"], spec.args["body"]["nextTime"])
        self.assertEqual(self.data.invalidated[-1], frozenset({"crm/customer", "crm/contact", "crm/business"}))

    def test_unknown_or_duplicate_member_does_not_prepare_a_smaller_set(self):
        for members in (["林舟", "不存在的合成联系人"], ["林舟", "林舟"]):
            with self.subTest(members=members), self.assertRaises(BusinessToolRequestError):
                self.tools.prepare_mutation("crm_follow_up_create", {**self.inputs, "contacts": members}, context=context())
        self.assertEqual(self.client.writes, [])

    def test_read_permission_does_not_allow_linked_write(self):
        original = self.client.get
        def read_only(path, **params):
            return [{"userId": 9, "level": 2}] if path == "crm/permission/list" else original(path, **params)
        with patch.object(self.client, "get", side_effect=read_only), self.assertRaises(BusinessToolPermissionError):
            self.tools.prepare_mutation("crm_follow_up_create", self.inputs, context=context())
        self.assertEqual(self.client.writes, [])

    def test_permission_revocation_after_approval_prevents_dispatch(self):
        spec = self.tools.prepare_mutation("crm_follow_up_create", self.inputs, context=context())
        original = self.client.get
        def revoked(path, **params):
            return [] if path == "crm/permission/list" else original(path, **params)
        with patch.object(self.client, "get", side_effect=revoked), self.assertRaises(BusinessToolPermissionError):
            self.tools.execute_mutation("crm_follow_up_create", spec.args, context=context())
        self.assertEqual(self.client.writes, [])

    def test_missing_linked_postcondition_remains_indeterminate_after_one_write(self):
        spec = self.tools.prepare_mutation("crm_follow_up_create", self.inputs, context=context())
        original = self.client.write_once
        def partial(*args, **kwargs):
            result = original(*args, **kwargs)
            self.client.records[("crm/contact", 11)]["contactNextTime"] = 1
            return result
        with patch.object(self.client, "write_once", side_effect=partial), self.assertRaises(BusinessMutationIndeterminate):
            self.tools.execute_mutation("crm_follow_up_create", spec.args, context=context())
        self.assertEqual(len(self.client.writes), 1)
