"""Phase 51 Gate G0 offline unit tests for Yudao write loop and table verification."""
from __future__ import annotations

from contextlib import nullcontext
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
import time
import unittest
from unittest.mock import patch

from web_operation_agent.business.approvals import ApprovalWatch
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.errors import BusinessMutationRefusedError, BusinessToolRequestError
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.execution.turn_tables import TurnTables
from web_operation_agent.goal.skill_writes import SkillWrites
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS
from web_operation_agent.schemas.browser import ElementRef, PageObservation
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent_yudao.executor.actor_knowledge import (
    ActorKnowledge,
    _cell_values,
    _entity_slug_for_path,
)
from web_operation_agent_yudao.executor.domain_toolpacks import YUDAO_DEFERRED_DOM_TOOLS, is_yudao_hub_route
from web_operation_agent_yudao.executor.page_prompts import _words
from web_operation_agent_yudao.executor.slim_loop import _STATUS_READ_TOOLS, YudaoLoopProcedures, YudaoTurnExecutor
from web_operation_agent_yudao.goal_compat.page_goals import NativePageGoalPort
from web_operation_agent_yudao.host.application import YudaoApplication
from web_operation_agent_yudao.reads.bound_data_reads import turn_actor_reads
from web_operation_agent_yudao.reads.client import YudaoReadRefused
from web_operation_agent_yudao.writes.convention_writes import YudaoConventionWritePort
from web_operation_agent_yudao.writes.native_submit import YudaoSubmitPort

from .slim_loop_support import DOCS as BASE_DOCS


def _make_principal(scopes=("yudao.read", "yudao.write")):
    now = int(time.time() * 1000)
    return PrincipalContext(
        schema_version=1,
        application_id="yudao",
        tenant_id="1",
        workspace_id="synthetic",
        actor_id="257",
        delegation_id="g0-unit",
        issued_at=now - 1000,
        expires_at=now + 60_000,
        scopes=scopes,
    )


class _DummyChannel:
    def __init__(self, page_url: str, principal: PrincipalContext, obs: PageObservation | None = None):
        self.page_url = page_url
        self.principal = principal
        self.read_only_mode = True
        self.latest_observation_envelope = (
            NS(
                observation_id="obs-1",
                page_epoch=obs.epoch,
                observation=obs,
            )
            if obs is not None
            else None
        )

    def publish(self, *args, **kwargs):
        return None


class _MockWriteClient:
    def __init__(self):
        self.read_generation = 1
        self.tenant = "1"
        self.calls: list[tuple[str, dict]] = []
        self.contracts = [
            {"id": 101, "name": "2026框架协议", "no": "HT-101"},
            {"id": 102, "name": "备用协议", "no": "HT-102"},
        ]
        self.plans = [
            {"id": 501, "contractId": 101, "period": 1, "price": 50000},
            {"id": 502, "contractId": 102, "period": 1, "price": 30000},
            {"id": 503, "contractId": 101, "period": 2, "price": 50000},
        ]

    def get(self, path, **params):
        self.calls.append((path, dict(params)))
        if path == "system/auth/get-permission-info":
            return {"user": {"id": 257, "nickname": "测试员", "deptId": None}, "menus": []}
        if path == "crm/contract/page":
            return {"list": list(self.contracts), "total": len(self.contracts)}
        if path == "crm/receivable-plan/page":
            cid = params.get("contractId")
            rows = [p for p in self.plans if cid is None or p["contractId"] == cid]
            return {"list": rows, "total": len(rows)}
        if path == "crm/receivable-plan/get":
            pid = params.get("id")
            return next((p for p in self.plans if str(p["id"]) == str(pid)), None)
        return []

    def read_window(self, _check):
        return nullcontext()


