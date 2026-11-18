"""Whole native state, plan and publication effects through the installed tools."""
import copy
import unittest

from web_operation_agent.business.errors import (BusinessToolRequestError, BusinessMutationRefusedError,
    BusinessMutationIndeterminate)
from web_operation_agent_yudao.writes.writes import native_tools
from web_operation_agent_yudao.writes.write_special import read_pages
from .test_record_expansion import Records
from .test_native_writes import Data, context


class SpecialClient(Records):
    def __init__(self):
        super().__init__()
        self.records[("crm/customer", 20)].update(lockStatus=False, dealStatus=False, ownerTime=1)
        self.records[("crm/contract", 40)] = {"id": 40, "name": "合成合同", "customerId": 20}
        self.grants = [{"id": 1, "userId": 9, "level": 1}, {"id": 2, "userId": 12, "level": 2}]
        self.plans = []
        self.receivers = [11, 12]
        self.corrupt = False
        self.dictionary += [
            {"dictType": "crm_receivable_return_type", "label": "转账", "value": "2"},
            {"dictType": "oa_announcement_type", "label": "通知", "value": "2"},
            {"dictType": "oa_priority", "label": "一般", "value": "1"}]

    def get(self, path, **params):
        if path == "crm/customer/page":
            rows = [r for (entity, _), r in self.records.items() if entity == "crm/customer" and
                (r["ownerUserId"] is None) == params["pool"] and params["name"] in r["name"]]
            return {"list": copy.deepcopy(rows), "total": len(rows)}
        if path == "crm/contact/page-by-customer":
            rows = [r for (entity, _), r in self.records.items() if entity == "crm/contact" and r["customerId"] == params["customerId"]]
            return {"list": copy.deepcopy(rows), "total": len(rows)}
        if path == "crm/permission/list": return copy.deepcopy(self.grants)
        if path == "crm/receivable-plan/page-by-customer": return {"list": copy.deepcopy(self.plans), "total": len(self.plans)}
        if path == "system/dept/list": return [{"id": 6, "parentId": 0, "leaderUserId": 9}, {"id": 8, "parentId": 6, "leaderUserId": None}]
        if path == "system/user/page":
            return {"list": [{"id": 9, "deptId": 6}, {"id": 11, "deptId": 6}, {"id": 12, "deptId": 8}], "total": 3}
        return super().get(path, **params)

    def write_once(self, method, path, *, body, params=None, operation=""):
        if path.startswith("crm/customer/"):
            self.writes.append((method, path, copy.deepcopy(body), copy.deepcopy(params)))
            row = self.records[("crm/customer", 20)]
            if path.endswith("/lock"): row["lockStatus"] = body["lockStatus"]
            if path.endswith("/update-deal-status"): row["dealStatus"] = params["dealStatus"]
            if path.endswith("/put-pool"):
                row["ownerUserId"] = None
                if not self.corrupt:
                    self.records[("crm/contact", 10)]["ownerUserId"] = None
                self.grants = [g for g in self.grants if g["level"] != 1]
            if path.endswith("/receive"):
                row.update(ownerUserId=9, ownerTime=2)
                self.grants.append({"id": 3, "userId": 9, "level": 1})
            return True
        result = super().write_once(method, path, body=body)
        if path == "crm/receivable-plan/create":
            row = self.records[("crm/receivable-plan", result)]
            row.update(customerId=20, period=1, remindTime=body["returnTime"] - body["remindDays"] * 86400000)
        if path == "oa/announcement/create":
            row = self.records[("oa/announcement", result)]
            row.update(publisherUserId=9, receiverUserIds=[] if self.corrupt else self.receivers)
        return result


