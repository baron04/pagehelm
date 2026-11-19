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
from web_operation_agent.business.write_pipeline import WritePipeline
from web_operation_agent.execution import form_tools, harness as harness_mod, turn as turn_mod
from web_operation_agent.execution.draft_handoff import handoff_draft
from web_operation_agent.execution.harness import (
    HarnessTurnContext,
    ModelTurnState,
    TurnHarness,
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


class Wave3Acc13Tests(unittest.TestCase):
    def test_field_outcomes_propagate_without_masking(self):
        with tempfile.TemporaryDirectory() as tmp:
            def handler(fx, name, arguments):
                after = _obs_with_controls(epoch=3)
                receipt = ActionReceipt(
                    1,
                    "cmd-fill",
                    "fill",
                    {"ref": "e2", "value": "abc"},
                    False,
                    error_code="option_unavailable",
                    observation=after,
                    url_before=fx.state.obs.url,
                    url_after=after.url,
                    applied_fields={"Remark": "abc"},
                    rejected_fields={"Status": "option_unavailable"},
                    previous_fields={"Remark": "old"},
                    restricted_fields=("Password",),
                    candidate_labels=("Draft", "Active"),
                )
                fx._tool_receipts.append(receipt)
                return LoopToolResult("option unavailable", kind="page", state="refused", error_code="option_unavailable")

            fx = _Wave3Fixture(
                Path(tmp),
                [
                    reply(call("fill", index=2, value="abc")),
                    reply(call("finish", status="cannot", answer="字段选项不可用")),
                ],
                handler,
            )
            answer = fx.run("填写备注")
            self.assertEqual(answer, "字段选项不可用")
            tool_msg = next(m["content"] for m in fx._messages if m["role"] == "tool")
            self.assertIn("error_code=option_unavailable", tool_msg)
            self.assertIn("applied_fields=", tool_msg)
            self.assertIn("rejected_fields=", tool_msg)
            self.assertIn("restricted_fields=Password", tool_msg)
            self.assertIn("candidates=Draft、Active", tool_msg)
            self.assertEqual(fx.turn["form_field_outcomes"]["previous_fields"], {"Remark": "old"})


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

        # 3. Compound task with prior corrections, failed_reads, page_failures, completion_reviews,
        #    or subsequent tool calls after the verified write -> NEVER synthesized as agent_finished
        for bad_turn_fields in (
            {"corrections": 1},
            {"failed_reads": {("call_api", "{}"): "404"}},
            {"page_failures": 1},
            {"completion_reviews": 1},
            {"tool_calls": 2, "last_verified_mutation_call": 1},
        ):
            with self.subTest(bad_turn_fields=bad_turn_fields):
                with tempfile.TemporaryDirectory() as tmp:
                    fx = _Wave3Fixture(
                        Path(tmp),
                        [ProviderError("timeout", code="provider_deadline")],
                        lambda *_: None,
                    )
                    fx.state.mutation_outcomes = [
                        {"position": 0, "effect": "更新", "targets": ["张三"], "verified": True}
                    ]
                    fx.turn.update(bad_turn_fields)
                    answer = fx.run("修改 A 并修改 B")
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