class Phase51G0Tests(unittest.TestCase):
    def test_resident_convention_skill_silently_loaded_and_select_compatible(self):
        client = _MockWriteClient()
        docs = {"paths": {}, "components": {"schemas": {}}}
        ro_app = YudaoApplication("https://app.test", None, docs, client=client, native_writes=False)
        sel = ro_app.business_tools.select("查看客户列表", allow_mutations=False)
        self.assertGreaterEqual(len(sel.skills), 25)

        rw_app = YudaoApplication("https://app.test", None, docs, client=client, native_writes=True)
        self.assertEqual(
            rw_app.ports.operations.default_loaded_skill_ids,
            frozenset({"yudao.convention-record-mutation"}),
        )
        with tempfile.TemporaryDirectory() as td:
            ledger = SessionLedger(Path(td) / "audit.jsonl", "run")
            approvals = ApprovalWatch(
                ledger=ledger,
                run_id="run",
                principal=_make_principal(),
                session_id="s1",
                turn_id="t1",
                business_approval=lambda *_a, **_k: None,
                cancelled=lambda: False,
            )
            writes = SkillWrites(rw_app, approvals)
            self.assertEqual(writes.loaded_ids, {"yudao.convention-record-mutation"})
            self.assertEqual(
                {d.name for d in writes.resident_definitions()},
                {"create_record", "update_record", "toggle_record_status", "assign_record_relations"},
            )
            self.assertEqual(writes.dynamic_definitions(), ())
            self.assertEqual([e["event"] for e in ledger.events if e["event"] == "business_skill_loaded"], [])

    def test_hub_deferral_prefix_stable_tools_routing_and_switch_domain(self):
        client = _MockWriteClient()
        app = YudaoApplication("https://app.test", None, BASE_DOCS, client=client, native_writes=True)
        bound_app = bind_application(app)
        hub_obs = PageObservation(
            1, 1, "https://app.test/#/index", "工作台", [], [ElementRef("e0", "link", "首页", "a", 1, {})], content="[0]<a href=/index>首页 />"
        )
        sub_obs = PageObservation(
            1, 2, "https://app.test/#/crm/contract", "合同管理", [], [ElementRef("e0", "button", "新建", "button", 2, {})], content="[0]<button>新建 />"
        )
        self.assertTrue(is_yudao_hub_route(hub_obs))
        self.assertFalse(is_yudao_hub_route(sub_obs))
        principal = _make_principal()
        channel = _DummyChannel("https://app.test/#/index", principal, hub_obs)
        chat = NS(
            provider="synthetic",
            model="scripted",
            complete=lambda *_a, **_k: {"role": "assistant", "content": ""},
            complete_cancellable=lambda *_a, **_k: {"role": "assistant", "content": ""},
        )
        goal_port = NativePageGoalPort(app.goal_data)
        orig_caps = goal_port.capabilities
        goal_port.capabilities = lambda: orig_caps() | frozenset({"handoffs"})
        with tempfile.TemporaryDirectory() as td:
            executor = YudaoTurnExecutor(
                channel=channel,
                chat=chat,
                config=AgentTurnConfig(
                    supported_scope="Yudao",
                    ledger_path=Path(td) / "audit.jsonl",
                    require_identity=True,
                    deployment_profile="test",
                    allow_business_mutations=True,
                ),
                application=bound_app,
                goal_port=goal_port,
                knowledge=ActorKnowledge(client, BASE_DOCS),
                control=ReadOnlyControl(),
                native_submit=NS(),
                verify=False,
                run_id_factory=lambda _m: "g0-exec",
            )
            instr = executor.profile.instructions
            self.assertIn("路 1（API 写入优先，严禁未请求预填时滑向网页表单）", instr)
            self.assertIn("路 2（单据审批与表格行内无表单按钮）", instr)
            self.assertIn("路 3（安全边界与未覆盖草稿预填交接）", instr)
            self.assertIn("在用户未明示草稿预填时严禁滑向网页表单预填冒充完成", instr)
            self.assertNotIn("Skill 自身失败不会阻碍你改用页面表单完成预填交接", instr)
            self.assertIn("switch_domain", _STATUS_READ_TOOLS)

            with tempfile.TemporaryDirectory() as td2:
                ledger = SessionLedger(Path(td2) / "a.jsonl", "run")
                approvals = ApprovalWatch(
                    ledger=ledger,
                    run_id="run",
                    principal=principal,
                    session_id="s1",
                    turn_id="t1",
                    business_approval=lambda *_a, **_k: None,
                    cancelled=lambda: False,
                )
                writes = SkillWrites(app, approvals)
                turn = {"writes": writes, "obs": hub_obs, "sources": None, "skills": set(), "step": 0}
                hub_tools = [t["function"]["name"] for t in executor._tools(turn)]
                for dom_tool in YUDAO_DEFERRED_DOM_TOOLS:
                    self.assertNotIn(dom_tool, hub_tools)
                self.assertIn("goto", hub_tools)
                self.assertIn("create_record", hub_tools)
                self.assertIn("switch_domain", hub_tools)

                fake_state = NS(obs=hub_obs)
                deferred_res = executor._invoke_tool(fake_state, "click", {"index": 0}, turn)
                self.assertEqual(deferred_res.error_code, "hub_route_deferred")

                turn["obs"] = sub_obs
                sub_tools = [t["function"]["name"] for t in executor._tools(turn)]
                fin_idx = hub_tools.index("finish")
                self.assertEqual(sub_tools[: fin_idx + 1], hub_tools[: fin_idx + 1])
                tail = set(sub_tools[fin_idx + 1 :])
                self.assertEqual(tail, {"click", "fill_fields", "input", "select", "scroll", "highlight"})

            # switch_domain pins domain pack and _words strips injected domain contract block
            self.assertEqual(
                _words("新增一条合同回款\n\n当前激活业务域契约（仅覆盖本轮命中域；跨域时可调 switch_domain 追加）：\n- [crm]"),
                "新增一条合同回款",
            )
            fake_ctx_turn: dict = {}
            executor.turn_context.executor_turn = fake_ctx_turn
            with patch.object(executor.procedures, "_note"):
                s = NS(operation=NS(principal=principal), measure=lambda _k: nullcontext(), progress=lambda _f: None)
                sw = executor.procedures._tool(s, "switch_domain", {"domain": "erp"}, fake_ctx_turn)
            self.assertEqual(sw.state, "completed")
            self.assertIn("erp_document_status", sw.feedback)
            self.assertIn("erp_product_unit_create", sw.feedback)
            self.assertEqual(fake_ctx_turn["pinned_domains"], ["erp_wms"])

            # Fuzzy load_skill resolves dashed domain tokens into active domain write contract
            with patch.object(executor, "_note"):
                fu = executor._invoke_tool(
                    NS(operation=NS(principal=principal), obs=hub_obs, step=0, measure=lambda _k: nullcontext(), progress=lambda _f: None),
                    "load_skill",
                    {"skill": "hrm-salary"},
                    turn,
                )
            self.assertEqual(fu.state, "completed")
            self.assertIn("hrm/salary/employee-info", fu.feedback)
            self.assertIn("hrm", turn.get("pinned_domains", []))

    def test_search_api_write_guidance_and_programmatic_domain_packs(self):
        from web_operation_agent_yudao.executor.domain_toolpacks import (
            YUDAO_DOMAIN_PACKS,
            active_domain_contract_block,
        )
        from web_operation_agent.goal.domain_packs import select_active_domains

        docs = {
            "paths": {
                "/admin-api/crm/receivable/create": {
                    "post": {
                        "tags": ["管理后台 - CRM 回款"],
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/RecCreate"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/crm/receivable/get": {
                    "get": {
                        "tags": ["管理后台 - CRM 回款"],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/RecGetResp"}}}}},
                    }
                },
                "/admin-api/crm/receivable/page": {
                    "get": {
                        "tags": ["管理后台 - CRM 回款"],
                        "summary": "分页查询回款",
                        "parameters": [{"name": "pageNo", "in": "query"}, {"name": "pageSize", "in": "query"}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PageResp"}}}}},
                    }
                },
                "/admin-api/hrm/employee/create": {
                    "post": {
                        "tags": ["管理后台 - HRM 员工档案"],
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/EmpCreate"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/hrm/employee/get": {
                    "get": {
                        "tags": ["管理后台 - HRM 员工档案"],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/EmpGetResp"}}}}},
                    }
                },
                "/admin-api/system/user/create": {
                    "post": {
                        "tags": ["管理后台 - 用户"],
                        "summary": "新增用户",
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/UserCreate"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/system/user/get": {
                    "get": {
                        "tags": ["管理后台 - 用户"],
                        "summary": "获得用户详情",
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/UserGetResp"}}}}},
                    }
                },
                "/admin-api/erp/stock-check/update": {
                    "put": {
                        "tags": ["管理后台 - ERP 库存调拨单"],
                        "summary": "更新库存盘点单",
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/StockCheckUpdate"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                    }
                },
                "/admin-api/erp/stock-check/get": {
                    "get": {
                        "tags": ["管理后台 - ERP 库存调拨单"],
                        "summary": "获得库存盘点单",
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/StockCheckGetResp"}}}}},
                    }
                },
                "/admin-api/erp/stock-check/page": {
                    "get": {
                        "tags": ["管理后台 - ERP 库存调拨单"],
                        "summary": "获得库存盘点单分页",
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PageResp"}}}}},
                    }
                },
            },
            "components": {
                "schemas": {
                    "CommonResultLong": {"properties": {"code": {"type": "integer"}, "data": {"type": "integer", "format": "int64"}}},
                    "CommonResultBoolean": {"properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                    "PageResp": {"properties": {"data": {"type": "object"}}},
                    "RecGetResp": {"properties": {"data": {"$ref": "#/components/schemas/RecData"}}},
                    "RecData": {"properties": {"id": {"type": "integer"}, "contractId": {"type": "integer"}, "price": {"type": "number"}, "returnTime": {"type": "string"}}},
                    "RecCreate": {
                        "required": ["contractId", "price", "returnTime"],
                        "properties": {
                            "contractId": {"type": "integer", "description": "合同ID"},
                            "price": {"type": "number", "description": "回款金额"},
                            "returnTime": {"type": "string", "description": "回款日期"},
                        },
                    },
                    "EmpGetResp": {"properties": {"data": {"$ref": "#/components/schemas/EmpData"}}},
                    "EmpData": {"properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "mobile": {"type": "string"}, "entryTime": {"type": "string"}}},
                    "EmpCreate": {
                        "required": ["name", "mobile", "entryTime"],
                        "properties": {
                            "name": {"type": "string", "description": "员工姓名"},
                            "mobile": {"type": "string", "description": "手机号"},
                            "entryTime": {"type": "string", "description": "入职时间"},
                        },
                    },
                    "UserGetResp": {"properties": {"data": {"$ref": "#/components/schemas/UserData"}}},
                    "UserData": {"properties": {"id": {"type": "integer"}, "username": {"type": "string"}, "nickname": {"type": "string"}}},
                    "UserCreate": {
                        "required": ["username", "nickname", "password"],
                        "properties": {
                            "username": {"type": "string"},
                            "nickname": {"type": "string"},
                            "password": {"type": "string"},
                        },
                    },
                    "StockCheckGetResp": {"properties": {"data": {"$ref": "#/components/schemas/StockCheckData"}}},
                    "StockCheckData": {"properties": {"id": {"type": "integer"}, "no": {"type": "string"}, "checkTime": {"type": "string"}, "remark": {"type": "string"}}},
                    "StockCheckUpdate": {
                        "required": ["id", "checkTime"],
                        "properties": {
                            "id": {"type": "integer"},
                            "no": {"type": "string"},
                            "checkTime": {"type": "string"},
                            "remark": {"type": "string"},
                        },
                    },
                }
            },
        }
        client = _MockWriteClient()
        know = ActorKnowledge(client, docs)
        # include_writes=False preserves legacy read-only behavior and never emits write contracts
        res_ro = know.search_api("回款 创建", include_writes=False)
        self.assertIn("GET /admin-api/crm/receivable/page", res_ro)
        self.assertNotIn("[写入契约]", res_ro)
        self.assertIn("没有匹配的只读接口", know.search_api("不存在的接口xyz", include_writes=False))
        # include_writes=True discovers compiled create_record entities even if query contains create verb
        res_rec = know.search_api("回款 创建", include_writes=True)
        self.assertIn("[写入契约]", res_rec)
        self.assertIn('create_record(entity="crm/receivable"', res_rec)
        res_emp = know.search_api("员工 新建", include_writes=True)
        self.assertIn('create_record(entity="hrm/employee"', res_emp)
        res_user = know.search_api("用户 新建", include_writes=True)
        self.assertIn("[写入边界] system/user", res_user)
        res_chk = know.search_api("盘点单 修改", include_writes=True)
        self.assertIn('update_record(entity="erp/stock-check"', res_chk)

        # Co-activation of oa_bpm + hrm + system_infra & 8 domains structure
        self.assertEqual(
            [p.domain_id for p in YUDAO_DOMAIN_PACKS],
            ["crm", "erp_wms", "mall_promotion", "fms_pay", "hrm", "oa_bpm", "mes_iot", "system_infra"],
        )
        co_hrm = select_active_domains(YUDAO_DOMAIN_PACKS, page_url="https://app.test/#/index", prompt="请假并查员工档案")
        self.assertEqual({p.domain_id for p in co_hrm}, {"oa_bpm", "hrm", "system_infra"})
        block = active_domain_contract_block(page_url="https://app.test/#/index", prompt="新建一个员工档案", docs=docs)
        self.assertIn("hrm/employee(name,mobile,entryTime)", block)
        self.assertIn("oa_bpm", block)
        self.assertIn("system_infra", block)

    def test_native_submit_get_detail_fallback(self):
        class FallbackClient:
            def __init__(self):
                self.calls = []

            def get(self, path, **params):
                self.calls.append((path, dict(params)))
                if path.endswith("/get"):
                    raise YudaoReadRefused("404", code=404)
                if path.endswith("/get-detail"):
                    return {"id": 11, "name": "Detail Row"}
                return None

        c = FallbackClient()
        port = YudaoSubmitPort(c, NS(), lambda: {"paths": {}})
        rec = port.read_record(("/admin-api/crm/contract/",), 11)
        self.assertEqual(rec, {"id": 11, "name": "Detail Row"})
        self.assertEqual([p for p, _ in c.calls], ["crm/contract/get", "crm/contract/get-detail"])

        class ForbiddenClient:
            def get(self, path, **params):
                raise YudaoReadRefused("forbidden", code=403)

        port403 = YudaoSubmitPort(ForbiddenClient(), NS(), lambda: {"paths": {}})
        with self.assertRaises(YudaoReadRefused):
            port403.read_record(("/admin-api/crm/contract/",), 11)

    def test_resolve_single_ref_period_parent_fk_verified_id_and_recheck_bindings(self):
        client = _MockWriteClient()
        docs = {
            "paths": {
                "/admin-api/crm/contract/page": {
                    "get": {"parameters": [{"name": "pageNo", "in": "query"}, {"name": "pageSize", "in": "query"}]}
                },
                "/admin-api/crm/receivable-plan/page": {
                    "get": {
                        "parameters": [
                            {"name": "pageNo", "in": "query"},
                            {"name": "pageSize", "in": "query"},
                            {"name": "contractId", "in": "query"},
                        ]
                    }
                },
            },
            "components": {"schemas": {}},
        }
        port = YudaoConventionWritePort(client, NS(_documents=docs))

        # 1. Parent-FK narrowed ordinal period resolution
        rid, lbl, binding = port._resolve_single_ref("crm/receivable-plan", "第一期", query_ctx={"contractId": 101})
        self.assertEqual(rid, 501)
        self.assertEqual(lbl, "第1期")
        self.assertEqual(binding.get("expected_fields"), {"contractId": 101, "period": 1})

        # 2. Qualified Parent/Child token without query_ctx
        rid2, lbl2, _ = port._resolve_single_ref("crm/receivable-plan", "2026框架协议/第2期")
        self.assertEqual((rid2, lbl2), (503, "第2期"))

        # 3. Unverified bare numeric ID still refused, verified turn ID accepted
        with self.assertRaises(BusinessToolRequestError):
            port._resolve_single_ref("crm/receivable-plan", "501")

        ctx = NS(executor_turn={"sources": None})
        know = NS(turn_targets={"/admin-api/crm/receivable-plan": {"501"}})
        with turn_actor_reads(client, turn_context=ctx, knowledge=know):
            rid3, lbl3, _ = port._resolve_single_ref("crm/receivable-plan", "501", query_ctx={"contractId": 101})
            self.assertEqual((rid3, lbl3), (501, "第1期"))

        # 4. recheck_bindings checks expected_fields when entity lacks name keys
        port.recheck_bindings([binding], actor_id="257")
        client.plans[0]["period"] = 99
        with self.assertRaises(BusinessMutationRefusedError):
            port.recheck_bindings([binding], actor_id="257")

    def test_actor_knowledge_entity_slug_ties_zero_row_and_sql_order_summary(self):
        self.assertEqual(_entity_slug_for_path("/admin-api/crm/receivable-plan/page"), "crm/receivable-plan")
        cands, _ = _cell_values([{"id": 501, "period": 1, "price": 200}], entity_slug="crm/receivable-plan")
        self.assertIn(("crm/receivable-plan", 501), cands)
        self.assertIn(("crm/receivable-plan", 1), cands)

        class ReadClient:
            def __init__(self, rows):
                self.rows = rows

            def get(self, path, **params):
                if path == "erp/sale/page":
                    return {"list": list(self.rows), "total": len(self.rows)}
                return {}

            def read_window(self, _check):
                return nullcontext()

        docs = {
            "paths": {
                "/admin-api/erp/sale/page": {
                    "get": {
                        "summary": "销售单",
                        "tags": ["ERP"],
                        "parameters": [
                            {"name": "no", "in": "query"},
                            {"name": "pageNo", "in": "query", "required": True},
                            {"name": "pageSize", "in": "query", "required": True},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PageResp"}}}}},
                    }
                }
            },
            "components": {
                "schemas": {
                    "PageResp": {"properties": {"data": {"$ref": "#/components/schemas/Page"}}},
                    "Page": {"properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/Sale"}}}},
                    "Sale": {"properties": {"id": {}, "no": {"description": "单号"}, "amount": {"description": "金额"}}},
                }
            },
        }
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables.close)
        know = ActorKnowledge(ReadClient([
            {"id": 1, "no": "S1", "amount": 500},
            {"id": 2, "no": "S2", "amount": 500},
            {"id": 3, "no": "S3", "amount": 100},
        ]), docs)
        text = know.call_api("erp/sale/page", {"pageNo": 1, "pageSize": 20}, row_sink=tables.add)
        self.assertIn("内联全表同值统计（共 3 行）：amount：最大值 500 共 2 行同值。", text)
        self.assertNotIn("必须", text)

        proc = YudaoLoopProcedures(know, NS(capabilities=lambda: frozenset()), "https://app.test")
        proc.turn_context.turn_tables = (tables, "2026-10-25", "Asia/Shanghai")
        sql_res = proc._tool(NS(operation=NS(require_live=lambda **_k: None)), "sql", {"query": "SELECT id, amount FROM t1 ORDER BY amount DESC"}, {})
        self.assertIn("排序首键统计：", sql_res.feedback)
        self.assertIn("前 2 行同为 500", sql_res.feedback)

        know_empty = ActorKnowledge(ReadClient([]), docs)
        tables2 = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables2.close)
        empty_text = know_empty.call_api("erp/sale/page", {"no": "NOPE", "pageNo": 1, "pageSize": 20}, row_sink=tables2.add)
        self.assertIn("精确绑定空集核实（complete=True，0 行）", empty_text)


if __name__ == "__main__":
    unittest.main()