class SpecialWriteTests(unittest.TestCase):
    def setUp(self):
        self.client = SpecialClient()
        self.tools = native_tools(self.client, Data(self.client))

    def prepare(self, operation, inputs):
        spec = self.tools.prepare_mutation(operation, inputs, context=context())
        spec.effect_projection.require(self.tools.get(operation).effect_contract.project_inputs(inputs))
        return spec

    def complete(self, operation, inputs):
        spec = self.prepare(operation, inputs)
        result = self.tools.execute_mutation(operation, spec.args, context=context())
        spec.effect_projection.require_receipt(result.content)
        return spec, result

    def test_pool_and_receive_verify_compound_ownership_and_permissions(self):
        spec, result = self.complete("crm_customer_put_pool", {"target": "合成星河"})
        self.assertIn("关联联系人负责人", [d.label for d in spec.details])
        self.assertIsNone(self.client.records[("crm/contact", 10)]["ownerUserId"])
        self.complete("crm_customer_receive", {"target": "合成星河", "owner": "合成员工"})
        self.assertIsNone(self.client.records[("crm/contact", 10)]["ownerUserId"])
        self.assertEqual(self.client.records[("crm/customer", 20)]["ownerUserId"], 9)
        self.assertEqual(len(self.client.writes), 2)

    def test_state_operations_use_reviewed_body_or_query_and_no_repeat_same_state(self):
        self.complete("crm_customer_lock", {"target": "合成星河", "lockStatus": True})
        self.complete("crm_customer_deal_status", {"target": "合成星河", "dealStatus": True})
        self.assertEqual(self.client.writes[0][2:], ({"id": 20, "lockStatus": True}, {}))
        self.assertEqual(self.client.writes[1][2:], ({}, {"id": 20, "dealStatus": True}))
        with self.assertRaises(BusinessToolRequestError):
            self.prepare("crm_customer_deal_status", {"target": "合成星河", "dealStatus": True})
        self.assertEqual(len(self.client.writes), 2)

    def test_related_set_or_state_drift_stops_before_dispatch(self):
        spec = self.prepare("crm_customer_put_pool", {"target": "合成星河"})
        self.client.records[("crm/contact", 10)]["ownerUserId"] = 12
        with self.assertRaises(BusinessMutationRefusedError):
            self.tools.execute_mutation("crm_customer_put_pool", spec.args, context=context())
        self.assertFalse(self.client.writes)

    def test_partial_pool_effect_is_indeterminate_after_one_dispatch(self):
        self.client.corrupt = True
        spec = self.prepare("crm_customer_put_pool", {"target": "合成星河"})
        with self.assertRaises(BusinessMutationIndeterminate):
            self.tools.execute_mutation("crm_customer_put_pool", spec.args, context=context())
        self.assertEqual(len(self.client.writes), 1)

    def plan(self):
        return {"contract": "合成合同", "owner": "合成员工", "returnTime": "2026-10-18 09:00:00",
                "price": 199.25, "returnType": "转账", "remindDays": 2, "remark": "完整备注"}

    def test_plan_binds_contract_owner_and_derives_period_reminder_without_receivable(self):
        spec, result = self.complete("crm_receivable_plan_create", self.plan())
        self.assertEqual(spec.args["body"]["price"], 199.25)
        self.assertNotIn("customerId", spec.args["body"])
        self.assertEqual({e["effect"] for e in result.content["effects"]}, {"record", "plan_derivation", "owner_permission"})

    def test_plan_period_drift_stops_before_dispatch(self):
        spec = self.prepare("crm_receivable_plan_create", self.plan())
        self.client.plans.append({"id": 51, "contractId": 40, "period": 1})
        with self.assertRaises(BusinessMutationRefusedError):
            self.tools.execute_mutation("crm_receivable_plan_create", spec.args, context=context())
        self.assertFalse(self.client.writes)

    def announcement(self):
        return {"title": "合成公告", "content": "全文\n保留；", "type": "通知", "priority": "一般",
                "top": False, "audience": "原生下属规则"}

    def test_announcement_binds_dynamic_rule_and_verifies_receiver_relationships(self):
        spec, result = self.complete("oa_announcement_create", self.announcement())
        self.assertNotIn("audience", spec.args["body"])
        self.assertEqual(result.content["effects"][-1]["count"], 2)
        self.assertTrue(any("不是固定名单" in d.text for d in spec.details))
        with self.assertRaises(BusinessToolRequestError):
            self.prepare("oa_announcement_create", {**self.announcement(), "audience": "指定同事"})

    def test_missing_receiver_effect_remains_indeterminate(self):
        self.client.corrupt = True
        spec = self.prepare("oa_announcement_create", self.announcement())
        with self.assertRaises(BusinessMutationIndeterminate):
            self.tools.execute_mutation("oa_announcement_create", spec.args, context=context())
        self.assertEqual(len(self.client.writes), 1)

    def test_incomplete_native_list_is_not_absence_or_success(self):
        class Partial:
            def get(self, *args, **kwargs): return {"list": [], "total": 1}
        with self.assertRaises(BusinessToolRequestError):
            read_pages(Partial(), "synthetic/page")


if __name__ == "__main__": unittest.main()
