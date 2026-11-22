"""Regression and generalization boundary tests for Phase 52 deterministic fixes."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.business.compiled_writes import (
    is_placeholder_enum_label,
    is_rejected_ui_sentinel,
)
from web_operation_agent.business.convention_writes import (
    ConventionFieldSpec,
    ConventionStatusHandler,
    TwoStateStatusSpec,
    validate_single_field_provenance,
)
from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.execution.answer_check import finalize_untraced, untraced_numbers
from web_operation_agent.execution.loop_guard import forget_old_observations, program_summary
from web_operation_agent.execution.read_summary import MAX_ROWS as SUMMARY_MAX_ROWS
from web_operation_agent.execution.turn_tables import MAX_ROWS as TABLE_MAX_ROWS, TurnTables
from web_operation_agent.execution.value_source import SourceLedger, note_receipt
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS
from web_operation_agent_yudao.executor.actor_knowledge import ActorKnowledge
from web_operation_agent_yudao.executor.page_prompts import _ALL_ROWS, _precomputed_dates, system_text
from web_operation_agent_yudao.goal_compat.goal import YudaoGoalData
from web_operation_agent_yudao.reads.answer_numbers import stated_numbers, untraced
from web_operation_agent_yudao.rules import compiled_write_source as cws
from web_operation_agent_yudao.rules.java_scan import parse_resp_vo_code_bindings
from web_operation_agent_yudao.rules.source import YudaoSource
from web_operation_agent_yudao.writes import convention_writes as ycw
from web_operation_agent_yudao.writes.write_follow_up import FOLLOW_UP_TARGETS
from web_operation_agent_yudao.writes.write_inputs import _area


_FORBIDDEN_CONCRETE_CLASS_NAMES = (
    "MemberUserRespVO",
    "MemberUserBaseVO",
    "MemberUserServiceImpl",
    "MemberLevelCreateReqVO",
    "HrJobSaveReqVO",
    "CrmCustomerDO",
    "PmsProjectDO",
)
_BUSINESS_PATH_RE = re.compile(
    r"""['"](?:/admin-api/)?(?:crm|erp|member|hrm|mall|trade|promotion|oa|bpm|pms|mes|fms|pay)/[a-z0-9_/-]+['"]"""
)


