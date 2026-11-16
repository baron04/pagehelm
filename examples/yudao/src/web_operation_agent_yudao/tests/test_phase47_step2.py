"""Tests for Phase 47 Step 2: domain playbooks, source rules tool, and new typed writes."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

sys.path[:0] = [str(Path(__file__).resolve().parents[4] / "src")]

from web_operation_agent.business.errors import BusinessMutationRefusedError
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexRule
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent_yudao.skill_packages import domain_playbooks, packaged_skills
from web_operation_agent_yudao.slim_loop import YudaoLoopProcedures
from web_operation_agent_yudao.writes import native_tools


class FakeClient:
    tenant = "1"

    def __init__(self, records=None):
        self.records = records or {}
        self.writes = []

    def get(self, path, **params):
        if path == "system/auth/get-permission-info":
            return {"user": {"id": 1, "nickname": "Admin"}, "roles": ["super_admin"]}
        if path == "system/dict-data/simple-list":
            return []
        if path.endswith("/page"):
            entity = path[:-len("/page")]
            rows = [r for r in self.records.get(entity, {}).values()]
            if "no" in params:
                rows = [r for r in rows if r.get("no") == params["no"]]
            if "name" in params:
                rows = [r for r in rows if r.get("name") == params["name"]]
            return {"list": rows, "total": len(rows)}
        if path.endswith("/get"):
            entity = path[:-len("/get")]
            return dict(self.records[entity][params["id"]])
        raise AssertionError(f"unexpected GET {path} {params}")

    def write_once(self, method, path, *, body=None, params=None, operation=""):
        self.writes.append((method, path, body, params, operation))
        entity = path.rsplit("/", 1)[0]
        if path.endswith("/update-status") or path.endswith("/close"):
            rid = (params or {}).get("id") or (body or {}).get("id")
            new_status = 1 if path.endswith("/close") else ((params or {}).get("status") if params and "status" in params else body["status"])
            self.records[entity][rid]["status"] = new_status
            return True
        if path.endswith("/update"):
            rid = body["id"]
            self.records[entity][rid].update(body)
            return True
        if path.endswith("/create"):
            rid = 999
            self.records.setdefault(entity, {})[rid] = {"id": rid, **body}
            return rid
        raise AssertionError(f"unexpected write {method} {path}")


class FakeGoalData:
    time_zone = "Asia/Shanghai"

    def __init__(self, client):
        self.client = client
        self.invalidated = []

    def find(self, entity, text, *, limit=8):
        rows = self.client.records.get(entity, {}).values()
        return [NS(id=r["id"], name=r.get("name", ""), display=r.get("name", "")) for r in rows if r.get("name") == text]

    def invalidate_reads(self, entities):
        self.invalidated.append(entities)


def _ctx():
    import time
    now = int(time.time() * 1000)
    return OperationContext(
        principal=PrincipalContext(1, "yudao", "1", "1", "1", "del-1", now, now + 300_000, ("yudao.read", "yudao.write")),
        operation_id="op-1", turn_id="turn-1", session_id="sess-1", issued_at=now, expires_at=now + 300_000,
    )


class Phase47Step2Tests(unittest.TestCase):
    def test_eight_domain_playbooks_exist_and_load_in_readonly_or_draft_turns(self):
        books = domain_playbooks()
        self.assertEqual(set(books), {
            "crm-sales", "erp-trade-stock", "erp-finance", "mall-catalog-promo",
            "mall-trade-aftersale", "hrm-org-salary", "fms-accounting", "oa-bpm-system",
        })
        procedures = object.__new__(YudaoLoopProcedures)
        procedures.knowledge = NS()
        procedures.goal_port = NS()
        res = procedures._tool(NS(operation=_ctx(), progress=lambda *a, **k: None),
                               "load_domain_guide", {"domain": "mall-catalog-promo"}, {})
        self.assertEqual(res.state, "completed")
        self.assertIn("分（fen）", res.feedback)

    def test_entity_rules_tool_surfaces_compiled_refusal_and_cascade_rules(self):
        procedures = object.__new__(YudaoLoopProcedures)
        procedures.knowledge = NS()
        index = ApplicationIndex(entities=(
            IndexEntity("promotion/seckill-activity", "秒杀活动", rules=(
                IndexRule("r1", "refusal", "删除秒杀活动", "秒杀活动未关闭或未结束，不能删除",
                          subject="promotion/seckill-activity/delete"),
                IndexRule("r2", "cascade", "删除秒杀活动", "连带改动：SeckillProduct.deleteByIds",
                          subject="promotion/seckill-activity/delete"),
            )),
        ))
        procedures.goal_port = NS(index=lambda context=None: index)
        res = procedures._tool(NS(operation=_ctx(), progress=lambda *a, **k: None),
                               "entity_rules", {"target": "秒杀活动", "operation": "删除"}, {})
        self.assertEqual(res.state, "completed")
        self.assertIn("秒杀活动未关闭或未结束，不能删除", res.feedback)
        self.assertIn("SeckillProduct.deleteByIds", res.feedback)

    def test_erp_document_status_and_promo_status_and_account_preserve_fields_and_readback(self):
        client = FakeClient({
            "erp/purchase-in": {11: {"id": 11, "no": "CGRK-001", "status": 10, "accountId": 998034, "totalPrice": 120}},
            "promotion/coupon-template": {21: {"id": 21, "name": "新人券", "status": 0, "discountPrice": 1500, "usePrice": 10000}},
            "promotion/seckill-activity": {31: {"id": 31, "name": "双11秒杀", "status": 0, "spuId": 77}},
            "erp/account": {41: {"id": 41, "name": "招行基本户", "no": "A01", "sort": 1, "status": 0, "defaultStatus": True, "remark": "old"}},
        })
        goal_data = FakeGoalData(client)
        registry = native_tools(client, goal_data, runtime_dictionary=False)
        skills = packaged_skills()
        self.assertTrue({op for s in skills.artifacts for op in s.write_operations} <= {t.name for t in registry.definitions})

        ctx = _ctx()
        # 1. ERP document approve
        spec = registry.prepare_mutation("erp_document_status", {"document_type": "采购入库单", "no": "CGRK-001", "action": "审批"}, context=ctx)
        self.assertEqual(spec.effect, "审批采购入库单「CGRK-001」")
        res = registry.execute_mutation("erp_document_status", spec.args, context=ctx)
        self.assertTrue(res.content["verified"])
        self.assertEqual(client.records["erp/purchase-in"][11]["status"], 20)
        self.assertEqual(client.records["erp/purchase-in"][11]["accountId"], 998034)
        # Re-approving an already approved doc refuses cleanly
        with self.assertRaises(BusinessMutationRefusedError):
            registry.prepare_mutation("erp_document_status", {"document_type": "采购入库单", "no": "CGRK-001", "action": "审批"}, context=ctx)

        # 2. Coupon template disable
        spec2 = registry.prepare_mutation("promotion_coupon_template_status", {"target": "新人券", "status": "关闭"}, context=ctx)
        res2 = registry.execute_mutation("promotion_coupon_template_status", spec2.args, context=ctx)
        self.assertTrue(res2.content["verified"])
        self.assertEqual(client.records["promotion/coupon-template"][21]["status"], 1)
        self.assertEqual(client.records["promotion/coupon-template"][21]["discountPrice"], 1500)

        # 3. Seckill close
        spec3 = registry.prepare_mutation("promotion_seckill_activity_close", {"target": "双11秒杀"}, context=ctx)
        res3 = registry.execute_mutation("promotion_seckill_activity_close", spec3.args, context=ctx)
        self.assertTrue(res3.content["verified"])
        self.assertEqual(client.records["promotion/seckill-activity"][31]["status"], 1)

        # 4. ERP account update preserves defaultStatus
        spec4 = registry.prepare_mutation("erp_account_update", {"target": "招行基本户", "changes": {"remark": "new"}}, context=ctx)
        res4 = registry.execute_mutation("erp_account_update", spec4.args, context=ctx)
        self.assertTrue(res4.content["verified"])
        self.assertIs(client.records["erp/account"][41]["defaultStatus"], True)
        self.assertEqual(client.records["erp/account"][41]["remark"], "new")
