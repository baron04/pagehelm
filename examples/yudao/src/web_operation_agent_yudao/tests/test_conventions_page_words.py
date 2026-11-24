"""Zero-paid unit and contract tests for page-word-first convention resolution."""
from __future__ import annotations

import contextlib
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from web_operation_agent.business.approvals import ApprovalWatch
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.execution.model_loop_contracts import PageActionOutcome
from web_operation_agent.goal.conventions import load_conventions
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.index import IndexConvention
from web_operation_agent.schemas.browser import PageObservation
from web_operation_agent.state.conversation import ConversationState
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent_yudao.conventions import (
    REFERENCE_CONVENTIONS,
    label_matches_term,
    resolve_turn_conventions,
)
from web_operation_agent_yudao.executor.actor_knowledge import ActorKnowledge
from web_operation_agent_yudao.executor.slim_loop import YudaoTurnExecutor
from web_operation_agent_yudao.goal_compat.page_goals import NativePageGoalPort
from web_operation_agent_yudao.host.application import YudaoApplication
from web_operation_agent_yudao.rules.ui_source import UiSource
from web_operation_agent_yudao.tests.slim_loop_support import Budget, Channel, Ledger, Script, call, reply


_REAL_UI_DIR = next(
    (cand for parent in Path(__file__).resolve().parents if (cand := parent / ".q0-cache" / "yudao" / "ui").is_dir()),
    None,
)


def _synth_docs() -> dict:
    vo_schemas = {
        "CrmCustomerVO": {"properties": {"id": {"type": "integer"}, "name": {"type": "string", "description": "客户名称"}}},
        "ErpCustomerVO": {"properties": {"id": {"type": "integer"}, "name": {"type": "string", "description": "客户名称"}}},
        "ProjectVO": {"properties": {"id": {"type": "integer"}, "name": {"type": "string", "description": "项目名称"}}},
        "OaLeaveVO": {"properties": {"id": {"type": "integer"}, "reason": {"type": "string", "description": "原因"}, "result": {"type": "integer", "description": "审批结果"}}},
        "PlanVO": {"properties": {"id": {"type": "integer"}, "period": {"type": "integer", "description": "期数"}}},
        "ContractVO": {"properties": {"id": {"type": "integer"}, "name": {"type": "string", "description": "合同名称"}, "customerId": {"type": "integer", "description": "客户"}}},
        "OAuthClientVO": {"properties": {"id": {"type": "integer"}, "clientId": {"type": "string", "description": "客户端编号"}, "name": {"type": "string", "description": "客户端名称"}}},
        "SaleOrderVO": {"properties": {"id": {"type": "integer"}, "price": {"type": "integer", "description": "销售单价"}}},
        "NoticeVO": {"properties": {"id": {"type": "integer"}, "title": {"type": "string", "description": "公告标题"}}},
        "OaNoticeVO": {"properties": {"id": {"type": "integer"}, "title": {"type": "string", "description": "公告标题"}}},
    }
    schemas = dict(vo_schemas)
    for vo in vo_schemas:
        schemas[f"PageResult_{vo}"] = {
            "properties": {"list": {"type": "array", "items": {"$ref": f"#/components/schemas/{vo}"}}}
        }
        schemas[f"CommonResult_{vo}"] = {
            "properties": {"data": {"$ref": f"#/components/schemas/PageResult_{vo}"}}
        }

    def _page_op(summary: str, tag: str, schema_ref: str) -> dict:
        return {
            "get": {
                "summary": summary,
                "tags": [f"管理后台 - {tag}"],
                "parameters": [
                    {"name": "pageNo", "in": "query", "required": True, "schema": {"type": "integer"}},
                    {"name": "pageSize", "in": "query", "required": True, "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {
                        "content": {
                            "*/*": {
                                "schema": {"$ref": f"#/components/schemas/CommonResult_{schema_ref}"}
                            }
                        }
                    }
                },
            }
        }

    return {
        "paths": {
            "/admin-api/crm/customer/page": _page_op("CRM客户分页", "CRM 客户", "CrmCustomerVO"),
            "/admin-api/erp/customer/page": _page_op("ERP客户分页", "ERP 客户", "ErpCustomerVO"),
            "/admin-api/pms/pm/project/page": _page_op("项目分页", "PMS 项目", "ProjectVO"),
            "/admin-api/bpm/oa/leave/page": _page_op("OA请假分页", "OA 请假申请", "OaLeaveVO"),
            "/admin-api/crm/receivable-plan/page": _page_op("CRM回款计划分页", "CRM 回款计划", "PlanVO"),
            "/admin-api/crm/contract/page": _page_op("CRM合同分页", "CRM 合同", "ContractVO"),
            "/admin-api/system/oauth2-client/page": _page_op("客户端分页", "OAuth2 客户端", "OAuthClientVO"),
            "/admin-api/erp/sale-order/page": _page_op("ERP销售订单分页", "ERP 销售订单", "SaleOrderVO"),
            "/admin-api/system/notice/page": _page_op("系统公告分页", "通知公告", "NoticeVO"),
            "/admin-api/oa/announcement/page": _page_op("OA公告分页", "OA 公告", "OaNoticeVO"),
        },
        "components": {"schemas": schemas},
    }


