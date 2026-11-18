"""Route-level regression tests verifying Yudao write routes share write_pipeline invariants."""
from __future__ import annotations

import copy
import time
import unittest

from web_operation_agent.business.errors import (
    BusinessMutationIndeterminate,
    BusinessMutationRefusedError,
)
from web_operation_agent.goal.data import Ref
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent_yudao.writes.writes import native_tools


def _ctx(actor_id: str = "9") -> OperationContext:
    now = int(time.time() * 1000)
    actor = PrincipalContext(
        1, "yudao", "1", "7", actor_id, "synthetic",
        now - 1000, now + 60000, ("yudao.read", "yudao.write"),
    )
    return OperationContext.create(actor, session_id="s", turn_id="t", operation_id="o", deadline_ms=now + 30000)


class _StubClient:
    tenant = "1"

    def __init__(self):
        self.records = {
            ("crm/contact", 10): {
                "id": 10,
                "name": "林舟",
                "customerId": 20,
                "ownerUserId": 9,
                "mobile": "13800000001",
                "remark": "原备注",
            },
            ("crm/customer", 20): {
                "id": 20,
                "name": "合成星河",
                "ownerUserId": 9,
                "lockStatus": False,
                "dealStatus": False,
            },
            ("erp/sale-order", 40): {
                "id": 40,
                "no": "XSDD20260001",
                "status": 10,
                "accountId": 1,
                "totalPrice": 100,
                "outCount": 0,
                "returnCount": 0,
            },
            ("promotion/coupon-template", 50): {
                "id": 50,
                "name": "满减券",
                "status": 0,
                "discountPrice": 500,
                "usePrice": 5000,
            },
        }
        self.writes = []
        self.readback_error: Exception | None = None
        self.dispatch_error: Exception | None = None

    def get(self, path, **params):
        if path == "system/auth/get-permission-info":
            return {"user": {"id": 9, "nickname": "合成员工"}, "roles": ["crm_admin"]}
        if path == "system/dict-data/simple-list":
            return []
        if path == "crm/permission/list":
            return [{"userId": 9, "level": 1}]
        if path == "crm/customer/page":
            rows = [
                copy.deepcopy(row)
                for (ent, _), row in self.records.items()
                if ent == "crm/customer" and row.get("name") == params.get("name")
            ]
            return {"list": rows, "total": len(rows)}
        if path == "erp/sale-order/page":
            rows = [
                copy.deepcopy(row)
                for (ent, _), row in self.records.items()
                if ent == "erp/sale-order" and row.get("no") == params.get("no")
            ]
            return {"list": rows, "total": len(rows)}
        if path == "promotion/coupon-template/page":
            rows = [
                copy.deepcopy(row)
                for (ent, _), row in self.records.items()
                if ent == "promotion/coupon-template" and row.get("name") == params.get("name")
            ]
            return {"list": rows, "total": len(rows)}
        if path.endswith("/get") and self.readback_error is not None and self.writes:
            raise self.readback_error
        return copy.deepcopy(self.records.get((path.removesuffix("/get"), int(params["id"]))))

    def write_once(self, method, path, *, body=None, params=None, operation=""):
        self.writes.append((method, path, copy.deepcopy(body), copy.deepcopy(params)))
        if self.dispatch_error is not None:
            raise self.dispatch_error
        if path == "crm/contact/update":
            self.records[("crm/contact", body["id"])].update(body)
            return True
        if path == "crm/customer/lock":
            self.records[("crm/customer", body["id"])]["lockStatus"] = body["lockStatus"]
            return True
        if path == "erp/sale-order/update-status":
            self.records[("erp/sale-order", params["id"])]["status"] = params["status"]
            return True
        if path == "promotion/coupon-template/update-status":
            self.records[("promotion/coupon-template", body["id"])]["status"] = body["status"]
            return True
        raise AssertionError(f"unexpected write: {method} {path}")


class _StubGoalData:
    def __init__(self, client: _StubClient):
        self.client = client
        self.invalidated = []

    def find(self, entity, name, *, limit):
        return tuple(
            Ref(entity, str(rid), row["name"], row["name"])
            for (kind, rid), row in self.client.records.items()
            if kind == entity and row.get("name") == name
        )

    def invalidate_reads(self, entities):
        self.invalidated.append(entities)


class YudaoWritePipelineRoutesTests(unittest.TestCase):
    def setUp(self):
        self.client = _StubClient()
        self.data = _StubGoalData(self.client)
        self.tools = native_tools(self.client, self.data)

    def test_status_operation_reread_drift_aborts_before_dispatch(self):
        spec = self.tools.prepare_mutation(
            "erp_document_status",
            {"document_type": "销售订单", "no": "XSDD20260001", "action": "审批"},
            context=_ctx(),
        )
        # Concurrent approval before dispatch
        self.client.records[("erp/sale-order", 40)]["status"] = 20
        with self.assertRaises(BusinessMutationRefusedError) as caught:
            self.tools.execute_mutation("erp_document_status", spec.args, context=_ctx())
        self.assertEqual(caught.exception.reason, "precondition_changed")
        self.assertEqual(self.client.writes, [])

    def test_status_operation_readback_timeout_is_indeterminate_with_no_retry(self):
        spec = self.tools.prepare_mutation(
            "promotion_coupon_template_status",
            {"target": "满减券", "status": "关闭"},
            context=_ctx(),
        )
        self.client.readback_error = TimeoutError("readback timeout")
        with self.assertRaises(BusinessMutationIndeterminate) as caught:
            self.tools.execute_mutation("promotion_coupon_template_status", spec.args, context=_ctx())
        self.assertEqual(caught.exception.phase, "postcondition")
        self.assertEqual(len(self.client.writes), 1)

    def test_special_state_op_preserves_unrelated_fields_and_recovers_on_lost_ack(self):
        spec = self.tools.prepare_mutation(
            "crm_customer_lock",
            {"target": "合成星河", "lockStatus": True},
            context=_ctx(),
        )
        orig_write = self.client.write_once

        def lost_ack(method, path, *, body=None, params=None, operation=""):
            orig_write(method, path, body=body, params=params, operation=operation)
            raise BusinessMutationIndeterminate("lost ack", phase="dispatch")

        self.client.write_once = lost_ack
        result = self.tools.execute_mutation("crm_customer_lock", spec.args, context=_ctx())
        self.assertTrue(result.content["verified"])
        self.assertEqual(len(self.client.writes), 1)
        self.assertEqual(self.client.records[("crm/customer", 20)]["dealStatus"], False)

    def test_native_write_readback_mismatch_never_retries(self):
        spec = self.tools.prepare_mutation(
            "crm_contact_update",
            {"target": "林舟", "changes": {"mobile": "13900000002"}},
            context=_ctx(),
        )
        self.client.dispatch_error = BusinessMutationIndeterminate("timeout", phase="dispatch")
        with self.assertRaises(BusinessMutationIndeterminate):
            self.tools.execute_mutation("crm_contact_update", spec.args, context=_ctx())
        self.assertEqual(len(self.client.writes), 1)


if __name__ == "__main__":
    unittest.main()
