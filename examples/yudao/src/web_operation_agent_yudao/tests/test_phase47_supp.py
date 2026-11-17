"""Tests for Phase 47 supplementary speed and regression fixes."""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock

sys.path[:0] = [str(Path(__file__).resolve().parents[4] / "src")]

from web_operation_agent.goal.data import Compare, EVERYTHING
from web_operation_agent.goal.index import IndexRule
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS
from web_operation_agent_yudao.goal import YudaoGoalData
from web_operation_agent_yudao.slim_loop import (
    ActorKnowledge,
    YudaoLoopProcedures,
    _precomputed_dates,
    system_text,
)
from web_operation_agent_yudao.turn_tables import TurnTables
from web_operation_agent_yudao.write_intent import classify_write_intent


def _page(tag: str, summary: str, item: str, params: list[tuple[str, str]]) -> dict:
    return {
        "get": {
            "tags": [tag],
            "summary": summary,
            "parameters": [{"name": n, "in": "query", "schema": {"type": t}} for n, t in params],
            "responses": {
                "200": {
                    "content": {
                        "*/*": {"schema": {"$ref": f"#/components/schemas/R{item}"}}
                    }
                }
            },
        }
    }


def _schemas(item: str, props: dict) -> dict:
    return {
        f"R{item}": {"properties": {"data": {"$ref": f"#/components/schemas/P{item}"}}},
        f"P{item}": {"properties": {"list": {"items": {"$ref": f"#/components/schemas/{item}"}}}},
        item: {"properties": props},
    }


def _mini_docs() -> dict:
    return {
        "paths": {
            "/admin-api/erp/stock-move/page": _page(
                "管理后台 - ERP 库存调拨单",
                "获得库存调拨单分页",
                "StockMove",
                [("no", "string"), ("name", "string"), ("pageNo", "integer"), ("pageSize", "integer")],
            ),
            "/admin-api/erp/stock-record/page": _page(
                "管理后台 - ERP 产品库存明细",
                "获得产品库存明细分页",
                "StockRecord",
                [("bizNo", "string"), ("pageNo", "integer"), ("pageSize", "integer")],
            ),
            "/admin-api/system/notice/page": _page(
                "管理后台 - 通知公告",
                "获得通知公告分页",
                "Notice",
                [("title", "string"), ("pageNo", "integer"), ("pageSize", "integer")],
            ),
            "/admin-api/system/notify-message/page": _page(
                "管理后台 - 站内信记录",
                "获得站内信分页",
                "NotifyMessage",
                [("pageNo", "integer")],
            ),
        },
        "components": {
            "schemas": {
                **_schemas(
                    "StockMove",
                    {
                        "id": {"type": "integer", "description": "编号"},
                        "no": {"type": "string", "description": "调拨单号"},
                        "name": {"type": "string", "description": "调拨名称"},
                        "moveTime": {"type": "integer", "description": "调拨时间"},
                    },
                ),
                **_schemas(
                    "StockRecord",
                    {
                        "id": {"type": "integer", "description": "编号"},
                        "bizNo": {"type": "string", "description": "业务单号"},
                        "count": {"type": "number", "description": "操作数量"},
                    },
                ),
                **_schemas(
                    "Notice",
                    {
                        "id": {"type": "integer", "description": "编号"},
                        "title": {"type": "string", "description": "公告标题"},
                    },
                ),
                **_schemas(
                    "NotifyMessage",
                    {
                        "id": {"type": "integer", "description": "编号"},
                        "templateContent": {"type": "string", "description": "内容"},
                    },
                ),
            }
        },
    }


