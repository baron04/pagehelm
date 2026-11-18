"""Tests for ARC-03 (TurnHarness & ModelTurnState), ARC-08 (trace/audit/debug_trace), and ARC-09 (concurrent pure-read hook)."""
from __future__ import annotations

from dataclasses import replace
import json
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.execution import harness as harness_mod
from web_operation_agent.execution.harness import ModelTurnState, TurnHarness
from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.model_loop_contracts import (
    AnswerTarget,
    LoopProcedure,
    LoopToolResult,
    ModelLoopProfile,
)
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.tests.execution.product_support import (
    _Channel,
    _RemoteBrowser,
    _message,
)
from web_operation_agent.tests.support.application import _InvoiceAdapter
from web_operation_agent.tests.support.chat_port_contract import CancellableChatDouble
from web_operation_agent.transport.panel_view import _terminal_extras, panel_limits


class _Meter:
    def __init__(self, *, input_tokens: int = 120, output_tokens: int = 45):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def run_report(self, run_id: str) -> dict:
        return {
            "model_calls": 2,
            "usage_unavailable_calls": 0,
            "known_input_tokens": self.input_tokens,
            "known_output_tokens": self.output_tokens,
            "known_cached_tokens": 0,
            "known_reasoning_tokens": 0,
            "known_cost_cny": 0.0,
            "total_cost_cny": 0.0,
        }


class _ScriptedChat(CancellableChatDouble):
    model = "synthetic-model"
    provider = "synthetic-provider"
    options = {"enable_thinking": False}

    def __init__(self, replies, *, meter=None):
        self.replies = list(replies)
        self.meter = meter
        self.seen_messages = []

    def complete(self, messages, tools, run_id, **kwargs):
        self.seen_messages.append(messages)
        return self.replies.pop(0)


class _DummyGoalPort:
    def capabilities(self):
        return frozenset()


def _tool_reply(*calls: tuple[str, dict]):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call-{idx}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
            for idx, (name, args) in enumerate(calls, 1)
        ],
    }


def _make_schema(name: str, *, props: dict | None = None, required: tuple[str, ...] = ()) -> str:
    return json.dumps(
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Run {name}",
                "parameters": {
                    "type": "object",
                    "properties": props or {"key": {"type": "string"}},
                    "required": list(required),
                    "additionalProperties": False,
                },
            },
        },
        ensure_ascii=False,
    )


class _HarnessChannel(_Channel):
    def __init__(self, page_url: str, *, local_dev_mode: bool = False):
        now = int(time.time() * 1000)
        tool_scopes = frozenset().union(*_InvoiceAdapter().business_tools.required_scopes.values())
        scopes = tuple(sorted(tool_scopes | {"records:read"}))
        principal = PrincipalContext(
            1, "ledger-demo", "tenant", "workspace", "actor", "delegation",
            now - 1000, now + 60_000, scopes,
        )
        super().__init__(page_url=page_url, principal=principal)
        self.local_dev_mode = local_dev_mode
        self.events: list[tuple[str, dict, str]] = []

    def publish(self, kind: str, payload: dict, *, turn_id: str = "") -> None:
        self.events.append((kind, dict(payload), turn_id))


class _FixtureIdentityPort:
    def __init__(self, scopes: frozenset[str]):
        self._scopes = scopes

    @property
    def required(self) -> bool:
        return True

    @property
    def required_scopes(self) -> frozenset[str]:
        return self._scopes

    def bind(self, page_identity, *, delegation_id: str, lifetime_s: int = 300, now_ms: int | None = None):
        raise NotImplementedError

    def validate(self, principal, *, application_id: str) -> None:
        return None


def _bound_adapter():
    adapter = _InvoiceAdapter()
    scopes = frozenset().union(*adapter.business_tools.required_scopes.values())
    adapter.ports = replace(adapter.ports, identity=_FixtureIdentityPort(scopes))
    return bind_application(adapter)


