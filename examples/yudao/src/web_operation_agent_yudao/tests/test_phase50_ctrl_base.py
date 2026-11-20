"""Contract and regression tests for Phase 50 Layer 1 (`ctrl_base`)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from web_operation_agent.business.convention_writes import WriteRouteCapability, resolve_write_route_capability
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS
from web_operation_agent_yudao.executor.actor_knowledge import USER_READ_WRITING_GET_ALLOWLIST, ActorKnowledge
from web_operation_agent_yudao.executor.page_prompts import (
    NATIVE_SUBMIT_RULES,
    apply_write_route_capability,
    system_text,
)
from web_operation_agent_yudao.executor.procedures import _note_sql_receipt_values
from web_operation_agent_yudao.executor.slim_loop import _clean_user_answer
from web_operation_agent_yudao.goal_compat.goal import YudaoGoalData
from web_operation_agent_yudao.goal_compat.page_goals import NativePageGoalPort
from web_operation_agent_yudao.reads.filter_semantics import FilterSemantics
from web_operation_agent_yudao.reads.turn_tables import TurnTables
from web_operation_agent_yudao.rules.java_scan import parse_resp_vo_code_bindings
from web_operation_agent_yudao.rules.skill_packages import domain_playbooks
from web_operation_agent_yudao.rules.ui_source import _derived_columns, _literal_column_labels
from web_operation_agent_yudao.writes.convention_writes import YUDAO_WRITE_ROUTE_CAPABILITY


class _StubClient:
    def __init__(self, responses: dict | None = None):
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, dict]] = []
        self.read_generation = 0

    def get(self, path: str, **params):
        key = path.lstrip("/")
        self.calls.append((key, dict(params)))
        val = self.responses.get(key)
        if callable(val):
            return val(params)
        return val


class Phase50CtrlBaseTests(unittest.TestCase):
    def test_write_route_capability_single_source(self):
        base = system_text(
            user="测试员",
            date="2026-10-25",
            tz="Asia/Shanghai",
            catalog=[(SimpleNamespace(name="demo", description="演示"), ())],
        )
        enabled_text = apply_write_route_capability(base, YUDAO_WRITE_ROUTE_CAPABILITY, native_submit=True)
        self.assertIn(NATIVE_SUBMIT_RULES, enabled_text)
        self.assertIn("不对表单调用 request_submit", enabled_text)

        disabled = WriteRouteCapability(compiled_api_writes=True, inline_row_status_submit=False, page_form_mode="disabled")
        dis_text = apply_write_route_capability(base, disabled, native_submit=True)
        self.assertIn("当前部署不开放页面表单提交", dis_text)
        self.assertNotIn("不对表单调用 request_submit", dis_text)

        port = NativePageGoalPort(YudaoGoalData(_StubClient(), {"paths": {}, "components": {"schemas": {}}}))
        self.assertEqual(resolve_write_route_capability(port), YUDAO_WRITE_ROUTE_CAPABILITY)

    def test_writing_get_allowlist_and_filter_unbound(self):
        docs = {
            "paths": {
                "/admin-api/fms/home/get": {"get": {"summary": "财务首页", "tags": ["财务"], "parameters": []}},
                "/admin-api/infra/redis/get": {"get": {"summary": "Redis", "tags": ["监控"], "parameters": []}},
                "/admin-api/oa/item/page": {
                    "get": {
                        "summary": "分页", "tags": ["OA"],
                        "parameters": [{"name": "deptId", "in": "query", "schema": {"type": "integer"}}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/P"}}}}},
                    }
                },
                "/admin-api/oa/item/page-by-dept": {"get": {"summary": "按部门分页", "tags": ["OA"], "parameters": []}},
            },
            "components": {"schemas": {"P": {"type": "object", "properties": {"id": {"type": "integer"}}}}},
        }
        client = _StubClient({
            "system/auth/get-permission-info": {"user": {"nickname": "测试"}, "menus": []},
            "fms/home/get": {"cash": 100},
            "oa/item/page": {"list": [{"id": 1}], "total": 1},
        })
        fs = FilterSemantics(
            {"/admin-api/oa/item/page": {}},
            unknown={"/admin-api/oa/item/page": frozenset({"deptId"})},
        )
        writing = frozenset({"/admin-api/fms/home/get", "/admin-api/infra/redis/get"})
        know = ActorKnowledge(client, docs, writing_gets=writing, filter_semantics=fs)
        self.assertEqual(len(USER_READ_WRITING_GET_ALLOWLIST), 16)
        # Disallowed when allow_user_writing_gets=False (background/probe)
        blocked = know._prepare_call_api("fms/home/get", {}, allow_user_writing_gets=False)
        self.assertIn("助手不允许调用", blocked["text"])
        # Allowed on active user call_api for allowlisted snapshot GET
        ok = know.call_api("fms/home/get", {})
        self.assertIn("100", ok)
        self.assertTrue(know.last_writing_get_allowed)
        # Still refused for non-allowlisted writing GET
        refused_redis = know.call_api("infra/redis/get", {})
        self.assertIn("助手不允许调用", refused_redis)
        # FilterSemantics unbound_for surfaces sibling endpoint hint
        hinted = know.call_api("oa/item/page", {"deptId": 10})
        self.assertIn("page-by-dept", hinted)

    def test_derived_columns_not_overwriting_raw_prop_labels(self):
        vue = '''
        <el-table>
          <el-table-column prop="price" label="合同金额" />
          <el-table-column prop="price" label="未回款金额">
            <template #default="{ row }">{{ row.totalPrice - row.receivablePrice }}</template>
          </el-table-column>
        </el-table>
        '''
        labels = dict(_literal_column_labels(vue))
        derived = dict(_derived_columns(vue))
        self.assertEqual(labels, {"price": "合同金额"})
        self.assertEqual(derived, {"未回款金额": "totalPrice - receivablePrice"})

    def test_tie_boundary_strips_trailing_id_and_preserves_ties(self):
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        try:
            tables.add(
                path="oa/attendance/page",
                params={},
                rows=[
                    {"id": 10, "name": "张三", "cnt": 3},
                    {"id": 20, "name": "李四", "cnt": 3},
                    {"id": 30, "name": "王五", "cnt": 2},
                ],
                total=3,
                complete=True,
                column_labels={"name": "姓名", "cnt": "迟到次数"},
                code_maps={"cnt": {"3": "三次"}},
            )
            res = tables.query("SELECT id, name, cnt, cnt_label FROM t1 ORDER BY cnt DESC, id DESC LIMIT 1")
            self.assertTrue(res["tie"]["tied"])
            self.assertEqual(len(res["rows"]), 2)
            self.assertEqual({r[1] for r in res["rows"]}, {"张三", "李四"})
            self.assertEqual(res["rows"][0][3], "三次")
            # Zero sql_tie_dup_check / sql_scope_check / sql_*_check errors
            self.assertFalse(hasattr(tables, "last_sql_triggers"))
        finally:
            tables.close()

    def test_resp_vo_bindings_non_bleed_and_s3_erp_stock_record_t1_sib(self):
        java = '''
        public class BpmProcessInstanceRespVO {
            @Schema(description = "流程状态")
            private Integer status; // 参见 BpmProcessInstanceStatusEnum 枚举
            @Schema(description = "发起时间")
            private LocalDateTime startTime;
            @DictFormat(DictTypeConstants.AUDIT_STATUS)
            private Integer audit;
        }
        '''
        parsed = parse_resp_vo_code_bindings(java, {"AUDIT_STATUS": "erp_audit_status"})
        self.assertEqual(parsed.get("status"), ("enum", "BpmProcessInstanceStatusEnum"))
        self.assertNotIn("startTime", parsed)
        self.assertEqual(parsed.get("audit"), ("dict", "erp_audit_status"))

        docs = {
            "paths": {
                "/admin-api/erp/purchase-in/page": {
                    "get": {
                        "summary": "获得采购入库分页", "tags": ["管理后台 - ERP 采购入库"],
                        "parameters": [
                            {"name": "pageNo", "in": "query", "schema": {"type": "integer"}},
                            {"name": "pageSize", "in": "query", "schema": {"type": "integer"}},
                            {"name": "createTime", "in": "query", "schema": {"type": "array"}},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/P1"}}}}},
                    }
                },
                "/admin-api/erp/stock-record/page": {
                    "get": {
                        "summary": "获得产品库存明细分页", "tags": ["管理后台 - ERP 产品库存明细"],
                        "parameters": [
                            {"name": "pageNo", "in": "query", "schema": {"type": "integer"}},
                            {"name": "pageSize", "in": "query", "schema": {"type": "integer"}},
                            {"name": "bizType", "in": "query", "schema": {"type": "integer"}},
                            {"name": "createTime", "in": "query", "schema": {"type": "array"}},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/P2"}}}}},
                    }
                },
            },
            "components": {
                "schemas": {
                    "P1": {"type": "object", "properties": {"id": {"type": "integer"}, "totalCount": {"type": "number"}}},
                    "P2": {"type": "object", "properties": {"id": {"type": "integer"}, "bizType": {"type": "integer"}, "count": {"type": "number"}}},
                }
            },
        }
        client = _StubClient({
            "system/auth/get-permission-info": {"user": {"nickname": "测试"}, "menus": []},
            "erp/purchase-in/page": {"list": [], "total": 0},
            "erp/stock-record/page": {"list": [{"id": 901, "bizType": 70, "count": 5}], "total": 1},
        })
        codes_fn = lambda rel: {"bizType": {"70": "采购入库", "71": "采购入库（作废）"}} if rel == "erp/stock-record/page" else {}
        know = ActorKnowledge(client, docs, codes=codes_fn)
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        try:
            out = know.call_api("erp/purchase-in/page", {"pageNo": 1, "pageSize": 20}, row_sink=tables.add, allow_cluster_probe=True)
            self.assertIn("t1_sib", out)
            self.assertIn("t1", tables.tables)
            self.assertIn("t1_sib", tables.tables)
            # Adding another primary table stays t2 (not shifted by t1_sib)
            t2 = tables.add(path="erp/stock-record/page", params={}, rows=[{"id": 1}], total=1, complete=True)
            self.assertEqual(t2["name"], "t2")
            q_sib = tables.query("SELECT id, bizType, bizType_label, count FROM t1_sib")
            self.assertEqual(q_sib["rows"], [[901, 70, "采购入库", 5]])
            self.assertEqual(_clean_user_answer("统计见行表 `t1_sib` 结果", BASELINE_SESSION_HANDOFFS), "统计见本轮读取数据 结果")
        finally:
            tables.close()

    def test_fms_dual_month_and_crm_sales_playbook_and_sql_receipts(self):
        docs = {
            "paths": {
                "/admin-api/fms/config/account-set/list": {"get": {"summary": "账套列表", "tags": ["FMS"], "parameters": []}},
                "/admin-api/fms/closing/period/current-month": {"get": {"summary": "当前会计期", "tags": ["FMS"], "parameters": []}},
            },
            "components": {"schemas": {}},
        }
        client = _StubClient({
            "system/auth/get-permission-info": {"user": {"nickname": "财务"}, "menus": []},
            "fms/config/account-set/list": [{"id": 7, "companyName": "测试公司", "defaultStatus": True}],
            "fms/closing/period/current-month": "2026-03",
        })
        know = ActorKnowledge(client, docs)
        note, vals = know.fms_dual_month_context("2026-11-20")
        self.assertIn("系统当前日历月=2026-11", note)
        self.assertIn("当前会计期=2026-03（year=2026，period=3）", note)
        self.assertIn("2026-03", vals)
        self.assertNotIn("（成交）", domain_playbooks().get("crm-sales", ""))

        # _note_sql_receipt_values accepts t1_sib results
        from web_operation_agent.execution.value_source import SourceLedger
        turn = {"source_ledger": SourceLedger()}
        _note_sql_receipt_values(turn, {"rows": [[9500, "2026-10-25"]]})
        receipts = {r.value for r in turn["source_ledger"].receipts}
        self.assertIn("9500", receipts)
        self.assertIn("2026-10-25", receipts)

    def test_find_record_global_fallback_and_status_dict(self):
        from contextlib import nullcontext
        from web_operation_agent_yudao.executor.procedures import YudaoProceduresBase

        docs = {"paths": {}, "components": {"schemas": {}}}
        know = ActorKnowledge(
            _StubClient({"system/auth/get-permission-info": {"user": {"nickname": "测试"}, "menus": []}}),
            docs,
            codes=lambda rel: {"status": {"0": "开启", "1": "关闭"}} if rel == "system/notice/page" else {},
        )
        know._load()
        know.list_api = lambda _entity: ""
        know.page_path = lambda page: page
        index = SimpleNamespace(
            entities=[
                SimpleNamespace(key="oa/schedule", label="OA 日程", aliases=()),
                SimpleNamespace(key="system/notice", label="通知公告", aliases=()),
            ],
            pages=[
                SimpleNamespace(entity="system/notice", breadcrumb="/system/notice", label="通知公告"),
            ],
        )
        calls = []

        class _Port:
            def index(self, *, context=None):
                return index

            def outline_index(self, *, context=None):
                return index

            def records_named(self, name, *, context=None, entities=()):
                return ()

            def records_named_diagnostics(self, name, *, context=None, entities=()):
                calls.append(entities)
                if entities:
                    return (), ()
                return (("system/notice", (("101", "全员例会公告", ""),)),), ()

        proc = YudaoProceduresBase(know, _Port(), "http://127.0.0.1")
        s = SimpleNamespace(operation=SimpleNamespace(), measure=lambda _k: nullcontext())
        out = proc._find_record(s, "全员例会公告", "日程")
        self.assertEqual(len(calls), 2)
        self.assertIn("已自动扩大为全局检索", out)
        self.assertIn("通知公告", out)
        self.assertIn("status字典：开启/关闭", out)

        class _AlwaysEmptyPort:
            def index(self, *, context=None):
                return index

            def outline_index(self, *, context=None):
                return index

            def records_named(self, name, *, context=None, entities=()):
                return ()

            def records_named_diagnostics(self, name, *, context=None, entities=()):
                return (), ()

        proc2 = YudaoProceduresBase(know, _AlwaysEmptyPort(), "http://127.0.0.1")
        miss = proc2._find_record(s, "不存在的记录", "日程")
        self.assertIn("全局模块里", miss)
        self.assertNotIn("确切名称时，如实说明这些模块没有该名称", miss)

    def test_host_forget_closed_and_bootstrap_eviction(self):
        import threading
        from unittest.mock import patch
        from web_operation_agent_yudao.host.native_server import YudaoHost, _Active

        host = YudaoHost.__new__(YudaoHost)
        host._lock = threading.RLock()
        host._draining = False
        host._sessions = {}
        host.config = SimpleNamespace(
            api_base="http://localhost:48080",
            native_writes=False,
            native_write_preparation=False,
            principal_lifetime_s=600,
        )

        def dead_channel():
            def _fail():
                raise RuntimeError("closed")

            return SimpleNamespace(validate_live=_fail)

        live_channel = SimpleNamespace(validate_live=lambda: None)
        host._sessions[("1", "u1", "dead-tab")] = _Active(
            session=SimpleNamespace(channel=dead_channel()),
            client=SimpleNamespace(renew=lambda _t: None),
            delegation_id="d1",
        )
        renewed_principals = []
        renewed_tokens = []
        live_session = SimpleNamespace(
            channel=live_channel,
            renew_principal=lambda p: renewed_principals.append(p),
        )
        host._sessions[("1", "u1", "live-tab")] = _Active(
            session=live_session,
            client=SimpleNamespace(renew=lambda tok: renewed_tokens.append(tok)),
            delegation_id="d-live",
        )
        self.assertEqual(host.forget_closed(), 1)
        self.assertEqual(list(host._sessions.keys()), [("1", "u1", "live-tab")])

        app_calls = []
        open_calls = []
        host._application = lambda *a, **k: (
            app_calls.append(1)
            or SimpleNamespace(
                ports=SimpleNamespace(
                    identity=SimpleNamespace(bind=lambda page, delegation_id, lifetime_s: SimpleNamespace(delegation_id=delegation_id))
                )
            )
        )
        host._open = lambda app, principal, key, page_window: (open_calls.append(key) or SimpleNamespace(channel=live_channel))
        host._browser_bootstrap = lambda session: {"ok": True, "session": id(session)}

        fake_client = SimpleNamespace(_login=lambda: "u1", tenant="1", renew=lambda _t: None)
        fake_page = SimpleNamespace(actor_id="u1")
        with (
            patch("web_operation_agent_yudao.host.native_server.YudaoClient.for_token", return_value=fake_client),
            patch("web_operation_agent_yudao.host.native_server.YudaoIdentityPort") as id_cls,
        ):
            id_inst = id_cls.return_value
            id_inst.page_identity.return_value = fake_page
            id_inst.bind.side_effect = lambda page, delegation_id, lifetime_s: SimpleNamespace(delegation_id=delegation_id)
            res_live = host.bootstrap("tok-new", "1", "live-tab")
            self.assertTrue(res_live["ok"])
            self.assertEqual(renewed_tokens, ["tok-new"])
            self.assertEqual(app_calls, [])
            live_channel.validate_live = lambda: (_ for _ in ()).throw(RuntimeError("evicted"))
            res_new = host.bootstrap("tok-next", "1", "fresh-tab")
            self.assertTrue(res_new["ok"])
            self.assertEqual(len(app_calls), 1)
            self.assertEqual(open_calls, [("1", "u1", "fresh-tab")])
            self.assertEqual(list(host._sessions.keys()), [("1", "u1", "fresh-tab")])


if __name__ == "__main__":
    unittest.main()