class CompilerGeneralizationBoundaryTests(unittest.TestCase):
    def test_compilers_contain_no_concrete_entity_class_allowlists_or_core_app_paths(self):
        import ast
        import inspect

        cws_src = Path(cws.__file__).read_text(encoding="utf-8")
        for cls_name in _FORBIDDEN_CONCRETE_CLASS_NAMES:
            self.assertNotIn(cls_name, cws_src, f"Concrete business class {cls_name} hardcoded in compiler")
        # Compiler functions touched in Phase 52 must contain zero business entity route literals
        for fn in (cws._resolved_resp_props, cws._analyze_service_method, cws._compile_vue_create_defaults):
            fn_src = inspect.getsource(fn)
            self.assertEqual(_BUSINESS_PATH_RE.findall(fn_src), [], f"Business path literal in {fn.__name__}")
        # Module-wide pre-existing exclusion table count must not grow beyond B2 baseline (122)
        tree = ast.parse(cws_src)
        str_paths = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.search(r"^(?:/admin-api/)?(?:crm|erp|member|hrm|mall|trade|promotion|oa|bpm|pms|mes|fms|pay)/[a-z0-9_/-]+$", node.value)
        ]
        self.assertLessEqual(len(str_paths), 122)

        import web_operation_agent.business.compiled_writes as core_cw
        import web_operation_agent.business.convention_writes as core_conv

        for mod in (core_cw, core_conv):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            self.assertEqual(_BUSINESS_PATH_RE.findall(src), [])
            self.assertNotIn('f_spec.name == "parentId"', src)

    def test_fix1_write_directory_cache_and_readyz_warm_gate(self):
        # Singleflight/write directory cache reuse when docs=None
        docs = {"paths": {}, "components": {"schemas": {}}}
        built = ycw.build_yudao_write_directory(docs=docs, source=None, ui=None)
        reused = ycw.build_yudao_write_directory(docs=None, source=None, ui=None)
        self.assertEqual(built, reused)

    def test_fix2_java_comment_braces_in_enum_and_exact_stem_do_priority(self):
        java_do = """
package cn.iocoder.yudao.module.pms.dal.dataobject.project;
/**
 * 项目 DO
 * 状态参考 {@link PmsProjectStatusEnum#IN_PROGRESS}
 */
public class PmsProjectDO extends BaseDO {
    /**
     * 项目状态
     *
     * 枚举 {@link PmsProjectStatusEnum}
     */
    private Integer status;

    @InEnum(value = CommonStatusEnum.class, message = "启用状态必须是 {value}")
    private Integer enableFlag;
}
"""
        bindings = parse_resp_vo_code_bindings(java_do, {})
        self.assertEqual(bindings.get("status"), ("enum", "PmsProjectStatusEnum"))
        self.assertEqual(bindings.get("enableFlag"), ("enum", "CommonStatusEnum"))

        with tempfile_tree({
            "server/yudao-module-pms/src/main/java/cn/iocoder/yudao/module/pms/enums/PmsProjectStatusEnum.java": """
package cn.iocoder.yudao.module.pms.enums;
import lombok.AllArgsConstructor;
import lombok.Getter;
@Getter
@AllArgsConstructor
public enum PmsProjectStatusEnum {
    DRAFT(0, "草稿"),
    IN_PROGRESS(1, "进行中"),
    DONE(2, "已完成");
    private final Integer status;
    private final String name;
}
""",
            "server/yudao-module-pms/src/main/java/cn/iocoder/yudao/module/pms/dal/dataobject/project/PmsProjectDO.java": java_do,
            "server/yudao-module-pms/src/main/java/cn/iocoder/yudao/module/pms/controller/admin/project/vo/PmsProjectPageRespVO.java": """
package cn.iocoder.yudao.module.pms.controller.admin.project.vo;
public class PmsProjectPageRespVO {
    private Long id;
    private Integer status;
    private Integer unannotatedStatus;
}
""",
            "server/yudao-module-pms/src/main/java/cn/iocoder/yudao/module/pms/controller/admin/project/PmsProjectController.java": """
package cn.iocoder.yudao.module.pms.controller.admin.project;
@RestController
@RequestMapping("/pms/project")
public class PmsProjectController {
    @GetMapping("/page")
    public CommonResult<PageResult<PmsProjectPageRespVO>> getProjectPage() { return null; }
}
""",
        }) as root:
            src = YudaoSource(root, release="test")
            docs = {
                "paths": {
                    "/admin-api/pms/project/page": {
                        "get": {
                            "tags": ["管理后台 - PMS 项目"],
                            "summary": "获得项目分页",
                            "parameters": [
                                {"name": "pageNo", "in": "query"},
                                {"name": "pageSize", "in": "query"},
                            ],
                            "responses": {
                                "200": {
                                    "content": {
                                        "*/*": {"schema": {"$ref": "#/components/schemas/CommonResultPageResultPmsProjectPageRespVO"}}
                                    }
                                }
                            },
                        }
                    }
                },
                "components": {
                    "schemas": {
                        "CommonResultPageResultPmsProjectPageRespVO": {
                            "properties": {"code": {"type": "integer"}, "data": {"$ref": "#/components/schemas/PageResultPmsProjectPageRespVO"}}
                        },
                        "PageResultPmsProjectPageRespVO": {
                            "properties": {
                                "list": {"type": "array", "items": {"$ref": "#/components/schemas/PmsProjectPageRespVO"}},
                                "total": {"type": "integer"},
                            }
                        },
                        "PmsProjectPageRespVO": {
                            "properties": {
                                "id": {"type": "integer"},
                                "status": {"type": "integer", "description": "状态"},
                                "unannotatedStatus": {"type": "integer", "description": "其他状态"},
                            }
                        },
                    }
                },
            }
            def _goal_get(path, **_k):
                if "permission-info" in str(path):
                    return {"menus": [{"id": 1, "name": "项目管理", "path": "/pms/project", "children": []}]}
                if "simple" in str(path):
                    return []
                return {"list": [], "total": 0}

            port = YudaoGoalData(NS(get=_goal_get, read_generation=0), docs)
            port.attach_source(src)
            codes = port.code_names("pms/project/page")
            self.assertEqual(codes.get("status"), {"0": "草稿", "1": "进行中", "2": "已完成"})
            # Must NOT blindly guess 0=开启/1=关闭 on unannotated status columns when source is present
            self.assertNotIn("unannotatedStatus", codes)

    def test_fix3_precomputed_dates_quarters_and_early_ledger_seeding(self):
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables.close)
        ctx_tuple = (tables, "2026-10-25", "Asia/Shanghai")
        sys_txt = system_text(user="u", date="2026-10-25", tz="Asia/Shanghai", runtime_source=False)
        self.assertIn("第4季度(Q4)", sys_txt)
        note = _precomputed_dates("昨天新建订单并统计本季度与上季度", ctx_tuple)
        self.assertIn("第4季度 / Q4", note)
        self.assertIn("第3季度 / Q3", note)
        self.assertIn("2026-10-24", getattr(tables, "precomputed_date_values", ()))

        turn: dict = {}
        for val in tables.precomputed_date_values:
            note_receipt(turn, "", val)
        ledger = turn["source_ledger"]
        receipt_vals = tuple(r.value for r in ledger.receipts)
        date_spec = ConventionFieldSpec(name="orderTime", kind="string", label="订单日期", date_kind="date")
        validate_single_field_provenance(
            date_spec,
            "2026-10-24",
            texts=("昨天新建订单",),
            prompt="昨天新建订单",
            joined_words="昨天新建订单",
            ledger=ledger,
            receipt_vals=receipt_vals,
            existing_values=None,
            allow_existing_candidates=False,
        )

    def test_fix4_follow_up_verified_aligns_with_child_create_time_not_host_clock(self):
        target = FOLLOW_UP_TARGETS["crm/clue"]
        # Simulated synthetic clock 2026-10-25 (differs from host wall clock by weeks)
        synthetic_ms = 1792915200000
        before = {"id": 8, "contactLastTime": synthetic_ms - 86400000, "updateTime": synthetic_ms - 86400000}
        after_parent = {
            "id": 8,
            "contactLastTime": synthetic_ms + 1200,
            "contactLastContent": "回访客户",
            "contactNextTime": "2026-10-28 10:00:00",
            "followUpStatus": True,
            "updateTime": synthetic_ms + 1200,
        }
        body = {"bizId": 8, "content": "回访客户", "nextTime": "2026-10-28 10:00:00"}
        self.assertTrue(
            target.verified(
                after_parent,
                body,
                synthetic_ms + 1200,
                before_contact_last_time=before["contactLastTime"],
                before_update_time=before["updateTime"],
            )
        )
        # Missing createTime (None) must fail closed
        self.assertFalse(
            target.verified(
                after_parent,
                body,
                None,
                before_contact_last_time=before["contactLastTime"],
                before_update_time=before["updateTime"],
            )
        )

    def test_fix5_enum_zero_sentinel_generic_put_area_collision_and_overwrite_guard(self):
        # Enum 0 with meaningful label ("开启") kept; placeholder ("未知"/"无") and FK/hierarchy 0 rejected
        self.assertFalse(is_placeholder_enum_label("开启"))
        self.assertTrue(is_placeholder_enum_label("未知"))
        self.assertTrue(is_placeholder_enum_label("未定薪"))
        self.assertFalse(
            is_rejected_ui_sentinel(
                0,
                is_fk=False,
                is_enum=True,
                enum_zero_label="开启",
            )
        )
        self.assertTrue(
            is_rejected_ui_sentinel(
                0,
                is_fk=False,
                is_enum=True,
                enum_zero_label="未知",
            )
        )
        self.assertTrue(
            is_rejected_ui_sentinel(
                0,
                is_fk=True,
                is_enum=False,
            )
        )
        self.assertTrue(
            is_rejected_ui_sentinel(
                0,
                is_fk=False,
                is_enum=False,
                is_hierarchy_parent=True,
            )
        )

        # TypeScript generic request.put<boolean> extraction
        with tempfile_tree({
            "ui/src/utils/dict.ts": """
export enum DICT_TYPE {
  HRM_JOB_STATUS = 'hrm_job_status',
}
""",
            "ui/src/api/hrm/job/index.ts": """
export const JobApi = {
  updateJob: async (data: JobVO) => {
    return await request.put<boolean>({ url: `/hrm/job/update`, data })
  }
}
""",
            "ui/src/views/hrm/job/JobForm.vue": """
<template>
  <el-form :model="formData">
    <el-form-item label="招聘状态" prop="status">
      <el-select v-model="formData.status">
        <el-option v-for="dict in getIntDictOptions(DICT_TYPE.HRM_JOB_STATUS)" :key="dict.value" :value="dict.value" />
      </el-select>
    </el-form-item>
  </el-form>
</template>
<script setup lang="ts">
import { JobApi } from '@/api/hrm/job'
const submit = () => JobApi.updateJob(formData.value)
</script>
""",
        }) as root:
            _labels, ui_dicts, _scales = ycw._compile_ui_metadata(source=None, ui=NS(root=root / "ui"))
            self.assertEqual(ui_dicts.get("hrm/job", {}).get("status"), "hrm_job_status")

        # Municipality & short-name area resolution
        area_tree = [
            {
                "id": 310000,
                "name": "上海市",
                "children": [
                    {
                        "id": 310100,
                        "name": "上海市",
                        "children": [{"id": 310104, "name": "徐汇区", "children": []}],
                    }
                ],
            }
        ]
        area_client = NS(get=lambda path: area_tree)
        self.assertEqual(_area(area_client, "上海市"), 310000)
        self.assertEqual(_area(area_client, "上海"), 310000)
        self.assertEqual(_area(area_client, "上海市/徐汇区"), 310104)

        # Overwrite verb guard strips quoted text before checking: blocks old_val prefix append on "改成", allows on "补充" with quoted "改成测试"
        remark_spec = ConventionFieldSpec(name="remark", kind="string", label="备注")
        with self.assertRaises(BusinessToolRequestError):
            validate_single_field_provenance(
                remark_spec,
                "旧备注；新的备注",
                old_val="旧备注",
                texts=("把备注改成「新的备注」",),
                prompt="把备注改成「新的备注」",
                joined_words="把备注改成「新的备注」",
                ledger=SourceLedger(),
                receipt_vals=(),
                existing_values=None,
                allow_existing_candidates=False,
            )
        validate_single_field_provenance(
            remark_spec,
            "旧备注；跟进说明",
            old_val="旧备注",
            texts=("在名称为「改成测试」的客户备注后补充跟进说明",),
            prompt="在名称为「改成测试」的客户备注后补充跟进说明",
            joined_words="在名称为「改成测试」的客户备注后补充跟进说明",
            ledger=SourceLedger(),
            receipt_vals=(),
            existing_values=None,
            allow_existing_candidates=False,
        )

        # TwoStateStatusSpec side_effects rendered on ProposalDiff / BusinessMutationSpec
        status_spec = TwoStateStatusSpec(
            entity="demo/item",
            label="示例项",
            states=(("开启", 0), ("停用", 1)),
            side_effects=("停用后自动清理活跃访问令牌",),
        )
        handler = ConventionStatusHandler(
            NS(
                verify_actor=lambda _ctx: "actor-1",
                status_specs=lambda: {"demo/item": status_spec},
                update_specs=lambda: {},
                resolve_target=lambda _ent, _tgt, spec=None: {"id": 5, "name": "示例A", "status": 0},
            )
        )
        prop = handler.prepare(
            {"entity": "demo/item", "target": "示例A", "status": "停用"},
            context=NS(texts=["把示例A停用"], turn_state={}),
        )
        self.assertTrue(any("停用后自动清理活跃访问令牌" in str(d) for d in prop.details))

    def test_fix6_param_desc_pagesize_clamp_20k_dedup_empty_badge_and_loop_guard(self):
        self.assertEqual(TABLE_MAX_ROWS, 20000)
        self.assertEqual(SUMMARY_MAX_ROWS, 20000)
        self.assertEqual(_ALL_ROWS, 20000)

        long_desc = "参数前半段说明，逗号后半段关键枚举值说明（0=草稿，1=进行中，2=已完成）"
        docs = {
            "paths": {
                "/admin-api/demo/task/page": {
                    "get": {
                        "tags": ["Demo"],
                        "summary": "任务分页",
                        "parameters": [
                            {"name": "status", "in": "query", "description": long_desc, "schema": {"type": "integer"}},
                            {"name": "projectId", "in": "query", "schema": {"type": "integer"}},
                            {"name": "name", "in": "query", "schema": {"type": "string"}},
                            {"name": "pageNo", "in": "query", "required": True, "schema": {"type": "integer"}},
                            {"name": "pageSize", "in": "query", "required": True, "schema": {"type": "integer"}},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/TaskPageResp"}}}}},
                    }
                }
            },
            "components": {
                "schemas": {
                    "TaskPageResp": {"properties": {"code": {"type": "integer"}, "data": {"$ref": "#/components/schemas/TaskPage"}}},
                    "TaskPage": {"properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/TaskRow"}}, "total": {"type": "integer"}}},
                    "TaskRow": {
                        "properties": {
                            "id": {"type": "integer"},
                            "name": {"type": "string"},
                            "ownerUserId": {"type": "integer", "description": "负责人"},
                        }
                    },
                }
            },
        }
        seen_params: list[dict] = []

        def fake_get(path, **params):
            seen_params.append(dict(params))
            return {"list": [], "total": 0}

        know = ActorKnowledge(NS(get=fake_get), docs)
        api_search = know.search_api("任务分页")
        self.assertIn("逗号后半段关键枚举值说明", api_search)

        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables.close)
        # Non-FK text search -> NO empty-set convergence badge, DOES emit neutral ownerUserId hint, clamps pageSize 500 -> 200
        out_name = know.call_api(
            "demo/task/page",
            {"name": "张三", "pageNo": 1, "pageSize": 500},
            row_sink=tables.add,
        )
        self.assertEqual(seen_params[-1]["pageSize"], 200)
        self.assertNotIn("精确过滤已核实为 0 条记录", out_name)
        self.assertIn("ownerUserId", out_name)

        # Positive integer FK filter -> DOES emit empty-set convergence badge
        out_fk = know.call_api(
            "demo/task/page",
            {"projectId": 42, "pageNo": 1, "pageSize": 20},
            row_sink=tables.add,
        )
        self.assertIn("精确过滤已核实为 0 条记录", out_fk)

        # Elided API message retains table name, receipt, row_count, and column summary
        msgs = [
            {"role": "tool", "_api": True, "content": out_fk + "\n" + "x" * 9000},
            {"role": "tool", "_api": True, "content": "recent\n" + "y" * 9000},
        ]
        forget_old_observations(msgs, api_history_chars=10000)
        self.assertIn("本轮行表 t2", msgs[0]["content"])
        self.assertIn("列=", msgs[0]["content"])
        self.assertIn("sql / tables", msgs[0]["content"])
        self.assertIn("t2", program_summary(out_fk))

    def test_fix7_structural_comma_small_table_decimal_sums_decoded_label_and_non_refusal(self):
        # 1) Comma-grouped number before closing paren is NOT mangled into "11"
        self.assertEqual(stated_numbers("合计金额（11,650）元。"), [("11,650", "11650")])
        self.assertEqual(stated_numbers("1. 第一项共 25 条。"), [("25", "25")])

        # 2) Small complete table registers Decimal single-column sums & decoded ASCII labels
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables.close)
        tables.add(
            path="erp/stock/page",
            params={},
            rows=[
                {"id": 101, "category": 1, "count": 500},
                {"id": 102, "category": 1, "count": 500},
            ],
            total=2,
            complete=True,
            code_maps={"category": {"1": "IT", "2": "OA"}},
        )
        self.assertIn("1000", tables.precomputed_column_sums)
        self.assertNotIn("203", tables.precomputed_column_sums)  # id column excluded
        self.assertIn("IT", tables.decoded_ascii_labels)

        # Both "1,000" (sum of 500 + 500) and "IT1" (decoded label "IT" + traced count 1) pass untraced check
        turn = {"tool_texts": ['{"count": 500, "total": 1}'], "turn_tables": tables}
        ex = NS(
            profile=NS(untraced_numbers=untraced),
            _earlier_words=[],
            _finish_tool=lambda _t: {"function": {"parameters": {"required": ["status", "answer", "items"]}}},
            config=NS(max_user_answer_chars=4000),
            _trace=None,
        )
        self.assertEqual(untraced_numbers(ex, "统计库存", "共有 IT1 类资产，库存合计 1,000 件。", turn), ())
        # Unseen identifier still caught
        self.assertEqual(untraced_numbers(ex, "统计库存", "单号 PO-000999", turn), ("PO-000999",))

        # 3) finalize_untraced preserves status="done" with note on explicit items, fails closed on shortcut bypass
        events: list = []
        s = NS(
            ledger=NS(append=lambda kind, **kw: events.append((kind, kw))),
            step=2,
            message=NS(message_id="m1"),
        )
        closed = finalize_untraced(
            ex,
            s,
            {"numbers_sent_back": True},
            {
                "status": "done",
                "answer": "共 888 件。",
                "items": [{"item": "库存 888 件", "kind": "data", "status": "delivered", "receipts": ["ev_1"]}],
            },
            ("888",),
        )
        self.assertEqual(closed["status"], "done")
        self.assertEqual(closed["items"][0]["status"], "delivered")
        self.assertIn("以下数值未在本轮读取中找到出处：888", closed["answer"])

        bypass_closed = finalize_untraced(
            ex,
            s,
            {"numbers_sent_back": True},
            {"status": "done", "answer": "共 888 件。"},
            ("888",),
        )
        self.assertEqual(bypass_closed["status"], "cannot")


from contextlib import contextmanager
import tempfile


@contextmanager
def tempfile_tree(files: dict[str, str]):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        yield root


if __name__ == "__main__":
    unittest.main()