class Arc03CompositionAndStateTests(unittest.TestCase):
    def test_model_driven_executor_composes_harness_without_goal_inheritance(self):
        self.assertFalse(issubclass(ModelDrivenTurnExecutor, GoalTurnExecutor))

    def test_model_turn_state_supports_typed_fields_and_mapping_compat(self):
        state = ModelTurnState(read_only=True)
        self.assertNotIn("forced_summary_at", state)
        self.assertNotIn("pending_delivery", state)
        self.assertNotIn("review_completion", state)
        self.assertIsNone(state.get("forced_summary_at"))

        state["forced_summary_at"] = 4
        state["forced_summary_kind"] = "read"
        self.assertIn("forced_summary_at", state)
        self.assertEqual(state.forced_summary_at, 4)
        self.assertEqual(state.get("forced_summary_at"), 4)

        state["review_completion"] = True
        self.assertTrue(state.review_completion)
        self.assertIs(state.pop("review_completion", None), True)
        self.assertNotIn("review_completion", state)

        ledger = state.setdefault("source_ledger", ["entry-1"])
        self.assertEqual(ledger, ["entry-1"])
        self.assertEqual(state.source_ledger, ["entry-1"])

        state["custom_adapter_key"] = {"ok": True}
        self.assertEqual(state.extras["custom_adapter_key"], {"ok": True})
        copied = {**state, "not_executed": [("click", "Save", "unreviewed")]}
        self.assertEqual(copied["forced_summary_at"], 4)
        self.assertEqual(copied["custom_adapter_key"], {"ok": True})
        self.assertEqual(copied["not_executed"], [("click", "Save", "unreviewed")])