def _write_synth_ui(root: Path) -> UiSource:
    (root / "src/utils").mkdir(parents=True)
    (root / "src/router/modules").mkdir(parents=True)
    (root / "src/api/crm/customer").mkdir(parents=True)
    (root / "src/api/erp/customer").mkdir(parents=True)
    (root / "src/api/pms/pm/project").mkdir(parents=True)
    (root / "src/api/bpm/oa/leave").mkdir(parents=True)
    (root / "src/api/crm/receivable").mkdir(parents=True)
    (root / "src/api/crm/contract").mkdir(parents=True)
    (root / "src/api/system/oauth2").mkdir(parents=True)
    (root / "src/api/erp/sale").mkdir(parents=True)
    (root / "src/api/system/notice").mkdir(parents=True)
    (root / "src/api/oa/announcement").mkdir(parents=True)

    (root / "src/utils/dict.ts").write_text(
        "export enum DICT_TYPE {\n"
        "  BPM_PROCESS_INSTANCE_STATUS = 'bpm_process_instance_status',\n"
        "  SYSTEM_NOTICE_TYPE = 'system_notice_type',\n"
        "}\n",
        encoding="utf-8",
    )

    pages = {
        "crm/customer/index.vue": (
            "<template><el-table-column label=\"客户名称\" prop=\"name\" /></template>\n"
            "<script setup lang=\"ts\">import { getCustomerPage } from '@/api/crm/customer'\n"
            "const getList = async () => { await getCustomerPage() }</script>\n"
        ),
        "crm/customer/detail/index.vue": (
            "<template><el-descriptions><el-descriptions-item label=\"客户名称\" /></el-descriptions></template>\n"
        ),
        "erp/customer/index.vue": (
            "<template><el-table-column label=\"客户名称\" prop=\"name\" /></template>\n"
        ),
        "pms/pm/project/list/index.vue": (
            "<template>"
            "<el-tabs><el-tab-pane label=\"全部项目\" /><el-tab-pane label=\"我负责的\" /></el-tabs>"
            "<el-table-column label=\"项目名称\" prop=\"name\" />"
            "</template>\n"
            "<script setup lang=\"ts\">\n"
            "import { getProjectPage } from '@/api/pms/pm/project'\n"
            "const getList = async () => {\n"
            "  await getProjectPage()\n"
            "}\n"
            "</script>\n"
        ),
        "bpm/oa/leave/index.vue": (
            "<template>"
            "<el-form :model=\"queryParams\"><el-form-item label=\"审批结果\">"
            "<el-select v-model=\"queryParams.result\">"
            "<el-option v-for=\"d in getIntDictOptions(DICT_TYPE.BPM_PROCESS_INSTANCE_STATUS)\" :key=\"d.value\" :label=\"d.label\" :value=\"d.value\" />"
            "</el-select></el-form-item></el-form>"
            "<el-table-column label=\"原因\" prop=\"reason\" />"
            "</template>\n"
        ),
        "crm/receivable/plan/index.vue": (
            "<template><el-table-column label=\"期数\" prop=\"period\" /></template>\n"
        ),
        "crm/contract/index.vue": (
            "<template><el-table-column label=\"合同名称\" prop=\"name\" /><el-table-column label=\"客户\" prop=\"customerId\" /></template>\n"
        ),
        "system/oauth2/client/index.vue": (
            "<template><el-table-column label=\"客户端编号\" prop=\"clientId\" /><el-table-column label=\"客户端名称\" prop=\"name\" /></template>\n"
        ),
        "erp/sale/order/index.vue": (
            "<template><el-table-column label=\"销售单价（元）\" prop=\"price\" /></template>\n"
        ),
        "system/notice/index.vue": (
            "<template><el-table-column label=\"公告标题\" prop=\"title\" />"
            "<el-table-column label=\"公告类型\"><template #default=\"scope\"><dict-tag :type=\"DICT_TYPE.SYSTEM_NOTICE_TYPE\" :value=\"scope.row.type\" /></template></el-table-column>"
            "</template>\n"
        ),
        "oa/announcement/index.vue": (
            "<template><el-table-column label=\"公告标题\" prop=\"title\" /></template>\n"
        ),
        "system/notify/my/index.vue": (
            "<template><el-table-column label=\"发送人\" prop=\"userId\" />"
            "<el-tabs><el-tab-pane label=\"未回款通知\" /></el-tabs></template>\n"
        ),
    }
    for rel, content in pages.items():
        p = root / "src/views" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    (root / "src/api/crm/customer/index.ts").write_text(
        "export const getCustomerPage = () => request.get({ url: '/crm/customer/page' })\n",
        encoding="utf-8",
    )
    (root / "src/api/pms/pm/project/index.ts").write_text(
        "export const getProjectPage = () => request.get({ url: '/pms/pm/project/page' })\n",
        encoding="utf-8",
    )
    (root / "src/router/modules/remaining.ts").write_text(
        "const remainingRouter: AppRouteRecordRaw[] = [\n"
        "  {\n"
        "    path: '/crm',\n"
        "    children: [{\n"
        "      path: 'customer/detail/:id(\\\\d+)',\n"
        "      component: () => import('@/views/crm/customer/detail/index.vue'),\n"
        "      meta: { activeMenu: '/crm/customer' },\n"
        "    }],\n"
        "  },\n"
        "  {\n"
        "    path: '/user/notify-message',\n"
        "    component: () => import('@/views/system/notify/my/index.vue'),\n"
        "    meta: { hidden: true },\n"
        "  },\n"
        "]\n",
        encoding="utf-8",
    )
    return UiSource.read(root)


