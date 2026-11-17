"""Tests for Phase 47 Step 2: domain playbooks, source rules tool, and new typed writes."""
from __future__ import annotations

from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[4] / "src")]

from web_operation_agent.business.errors import (
    BusinessMutationIndeterminate,
    BusinessMutationRefusedError,
    BusinessToolPermissionError,
    BusinessToolReferenceError,
)
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexRule
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent_yudao.skill_packages import domain_playbooks, packaged_skills
from web_operation_agent_yudao.slim_loop import (
     SlimTurnExecutor,
    TABLE_READ_RULES,
    YudaoLoopProcedures,
    _clean_user_answer,
    _requires_page_delivery,
    system_text,
)
from web_operation_agent_yudao.writes import native_tools


class FakeClient:
    tenant = "1"

    def __init__(self, records=None):
        self.records = records or {}
        self.writes = []
        self.raise_indeterminate_once = False

    def get(self, path, **params):
        if path == "system/auth/get-permission-info":
            return {"user": {"id": 1, "nickname": "Admin"}, "roles": ["super_admin"]}
        if path == "system/dict-data/simple-list":
            return []
        if path == "system/dept/simple-list":
            return list(self.records.get("system/dept", {}).values())
        if path == "system/user/simple-list":
            return [{"id": 1, "nickname": "Admin", "deptId": 100}]
        if path.endswith("/page"):
            entity = path[:-len("/page")]
            rows = [r for r in self.records.get(entity, {}).values()]
            if "no" in params:
                rows = [r for r in rows if params["no"] in str(r.get("no", ""))]
            if "name" in params:
                rows = [r for r in rows if params["name"] in str(r.get("name", ""))]
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
            if self.raise_indeterminate_once:
                self.raise_indeterminate_once = False
                raise BusinessMutationIndeterminate("network timeout after server commit", phase="dispatch")
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
    now = int(time.time() * 1000)
    return OperationContext(
        principal=PrincipalContext(1, "yudao", "1", "1", "1", "del-1", now, now + 300_000, ("yudao.read", "yudao.write")),
        operation_id="op-1", turn_id="turn-1", session_id="sess-1", issued_at=now, expires_at=now + 300_000,
    )


