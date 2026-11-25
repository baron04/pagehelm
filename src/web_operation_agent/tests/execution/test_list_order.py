"""Zero-paid-call unit tests for list default order and program-owned ordinal binding."""
from __future__ import annotations

import json
import time
import unittest

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.tools.contracts import BusinessMutationSpec, BusinessToolResult, ToolOperation
from web_operation_agent.business.tools.dispatcher import BusinessToolRuntime
from web_operation_agent.business.tools.registry import ToolDefinition, ToolRegistry
from web_operation_agent.execution.list_order import (
    DeliveredList,
    DeliveredListItem,
    build_delivered_items_from_rows,
    check_answer_list_order,
    enforce_sql_default_order,
    extract_delivered_list_from_answer,
    extract_delivered_list_from_observation,
    extract_delivered_list_from_text,
    extract_prefix_limit,
    has_explicit_sort_request,
    parse_ordinal_reference,
    reorder_answer_to_default,
    resolve_ordinal_binding,
    strip_unrequested_sort_params,
    target_matches_bound,
    value_matches_other_row,
)
from web_operation_agent.execution.turn_tables import TurnTables
from web_operation_agent.schemas.approval_details import ApprovalDetail
from web_operation_agent.state.identity import OperationContext, PRINCIPAL_CONTEXT_VERSION, PrincipalContext


class _FakeExecutor:
    def tool_schemas(self):
        return ()

    def tool_effects(self):
        return {"update_item": "write"}

    def execute(self, name, args, *, context=None):
        raise AssertionError("unexpected read")

    def prepare(self, args, *, context=None):
        return BusinessMutationSpec(
            args={"id": 22, "remark": args["remark"]},
            effect="修改记录备注",
            targets=("乙号记录",),
            details=(ApprovalDetail("change", "备注", "旧 -> " + args["remark"]),),
            reconciliation={"id": 22},
        )

    def dispatch(self, args, *, context=None):
        return BusinessToolResult(
            content={"id": args["id"], "remark": args["remark"]},
            audit={"operation": "update_item", "id": args["id"]},
        )


class SortIntentAndPrefixTests(unittest.TestCase):
    def test_explicit_sort_detection_by_class(self):
        for ask in (
            "列出所有记录，按创建时间从新到旧排序",
            "按金额降序排列前 5 条",
            "帮我看最新的 3 条申请",
            "销量最高的 3 个品是什么",
            "查最近 5 条单据",
            "把列表按编号从小到大排",
            "把列表按总数量从少到多列出来",
            "按可用数量从多到少展示",
            "把条目按单价从高到低排一下",
            "按分数从低到高列出前 3 项",
            "由少到多排列当前条目",
            "按指标由多到少列出来",
            "按数值从少至多列出",
        ):
            with self.subTest(ask=ask):
                self.assertTrue(has_explicit_sort_request(ask))

        for ask in (
            "列出当前所有记录",
            "把列表列出来",
            "给我看前 3 个",
            "前 5 条是什么",
            "前 3 名名单发我",
            "查一下待处理的 3 笔单子",
        ):
            with self.subTest(ask=ask):
                self.assertFalse(has_explicit_sort_request(ask))

    def test_extract_prefix_limit(self):
        self.assertEqual(extract_prefix_limit("给我看前 3 个"), 3)
        self.assertEqual(extract_prefix_limit("前五条是什么"), 5)
        self.assertIsNone(extract_prefix_limit("前 3 天的数据"))
        self.assertIsNone(extract_prefix_limit("最新的前 3 个"))
        self.assertIsNone(extract_prefix_limit("按金额排前 3 名"))