class MiniClient:
    read_generation = 0

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, **params):
        self.calls.append((path, params))
        if path == "system/auth/get-permission-info":
            return {
                "user": {"id": 1, "nickname": "测试员"},
                "permissions": [
                    "erp:stock-move:query",
                    "erp:stock-record:query",
                    "system:notice:query",
                    "system:notify-message:query",
                ],
                "menus": [
                    {
                        "id": 1,
                        "name": "ERP",
                        "path": "/erp",
                        "visible": True,
                        "children": [
                            {
                                "id": 11,
                                "name": "库存调拨单",
                                "path": "stock-move",
                                "component": "erp/stock/move/index",
                                "visible": True,
                            },
                            {
                                "id": 12,
                                "name": "产品库存明细",
                                "path": "stock-record",
                                "component": "erp/stock/record/index",
                                "visible": True,
                            },
                        ],
                    },
                    {
                        "id": 2,
                        "name": "系统管理",
                        "path": "/system",
                        "visible": True,
                        "children": [
                            {
                                "id": 21,
                                "name": "通知公告",
                                "path": "notice",
                                "component": "system/notice/index",
                                "visible": True,
                            },
                            {
                                "id": 22,
                                "name": "站内信记录",
                                "path": "notify-message",
                                "component": "system/notify/message/index",
                                "visible": True,
                            },
                        ],
                    },
                ],
            }
        if path in ("system/dict-data/simple-list", "system/dict-type/simple-list"):
            return []
        if path == "system/user/simple-list":
            return [{"id": 1, "nickname": "测试员"}]
        if path == "system/dept/simple-list":
            return []
        if path == "erp/stock-move/page":
            return {
                "total": 1,
                "list": [
                    {
                        "id": 42,
                        "no": "SYNDB202610250001",
                        "name": "合成调拨",
                        "moveTime": 1795485600000,
                    }
                ],
            }
        if path == "erp/stock-record/page":
            return {
                "total": 1,
                "list": [
                    {
                        "id": 501,
                        "bizNo": params.get("bizNo", "SYNPD202610230001"),
                        "count": -5,
                    }
                ],
            }
        raise AssertionError(f"unexpected GET {path} {params}")


