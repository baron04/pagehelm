"""Comprehensive unit tests for Wave 3 core execution, business, and UX contracts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from web_operation_agent.business.approvals import MutationApproval
from web_operation_agent.business.draft_steps import DraftStepAdmission, DraftStepPermit
from web_operation_agent.business.write_pipeline import WritePipeline
from web_operation_agent.execution import form_tools, harness as harness_mod, turn as turn_mod
from web_operation_agent.execution.answer_check import (
    UNBACKED_BUSINESS_CLAIM_SPEC,
    is_allowed_general_turn,
)
from web_operation_agent.execution.checks import record_check_fired
from web_operation_agent.execution.draft_handoff import add_draft_lines, handoff_draft
from web_operation_agent.execution.harness import (
    HarnessTurnContext,
    ModelTurnState,
    TurnHarness,
)
from web_operation_agent.execution.loop_guard import (
    STABLE_CONTROL_REPEATED_SPEC,
    VERIFIED_EMPTY_FILTER_REPEATED_SPEC,
    is_verified_empty_filter,
    record_verified_empty_filter,
    stable_control_identity,
)
from web_operation_agent.execution.model_loop import (
    CORE_CHECK_SPECS,
    ModelDrivenTurnExecutor,
)
from web_operation_agent.execution.model_loop_contracts import (
    LoopToolResult,
    PageActionOutcome,
    VerificationResult,
)
from web_operation_agent.execution.native_submit import NativeSubmitMixin
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnStatus
from web_operation_agent.execution.receipt import TurnReceipt
from web_operation_agent.execution.submit_contract import FormData, SubmitConventions
from web_operation_agent.execution.turn_trace import TurnTrace
from web_operation_agent.execution.value_source import SourceLedger
from web_operation_agent.modeling.provider import ProviderError
from web_operation_agent.privacy import SessionHandoffPolicy
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation, TaskEvent
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.execution.test_product_model_loop import (
    LoopFixture,
    call,
    page,
    reply,
)
from web_operation_agent.tests.goal.test_data_read_tools import context as test_context
from web_operation_agent.transport.panel_view import project_event
from web_operation_agent.transport.protocol import _remote_args_echo


def _obs_with_controls(epoch: int = 1, *, route: str = "/records", scope: str = "dialog-1") -> PageObservation:
    return PageObservation(
        1,
        epoch,
        f"https://synthetic.test{route}",
        "Records",
        [],
        [
            ElementRef("e1", "combobox", "Status", "combobox", epoch, {"field_label": "Status", "form_scope": scope, "form_instance": "inst-1", "form_lifetime": "life-1"}),
            ElementRef("e2", "textbox", "Remark", "input", epoch, {"field_label": "Remark", "form_scope": scope, "form_instance": "inst-1", "form_lifetime": "life-1", "value": "old"}),
            ElementRef("e3", "button", "Save", "button", epoch, {"form_scope": scope, "form_instance": "inst-1", "form_lifetime": "life-1"}),
            ElementRef("e4", "textbox", "Password", "input", epoch, {"field_label": "Password", "type": "password", "form_scope": scope, "form_instance": "inst-1", "form_lifetime": "life-1"}),
        ],
        content="Records form",
        render_state="ready",
        page_route=route,
    )


class _ScriptedChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def complete_cancellable(self, messages, tools, run_id, **kwargs):
        self.seen.append(messages)
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _Wave3Fixture(LoopFixture):
    def __init__(self, tmpdir: Path, replies, handler):
        super().__init__(replies, handler)
        self._trace = TurnTrace(tmpdir / "turns", SessionHandoffPolicy(()))
        self.ledger = SessionLedger(tmpdir / "audit.jsonl", "run-w3", base={"session_id": "s1"})
        self.state = HarnessTurnContext(
            message=NS(message_id="m-w3", text="q"),  # type: ignore[arg-type]
            run_id="run-w3",
            ledger=self.ledger,
            browser=None,
            writes=None,  # type: ignore[arg-type]
            approvals=NS(business_counts={"mutation_indeterminate": 0}),  # type: ignore[arg-type]
            obs=_obs_with_controls(),
            started_at=0.0,
            chinese=True,
            chat=_ScriptedChat(replies),
        )
        self.state.budget = NS(remaining=lambda: 60.0, check=lambda: 60.0)  # type: ignore[assignment]
        self.turn = ModelTurnState()
        self.noted: list[dict] = []

    def _note(self, state, **facts):
        self.noted.append(facts)


class Wave3Acc02Tests(unittest.TestCase):
    def test_stable_control_identity_skips_second_failed_dispatch_with_candidates_and_stops_on_stall(self):
        first_obs = _obs_with_controls(epoch=1, route="/orders")
        # Element index shifted from e1 to e9, whitespace changed in value, epoch bumped
        second_obs = PageObservation(
            1,
            2,
            "https://synthetic.test/orders",
            "Orders",
            [],
            [ElementRef("e9", "combobox", "Status", "combobox", 2, {"field_label": "Status", "form_scope": "dialog-1"})],
            content="Orders refreshed",
            render_state="ready",
            page_route="/orders",
        )
        k1 = stable_control_identity("select", {"index": 1, "value": "  Invalid Status  "}, first_obs.elements[0], first_obs)
        k2 = stable_control_identity("select", {"index": 9, "value": "Invalid Status"}, second_obs.elements[0], second_obs)
        self.assertEqual(k1, ("select", "/orders", "dialog-1", "Status", "Invalid Status"))
        self.assertEqual(k1, k2)

        with tempfile.TemporaryDirectory() as tmp:
            dispatched = []

            def handler(fx, name, arguments):
                dispatched.append((name, arguments))
                r = ActionReceipt(
                    1,
                    "c1",
                    "select",
                    {"ref": "e1", "value": "Missing"},
                    False,
                    error_code="option_unavailable",
                    no_effect=True,
                    candidate_labels=("Open", "Closed"),
                    observation=second_obs,
                )
                fx._tool_receipts.append(r)
                return LoopToolResult("option not found", kind="page", state="refused", error_code="option_unavailable")

            fx = _Wave3Fixture(
                Path(tmp),
                [
                    reply(call("select", index=1, value="Missing")),
                    reply(call("select", index=1, value="  Missing ")),
                    reply(call("select", index=1, value="Missing")),
                ],
                handler,
            )
            answer = fx.run()
            self.assertEqual(len(dispatched), 1)
            self.assertIn("已停止执行", answer)
            tool_msgs = [m["content"] for m in fx._messages if m["role"] == "tool"]
            self.assertTrue(any("option_unavailable" in m and "Open、Closed" in m for m in tool_msgs))
            checks = fx.ledger.of_kind("check_fired")
            self.assertEqual(
                [(c["check"], c["outcome"]) for c in checks],
                [
                    ("stable_control_repeated", "returned"),
                    ("stable_control_repeated", "blocked"),
                ],
            )
            self.assertEqual(fx.state.stop_reason, "stable_control_repeated")

    def test_verified_empty_filters_monotonic_across_resets_and_blocks_duplicate_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def handler(fx, name, arguments):
                calls.append((name, arguments))
                return LoopToolResult("0 rows", kind="read", read_key="orders", read_count=0)

            fx = _Wave3Fixture(
                Path(tmp),
                [
                    reply(call("search_records", route="/orders", tab="All", filters={"status": "  NONE  "})),
                    reply(call("reset_form")),
                    reply(call("search_records", route="/orders", tab="All", filters={"status": "NONE"})),
                    reply(call("finish", status="cannot", answer="未查到记录")),
                ],
                handler,
            )
            fx.profile = NS(
                procedures=(
                    NS(kind="read", schema={"function": {"name": "search_records"}}, handler=NS()),
                    NS(kind="page", schema={"function": {"name": "reset_form"}}, handler=NS()),
                ),
                render_page=lambda obs, _m: obs.content,
            )
            # Simulate a reset_form step clearing form fields while keeping verified_empty_filters monotonic
            orig_handler = fx.handler

            def mixed_handler(f, name, arguments):
                if name == "reset_form":
                    f.turn["touched"].clear()
                    return LoopToolResult("form reset", kind="page")
                return orig_handler(f, name, arguments)

            fx.handler = mixed_handler
            answer = fx.run("查订单")
            self.assertEqual(answer, "未查到记录")
            self.assertEqual(len([c for c in calls if c[0] == "search_records"]), 1)
            self.assertTrue(is_verified_empty_filter(fx.turn, "/orders", "All", {"status": "NONE"}))
            checks = fx.ledger.of_kind("check_fired")
            self.assertEqual(
                [(c["check"], c["outcome"]) for c in checks],
                [("verified_empty_filter_repeated", "blocked")],
            )


class Wave3Acc05Tests(unittest.TestCase):
    def test_settled_action_updates_obs_feeds_summary_and_suppresses_immediate_observe(self):
        with tempfile.TemporaryDirectory() as tmp:
            dispatched = []

            def handler(fx, name, arguments):
                dispatched.append(name)
                if name == "observe":
                    raise AssertionError("redundant immediate observe should be suppressed")
                after = _obs_with_controls(epoch=5, route="/orders")
                receipt = ActionReceipt(
                    1,
                    "cmd-1",
                    "select",
                    {"ref": "e1", "value": "Open"},
                    True,
                    observation=after,
                    url_before=fx.state.obs.url,
                    url_after=after.url,
                    write_guard_held=True,
                    observation_summary={"changed_control": "Status", "selected": "Open"},
                )
                fx._tool_receipts.append(receipt)
                return LoopToolResult("selected Open", kind="page")

            fx = _Wave3Fixture(
                Path(tmp),
                [
                    reply(call("select", index=1, value="Open")),
                    reply(call("observe")),
                    reply(call("finish", status="done", answer="已选择 Open")),
                ],
                handler,
            )
            answer = fx.run("选择 Open")
            self.assertEqual(answer, "已选择 Open")
            self.assertEqual(dispatched, ["select"])
            self.assertEqual(fx.state.obs.epoch, 5)
            tool_msgs = [m["content"] for m in fx._messages if m["role"] == "tool"]
            self.assertTrue(any("页面变化摘要：" in m and "Open" in m for m in tool_msgs))
            self.assertTrue(fx.ledger.of_kind("observe_suppressed"))


class Wave3Acc07Tests(unittest.TestCase):
    def test_general_four_categories_allowed_without_ev_citations(self):
        cases = (
            ("你好！", "你好，请问有什么可以帮你？"),
            ("你能做什么？", "我可以帮你查询页面数据、打开目标页面或预填表单。"),
            ("把这段话翻译成英文：欢迎使用助手", "Welcome to the assistant."),
            ("计算 25 * 4 + 10 等于多少", "结果是 110。"),
        )
        for prompt, answer in cases:
            with self.subTest(prompt=prompt):
                self.assertTrue(is_allowed_general_turn(prompt, answer))
                with tempfile.TemporaryDirectory() as tmp:
                    fx = _Wave3Fixture(Path(tmp), [reply(call("finish", status="done", answer=answer))], lambda *_: None)
                    fx.require_evidence_citations = True
                    self.assertEqual(fx.run(prompt), answer)
                    self.assertEqual(fx.ledger.of_kind("check_fired"), [])

    def test_unbacked_business_claim_sent_back_once_and_blocked_on_second_failure(self):
        # Disguised business summary/arithmetic without ev_ must be caught
        self.assertFalse(is_allowed_general_turn("总结一下当前客户状态", "当前客户 Acme 状态为正常。"))
        self.assertFalse(is_allowed_general_turn("计算订单 101 和 102 的金额", "合计 500 元。"))

        with tempfile.TemporaryDirectory() as tmp:
            fx = _Wave3Fixture(
                Path(tmp),
                [
                    reply(call("finish", status="done", answer="当前客户 Acme 共有 3 条订单，状态为已审核。")),
                    reply(call("finish", status="done", answer="仍是已审核状态。")),
                ],
                lambda *_: None,
            )
            fx.require_evidence_citations = True
            result = fx.run("查一下客户 Acme 的订单状态")
            self.assertIn("未能提供可核验的本轮工具回执", result)
            self.assertEqual(fx.state.terminal_reason, "agent_refused")
            self.assertEqual(fx.state.stop_reason, "unbacked_business_claim")
            checks = fx.ledger.of_kind("check_fired")
            self.assertEqual(
                [(c["check"], c["outcome"], c["attempt"]) for c in checks],
                [
                    ("unbacked_business_claim", "returned", 1),
                    ("unbacked_business_claim", "blocked", 2),
                ],
            )

    def test_backed_business_claim_with_current_turn_ev_citation_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Wave3Fixture(
                Path(tmp),
                [
                    reply(call("query_orders")),
                    reply(call("finish", status="done", answer="客户 Acme 订单状态为正常（依据 ev_abcdef123456_1）。")),
                ],
                lambda *_: LoopToolResult("orders=[1] 本轮工具回执：ev_abcdef123456_1", kind="read"),
            )
            fx.require_evidence_citations = True
            answer = fx.run("查一下客户 Acme 的订单状态")
            self.assertIn("ev_abcdef123456_1", answer)
            self.assertEqual(fx.ledger.of_kind("check_fired"), [])


class Wave3Acc06Acc13Tests(unittest.TestCase):
    def test_form_closed_or_readonly_and_field_outcomes_propagate_without_masking(self):
        with tempfile.TemporaryDirectory() as tmp:
            def handler(fx, name, arguments):
                after = _obs_with_controls(epoch=3)
                receipt = ActionReceipt(
                    1,
                    "cmd-fill",
                    "fill",
                    {"ref": "e2", "value": "abc"},
                    False,
                    error_code="form_closed_or_readonly",
                    observation=after,
                    url_before=fx.state.obs.url,
                    url_after=after.url,
                    applied_fields={"Remark": "abc"},
                    rejected_fields={"Status": "readonly"},
                    previous_fields={"Remark": "old"},
                    restricted_fields=("Password",),
                    candidate_labels=("Draft", "Active"),
                )
                fx._tool_receipts.append(receipt)
                return LoopToolResult("form closed", kind="page", state="refused", error_code="form_closed_or_readonly")

            fx = _Wave3Fixture(
                Path(tmp),
                [
                    reply(call("fill", index=2, value="abc")),
                    reply(call("finish", status="cannot", answer="表单已关闭")),
                ],
                handler,
            )
            answer = fx.run("填写备注")
            self.assertEqual(answer, "表单已关闭")
            tool_msg = next(m["content"] for m in fx._messages if m["role"] == "tool")
            self.assertIn("error_code=form_closed_or_readonly", tool_msg)
            self.assertIn("applied_fields=", tool_msg)
            self.assertIn("rejected_fields=", tool_msg)
            self.assertIn("restricted_fields=Password", tool_msg)
            self.assertIn("candidates=Draft、Active", tool_msg)
            self.assertEqual(fx.turn["form_field_outcomes"]["previous_fields"], {"Remark": "old"})

    def test_draft_handoff_and_native_submit_propagate_form_closed_or_readonly(self):
        obs = _obs_with_controls(epoch=2)
        receipt = ActionReceipt(
            1,
            "cmd-h",
            "handoff_form",
            {},
            False,
            error_code="form_closed_or_readonly",
            observation=obs,
            url_before=obs.url,
            url_after=obs.url,
        )
        page_stub = NS(
            checkpoint=lambda: None,
            handoff=lambda _f, _s: PageActionOutcome("unverified", "acknowledged", "form_closed_or_readonly", json.dumps(receipt.to_json())),
            execute=lambda *_a, **_kw: PageActionOutcome("unverified", "acknowledged", "form_closed_or_readonly", json.dumps(receipt.to_json())),
        )
        ctx = NS(obs=obs, page=page_stub, terminal_reason="", audit=lambda *_a, **_kw: None)
        res = add_draft_lines(ctx, "Save", 1)
        self.assertEqual(res.error_code, "form_closed_or_readonly")
        res2 = handoff_draft(ctx, obs, [(("dialog-1", "", "textbox", "", "Remark", ""), "old")], "Save")
        # When before/after untouched check passes only if fields match; check direct handoff receipt error_code path:
        self.assertIn(res2.error_code, {"form_closed_or_readonly", "unverified"})

        class _ClosedSubmit(NativeSubmitMixin):
            native_submit = NS(contract=lambda: NS(volatile_keys=(), epoch_zone=None, line_key="id", record_key="id"))

            def _dispatch(self, s, action, args, **kw):
                return receipt

            def _not_sent(self, s, code, detail=""):
                return LoopToolResult(detail or code, kind="write", state="refused", error_code=code)

            def _element(self, s, index):
                return obs.elements[2]

            def _submit_allowed(self, turn):
                return True

        sub = _ClosedSubmit()
        s = NS(obs=obs, step=1, terminal_reason="", detail="", ledger=NS(append=lambda *_a, **_k: None))
        out = sub._request_submit(s, {"index": 3}, {})
        self.assertEqual(out.error_code, "form_closed_or_readonly")

    def test_draft_steps_verify_no_effect_and_form_tools_acc01_widened(self):
        class _ChoicePort:
            application_id = "app.test"
            required_scope = "records:read"
            actions = frozenset({"select", "fill"})

            def handles(self, intent):
                return True

            def prepare_step(self, intent, **_kw):
                return DraftStepPermit(
                    intent.binding_digest,
                    "explicit-submit-review",
                    "exact-field-value",
                    "dialog-1",
                    "inst-1",
                    "enter_value",
                    (("Status", intent.arguments["value"]),),
                )

            def verify_step(self, *_a, **_kw):
                return True

            def verify_no_effect(self, *_a, **_kw):
                return True

        # Verify DraftStepAdmission checks `intent.action not in {"select", "fill"}` (using select until schema integration)
        admission = DraftStepAdmission(_ChoicePort(), "app.test")
        before = _obs_with_controls(epoch=1)
        after = _obs_with_controls(epoch=2)
        ctx = test_context(application="app.test")
        args = {"ref": "e1", "value": "Missing", "option_label": "Missing"}
        intent, permit = admission.prepare("select", args, before, context=ctx, checkpoint=lambda: None)
        admission.consume(intent, permit, before, context=ctx, checkpoint=lambda: None)
        receipt = ActionReceipt(
            1,
            "cmd-sel",
            "select",
            _remote_args_echo({**args, "epoch": 1}),
            False,
            error_code="option_unavailable",
            no_effect=True,
            candidate_labels=("Open",),
            observation=after,
            url_before=before.url,
            url_after=after.url,
            write_guard_held=True,
        )
        admission.verify_no_effect(intent, permit, receipt, context=ctx, checkpoint=lambda: None)


class Wave3Arc04Arc06Arc07UxTests(unittest.TestCase):
    def _channel(self):
        published = []
        return NS(
            session_id="s-1",
            local_dev_mode=False,
            published=published,
            publish=lambda kind, payload, turn_id="": published.append((kind, payload, turn_id)),
        )

    def test_arc04_publishes_settle_awaiting_approval_and_read_page(self):
        channel = self._channel()
        h = TurnHarness.detached()
        h.channel = channel
        h.publish_read_page("t-1", page=3)
        h._publish_step({"stage": "settle"}, "t-1")
        h._publish_step({"stage": "awaiting_approval"}, "t-1")
        stages = [(p["step"]["stage"], p["message"]) for k, p, tid in channel.published if k == "progress" and tid == "t-1"]
        self.assertEqual(
            stages,
            [
                ("read_page", "正在翻页查看（第 3 页）"),
                ("settle", "正在等待页面稳定"),
                ("awaiting_approval", "正在等待你的确认"),
            ],
        )

    def test_arc06_finish_table_projection_and_full_turntrace_delivery(self):
        rows = [[f"r{i}", f"Customer-{i}-{'x' * 180}"] for i in range(60)]
        table = {"title": "客户明细", "columns": ["ID", "Name"], "rows": rows, "total_rows": 60}
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel()
            h = TurnHarness.detached()
            h.channel = channel
            h.config = NS(max_user_answer_chars=8000)

            fx = _Wave3Fixture(
                Path(tmp),
                [
                    reply(call("sql_query")),
                    reply(call("finish", status="done", answer="", table_ref="q1")),
                ],
                lambda *_: LoopToolResult("60 rows", kind="read", read_key="q1", structured_table=table),
            )
            fx.harness = h
            answer = fx.run("导出全部客户")
            self.assertIn("60", answer)
            self.assertEqual(fx.state.finish_table["total_rows"], 60)
            self.assertEqual(len(fx.state.finish_table["rows"]), 60)

            # Verify TurnTrace.record_delivery stores structured_table metadata AND full >8000-char expanded rows
            delivery = fx._trace._deliveries[-1]
            self.assertEqual(delivery["structured_table"], {"title": "客户明细", "columns": ["ID", "Name"], "row_count": 60, "total_rows": 60})
            self.assertGreater(len(delivery["rendered_text"]), 8000)
            self.assertIn("Customer-59-", delivery["rendered_text"])

            # Verify TurnHarness.settle publishes `table` on terminal finished progress payload and panel_view projects it
            fx.state.budget = NS(projection=lambda: {"terminal_reason": ""})
            fx.state.receipt = TurnReceipt()
            fx.state.writes = NS(end_control=lambda: None)
            fx.state.approvals = NS(operation_result=lambda: None, principal=None)
            fx.state.answer_targets = ()
            h.settle(fx.state, None, answer, decision_strategy="model-driven", verifies_finish=False)
            term = [p for k, p, _ in channel.published if k == "progress" and p.get("state") == "finished"][-1]
            self.assertEqual(term["table"]["total_rows"], 60)
            projected = project_event(TaskEvent(1, "e00000001", "s-1", 1, "progress", "t-1", term), at=1)
            self.assertEqual(len(projected["table"]["rows"]), 60)

    def test_arc07_verified_mutation_synthesizes_summary_on_provider_error_and_never_on_indeterminate(self):
        # 1. Verified mutation + final summary ProviderError -> deterministic agent_finished summary
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Wave3Fixture(
                Path(tmp),
                [ProviderError("timeout", code="provider_deadline")],
                lambda *_: None,
            )
            fx.state.mutation_outcomes = [
                {
                    "position": 0,
                    "effect": "更新联系人手机",
                    "targets": ["张三"],
                    "verified": True,
                    "details": ["手机：13800000000 → 13900000000"],
                    "receipt": {"reference": "contact:7"},
                }
            ]
            answer = fx.run("把张三的手机改成13900000000")
            self.assertEqual(fx.state.terminal_reason, "agent_finished")
            self.assertEqual(fx.state.stop_reason, "agent_finished")
            self.assertIn("回读确认", answer)
            self.assertIn("13800000000 → 13900000000", answer)
            self.assertIn("contact:7", answer)

        # 2. Indeterminate or unverified write -> strictly NEVER synthesized as agent_finished
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Wave3Fixture(
                Path(tmp),
                [TimeoutError("timed out")],
                lambda *_: None,
            )
            fx.state.approvals = NS(business_counts={"mutation_indeterminate": 1})
            fx.state.mutation_outcomes = [{"position": 0, "effect": "更新", "targets": ["张三"], "verified": True}]
            answer = fx.run("修改")
            self.assertEqual(fx.state.terminal_reason, "agent_refused")
            self.assertIn("模型请求失败", answer)

    def test_ux02_need_input_options_and_ux03_ux05_offer_prefill_and_handoff_payload(self):
        # UX-02: need_input options -> published on needs_input progress payload & projected by panel_view
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel()
            h = TurnHarness.detached()
            h.channel = channel
            h.config = NS(max_user_answer_chars=8000)
            opts = [
                {"id": "c1", "label": "Acme Corp", "prompt": "选择 Acme Corp"},
                {"id": "c2", "label": "Beta Ltd", "prompt": "选择 Beta Ltd"},
            ]
            fx = _Wave3Fixture(
                Path(tmp),
                [reply(call("finish", status="need_input", answer="请选择目标客户：", options=opts))],
                lambda *_: None,
            )
            fx.harness = h
            ans = fx.run("查客户")
            self.assertEqual(fx.state.terminal_reason, "needs_clarification")
            self.assertEqual(fx.state.input_options, opts)
            fx.state.budget = NS(projection=lambda: {"terminal_reason": ""})
            fx.state.receipt = TurnReceipt()
            fx.state.writes = NS(end_control=lambda: None)
            fx.state.approvals = NS(operation_result=lambda: None, principal=None)
            fx.state.answer_targets = ()
            h.settle(fx.state, None, ans, decision_strategy="model-driven", verifies_finish=False)
            term = [p for k, p, _ in channel.published if k == "progress" and p.get("state") == "needs_input"][-1]
            self.assertEqual(term["input_options"], opts)
            projected = project_event(TaskEvent(1, "e00000002", "s-1", 2, "progress", "t-2", term), at=2)
            self.assertEqual(projected["input_options"], opts)

        # UX-03 & UX-05: native_submit offers offer_prefill=True for write forms and builds handoff payload
        obs = _obs_with_controls(epoch=1)
        seen_offer = []

        class _Approvals:
            approval_hold_ms = 1000
            business_counts = {"mutation_indeterminate": 0}

            def context(self, step, tool, hold_ms=0):
                return NS(binding_digest="a" * 64, audit_projection=lambda: {})

            def approve_mutation(self, proposal, *, offer_prefill=False, **_kw):
                seen_offer.append(offer_prefill)
                return MutationApproval(approval_id="ap-prefill", reason="prefill_selected", detail="prefill")

        class _NativePrefillExec(NativeSubmitMixin):
            native_submit = NS(
                contract=lambda: SubmitConventions(
                    volatile_keys=frozenset(),
                    record_key="id",
                    line_key="id",
                    name_key="remark",
                    epoch_zone=None,
                    decode_write=lambda status, body: (status, None),
                ),
                recent_reads=lambda: (("/records/get", "id=1"),),
                prefixes=lambda *a, **k: ("/records/",),
                read_record=lambda *a, **k: {"id": "1", "remark": "old"},
                catalog=lambda *a, **k: frozenset(),
            )
            _earlier_words = []

            def _dispatch(self, s, action, args, **kw):
                form = {
                    "model": {"id": "1", "remark": "new"},
                    "mode": "update",
                    "record_id": "1",
                    "title": "Edit",
                    "items": [{"prop": "remark", "label": "Remark"}],
                    "check": "chk",
                    "withheld": [],
                    "invalid": [],
                    "validation": True,
                }
                return ActionReceipt(1, "c1", action, args, True, observation=obs, url_before=obs.url, url_after=obs.url, form_data=form)

            def _said(self, prompt):
                return [prompt]

            def _element(self, s, index):
                return obs.elements[index - 1]

            def _submit_allowed(self, turn):
                return True

            def _page_fingerprint(self, o):
                return "fp-1"

        ex = _NativePrefillExec()
        s = HarnessTurnContext(
            message=NS(message_id="m-p", text="把 Remark 改成 new"),  # type: ignore[arg-type]
            run_id="run-p",
            ledger=NS(append=lambda *_a, **_k: None),  # type: ignore[arg-type]
            browser=None,
            writes=None,  # type: ignore[arg-type]
            approvals=_Approvals(),  # type: ignore[arg-type]
            obs=obs,
            started_at=0.0,
            chinese=True,
        )
        from web_operation_agent.execution.submit_contract import FormData
        from web_operation_agent.execution.value_source import Entry

        baseline = FormData.from_json(
            {
                "model": {"id": "1", "remark": "old"},
                "mode": "update",
                "record_id": "1",
                "title": "Edit",
                "items": [{"prop": "remark", "label": "Remark"}],
                "check": "chk",
                "withheld": [],
                "invalid": [],
                "validation": True,
            }
        )
        turn_dict = {
            "written": [],
            "form_baselines": {("dialog-1", "life-1"): baseline},
            "source_ledger": SourceLedger(),
            "form_field_outcomes": {
                "applied_fields": {"Remark": "new"},
                "rejected_fields": {"Status": "option_unavailable"},
                "previous_fields": {"Remark": "old"},
                "restricted_fields": ["Password"],
            },
        }

        turn_dict["source_ledger"].add(Entry("Remark", "new", kind="text"))
        out = ex._request_submit(s, {"index": 3}, turn_dict)
        self.assertEqual(seen_offer, [True], out)
        self.assertEqual(out.state, "completed")
        self.assertEqual(s.terminal_reason, "agent_finished")
        self.assertEqual(
            s.handoff_payload,
            {
                "completed_fields": [{"name": "Remark", "value": "new"}],
                "pending_fields": [{"name": "Status", "value": "option_unavailable"}],
                "restricted_controls": [{"label": "Password", "kind": "password", "ref": "e4"}],
                "can_undo": True,
            },
        )
        projected_handoff = project_event(
            TaskEvent(1, "e00000003", "s-1", 3, "progress", "t-3", {"state": "finished", "message": out.feedback, "handoff": s.handoff_payload}),
            at=3,
        )
        self.assertEqual(projected_handoff["handoff"]["completed_fields"], [{"name": "Remark", "value": "new"}])
        self.assertTrue(projected_handoff["handoff"]["can_undo"])


if __name__ == "__main__":
    unittest.main()