class ApiAndSqlOrderEnforcementTests(unittest.TestCase):
    def test_strip_unrequested_sort_params(self):
        params = {
            "pageNo": 1,
            "pageSize": 20,
            "status": 0,
            "order": "asc",
            "sortField": "id",
            "sortingField.field": "createTime",
        }
        cleaned, removed = strip_unrequested_sort_params(params)
        self.assertEqual(cleaned, {"pageNo": 1, "pageSize": 20, "status": 0})
        self.assertEqual(sorted(removed), ["order", "sortField", "sortingField.field"])

    def test_enforce_sql_default_order_strips_unsolicited_order_by_and_keeps_limit(self):
        sql = "SELECT id, name FROM t1 WHERE status = 0 ORDER BY id ASC LIMIT 3"
        rewritten, changed, orig = enforce_sql_default_order(sql, allow_sort=False)
        self.assertTrue(changed)
        self.assertEqual(orig, "id ASC")
        self.assertEqual(rewritten, "SELECT id, name FROM t1 WHERE status = 0 ORDER BY _row ASC LIMIT 3")

    def test_enforce_sql_default_order_allows_user_sort_and_aggregates(self):
        sql = "SELECT id, name FROM t1 ORDER BY amount DESC LIMIT 5"
        rewritten, changed, _ = enforce_sql_default_order(sql, allow_sort=True)
        self.assertFalse(changed)
        self.assertEqual(rewritten, sql)

        agg_sql = "SELECT deptId, COUNT(*) AS cnt FROM t1 GROUP BY deptId ORDER BY cnt DESC"
        rewritten_agg, changed_agg, _ = enforce_sql_default_order(agg_sql, allow_sort=False)
        self.assertFalse(changed_agg)
        self.assertEqual(rewritten_agg, agg_sql)

    def test_turn_tables_query_enforces_default_order_and_tracks_rows(self):
        tables = TurnTables()
        try:
            tables.add(
                path="oa/demo/page",
                params={"status": 0},
                rows=[
                    {"id": 9, "name": "第九项"},
                    {"id": 2, "name": "第二项"},
                    {"id": 5, "name": "第五项"},
                ],
                total=3,
                complete=True,
            )
            res = tables.query(
                "SELECT id, name FROM t1 ORDER BY id ASC LIMIT 2",
                preserve_default_order=True,
            )
            self.assertTrue(res.get("order_enforced"))
            self.assertEqual(res["rows"], [[9, "第九项"], [2, "第二项"]])
            ref_rows, meta = tables.rows_for_ref(res["table_ref"])
            self.assertEqual([r["id"] for r in ref_rows], [9, 2])
            self.assertEqual(meta["path"], "oa/demo/page")
        finally:
            tables.close()


