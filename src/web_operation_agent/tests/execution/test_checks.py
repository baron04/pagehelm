"""Tests for ARC-12 ProgramCheckSpec, check_fired telemetry, and trace projection."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.execution.answer_check import (
    MODEL_DELIVERY_REJECTED_SPEC,
    UNTRACED_NUMBERS_SPEC,
)
from web_operation_agent.execution.checks import (
    CHECK_OUTCOMES,
    ProgramCheckSpec,
    record_check_fired,
)
from web_operation_agent.execution.harness import (
    HarnessTurnContext,
    ModelTurnState,
    _build_debug_trace,
)
from web_operation_agent.execution.loop_guard import (
    FAILED_READ_REPEATED_SPEC,
    SAME_STEP_STALLED_SPEC,
)
from web_operation_agent.execution.model_loop import (
    COMPLETION_REVIEW_SPEC,
    CORE_CHECK_SPECS,
    FINISH_TOOL,
    MODEL_ANSWER_NOT_DELIVERED_SPEC,
    SLIM_VERIFY_SPEC,
    ModelDrivenTurnExecutor,
)
from web_operation_agent.execution.model_loop_contracts import (
    DeliveryFeedback,
    LoopToolResult,
    VerificationResult,
)
from web_operation_agent.execution.turn_trace import TurnTrace
from web_operation_agent.modeling.provider import ProviderError
from web_operation_agent.privacy import SessionHandoffPolicy
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.state.ledger import SessionLedger


class ProgramCheckSpecTests(unittest.TestCase):
    def test_program_check_spec_validates_non_empty_fields(self):
        spec = ProgramCheckSpec(
            name="untraced_numbers",
            checks_what="stated numbers trace to turn sources",
            retry_guidance="replace untraced numbers with observed tool values",
            terminal_delivery="mark affected deliverable items undelivered or append note",
        )
        self.assertEqual(spec.name, "untraced_numbers")
        for bad in ("", "   ", 123):
            with self.assertRaises(ValueError):
                ProgramCheckSpec(
                    name=bad,  # type: ignore[arg-type]
                    checks_what="a",
                    retry_guidance="b",
                    terminal_delivery="c",
                )

    def test_record_check_fired_updates_ledger_context_trace_and_debug_trace(self):
        self.assertEqual(CHECK_OUTCOMES, frozenset({"returned", "annotated", "blocked"}))
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SessionLedger(Path(tmp) / "audit.jsonl", "run-chk", base={"session_id": "s1"})
            trace = TurnTrace(Path(tmp) / "turns", SessionHandoffPolicy(()))
            ctx = HarnessTurnContext(
                message=None,  # type: ignore[arg-type]
                run_id="run-chk",
                ledger=ledger,
                browser=None,
                writes=None,  # type: ignore[arg-type]
                approvals=None,  # type: ignore[arg-type]
                obs=None,
                started_at=0.0,
            )

            record_check_fired(
                ledger,
                s=ctx,
                trace=trace,
                check="untraced_numbers",
                outcome="returned",
                attempt=1,
                answer_preserved=False,
            )
            record_check_fired(
                ledger,
                s=ctx,
                trace=trace,
                check="untraced_numbers",
                outcome="annotated",
                attempt=2,
                answer_preserved=True,
            )

            events = [
                json.loads(line)
                for line in (Path(tmp) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([e["event"] for e in events], ["check_fired", "check_fired"])
            self.assertEqual(
                events[0],
                {
                    "seq": 1,
                    "t": events[0]["t"],
                    "run_id": "run-chk",
                    "session_id": "s1",
                    "event": "check_fired",
                    "check": "untraced_numbers",
                    "outcome": "returned",
                    "attempt": 1,
                    "answer_preserved": False,
                },
            )
            self.assertTrue(events[1]["answer_preserved"])
            self.assertEqual(events[1]["outcome"], "annotated")
            self.assertEqual(events[1]["attempt"], 2)

            path = trace.write(
                run_id="run-chk",
                turn_id="t1",
                prompt="check",
                start_route="/",
                terminal_reason="agent_finished",
                wall_seconds=0.1,
                answer="ok",
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["checks"]), 2)
            self.assertEqual(saved["checks"][1]["outcome"], "annotated")

            debug = _build_debug_trace(
                s=ctx,
                wall_seconds=0.25,
                stop_reason="agent_finished",
                stage_timings={"model": 0.1},
                receipt_report={},
            )
            self.assertEqual(debug["checks"], saved["checks"])

    def test_record_check_fired_rejects_invalid_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SessionLedger(Path(tmp) / "audit.jsonl", "run-bad", base={"session_id": "s1"})
            with self.assertRaises(ValueError):
                record_check_fired(ledger, check="", outcome="returned", attempt=1, answer_preserved=False)
            with self.assertRaises(ValueError):
                record_check_fired(ledger, check="x", outcome="unknown", attempt=1, answer_preserved=False)
            with self.assertRaises(ValueError):
                record_check_fired(ledger, check="x", outcome="blocked", attempt=0, answer_preserved=False)
            with self.assertRaises(ValueError):
                record_check_fired(ledger, check="x", outcome="blocked", attempt=1, answer_preserved=1)  # type: ignore[arg-type]


def _obs(epoch: int = 1, route: str = "/list") -> PageObservation:
    return PageObservation(
        1,
        epoch,
        "https://synthetic.test" + route,
        "List",
        [],
        [ElementRef("e1", "button", "Next", "button", epoch)],
        content="List",
        render_state="ready",
        page_route=route,
    )


def _call(name: str, **arguments) -> dict:
    return {
        "id": f"{name}-call",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _reply(*calls: dict) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


class _ScriptedChat:
    def __init__(self, replies):
        self._replies = list(replies)

    def complete_cancellable(self, messages, tools, run_id, **kwargs):
        item = self._replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _CheckExecutor(ModelDrivenTurnExecutor):
    """Minimal ModelDrivenTurnExecutor harness for verifying ARC-12 check emission."""

    def __init__(self, tmpdir: Path, replies, handler=None, *, verify_fn=None, budget_remaining: float = 60.0):
        self._messages = []
        self._earlier_words = []
        self.model_steps = 12
        self.verify = True
        self.handler = handler or (lambda *_: LoopToolResult("ok", kind="read"))
        self._verify_fn = verify_fn or (lambda *_: VerificationResult("passed"))
        self._tool_receipts = []
        self._trace = TurnTrace(tmpdir / "turns", SessionHandoffPolicy(()))
        self.ledger = SessionLedger(tmpdir / "audit.jsonl", "run-checks", base={"session_id": "s1"})
        self.state = HarnessTurnContext(
            message=NS(message_id="m-1", text="q"),  # type: ignore[arg-type]
            run_id="run-checks",
            ledger=self.ledger,
            browser=None,
            writes=None,  # type: ignore[arg-type]
            approvals=None,  # type: ignore[arg-type]
            obs=_obs(),
            started_at=0.0,
            chinese=True,
            chat=_ScriptedChat(replies),
        )
        self.state.budget = NS(
            remaining=lambda: budget_remaining,
            check=lambda: budget_remaining,
        )  # type: ignore[assignment]
        self.turn = ModelTurnState()

    def _dispatch(self, state, action, arguments, **kwargs):
        before = state.obs
        after = before if action == "observe" else _obs(before.epoch + 1, route=before.page_route)
        state.obs = after
        receipt = ActionReceipt(
            1,
            "cmd",
            action,
            arguments,
            True,
            observation=after,
            url_before=before.url,
            url_after=after.url,
            write_guard_held=True,
        )
        self._tool_receipts.append(receipt)
        self._verified_action_commands = {receipt.command_id: json.dumps(after.to_json())}
        return receipt

    def _tools(self, turn):
        return []

    def _page(self, state, turn):
        return f"epoch={state.obs.epoch}"

    def _completion_conflict(self, observation, answer):
        return ()

    def _invoke_tool(self, state, name, arguments, turn):
        return self.handler(self, name, arguments)

    def _note(self, state, **facts):
        pass

    def _verify(self, s, prompt, answer, turn):
        return self._verify_fn(s, prompt, answer, turn)

    def run(self, prompt: str = "q") -> str:
        return self._loop(self.state, prompt, self.turn, verified=False)


class CoreCheckSpecTests(unittest.TestCase):
    def test_core_check_specs_registry_has_non_empty_specs_for_all_core_checks(self):
        expected = {
            "untraced_numbers": UNTRACED_NUMBERS_SPEC,
            "model_delivery_rejected": MODEL_DELIVERY_REJECTED_SPEC,
            "completion_review": COMPLETION_REVIEW_SPEC,
            "slim_verify": SLIM_VERIFY_SPEC,
            "failed_read_repeated": FAILED_READ_REPEATED_SPEC,
            "same_step_stalled": SAME_STEP_STALLED_SPEC,
            "model_answer_not_delivered": MODEL_ANSWER_NOT_DELIVERED_SPEC,
        }
        self.assertEqual(set(CORE_CHECK_SPECS), set(expected))
        for name, spec in expected.items():
            self.assertIs(CORE_CHECK_SPECS[name], spec)
            self.assertIsInstance(spec, ProgramCheckSpec)
            self.assertEqual(spec.name, name)
            self.assertTrue(spec.checks_what.strip())
            self.assertTrue(spec.retry_guidance.strip())
            self.assertTrue(spec.terminal_delivery.strip())


class CoreCheckEmissionTests(unittest.TestCase):
    def _assert_checks(self, executor: _CheckExecutor, expected: list[dict]):
        ledger_checks = [
            {
                "check": ev["check"],
                "outcome": ev["outcome"],
                "attempt": ev["attempt"],
                "answer_preserved": ev["answer_preserved"],
            }
            for ev in executor.ledger.of_kind("check_fired")
        ]
        self.assertEqual(ledger_checks, expected)
        self.assertEqual(executor.state.checks, expected)
        self.assertEqual(executor._trace.checks, expected)

    def test_untraced_numbers_returned_then_annotated_and_budget_exhausted_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [
                    _reply(_call("finish", status="done", answer="共 999 条")),
                    _reply(_call("finish", status="done", answer="共 12 条")),
                ],
            )
            ex.profile = NS(untraced_numbers=lambda ans, _src: ("999",) if "999" in ans else ())
            self.assertEqual(ex.run(), "共 12 条")
            self._assert_checks(
                ex,
                [{"check": "untraced_numbers", "outcome": "returned", "attempt": 1, "answer_preserved": False}],
            )

        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [
                    _reply(_call("finish", status="done", answer="共 999 条")),
                    _reply(_call("finish", status="done", answer="仍是 999 条")),
                ],
            )
            ex.profile = NS(untraced_numbers=lambda *_args: ("999",))
            answer = ex.run()
            self.assertIn("以下数值未在本轮读取中找到出处：999", answer)
            self._assert_checks(
                ex,
                [
                    {"check": "untraced_numbers", "outcome": "returned", "attempt": 1, "answer_preserved": False},
                    {"check": "untraced_numbers", "outcome": "annotated", "attempt": 2, "answer_preserved": True},
                ],
            )

        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [_reply(_call("finish", status="done", answer="共 999 条"))],
                budget_remaining=0.5,
            )
            ex.profile = NS(untraced_numbers=lambda *_args: ("999",))
            ex.run()
            self._assert_checks(
                ex,
                [{"check": "untraced_numbers", "outcome": "annotated", "attempt": 1, "answer_preserved": True}],
            )

        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [
                    _reply(_call("finish", status="done", answer="共 999 条")),
                    ProviderError("synthetic unavailable"),
                ],
            )
            ex.profile = NS(untraced_numbers=lambda *_args: ("999",))
            ex.run()
            self._assert_checks(
                ex,
                [
                    {"check": "untraced_numbers", "outcome": "returned", "attempt": 1, "answer_preserved": False},
                    {"check": "untraced_numbers", "outcome": "annotated", "attempt": 2, "answer_preserved": True},
                ],
            )

    def test_model_delivery_rejected_returned_and_annotated_with_and_without_answer(self):
        tool = deepcopy(FINISH_TOOL)
        tool["function"]["parameters"]["properties"]["items"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "kind": {"type": "string"},
                    "status": {"type": "string"},
                    "receipts": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["item", "kind", "status", "receipts"],
                "additionalProperties": False,
            },
        }
        items = [{"item": "打开详情", "kind": "page", "status": "delivered", "receipts": ["old"]}]

        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [
                    _reply(_call("finish", status="done", answer="已打开详情。", items=items)),
                    _reply(_call("finish", status="done", answer="已打开详情。", items=items)),
                ],
            )
            ex._finish_tool = lambda _: tool
            ex._finish_feedback = lambda *_: DeliveryFeedback("delivery_page_receipt", "回执未核实。", (0,))
            ex.run()
            self._assert_checks(
                ex,
                [
                    {"check": "model_delivery_rejected", "outcome": "returned", "attempt": 1, "answer_preserved": False},
                    {"check": "model_delivery_rejected", "outcome": "annotated", "attempt": 2, "answer_preserved": True},
                ],
            )

        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [_reply(_call("finish", status="done", answer="   ", items=items))],
                budget_remaining=0.5,
            )
            ex._finish_tool = lambda _: tool
            ex._finish_feedback = lambda *_: DeliveryFeedback("delivery_page_receipt", "回执未核实。", (0,))
            ex.run()
            self._assert_checks(
                ex,
                [{"check": "model_delivery_rejected", "outcome": "annotated", "attempt": 1, "answer_preserved": False}],
            )

    def test_completion_review_returned_and_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [
                    _reply(_call("finish", status="done", answer="First")),
                    _reply(_call("finish", status="done", answer="Fixed")),
                ],
                verify_fn=lambda _s, _p, ans, _t: (
                    VerificationResult("passed")
                    if ans == "Fixed"
                    else VerificationResult("disputed", ("Missing item",))
                ),
            )
            ex.turn["review_completion"] = True
            self.assertEqual(ex.run(), "Fixed")
            self._assert_checks(
                ex,
                [{"check": "completion_review", "outcome": "returned", "attempt": 1, "answer_preserved": False}],
            )

        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [
                    _reply(_call("finish", status="done", answer="First")),
                    _reply(_call("finish", status="done", answer="Second")),
                ],
                verify_fn=lambda *_: VerificationResult("disputed", ("Still wrong",)),
            )
            ex.turn["review_completion"] = True
            ex.run()
            self._assert_checks(
                ex,
                [
                    {"check": "completion_review", "outcome": "returned", "attempt": 1, "answer_preserved": False},
                    {"check": "completion_review", "outcome": "blocked", "attempt": 2, "answer_preserved": False},
                ],
            )

        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [_reply(_call("finish", status="done", answer="First"))],
                verify_fn=lambda *_: VerificationResult("unavailable"),
            )
            ex.turn["review_completion"] = True
            ex.run()
            self._assert_checks(
                ex,
                [{"check": "completion_review", "outcome": "blocked", "attempt": 1, "answer_preserved": False}],
            )

    def test_slim_verify_returned_and_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [
                    _reply(_call("finish", status="done", answer="Done")),
                    _reply(_call("finish", status="done", answer="Confirmed")),
                ],
                verify_fn=lambda *_: VerificationResult("disputed", ("Check field value",)),
            )
            ex.turn["touched"] = [("字段", "未说出值")]
            self.assertEqual(ex.run("q"), "Confirmed")
            self._assert_checks(
                ex,
                [{"check": "slim_verify", "outcome": "returned", "attempt": 1, "answer_preserved": False}],
            )

        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [_reply(_call("finish", status="done", answer="Done"))],
                verify_fn=lambda *_: VerificationResult("unavailable"),
            )
            ex.turn["touched"] = [("字段", "未说出值")]
            ex.run("q")
            self._assert_checks(
                ex,
                [{"check": "slim_verify", "outcome": "blocked", "attempt": 1, "answer_preserved": False}],
            )

    def test_failed_read_repeated_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            same = _call("records", query="bad")
            ex = _CheckExecutor(
                Path(tmp),
                [_reply(same), _reply(same), _reply(same)],
                handler=lambda *_: LoopToolResult("invalid", kind="read", state="unverified", error_code="invalid_spec"),
            )
            ex.profile = NS(procedures=(NS(kind="read", schema={"function": {"name": "records"}}),))
            answer = ex.run()
            self.assertIn("同一读取反复以相同参数失败", answer)
            self._assert_checks(
                ex,
                [{"check": "failed_read_repeated", "outcome": "blocked", "attempt": 1, "answer_preserved": False}],
            )

    def test_same_step_stalled_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            step = _call("click", index=1)
            ex = _CheckExecutor(
                Path(tmp),
                [_reply(step) for _ in range(5)],
                handler=lambda *_: LoopToolResult("clicked", kind="page"),
            )
            answer = ex.run()
            self.assertEqual(answer, "同一步重复了几次都没有效果，本次没有完成。")
            self._assert_checks(
                ex,
                [{"check": "same_step_stalled", "outcome": "blocked", "attempt": 4, "answer_preserved": False}],
            )

    def test_model_answer_not_delivered_returned_and_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [
                    {"content": "plain intermediate draft"},
                    _reply(_call("finish", status="done", answer="Final")),
                ],
            )
            self.assertEqual(ex.run(), "Final")
            self._assert_checks(
                ex,
                [{"check": "model_answer_not_delivered", "outcome": "returned", "attempt": 1, "answer_preserved": False}],
            )

        with tempfile.TemporaryDirectory() as tmp:
            ex = _CheckExecutor(
                Path(tmp),
                [{"content": "plain draft"}] * 3,
            )
            ex.run()
            self._assert_checks(
                ex,
                [
                    {"check": "model_answer_not_delivered", "outcome": "returned", "attempt": 1, "answer_preserved": False},
                    {"check": "model_answer_not_delivered", "outcome": "returned", "attempt": 1, "answer_preserved": False},
                    {"check": "model_answer_not_delivered", "outcome": "blocked", "attempt": 3, "answer_preserved": False},
                ],
            )


if __name__ == "__main__":
    unittest.main()