class Phase47SuppTests(unittest.TestCase):
    def test_rules_index_skips_line_table_discovery_gets(self):
        client = MiniClient()
        goal = YudaoGoalData(client, _mini_docs)
        fake_source = NS(
            release="v-test",
            rules=lambda entities: (
                {
                    entities[0].key: (
                        IndexRule(
                            id="r1",
                            kind="refusal",
                            about="删除",
                            text="已审核单据不能删除",
                            citation="Svc.java:10-12@v-test sha256:00",
                            subject="erp/stock-move/delete",
                        ),
                    )
                },
                {},
            ),
        )
        goal.attach_source(fake_source)
        idx = goal.rules_index()
        # Only metadata endpoints were read; no `/page` or `/get` sample row fetches ran.
        called_paths = [p for p, _ in client.calls]
        self.assertNotIn("erp/stock-move/page", called_paths)
        self.assertTrue(any(e.rules for e in idx.entities))

        proc = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        proc.goal_port = NS(
            data=goal,
            index=MagicMock(side_effect=AssertionError("should use rules_index, not full index()")),
        )
        out = proc._entity_rules(NS(operation=None), "库存调拨", "删除")
        self.assertIn("已审核单据不能删除", out)

    def test_serial_code_lookup_prefers_code_param_and_retains_bizno_hits(self):
        client = MiniClient()
        goal = YudaoGoalData(client, _mini_docs)
        goal.outline()
        move_key = next(k for k in goal._entities if "stock-move" in k)
        record_key = next(k for k in goal._entities if "stock-record" in k)
        notice_key = next(k for k in goal._entities if "notice" in k)

        # Serial code routes to `no` on stock-move, `bizNo` on stock-record, and skips title-only notice & users.
        self.assertEqual(goal._narrowing(move_key, "SYNDB202610250001"), Compare("no", "contains", "SYNDB202610250001"))
        self.assertEqual(
            goal._narrowing(record_key, "SYNPD202610230001"),
            Compare("bizNo", "contains", "SYNPD202610230001"),
        )
        self.assertIs(goal._narrowing(notice_key, "SYNDB202610250001"), EVERYTHING)
        self.assertTrue(goal.name_searchable(move_key, "SYNDB202610250001"))
        self.assertTrue(goal.name_searchable(record_key, "SYNPD202610230001"))
        self.assertFalse(goal.name_searchable(notice_key, "SYNDB202610250001"))
        self.assertFalse(goal.name_searchable("system/user", "SYNDB202610250001"))

        # `find` retains rows matched via narrowed `bizNo` even when display has no `name`/`no`.
        hits = goal.find(record_key, "SYNPD202610230001")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].id, "501")

        # Ordinary shorthand still searches name/title entities and users.
        self.assertTrue(goal.name_searchable(move_key, "合成"))
        self.assertTrue(goal.name_searchable(notice_key, "系统维护"))
        self.assertTrue(goal.name_searchable("system/user", "测试"))

    def test_inline_small_table_formats_epoch_ms_and_keeps_all_columns_in_sqlite(self):
        client = MiniClient()
        know = ActorKnowledge(client, _mini_docs, time_zone="Asia/Shanghai")
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        try:
            text = know.call_api(
                "erp/stock-move/page",
                {"pageNo": 1, "pageSize": 10},
                ["no", "moveTime", "nonexistentCol"],
                row_sink=tables.add,
            )
            # 1795485600000 ms == 2026-11-24 10:00:00 +08:00
            self.assertIn("2026-11-24 10:00:00", text)
            self.assertIn("nonexistentCol", text)
            self.assertIn("返回记录中不存在", text)
            # SQLite row table keeps integer milliseconds AND retains unprojected real columns (`name`).
            sql_res = tables.query("SELECT typeof(moveTime), moveTime, name FROM t1")
            self.assertEqual(sql_res["rows"], [["integer", 1795485600000, "合成调拨"]])
        finally:
            tables.close()

    def test_suggest_matches_stemmed_nouns_without_model_call(self):
        client = MiniClient()
        know = ActorKnowledge(client, _mini_docs, time_zone="Asia/Shanghai")
        hit_move = know.suggest("帮我看看最早那张调拨单")
        self.assertIn("/erp/stock-move", hit_move)
        self.assertIn("/admin-api/erp/stock-move/page", hit_move)
        hit_notify = know.suggest("查一下最近一条站内信")
        self.assertIn("/admin-api/system/notify-message/page", hit_notify)

    def test_precomputed_dates_cover_relative_expressions_and_seed_values(self):
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        try:
            rendered = _precomputed_dates(
                "查一下本月和最近一个月以及近7天的单据",
                (tables, "2026-10-25", "Asia/Shanghai"),
            )
            self.assertIn("2026-10-01 ~ 2026-10-25", rendered)
            self.assertIn("2026-10-01 ~ 2026-10-31", rendered)
            self.assertIn("2026-09-26 ~ 2026-10-25", rendered)
            self.assertIn("2026-10-19 ~ 2026-10-25", rendered)
            self.assertTrue(getattr(tables, "precomputed_date_values", None))
            self.assertEqual(_precomputed_dates("查编号100的详情", (tables, "2026-10-25", "Asia/Shanghai")), "")
        finally:
            tables.close()

    def test_deferred_manual_submit_stays_draft_and_skips_handoff_wait(self):
        intent = classify_write_intent("帮我先填上日期和供应商，商品明细留空，回头补上产品和数量后再提交")
        self.assertEqual(intent.mode, "draft")
        commit_intent = classify_write_intent("帮我先填上备注，然后再由你直接提交保存")
        self.assertEqual(commit_intent.mode, "save")

    def test_native_submit_instructions_do_not_claim_only_skills_can_write(self):
        base = system_text(
            user="测试员",
            date="2026-10-25",
            tz="Asia/Shanghai",
            catalog=[(NS(name="demo-create", description="仅新建演示分组"), ())],
        )
        self.assertIn("只有下面“已安装的 Skill”覆盖的操作能办", base)
        replaced = (
            base.replace(
                "只有下面“已安装的 Skill”覆盖的操作能办：你提出写入提案，用户在批准卡片上确认后，程序执行一次并回读核对。\n"
                "   - 本轮提供了 Skill 写入工具且用户要求办理其覆盖的事时，先 load_skill，再用已安装操作提出提案；没有对应 Skill 时把表单填好交接。",
                "优先用下面“已安装的 Skill”覆盖的操作提出提案；Skill 未覆盖的新增、修改或行内状态动作，"
                "在用户页用原生表单或行内按钮配合 request_submit 经批准卡提交（不得因无对应 Skill 而拒绝，唯独涉及密码或本地文件上传时填好其余字段交接）。\n"
                "   - 本轮提供了 Skill 写入工具且用户要求办理其覆盖的事时，先 load_skill，再用已安装操作提出提案；没有对应 Skill 且非纯预填时，打开对应页面表单填好后用 request_submit 请求批准提交。",
                1,
            )
        )
        self.assertNotIn("只有下面“已安装的 Skill”覆盖的操作能办", replaced)
        self.assertIn("不得因无对应 Skill 而拒绝", replaced)

    def test_call_api_shapes_date_range_echoes_declared_params_and_warns_clocks_with_row_sink(self):
        class RichClient(MiniClient):
            def __init__(self):
                super().__init__()
                self.page_size_limit = False

            def get(self, path, **params):
                self.calls.append((path, dict(params)))
                if path == "erp/stock-move/page":
                    if self.page_size_limit:
                        return {
                            "total": 4,
                            "list": [
                                {"id": 1, "no": "DB1", "moveTime": 1795485600000, "createTime": 1793491200000},
                                {"id": 2, "no": "DB2", "moveTime": 1795485600000, "createTime": 1793491200000},
                            ],
                        }
                    return {
                        "total": 2,
                        "list": [
                            {"id": 1, "no": "DB1", "moveTime": 1795485600000, "createTime": 1793491200000},
                            {"id": 2, "no": "DB2", "moveTime": 1796090400000, "createTime": 1793491200000},
                        ],
                    }
                return super().get(path, **params)

        def rich_docs():
            d = _mini_docs()
            params = d["paths"]["/admin-api/erp/stock-move/page"]["get"]["parameters"]
            params.append({
                "name": "createTime",
                "in": "query",
                "schema": {"type": "array", "items": {"type": "string", "format": "date-time"}},
            })
            d["components"]["schemas"]["StockMove"]["properties"]["createTime"] = {
                "type": "integer",
                "description": "创建时间",
            }
            return d

        client = RichClient()
        know = ActorKnowledge(client, rich_docs, time_zone="Asia/Shanghai")
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        try:
            text = know.call_api(
                "erp/stock-move/page",
                {"createTime": ["2026-10-01", "2026-10-31"], "pageNo": 1, "pageSize": 10},
                row_sink=tables.add,
            )
            sent_params = [p for path, p in client.calls if path == "erp/stock-move/page"][-1]
            self.assertEqual(sent_params["createTime"], ["2026-10-01 00:00:00", "2026-10-31 23:59:59"])
            self.assertIn("这些行有几个时间列:", text)
            self.assertIn("moveTime 按月的行数:", text)
            self.assertIn("createTime 按月的行数:", text)

            # Unknown query param echoes declared parameter names
            from web_operation_agent_yudao.slim_loop import QueryParametersError
            with self.assertRaisesRegex(QueryParametersError, r"declared:.*createTime"):
                know.call_api("erp/stock-move/page", {"unknownField": "1"})

            # Partial read via row_sink still warns incomplete
            client.page_size_limit = True
            orig_pages = know._pages
            know._pages = lambda rel, prm, follow: (
                {"total": 4, "list": [{"id": 1, "no": "DB1"}, {"id": 2, "no": "DB2"}]},
                [{"id": 1, "no": "DB1"}, {"id": 2, "no": "DB2"}],
                4,
                False,
                1,
            )
            try:
                partial_text = know.call_api(
                    "erp/stock-move/page",
                    {"pageNo": 1, "pageSize": 2},
                    row_sink=tables.add,
                )
            finally:
                know._pages = orig_pages
            self.assertIn("没有读完符合条件的全部记录", partial_text)
        finally:
            tables.close()

    def test_name_searchable_skips_unsatisfied_required_and_matches_extended_narrowing_keys(self):
        from web_operation_agent_yudao.goal import _Entity

        client = MiniClient()
        goal = YudaoGoalData(client, _mini_docs)
        goal.outline()
        goal._entities = {
            "erp/child-item": _Entity(
                key="erp/child-item", module="erp", resource="child-item", label="子明细",
                list_path="erp/child-item/page", name_field="itemName",
                params={"name": {"type": "string"}, "parentId": {"type": "integer"}},
                props={"id": {"type": "integer"}, "itemName": {"type": "string"}},
                required=frozenset({"parentId"}), fixed_params=(), has_get=False, operations=(),
                view=None, lists=frozenset(), money={},
            ),
            "mall/sku-item": _Entity(
                key="mall/sku-item", module="mall", resource="sku-item", label="商品项",
                list_path="mall/sku-item/page", name_field="itemName",
                params={"keyword": {"type": "string"}},
                props={"id": {"type": "integer"}, "keyword": {"type": "string"}, "itemName": {"type": "string"}},
                required=frozenset(), fixed_params=(), has_get=False, operations=(),
                view=None, lists=frozenset(), money={},
            ),
        }
        self.assertFalse(goal.name_searchable("erp/child-item", "苹果"))
        self.assertTrue(goal.name_searchable("mall/sku-item", "苹果"))
        self.assertEqual(goal._narrowing("mall/sku-item", "苹果"), Compare("keyword", "contains", "苹果"))

    def test_precomputed_dates_variants_and_fill_fields_plain_input_coalescing(self):
        from web_operation_agent.schemas import ElementRef, PageObservation

        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        try:
            _precomputed_dates("查今天的数据", (tables, "2026-10-25", "Asia/Shanghai"))
            self.assertIn("2026-10-25", tables.precomputed_date_values)
            self.assertIn("2026-10-25 00:00:00", tables.precomputed_date_values)
            self.assertIn("2026-10-25 23:59:59", tables.precomputed_date_values)
            self.assertIn("2026-10-26 00:00:00", tables.precomputed_date_values)
            self.assertNotIn("2026-10-26 23:59:59", tables.precomputed_date_values)
        finally:
            tables.close()

        proc = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        proc.knowledge = None
        proc._choice_label = lambda s, el, text: text
        settled_calls = []
        proc._act = lambda s, action, args, **kw: None
        proc._settled = lambda s, **kw: settled_calls.append(kw) or "still"
        obs = PageObservation(
            1, 1, "https://synthetic.test/form", "Form", [],
            [
                ElementRef("e1", "textbox", "", "input", 1, {"field_label": "字段一"}),
                ElementRef("e2", "textbox", "", "input", 1, {"field_label": "字段二"}),
                ElementRef("e3", "textbox", "", "input", 1, {"field_label": "字段三"}),
                ElementRef("e4", "combobox", "", "input", 1, {"field_label": "类别", "aria-haspopup": "listbox"}),
            ],
            content="[1]<input />\n[2]<input />\n[3]<input />\n[4]<input />",
        )
        s = NS(obs=obs, terminal_reason="", page=NS(action_observations=lambda: ()))
        res = proc._fill_fields(
            s,
            [
                {"label": "字段一", "value": "A"},
                {"label": "字段二", "value": "B"},
                {"label": "字段三", "value": "C"},
                {"label": "类别", "value": "普通"},
            ],
            "",
            {"touched": [], "trace": []},
        )
        self.assertEqual(res.state, "completed")
        # Plain fields 1->2 and 2->3 coalesce; settles before dropdown 4 and after dropdown 4 => 2 settles
        self.assertEqual(len(settled_calls), 2)


if __name__ == "__main__":
    unittest.main()
