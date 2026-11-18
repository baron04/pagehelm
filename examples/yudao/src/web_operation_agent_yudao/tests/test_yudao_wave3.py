"""Comprehensive unit tests for Yudao Wave 3 (ACC-05/13, ARC-04/06, UX-02/03/05)."""
from __future__ import annotations

import contextlib
from dataclasses import replace
import json
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from web_operation_agent.business.tools.contracts import BusinessMutationProposal
from web_operation_agent.execution.model_loop_contracts import PageActionOutcome, PageProcedureContext
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS, SessionHandoffPolicy
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation, TaskEvent
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.transport.panel_view import project_event, project_history_record
from web_operation_agent_yudao.executor.procedures import (
    YudaoProceduresBase,
    build_handoff_facts,
)
from web_operation_agent_yudao.executor.slim_loop import (
    YudaoLoopProcedures,
    YudaoTurnExecutor,
)
from web_operation_agent_yudao.reads.turn_tables import TurnTables
from web_operation_agent_yudao.tests.slim_loop_support import (
    Budget,
    Channel,
    Client,
    DOCS,
    Ledger,
    Script,
    State,
    call,
    element,
    observation,
    reply,
)
from web_operation_agent_yudao.tests.test_yudao_arc_wave2 import (
    _ConcurrentTestClient,
    _make_executor,
    _run_turn,
)
from web_operation_agent_yudao.writes.mutation_prefill import YudaoMutationPrefill


def _principal(scopes=("yudao.read", "yudao.write")) -> PrincipalContext:
    now = int(time.time() * 1000)
    return PrincipalContext(1, "yudao", "1", "7", "9", "synthetic", now - 1000, now + 60000, scopes)


def _op_context(scopes=("yudao.read", "yudao.write")) -> OperationContext:
    now = int(time.time() * 1000)
    return OperationContext.create(_principal(scopes), session_id="s", turn_id="t1", operation_id="o1",
                                   deadline_ms=now + 30000)


class Wave3Acc05Tests(unittest.TestCase):
    def test_settled_click_select_goto_consumes_observation_and_summary_skips_settled(self):
        proc = YudaoLoopProcedures.__new__(YudaoLoopProcedures)
        proc.knowledge = NS(handoffs=SessionHandoffPolicy.extending(()))
        proc.goal_port = NS(review=None)
        proc.application = NS(descriptor=NS(origin="http://synthetic.invalid"))
        settled_calls = []
        proc._settled = lambda *a, **kw: settled_calls.append((a, kw))

        next_obs = PageObservation(
            1, 2, "http://synthetic.invalid/orders", "Orders", [],
            [ElementRef("e1", "combobox", "状态", "input", 2, {"form_scope": "dialog:create", "form_instance": "i1", "selected_option": "已审核"})],
            content="[1]<input /> 状态", render_state="ready", page_route="/orders", active_view="已审核",
        )
        rcpt = ActionReceipt(
            1, "cmd-1", "select", {"ref": "e1", "value": "已审核", "option_label": "已审核"},
            True, observation=next_obs,
            observation_summary={"url_changed": False, "epoch_changed": True, "active_view": "已审核", "row_count": 3},
        )
        # Fake outcome object holding real receipt instance with observation + observation_summary
        fake_outcome = NS(state="completed", dispatch="acknowledged", error_code="", receipt=rcpt)
        state = NS(
            obs=PageObservation(1, 1, "http://synthetic.invalid/orders", "Orders", [],
                                [ElementRef("e1", "combobox", "状态", "input", 1,
                                            {"form_scope": "dialog:create", "form_instance": "i1",
                                             "aria-haspopup": "listbox", "aria-controls": "owned"})],
                                content="[1]<input /> 状态", render_state="ready", page_route="/orders"),
            page=NS(execute=lambda action, args, navigating=False: fake_outcome),
            operation=_op_context(),
            audit=lambda *a, **kw: None,
            terminal_reason="",
        )
        turn = {"reads": [], "marked": "", "trace": [], "touched": []}
        res = proc._tool(state, "select", {"index": 1, "option": "已审核"}, turn)
        self.assertEqual(res.state, "completed")
        self.assertEqual(settled_calls, [])  # skipped _settled because settled receipt brought observation
        self.assertEqual(state.obs.epoch, 2)
        self.assertIn("当前视图=已审核", res.feedback)