class _SynthClient:
    tenant = "1"
    read_generation = 0

    def __init__(self):
        self.menus = [
            {"name": "CRM 系统", "path": "/crm", "component": "Layout", "children": [
                {"name": "客户管理", "path": "customer", "component": "crm/customer/index"},
                {"name": "合同管理", "path": "contract", "component": "crm/contract/index"},
                {"name": "回款计划", "path": "receivable-plan", "component": "crm/receivable/plan/index"},
            ]},
            {"name": "ERP 系统", "path": "/erp", "component": "Layout", "children": [
                {"name": "客户信息", "path": "customer", "component": "erp/customer/index"},
                {"name": "销售订单", "path": "sale-order", "component": "erp/sale/order/index"},
            ]},
            {"name": "项目管理", "path": "/pms", "component": "Layout", "children": [
                {"name": "项目列表", "path": "pm/project/list", "component": "pms/pm/project/list/index"},
            ]},
            {"name": "OA 办公", "path": "/bpm/oa", "component": "Layout", "children": [
                {"name": "请假申请", "path": "leave", "component": "bpm/oa/leave/index"},
                {"name": "公告列表", "path": "announcement", "component": "oa/announcement/index"},
            ]},
            {"name": "系统管理", "path": "/system", "component": "Layout", "children": [
                {"name": "客户端管理", "path": "oauth2-client", "component": "system/oauth2/client/index"},
                {"name": "通知公告", "path": "notice", "component": "system/notice/index"},
            ]},
        ]

    @staticmethod
    def read_window(_check):
        return contextlib.nullcontext()

    def get(self, path, **params):
        if path == "system/auth/get-permission-info":
            return {
                "user": {"id": 9, "deptId": 7, "nickname": "测试员"},
                "permissions": [
                    "crm:customer:query",
                    "crm:contract:query",
                    "crm:receivable-plan:query",
                    "erp:customer:query",
                    "erp:sale-order:query",
                    "pms:pm:project:query",
                    "bpm:oa:leave:query",
                    "oa:announcement:query",
                    "system:oauth2-client:query",
                    "system:notice:query",
                ],
                "menus": self.menus,
            }
        if path == "system/dict-data/simple-list":
            return [
                {"dictType": "bpm_process_instance_status", "value": "2", "label": "审批通过"},
                {"dictType": "bpm_process_instance_status", "value": "1", "label": "审批中"},
                {"dictType": "system_notice_type", "value": "1", "label": "通知"},
                {"dictType": "system_notice_type", "value": "2", "label": "公告"},
            ]
        if path == "system/dict-type/simple-list":
            return [
                {"type": "bpm_process_instance_status", "name": "流程实例状态"},
                {"type": "system_notice_type", "name": "通知公告类型"},
            ]
        raise AssertionError(f"unexpected GET {path} {params}")


class ConventionsPageWordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.docs = _synth_docs()
        self.ui = _write_synth_ui(self.root)
        self.client = _SynthClient()
        self.conventions = (
            IndexConvention(("客户",), "crm/customer (CRM 客户)", "crm/customer"),
            IndexConvention(("我负责的",), "crm/customer records where owner matches current user", "crm/customer"),
            IndexConvention(("待回款", "未回款", "还没回款"), "crm/receivable-plan records whose 回款 is empty"),
            IndexConvention(("产品",), "erp/product (ERP 产品)", "erp/product"),
            IndexConvention(("销售单",), "erp/sale-order (ERP 销售单)", "erp/sale-order"),
            IndexConvention(("待审批", "审批中", "还没批"), "bpm/process-instance records in progress", "bpm/process-instance"),
            IndexConvention(("公告",), "system/notice (公告类型=公告)", "system/notice"),
            IndexConvention(("通知公告",), "system/notice (全部通知公告)", "system/notice"),
            IndexConvention(("我能看的",), "every record readable by current user"),
        )
        self.app = YudaoApplication(
            "https://synthetic.test",
            None,
            self.docs,
            client=self.client,
            ui=self.ui,
            conventions=None,
        )
        self.app.goal_data.attach_conventions(self.conventions)
        self.goal_data = self.app.goal_data
        self.knowledge = ActorKnowledge(self.client, self.docs)
        self.knowledge._load()

    def test_morpheme_boundary_rules_distinguish_compounds_from_false_positives(self):
        self.assertTrue(label_matches_term("客户：", "客户"))
        self.assertTrue(label_matches_term("客户（元）", "客户"))
        self.assertTrue(label_matches_term("客户名称", "客户"))
        self.assertTrue(label_matches_term("销售单状态", "销售单"))
        self.assertTrue(label_matches_term("未回款金额（元）", "未回款"))
        self.assertTrue(label_matches_term("通知公告", "通知公告", sibling_longer_terms=()))

        self.assertFalse(label_matches_term("客户端", "客户"))
        self.assertFalse(label_matches_term("客户端名称", "客户"))
        self.assertFalse(label_matches_term("资产品", "产品"))
        self.assertFalse(label_matches_term("固定资产品牌", "产品"))
        self.assertFalse(label_matches_term("销售单价", "销售单"))
        self.assertFalse(label_matches_term("销售单价（元）", "销售单"))
        self.assertFalse(label_matches_term("采购单价", "采购单"))
        self.assertFalse(label_matches_term("通知公告", "公告", sibling_longer_terms=("通知公告",)))
        self.assertFalse(label_matches_term("通知公告", "公告"))
        self.assertTrue(label_matches_term("OAuth客户端及客户名称", "客户"))

    def test_rule1_convention_remains_active_on_its_own_target_entity(self):
        res = resolve_turn_conventions(
            self.conventions,
            prompt="查CRM客户列表",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
        )
        self.assertIn("crm/customer", res.target_entities)
        active_terms = {c.terms[0] for c in res.active}
        self.assertIn("客户", active_terms)
        self.assertIn("我负责的", active_terms)

        # Convention #11 derives its target entity `crm/receivable-plan` from `meaning` when `entity == ""`.
        res_plan = resolve_turn_conventions(
            self.conventions,
            prompt="查回款计划里待回款的",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
        )
        self.assertIn("crm/receivable-plan", res_plan.target_entities)
        self.assertIn("待回款", {c.terms[0] for c in res_plan.active})

    def test_rule2_non_target_page_words_suppress_exact_and_compounds_but_not_false_positives(self):
        # ERP customer suppresses "客户" (due to "客户名称") while unrelated conventions stay active.
        res_erp = resolve_turn_conventions(
            self.conventions,
            prompt="查一下ERP里的客户",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
        )
        self.assertEqual(res_erp.target_entities, ("erp/customer",))
        suppressed_map = {s.convention.terms[0]: s.matched_words for s in res_erp.suppressed}
        self.assertIn("客户", suppressed_map)
        self.assertIn("客户名称", suppressed_map["客户"])
        self.assertIn("我负责的", {c.terms[0] for c in res_erp.active})

        # PMS project suppresses "我负责的" via its tab label, without dropping unrelated conventions.
        res_pms = resolve_turn_conventions(
            self.conventions,
            prompt="看项目列表里我负责的",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
        )
        self.assertEqual(res_pms.target_entities, ("pms/pm/project",))
        self.assertNotIn("我负责的", {c.terms[0] for c in res_pms.active})
        self.assertIn("我能看的", {c.terms[0] for c in res_pms.active})

        # BPM OA leave query dictionary contains "审批中" -> suppresses BPM "待审批 / 审批中".
        res_leave = resolve_turn_conventions(
            self.conventions,
            prompt="请假申请里审批中的记录有哪些",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
        )
        self.assertEqual(res_leave.target_entities, ("bpm/oa/leave",))
        suppressed_leave = {s.convention.terms[0]: s.matched_words for s in res_leave.suppressed}
        self.assertIn("待审批", suppressed_leave)
        self.assertIn("审批中", suppressed_leave["待审批"])

        # OAuth2 client only has "客户端编号"/"客户端名称" -> does NOT suppress "客户".
        res_client = resolve_turn_conventions(
            self.conventions,
            prompt="看应用管理里的客户端",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
            active_route="/system/oauth2-client",
        )
        self.assertIn("客户", {c.terms[0] for c in res_client.active})

        # ERP sale order has "销售单价（元）" -> does NOT suppress "销售单".
        res_sale = resolve_turn_conventions(
            self.conventions,
            prompt="ERP销售订单的销售单价是多少",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
        )
        self.assertIn("erp/sale-order", res_sale.target_entities)
        self.assertIn("销售单", {c.terms[0] for c in res_sale.active})

        # CRM contract does not accidentally match "CRM客户" via shared module prefix.
        res_contract = resolve_turn_conventions(
            self.conventions,
            prompt="查CRM合同中的客户",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
        )
        self.assertEqual(res_contract.target_entities, ("crm/contract",))
        self.assertNotIn("客户", {c.terms[0] for c in res_contract.active})

    def test_rule3_unresolved_prompt_and_hub_page_keep_all_conventions_active(self):
        # Truly unresolved convention prompts on a hub route must stay unresolved with all conventions active,
        # and multi-char non-entity conventions ("待回款") must not bleed into shorter substrings ("回款").
        convs_with_receivable = (
            *self.conventions,
            IndexConvention(("回款",), "crm/receivable: money received", "crm/receivable"),
        )
        for prompt in ("查一下我能看的记录", "查一下待回款"):
            res = resolve_turn_conventions(
                convs_with_receivable,
                prompt=prompt,
                goal_data=self.goal_data,
                knowledge=self.knowledge,
                active_route="/home",
            )
            self.assertEqual(res.source, "unresolved")
            self.assertEqual(res.active, convs_with_receivable)
            self.assertEqual(res.suppressed, ())

        # Entity-naming convention ("客户" -> crm/customer) resolves to its own target entity, never erp/customer.
        res_customer = resolve_turn_conventions(
            convs_with_receivable,
            prompt="查一下客户列表",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
            active_route="/home",
        )
        self.assertEqual(res_customer.source, "convention_entity")
        self.assertEqual(res_customer.target_entities, ("crm/customer",))
        self.assertIn("客户", {c.terms[0] for c in res_customer.active})

    def test_prompt_entity_overrides_active_page_while_empty_prompt_falls_back(self):
        # Active page is PMS project, but prompt explicitly switches to CRM customer.
        res_override = resolve_turn_conventions(
            self.conventions,
            prompt="查CRM客户里我负责的",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
            active_route="/pms/pm/project/list",
        )
        self.assertEqual(res_override.source, "prompt")
        self.assertEqual(res_override.target_entities, ("crm/customer",))
        self.assertIn("我负责的", {c.terms[0] for c in res_override.active})

        # Active page is PMS project, prompt names no entity -> falls back to active_page.
        res_fallback = resolve_turn_conventions(
            self.conventions,
            prompt="只看我负责的记录",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
            active_route="/pms/pm/project/list",
        )
        self.assertEqual(res_fallback.source, "active_page")
        self.assertEqual(res_fallback.target_entities, ("pms/pm/project",))
        self.assertNotIn("我负责的", {c.terms[0] for c in res_fallback.active})

    def test_goal_data_detail_route_entity_and_data_refused_fallback(self):
        self.assertEqual(self.goal_data.route_entity("/crm/customer/detail/42"), "crm/customer")
        self.assertIn("客户名称", self.goal_data.page_words(route="/crm/customer/detail/42"))

        broken_goal = NS(
            page_catalog=lambda: (_ for _ in ()).throw(DataRefused("denied")),
            route_entity=lambda _r: "",
            page_words=lambda **_k: (),
        )
        res = resolve_turn_conventions(
            self.conventions,
            prompt="查ERP客户",
            goal_data=broken_goal,
            active_route="/erp/customer",
        )
        self.assertEqual(res.source, "unresolved")
        self.assertEqual(res.active, self.conventions)

    def test_slim_loop_turn_executor_resolves_conventions_per_turn_not_in_static_system_prompt(self):
        cfg = AgentTurnConfig(
            supported_scope="Yudao",
            ledger_path=self.root / "audit.jsonl",
            require_identity=False,
            deployment_profile="test",
            allow_business_mutations=False,
        )
        bound_app = bind_application(self.app)
        goal_port = NativePageGoalPort(self.app.goal_data)

        class _TestChat(Script):
            provider = "synthetic"
            model = "scripted"

            def complete(self, messages, tools, run_id, **kwargs):
                return self.complete_cancellable(messages, tools, run_id, cancelled=lambda: False)

        scripted_chat = _TestChat(
            reply(call("finish", status="done", answer="ok1")),
            reply(call("finish", status="done", answer="ok2")),
        )
        channel = Channel()
        executor = YudaoTurnExecutor(
            channel=channel,
            chat=scripted_chat,
            config=cfg,
            application=bound_app,
            goal_port=goal_port,
            control=ReadOnlyControl(),
            knowledge=self.knowledge,
            conventions=self.conventions,
            actor_name="测试员",
            today="2026-11-24",
            time_zone="Asia/Shanghai",
            enable_domain_packs=False,
            verify=False,
            run_id_factory=lambda _m: "conv-test",
        )
        executor._messages = []
        executor.conversation_state = ConversationState()
        # Static system prompt no longer embeds deployment conventions.
        self.assertNotIn("本部署的用语约定", executor.profile.instructions)
        self.assertNotIn("crm/customer (CRM 客户)", executor.profile.instructions)

        def _run_one(prompt: str, route: str):
            now = int(time.time() * 1000)
            principal = PrincipalContext(1, "yudao", "1", "7", "9", "synthetic", now - 1000, now + 60000, ("yudao.read",))
            ledger = SessionLedger(self.root / f"audit-{abs(hash((prompt, route)))}.jsonl", "conv-test")
            approvals = ApprovalWatch(
                ledger=ledger,
                run_id="conv-test",
                principal=principal,
                session_id="s",
                turn_id="t",
                business_approval=None,
                cancelled=lambda: False,
                approval_hold_s=30,
            )
            approvals.start_turn(now + 30000)
            run_chat = _TestChat(
                reply(call("finish", status="done", answer="ok")),
                reply(call("finish", status="done", answer="ok")),
            )
            state = NS(
                terminal_reason="",
                detail="",
                clarification=None,
                mutation_outcomes=[],
                message=NS(text=prompt, message_id="m-1"),
                step=0,
                run_id="conv-test",
                started_at=time.time(),
                model_stop=threading.Event(),
                obs=PageObservation(1, 1, f"https://synthetic.test{route}", "Page", [], [], page_route=route, render_state="ready"),
                chat=run_chat,
                ledger=ledger,
                approvals=approvals,
                budget=NS(remaining=lambda: 60.0, check=lambda: 60.0),
                receipt=NS(stage=lambda _name: contextlib.nullcontext()),
                write_intent=False,
                writes=NS(begin_control=lambda: None, start_observing=lambda: None),
                channel=channel,
                operation=NS(
                    principal=principal,
                    session_id="s",
                    turn_id="t",
                    require_live=lambda **_k: None,
                ),
                conversation_state=executor.conversation_state,
                page=NS(
                    checkpoint=lambda: None,
                    action_observations=lambda: (),
                    execute=lambda *a, **k: PageActionOutcome("unavailable", "not_dispatched", "done"),
                ),
                cancelled=lambda: False,
            )
            state.measure = lambda _: contextlib.nullcontext()
            state.audit = ledger.append
            state.progress = lambda _facts: None
            with patch("web_operation_agent.execution.model_loop.MeasuredChat", lambda *a, **k: state.chat), \
                    patch("web_operation_agent.execution.model_loop.AuditedChat", lambda chat, ledger: chat):
                executor._run(state)
            return state.chat.seen[0]

        # Turn 1 on PMS project page suppresses "我负责的" per turn.
        msgs_pms = _run_one("看看我负责的", "/pms/pm/project/list")
        self.assertEqual(msgs_pms[0]["role"], "system")
        self.assertNotIn("本部署的用语约定", msgs_pms[0]["content"])
        self.assertIn("本部署的用语约定", msgs_pms[1]["content"])
        self.assertNotIn("我负责的：", msgs_pms[1]["content"])
        self.assertIn("客户：", msgs_pms[1]["content"])

        # Turn 2 on CRM customer page restores "我负责的".
        msgs_crm = _run_one("看看我负责的", "/crm/customer")
        self.assertIn("本部署的用语约定", msgs_crm[1]["content"])
        self.assertIn("我负责的：", msgs_crm[1]["content"])

    def test_route_only_page_without_entity_resolves_vocabulary_and_suppresses_matching_convention(self):
        self.assertEqual(self.goal_data.route_entity("/user/notify-message"), "")
        words = self.goal_data.page_words(route="/user/notify-message")
        self.assertIn("未回款通知", words)
        res = resolve_turn_conventions(
            self.conventions,
            prompt="查这里的未回款情况",
            goal_data=self.goal_data,
            knowledge=self.knowledge,
            active_route="/user/notify-message",
        )
        self.assertEqual(res.source, "active_page")
        self.assertEqual(res.target_routes, ("/user/notify-message",))
        self.assertNotIn("待回款", {c.terms[0] for c in res.active})

    @unittest.skipUnless(_REAL_UI_DIR is not None, "cached yudao UI not present")
    def test_real_cached_ui_contract_loads_audited_pages_and_reference_conventions(self):
        real_ui = UiSource.read(_REAL_UI_DIR)
        real_convs = load_conventions(REFERENCE_CONVENTIONS)
        self.assertEqual(len(real_convs), 16)
        self.assertIn("请假类型", real_ui.page_vocabulary("bpm/oa/leave/index", route="/bpm/oa/leave"))
        self.assertIn("订单来源", real_ui.page_vocabulary("mall/trade/order/index", route="/mall/trade/order"))
        self.assertIn("我负责的", real_ui.page_vocabulary("pms/pm/project/list/index", route="/pms/pm/project/list"))
        self.assertIn("发送人", real_ui.page_vocabulary("", route="/user/notify-message"))
        self.assertGreater(len(real_ui.page_vocabulary("", route="/crm/customer/detail/42")), 10)

    def test_step1_precision_fixes_cover_real_catalog_disambiguation(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src/utils").mkdir(parents=True)
            (root / "src/router/modules").mkdir(parents=True)
            (root / "src/api").mkdir(parents=True)
            (root / "src/utils/dict.ts").write_text("export enum DICT_TYPE {}\n", encoding="utf-8")
            (root / "src/router/modules/remaining.ts").write_text("export default []\n", encoding="utf-8")
            (root / "src/api/mall/trade/order").mkdir(parents=True)
            (root / "src/api/mall/trade/order/index.ts").write_text(
                "export const getOrderPage = () => request.get({ url: '/trade/order/page' })\n",
                encoding="utf-8",
            )
            pages = {
                "crm/customer/index.vue": (
                    "<template>"
                    "<el-table-column label=\"客户名称\" prop=\"name\" />"
                    "<el-table-column label=\"成交状态\" prop=\"dealStatus\" />"
                    "</template>\n"
                ),
                "crm/contract/index.vue": (
                    "<template>"
                    "<el-table-column label=\"合同名称\" prop=\"name\" />"
                    "<el-table-column label=\"审批状态\" prop=\"auditStatus\" />"
                    "</template>\n"
                ),
                "mall/trade/order/index.vue": (
                    "<template><el-table-column label=\"订单状态\" prop=\"status\" /></template>\n"
                    "<script setup lang=\"ts\">\n"
                    "import * as TradeOrderApi from '@/api/mall/trade/order'\n"
                    "const getList = async () => {\n"
                    "  await TradeOrderApi.getOrderPage()\n"
                    "}\n"
                    "</script>\n"
                ),
                "pay/order/index.vue": (
                    "<template><el-table-column label=\"订单号\" prop=\"no\" /></template>\n"
                ),
                "erp/sale/order/index.vue": (
                    "<template><el-table-column label=\"订单编号\" prop=\"no\" /></template>\n"
                ),
                "erp/purchase/order/index.vue": (
                    "<template><el-table-column label=\"订单单号\" prop=\"no\" /></template>\n"
                ),
                "erp/purchase/in/index.vue": (
                    "<template><el-table-column label=\"入库单号\" prop=\"no\" /></template>\n"
                ),
                "mes/wm/itemreceipt/index.vue": (
                    "<template><el-table-column label=\"未付款\" prop=\"unpaid\" /></template>\n"
                ),
            }
            for rel, content in pages.items():
                p = root / "src/views" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")

            def _op(summary: str, tag: str, vo: str) -> dict:
                return {
                    "get": {
                        "summary": summary,
                        "tags": [f"管理后台 - {tag}"],
                        "parameters": [
                            {"name": "pageNo", "in": "query", "required": True, "schema": {"type": "integer"}},
                            {"name": "pageSize", "in": "query", "required": True, "schema": {"type": "integer"}},
                        ],
                        "responses": {
                            "200": {"content": {"*/*": {"schema": {"$ref": f"#/components/schemas/CommonResult_{vo}"}}}}
                        },
                    }
                }

            vo_defs = {
                "CrmCustomerVO": {"id": {"type": "integer"}, "name": {"type": "string", "description": "客户名称"}, "dealStatus": {"type": "boolean", "description": "成交状态"}},
                "ContractVO": {"id": {"type": "integer"}, "name": {"type": "string", "description": "合同名称"}, "auditStatus": {"type": "integer", "description": "审批状态"}},
                "TradeOrderVO": {"id": {"type": "integer"}, "status": {"type": "integer", "description": "订单状态"}},
                "PayOrderVO": {"id": {"type": "integer"}, "no": {"type": "string", "description": "订单号"}},
                "SaleOrderVO": {"id": {"type": "integer"}, "no": {"type": "string", "description": "订单编号"}},
                "PurchaseOrderVO": {"id": {"type": "integer"}, "no": {"type": "string", "description": "订单单号"}},
                "PurchaseInVO": {"id": {"type": "integer"}, "no": {"type": "string", "description": "入库单号"}},
                "ItemReceiptVO": {"id": {"type": "integer"}, "unpaid": {"type": "integer", "description": "未付款"}},
            }
            schemas = {}
            for vo, props in vo_defs.items():
                schemas[vo] = {"properties": props}
                schemas[f"PageResult_{vo}"] = {"properties": {"list": {"type": "array", "items": {"$ref": f"#/components/schemas/{vo}"}}}}
                schemas[f"CommonResult_{vo}"] = {"properties": {"data": {"$ref": f"#/components/schemas/PageResult_{vo}"}}}
            docs = {
                "paths": {
                    "/admin-api/crm/customer/page": _op("CRM客户分页", "CRM 客户", "CrmCustomerVO"),
                    "/admin-api/crm/contract/page": _op("CRM合同分页", "CRM 合同", "ContractVO"),
                    "/admin-api/trade/order/page": _op("交易订单分页", "商城 交易订单", "TradeOrderVO"),
                    "/admin-api/pay/order/page": _op("支付订单分页", "支付 订单", "PayOrderVO"),
                    "/admin-api/erp/sale-order/page": _op("销售订单分页", "ERP 销售订单", "SaleOrderVO"),
                    "/admin-api/erp/purchase-order/page": _op("采购订单分页", "ERP 采购订单", "PurchaseOrderVO"),
                    "/admin-api/erp/purchase-in/page": _op("采购入库分页", "ERP 采购入库", "PurchaseInVO"),
                    "/admin-api/mes/wm/item-receipt/page": _op("采购入库单分页", "MES 采购入库单", "ItemReceiptVO"),
                },
                "components": {"schemas": schemas},
            }

            class _PrecisionClient:
                tenant = "1"
                read_generation = 0
                menus = [
                    {"name": "CRM 系统", "path": "/crm", "component": "Layout", "children": [
                        {"name": "客户管理", "path": "customer", "component": "crm/customer/index"},
                        {"name": "合同管理", "path": "contract", "component": "crm/contract/index"},
                    ]},
                    {"name": "MALL 商城系统", "path": "/mall", "component": "Layout", "children": [
                        {"name": "订单列表", "path": "trade/order", "component": "mall/trade/order/index"},
                    ]},
                    {"name": "支付系统", "path": "/pay", "component": "Layout", "children": [
                        {"name": "支付订单", "path": "order", "component": "pay/order/index"},
                    ]},
                    {"name": "ERP 系统", "path": "/erp", "component": "Layout", "children": [
                        {"name": "销售订单", "path": "sale/order", "component": "erp/sale/order/index"},
                        {"name": "采购订单", "path": "purchase/order", "component": "erp/purchase/order/index"},
                        {"name": "采购入库", "path": "purchase/in", "component": "erp/purchase/in/index"},
                    ]},
                    {"name": "MES 仓库", "path": "/mes", "component": "Layout", "children": [
                        {"name": "采购入库单", "path": "wm/item-receipt", "component": "mes/wm/itemreceipt/index"},
                    ]},
                ]

                @staticmethod
                def read_window(_check):
                    return contextlib.nullcontext()

                def get(self, path, **params):
                    if path == "system/auth/get-permission-info":
                        return {
                            "user": {"id": 9, "deptId": 7, "nickname": "测试员"},
                            "permissions": [
                                "crm:customer:query", "crm:contract:query", "trade:order:query",
                                "pay:order:query", "erp:sale-order:query", "erp:purchase-order:query",
                                "erp:purchase-in:query", "mes:wm:item-receipt:query",
                            ],
                            "menus": self.menus,
                        }
                    if path in ("system/dict-data/simple-list", "system/dict-type/simple-list"):
                        return []
                    raise AssertionError(f"unexpected GET {path} {params}")

            convs = (
                IndexConvention(("客户",), "crm/customer (CRM 客户)", "crm/customer"),
                IndexConvention(
                    ("成交", "已成交", "成交的合同"),
                    "crm/contract with 审批状态 = 审核通过",
                    "crm/contract",
                    (("审批状态", ("审核通过",)),),
                ),
                IndexConvention(("订单", "销售订单", "销售单"), "erp/sale-order", "erp/sale-order"),
                IndexConvention(("采购订单", "采购单"), "erp/purchase-order", "erp/purchase-order"),
                IndexConvention(
                    ("未付款", "没付款", "还没付款"),
                    "trade/order where 订单状态 = 待支付",
                    "trade/order",
                    (("订单状态", ("待支付",)),),
                ),
            )
            client = _PrecisionClient()
            app = YudaoApplication(
                "https://synthetic.test",
                None,
                docs,
                client=client,
                ui=UiSource.read(root),
                conventions=None,
            )
            app.goal_data.attach_conventions(convs)
            kn = ActorKnowledge(client, docs)
            kn._load()

            # 1) Entity-naming fallback ("客户" -> crm/customer) suppresses "成交" via 成交状态,
            # while "成交的合同" resolves crm/contract and keeps "成交" active via Rule 1.
            res_deal_cust = resolve_turn_conventions(convs, prompt="已成交的客户有哪些", goal_data=app.goal_data, knowledge=kn)
            self.assertEqual(res_deal_cust.source, "convention_entity")
            self.assertEqual(res_deal_cust.target_entities, ("crm/customer",))
            self.assertNotIn("成交", {c.terms[0] for c in res_deal_cust.active})

            res_deal_contract = resolve_turn_conventions(convs, prompt="查一下成交的合同", goal_data=app.goal_data, knowledge=kn)
            self.assertEqual(res_deal_contract.source, "prompt")
            self.assertEqual(res_deal_contract.target_entities, ("crm/contract",))
            self.assertIn("成交", {c.terms[0] for c in res_deal_contract.active})

            # 2) Mixed module token ("MALL 商城系统" -> "商城") resolves trade/order and suppresses "订单".
            res_mall = resolve_turn_conventions(convs, prompt="商城的拼团订单", goal_data=app.goal_data, knowledge=kn)
            self.assertEqual(res_mall.target_entities, ("trade/order",))
            self.assertNotIn("订单", {c.terms[0] for c in res_mall.active})

            # 3) Tier 2 <pref><suf> decomposition ("支付订单" -> "支付成功订单") resolves pay/order.
            res_pay = resolve_turn_conventions(convs, prompt="昨天支付成功订单有哪些", goal_data=app.goal_data, knowledge=kn)
            self.assertEqual(res_pay.target_entities, ("pay/order",))
            self.assertNotIn("订单", {c.terms[0] for c in res_pay.active})

            # 4) <X> / <X>单 co-resolution resolves both erp/purchase-in & mes/wm/item-receipt and suppresses "未付款".
            res_in = resolve_turn_conventions(convs, prompt="查一下未付款的采购入库单", goal_data=app.goal_data, knowledge=kn)
            self.assertEqual(set(res_in.target_entities), {"erp/purchase-in", "mes/wm/item-receipt"})
            self.assertNotIn("未付款", {c.terms[0] for c in res_in.active})

            # 5) Tier 3 core_len priority picks "采购订单" over generic "订单列表".
            res_po = resolve_turn_conventions(convs, prompt="还没入库的采购订单列表", goal_data=app.goal_data, knowledge=kn)
            self.assertEqual(res_po.target_entities, ("erp/purchase-order",))
            self.assertIn("采购订单", {c.terms[0] for c in res_po.active})


if __name__ == "__main__":
    unittest.main()