class Arc08TelemetryAndDebugTraceTests(unittest.TestCase):
    def _run_turn(self, *, local_dev_mode: bool, turn_traces: bool):
        url = "https://ledger.example/invoices"

        class ReadHandler:
            def execute(self, arguments, *, context):
                time.sleep(0.01)
                return LoopToolResult(
                    f"read:{arguments.get('key', '')}",
                    kind="read",
                    state="completed",
                    answer_targets=(
                        AnswerTarget("page", "/invoices", "Invoices", "invoices"),
                    ),
                )

        proc = LoopProcedure(
            _make_schema("lookup_item", required=("key",)),
            ReadHandler(),
            "read",
            "records:read",
        )
        profile = ModelLoopProfile(
            instructions="Use lookup_item and finish.",
            procedures=(proc,),
            render_page=lambda obs, _m: f"URL:{obs.url}",
            prepare=lambda _req, _ctx: "",
        )
        chat = _ScriptedChat(
            [
                _tool_reply(("lookup_item", {"key": "A1"})),
                _tool_reply(("finish", {"status": "done", "answer": "Found A1"})),
            ],
            meter=_Meter(input_tokens=100, output_tokens=30),
        )
        channel = _HarnessChannel(url, local_dev_mode=local_dev_mode)
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "audit.jsonl"
            executor = ModelDrivenTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read invoices.",
                    ledger_path=ledger_path,
                    confirm_page_operations=False,
                ),
                chat=chat,
                application=_bound_adapter(),
                goal_port=_DummyGoalPort(),
                control=ReadOnlyControl(),
                run_id_factory=lambda _m: "arc08-run",
                profile=profile,
                verify=False,
                turn_traces=turn_traces,
            )
            self.assertIsInstance(executor.harness, TurnHarness)
            with patch.object(harness_mod, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute(_message(url))
            audit = [json.loads(line) for line in ledger_path.read_text().splitlines()]
            trace_files = sorted((Path(tmp) / "turns").glob("*.json")) if turn_traces else []
            trace_data = json.loads(trace_files[0].read_text()) if trace_files else None
        return result, audit, channel.events, trace_data

    def test_production_mode_omits_debug_trace_while_recording_turn_trace_and_audit(self):
        result, audit, events, trace_data = self._run_turn(local_dev_mode=False, turn_traces=True)
        self.assertEqual(result.terminal_reason, "agent_finished")
        terminal_progress = [p for kind, p, _tid in events if kind == "progress" and p.get("state") == "finished"][-1]
        self.assertNotIn("debug_trace", terminal_progress)

        timing_events = [rec for rec in audit if rec.get("event") == "turn_timing"]
        self.assertEqual(len(timing_events), 1)
        timing = timing_events[0]
        self.assertEqual(timing["first_route_kind"], "read")
        self.assertEqual(timing["stop_reason"], "agent_finished")
        self.assertIn("business", timing["stage_timings"])
        self.assertEqual(len(timing["tool_timings"]), 1)
        self.assertEqual(timing["tool_timings"][0]["tool"], "lookup_item")
        self.assertTrue(timing["tool_timings"][0]["ok"])

        self.assertIsNotNone(trace_data)
        self.assertEqual(trace_data["first_route_kind"], "read")
        self.assertEqual(trace_data["stop_reason"], "agent_finished")
        self.assertIn("business", trace_data["stage_timings"])
        self.assertEqual(len(trace_data["tool_timings"]), 1)
        self.assertEqual(trace_data["tool_timings"][0]["tool"], "lookup_item")

    def test_local_dev_mode_attaches_sanitized_debug_trace_for_panel_projection(self):
        _result, _audit, events, _trace = self._run_turn(local_dev_mode=True, turn_traces=False)
        terminal_progress = [p for kind, p, _tid in events if kind == "progress" and p.get("state") == "finished"][-1]
        self.assertIn("debug_trace", terminal_progress)
        debug_trace = terminal_progress["debug_trace"]
        self.assertEqual(debug_trace["first_route_kind"], "read")
        self.assertEqual(debug_trace["stop_reason"], "agent_finished")
        self.assertGreaterEqual(debug_trace["wall_ms"], 0)
        self.assertEqual(debug_trace["model_calls"], 2)
        self.assertEqual(debug_trace["tokens"], {"input": 100, "output": 30, "total": 130})
        self.assertEqual(len(debug_trace["tool_chain"]), 1)
        self.assertEqual(debug_trace["tool_chain"][0]["tool"], "lookup_item")
        projected = _terminal_extras(terminal_progress, panel_limits())
        self.assertIn("debug_trace", projected)
        self.assertEqual(projected["debug_trace"]["tokens"]["total"], 130)


class Arc09ConcurrentPureReadHookTests(unittest.TestCase):
    def test_concurrent_pure_reads_refresh_once_run_in_parallel_and_commit_in_order(self):
        url = "https://ledger.example/invoices"
        barrier = threading.Barrier(2, timeout=3.0)
        prepare_threads: list[int] = []
        commit_order: list[str] = []
        main_thread = threading.get_ident()
        observed_executor: list[ModelDrivenTurnExecutor] = []

        class ConcurrentReadHandler:
            def execute(self, arguments, *, context):
                raise AssertionError("concurrent pure-read path should use prepare_read + commit_read")

            def prepare_read(self, arguments, *, observation, operation):
                key = arguments["key"]
                prepare_threads.append(threading.get_ident())
                # Proof that worker threads do not share or mutate executor._tool_receipts
                self.assertEqual(observed_executor[0]._tool_receipts, [])
                barrier.wait()
                if key == "first":
                    time.sleep(0.03)
                return {"key": key, "epoch": observation.epoch if observation else None}

            def commit_read(self, prepared, arguments, *, context):
                assert threading.get_ident() == main_thread
                commit_order.append(prepared["key"])
                return LoopToolResult(
                    f"committed:{prepared['key']}",
                    kind="read",
                    state="completed",
                    read_summary=(prepared["key"], f"epoch={prepared['epoch']}"),
                    answer_targets=(
                        AnswerTarget("record", f"/invoices/{prepared['key']}", prepared["key"], prepared["key"]),
                    ),
                )

        handler = ConcurrentReadHandler()
        handler.assertEqual = self.assertEqual
        proc = LoopProcedure(
            _make_schema("batch_read", required=("key",)),
            handler,
            "read",
            "records:read",
        )
        profile = ModelLoopProfile(
            instructions="Use batch_read and finish.",
            procedures=(proc,),
            render_page=lambda obs, _m: f"URL:{obs.url}",
            prepare=lambda _req, _ctx: "",
        )
        chat = _ScriptedChat(
            [
                _tool_reply(
                    ("batch_read", {"key": "first"}),
                    ("batch_read", {"key": "second"}),
                ),
                _tool_reply(
                    ("finish", {"status": "done", "answer": "Read first and second."}),
                ),
            ]
        )
        channel = _HarnessChannel(url)
        with tempfile.TemporaryDirectory() as tmp:
            executor = ModelDrivenTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read invoices.",
                    ledger_path=Path(tmp) / "audit.jsonl",
                    confirm_page_operations=False,
                ),
                chat=chat,
                application=_bound_adapter(),
                goal_port=_DummyGoalPort(),
                control=ReadOnlyControl(),
                run_id_factory=lambda _m: "arc09-run",
                profile=profile,
                verify=False,
            )
            observed_executor.append(executor)
            with patch.object(harness_mod, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute(_message(url))
            browser = _RemoteBrowser.instances[-1]

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(len(set(prepare_threads)), 2)
        self.assertEqual(commit_order, ["first", "second"])
        # Single main-thread observe before fan-out
        self.assertEqual(browser.actions, ["observe"])
        tool_msgs = [m for m in chat.seen_messages[1] if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        self.assertIn("committed:first", tool_msgs[0]["content"])
        self.assertIn("committed:second", tool_msgs[1]["content"])

    def test_concurrent_read_worker_exception_isolates_failing_call_and_preserves_order(self):
        url = "https://ledger.example/invoices"
        commit_order: list[str] = []

        class FailingConcurrentReadHandler:
            def execute(self, arguments, *, context):
                raise AssertionError("expected concurrent path")

            def prepare_read(self, arguments, *, observation, operation):
                if arguments["key"] == "bad":
                    raise DataRefused("denied", permission_denied=False)
                return {"key": arguments["key"]}

            def commit_read(self, prepared, arguments, *, context):
                commit_order.append(prepared["key"])
                return LoopToolResult(f"ok:{prepared['key']}", kind="read", state="completed")

        proc = LoopProcedure(
            _make_schema("batch_read", required=("key",)),
            FailingConcurrentReadHandler(),
            "read",
            "records:read",
        )
        profile = ModelLoopProfile(
            instructions="Use batch_read and finish.",
            procedures=(proc,),
            render_page=lambda obs, _m: f"URL:{obs.url}",
            prepare=lambda _req, _ctx: "",
        )
        chat = _ScriptedChat(
            [
                _tool_reply(
                    ("batch_read", {"key": "bad"}),
                    ("batch_read", {"key": "good"}),
                ),
                _tool_reply(("finish", {"status": "done", "answer": "Done with partial read."})),
            ]
        )
        channel = _HarnessChannel(url)
        with tempfile.TemporaryDirectory() as tmp:
            executor = ModelDrivenTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read invoices.",
                    ledger_path=Path(tmp) / "audit.jsonl",
                    confirm_page_operations=False,
                ),
                chat=chat,
                application=_bound_adapter(),
                goal_port=_DummyGoalPort(),
                control=ReadOnlyControl(),
                run_id_factory=lambda _m: "arc09-err",
                profile=profile,
                verify=False,
            )
            with patch.object(harness_mod, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute(_message(url))

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(commit_order, ["good"])
        tool_msgs = [m for m in chat.seen_messages[1] if m["role"] == "tool"]
        self.assertIn("application_unavailable", tool_msgs[0]["content"])
        self.assertIn("ok:good", tool_msgs[1]["content"])


if __name__ == "__main__":
    unittest.main()