class Wave3Acc13Ux03Ux05Tests(unittest.TestCase):
    def test_general_mappable_offer_prefill_and_handoff_facts_and_restricted_controls(self):
        class _Review:
            def permits(self, kind, path, control=""):
                return path == "/erp/supplier" and kind in {"form_entry", "fields"}

            def labels_on(self, path):
                return {"name": "供应商名称", "mobile": "手机", "status": "开启状态"}

        ctx = _op_context()
        obs_closed = PageObservation(
            1, 1, "http://synthetic.invalid/erp/supplier", "Supplier", [],
            [ElementRef("e1", "button", "新增", "button", 1, {})],
            "synthetic", render_state="ready", page_route="/erp/supplier",
        )
        proposal = BusinessMutationProposal(
            "erp_supplier_create",
            {
                "entity": "erp/supplier",
                "operation": "erp_supplier_create",
                "actor": "9",
                "before": {},
                "choices": [],
                "relations": [],
                "body": {"name": "合成供应", "mobile": "13800001111", "status": 0},
            },
            "新建ERP 供应商资料",
            ("合成供应",),
            "2" * 32,
            operation_context_digest=ctx.binding_digest,
        )
        prefill = YudaoMutationPrefill(NS(get=lambda *a, **kw: None), object(), _Review())
        offer = prefill.offer(proposal, context=ctx, observation=obs_closed)
        self.assertIsNotNone(offer)
        self.assertEqual(offer.target, "/erp/supplier#erp/supplier")
        self.assertEqual(dict(offer.fields), {"name": "合成供应", "mobile": "13800001111", "status": "开启"})

        # Now test handoff with one restricted control (password) + two editable fields
        dialog_obs = PageObservation(
            1, 2, "http://synthetic.invalid/erp/supplier", "Supplier", [],
            [
                ElementRef("e2", "textbox", "", "input", 2, {"form_scope": "dialog:1", "form_instance": "i1", "field_label": "供应商名称", "value": "旧名"}),
                ElementRef("e3", "textbox", "", "input", 2, {"form_scope": "dialog:1", "form_instance": "i1", "field_label": "手机", "value": ""}),
                ElementRef("e4", "combobox", "", "input", 2, {"form_scope": "dialog:1", "form_instance": "i1", "field_label": "开启状态", "selected_option": "开启"}),
                ElementRef("e5", "textbox", "", "input", 2, {"form_scope": "dialog:1", "form_instance": "i1", "field_label": "API密钥密码", "input_type": "password"}),
                ElementRef("e6", "button", "确定", "button", 2, {"form_scope": "dialog:1", "form_instance": "i1"}),
            ],
            "synthetic", render_state="ready", page_route="/erp/supplier",
        )
        state = NS(obs=dialog_obs)
        handoff_ctx = None

        def page_exec(action, args, navigating=False):
            if action == "click":
                state.obs = dialog_obs
                handoff_ctx.obs = dialog_obs
                return NS(state="completed", dispatch="acknowledged", error_code="",
                          receipt=ActionReceipt(1, f"c-{action}", action, args, True, observation=dialog_obs))
            if action in {"fill", "select"}:
                updated_els = []
                for el in handoff_ctx.obs.elements:
                    if el.ref == args["ref"]:
                        new_attrs = dict(el.attrs)
                        if action == "select":
                            new_attrs["selected_option"] = args["option_label"]
                        else:
                            new_attrs["value"] = args["value"]
                        updated_els.append(replace(el, attrs=new_attrs))
                    else:
                        updated_els.append(el)
                handoff_ctx.obs = replace(handoff_ctx.obs, elements=updated_els)
                return NS(state="completed", dispatch="acknowledged", error_code="",
                          receipt=ActionReceipt(1, f"c-{args['ref']}", action, args, True, observation=handoff_ctx.obs))
            if action == "handoff_form":
                return NS(state="completed", dispatch="acknowledged", error_code="",
                          receipt=ActionReceipt(1, "c-handoff", action, args, True, observation=handoff_ctx.obs))
            raise AssertionError(action)

        handoff_ctx = NS(
            operation=ctx,
            page=NS(
                execute=page_exec,
                checkpoint=lambda: None,
                handoff=lambda fields, submit: NS(
                    state="completed",
                    dispatch="acknowledged",
                    error_code="",
                    receipt=ActionReceipt(1, "c-handoff", "handoff_form", {"fields": fields, "submit": submit}, True, observation=handoff_ctx.obs),
                ),
            ),
            audit=lambda *a, **kw: None,
            obs=dialog_obs,
        )
        # Note: hook property-like access via SimpleNamespace keeps obs
        res = prefill.handoff(offer, proposal, context=handoff_ctx)
        self.assertEqual(res.state, "completed")
        self.assertEqual(res.marked, "确定")
        self.assertIsNotNone(prefill.last_handoff_facts)
        self.assertEqual(
            {f["name"] for f in prefill.last_handoff_facts["completed_fields"]},
            {"供应商名称", "手机", "开启状态"},
        )
        self.assertTrue(prefill.last_handoff_facts["can_undo"])
        # Restricted password control also surfaces cleanly via build_handoff_facts
        facts = build_handoff_facts({"field_outcomes": {
            "applied_fields": {"供应商名称": "合成供应"},
            "rejected_fields": {},
            "previous_fields": {"供应商名称": "旧名"},
            "restricted_fields": ["API密钥密码"],
        }}, dialog_obs)
        self.assertEqual(facts["restricted_controls"][0]["kind"], "password")
        self.assertIn({"name": "API密钥密码"}, facts["pending_fields"])


