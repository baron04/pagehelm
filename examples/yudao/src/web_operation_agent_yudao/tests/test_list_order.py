"""Zero-paid-call unit tests for Yudao list order enforcement and program ordinal binding."""
from __future__ import annotations

from dataclasses import replace
import json
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from web_operation_agent.business.approvals import ApprovalWatch
from web_operation_agent.business.control import ApprovalGrant
from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.tools.contracts import BusinessMutationSpec, BusinessToolResult, ToolOperation
from web_operation_agent.business.tools.dispatcher import BusinessToolRuntime
from web_operation_agent.business.tools.registry import ToolDefinition, ToolRegistry
from web_operation_agent.execution.list_order import DeliveredList, DeliveredListItem, resolve_ordinal_binding, parse_ordinal_reference
from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.model_loop_contracts import DeliveryFeedback, LoopToolResult
from web_operation_agent.execution.turn_tables import TurnTables
from web_operation_agent.goal.skill_writes import SkillWrites
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS
from web_operation_agent.schemas.approval_details import ApprovalDetail
from web_operation_agent.skills import SkillArtifact, SkillRegistry
from web_operation_agent.state.conversation import ConversationState
from web_operation_agent.state.identity import PRINCIPAL_CONTEXT_VERSION, PrincipalContext
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent_yudao.executor.actor_knowledge import ActorKnowledge
from web_operation_agent_yudao.executor.page_prompts import SlimTurnContext
from web_operation_agent_yudao.executor.slim_loop import YudaoLoopProcedures, YudaoTurnExecutor
from web_operation_agent_yudao.tests.slim_loop_support import Budget, Channel, Ledger, State


