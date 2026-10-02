"""Parallel timing, unknown accounting and deadline/cancellation at the model boundary."""
import threading
import tempfile
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor

from web_operation_agent.execution.receipt import MeasuredChat, TurnReceipt, occupied
from web_operation_agent.execution.budget import TurnBudget
from web_operation_agent.modeling.provider import ProviderError
from web_operation_agent.modeling.spend import SpendMeter, BudgetExhausted
from web_operation_agent.transport.browser import TurnCancelled


class Chat:
    model = "synthetic-model"
    provider = "synthetic-provider"
    options = {"enable_thinking": False, "api_key": "never report me"}

    def __init__(self):
        self.calls = []

    def complete(self, messages, tools, run_id, max_tokens=None, options=None, timeout_s=None):
        self.calls.append(timeout_s)
        return {"content": "private answer"}

    def complete_cancellable(self, messages, tools, run_id, *, cancelled,
                             max_tokens=None, options=None, timeout_s=None):
        return self.complete(messages, tools, run_id, max_tokens, options, timeout_s)


class ReceiptTests(unittest.TestCase):
    def test_overlapping_intervals_are_not_summed(self):
        self.assertEqual(occupied([(1, 4), (2, 5), (7, 9), (7.5, 8)]), 6)
        self.assertEqual(occupied([]), 0)

    def test_receipt_omits_content_and_keeps_missing_accounting_unknown(self):
        clock = [0.0]
        receipt = TurnReceipt(clock=lambda: clock[0])
        chat = Chat()
        wrapped = MeasuredChat(chat, receipt, remaining_s=lambda: 10)
        with receipt.stage("scope"):
            wrapped.complete([{"role": "user", "content": "private question"}], [], "run")
        clock[0] = 1
        report = receipt.report(chat, "run")
        self.assertEqual(report["model_requests"], 1)
        self.assertEqual(report["stages"]["scope"]["model_requests"], 1)
        self.assertIsNone(report["accounting"]["model_calls"])
        self.assertIsNone(report["accounting"]["total_cost_cny"])
        self.assertFalse(report["accounting_complete"])
        self.assertEqual(report["model_options"], {"enable_thinking": False})
        self.assertEqual(report["provider"], "synthetic-provider")
        for private in ("private question", "private answer", "never report me", "api_key"):
            self.assertNotIn(private, str(report))

    def test_meter_report_is_the_authority_for_tokens_and_actual_provider_calls(self):
        chat = Chat()
        with tempfile.TemporaryDirectory() as tmp:
            chat.meter = SpendMeter(ledger=Path(tmp)/"spend.jsonl", price={
                "input_per_m": 1, "cache_read_per_m": .1, "output_per_m": 2})
            chat.meter.record(provider="synthetic", model=chat.model, run_id="run", usage={
                "prompt_tokens": 100, "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 80},
                "completion_tokens_details": {"reasoning_tokens": 5}}, request_id="a"*32)
            report = TurnReceipt().report(chat, "run")
        self.assertTrue(report["accounting_complete"])
        self.assertEqual(report["accounting"]["model_calls"], 1)
        self.assertEqual(report["accounting"]["known_input_tokens"], 100)
        self.assertEqual(report["accounting"]["known_output_tokens"], 20)
        self.assertEqual(report["accounting"]["known_cached_tokens"], 80)
        self.assertEqual(report["accounting"]["known_reasoning_tokens"], 5)
        self.assertAlmostEqual(report["accounting"]["total_cost_cny"], .000068)
        self.assertNotIn("a"*32, str(report))

    def test_parallel_stage_context_and_counters_are_isolated(self):
        receipt, chat, gate = TurnReceipt(), Chat(), threading.Barrier(2)
        wrapped = MeasuredChat(chat, receipt, remaining_s=lambda: 10)
        def run(stage):
            with receipt.stage(stage):
                gate.wait(timeout=2)
                wrapped.complete([], [], "run")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, name) for name in ("interpret", "vote")]
            for future in futures:
                future.result()
        report = receipt.report(chat, "run")
        self.assertEqual(report["model_requests"], 2)
        self.assertEqual(report["model_inflight"], 0)
        self.assertEqual(report["stages"]["interpret"]["model_requests"], 1)
        self.assertEqual(report["stages"]["vote"]["model_requests"], 1)
        self.assertLessEqual(report["model_occupied_seconds"], report["wall_seconds"])
        self.assertEqual(TurnReceipt().report(chat, "another")["model_requests"], 0)

    def test_provider_failure_closes_timing_but_never_invents_usage(self):
        class FailedChat(Chat):
            def complete(self, *args, **kwargs):
                raise ProviderError("failed")
        chat, receipt = FailedChat(), TurnReceipt()
        wrapped = MeasuredChat(chat, receipt, remaining_s=lambda: 10)
        with self.assertRaises(ProviderError):
            wrapped.complete([], [], "run")
        report = receipt.report(chat, "run")
        self.assertEqual(report["model_inflight"], 0)
        self.assertEqual(report["model_outcomes"], {"failed": 1})
        self.assertIsNone(report["accounting"]["total_cost_cny"])

    def test_every_request_uses_the_same_remaining_deadline_and_checks_cancel(self):
        chat, receipt, left = Chat(), TurnReceipt(), [8.0]
        wrapped = MeasuredChat(chat, receipt, remaining_s=lambda: left[0])
        wrapped.complete([], [], "run", timeout_s=30)
        left[0] = 2
        wrapped.complete([], [], "run", timeout_s=30)
        self.assertEqual(chat.calls, [8, 2])
        left[0] = 0
        with self.assertRaises(ProviderError) as expired:
            wrapped.complete([], [], "run")
        self.assertEqual(expired.exception.code, "provider_deadline")
        self.assertFalse(expired.exception.retryable)
        left[0] = 1
        with self.assertRaises(ProviderError) as stopped:
            wrapped.complete_cancellable([], [], "run", cancelled=lambda: True)
        self.assertEqual(stopped.exception.code, "provider_cancelled")
        self.assertEqual(len(chat.calls), 2)
        self.assertEqual(receipt.report(chat, "run")["model_requests"], 2)

    def test_model_budget_is_shared_across_stages_and_does_not_count_cache_hits(self):
        chat, receipt = Chat(), TurnReceipt()
        budget = TurnBudget(steps=2, seconds=10)
        wrapped = MeasuredChat(chat, receipt, remaining_s=budget.remaining, budget=budget)
        for stage in ("scope", "source"):
            with receipt.stage(stage):
                wrapped.complete([], [], "run")
        # A cache hit stays outside ChatPort and consumes no provider request.
        with receipt.stage("interpret"):
            pass
        with receipt.stage("repair"), self.assertRaises(BudgetExhausted) as exhausted:
            wrapped.complete([], [], "run")
        self.assertEqual(exhausted.exception.kind, "step_budget")
        self.assertEqual(len(chat.calls), 2)
        self.assertEqual(budget.projection()["model_requests_used"], 2)
        with self.assertRaises(BudgetExhausted):
            budget.check()  # No business/UI work after terminal budget exhaustion.

    def test_meter_exhaustion_stops_every_subsequent_stage(self):
        class Spent(Chat):
            def complete_cancellable(self, *args, **kwargs):
                self.calls.append(1)
                raise BudgetExhausted("token_budget", "synthetic ceiling")
        chat, receipt = Spent(), TurnReceipt()
        budget = TurnBudget(steps=8, seconds=10)
        wrapped = MeasuredChat(chat, receipt, remaining_s=budget.remaining, budget=budget)
        with self.assertRaises(BudgetExhausted):
            wrapped.complete([], [], "run")
        with receipt.stage("source"), self.assertRaises(BudgetExhausted):
            wrapped.complete([], [], "run")
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(budget.projection()["terminal_reason"], "token_budget")

    def test_wall_deadline_and_user_cancellation_apply_to_non_model_substeps(self):
        clock, stopped = [0.0], [False]
        budget = TurnBudget(steps=8, seconds=2, clock=lambda: clock[0], cancelled=lambda: stopped[0])
        budget.check()
        stopped[0] = True
        with self.assertRaises(TurnCancelled):
            budget.check()
        stopped[0] = False
        clock[0] = 2
        with self.assertRaises(BudgetExhausted) as deadline:
            budget.check()
        self.assertEqual(deadline.exception.kind, "wall_budget")
        self.assertEqual(budget.projection()["model_requests_used"], 0)