class Wave3Arc04Arc06Ux02Tests(unittest.TestCase):
    def test_turn_tables_q_handles_and_export_table_large_rows_and_finish_delivery(self):
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables.close)
        rows = [{"id": i, "name": f"客户{i}", "amount": i * 10, "password": "secret"} for i in range(1, 225)]
        tables.add(path="crm/customer/page", params={}, rows=rows, total=224, complete=True)
        q_res = tables.query("SELECT id, name, amount FROM t1 ORDER BY id ASC")
        self.assertEqual(q_res["table_ref"], "q1")
        self.assertEqual(q_res["returned_rows"], 200)
        self.assertTrue(q_res["truncated"])
        self.assertEqual(tables.describe()["queries"][0]["name"], "q1")

        # export_table returns full 224 rows (bounded up to EXPORT_MAX_ROWS=2000), not capped at 200
        exported = tables.export_table("q1")
        self.assertIsNotNone(exported)
        self.assertEqual(exported["columns"], ["id", "name", "amount"])
        self.assertEqual(exported["total_rows"], 224)
        self.assertEqual(len(exported["rows"]), 224)
        from web_operation_agent_yudao.reads.turn_tables import TurnReadError
        self.assertEqual(exported["rows"][223], ["224", "客户224", "2240"])
        with self.assertRaises(TurnReadError):
            tables.export_table("q99")

        # End-to-end finish(..., table_ref="q1") through YudaoTurnExecutor -> terminal progress payload.table + TurnTrace
        client = _ConcurrentTestClient()
        with tempfile.TemporaryDirectory() as tmp:
            ex, ledger, principal, _ = _make_executor(client, replies=[], ledger_dir=tmp)
            published = []
            orig_pub = ex.channel.publish
            ex.channel.publish = lambda kind, payload, **kw: (published.append((kind, payload)) or orig_pub(kind, payload, **kw))

            step_counter = {"n": 0}

            def dynamic_chat(messages, tools, run_id, **_kw):
                idx = step_counter["n"]
                step_counter["n"] += 1
                if idx == 0:
                    return reply(call("call_api", path="orders/page", params='{"pageNo": 1, "pageSize": 10}'))
                if idx == 1:
                    return reply(call("sql", query="SELECT no, price FROM t1 ORDER BY no"))
                receipts = sorted(ex._ctx().turn_tables[0].receipts)
                return reply(call("finish", status="done", answer="共 2 条订单。", table_ref="q1", items=[
                    {"item": "订单表", "kind": "data", "status": "delivered", "receipts": [receipts[-1]]},
                ]))

            ex.chat.complete_cancellable = dynamic_chat
            ex.channel.session_id = "s"
            self.assertIsNone(ex.export_table("q99"))
            run = _run_turn(ex, "把订单表格查出来给我", ledger, principal)
            run["state"].writes.end_control = lambda: contextlib.nullcontext()
            run["state"].approvals.operation_result = lambda: {}
            ex._settle(run["state"], None, run["outcome"])
        progress_tables = [p.get("table") for k, p in published if k == "progress" and isinstance(p, dict) and "table" in p]
        self.assertEqual(len(progress_tables), 1)
        self.assertEqual(progress_tables[0]["columns"], ["no", "price"])
        self.assertEqual(progress_tables[0]["total_rows"], 2)
        self.assertEqual(len(progress_tables[0]["rows"]), 2)
        self.assertEqual(run["trace"]._deliveries[0]["source_reference"], "q1")
        self.assertEqual(run["trace"]._deliveries[0]["presented_records"], 2)

    def test_arc04_read_page_emits_steps_and_ux02_structured_options_on_need_input(self):
        # ARC-04: multi-page read emits read_page stage
        client = _ConcurrentTestClient()
        published_steps = []

        class MultiPageClient(_ConcurrentTestClient):
            def get(self, path, **kwargs):
                if path == "orders/page":
                    page = int(kwargs.get("pageNo", 1))
                    if page == 1:
                        return {"list": [{"id": 1, "no": "SO1", "price": 100, "remark": "a"},
                                         {"id": 2, "no": "SO2", "price": 200, "remark": "b"}], "total": 3}
                    return {"list": [{"id": 3, "no": "SO3", "price": 300, "remark": "c"}], "total": 3}
                return super().get(path, **kwargs)

        mclient = MultiPageClient()
        ex, ledger, principal, _ = _make_executor(
            mclient,
            replies=[
                reply(call("call_api", path="orders/page", params='{"pageNo": 1, "pageSize": 2}', fields=["no", "price"])),
                reply(call("finish", status="cannot", answer="读取完成", items=[
                    {"item": "订单", "kind": "data", "status": "undelivered", "receipts": []},
                ])),
            ],
        )
        published_step_payloads = []
        orig_pub = ex.channel.publish
        ex.channel.publish = lambda kind, payload, **kw: (
            published_step_payloads.append(dict(payload))
            if kind == "progress" and isinstance(payload, dict) and "step" in payload
            else None,
            orig_pub(kind, payload, **kw),
        )[1]
        _run_turn(ex, "汇总所有订单", ledger, principal)
        published_steps = [p.get("step") for p in published_step_payloads]
        self.assertIn({"stage": "read_page"}, published_steps)
        read_page_payload = next(
            p for p in published_step_payloads if p.get("step") == {"stage": "read_page"}
        )
        self.assertEqual(read_page_payload.get("state"), "step")
        self.assertEqual(read_page_payload.get("message"), "正在翻页查看（第 2 页）")
        projected_live = project_event(
            TaskEvent(1, "e00000001", "s-1", 1, "progress", "t-1", read_page_payload),
            at=1,
        )
        self.assertEqual(projected_live["kind"], "progress")
        self.assertEqual(projected_live["text"], "正在翻页查看（第 2 页）")
        self.assertEqual(projected_live["step"], {"stage": "read_page"})
        projected_hist = project_history_record(
            {"kind": "progress", "turn_id": "t-1", "at": 1, "payload": read_page_payload},
            index=1,
        )
        self.assertEqual(projected_hist["kind"], "progress")
        self.assertEqual(projected_hist["text"], "正在翻页查看（第 2 页）")
        self.assertEqual(projected_hist["panel_state"], "acting")

        # UX-02: need_input with 2..4 structured options emits input_options + clarification.choices
        published_ux = []
        ux_exec, ux_ledger, ux_principal, _ = _make_executor(
            client,
            replies=[
                reply(call(
                    "finish",
                    status="need_input",
                    answer="有两位同名客户，请选择你要查看的那位：",
                    options=[
                        {"id": "c1", "label": "星河科技（#101）", "prompt": "查看编号 101 的星河科技"},
                        {"id": "c2", "label": "星河科技（#202）", "prompt": "查看编号 202 的星河科技"},
                    ],
                    items=[{"item": "客户详情", "kind": "data", "status": "undelivered", "receipts": []}],
                )),
            ],
        )
        orig_ux_pub = ux_exec.channel.publish
        ux_exec.channel.publish = lambda kind, payload, **kw: (published_ux.append((kind, payload)) or orig_ux_pub(kind, payload, **kw))
        ux_exec.channel.session_id = "s"
        ux_run = _run_turn(ux_exec, "查看星河科技详情", ux_ledger, ux_principal)
        ux_run["state"].writes.end_control = lambda: contextlib.nullcontext()
        ux_run["state"].approvals.operation_result = lambda: {}
        ux_exec._settle(ux_run["state"], None, ux_run["outcome"])
        self.assertEqual(ux_run["state"].terminal_reason, "needs_clarification")
        self.assertEqual(len(ux_run["state"].input_options), 2)
        terminal_payloads = [p for k, p in published_ux if k == "progress" and isinstance(p, dict) and "terminal_reason" in p]
        self.assertEqual(len(terminal_payloads[-1]["input_options"]), 2)
        self.assertEqual(terminal_payloads[-1]["input_options"][0]["id"], "c1")

    def test_ux05_restricted_file_kind_and_ux03_radiogroup_select_in_prefill(self):
        from web_operation_agent_yudao.writes.mutation_prefill import (
            YudaoMutationPrefill, _attach_handoff_facts,
        )

        ctx = _op_context()
        facts = _attach_handoff_facts(
            ctx,
            applied_fields={"客户名称": "星河"},
            rejected_fields={},
            restricted_controls=["登录密码", "附件上传"],
            pending_fields=[],
            restricted_refs={"登录密码": "e9", "附件上传": "e10"},
        )
        self.assertEqual(
            facts["restricted_controls"],
            [
                {"label": "登录密码", "kind": "password", "ref": "e9"},
                {"label": "附件上传", "kind": "file", "ref": "e10"},
            ],
        )

        class _Review:
            def permits(self, kind, path, control=""):
                return path == "/erp/supplier" and kind in {"form_entry", "fields"}
            def labels_on(self, path):
                return {"status": "开启状态"}

        dialog_obs = PageObservation(
            1, 2, "http://synthetic.invalid/erp/supplier", "Supplier", [],
            [
                ElementRef("e1", "radiogroup", "开启状态", "div", 2, {
                    "form_scope": "dialog:1",
                    "form_instance": "i1",
                    "field_label": "开启状态",
                    "selected_option": "",
                    "radio_options": json.dumps([{"label": "开启", "value": "0"}, {"label": "关闭", "value": "1"}], ensure_ascii=False),
                }),
                ElementRef("e2", "textbox", "", "input", 2, {"form_scope": "dialog:1", "form_instance": "i1", "field_label": "附件上传", "input_type": "file"}),
                ElementRef("e3", "button", "确定", "button", 2, {"form_scope": "dialog:1", "form_instance": "i1"}),
            ],
            "synthetic", render_state="ready", page_route="/erp/supplier",
        )
        proposal = BusinessMutationProposal(
            "erp_supplier_create",
            {"entity": "erp/supplier", "operation": "erp_supplier_create", "actor": "9",
             "before": {}, "choices": [], "relations": [], "body": {"status": 0}},
            "新建ERP 供应商资料", ("开启",), "3" * 32,
            operation_context_digest=ctx.binding_digest,
        )
        prefill = YudaoMutationPrefill(NS(get=lambda *a, **kw: None), object(), _Review())
        offer = prefill.offer(proposal, context=ctx, observation=dialog_obs)
        self.assertIsNotNone(offer)

        executed = []
        class _Page:
            def checkpoint(self):
                return None

            def execute(self, action, args, **_kw):
                executed.append((action, dict(args)))
                if action == "select":
                    updated = [
                        replace(el, attrs={**el.attrs, "selected_option": args["option_label"]})
                        if el.ref == args["ref"] else el
                        for el in hctx.obs.elements
                    ]
                    hctx.obs = replace(hctx.obs, elements=updated)
                return NS(state="completed", dispatch="acknowledged", error_code="",
                          receipt=ActionReceipt(1, f"c-{action}", action, args, True, observation=hctx.obs))

            def handoff(self, fields, submit):
                return NS(
                    state="completed",
                    dispatch="acknowledged",
                    error_code="",
                    receipt=ActionReceipt(1, "c-handoff", "handoff_form", {"fields": fields, "submit": submit}, True, observation=hctx.obs),
                )

        hctx = NS(
            obs=dialog_obs,
            page=_Page(),
            binding_digest=ctx.binding_digest,
            operation=ctx,
            audit=lambda *a, **kw: None,
        )
        res = prefill.handoff(offer, proposal, context=hctx)
        self.assertEqual(res.state, "completed")
        self.assertEqual(executed[0][0], "select")
        self.assertEqual(executed[0][1]["ref"], "e1")


if __name__ == "__main__":
    unittest.main()