class OrdinalBindingAndDeliveryOrderTests(unittest.TestCase):
    def test_parse_ordinal_reference_and_non_false_positives(self):
        self.assertEqual(parse_ordinal_reference("把第 2 个禁用").position, 2)
        self.assertEqual(parse_ordinal_reference("看看第三条的详情").position, 3)
        self.assertEqual(parse_ordinal_reference("把第0个删掉").position, 0)
        rev = parse_ordinal_reference("把倒数第 2 个停用")
        self.assertIsNotNone(rev)
        self.assertTrue(rev.from_end)
        self.assertEqual(rev.position, 2)
        last_ref = parse_ordinal_reference("看看最后一个")
        self.assertIsNotNone(last_ref)
        self.assertTrue(last_ref.from_end)
        self.assertEqual(last_ref.position, 1)

        for non_ordinal in (
            "第 1 季度的汇总是多少",
            "翻到第 2 页",
            "金额排第 2 的是谁",
            "销量排名第 1 的是哪项",
            "第 1 车间的产量是多少",
            "前 3 个是什么",
        ):
            with self.subTest(prompt=non_ordinal):
                self.assertIsNone(parse_ordinal_reference(non_ordinal))

    def test_resolve_ordinal_binding_states(self):
        dlist = DeliveredList(
            entity="demo/item",
            source_path="/admin-api/demo/item/page",
            label="演示列表",
            total_rows=3,
            ordered_by_user=False,
            items=(
                DeliveredListItem(1, "10", "甲项", ("NO-10", "10")),
                DeliveredListItem(2, "22", "乙项", ("NO-22", "22")),
                DeliveredListItem(3, "35", "丙项", ("NO-35", "35")),
            ),
        )
        self.assertEqual(resolve_ordinal_binding(parse_ordinal_reference("第 2 个"), None).status, "missing_list")
        self.assertEqual(resolve_ordinal_binding(parse_ordinal_reference("第 0 个"), dlist).status, "out_of_range")
        self.assertEqual(resolve_ordinal_binding(parse_ordinal_reference("第 5 个"), dlist).status, "out_of_range")
        res = resolve_ordinal_binding(parse_ordinal_reference("第 2 个"), dlist)
        self.assertEqual(res.status, "bound")
        self.assertEqual(res.resolved_position, 2)
        self.assertEqual(res.item.record_id, "22")
        self.assertTrue(target_matches_bound("乙项", res.item))
        self.assertTrue(target_matches_bound("22", res.item))
        other = value_matches_other_row("甲项", dlist, 2)
        self.assertIsNotNone(other)
        self.assertEqual(other.record_id, "10")
        self.assertIsNone(value_matches_other_row("无关新负责人姓名", dlist, 2))

    def test_check_and_repair_delivery_order_and_prefix(self):
        rows = [
            {"id": 9, "name": "第九项"},
            {"id": 2, "name": "第二项"},
            {"id": 5, "name": "第五项"},
        ]
        reordered = "结果如下：\n1. 第二项（ID=2）\n2. 第五项（ID=5）\n3. 第九项（ID=9）"
        chk = check_answer_list_order(reordered, rows)
        self.assertFalse(chk["in_order"])
        fixed, changed = reorder_answer_to_default(reordered, rows)
        self.assertTrue(changed)
        chk_after = check_answer_list_order(fixed, rows)
        self.assertTrue(chk_after["in_order"])
        self.assertEqual(chk_after["matched_indices"], [0, 1, 2])

        wrong_prefix = "1. 第二项（ID=2）\n2. 第五项（ID=5）"
        chk_p = check_answer_list_order(wrong_prefix, rows, prefix_limit=2)
        self.assertFalse(chk_p["prefix_ok"])
        fixed_p, changed_p = reorder_answer_to_default(wrong_prefix, rows, prefix_limit=2)
        self.assertTrue(changed_p)
        self.assertEqual(check_answer_list_order(fixed_p, rows, prefix_limit=2)["matched_indices"], [0, 1])

        delivered = extract_delivered_list_from_answer(
            fixed,
            rows,
            entity="demo/item",
            source_path="/admin-api/demo/item/page",
            label="demo/item",
            total_rows=3,
            ordered_by_user=False,
        )
        self.assertIsNotNone(delivered)
        self.assertEqual([it.record_id for it in delivered.items], ["9", "2", "5"])

    def test_spec_decorator_preserves_proposal_digest_and_approval_execution(self):
        schema = {
            "type": "object",
            "properties": {"target": {"type": "string"}, "remark": {"type": "string"}},
            "required": ["target", "remark"],
            "additionalProperties": False,
        }
        definition = ToolDefinition(
            name="update_item",
            description="update",
            effect="write",
            operation=ToolOperation("demo.item", "update"),
            scope="demo.write",
            input_json=json.dumps(schema),
            handler=_FakeExecutor(),
            reference_inputs=("target",),
        )
        registry = ToolRegistry((definition,), mutation_dispatch_enabled=True)
        runtime = BusinessToolRuntime((), registry, application_id="demo-app")
        now = int(time.time() * 1000)
        principal = PrincipalContext(
            schema_version=PRINCIPAL_CONTEXT_VERSION,
            application_id="demo-app",
            tenant_id="1",
            workspace_id="1",
            actor_id="100",
            delegation_id="del-1",
            issued_at=now - 1000,
            expires_at=now + 60_000,
            scopes=("demo.write",),
        )
        ctx = OperationContext.create(
            principal,
            session_id="s1",
            turn_id="t1",
            operation_id="op1",
            deadline_ms=now + 60_000,
            now_ms=now,
        )

        def decorator(spec: BusinessMutationSpec) -> BusinessMutationSpec:
            return BusinessMutationSpec(
                args=spec.args,
                effect=spec.effect,
                targets=(f"列表第 2 行：{spec.targets[0]}",),
                reconcile_key=spec.reconcile_key,
                details=(ApprovalDetail("target_rule", "列表序号绑定", "最近交付列表第 2 行：乙号记录（ID=22）"), *spec.details),
                reconciliation=spec.reconciliation,
            )

        proposal = runtime.prepare_mutation(
            "update_item",
            {"target": "乙号记录", "remark": "新备注"},
            set(),
            context=ctx,
            spec_decorator=decorator,
        )
        self.assertEqual(proposal.targets[0], "列表第 2 行：乙号记录")
        self.assertEqual(proposal.details[0].kind, "target_rule")
        result = runtime.execute_mutation(proposal, proposal.content_digest, set(), context=ctx)
        self.assertEqual(result.content["id"], 22)

        def mismatch_decorator(spec: BusinessMutationSpec) -> BusinessMutationSpec:
            raise BusinessToolRequestError("ordinal mismatch")

        with self.assertRaises(BusinessToolRequestError):
            runtime.prepare_mutation(
                "update_item",
                {"target": "乙号记录", "remark": "新备注"},
                set(),
                context=ctx,
                spec_decorator=mismatch_decorator,
            )

    def test_extract_delivered_list_from_observation_deduplicates_rows_in_dom_order(self):
        from types import SimpleNamespace as NS

        obs = NS(
            url="https://synthetic.test/demo/items",
            page_route="/demo/items",
            title="演示条目列表",
            page_read={"path": "/demo/items/page", "total": 12},
            elements=[
                NS(ref="e1", role="button", name="查询", attrs={"page_control": "query"}),
                NS(
                    ref="e10",
                    role="button",
                    name="编辑",
                    attrs={
                        "record_table": "main-table",
                        "record_id": "19",
                        "record_values": json.dumps([["编码", "DEMO-19"], ["名称", "首行甲项"], ["创建时间", "2026-10-20"]]),
                    },
                ),
                NS(
                    ref="e11",
                    role="button",
                    name="删除",
                    attrs={
                        "record_table": "main-table",
                        "record_id": "19",
                        "record_values": json.dumps([["编码", "DEMO-19"], ["名称", "首行甲项"]]),
                    },
                ),
                NS(
                    ref="e12",
                    role="button",
                    name="编辑",
                    attrs={
                        "record_table": "main-table",
                        "record_id": "11",
                        "record_values": json.dumps([["编码", "DEMO-11"], ["名称", "次行乙项"], ["状态", "启用"]]),
                    },
                ),
            ],
        )
        delivered = extract_delivered_list_from_observation(obs, ordered_by_user=False)
        self.assertIsNotNone(delivered)
        self.assertEqual(delivered.entity, "demo/items")
        self.assertEqual(delivered.total_rows, 12)
        self.assertEqual([it.record_id for it in delivered.items], ["19", "11"])
        self.assertEqual([it.display for it in delivered.items], ["DEMO-19", "DEMO-11"])
        self.assertIn("次行乙项", delivered.items[1].aliases)
        self.assertIsNone(extract_delivered_list_from_observation(NS(elements=[])))

    def test_extract_delivered_list_from_text_supports_tabular_numbered_and_slash_lists(self):
        # Markdown table
        md = (
            "| 编号 | 名称 | 状态 |\n"
            "| --- | --- | --- |\n"
            "| 101 | 甲号记录 | 启用 |\n"
            "| 102 | 乙号记录 | 停用 |"
        )
        d_md = extract_delivered_list_from_text(md, entity="demo/items")
        self.assertIsNotNone(d_md)
        self.assertEqual([it.record_id for it in d_md.items], ["101", "102"])
        self.assertEqual([it.display for it in d_md.items], ["甲号记录", "乙号记录"])

        # Stacked header + TSV data rows (innerText style)
        tsv = (
            "单号\n\t\n对方\n\t\n金额\n\n"
            "NO-001\t甲公司\t300.00\n"
            "NO-002\t乙公司\t180.00\n"
            "NO-003\t丙公司\t120.00\n"
        )
        d_tsv = extract_delivered_list_from_text(tsv)
        self.assertIsNotNone(d_tsv)
        self.assertEqual([it.display for it in d_tsv.items], ["NO-001", "NO-002", "NO-003"])
        self.assertIn("丙公司", d_tsv.items[2].aliases)

        # Slash-separated list rows
        slash_txt = (
            "当前列表如下：\n\n"
            "申请甲标题 / 重要 / 发起人 / 2026-10-23 08:12:08 / 审批中\n"
            "申请乙标题 / 正常 / 发起人 / 2026-10-22 20:06:00 / 未提交"
        )
        d_slash = extract_delivered_list_from_text(slash_txt)
        self.assertIsNotNone(d_slash)
        self.assertEqual([it.display for it in d_slash.items], ["申请甲标题", "申请乙标题"])

        # Numbered list
        num_txt = "1. 条目甲（ID=21）\n2. 条目乙（ID=18）"
        d_num = extract_delivered_list_from_text(num_txt)
        self.assertIsNotNone(d_num)
        self.assertEqual([(it.record_id, it.display) for it in d_num.items], [("21", "条目甲"), ("18", "条目乙")])

        # Single-record key-value summary bullet list or 2-column 字段/值 table must NOT be treated as a list
        detail_bullets = "- 名称：甲号记录\n- 状态：启用\n- 创建时间：2026-10-20"
        self.assertIsNone(extract_delivered_list_from_text(detail_bullets))
        kv_table = "| 字段 | 值 |\n| --- | --- |\n| 名称 | 甲号记录 |\n| 状态 | 启用 |"
        self.assertIsNone(extract_delivered_list_from_text(kv_table))


if __name__ == "__main__":
    unittest.main()