_DEMO_DOCS = {
    "paths": {
        "/admin-api/demo/items/page": {
            "get": {
                "summary": "获得条目分页",
                "tags": ["演示 - 条目"],
                "parameters": [
                    {"name": "pageNo", "in": "query", "schema": {"type": "integer"}},
                    {"name": "pageSize", "in": "query", "schema": {"type": "integer"}},
                    {"name": "order", "in": "query", "schema": {"type": "string"}},
                    {"name": "sortField", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/Result"}}}}},
            }
        },
        "/admin-api/demo/items/simple-list": {
            "get": {
                "summary": "获得条目精简列表",
                "tags": ["演示 - 条目"],
                "parameters": [],
                "responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}},
            }
        },
        "/admin-api/demo/items/get": {
            "get": {
                "summary": "获得条目详情",
                "tags": ["演示 - 条目"],
                "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}],
            }
        },
    },
    "components": {
        "schemas": {
            "Result": {"properties": {"code": {}, "data": {"$ref": "#/components/schemas/Page"}}},
            "Page": {"properties": {"total": {}, "list": {"type": "array", "items": {"type": "object"}}}},
        }
    },
}


class _DemoClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, **params):
        self.calls.append((path, dict(params)))
        if path == "system/auth/get-permission-info":
            return {"user": {"id": 1, "nickname": "测试员"}, "menus": []}
        if path == "demo/items/page":
            # Application default order: IDs 9, 2, 5 (descending / business order)
            rows = [
                {"id": 9, "name": "首行甲项", "no": "NO-09"},
                {"id": 2, "name": "次行乙项", "no": "NO-02"},
                {"id": 5, "name": "末行丙项", "no": "NO-05"},
            ]
            if params.get("sortField") == "id" and params.get("order") == "asc":
                rows = sorted(rows, key=lambda r: r["id"])
            return {"total": 3, "list": rows}
        if path == "demo/items/simple-list":
            return [
                {"id": 2, "name": "次行乙项"},
                {"id": 5, "name": "末行丙项"},
                {"id": 9, "name": "首行甲项"},
            ]
        if path == "demo/items/get":
            rid = int(params.get("id", 0))
            by_id = {
                9: {"id": 9, "name": "首行甲项", "remark": "第一条备注"},
                2: {"id": 2, "name": "次行乙项", "remark": "第二条备注"},
                5: {"id": 5, "name": "末行丙项", "remark": "第三条备注"},
            }
            return by_id.get(rid, {})
        raise AssertionError(f"unexpected GET {path}")


class _DemoWriteExecutor:
    def __init__(self):
        self.prepared_targets: list[str] = []

    def tool_schemas(self):
        return ()

    def tool_effects(self):
        return {"update_record": "write"}

    def execute(self, name, args, *, context=None):
        raise AssertionError("read not expected")

    def prepare(self, args, *, context=None):
        self.prepared_targets.append(args["target"])
        id_map = {"首行甲项": 9, "次行乙项": 2, "末行丙项": 5}
        rid = id_map.get(args["target"], 9)
        return BusinessMutationSpec(
            args={"id": rid, "target": args["target"], "remark": args["remark"]},
            effect="修改记录",
            targets=(f"{args['target']}（ID={rid}）",),
            details=(ApprovalDetail("change", "备注", f"旧 -> {args['remark']}"),),
            reconciliation={"id": rid},
        )

    def dispatch(self, args, *, context=None):
        return BusinessToolResult(
            content={"id": args["id"], "target": args["target"], "remark": args["remark"], "reference": f"W-{args['id']}"},
            audit={"operation": "update_record", "id": args["id"]},
        )


def _make_procedures(prompt: str, *, delivered_list: dict | None = None):
    client = _DemoClient()
    know = ActorKnowledge(client, BASELINE_SESSION_HANDOFFS)
    know._docs = lambda: _DEMO_DOCS
    goal_port = NS(
        data=NS(_client=client, _source=None, _ui=None),
        review=None,
        index=lambda context=None: NS(entities=(), pages=()),
        records_named=lambda name, context=None, entities=None: (
            (("demo/items", (("9", "首行甲项", ""),)),) if "甲" in name else
            (("demo/items", (("2", "次行乙项", ""),)),) if "乙" in name else ()
        ),
    )
    tables = TurnTables(BASELINE_SESSION_HANDOFFS)
    ctx = SlimTurnContext(
        current_request=prompt,
        turn_tables=(tables, "2026-10-19", "Asia/Shanghai"),
    )
    from web_operation_agent.execution.list_order import extract_prefix_limit, has_explicit_sort_request

    ctx.explicit_sort = has_explicit_sort_request(prompt)
    ctx.prefix_limit = extract_prefix_limit(prompt)
    ref = parse_ordinal_reference(prompt)
    if ref is not None:
        dl = DeliveredList.from_dict(delivered_list) if delivered_list else None
        ctx.ordinal_binding = resolve_ordinal_binding(ref, dl)
    procs = YudaoLoopProcedures(know, goal_port, "https://synthetic.test", turn_context=ctx)
    return procs, client, know, tables, ctx


class YudaoListOrderTests(unittest.TestCase):
    def test_default_order_strips_call_api_sort_and_sql_order_by_while_honoring_explicit_sort(self):
        # Group A: user does NOT specify sort -> API sort params stripped & SQL ORDER BY overridden
        procs, client, _know, tables, _ctx = _make_procedures("列出前 2 个演示条目")
        try:
            s = State()
            s.operation = None
            turn = {"reads": [], "marked": "", "trace": [], "touched": []}
            res_api = procs._tool(
                s,
                "call_api",
                {"path": "/admin-api/demo/items/page", "params": json.dumps({"pageNo": 1, "pageSize": 20, "order": "asc", "sortField": "id"})},
                turn,
            )
            self.assertEqual(res_api.state, "completed")
            page_call = next(p for ep, p in client.calls if ep == "demo/items/page")
            self.assertNotIn("order", page_call)
            self.assertNotIn("sortField", page_call)
            self.assertTrue(any(ev == "list_order_enforced" for ev, _ in s.ledger.events))

            res_sql = procs._tool(s, "sql", {"query": "SELECT id, name FROM t1 ORDER BY id ASC LIMIT 2"}, turn)
            self.assertEqual(res_sql.state, "completed")
            self.assertIn("首行甲项", res_sql.feedback)
            self.assertIn("次行乙项", res_sql.feedback)
            self.assertEqual(sum(1 for ev, _ in s.ledger.events if ev == "list_order_enforced"), 2)
        finally:
            tables.close()

        # Group B: user EXPLICITLY asks to sort ("按总数量从少到多") -> preserved
        procs_b, client_b, _know_b, tables_b, _ctx_b = _make_procedures("按总数量从少到多列出前 2 个")
        try:
            s_b = State()
            turn_b = {"reads": [], "marked": "", "trace": [], "touched": []}
            procs_b._tool(
                s_b,
                "call_api",
                {"path": "/admin-api/demo/items/page", "params": json.dumps({"pageNo": 1, "pageSize": 20, "order": "asc", "sortField": "id"})},
                turn_b,
            )
            page_call_b = next(p for ep, p in client_b.calls if ep == "demo/items/page")
            self.assertEqual(page_call_b.get("order"), "asc")
            self.assertEqual(page_call_b.get("sortField"), "id")
            res_sql_b = procs_b._tool(s_b, "sql", {"query": "SELECT id, name FROM t1 ORDER BY id ASC LIMIT 2"}, turn_b)
            self.assertIn("次行乙项", res_sql_b.feedback)
            self.assertIn("末行丙项", res_sql_b.feedback)
            self.assertNotIn("list_order_enforced", [ev for ev, _ in s_b.ledger.events])
        finally:
            tables_b.close()

    def test_finish_bounces_reordered_delivery_and_closing_repairs_and_persists_delivered_list(self):
        executor = YudaoTurnExecutor.__new__(YudaoTurnExecutor)
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables.close)
        tables.add(
            path="demo/items/page",
            params={"pageNo": 1},
            rows=[
                {"id": 9, "name": "首行甲项", "no": "NO-09"},
                {"id": 2, "name": "次行乙项", "no": "NO-02"},
                {"id": 5, "name": "末行丙项", "no": "NO-05"},
            ],
            total=3,
            complete=True,
        )
        receipt = tables.issue_receipt()
        executor.turn_context = SlimTurnContext(
            current_request="列出前 2 个演示条目",
            explicit_sort=False,
            prefix_limit=2,
            turn_tables=(tables, "2026-10-19", "Asia/Shanghai"),
        )
        executor.knowledge = NS(handoffs=BASELINE_SESSION_HANDOFFS)
        executor.procedures = NS(form_fill=None)
        executor.config = NS(disabled_actions=frozenset(), max_user_answer_chars=4000)

        ledger = Ledger()
        s = NS(
            obs=NS(url="https://synthetic.test/demo", page_route="/demo", elements=()),
            ledger=ledger,
            delivered_list=None,
            mutation_outcomes=[],
        )
        turn = {"reads": [], "written": [], "trace": [], "touched": []}
        reordered_args = {
            "status": "done",
            "answer": "前 2 个如下：\n1. 次行乙项（ID=2）\n2. 末行丙项（ID=5）",
            "items": [{"item": "前2个", "kind": "data", "status": "delivered", "receipts": [receipt]}],
        }
        with patch.object(ModelDrivenTurnExecutor, "_finish_feedback", return_value=""):
            fb = YudaoTurnExecutor._finish_feedback(executor, s, turn, reordered_args)
        self.assertIsInstance(fb, DeliveryFeedback)
        self.assertEqual(fb.code, "list_delivery_order_mismatch")

        # Simulate model stubbornly returning reordered prose a second time: _closing repairs deterministically
        with patch.object(ModelDrivenTurnExecutor, "_finish_feedback", return_value=""):
            fb2 = YudaoTurnExecutor._finish_feedback(executor, s, turn, reordered_args)
        self.assertEqual(fb2, "")
        with patch.object(ModelDrivenTurnExecutor, "_closing", side_effect=lambda _s, _t, a: a):
            closed = YudaoTurnExecutor._closing(executor, s, turn, reordered_args)
        self.assertIn("首行甲项", closed["answer"])
        self.assertIn("次行乙项", closed["answer"])
        self.assertNotIn("末行丙项", closed["answer"])
        self.assertIsNotNone(s.delivered_list)
        self.assertEqual([it["record_id"] for it in s.delivered_list["items"]], ["9", "2"])

    def test_simple_list_bounces_when_ui_main_list_exists(self):
        procs, _client, _know, tables, _ctx = _make_procedures("列出演示条目")
        try:
            s = State()
            s.operation = None
            turn = {"reads": [], "marked": "", "trace": [], "touched": []}
            policy_turn = {"reads": [], "written": [], "trace": [], "touched": []}
            procs._ctx().executor_turn = policy_turn
            res = procs._tool(s, "call_api", {"path": "/admin-api/demo/items/simple-list", "params": "{}"}, turn)
            self.assertEqual(res.state, "completed")
            self.assertIn("/admin-api/demo/items/page", res.feedback)
            self.assertEqual(policy_turn["simple_list_substitutes"]["/admin-api/demo/items/simple-list"], "/admin-api/demo/items/page")
        finally:
            tables.close()

    def test_turn_start_ordinal_missing_and_out_of_range_return_needs_input_with_zero_model_calls(self):
        class _NeverCallChat:
            def complete(self, *args, **kwargs):
                raise AssertionError("model should not be called")

        def make_exec(conv_state):
            ex = YudaoTurnExecutor.__new__(YudaoTurnExecutor)
            ex.turn_context = SlimTurnContext()
            ex.knowledge = NS(client=NS(read_generation=0), handoffs=BASELINE_SESSION_HANDOFFS, turn_targets={}, probed_s3_edges=set())
            ex.conversation_state = conv_state
            ex.procedures = NS(form_fill=None, turn_context=None)
            ex.chat = _NeverCallChat()
            return ex

        # Missing list: write turn clarifies with zero model calls, read turn proceeds normally
        conv1 = ConversationState()
        ex1 = make_exec(conv1)
        s1 = NS(
            message=NS(text="把第 2 个的备注改为测试"),
            run_id="run-1",
            ledger=Ledger(),
            approvals=NS(mutations_enabled=True),
            terminal_reason="",
            stop_reason="",
            clarification=None,
        )
        q1 = YudaoTurnExecutor._run(ex1, s1)
        self.assertEqual(s1.terminal_reason, "needs_clarification")
        self.assertIn("第 2 个", q1)

        s_read = NS(
            message=NS(text="查看第 2 个的详情"),
            run_id="run-read-missing",
            ledger=Ledger(),
            approvals=NS(mutations_enabled=True),
            terminal_reason="",
            stop_reason="",
            clarification=None,
        )
        with patch.object(ModelDrivenTurnExecutor, "_run", return_value="已继续执行读取") as mocked_super:
            res_read = YudaoTurnExecutor._run(ex1, s_read)
        self.assertEqual(res_read, "已继续执行读取")
        mocked_super.assert_called_once()
        self.assertEqual(s_read.terminal_reason, "")
        self.assertTrue(any(ev == "list_ordinal_missing" for ev, _ in s_read.ledger.events))

        # Out of range (3 items delivered, asking for 9th and 0th)
        conv2 = ConversationState()
        conv2.record(
            message_id="m0",
            user_text="列出条目",
            assistant_text="1. 首行甲项\n2. 次行乙项\n3. 末行丙项",
            status="finished",
            delivered_list=DeliveredList(
                entity="demo/items",
                source_path="/admin-api/demo/items/page",
                label="条目",
                total_rows=3,
                ordered_by_user=False,
                items=(
                    DeliveredListItem(1, "9", "首行甲项", ("9",)),
                    DeliveredListItem(2, "2", "次行乙项", ("2",)),
                    DeliveredListItem(3, "5", "末行丙项", ("5",)),
                ),
            ).to_dict(),
        )
        ex2 = make_exec(conv2)
        for bad_prompt in ("查看第 9 个的详情", "把第 0 个禁用"):
            s2 = NS(
                message=NS(text=bad_prompt),
                run_id="run-2",
                ledger=Ledger(),
                approvals=NS(mutations_enabled=True),
                terminal_reason="",
                stop_reason="",
                clarification=None,
            )
            q2 = YudaoTurnExecutor._run(ex2, s2)
            self.assertEqual(s2.terminal_reason, "needs_clarification")
            self.assertIn("共有 3 行", q2)

    def test_ordinal_read_overrides_wrong_call_api_id_and_wrong_find_record(self):
        delivered = DeliveredList(
            entity="demo/items",
            source_path="/admin-api/demo/items/page",
            label="条目",
            total_rows=3,
            ordered_by_user=False,
            items=(
                DeliveredListItem(1, "9", "首行甲项", ("9",)),
                DeliveredListItem(2, "2", "次行乙项", ("2",)),
                DeliveredListItem(3, "5", "末行丙项", ("5",)),
            ),
        ).to_dict()
        procs, client, _know, tables, _ctx = _make_procedures("查看第 2 个的详情", delivered_list=delivered)
        try:
            s = State()
            s.operation = None
            turn = {"reads": [], "marked": "", "trace": [], "touched": []}
            res_get = procs._tool(s, "call_api", {"path": "/admin-api/demo/items/get", "params": json.dumps({"id": 9})}, turn)
            self.assertEqual(res_get.state, "completed")
            self.assertIn("次行乙项", res_get.feedback)
            self.assertEqual(next(p["id"] for ep, p in client.calls if ep == "demo/items/get"), 2)
            self.assertTrue(any(ev == "list_ordinal_override" for ev, _ in s.ledger.events))

            res_find = procs._tool(s, "find_record", {"name": "首行甲项"}, turn)
            self.assertIn("次行乙项", res_find.feedback)
            self.assertEqual(sum(1 for ev, _ in s.ledger.events if ev == "list_ordinal_override"), 2)
        finally:
            tables.close()

    def test_ordinal_write_overrides_disagreeing_target_and_annotates_approval_card(self):
        schema = {
            "type": "object",
            "properties": {"target": {"type": "string"}, "remark": {"type": "string"}},
            "required": ["target", "remark"],
            "additionalProperties": False,
        }
        definition = ToolDefinition(
            name="update_record",
            description="update record",
            effect="write",
            operation=ToolOperation("demo.items", "update"),
            scope="yudao.write",
            input_json=json.dumps(schema),
            handler=_DemoWriteExecutor(),
            reference_inputs=("target",),
            literal_inputs=("remark",),
        )
        skill = SkillArtifact(
            namespace="demo-app",
            name="demo-skill",
            description="demo",
            body="update record",
            write_operations=("update_record",),
            artifact_sha256="a" * 64,
            _files=(),
        )
        registry = ToolRegistry((definition,), mutation_dispatch_enabled=True)
        skill_reg = SkillRegistry((skill,))
        runtime = BusinessToolRuntime((), registry, application_id="demo-app", skill_registry=skill_reg)
        captured_proposals = []

        def fake_approve(proposal, *, context=None):
            captured_proposals.append(proposal)
            return ApprovalGrant(
                approval_id="ap-1",
                decision="approved",
                plan_digest=proposal.content_digest,
                decided_at=time.time(),
                operation_context_digest=(context.binding_digest if context else ""),
            )

        now = int(time.time() * 1000)
        ledger = Ledger()
        principal = PrincipalContext(
            schema_version=PRINCIPAL_CONTEXT_VERSION,
            application_id="demo-app",
            tenant_id="1",
            workspace_id="1",
            actor_id="1",
            delegation_id="del-1",
            issued_at=now - 1000,
            expires_at=now + 600_000,
            scopes=("yudao.write",),
        )
        approvals = ApprovalWatch(
            ledger=ledger,
            run_id="run-w",
            principal=principal,
            session_id="sess-w",
            turn_id="turn-w",
            business_approval=fake_approve,
            cancelled=lambda: False,
        )
        approvals.start_turn(now + 600_000)
        app_ns = NS(
            business_tools=runtime,
            descriptor=NS(application_id="demo-app"),
            ports=NS(
                operations=NS(
                    skills=skill_reg,
                    tools=registry,
                    invalidate_reads=lambda _entities: None,
                    default_loaded_skill_ids=frozenset(),
                )
            ),
        )
        writes = SkillWrites(app_ns, approvals)
        writes.load(skill.skill_id, step=1)

        delivered = DeliveredList(
            entity="demo/items",
            source_path="/admin-api/demo/items/page",
            label="条目",
            total_rows=3,
            ordered_by_user=False,
            items=(
                DeliveredListItem(1, "9", "首行甲项", ("9",)),
                DeliveredListItem(2, "2", "次行乙项", ("2",)),
                DeliveredListItem(3, "5", "末行丙项", ("5",)),
            ),
        ).to_dict()
        executor = YudaoTurnExecutor.__new__(YudaoTurnExecutor)
        executor._earlier_words = ["列出条目"]
        executor.profile = NS(mutation_prefill=None)
        executor.turn_context = SlimTurnContext(
            current_request="把第 2 个的备注改为程序核验备注",
            ordinal_binding=resolve_ordinal_binding(
                parse_ordinal_reference("把第 2 个的备注改为程序核验备注"),
                DeliveredList.from_dict(delivered),
            ),
        )

        budget = NS(remaining=lambda: 60.0, check=lambda: 60.0)
        s = NS(
            message=NS(text="把第 2 个的备注改为程序核验备注"),
            obs=None,
            step=1,
            budget=budget,
            ledger=ledger,
            approvals=approvals,
            write_intent=False,
            mutation_outcomes=[],
            terminal_reason="",
            detail="",
            receipt=NS(stage=lambda _name: __import__("contextlib").nullcontext()),
        )
        turn = {"skills": {skill.skill_id}, "written": [], "corrections": 0, "tool_calls": 1}

        # Model tries to pass row 1 ("首行甲项") instead of bound row 2 ("次行乙项")!
        res = YudaoTurnExecutor._propose(
            executor,
            s,
            writes,
            "update_record",
            {"target": "首行甲项", "remark": "程序核验备注"},
            turn,
        )
        self.assertEqual(res.state, "completed")
        self.assertEqual(len(captured_proposals), 1)
        prop = captured_proposals[0]
        # Approval card target & details explicitly disclose BOTH list row position and record name/ID
        self.assertIn("列表第 2 行", prop.targets[0])
        self.assertIn("次行乙项", prop.targets[0])
        self.assertTrue(any(d.kind == "target_rule" and "列表第 2 行" in d.text and "次行乙项" in d.text for d in prop.details))
        # Dispatched ID is row 2 (id=2), NOT row 1 (id=9)
        self.assertEqual(prop.args["id"], 2)
        events = [ev for ev, _ in ledger.events]
        self.assertIn("list_ordinal_override", events)

        # Post-prepare mismatch safety gate: if the handler somehow resolved to wrong ID (9 != 2), refuse cleanly!
        executor_bad = YudaoTurnExecutor.__new__(YudaoTurnExecutor)
        executor_bad._earlier_words = ["列出条目"]
        executor_bad.profile = NS(mutation_prefill=None)
        mismatch_list = DeliveredList(
            entity="demo/items",
            source_path="/admin-api/demo/items/page",
            label="条目",
            total_rows=3,
            ordered_by_user=False,
            items=(
                DeliveredListItem(1, "99", "未知行", ("99",)),
                DeliveredListItem(2, "999", "首行甲项", ("999",)),
            ),
        )
        executor_bad.turn_context = SlimTurnContext(
            current_request="把第 2 个的备注改为二次校验备注",
            ordinal_binding=resolve_ordinal_binding(
                parse_ordinal_reference("把第 2 个的备注改为二次校验备注"),
                mismatch_list,
            ),
        )
        res_bad = YudaoTurnExecutor._propose(
            executor_bad,
            s,
            writes,
            "update_record",
            {"target": "首行甲项", "remark": "程序核验备注"},
            turn,
        )
        self.assertEqual(res_bad.state, "refused")
        self.assertIn("不一致", res_bad.feedback)
        self.assertIn("list_ordinal_mismatch_refused", [ev for ev, _ in ledger.events])

    def test_closing_records_delivered_list_from_page_table_and_text_list(self):
        executor = YudaoTurnExecutor.__new__(YudaoTurnExecutor)
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables.close)
        executor.turn_context = SlimTurnContext(
            current_request="列出当前条目",
            explicit_sort=False,
            turn_tables=(tables, "2026-10-19", "Asia/Shanghai"),
        )
        executor.knowledge = NS(handoffs=BASELINE_SESSION_HANDOFFS)
        executor.procedures = NS(form_fill=None)
        executor.config = NS(disabled_actions=frozenset(), max_user_answer_chars=4000)

        # 1. Native page observation with record_table + record_id bindings
        obs_page = NS(
            url="https://synthetic.test/demo/items",
            page_route="/demo/items",
            record_route="",
            title="条目列表",
            page_read={"path": "/admin-api/demo/items/page", "total": 6},
            elements=(
                NS(
                    ref="e5",
                    role="button",
                    name="编辑",
                    attrs={
                        "record_table": "tbl",
                        "record_id": "55",
                        "record_values": json.dumps([["名称", "甲项"]]),
                    },
                ),
                NS(
                    ref="e6",
                    role="button",
                    name="编辑",
                    attrs={
                        "record_table": "tbl",
                        "record_id": "44",
                        "record_values": json.dumps([["名称", "乙项"]]),
                    },
                ),
            ),
        )
        s_page = NS(obs=obs_page, ledger=Ledger(), delivered_list=None, mutation_outcomes=[])
        turn_page = {"reads": [], "written": [], "trace": [], "touched": []}
        args_done = {"status": "done", "answer": "已打开列表页。", "items": []}
        with patch.object(ModelDrivenTurnExecutor, "_closing", side_effect=lambda _s, _t, a: a):
            YudaoTurnExecutor._closing(executor, s_page, turn_page, args_done)
        self.assertIsNotNone(s_page.delivered_list)
        self.assertEqual([it["record_id"] for it in s_page.delivered_list["items"]], ["55", "44"])
        self.assertTrue(any(ev == "delivered_list_recorded" and data.get("source") == "page_table" for ev, data in s_page.ledger.events))

        # 2. Fallback to text list when native page has no row bindings
        obs_text = NS(url="https://synthetic.test/demo/items", page_route="/demo/items", record_route="", title="条目列表", page_read=None, elements=())
        s_text = NS(obs=obs_text, ledger=Ledger(), delivered_list=None, mutation_outcomes=[])
        args_text = {
            "status": "done",
            "answer": "第 1 页如下：\n条目甲 / 重要 / 发起人 / 2026-10-20 / 审批中\n条目乙 / 正常 / 发起人 / 2026-10-19 / 未提交",
            "items": [],
        }
        with patch.object(ModelDrivenTurnExecutor, "_closing", side_effect=lambda _s, _t, a: a):
            YudaoTurnExecutor._closing(executor, s_text, turn_page, args_text)
        self.assertIsNotNone(s_text.delivered_list)
        self.assertEqual([it["display"] for it in s_text.delivered_list["items"]], ["条目甲", "条目乙"])
        self.assertTrue(any(ev == "delivered_list_recorded" and data.get("source") == "text_list" for ev, data in s_text.ledger.events))

    def test_propose_blocks_missing_list_when_ordinal_write_reaches_propose(self):
        schema = {"type": "object", "properties": {"target": {"type": "string"}, "remark": {"type": "string"}}}
        definition = NS(name="update_record", input_json=json.dumps(schema), reference_inputs=("target",))
        writes = NS(owner_for_operation=lambda op: ("demo", definition) if op == "update_record" else None)
        executor = YudaoTurnExecutor.__new__(YudaoTurnExecutor)
        executor._earlier_words = ["查看列表"]
        executor.turn_context = SlimTurnContext(
            current_request="把第 2 个改一下",
            ordinal_binding=resolve_ordinal_binding(parse_ordinal_reference("把第 2 个改一下"), None),
        )
        s = NS(
            message=NS(text="把第 2 个改一下"),
            step=1,
            ledger=Ledger(),
        )
        res = YudaoTurnExecutor._propose(executor, s, writes, "update_record", {"target": "第 2 个", "remark": "新"}, {})
        self.assertEqual(res.state, "needs_input")
        self.assertEqual(res.error_code, "list_ordinal_missing")
        self.assertIn("list_ordinal_missing_write_blocked", [ev for ev, _ in s.ledger.events])


if __name__ == "__main__":
    unittest.main()