class Phase47Step2Tests(unittest.TestCase):
    def test_eight_domain_playbooks_exist_reference_only_real_tools_and_degrade_safely(self):
        books = domain_playbooks()
        self.assertEqual(set(books), {
            "crm-sales", "erp-trade-stock", "erp-finance", "mall-catalog-promo",
            "mall-trade-aftersale", "hrm-org-salary", "fms-accounting", "oa-bpm-system",
        })
        client = FakeClient()
        registry = native_tools(client, FakeGoalData(client), runtime_dictionary=False)
        real_ops = {t.name for t in registry.definitions}
        for domain, text in books.items():
            # Every snake_case write-tool token mentioned in backticks must exist in native_tools!
            mentioned_ops = set(re.findall(r"`([a-z]+_[a-z0-9_]+)`", text)) - {
                "document_type", "entity_rules", "search_api", "load_domain_guide", "call_api",
            }
            unknown = mentioned_ops - real_ops
            self.assertEqual(unknown, set(), f"{domain} references non-existent write ops: {unknown}")
            self.assertNotIn("hrm/recruit-post", text)
        self.assertIn("元（yuan）", books["crm-sales"])
        self.assertNotIn("单位均为**分", books["crm-sales"])
        self.assertIn("hrm/recruit/post", books["hrm-org-salary"])
        self.assertIn("hrm/salary/employee-info", books["hrm-org-salary"])
        self.assertIn("entity_rules", TABLE_READ_RULES)
        self.assertIn("load_domain_guide", TABLE_READ_RULES)

        # Missing directory degrades to {} instead of raising FileNotFoundError
        with patch("importlib.resources.files") as mock_files:
            mock_files.return_value.joinpath.return_value.is_dir.return_value = False
            self.assertEqual(domain_playbooks(), {})

    def test_entity_rules_truncation_filter_miss_and_compiled_rules_prompt_boundary(self):
        procedures = object.__new__(YudaoLoopProcedures)
        procedures.knowledge = NS()
        long_rules = tuple(
            IndexRule(f"r{i}", "refusal", "删除活动", f"拒绝原因{i}：" + ("规则详情" * 200),
                      subject="promotion/seckill-activity/delete")
            for i in range(20)
        )
        index = ApplicationIndex(entities=(
            IndexEntity("promotion/seckill-activity", "秒杀活动", rules=long_rules),
            IndexEntity("crm/contract", "合同", rules=long_rules),
            IndexEntity("erp/sale-order", "销售订单", rules=long_rules),
        ))
        procedures.goal_port = NS(index=lambda context=None: index)
        # Filter miss adds explicit note instead of silently returning unfiltered rules
        miss = procedures._tool(NS(operation=_ctx(), progress=lambda *a, **k: None),
                                "entity_rules", {"target": "秒杀活动", "operation": "不存在动作"}, {})
        self.assertIn("未命中操作词", miss.feedback)
        # Large multi-entity rule output stays bounded well below LoopToolResult 32K cap
        broad = procedures._tool(NS(operation=_ctx(), progress=lambda *a, **k: None),
                                 "entity_rules", {"target": "delete"}, {})
        self.assertLessEqual(len(broad.feedback), 12_500)

        # Prompt boundary: compiled_rules=True permits entity_rules while keeping code implementation D3 refusal
        no_src = system_text(user="u", date="2026-10-25", tz="Asia/Shanghai", runtime_source=False, compiled_rules=False)
        with_rules = system_text(user="u", date="2026-10-25", tz="Asia/Shanghai", runtime_source=False, compiled_rules=True)
        self.assertIn("源码规则或提交历史时，用固定答复", no_src)
        self.assertIn("entity_rules 查询编译期规则", with_rules)
        self.assertNotIn("源码规则或提交历史时，用固定答复", with_rules)

    def test_status_ops_side_effects_drift_ambiguity_indeterminate_recovery_and_dept_update(self):
        client = FakeClient({
            "erp/purchase-in": {
                11: {"id": 11, "no": "CGRK-001", "status": 10, "accountId": 998034, "totalPrice": 120},
                12: {"id": 12, "no": "CGRK-001-DUP", "status": 10, "accountId": 998034, "totalPrice": 80},
            },
            "promotion/coupon-template": {21: {"id": 21, "name": "新人券", "status": 0, "discountPrice": 1500, "usePrice": 10000}},
            "promotion/seckill-activity": {31: {"id": 31, "name": "双11秒杀", "status": 0, "spuId": 77}},
            "erp/account": {41: {"id": 41, "name": "招行基本户", "no": "A01", "sort": 1, "status": 0, "defaultStatus": True, "remark": "old"}},
            "system/dept": {
                100: {"id": 100, "name": "总公司", "parentId": 0, "sort": 1, "status": 0},
                101: {"id": 101, "name": "研发部", "parentId": 100, "sort": 2, "status": 0, "phone": "13800138000"},
            },
        })
        goal_data = FakeGoalData(client)
        registry = native_tools(client, goal_data, runtime_dictionary=False)
        skills = packaged_skills()
        self.assertTrue({op for s in skills.artifacts for op in s.write_operations} <= {t.name for t in registry.definitions})

        ctx = _ctx()
        # 1. Missing / substring-only no raises BusinessToolReferenceError with candidates
        with self.assertRaises(BusinessToolReferenceError) as missing_err:
            registry.prepare_mutation("erp_document_status", {"document_type": "采购入库单", "no": "CGRK", "action": "审批"}, context=ctx)
        self.assertIn("CGRK-001", missing_err.exception.choices)

        # 2. ERP document approve includes side_effect ApprovalDetail and recovers on indeterminate after commit
        spec = registry.prepare_mutation("erp_document_status", {"document_type": "采购入库单", "no": "CGRK-001", "action": "审批"}, context=ctx)
        self.assertEqual(spec.effect, "审批采购入库单「CGRK-001」")
        self.assertTrue(any(d.kind == "side_effect" and "库存" in d.text for d in spec.details))

        # Cross-operation replay is refused
        with self.assertRaises(BusinessToolPermissionError):
            registry.execute_mutation("promotion_seckill_activity_close", spec.args, context=ctx)

        # Indeterminate network error after backend commit recovers cleanly via same-actor readback
        client.raise_indeterminate_once = True
        res = registry.execute_mutation("erp_document_status", spec.args, context=ctx)
        self.assertTrue(res.content["verified"])
        self.assertEqual(client.records["erp/purchase-in"][11]["status"], 20)

        # Precondition drift: stale snapshot prepared when status was 10 refuses once status is 20
        with self.assertRaises(BusinessMutationRefusedError) as drift_err:
            registry.execute_mutation("erp_document_status", spec.args, context=ctx)
        self.assertEqual(drift_err.exception.reason, "precondition_changed")

        # 3. Seckill close includes side_effect detail
        spec3 = registry.prepare_mutation("promotion_seckill_activity_close", {"target": "双11秒杀"}, context=ctx)
        self.assertTrue(any(d.kind == "side_effect" for d in spec3.details))
        res3 = registry.execute_mutation("promotion_seckill_activity_close", spec3.args, context=ctx)
        self.assertTrue(res3.content["verified"])

        # 4. system_dept_update with root=True parent relation ("根节点")
        spec_dept = registry.prepare_mutation("system_dept_update", {
            "target": "研发部",
            "changes": {"parent": "根节点", "leader": "Admin", "sort": 5},
        }, context=ctx)
        res_dept = registry.execute_mutation("system_dept_update", spec_dept.args, context=ctx)
        self.assertTrue(res_dept.content["verified"])
        self.assertEqual(client.records["system/dept"][101]["parentId"], 0)
        self.assertEqual(client.records["system/dept"][101]["phone"], "13800138000")

        # 5. Un-approve ERP document with downstream write-off / shipment is refused before approval card
        client.records["erp/purchase-in"][11]["paymentPrice"] = 50
        with self.assertRaises(BusinessMutationRefusedError) as downstream_err:
            registry.prepare_mutation(
                "erp_document_status",
                {"document_type": "采购入库单", "no": "CGRK-001", "action": "反审批"},
                context=ctx,
            )
        self.assertEqual(downstream_err.exception.reason, "downstream_document_exists")

        # 6. erp_account_update projects before_json for append-safety checks
        spec_acc = registry.prepare_mutation(
            "erp_account_update",
            {"target": "招行基本户", "changes": {"remark": "old；限额五万"}},
            context=ctx,
        )
        self.assertIn('"remark":"old"', spec_acc.effect_projection.before_json)

    def test_dangerous_typed_ops_gate_negated_page_obligation_and_table_token_cleaning(self):
        from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS

        executor = object.__new__(SlimTurnExecutor)
        executor._earlier_words = []
        events = []
        state = NS(
            step=1,
            message=NS(text="把双11秒杀活动关闭"),
            ledger=NS(append=lambda event, **kw: events.append((event, kw))),
        )
        blocked = executor._propose(state, NS(), "promotion_seckill_activity_close", {"target": "双11秒杀"}, {})
        self.assertEqual((blocked.state, blocked.error_code), ("needs_input", "dangerous_unconfirmed"))

        blocked_unapprove = executor._propose(
            state, NS(), "erp_document_status",
            {"document_type": "采购入库单", "no": "CGRK-001", "action": "反审批"}, {},
        )
        self.assertEqual((blocked_unapprove.state, blocked_unapprove.error_code), ("needs_input", "dangerous_unconfirmed"))

        self.assertFalse(_requires_page_delivery("查一下本月成交合同金额，不用打开页面"))
        self.assertFalse(_requires_page_delivery("帮我看下待审核出库单，不要跳转到列表"))
        self.assertTrue(_requires_page_delivery("不用打开详情，直接在列表页面筛选未审核单据"))

        cleaned = _clean_user_answer("根据表 t1 与 `t2` 计算，合计 300 元。", BASELINE_SESSION_HANDOFFS)
        self.assertNotIn("t1", cleaned)
        self.assertNotIn("t2", cleaned)

    def test_erp_document_status_canonicalizes_dispatch_menu_alias(self):
        client = FakeClient({
            "erp/stock-move": {91: {"id": 91, "no": "QCDB20260001", "status": 10, "totalPrice": 1200}},
        })
        goal_data = FakeGoalData(client)
        registry = native_tools(client, goal_data, runtime_dictionary=False)
        spec = registry.prepare_mutation(
            "erp_document_status",
            {"document_type": "库存调度单", "no": "QCDB20260001", "action": "审批"},
            context=_ctx(),
        )
        self.assertEqual(spec.args["entity"], "erp/stock-move")
        self.assertIn("库存调拨单", spec.effect)
        res = registry.execute_mutation("erp_document_status", spec.args, context=_ctx())
        self.assertTrue(res.content["verified"])
        self.assertEqual(client.records["erp/stock-move"][91]["status"], 20)
