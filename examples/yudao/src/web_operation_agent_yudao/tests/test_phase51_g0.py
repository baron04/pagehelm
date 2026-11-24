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
        self.assertNotIn("内联全表同值统计", text)

        proc = YudaoLoopProcedures(know, NS(capabilities=lambda: frozenset()), "https://app.test")
        proc.turn_context.turn_tables = (tables, "2026-10-25", "Asia/Shanghai")
        sql_res = proc._tool(NS(operation=NS(require_live=lambda **_k: None)), "sql", {"query": "SELECT id, amount FROM t1 ORDER BY amount DESC"}, {})
        self.assertNotIn("排序首键统计：", sql_res.feedback)

        know_empty = ActorKnowledge(ReadClient([]), docs)
        tables2 = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables2.close)
        empty_text = know_empty.call_api("erp/sale/page", {"no": "NOPE", "pageNo": 1, "pageSize": 20}, row_sink=tables2.add)
        self.assertNotIn("精确绑定空集核实（complete=True，0 行）", empty_text)

    def test_cell_values_large_table_and_verified_turn_id_source_ledger(self):
        from web_operation_agent.execution.value_source import SourceLedger
        from web_operation_agent_yudao.writes.convention_writes import _verified_turn_id

        rows = [{"id": i + 1, "name": f"计划-{i + 1}", "remark": f"note-{i + 1}"} for i in range(460)]
        rows.append({"id": 9999, "name": "末尾目标计划", "remark": "tail"})
        all_cands, bound = _cell_values(rows, cap=400, entity_slug="crm/receivable-plan")
        self.assertEqual(len(bound), 400)
        self.assertIn(("crm/receivable-plan", 9999), all_cands)
        self.assertIn(("", 9999), all_cands)

        ledger = SourceLedger()
        ledger.add_candidates([("system/dept", "103"), ("system/user", "257")])
        ctx = NS(executor_turn={"source_ledger": ledger})
        with turn_actor_reads(_MockWriteClient(), turn_context=ctx, knowledge=NS(turn_targets={})):
            self.assertTrue(_verified_turn_id("system/dept", 103))
            self.assertFalse(_verified_turn_id("crm/contract", 257))

    def test_g0r3_end_to_end_find_record_to_employee_create_and_prereq_guards(self):
        from datetime import date
        from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
        from web_operation_agent_yudao.rules.source import YudaoSource

        def _reply(*calls):
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                    }
                    for i, (name, args) in enumerate(calls, 1)
                ],
            }

        class _ScriptedChat:
            model = "scripted"
            provider = "synthetic"

            def __init__(self, replies):
                self.replies = list(replies)
                self.calls = []

            def complete(self, messages, tools=None, run_id="", **kwargs):
                self.calls.append((list(messages), list(tools or ())))
                if not self.replies:
                    tail = [
                        (m.get("role"), m.get("name"), str(m.get("content", ""))[:400])
                        for m in messages[-4:]
                    ]
                    raise AssertionError(f"no scripted reply left after {len(self.calls)} calls; tail={tail}")
                reply = self.replies.pop(0)
                return reply(messages) if callable(reply) else reply

            def complete_cancellable(self, messages, tools=None, run_id="", *, cancelled=None, **kwargs):
                return self.complete(messages, tools, run_id, **kwargs)

        docs = {
            "paths": {
                "/admin-api/system/dept/simple-list": {
                    "get": {
                        "tags": ["管理后台 - 部门"],
                        "summary": "获取部门精简信息列表",
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/DeptListResp"}}}}},
                    }
                },
                "/admin-api/system/dept/list": {
                    "get": {
                        "tags": ["管理后台 - 部门"],
                        "summary": "获取部门列表",
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/DeptListResp"}}}}},
                    }
                },
                "/admin-api/system/dept/get": {
                    "get": {
                        "tags": ["管理后台 - 部门"],
                        "summary": "获得部门信息",
                        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/DeptGetResp"}}}}},
                    }
                },
                "/admin-api/system/user/simple-list": {
                    "get": {
                        "tags": ["管理后台 - 用户"],
                        "summary": "获取用户精简信息列表",
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/UserListResp"}}}}},
                    }
                },
                "/admin-api/system/user/get": {
                    "get": {
                        "tags": ["管理后台 - 用户"],
                        "summary": "获得用户详情",
                        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/UserGetResp"}}}}},
                    }
                },
                "/admin-api/system/dict-data/simple-list": {
                    "get": {"tags": ["管理后台 - 字典"], "summary": "字典精简列表", "responses": {"200": {"description": "OK"}}}
                },
                "/admin-api/hrm/employee/create": {
                    "post": {
                        "tags": ["管理后台 - HRM 员工"],
                        "summary": "创建员工档案",
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/HrmEmployeeSaveReqVO"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/hrm/employee/get": {
                    "get": {
                        "tags": ["管理后台 - HRM 员工"],
                        "summary": "获得员工档案",
                        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/HrmEmployeeGetResp"}}}}},
                    }
                },
                "/admin-api/hrm/employee/page": {
                    "get": {
                        "tags": ["管理后台 - HRM 员工"],
                        "summary": "获得员工档案分页",
                        "parameters": [
                            {"name": "name", "in": "query", "schema": {"type": "string"}},
                            {"name": "pageNo", "in": "query", "required": True, "schema": {"type": "integer"}},
                            {"name": "pageSize", "in": "query", "required": True, "schema": {"type": "integer"}},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/EmpPageResp"}}}}},
                    }
                },
                "/admin-api/crm/receivable-plan/create": {
                    "post": {
                        "tags": ["管理后台 - CRM 回款计划"],
                        "summary": "创建回款计划",
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/RecPlanSaveReqVO"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/crm/receivable-plan/get": {
                    "get": {
                        "tags": ["管理后台 - CRM 回款计划"],
                        "summary": "获得回款计划",
                        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/RecPlanGetResp"}}}}},
                    }
                },
                "/admin-api/crm/receivable-plan/page": {
                    "get": {
                        "tags": ["管理后台 - CRM 回款计划"],
                        "summary": "获得回款计划分页",
                        "parameters": [
                            {"name": "contractId", "in": "query", "schema": {"type": "integer"}},
                            {"name": "pageNo", "in": "query", "required": True, "schema": {"type": "integer"}},
                            {"name": "pageSize", "in": "query", "required": True, "schema": {"type": "integer"}},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PageResp"}}}}},
                    }
                },
                "/admin-api/crm/receivable-plan/page-by-customer": {
                    "get": {
                        "tags": ["管理后台 - CRM 回款计划"],
                        "summary": "获得客户下回款计划分页",
                        "parameters": [
                            {"name": "customerId", "in": "query", "required": True, "schema": {"type": "integer"}},
                            {"name": "contractId", "in": "query", "schema": {"type": "integer"}},
                            {"name": "pageNo", "in": "query", "required": True, "schema": {"type": "integer"}},
                            {"name": "pageSize", "in": "query", "required": True, "schema": {"type": "integer"}},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PageResp"}}}}},
                    }
                },
                "/admin-api/crm/contract/page": {
                    "get": {
                        "tags": ["管理后台 - CRM 合同"],
                        "summary": "获得合同分页",
                        "parameters": [
                            {"name": "name", "in": "query", "schema": {"type": "string"}},
                            {"name": "pageNo", "in": "query", "required": True, "schema": {"type": "integer"}},
                            {"name": "pageSize", "in": "query", "required": True, "schema": {"type": "integer"}},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PageResp"}}}}},
                    }
                },
                "/admin-api/crm/contract/get": {
                    "get": {
                        "tags": ["管理后台 - CRM 合同"],
                        "summary": "获得合同详情",
                        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PageResp"}}}}},
                    }
                },
            },
            "components": {
                "schemas": {
                    "CommonResultLong": {"properties": {"code": {"type": "integer"}, "data": {"type": "integer", "format": "int64"}}},
                    "PageResp": {"properties": {"data": {"$ref": "#/components/schemas/PageInner"}}},
                    "PageInner": {"properties": {"total": {"type": "integer"}, "list": {"type": "array", "items": {"type": "object"}}}},
                    "DeptListResp": {"properties": {"data": {"type": "array", "items": {"$ref": "#/components/schemas/DeptVO"}}}},
                    "DeptGetResp": {"properties": {"data": {"$ref": "#/components/schemas/DeptVO"}}},
                    "DeptVO": {"properties": {"id": {"type": "integer"}, "name": {"type": "string", "description": "部门名称"}, "parentId": {"type": "integer"}}},
                    "UserListResp": {"properties": {"data": {"type": "array", "items": {"$ref": "#/components/schemas/UserVO"}}}},
                    "UserGetResp": {"properties": {"data": {"$ref": "#/components/schemas/UserVO"}}},
                    "UserVO": {"properties": {"id": {"type": "integer"}, "nickname": {"type": "string", "description": "用户昵称"}, "username": {"type": "string"}}},
                    "EmpPageResp": {"properties": {"data": {"$ref": "#/components/schemas/EmpPage"}}},
                    "EmpPage": {"properties": {"total": {"type": "integer"}, "list": {"type": "array", "items": {"$ref": "#/components/schemas/HrmEmployeeRespVO"}}}},
                    "HrmEmployeeGetResp": {"properties": {"data": {"$ref": "#/components/schemas/HrmEmployeeRespVO"}}},
                    "HrmEmployeeRespVO": {
                        "properties": {
                            "id": {"type": "integer", "format": "int64"},
                            "name": {"type": "string", "description": "员工姓名"},
                            "mobile": {"type": "string", "description": "手机号"},
                            "deptId": {"type": "integer", "format": "int64", "description": "部门编号"},
                            "status": {"type": "integer", "format": "int32", "description": "员工状态"},
                            "entryStatus": {"type": "integer", "format": "int32", "description": "入职状态"},
                            "ownerUserId": {"type": "integer", "format": "int64", "description": "负责人"},
                            "entryTime": {"type": "string", "format": "date-time", "description": "入职日期"},
                        }
                    },
                    "HrmEmployeeSaveReqVO": {
                        "required": ["name", "mobile", "status", "entryStatus"],
                        "properties": {
                            "name": {"type": "string", "description": "员工姓名"},
                            "mobile": {"type": "string", "description": "手机号"},
                            "deptId": {"type": "integer", "format": "int64", "description": "部门编号"},
                            "status": {"type": "integer", "format": "int32", "description": "员工状态"},
                            "entryStatus": {"type": "integer", "format": "int32", "description": "入职状态"},
                            "ownerUserId": {"type": "integer", "format": "int64", "description": "负责人"},
                            "entryTime": {"type": "string", "format": "date-time", "description": "入职日期"},
                        },
                    },
                    "RecPlanGetResp": {"properties": {"data": {"$ref": "#/components/schemas/RecPlanSaveReqVO"}}},
                    "RecPlanSaveReqVO": {
                        "required": ["contractId", "returnTime", "price"],
                        "properties": {
                            "contractId": {"type": "integer", "format": "int64", "description": "合同编号"},
                            "returnTime": {"type": "string", "description": "计划回款日期"},
                            "price": {"type": "number", "description": "计划回款金额"},
                        },
                    },
                }
            },
        }

        class _HrmCrmClient:
            def __init__(self):
                self.read_generation = 1
                self.tenant = "1"
                self.posts = []
                self.employees = {}
                self.next_id = 901
                self.contracts = [{"id": 101, "name": "2026框架协议", "no": "HT-101", "customerId": 11, "totalPrice": 100000}]
                self.plans = []

            def read_window(self, _check):
                return nullcontext()

            def write_window(self, _check):
                return nullcontext()

            def get(self, path, **params):
                if path == "system/auth/get-permission-info":
                    return {
                        "user": {"id": 257, "nickname": "芋道源码", "username": "yudao", "deptId": 100},
                        "roles": ["super_admin"],
                        "permissions": ["system:dept:query", "hrm:employee:query", "crm:contract:query", "crm:receivable-plan:query"],
                        "menus": [
                            {"id": 1, "name": "系统管理", "path": "/system", "children": [
                                {"id": 10, "name": "部门管理", "path": "dept", "component": "system/dept/index"},
                            ]},
                            {"id": 2, "name": "人事管理", "path": "/hrm", "children": [
                                {"id": 20, "name": "员工管理", "path": "employee", "component": "hrm/employee/index"},
                            ]},
                            {"id": 3, "name": "CRM管理", "path": "/crm", "children": [
                                {"id": 30, "name": "合同管理", "path": "contract", "component": "crm/contract/index"},
                                {"id": 31, "name": "回款计划", "path": "receivable-plan", "component": "crm/receivable-plan/index"},
                            ]},
                        ],
                    }
                if path in {"system/dept/simple-list", "system/dept/list"}:
                    return [{"id": 103, "name": "研发部门", "parentId": 100}]
                if path == "system/dept/get":
                    return {"id": 103, "name": "研发部门", "parentId": 100}
                if path == "system/user/simple-list":
                    return [{"id": 257, "nickname": "芋道源码", "username": "yudao"}]
                if path == "system/user/get":
                    return {"id": 257, "nickname": "芋道源码", "username": "yudao"}
                if path == "system/dict-data/simple-list":
                    return [{"dictType": "crm_receivable_return_type", "value": "1", "label": "银行转账"}]
                if path == "hrm/employee/page":
                    rows = list(self.employees.values())
                    return {"list": rows, "total": len(rows)}
                if path == "hrm/employee/get":
                    return self.employees.get(str(params.get("id")))
                if path == "crm/contract/page":
                    return {"list": list(self.contracts), "total": len(self.contracts)}
                if path == "crm/contract/get":
                    return self.contracts[0]
                if path in {"crm/receivable-plan/page", "crm/receivable-plan/page-by-customer"}:
                    return {"list": list(self.plans), "total": len(self.plans)}
                return []

            def write_once(self, method, path, *, body=None, params=None, operation="", expect_result="bool"):
                self.posts.append((method, path, dict(body or {}), operation))
                if path == "hrm/employee/create":
                    eid = self.next_id
                    self.next_id += 1
                    rec = {"id": eid, **dict(body or {})}
                    self.employees[str(eid)] = rec
                    return eid
                if path == "crm/receivable-plan/create":
                    self.plans.append({"id": 77, **dict(body or {}), "period": 1})
                    return 77
                return True

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            srv = root / "server/yudao-module-hrm/src/main/java/cn/iocoder/yudao/module/hrm"
            (srv / "enums/employee").mkdir(parents=True)
            (srv / "controller/admin/employee/vo").mkdir(parents=True)
            (srv / "service/employee").mkdir(parents=True)
            (srv / "dal/dataobject/employee").mkdir(parents=True)
            (srv / "enums/employee/HrmEmployeeTypeEnum.java").write_text(
                'package cn.iocoder.yudao.module.hrm.enums.employee;\n'
                'public enum HrmEmployeeTypeEnum {\n'
                '    FORMAL(1, "正式");\n'
                '    private final Integer type; private final String name;\n'
                '    public Integer getType() { return type; }\n'
                '}\n',
                encoding="utf-8",
            )
            (srv / "enums/employee/HrmEmployeeStatusEnum.java").write_text(
                'package cn.iocoder.yudao.module.hrm.enums.employee;\n'
                'public enum HrmEmployeeStatusEnum {\n'
                '    REGULAR(1, HrmEmployeeTypeEnum.FORMAL.getType(), "正式"),\n'
                '    PROBATION(2, HrmEmployeeTypeEnum.FORMAL.getType(), "试用");\n'
                '}\n',
                encoding="utf-8",
            )
            (srv / "enums/employee/HrmEmployeeEntryStatusEnum.java").write_text(
                'package cn.iocoder.yudao.module.hrm.enums.employee;\n'
                'public enum HrmEmployeeEntryStatusEnum {\n'
                '    IN_SERVICE(1, "在职"),\n'
                '    PENDING_ENTRY(2, "待入职");\n'
                '}\n',
                encoding="utf-8",
            )
            (srv / "dal/dataobject/employee/HrmEmployeeDO.java").write_text(
                "public class HrmEmployeeDO extends BaseDO {\n"
                "    private Long id;\n"
                "    private String name;\n"
                "    private String mobile;\n"
                "    private Long deptId;\n"
                "    private Integer status;\n"
                "    private Integer entryStatus;\n"
                "    private Long ownerUserId;\n"
                "    private LocalDateTime entryTime;\n"
                "}\n",
                encoding="utf-8",
            )
            (srv / "controller/admin/employee/vo/HrmEmployeeSaveReqVO.java").write_text(
                "package cn.iocoder.yudao.module.hrm.controller.admin.employee.vo;\n"
                "import cn.iocoder.yudao.framework.common.validation.InEnum;\n"
                "import cn.iocoder.yudao.module.hrm.enums.employee.HrmEmployeeStatusEnum;\n"
                "import cn.iocoder.yudao.module.hrm.enums.employee.HrmEmployeeEntryStatusEnum;\n"
                "public class HrmEmployeeSaveReqVO {\n"
                "    @Schema(description = \"员工姓名\")\n"
                "    private String name;\n"
                "    @Schema(description = \"手机号\")\n"
                "    private String mobile;\n"
                "    @Schema(description = \"部门编号\")\n"
                "    private Long deptId;\n"
                "    @InEnum(HrmEmployeeStatusEnum.class)\n"
                "    private Integer status;\n"
                "    @InEnum(HrmEmployeeEntryStatusEnum.class)\n"
                "    private Integer entryStatus;\n"
                "    @Schema(description = \"负责人\")\n"
                "    private Long ownerUserId;\n"
                "    @Schema(description = \"入职日期\")\n"
                "    private LocalDateTime entryTime;\n"
                "}\n",
                encoding="utf-8",
            )
            (srv / "controller/admin/employee/HrmEmployeeController.java").write_text(
                'package cn.iocoder.yudao.module.hrm.controller.admin.employee;\n'
                'import cn.iocoder.yudao.module.hrm.controller.admin.employee.vo.HrmEmployeeSaveReqVO;\n'
                '@RequestMapping("/hrm/employee")\n'
                'public class HrmEmployeeController {\n'
                '    @Resource\n'
                '    private HrmEmployeeService employeeService;\n'
                '    @PostMapping("/create")\n'
                '    public CommonResult<Long> createEmployee(@RequestBody HrmEmployeeSaveReqVO createReqVO) { return success(employeeService.createEmployee(createReqVO)); }\n'
                '    @GetMapping("/get")\n'
                '    public CommonResult<HrmEmployeeRespVO> getEmployee(@RequestParam("id") Long id) { return success(null); }\n'
                '}\n',
                encoding="utf-8",
            )
            (srv / "service/employee/HrmEmployeeServiceImpl.java").write_text(
                "public class HrmEmployeeServiceImpl implements HrmEmployeeService {\n"
                "    @Resource\n"
                "    private HrmEmployeeMapper employeeMapper;\n"
                "    public Long createEmployee(HrmEmployeeSaveReqVO createReqVO) {\n"
                "        HrmEmployeeDO emp = BeanUtils.toBean(createReqVO, HrmEmployeeDO.class);\n"
                "        employeeMapper.insert(emp);\n"
                "        return emp.getId();\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            source = YudaoSource(root, release="local")
            client = _HrmCrmClient()
            app = YudaoApplication(
                "https://app.test",
                None,
                docs,
                client=client,
                source=source,
                today=date(2026, 10, 25),
                native_writes=True,
            )
            bound_app = bind_application(app)
            principal = PrincipalContext(1, "yudao", "1", "100", "257", "g0r3-e2e", int(time.time() * 1000) - 1000, int(time.time() * 1000) + 60_000, ("yudao.read", "yudao.write"))
            hub_obs = PageObservation(
                1, 1, "https://app.test/#/index", "工作台", [], [ElementRef("e0", "link", "首页", "a", 1, {})], content="[0]<a href=/index>首页 />"
            )
            channel = _Channel(page_url="https://app.test/#/index", decision="approved", principal=principal)
            channel.latest_observation_envelope = NS(observation_id="obs-1", page_epoch=1, observation=hub_obs)
            goal_port = NativePageGoalPort(app.goal_data)
            chat = _ScriptedChat([
                _reply(("find_record", {"name": "研发部门", "kind": "system/dept"})),
                _reply(("create_record", {
                    "entity": "hrm/employee",
                    "fields": [
                        {"field": "name", "value": "张三"},
                        {"field": "mobile", "value": "13800001111"},
                        {"field": "deptId", "value": "研发部门"},
                        {"field": "status", "value": "正式"},
                        {"field": "entryStatus", "value": 2},
                        {"field": "ownerUserId", "value": "芋道源码"},
                        {"field": "entryTime", "value": "2026-10-25 00:00:00"},
                    ],
                })),
                lambda msgs: _reply(("finish", {
                    "status": "done",
                    "answer": "已创建员工档案张三（ID=901）。",
                    "items": [{
                        "item": "新增员工张三",
                        "kind": "write",
                        "status": "delivered",
                        "receipts": [
                            next(
                                line.split("：", 1)[1].strip()
                                for m in reversed(msgs)
                                for line in str(m.get("content") or "").splitlines()
                                if "本轮工具回执：" in line
                            )
                        ],
                    }],
                })),
            ])
            executor = YudaoTurnExecutor(
                channel=channel,
                chat=chat,
                config=AgentTurnConfig(
                    supported_scope="Yudao",
                    ledger_path=root / "audit_e2e.jsonl",
                    require_identity=True,
                    deployment_profile="test",
                    allow_business_mutations=True,
                ),
                application=bound_app,
                goal_port=goal_port,
                knowledge=ActorKnowledge(client, docs),
                control=ReadOnlyControl(),
                native_submit=NS(),
                verify=False,
                today="2026-10-25",
                run_id_factory=lambda _m: "g0r3-e2e",
            )
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                res = executor.execute({
                    "message_id": "m1",
                    "text": "请新增员工张三，手机号13800001111，归属上面查到的研发部门，状态为正式，入职状态为待入职。",
                    "page_url": "https://app.test/#/index",
                })
            self.assertEqual(res.terminal_reason, "agent_finished")
            self.assertEqual(len(client.posts), 1)
            self.assertEqual(client.posts[0][1], "hrm/employee/create")
            self.assertEqual(client.posts[0][2]["deptId"], 103)
            self.assertEqual(client.posts[0][2]["status"], 1)
            self.assertEqual(client.posts[0][2]["entryStatus"], 2)
            self.assertEqual(client.posts[0][2]["ownerUserId"], 257)
            self.assertEqual(len(channel.approvals), 1)

            # Verify concurrent find_record also populates entity-slug-bound candidates in source_ledger
            from web_operation_agent.execution.value_source import SourceLedger

            conc_turn = {"source_ledger": SourceLedger()}
            executor.turn_context.executor_turn = conc_turn
            with turn_actor_reads(client, turn_context=executor.turn_context, knowledge=executor.knowledge):
                op = NS(principal=principal, session_id="s1", turn_id="t1", require_live=lambda **_k: None)
                prep = executor.procedures.prepare_read("find_record", {"name": "研发部门", "kind": "system/dept"}, operation=op)
                with patch.object(executor.procedures, "_note"):
                    executor.procedures.commit_read("find_record", prep, {"name": "研发部门"}, NS(operation=op, measure=lambda _k: nullcontext(), progress=lambda _f: None))
            self.assertTrue(any(c.binding == "system/dept" and c.value == "研发部门" for c in conc_turn["source_ledger"].candidates))
            self.assertTrue(any(c.binding == "system/dept" and c.value == "103" for c in conc_turn["source_ledger"].candidates))

            # Prerequisite guard blocks unrequested crm_receivable_plan_create on downstream receivable request
            guard_chat = _ScriptedChat([
                _reply(("load_skill", {"skill": "crm-receivable-planning"})),
                _reply(("propose_operation", {
                    "operation": "crm_receivable_plan_create",
                    "inputs": {
                        "contract": "2026框架协议",
                        "owner": "芋道源码",
                        "returnTime": "2026-10-30 00:00:00",
                        "price": 5000,
                    },
                })),
                _reply(("create_record", {
                    "entity": "crm/receivable-plan",
                    "fields": [
                        {"field": "contractId", "value": "2026框架协议"},
                        {"field": "returnTime", "value": "2026-10-30"},
                        {"field": "price", "value": 5000},
                    ],
                })),
                _reply(("finish", {
                    "status": "cannot",
                    "answer": "合同缺少对应回款计划，未擅自新建回款计划。",
                    "items": [{"item": "登记实际回款", "kind": "write", "status": "undelivered", "receipts": []}],
                })),
            ])
            channel_guard = _Channel(page_url="https://app.test/#/index", decision="approved", principal=principal)
            channel_guard.latest_observation_envelope = NS(observation_id="obs-2", page_epoch=1, observation=hub_obs)
            guard_exec = YudaoTurnExecutor(
                channel=channel_guard,
                chat=guard_chat,
                config=AgentTurnConfig(
                    supported_scope="Yudao",
                    ledger_path=root / "audit_guard.jsonl",
                    require_identity=True,
                    deployment_profile="test",
                    allow_business_mutations=True,
                ),
                application=bound_app,
                goal_port=NativePageGoalPort(app.goal_data),
                knowledge=ActorKnowledge(client, docs),
                control=ReadOnlyControl(),
                native_submit=NS(),
                verify=False,
                today="2026-10-25",
                enable_domain_packs=False,
                run_id_factory=lambda _m: "g0r3-guard",
            )
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                guard_res = guard_exec.execute({
                    "message_id": "m2",
                    "text": "给合同2026框架协议登记一笔5000元实际回款。",
                    "page_url": "https://app.test/#/index",
                })
            self.assertEqual(guard_res.status, "declined")
            self.assertEqual(len(client.plans), 0)
            self.assertEqual(len(channel_guard.approvals), 0)
            self.assertIn("严禁擅自补建回款计划", json.dumps(guard_chat.calls[2][0], ensure_ascii=False))
            self.assertIn("严禁擅自补建回款计划", json.dumps(guard_chat.calls[3][0], ensure_ascii=False))
            # enable_domain_packs=False omits switch_domain from offered tools
            offered_names = [t["function"]["name"] for t in guard_chat.calls[0][1]]
            self.assertNotIn("switch_domain", offered_names)

            # Explicit plan creation prompt passes intent alignment check cleanly
            port = YudaoConventionWritePort(client, app.goal_data)
            port.check_create_intent_alignment("crm/receivable-plan", ("为2026框架协议新建一期回款计划",))

    def test_hrm_salary_employee_info_target_suffix_and_line_changes_guidance(self):
        docs = {
            "paths": {
                "/admin-api/hrm/salary/employee-info/page": {
                    "get": {
                        "tags": ["管理后台 - HRM 员工薪资信息"],
                        "summary": "分页",
                        "parameters": [
                            {"name": "employeeId", "in": "query"},
                            {"name": "search", "in": "query"},
                            {"name": "postName", "in": "query"},
                            {"name": "pageNo", "in": "query", "required": True},
                            {"name": "pageSize", "in": "query", "required": True},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SalPageResp"}}}}},
                    }
                },
                "/admin-api/hrm/salary/employee-info/get": {
                    "get": {
                        "tags": ["管理后台 - HRM 员工薪资信息"],
                        "summary": "详情",
                        "parameters": [{"name": "employeeId", "in": "query", "required": True}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SalGetResp"}}}}},
                    }
                },
                "/admin-api/hrm/salary/employee-info/update": {
                    "put": {
                        "tags": ["管理后台 - HRM 员工薪资信息"],
                        "summary": "修改员工薪资",
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SalUpdateVO"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
            },
            "components": {
                "schemas": {
                    "CommonResultLong": {"properties": {"code": {"type": "integer"}, "data": {"type": "integer", "format": "int64"}}},
                    "SalPageResp": {"properties": {"data": {"$ref": "#/components/schemas/SalPage"}}},
                    "SalPage": {"properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/SalRespVO"}}}},
                    "SalGetResp": {"properties": {"data": {"$ref": "#/components/schemas/SalRespVO"}}},
                    "SalRespVO": {
                        "properties": {
                            "id": {"type": "integer"},
                            "employeeId": {"type": "integer"},
                            "employeeName": {"type": "string"},
                            "postName": {"type": "string"},
                            "remark": {"type": "string", "description": "备注"},
                            "salaryOptions": {"type": "array", "items": {"$ref": "#/components/schemas/SalOptVO"}},
                        }
                    },
                    "SalUpdateVO": {
                        "properties": {
                            "id": {"type": "integer"},
                            "employeeId": {"type": "integer", "description": "员工编号"},
                            "remark": {"type": "string", "description": "备注"},
                            "salaryOptions": {"type": "array", "description": "薪资项", "items": {"$ref": "#/components/schemas/SalOptVO"}},
                        }
                    },
                    "SalOptVO": {
                        "required": ["code", "value"],
                        "properties": {
                            "code": {"type": "integer", "description": "编码"},
                            "name": {"type": "string", "description": "名称"},
                            "value": {"type": "number", "description": "金额"},
                        },
                    },
                }
            },
        }

        class _SalaryClient:
            def __init__(self):
                self.tenant = "1"
                self.page_queries = []
                self.row = {
                    "id": 55,
                    "employeeId": 112,
                    "employeeName": "李云珂",
                    "postName": "研发总监",
                    "remark": "原备注",
                    "salaryOptions": [{"code": 10101, "name": "基本工资", "value": 10000}],
                }

            def get(self, path, **params):
                if path == "system/auth/get-permission-info":
                    return {"user": {"id": 257, "nickname": "芋道源码", "deptId": 100}, "roles": ["super_admin"], "menus": []}
                if path == "hrm/salary/employee-info/page":
                    self.page_queries.append(dict(params))
                    return {"list": [dict(self.row)], "total": 1}
                if path == "hrm/salary/employee-info/get":
                    return dict(self.row)
                return []

        from web_operation_agent.business.compiled_writes import CompiledAggregateUpdateHandler

        client = _SalaryClient()
        port = YudaoConventionWritePort(client, NS(_documents=lambda: docs, find=lambda *_a, **_k: ()))
        handler = CompiledAggregateUpdateHandler(port)
        actor_ctx = NS(principal=_make_principal(), require_live=lambda **_k: None)
        with self.assertRaises(BusinessToolRequestError) as err:
            handler.prepare({
                "entity": "hrm/salary/employee-info",
                "target": "李云珂（employeeId=112）",
                "changes": [{"field": "regularSalary", "value": 15000}],
            }, context=actor_ctx)
        self.assertIn("line_changes", str(err.exception))
        self.assertIn("salaryOptions", str(err.exception))
        self.assertNotIn("页面表单", str(err.exception))
        self.assertTrue(client.page_queries)
        self.assertNotIn("postName", client.page_queries[0])
        self.assertEqual(client.page_queries[0].get("employeeId"), 112)

        ok_spec = handler.prepare({
            "entity": "hrm/salary/employee-info",
            "target": "李云珂（employeeId=112）",
            "line_changes": [{"collection": "salaryOptions", "match": "基本工资", "changes": [{"field": "value", "value": 15000}]}],
        }, context=actor_ctx)
        self.assertEqual(ok_spec.args["payload"]["salaryOptions"][0]["value"], 15000)

    def test_g0r4_defaults_omission_all_equal_small_table_entity_rules_and_sql_receipt(self):
        from web_operation_agent.business.compiled_writes import CompiledCreateSpec, CompiledFieldDefault
        from web_operation_agent.business.convention_writes import ConventionFieldSpec
        from web_operation_agent_yudao.executor.domain_toolpacks import format_write_entry_line
        from web_operation_agent_yudao.writes.convention_writes import _format_create_req

        spec = CompiledCreateSpec(
            entity="hrm/employee",
            label="HRM 员工档案",
            fields=(
                ConventionFieldSpec("name", "string", "姓名", required=True),
                ConventionFieldSpec("mobile", "string", "手机", required=True),
                ConventionFieldSpec("entryStatus", "integer", "入职状态", required=True, enum_type="__java_enum__:HrmEmployeeEntryStatusEnum"),
                ConventionFieldSpec("type", "integer", "聘用形式", required=True, enum_type="__java_enum__:HrmEmployeeTypeEnum"),
                ConventionFieldSpec("entryTime", "integer", "入职时间", required=True, date_kind="epoch_ms"),
                ConventionFieldSpec("deptId", "integer", "部门", required=False, reference_entity="system/dept"),
            ),
            defaults=(
                CompiledFieldDefault("entryStatus", "literal", 1, display="在职"),
                CompiledFieldDefault("type", "literal", 1, display="正式"),
            ),
        )
        req, fks, auto_defaults = _format_create_req(spec)
        self.assertEqual(req, ["name", "mobile", "entryTime"])
        self.assertEqual(auto_defaults, ["entryStatus", "type"])
        self.assertEqual(fks, ["deptId->system/dept(选填)"])
        line = format_write_entry_line({
            "kind": "compiled_create",
            "tool": "create_record",
            "entity": "hrm/employee",
            "label": "HRM 员工档案",
            "required": req,
            "defaults": auto_defaults,
            "relations": fks,
        })
        self.assertIn("必填: name, mobile, entryTime", line)
        self.assertIn("默认自动补齐(未指明可省略): entryStatus, type", line)
        self.assertIn("deptId->system/dept(选填)", line)
        self.assertIn("其余未列字段均为选填", line)

        # Inline complete small table where ALL rows share identical numeric value (e.g. browseCount=0)
        class AllZeroArticleClient:
            tenant = "1"

            def get(self, path, **params):
                if path == "system/auth/get-permission-info":
                    return {"user": {"id": 257, "nickname": "芋道源码", "deptId": 100}, "menus": [], "permissions": []}
                if path == "promotion/article/page":
                    if params.get("status") == 0:
                        return {"list": [], "total": 0}
                    return {"list": [{"id": i, "title": f"文章{i}", "browseCount": 0} for i in range(1, 5)], "total": 4}
                return {"list": [], "total": 0}

            def read_window(self, _check):
                return nullcontext()

        docs = {
            "paths": {
                "/admin-api/promotion/article/page": {
                    "get": {
                        "summary": "文章分页",
                        "tags": ["文章"],
                        "parameters": [
                            {"name": "status", "in": "query"},
                            {"name": "pageNo", "in": "query", "required": True},
                            {"name": "pageSize", "in": "query", "required": True},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ArtPageResp"}}}}},
                    }
                }
            },
            "components": {
                "schemas": {
                    "ArtPageResp": {"properties": {"data": {"$ref": "#/components/schemas/ArtPage"}}},
                    "ArtPage": {"properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/ArtItem"}}}},
                    "ArtItem": {"properties": {"id": {}, "title": {"description": "标题"}, "browseCount": {"description": "浏览量"}}},
                }
            },
        }
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables.close)
        know = ActorKnowledge(AllZeroArticleClient(), docs)
        text = know.call_api("promotion/article/page", {"pageNo": 1, "pageSize": 20}, row_sink=tables.add)
        self.assertNotIn("内联全表同值统计", text)
        zero_text = know.call_api("promotion/article/page", {"status": 0, "pageNo": 1, "pageSize": 20}, row_sink=tables.add)
        self.assertNotIn("精确过滤已核实为 0 条记录", zero_text)

        # entity_rules under writes_enabled surfaces compiled write contracts & auto-defaults
        from web_operation_agent.goal.index import ApplicationIndex, IndexEntity

        write_docs = {
            **BASE_DOCS,
            "paths": {
                **BASE_DOCS["paths"],
                "/admin-api/hrm/employee/get": {
                    "get": {
                        "tags": ["管理后台 - HRM 员工档案"],
                        "summary": "获取员工详情",
                        "parameters": [{"name": "id", "in": "query", "required": True}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/EmpResp"}}}}},
                    }
                },
                "/admin-api/hrm/employee/create": {
                    "post": {
                        "tags": ["管理后台 - HRM 员工档案"],
                        "summary": "创建员工档案",
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/EmpCreateReq"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
            },
            "components": {
                **BASE_DOCS.get("components", {}),
                "schemas": {
                    **BASE_DOCS.get("components", {}).get("schemas", {}),
                    "EmpResp": {"properties": {"data": {"$ref": "#/components/schemas/EmpVO"}}},
                    "EmpVO": {"properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "mobile": {"type": "string"}, "remark": {"type": "string"}}},
                    "EmpCreateReq": {
                        "required": ["name", "mobile"],
                        "properties": {
                            "name": {"type": "string", "description": "员工姓名"},
                            "mobile": {"type": "string", "description": "手机号"},
                            "remark": {"type": "string", "description": "备注"},
                        },
                    },
                },
            },
        }
        proc_w = YudaoLoopProcedures(
            ActorKnowledge(AllZeroArticleClient(), write_docs),
            NS(
                capabilities=lambda: frozenset(),
                data=NS(_source=None, _ui=None),
                rules_index=lambda context=None: ApplicationIndex(entities=(IndexEntity("hrm/employee", "HRM 员工档案", rules=()),)),
            ),
            "https://app.test",
        )
        proc_w.writes_enabled = True
        rules_out = proc_w._entity_rules(NS(operation=NS(principal=_make_principal())), "hrm/employee")
        self.assertIn("编译写契约", rules_out)
        self.assertIn("create_record(entity=\"hrm/employee\"", rules_out)
        self.assertIn("必填: name, mobile", rules_out)
        self.assertIn("选填字段: remark(备注)", rules_out)

    def test_crm_fk_permission_precheck_blocks_1020007001_before_approval_and_prevents_silent_drop(self):
        import time
        from web_operation_agent.business.compiled_writes import CompiledCreateHandler
        from web_operation_agent.business.errors import BusinessMutationRefusedError, BusinessToolRequestError
        from web_operation_agent.state.identity import OperationContext, PrincipalContext
        from web_operation_agent_yudao.reads.bound_data_reads import turn_actor_reads
        from web_operation_agent_yudao.reads.client import YudaoReadRefused
        from web_operation_agent_yudao.writes.convention_writes import YudaoConventionWritePort

        now = int(time.time() * 1000)
        actor = PrincipalContext(
            1, "yudao", "1", "7", "1", "synthetic",
            now - 1000, now + 60000, ("yudao.read", "yudao.write"),
        )
        op_ctx = OperationContext.create(actor, session_id="s", turn_id="t", operation_id="o", deadline_ms=now + 30000)

        class _CrmReceivableClient:
            def __init__(self):
                self.read_generation = 1
                self.tenant = "1"
                self.dispatches = []
                self.contract_audit_status = 20
                self.contract_total_price = 1000.0
                self.plan_receivable_id = None
                self.plan_get_error = YudaoReadRefused(
                    "crm/receivable-plan/get: 1020007001 回款计划操作失败，原因：没有权限",
                    code=1020007001,
                    reason="回款计划操作失败，原因：没有权限",
                )
                self.existing_receivables = []

            def read_window(self, _check):
                return nullcontext()

            def write_window(self, _check):
                return nullcontext()

            def get(self, path, **params):
                if path == "system/auth/get-permission-info":
                    return {
                        "user": {"id": 1, "nickname": "芋道源码", "username": "admin"},
                        "roles": [],
                        "permissions": ["crm:receivable:create"],
                        "menus": [],
                    }
                if path == "system/dict-data/simple-list":
                    return [{"dictType": "crm_receivable_return_type", "value": "4", "label": "电汇"}]
                if path == "crm/customer/simple-list":
                    return [{"id": 998326, "name": "青岛望川瑞宁电力技术有限责任公司"}]
                if path == "crm/customer/get":
                    return {"id": 998326, "name": "青岛望川瑞宁电力技术有限责任公司"}
                if path == "crm/contract/simple-list":
                    return [{"id": 998427, "name": "北方采购协同供货合同（南区）", "customerId": 998326}]
                if path == "crm/contract/get":
                    return {
                        "id": 998427,
                        "name": "北方采购协同供货合同（南区）",
                        "customerId": 998326,
                        "auditStatus": self.contract_audit_status,
                        "totalPrice": self.contract_total_price,
                    }
                if path == "crm/receivable-plan/simple-list":
                    return [{"id": 7, "period": 1, "contractId": 998427, "customerId": 998326}]
                if path == "crm/receivable-plan/get":
                    if self.plan_get_error is not None:
                        raise self.plan_get_error
                    return {
                        "id": 7,
                        "period": 1,
                        "contractId": 998427,
                        "customerId": 998326,
                        "receivableId": self.plan_receivable_id,
                    }
                if path == "crm/receivable/page":
                    return {"list": list(self.existing_receivables), "total": len(self.existing_receivables)}
                return []

            def write_once(self, method, path, *, body=None, params=None, operation="", expect_result="bool"):
                self.dispatches.append((method, path, dict(body or {})))
                return 888

        docs = {
            "paths": {
                "/admin-api/crm/customer/page": {"get": {"responses": {"200": {}}}},
                "/admin-api/crm/customer/simple-list": {"get": {"responses": {"200": {}}}},
                "/admin-api/crm/customer/get": {
                    "get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {}}}
                },
                "/admin-api/crm/contract/page": {"get": {"responses": {"200": {}}}},
                "/admin-api/crm/contract/simple-list": {
                    "get": {"parameters": [{"name": "customerId", "in": "query", "required": True}], "responses": {"200": {}}}
                },
                "/admin-api/crm/contract/get": {
                    "get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {}}}
                },
                "/admin-api/crm/receivable-plan/page": {"get": {"responses": {"200": {}}}},
                "/admin-api/crm/receivable-plan/simple-list": {
                    "get": {
                        "parameters": [
                            {"name": "customerId", "in": "query", "required": True},
                            {"name": "contractId", "in": "query", "required": True},
                        ],
                        "responses": {"200": {}},
                    }
                },
                "/admin-api/crm/receivable-plan/get": {
                    "get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {}}}
                },
                "/admin-api/crm/receivable/page": {
                    "get": {"parameters": [{"name": "contractId", "in": "query"}], "responses": {"200": {}}}
                },
                "/admin-api/crm/receivable/get": {
                    "get": {
                        "parameters": [{"name": "id", "in": "query", "required": True}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/RecvGetResp"}}}}},
                    }
                },
                "/admin-api/crm/receivable/create": {
                    "post": {
                        "tags": ["管理后台 - CRM 回款"],
                        "summary": "创建回款",
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/RecvSaveReqVO"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
            },
            "components": {
                "schemas": {
                    "CommonResultLong": {"properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                    "RecvGetResp": {"properties": {"data": {"$ref": "#/components/schemas/RecvSaveReqVO"}}},
                    "RecvSaveReqVO": {
                        "required": ["contractId", "customerId", "price", "returnTime", "returnType"],
                        "properties": {
                            "contractId": {"type": "integer", "format": "int64", "description": "合同名称"},
                            "customerId": {"type": "integer", "format": "int64", "description": "客户名称"},
                            "planId": {"type": "integer", "format": "int64", "description": "回款期数"},
                            "price": {"type": "number", "description": "回款金额"},
                            "returnTime": {"type": "string", "format": "date-time", "description": "回款日期"},
                            "returnType": {"type": "integer", "format": "int32", "description": "回款方式"},
                        },
                    },
                }
            },
        }
        client = _CrmReceivableClient()
        port = YudaoConventionWritePort(client, NS(_documents=docs))
        handler = CompiledCreateHandler(port)

        # (a) simple-list returns id=7 while /get?id=7 throws 1020007001 -> prepare refuses before approval,
        # and retrying in the same turn with planId silently omitted is also refused.
        turn_ctx = NS(executor_turn={})
        with turn_actor_reads(client, turn_ctx):
            with self.assertRaises(BusinessToolRequestError) as err1:
                handler.prepare(
                    {
                        "entity": "crm/receivable",
                        "fields": [
                            {"field": "contractId", "value": "北方采购协同供货合同（南区）"},
                            {"field": "customerId", "value": "青岛望川瑞宁电力技术有限责任公司"},
                            {"field": "planId", "value": "第1期"},
                            {"field": "price", "value": 160},
                            {"field": "returnTime", "value": "2026-10-25"},
                            {"field": "returnType", "value": 4},
                        ],
                    },
                    context=op_ctx,
                )
            self.assertIn("1020007001", str(err1.exception))
            self.assertIn("禁止在同一轮静默省略", str(err1.exception))

            with self.assertRaises(BusinessToolRequestError) as err_drop:
                handler.prepare(
                    {
                        "entity": "crm/receivable",
                        "fields": [
                            {"field": "contractId", "value": "北方采购协同供货合同（南区）"},
                            {"field": "customerId", "value": "青岛望川瑞宁电力技术有限责任公司"},
                            {"field": "price", "value": 160},
                            {"field": "returnTime", "value": "2026-10-25"},
                            {"field": "returnType", "value": 4},
                        ],
                    },
                    context=op_ctx,
                )
            self.assertIn("禁止在同一轮静默省略该关联字段", str(err_drop.exception))

        # (b) plan already linked (receivableId=7), unapproved contract (auditStatus=10), and over-limit price
        client.plan_get_error = None
        client.plan_receivable_id = 77
        with turn_actor_reads(client, NS(executor_turn={})):
            with self.assertRaises(BusinessToolRequestError) as err_dup:
                handler.prepare(
                    {
                        "entity": "crm/receivable",
                        "fields": [
                            {"field": "contractId", "value": "北方采购协同供货合同（南区）"},
                            {"field": "customerId", "value": "青岛望川瑞宁电力技术有限责任公司"},
                            {"field": "planId", "value": "第1期"},
                            {"field": "price", "value": 160},
                            {"field": "returnTime", "value": "2026-10-25"},
                            {"field": "returnType", "value": 4},
                        ],
                    },
                    context=op_ctx,
                )
            self.assertIn("已关联回款", str(err_dup.exception))

        client.plan_receivable_id = None
        client.contract_audit_status = 10
        with turn_actor_reads(client, NS(executor_turn={})):
            with self.assertRaises(BusinessToolRequestError) as err_audit:
                handler.prepare(
                    {
                        "entity": "crm/receivable",
                        "fields": [
                            {"field": "contractId", "value": "北方采购协同供货合同（南区）"},
                            {"field": "customerId", "value": "青岛望川瑞宁电力技术有限责任公司"},
                            {"field": "planId", "value": "第1期"},
                            {"field": "price", "value": 160},
                            {"field": "returnTime", "value": "2026-10-25"},
                            {"field": "returnType", "value": 4},
                        ],
                    },
                    context=op_ctx,
                )
            self.assertIn("未审核通过", str(err_audit.exception))

        client.contract_audit_status = 20
        client.existing_receivables = [{"id": 1, "price": 900, "auditStatus": 20}]
        with turn_actor_reads(client, NS(executor_turn={})):
            with self.assertRaises(BusinessToolRequestError) as err_limit:
                handler.prepare(
                    {
                        "entity": "crm/receivable",
                        "fields": [
                            {"field": "contractId", "value": "北方采购协同供货合同（南区）"},
                            {"field": "customerId", "value": "青岛望川瑞宁电力技术有限责任公司"},
                            {"field": "planId", "value": "第1期"},
                            {"field": "price", "value": 160},
                            {"field": "returnTime", "value": "2026-10-25"},
                            {"field": "returnType", "value": 4},
                        ],
                    },
                    context=op_ctx,
                )
            self.assertIn("剩余可回款金额", str(err_limit.exception))
        self.assertEqual(client.dispatches, [])

        # (c) post-approval recheck_bindings refuses when /get raises 1020007001 after approval
        client.existing_receivables = []
        with turn_actor_reads(client, NS(executor_turn={})):
            prepared = handler.prepare(
                {
                    "entity": "crm/receivable",
                    "fields": [
                        {"field": "contractId", "value": "北方采购协同供货合同（南区）"},
                        {"field": "customerId", "value": "青岛望川瑞宁电力技术有限责任公司"},
                        {"field": "planId", "value": "第1期"},
                        {"field": "price", "value": 160},
                        {"field": "returnTime", "value": "2026-10-25"},
                        {"field": "returnType", "value": 4},
                    ],
                },
                context=op_ctx,
            )
        self.assertTrue(any(b.get("verify_get_path") == "crm/receivable-plan/get" for b in prepared.args["bindings"]))
        client.plan_get_error = YudaoReadRefused("denied after approval", code=1020007001)
        with self.assertRaises(BusinessMutationRefusedError) as ref_ctx:
            port.recheck_bindings(prepared.args["bindings"], actor_id="1")
        self.assertEqual(ref_ctx.exception.reason, "precondition_changed")
        self.assertEqual(client.dispatches, [])

    def test_hrm_employee_visible_create_fields_strips_hidden_defaults_and_blocks_hidden_inputs_and_dupes(self):
        import tempfile
        import time
        from pathlib import Path
        from web_operation_agent.business.compiled_writes import CompiledCreateHandler
        from web_operation_agent.business.errors import BusinessMutationRefusedError, BusinessToolRequestError
        from web_operation_agent.state.identity import OperationContext, PrincipalContext
        from web_operation_agent_yudao.rules.source import YudaoSource
        from web_operation_agent_yudao.writes.convention_writes import YudaoConventionWritePort

        now = int(time.time() * 1000)
        actor = PrincipalContext(
            1, "yudao", "1", "7", "1", "synthetic",
            now - 1000, now + 60000, ("yudao.read", "yudao.write"),
        )
        op_ctx = OperationContext.create(actor, session_id="s", turn_id="t", operation_id="o", deadline_ms=now + 30000)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            srv = root / "server/yudao-module-hrm/src/main/java/cn/iocoder/yudao/module/hrm"
            (srv / "controller/admin/employee/vo").mkdir(parents=True)
            (srv / "service/employee").mkdir(parents=True)
            (srv / "dal/dataobject/employee").mkdir(parents=True)
            (root / "ui/src/api/hrm/employee").mkdir(parents=True)
            (root / "ui/src/views/hrm/employee").mkdir(parents=True)

            (srv / "dal/dataobject/employee/HrmEmployeeDO.java").write_text(
                "public class HrmEmployeeDO extends BaseDO {\n"
                "    private Long id; private String name; private String mobile; private String country;\n"
                "    private Integer idType; private Integer entryStatus; private Integer type; private Integer probation;\n"
                "    private String jobNumber; private Long deptId;\n"
                "}\n",
                encoding="utf-8",
            )
            (srv / "controller/admin/employee/vo/HrmEmployeeSaveReqVO.java").write_text(
                "package cn.iocoder.yudao.module.hrm.controller.admin.employee.vo;\n"
                "public class HrmEmployeeSaveReqVO {\n"
                "    private String name; private String mobile; private String country;\n"
                "    private Integer idType; private Integer entryStatus; private Integer type;\n"
                "    private Integer probation; private String jobNumber; private Long deptId;\n"
                "}\n",
                encoding="utf-8",
            )
            (srv / "controller/admin/employee/HrmEmployeeController.java").write_text(
                'package cn.iocoder.yudao.module.hrm.controller.admin.employee;\n'
                'import cn.iocoder.yudao.module.hrm.controller.admin.employee.vo.HrmEmployeeSaveReqVO;\n'
                '@RequestMapping("/hrm/employee")\n'
                'public class HrmEmployeeController {\n'
                '    @Resource\n'
                '    private HrmEmployeeService employeeService;\n'
                '    @PostMapping("/create") public CommonResult<Long> createEmployee(@RequestBody HrmEmployeeSaveReqVO createReqVO) { return success(employeeService.createEmployee(createReqVO)); }\n'
                '    @GetMapping("/get") public CommonResult<HrmEmployeeRespVO> getEmployee(@RequestParam("id") Long id) { return success(null); }\n'
                '}\n',
                encoding="utf-8",
            )
            (srv / "service/employee/HrmEmployeeServiceImpl.java").write_text(
                "public class HrmEmployeeServiceImpl implements HrmEmployeeService {\n"
                "    @Resource\n"
                "    private HrmEmployeeMapper employeeMapper;\n"
                "    public Long createEmployee(HrmEmployeeSaveReqVO createReqVO) {\n"
                "        HrmEmployeeDO emp = BeanUtils.toBean(createReqVO, HrmEmployeeDO.class);\n"
                "        employeeMapper.insert(emp);\n"
                "        return emp.getId();\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            (root / "ui/src/api/hrm/employee/index.ts").write_text(
                "export const createEmployee = (data: any) => request.post({ url: '/hrm/employee/create', data })\n",
                encoding="utf-8",
            )
            (root / "ui/src/views/hrm/employee/EmployeeForm.vue").write_text(
                "<script setup lang=\"ts\">\n"
                "import * as EmployeeApi from '@/api/hrm/employee'\n"
                "function createDefaultFormData() {\n"
                "  return {\n"
                "    country: '中国',\n"
                "    idType: 1,\n"
                "    type: 1,\n"
                "    probation: 0,\n"
                "  }\n"
                "}\n"
                "const submitForm = () => EmployeeApi.createEmployee(formData.value)\n"
                "</script>\n",
                encoding="utf-8",
            )

            docs = {
                "paths": {
                    "/admin-api/hrm/employee/config/create-field/list": {
                        "get": {"parameters": [{"name": "entryStatus", "in": "query", "required": True}], "responses": {"200": {}}}
                    },
                    "/admin-api/hrm/employee/page": {
                        "get": {"parameters": [{"name": "mobile", "in": "query"}, {"name": "jobNumber", "in": "query"}], "responses": {"200": {}}}
                    },
                    "/admin-api/hrm/employee/get": {
                        "get": {
                            "parameters": [{"name": "id", "in": "query", "required": True}],
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/EmpGetResp"}}}}},
                        }
                    },
                    "/admin-api/hrm/employee/create": {
                        "post": {
                            "tags": ["管理后台 - HRM 员工档案"],
                            "summary": "创建员工档案",
                            "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/EmpCreateReq"}}}},
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                        }
                    },
                },
                "components": {
                    "schemas": {
                        "CommonResultLong": {"properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                        "EmpGetResp": {"properties": {"data": {"$ref": "#/components/schemas/EmpCreateReq"}}},
                        "EmpCreateReq": {
                            "required": ["name", "mobile", "entryStatus", "type"],
                            "properties": {
                                "name": {"type": "string", "description": "员工姓名"},
                                "mobile": {"type": "string", "description": "手机号"},
                                "country": {"type": "string", "description": "国家地区"},
                                "idType": {"type": "integer", "description": "证件类型"},
                                "entryStatus": {"type": "integer", "description": "入职状态"},
                                "type": {"type": "integer", "description": "聘用形式"},
                                "probation": {"type": "integer", "description": "试用期"},
                                "jobNumber": {"type": "string", "description": "工号"},
                                "deptId": {"type": "integer", "description": "部门"},
                            },
                        },
                    }
                },
            }

            class _HrmVisibilityClient:
                def __init__(self):
                    self.read_generation = 1
                    self.tenant = "1"
                    self.employees = {}
                    self.dispatches = []
                    self.config_rows = [
                        {"name": "name", "visible": True},
                        {"name": "mobile", "visible": True},
                        {"name": "country", "visible": False},
                        {"name": "idType", "visible": True},
                        {"name": "entryStatus", "visible": True},
                        {"name": "type", "visible": True},
                        {"name": "probation", "visible": True},
                        {"name": "jobNumber", "visible": False},
                        {"name": "deptId", "visible": True},
                    ]
                    self.existing_rows = []

                def read_window(self, _check):
                    return nullcontext()

                def write_window(self, _check):
                    return nullcontext()

                def get(self, path, **params):
                    if path == "system/auth/get-permission-info":
                        return {"user": {"id": 1, "nickname": "芋道源码", "username": "admin"}, "roles": [], "permissions": [], "menus": []}
                    if path in {"system/dept/simple-list", "system/dept/list"}:
                        return [{"id": 103, "name": "研发部门", "parentId": 0}]
                    if path == "hrm/employee/config/create-field/list":
                        return list(self.config_rows)
                    if path == "hrm/employee/page":
                        return {"list": list(self.existing_rows), "total": len(self.existing_rows)}
                    if path == "hrm/employee/get":
                        return self.employees.get(str(params.get("id")))
                    return []

                def write_once(self, method, path, *, body=None, params=None, operation="", expect_result="bool"):
                    self.dispatches.append((method, path, dict(body or {})))
                    rec = {"id": 501, **dict(body or {})}
                    self.employees["501"] = rec
                    return 501

            client = _HrmVisibilityClient()
            source = YudaoSource(root, release="v-g0r6-vis")
            port = YudaoConventionWritePort(
                client,
                NS(_documents=docs, _source=source, _ui=None, invalidate_reads=lambda _entities: None),
            )
            handler = CompiledCreateHandler(port)

            # (a) Hidden default country='中国' is stripped from payload/expected/details while visible defaults remain,
            #     and dispatch + readback succeed cleanly.
            spec = handler.prepare(
                {
                    "entity": "hrm/employee",
                    "fields": [
                        {"field": "name", "value": "陈砚秋"},
                        {"field": "mobile", "value": "13622190047"},
                        {"field": "entryStatus", "value": 2},
                        {"field": "deptId", "value": "研发部门"},
                    ],
                },
                context=op_ctx,
            )
            payload = spec.args["payload"]
            self.assertNotIn("country", payload)
            self.assertNotIn("country", spec.args["expected"])
            self.assertEqual((payload.get("idType"), payload.get("type"), payload.get("probation")), (1, 1, 0))
            self.assertFalse(any("country" in d.label for d in spec.details))
            res = handler.dispatch(spec.args, context=op_ctx)
            self.assertTrue(res.content["verified"])
            self.assertEqual(len(client.dispatches), 1)
            self.assertNotIn("country", client.dispatches[0][2])

            # (b) Explicit hidden field ('country' or 'jobNumber' under entryStatus=2) and duplicate mobile refuse pre-approval.
            for hidden_field, hidden_val in (("country", "中国"), ("jobNumber", "EMP-99")):
                with self.assertRaises(BusinessToolRequestError) as hid_ctx:
                    handler.prepare(
                        {
                            "entity": "hrm/employee",
                            "fields": [
                                {"field": "name", "value": "陈砚秋"},
                                {"field": "mobile", "value": "13622190048"},
                                {"field": "entryStatus", "value": 2},
                                {"field": hidden_field, "value": hidden_val},
                            ],
                        },
                        context=op_ctx,
                    )
                self.assertIn("未开启显示", str(hid_ctx.exception))

            client.existing_rows = [{"id": 9, "mobile": "13622190047", "jobNumber": "EMP-01"}]
            with self.assertRaises(BusinessToolRequestError) as dup_ctx:
                handler.prepare(
                    {
                        "entity": "hrm/employee",
                        "fields": [
                            {"field": "name", "value": "陈砚秋"},
                            {"field": "mobile", "value": "13622190047"},
                            {"field": "entryStatus", "value": 2},
                        ],
                    },
                    context=op_ctx,
                )
            self.assertIn("已存在对应员工档案", str(dup_ctx.exception))

            # (c) Post-approval visibility drift aborts dispatch safely with precondition_changed.
            client.existing_rows = []
            client.config_rows = [r for r in client.config_rows if r["name"] != "probation"] + [{"name": "probation", "visible": False}]
            with self.assertRaises(BusinessMutationRefusedError) as ref_ctx:
                handler.dispatch(spec.args, context=op_ctx)
            self.assertEqual(ref_ctx.exception.reason, "precondition_changed")
            self.assertEqual(len(client.dispatches), 1)

    def test_hrm_salary_future_change_record_reference_explains_effective_date_readback(self):
        import time
        from web_operation_agent.business.compiled_writes import CompiledAggregateUpdateHandler
        from web_operation_agent.state.identity import OperationContext, PrincipalContext
        from web_operation_agent_yudao.executor.domain_toolpacks import resolve_domain_pack, YUDAO_DOMAIN_PACKS
        from web_operation_agent_yudao.writes.convention_writes import YudaoConventionWritePort

        now = int(time.time() * 1000)
        actor = PrincipalContext(
            1, "yudao", "1", "7", "1", "synthetic",
            now - 1000, now + 60000, ("yudao.read", "yudao.write"),
        )
        op_ctx = OperationContext.create(actor, session_id="s", turn_id="t", operation_id="o", deadline_ms=now + 30000)

        docs = {
            "paths": {
                "/admin-api/hrm/salary/employee-info/page": {
                    "get": {
                        "tags": ["管理后台 - HRM 员工薪资信息"],
                        "parameters": [
                            {"name": "employeeId", "in": "query"},
                            {"name": "pageNo", "in": "query", "required": True},
                            {"name": "pageSize", "in": "query", "required": True},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SalPageResp"}}}}},
                    }
                },
                "/admin-api/hrm/salary/employee-info/get": {
                    "get": {
                        "tags": ["管理后台 - HRM 员工薪资信息"],
                        "parameters": [{"name": "employeeId", "in": "query", "required": True}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SalGetResp"}}}}},
                    }
                },
                "/admin-api/hrm/salary/employee-info/update": {
                    "put": {
                        "tags": ["管理后台 - HRM 员工薪资信息"],
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SalUpdateVO"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
            },
            "components": {
                "schemas": {
                    "CommonResultLong": {"properties": {"code": {"type": "integer"}, "data": {"type": "integer", "format": "int64"}}},
                    "SalPageResp": {"properties": {"data": {"$ref": "#/components/schemas/SalPage"}}},
                    "SalPage": {"properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/SalRespVO"}}}},
                    "SalGetResp": {"properties": {"data": {"$ref": "#/components/schemas/SalRespVO"}}},
                    "SalRespVO": {
                        "properties": {
                            "id": {"type": "integer"},
                            "employeeId": {"type": "integer"},
                            "employeeName": {"type": "string"},
                            "effectTime": {"type": "integer"},
                            "changeReason": {"type": "integer"},
                            "remark": {"type": "string"},
                            "salaryOptions": {"type": "array", "items": {"$ref": "#/components/schemas/SalOptVO"}},
                        }
                    },
                    "SalUpdateVO": {
                        "properties": {
                            "id": {"type": "integer"},
                            "employeeId": {"type": "integer"},
                            "effectTime": {"type": "string", "format": "date-time", "description": "生效日期"},
                            "changeReason": {"type": "integer", "description": "调整原因"},
                            "remark": {"type": "string"},
                            "salaryOptions": {"type": "array", "items": {"$ref": "#/components/schemas/SalOptVO"}},
                        }
                    },
                    "SalOptVO": {
                        "required": ["code", "value"],
                        "properties": {
                            "code": {"type": "integer", "description": "编码"},
                            "name": {"type": "string", "description": "名称"},
                            "value": {"type": "number", "description": "金额"},
                        },
                    },
                }
            },
        }

        class _FutureSalaryClient:
            def __init__(self):
                self.tenant = "1"
                self.read_generation = 1
                self.row = {
                    "id": 1,
                    "employeeId": 112,
                    "employeeName": "李云珂",
                    "effectTime": 1767715200000,
                    "changeReason": 0,
                    "remark": "seed",
                    "salaryOptions": [{"code": 10101, "name": "基本工资", "value": 8000.0}],
                }
                self.change_records = {}

            def read_window(self, _check):
                return nullcontext()

            def write_window(self, _check):
                return nullcontext()

            def get(self, path, **params):
                if path == "system/auth/get-permission-info":
                    return {"user": {"id": 1, "nickname": "芋道源码", "deptId": 100}, "roles": ["super_admin"], "menus": []}
                if path == "hrm/salary/employee-info/page":
                    return {"list": [dict(self.row)], "total": 1}
                if path == "hrm/salary/employee-info/get":
                    # Future-dated change keeps pre-effective value in employee-info before effective date
                    return dict(self.row)
                if path == "hrm/salary/change-record/get":
                    return dict(self.change_records[int(params["id"])])
                return []

            def write_once(self, method, path, *, body=None, params=None, operation="", expect_result="bool"):
                self.change_records[3] = {
                    "id": 3,
                    "employeeId": 112,
                    "effectTime": body["effectTime"],
                    "changeReason": body["changeReason"],
                    "salaryOptions": list(body["salaryOptions"]),
                }
                return 3

        client = _FutureSalaryClient()
        port = YudaoConventionWritePort(client, NS(_documents=docs, _source=None, _ui=None, invalidate_reads=lambda _e: None))
        handler = CompiledAggregateUpdateHandler(port)
        spec = handler.prepare({
            "entity": "hrm/salary/employee-info",
            "target": "李云珂（employeeId=112）",
            "changes": [{"field": "effectTime", "value": "2026-11-01"}],
            "line_changes": [{"collection": "salaryOptions", "match": "基本工资", "changes": [{"field": "value", "value": 9500}]}],
        }, context=op_ctx)
        res = handler.dispatch(spec.args, context=op_ctx)
        self.assertTrue(res.content["verified"])
        self.assertEqual(res.content["change_record_id"], "3")
        self.assertIn("hrm/salary/change-record ID=3", res.content["reference"])
        self.assertIn("到期前保持原薪资", res.content["reference"])
        hrm_pack = resolve_domain_pack(YUDAO_DOMAIN_PACKS, "hrm")
        self.assertIsNotNone(hrm_pack)
        self.assertIn("hrm/salary/change-record", hrm_pack.write_contract_text)

    def test_cat5_erp_po_auto_derives_unit_and_blocks_scalar_borrow(self):
        from web_operation_agent.business.compiled_writes import CompiledCreateHandler
        from web_operation_agent.business.tools.contracts import BusinessMutationProposal
        from web_operation_agent.state.identity import OperationContext
        from web_operation_agent_yudao.executor.domain_toolpacks import format_write_entry_line
        from web_operation_agent_yudao.writes.convention_writes import build_yudao_write_directory

        principal = _make_principal()
        op_ctx = OperationContext.create(
            principal,
            session_id="p51-cat5-unit",
            turn_id="turn-1",
            operation_id="op-1",
            deadline_ms=int(time.time() * 1000) + 60_000,
        )
        docs = {
            "paths": {
                "/admin-api/erp/purchase-order/create": {
                    "post": {
                        "tags": ["管理后台 - ERP 采购订单"],
                        "summary": "创建采购订单",
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErpPurchaseOrderSaveReqVO"}}}},
                        "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/erp/purchase-order/get": {
                    "get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CommonResultErpPurchaseOrderRespVO"}}}}}}
                },
                "/admin-api/erp/supplier/simple-list": {"get": {"responses": {"200": {"description": "ok"}}}},
                "/admin-api/erp/product/simple-list": {"get": {"responses": {"200": {"description": "ok"}}}},
                "/admin-api/erp/product-unit/simple-list": {"get": {"responses": {"200": {"description": "ok"}}}},
                "/admin-api/wms/warehouse/simple-list": {"get": {"responses": {"200": {"description": "ok"}}}},
                "/admin-api/erp/warehouse/simple-list": {"get": {"responses": {"200": {"description": "ok"}}}},
                "/admin-api/wms/item-sku/page": {"get": {"responses": {"200": {"description": "ok"}}}},
                "/admin-api/wms/receipt-order/create": {
                    "post": {
                        "tags": ["管理后台 - WMS 入库单"],
                        "summary": "创建入库单",
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/WmsReceiptOrderSaveReqVO"}}}},
                        "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/wms/receipt-order/get": {
                    "get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CommonResultWmsReceiptOrderRespVO"}}}}}}
                },
            },
            "components": {
                "schemas": {
                    "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                    "CommonResultErpPurchaseOrderRespVO": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/ErpPurchaseOrderRespVO"}}},
                    "CommonResultWmsReceiptOrderRespVO": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/WmsReceiptOrderRespVO"}}},
                    "ErpPurchaseOrderItemVO": {
                        "type": "object",
                        "required": ["productId", "productUnitId", "count"],
                        "properties": {
                            "productId": {"type": "integer", "description": "产品编号"},
                            "productUnitId": {"type": "integer", "description": "产品单位编号"},
                            "count": {"type": "number", "description": "数量"},
                        },
                    },
                    "ErpPurchaseOrderSaveReqVO": {
                        "type": "object",
                        "required": ["supplierId", "orderTime"],
                        "properties": {
                            "supplierId": {"type": "integer", "description": "供应商编号"},
                            "orderTime": {"type": "string", "format": "date-time", "description": "采购时间"},
                            "items": {"type": "array", "minItems": 1, "description": "采购订单明细", "items": {"$ref": "#/components/schemas/ErpPurchaseOrderItemVO"}},
                        },
                    },
                    "ErpPurchaseOrderRespVO": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "supplierId": {"type": "integer"},
                            "orderTime": {"type": "string", "format": "date-time"},
                            "items": {"type": "array", "items": {"$ref": "#/components/schemas/ErpPurchaseOrderItemVO"}},
                        },
                    },
                    "WmsReceiptDetailVO": {
                        "type": "object",
                        "required": ["skuId", "quantity"],
                        "properties": {
                            "skuId": {"type": "integer", "description": "物料规格"},
                            "warehouseId": {"type": "integer", "description": "仓库编号"},
                            "quantity": {"type": "number", "description": "数量"},
                        },
                    },
                    "WmsReceiptOrderSaveReqVO": {
                        "type": "object",
                        "required": ["no", "warehouseId"],
                        "properties": {
                            "no": {"type": "string", "description": "单号"},
                            "warehouseId": {"type": "integer", "description": "仓库编号"},
                            "details": {"type": "array", "description": "入库明细", "items": {"$ref": "#/components/schemas/WmsReceiptDetailVO"}},
                        },
                    },
                    "WmsReceiptOrderRespVO": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "no": {"type": "string"},
                            "warehouseId": {"type": "integer"},
                            "details": {"type": "array", "items": {"$ref": "#/components/schemas/WmsReceiptDetailVO"}},
                        },
                    },
                }
            },
        }

        class _Cat5Client:
            def __init__(self):
                self.tenant = "1"
                self.read_generation = 1
                self.created = {}

            def read_window(self, _check):
                return nullcontext()

            def write_window(self, _check):
                return nullcontext()

            def get(self, path, **params):
                if path == "system/auth/get-permission-info":
                    return {"user": {"id": 257, "nickname": "芋道源码", "deptId": 100}, "roles": ["super_admin"], "menus": []}
                if path == "erp/supplier/simple-list":
                    return [{"id": 11, "name": "华为供应商"}]
                if path == "erp/product/simple-list":
                    return [{"id": 21, "name": "M6螺丝", "unitId": 31, "unitName": "盒"}]
                if path == "erp/product/get":
                    return {"id": 21, "name": "M6螺丝", "unitId": 31, "unitName": "盒"}
                if path == "erp/product-unit/simple-list":
                    return [{"id": 31, "name": "盒"}]
                if path == "erp/purchase-order/get":
                    return dict(self.created[int(params["id"])])
                return []

            def write_once(self, method, path, *, body=None, params=None, operation="", expect_result="bool"):
                self.created[501] = {"id": 501, **body}
                return 501

        client = _Cat5Client()
        port = YudaoConventionWritePort(client, NS(_documents=docs, _source=None, _ui=None, invalidate_reads=lambda _e: None))
        wms_spec = port.create_specs()["wms/receipt-order"]
        wms_wh = next(f for f in wms_spec.fields if f.name == "warehouseId")
        self.assertEqual(wms_wh.reference_entity, "wms/warehouse")
        wms_sku = next(f for f in wms_spec.collections[0].fields if f.name == "skuId")
        self.assertEqual(wms_sku.reference_entity, "wms/item-sku")

        directory = build_yudao_write_directory(docs=docs, source=None, ui=None)
        po_entry = next(e for e in directory if e["kind"] == "compiled_create" and e["entity"] == "erp/purchase-order")
        self.assertIn("items", po_entry["collections"])
        contract_line = format_write_entry_line(po_entry)
        self.assertIn("lines=[", contract_line)
        self.assertIn("productUnitId随productId自动带出", contract_line)

        handler = CompiledCreateHandler(port)
        bad_inputs = {
            "entity": "erp/purchase-order",
            "fields": [{"field": "supplierId", "value": "华为供应商"}, {"field": "orderTime", "value": "2026-10-25"}],
            "lines": [{"collection": "items", "fields": [{"field": "productId", "value": "M6螺丝"}, {"field": "count", "value": 9}]}],
        }
        bad_spec = handler.prepare(bad_inputs, context=op_ctx)
        bad_proposal = BusinessMutationProposal(
            tool="create_record",
            args=bad_spec.args,
            effect=bad_spec.effect,
            targets=bad_spec.targets,
            nonce="0" * 32,
            details=bad_spec.details,
            effect_projection=bad_spec.effect_projection,
        )
        with self.assertRaises(BusinessToolRequestError) as ctx_borrow:
            handler.validate_provenance(
                bad_inputs,
                ("给华为供应商新建采购订单，采购日期 2026-10-25，明细要 M6螺丝 5",),
                bad_proposal,
                existing_values={"erp/supplier": ("华为供应商",), "erp/product": ("M6螺丝",), "erp/purchase-order": ("9",)},
            )
        self.assertIn("9", str(ctx_borrow.exception))

        good_inputs = {
            "entity": "erp/purchase-order",
            "fields": [{"field": "supplierId", "value": "华为供应商"}, {"field": "orderTime", "value": "2026-10-25"}],
            "lines": [{"collection": "items", "fields": [{"field": "productId", "value": "M6螺丝"}, {"field": "count", "value": 5}]}],
        }
        spec = handler.prepare(good_inputs, context=op_ctx)
        good_proposal = BusinessMutationProposal(
            tool="create_record",
            args=spec.args,
            effect=spec.effect,
            targets=spec.targets,
            nonce="1" * 32,
            details=spec.details,
            effect_projection=spec.effect_projection,
        )
        handler.validate_provenance(
            good_inputs,
            ("给华为供应商新建采购订单，采购日期 2026-10-25，明细要 M6螺丝 5",),
            good_proposal,
            existing_values={"erp/supplier": ("华为供应商",), "erp/product": ("M6螺丝",), "erp/purchase-order": ("9",)},
        )
        self.assertTrue(any("（主数据属性）" in d.text for d in spec.details))
        self.assertEqual(spec.args["payload"]["items"][0]["productUnitId"], 31)
        res = handler.dispatch(spec.args, context=op_ctx)
        self.assertTrue(res.content["verified"])

    def test_fill_fields_expands_collapsed_filter_once_and_settles_after_search(self):
        from dataclasses import replace
        from web_operation_agent.execution.model_loop_contracts import LoopToolResult
        from web_operation_agent.privacy import SessionHandoffPolicy
        from web_operation_agent_yudao.executor.page_prompts import SlimTurnContext
        from web_operation_agent_yudao.tests.slim_loop_support import element, observation

        proc = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        proc.knowledge = NS(handoffs=SessionHandoffPolicy.extending(()))
        proc.goal_port = NS(data=NS(choice_label=lambda route, label, value: value))
        proc.turn_context = SlimTurnContext(current_request="筛选客户名称为测试客户并搜索")
        turn_state = {"touched": [], "trace": []}
        proc._ctx = lambda: NS(executor_turn=turn_state, write_intent=None, current_request=proc.turn_context.current_request, earlier_requests=())
        proc._note = lambda *a, **kw: None
        proc.form_fill = None

        expand_btn = element(1, "button", "展开", "button", page_control="query")
        search_btn = element(2, "button", "搜索", "button", page_control="query")
        obs_collapsed = replace(
            observation(),
            page_route="/crm/customer",
            render_state="ready",
            elements=[expand_btn, search_btn],
            content="[1]<button>展开</button>\n[2]<button>搜索</button>",
        )
        hidden_input = element(3, "textbox", "客户名称", "input", page_control="query", field_label="客户名称")
        obs_expanded = replace(
            obs_collapsed,
            epoch=2,
            elements=[replace(e, epoch=2) for e in (expand_btn, search_btn, hidden_input)],
            content="[1]<button>展开</button>\n[2]<button>搜索</button>\n[3]<input field_label=客户名称 />",
        )

        actions = []
        settled_calls = []
        state = NS(obs=obs_collapsed, terminal_reason="", operation=None, page=NS())

        def fake_act(s, action, args, **kw):
            actions.append((action, args, kw))
            if action == "click" and args.get("ref") == "e1":
                s.obs = obs_expanded
                return None
            if action == "type" and args.get("ref") == "e3":
                return None
            if action == "click" and args.get("ref") == "e2":
                return LoopToolResult("查询动作已核验。", kind="page", dispatch="acknowledged", completion_command="cmd-search")
            return None

        proc._act = fake_act
        proc._settled = lambda s, **kw: settled_calls.append((s.obs.epoch, kw)) or "still"

        out = proc._fill_fields(
            state,
            [{"label": "客户名称", "value": "测试客户"}],
            "搜索",
            turn_state,
            query_only=True,
        )
        self.assertEqual(out.completion_command, "cmd-search")
        self.assertEqual([(a, ar.get("ref")) for a, ar, _ in actions], [("click", "e1"), ("fill", "e3"), ("click", "e2")])
        self.assertGreaterEqual(len(settled_calls), 2)

    def test_step1b_erp_customer_fk_spu_computed_and_scaled_create_default(self):
        from web_operation_agent.business.compiled_writes import (
            CompiledChildCollectionSpec,
            CompiledCreateSpec,
            CompiledFieldDefault,
        )
        from web_operation_agent.business.convention_writes import ConventionFieldSpec
        from web_operation_agent_yudao.rules.compiled_write_source import compile_yudao_write_catalog

        docs = {
            "paths": {
                "/admin-api/erp/sale-order/create": {
                    "post": {
                        "tags": ["管理后台 - ERP 销售订单"],
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SaleSaveReqVO"}}}},
                        "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/erp/sale-order/get": {
                    "get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CommonResultSaleRespVO"}}}}}}
                },
                "/admin-api/erp/finance-receipt/create": {
                    "post": {
                        "tags": ["管理后台 - ERP 收款单"],
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ReceiptSaveReqVO"}}}},
                        "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/erp/finance-receipt/get": {
                    "get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CommonResultReceiptRespVO"}}}}}}
                },
                "/admin-api/product/spu/create": {
                    "post": {
                        "tags": ["管理后台 - 商品 SPU"],
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SpuSaveReqVO"}}}},
                        "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/product/spu/get-detail": {
                    "get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CommonResultSpuRespVO"}}}}}}
                },
                "/admin-api/erp/customer/simple-list": {"get": {"responses": {"200": {"description": "ok"}}}},
                "/admin-api/crm/customer/page": {"get": {"responses": {"200": {"description": "ok"}}}},
                "/admin-api/fms/config/account-set/list": {"get": {"responses": {"200": {"description": "ok"}}}},
            },
            "components": {
                "schemas": {
                    "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                    "CommonResultSaleRespVO": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/SaleSaveReqVO"}}},
                    "CommonResultReceiptRespVO": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/ReceiptSaveReqVO"}}},
                    "CommonResultSpuRespVO": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/SpuRespVO"}}},
                    "SaleItemVO": {
                        "type": "object",
                        "required": ["productId"],
                        "properties": {"productId": {"type": "integer"}, "remark": {"type": "string"}},
                    },
                    "SaleSaveReqVO": {
                        "type": "object",
                        "required": ["customerId", "saleUserId", "items"],
                        "properties": {
                            "customerId": {"type": "integer"},
                            "saleUserId": {"type": "integer"},
                            "items": {"type": "array", "items": {"$ref": "#/components/schemas/SaleItemVO"}},
                        },
                    },
                    "ReceiptItemVO": {
                        "type": "object",
                        "required": ["bizType"],
                        "properties": {"bizType": {"type": "integer"}},
                    },
                    "ReceiptSaveReqVO": {
                        "type": "object",
                        "required": ["customerId", "discountPrice", "items"],
                        "properties": {
                            "customerId": {"type": "integer"},
                            "discountPrice": {"type": "number"},
                            "financeUserId": {"type": "integer"},
                            "items": {"type": "array", "items": {"$ref": "#/components/schemas/ReceiptItemVO"}},
                        },
                    },
                    "SpuSkuVO": {
                        "type": "object",
                        "required": ["price", "picUrl", "stock"],
                        "properties": {
                            "price": {"type": "integer", "description": "价格 单位：分"},
                            "picUrl": {"type": "string"},
                            "stock": {"type": "integer"},
                        },
                    },
                    "SpuSaveReqVO": {
                        "type": "object",
                        "required": ["name", "skus"],
                        "properties": {
                            "name": {"type": "string"},
                            "accountSetId": {"type": "integer"},
                            "salesCount": {"type": "integer"},
                            "browseCount": {"type": "integer"},
                            "skus": {"type": "array", "items": {"$ref": "#/components/schemas/SpuSkuVO"}},
                        },
                    },
                    "SpuRespVO": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "status": {"type": "integer"},
                            "salesCount": {"type": "integer"},
                            "browseCount": {"type": "integer"},
                            "skus": {"type": "array", "items": {"$ref": "#/components/schemas/SpuSkuVO"}},
                        },
                    },
                }
            },
        }
        cat = compile_yudao_write_catalog(
            docs=docs,
            source=None,
            ui=None,
            dedicated_creates=frozenset(),
            dedicated_updates=frozenset(),
            dedicated_statuses=frozenset(),
            ui_metadata_fn=lambda *_a: ({}, {}, {}),
            enable_cat5_master_detail_creates=True,
        )
        sale_spec = cat["creates"]["erp/sale-order"]
        sale_fields = {f.name: f for f in sale_spec.fields}
        self.assertEqual(sale_fields["customerId"].reference_entity, "erp/customer")
        self.assertEqual(sale_fields["saleUserId"].reference_entity, "system/user")

        receipt_spec = cat["creates"]["erp/finance-receipt"]
        receipt_fields = {f.name: f for f in receipt_spec.fields}
        self.assertEqual(receipt_fields["customerId"].reference_entity, "erp/customer")
        self.assertEqual(receipt_fields["financeUserId"].reference_entity, "system/user")
        self.assertNotIn("discountPrice", receipt_spec.server_computed_fields)

        spu_spec = cat["creates"]["product/spu"]
        self.assertTrue({"status", "salesCount", "browseCount"} <= spu_spec.server_computed_fields)
        self.assertEqual({f.name: f for f in spu_spec.fields}["accountSetId"].reference_entity, "fms/config/account-set")

        # encode_create_default scales nonzero numeric UI literal defaults for fen/zhe fields
        dict_rows = [{"dictType": "pms_work_item_type", "label": "需求", "value": "2"}, {"dictType": "pms_work_item_type", "label": "缺陷", "value": "4"}]
        port = YudaoConventionWritePort(
            NS(tenant="1", read_generation=1, get=lambda path, **kw: dict_rows if path == "system/dict-data/simple-list" else None),
            NS(_documents=docs, _source=None, _ui=None, invalidate_reads=lambda _e: None),
        )
        self.assertEqual(port._ref_endpoint_for("fms/config/account-set"), ("fms/config/account-set/list", ("companyName", "name")))
        item_types_field = ConventionFieldSpec(name="itemTypes", kind="id_array", label="工作项类型", required=True, enum_type="pms_work_item_type")
        arr_codes, _old, arr_disp, arr_bind = port.encode_field(
            CompiledCreateSpec(entity="pms/pm/project-template", label="项目模板", fields=(item_types_field,)),
            item_types_field,
            ["需求", 4],
            {},
        )
        self.assertEqual(arr_codes, [2, 4])
        self.assertIn("需求", arr_disp)
        self.assertEqual(arr_bind["kind"], "batch")
        self.assertEqual([item["code"] for item in arr_bind["items"]], [2, 4])
        dummy_spec = CompiledCreateSpec(
            entity="trade/delivery/express-template",
            label="运费模板",
            fields=(ConventionFieldSpec(name="name", kind="string", label="模板名称", required=True),),
            collections=(
                CompiledChildCollectionSpec(
                    name="charges",
                    label="计费",
                    fields=(ConventionFieldSpec(name="startPrice", kind="integer", label="首费", required=True, unit_scale=100, display_unit="元"),),
                ),
            ),
        )
        api_val, disp, _binding = port.encode_create_default(
            dummy_spec,
            dummy_spec.collections[0].fields[0],
            CompiledFieldDefault(field="startPrice", value=1, kind="literal", display="1"),
            actor_id="1",
        )
        self.assertEqual(api_val, 100)
        self.assertIn("1", str(disp))

    def test_step1c_domain_packs_read_contracts_and_zero_case_hardcoding(self):
        import re
        from web_operation_agent_yudao.executor.domain_toolpacks import YUDAO_DOMAIN_PACKS

        self.assertEqual(len(YUDAO_DOMAIN_PACKS), 8)
        for pack in YUDAO_DOMAIN_PACKS:
            self.assertGreater(len(pack.read_contract_text.strip()), 0, pack.domain_id)
            self.assertLessEqual(len(pack.read_contract_text), 350, f"{pack.domain_id}: {len(pack.read_contract_text)}")
            self.assertGreater(len(pack.write_contract_text.strip()), 0, pack.domain_id)

        wt_root = Path(__file__).resolve().parents[4]
        case_pat = re.compile(r"\b[A-Z]\d-\d{2}\b")
        offenders: list[str] = []
        for scan_root in (wt_root / "src", wt_root / "examples" / "yudao" / "src"):
            for py in sorted(scan_root.rglob("*.py")):
                if "tests" in py.parts or "__pycache__" in py.parts:
                    continue
                if case_pat.search(py.read_text(encoding="utf-8", errors="ignore")):
                    offenders.append(str(py.relative_to(wt_root)))
        self.assertEqual(offenders, [], f"Unexpected benchmark case IDs in reusable/adapter code: {offenders}")

    def test_step2r2_salary_change_record_related_entity_ranks_first(self):
        from web_operation_agent_yudao.executor.domain_toolpacks import search_write_directory
        from web_operation_agent_yudao.writes.convention_writes import build_yudao_write_directory

        docs = {
            "paths": {
                "/admin-api/hrm/salary/employee-info/page": {
                    "get": {
                        "tags": ["管理后台 - HRM 员工薪资信息"],
                        "parameters": [
                            {"name": "employeeId", "in": "query"},
                            {"name": "pageNo", "in": "query", "required": True},
                            {"name": "pageSize", "in": "query", "required": True},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SalPageResp"}}}}},
                    }
                },
                "/admin-api/hrm/salary/employee-info/get": {
                    "get": {
                        "tags": ["管理后台 - HRM 员工薪资信息"],
                        "parameters": [{"name": "employeeId", "in": "query", "required": True}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SalGetResp"}}}}},
                    }
                },
                "/admin-api/hrm/salary/employee-info/update": {
                    "put": {
                        "tags": ["管理后台 - HRM 员工薪资信息"],
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SalUpdateVO"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/hrm/salary/change-template/get": {
                    "get": {
                        "tags": ["管理后台 - HRM 调薪模板"],
                        "parameters": [{"name": "id", "in": "query", "required": True}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/TplGetResp"}}}}},
                    }
                },
                "/admin-api/hrm/salary/change-template/create": {
                    "post": {
                        "tags": ["管理后台 - HRM 调薪模板"],
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/TplCreateVO"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
            },
            "components": {
                "schemas": {
                    "CommonResultLong": {"properties": {"code": {"type": "integer"}, "data": {"type": "integer", "format": "int64"}}},
                    "SalPageResp": {"properties": {"data": {"$ref": "#/components/schemas/SalPage"}}},
                    "SalPage": {"properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/SalRespVO"}}}},
                    "SalGetResp": {"properties": {"data": {"$ref": "#/components/schemas/SalRespVO"}}},
                    "SalRespVO": {
                        "properties": {
                            "id": {"type": "integer"},
                            "employeeId": {"type": "integer"},
                            "effectTime": {"type": "integer"},
                            "salaryOptions": {"type": "array", "items": {"$ref": "#/components/schemas/SalOptVO"}},
                        }
                    },
                    "SalUpdateVO": {
                        "properties": {
                            "id": {"type": "integer"},
                            "employeeId": {"type": "integer"},
                            "effectTime": {"type": "string", "format": "date-time"},
                            "salaryOptions": {"type": "array", "items": {"$ref": "#/components/schemas/SalOptVO"}},
                        }
                    },
                    "SalOptVO": {
                        "required": ["code", "value"],
                        "properties": {"code": {"type": "integer"}, "name": {"type": "string"}, "value": {"type": "number"}},
                    },
                    "TplGetResp": {"properties": {"data": {"$ref": "#/components/schemas/TplCreateVO"}}},
                    "TplCreateVO": {
                        "required": ["name"],
                        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                    },
                }
            },
        }
        directory = build_yudao_write_directory(docs=docs, source=None, ui=None)
        emp_entry = next(e for e in directory if e.get("entity") == "hrm/salary/employee-info")
        self.assertEqual(emp_entry.get("related_entities"), ["hrm/salary/change-record"])

        ranked = search_write_directory("hrm/salary/change-record", docs=docs, source=None, ui=None, limit=4)
        self.assertTrue(ranked)
        self.assertEqual(ranked[0]["entity"], "hrm/salary/employee-info")

        proc = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        proc.knowledge = NS(_loaded_docs=docs)
        proc.goal_port = NS(data=NS(_source=None, _ui=None))
        lines = proc._entity_compiled_write_lines("hrm/salary/change-record", "update")
        contract_blob = "\n".join(lines)
        self.assertIn("hrm/salary/employee-info", contract_blob)
        emp_pos = contract_blob.find("hrm/salary/employee-info")
        tpl_pos = contract_blob.find("hrm/salary/change-template")
        if tpl_pos != -1:
            self.assertLess(emp_pos, tpl_pos)

    def test_step2r2_domain_pack_selection_and_read_contract_bounds(self):
        from web_operation_agent.goal.domain_packs import select_active_domains
        from web_operation_agent_yudao.executor.domain_toolpacks import YUDAO_DOMAIN_PACKS
        from web_operation_agent_yudao.executor.page_prompts import SYSTEM_READ

        for pack in YUDAO_DOMAIN_PACKS:
            self.assertLessEqual(len(pack.read_contract_text), 350, pack.domain_id)

        selected_att = select_active_domains(YUDAO_DOMAIN_PACKS, prompt="把芋道源码上个月的迟到次数和早退次数合计一下")
        self.assertEqual([d.domain_id for d in selected_att], ["hrm", "system_infra", "oa_bpm"])

        selected_bpm = select_active_domains(YUDAO_DOMAIN_PACKS, prompt="这周我驳回过的申请")
        self.assertEqual(selected_bpm[0].domain_id, "oa_bpm")

        selected_msg = select_active_domains(YUDAO_DOMAIN_PACKS, prompt="查某人发过的站内信或公告")
        self.assertIn("system_infra", [d.domain_id for d in selected_msg])

        self.assertIn("查无结果要说清条件和范围", SYSTEM_READ)

    def test_step2r2_actor_knowledge_str_time_tiny_guessed_and_zero_row_finish(self):
        class _MixedClient:
            def __init__(self, rows):
                self._rows = rows

            def get(self, path, **params):
                return {"list": list(self._rows), "total": len(self._rows)}

        docs = {
            "paths": {
                "/admin-api/promotion/coupon-template/page": {
                    "get": {
                        "tags": ["管理后台 - 优惠券模板"],
                        "parameters": [
                            {"name": "status", "in": "query"},
                            {"name": "pageNo", "in": "query", "required": True},
                            {"name": "pageSize", "in": "query", "required": True},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CouponPageResp"}}}}},
                    }
                },
                "/admin-api/erp/product/page": {
                    "get": {
                        "tags": ["管理后台 - ERP 产品"],
                        "parameters": [
                            {"name": "name", "in": "query"},
                            {"name": "categoryId", "in": "query"},
                            {"name": "pageNo", "in": "query", "required": True},
                            {"name": "pageSize", "in": "query", "required": True},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ProdPageResp"}}}}},
                    }
                },
            },
            "components": {
                "schemas": {
                    "CouponPageResp": {"properties": {"data": {"$ref": "#/components/schemas/CouponPage"}}},
                    "CouponPage": {"properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/CouponVO"}}}},
                    "CouponVO": {
                        "properties": {
                            "id": {"type": "integer"},
                            "name": {"type": "string", "description": "名称"},
                            "validEndTime": {"type": "string", "description": "有效结束时间"},
                            "status": {"type": "integer", "description": "状态"},
                        }
                    },
                    "ProdPageResp": {"properties": {"data": {"$ref": "#/components/schemas/ProdPage"}}},
                    "ProdPage": {"properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/ProdVO"}}}},
                    "ProdVO": {
                        "properties": {
                            "id": {"type": "integer"},
                            "name": {"type": "string", "description": "产品名称"},
                            "standard": {"type": "string", "description": "规格"},
                            "unitName": {"type": "string", "description": "单位名称"},
                        }
                    },
                }
            },
        }
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables.close)

        str_know = ActorKnowledge(_MixedClient([{"id": 1, "name": "国庆券", "validEndTime": "2026-10-31 23:59:59", "status": 0}]), docs)
        str_out = str_know.call_api("promotion/coupon-template/page", {"pageNo": 1, "pageSize": 20}, row_sink=tables.add)
        self.assertIn("时间列存日期时间字符串", str_out)
        self.assertIn("切勿与毫秒整数", str_out)

        int_know = ActorKnowledge(_MixedClient([{"id": 1, "name": "国庆券", "validEndTime": 1793462399000, "status": 0}]), docs)
        int_out = int_know.call_api("promotion/coupon-template/page", {"pageNo": 1, "pageSize": 20}, row_sink=tables.add)
        self.assertIn("时间列仍存原始毫秒整数", int_out)

        prod_know = ActorKnowledge(_MixedClient([{"id": 9, "name": "M6螺栓", "standard": "M6*20", "unitName": "盒"}]), docs)
        prod_out = prod_know.call_api("erp/product/page", {"name": "M6螺栓", "pageNo": 1, "pageSize": 20}, ["spec", "unit"], row_sink=tables.add)
        self.assertIn("本轮行表已保留接口返回的全部真实列", prod_out)
        self.assertIn("M6*20", prod_out)
        self.assertIn("unitName", prod_out)

        empty_know = ActorKnowledge(_MixedClient([]), docs)
        empty_status_out = empty_know.call_api("promotion/coupon-template/page", {"status": 0, "pageNo": 1, "pageSize": 20}, row_sink=tables.add)
        self.assertNotIn("精确过滤已核实为 0 条记录", empty_status_out)
        empty_out = empty_know.call_api("erp/product/page", {"categoryId": 10, "pageNo": 1, "pageSize": 20}, row_sink=tables.add)
        self.assertIn("finish(status='done')", empty_out)
        self.assertNotIn("可用 completed", empty_out)

    def test_step2r2_fill_fields_label_for_placeholder_and_then_click_expand(self):
        from dataclasses import replace
        from web_operation_agent.execution.model_loop_contracts import LoopToolResult
        from web_operation_agent.privacy import SessionHandoffPolicy
        from web_operation_agent_yudao.executor.page_prompts import SlimTurnContext
        from web_operation_agent_yudao.tests.slim_loop_support import element, observation

        proc = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        proc.knowledge = NS(handoffs=SessionHandoffPolicy.extending(()))
        proc.goal_port = NS(data=NS(choice_label=lambda route, label, value: value))
        proc.turn_context = SlimTurnContext(current_request="筛选编号并点更多后点击查询")
        turn_state = {"touched": [], "trace": []}
        proc._ctx = lambda: NS(executor_turn=turn_state, write_intent=None, current_request=proc.turn_context.current_request, earlier_requests=())
        proc._note = lambda *a, **kw: None
        proc.form_fill = None

        more_btn = element(1, "button", "更多", "button")
        search_btn = element(2, "button", "查询", "button")
        obs_collapsed = replace(
            observation(),
            page_route="/fms/voucher",
            render_state="ready",
            elements=[more_btn, search_btn],
            content="[1]<button>更多</button>\n[2]<button>查询</button>",
        )
        lbl_el = ElementRef(ref="e3", role="label", name="凭证号:", tag="label", attrs={"for": "voucher-no-input"}, epoch=2)
        for_input = ElementRef(ref="e4", role="textbox", name="", tag="input", attrs={"id": "voucher-no-input"}, epoch=2)
        ph_input = ElementRef(ref="e5", role="textbox", name="请输入制单人", tag="input", attrs={"placeholder": "请输入制单人"}, epoch=2)
        obs_expanded = replace(
            obs_collapsed,
            epoch=2,
            elements=[replace(more_btn, epoch=2), replace(search_btn, epoch=2), lbl_el, for_input, ph_input],
            content="[1]<button>更多</button>\n[2]<button>查询</button>\n[3]<label for=voucher-no-input>凭证号:</label>\n[4]<input id=voucher-no-input />\n[5]<input placeholder=请输入制单人 />",
        )

        actions = []
        state = NS(obs=obs_collapsed, terminal_reason="", operation=None, page=NS())

        def fake_act(s, action, args, **kw):
            actions.append((action, args))
            if action == "click" and args.get("ref") == "e1":
                s.obs = obs_expanded
                return None
            if action == "click" and args.get("ref") == "e2":
                return LoopToolResult("查询动作已核验。", kind="page", dispatch="acknowledged", completion_command="cmd-more-search")
            return None

        proc._act = fake_act
        proc._settled = lambda s, **kw: "still"

        out = proc._fill_fields(
            state,
            [{"label": "凭证号", "value": "记-001"}, {"label": "制单人", "value": "芋道源码"}],
            "查询",
            turn_state,
            query_only=False,
        )
        self.assertEqual(out.completion_command, "cmd-more-search")
        self.assertEqual([(a, ar.get("ref"), ar.get("value")) for a, ar in actions], [
            ("click", "e1", None),
            ("fill", "e4", "记-001"),
            ("fill", "e5", "芋道源码"),
            ("click", "e2", None),
        ])


if __name__ == "__main__":
    unittest.main()
