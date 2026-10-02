"""Typed pre-dispatch retries and exact, fresh value verification for Goal handoffs."""
from dataclasses import replace
from contextlib import nullcontext
from types import SimpleNamespace
import unittest

from web_operation_agent.goal.executor import GoalTurnExecutor, _PageOutcome, _achieved
from web_operation_agent.goal.ports import PagePlan, Resolved
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.execution.budget import TurnBudget
from web_operation_agent.modeling.spend import BudgetExhausted


def observation(epoch=1, value="", *, name="", field="expiry", state="ready"):
    return PageObservation(schema_version=1, epoch=epoch, url="https://app.test/records/1",
        title="Fixture", landmarks=[], content="fixture", render_state=state,
        elements=[ElementRef("e1", "textbox", name, "input", epoch,
                            {"field": field, "value": value})])


class GoalFillTests(unittest.TestCase):
    def test_late_goal_receipt_is_audited_before_the_budget_stops_the_turn(self):
        clock, events, limits = [0.0], [], []
        budget = TurnBudget(steps=3, seconds=1, clock=lambda: clock[0])
        clock[0] = 0.75
        def execute(action, args, *, timeout_s, expect_navigation=False):
            limits.append(timeout_s)
            clock[0] = 2
            return ActionReceipt(schema_version=1, command_id="ack", action=action,
                args_echo={}, ok=True, observation=observation(2, "Value"))
        state = SimpleNamespace(terminal_reason="", budget=budget, step=0,
            obs=observation(), page_actions=0,
            ledger=SimpleNamespace(append=lambda event, **kw: events.append((event, kw))),
            approvals=SimpleNamespace(confirm_operation=lambda *a, **kw: ("", ""),
                                      settle_operation=lambda: None),
            writes=SimpleNamespace(expect_decision=lambda *a: None,
                                   write_outcome=lambda *a, **kw: {"state": "not_written"}),
            receipt=SimpleNamespace(stage=lambda stage: nullcontext()),
            browser=SimpleNamespace(execute_with_budget=execute))
        executor = GoalTurnExecutor.__new__(GoalTurnExecutor)
        executor.disabled_page_actions = ()
        with self.assertRaises(BudgetExhausted):
            executor._dispatch(state, "fill", {"ref": "e1", "value": "Value"})
        self.assertEqual(limits, [0.25])
        self.assertEqual(state.obs.epoch, 2)
        self.assertEqual([kw["command_id"] for event, kw in events
                          if event == "action_receipt"], ["ack"])
        self.assertEqual(state.page_actions, 1)

    def run_fill(self, responses, plan=None):
        plan = plan or PagePlan("/records/1", "Fixture", highlight="Expiry",
                               fill="2026-01-02", fill_kind="text", fill_field="expiry")
        state = SimpleNamespace(obs=observation(), terminal_reason="", step=0,
                                ledger=SimpleNamespace(append=lambda *a, **k: None))
        executor = GoalTurnExecutor.__new__(GoalTurnExecutor)
        calls = []

        def dispatch(s, action, args):
            calls.append((action, args))
            # Responses are (success, error code, value, fresh, render state).
            ok, code, value, fresh, render = responses[min(len(calls) - 1, len(responses) - 1)]
            epoch = s.obs.epoch + int(fresh)
            s.obs = observation(epoch, value, state=render)
            return ActionReceipt(schema_version=1, command_id=str(len(calls)), action=action,
                                 args_echo={}, ok=ok, error_code=code, observation=s.obs)
        executor._dispatch = dispatch
        result = executor._fill(state, state.obs.elements[0], plan)
        return result, calls

    def test_transposed_dates_extra_digits_and_case_changes_cannot_prove_the_value(self):
        for value in ("2026-02-01", "2026-01-02 12:30", "02026-01-02", ""):
            with self.subTest(value=value):
                result, calls = self.run_fill([(True, "", value, True, "ready")])
                self.assertFalse(result[1])
                self.assertEqual([a for a, _ in calls], ["fill", "wait_for"])
        plan = PagePlan("/records/1", "Fixture", highlight="Expiry", fill="CaseSensitive",
                        fill_kind="text", fill_field="expiry")
        result, _ = self.run_fill([(True, "", "casesensitive", True, "ready")], plan)
        self.assertFalse(result[1])

    def test_only_declared_exact_input_or_display_value_is_verified(self):
        for value in ("1200.50", "1,200.50"):
            plan = PagePlan("/records/1", "Fixture", highlight="Expiry", fill="1200.50",
                            fill_label="1,200.50", fill_kind="text", fill_field="expiry")
            result, _ = self.run_fill([(True, "", value, True, "ready")], plan)
            self.assertEqual(result, ("1,200.50", True))

    def test_failed_choice_never_retries_with_a_different_value(self):
        plan = PagePlan("/records/1", "Fixture", highlight="Expiry", fill="draft",
                        fill_label="Draft", fill_kind="choice", fill_field="expiry")
        for code in ("page_error", "bad_arguments", "user_intervened"):
            with self.subTest(code=code):
                result, calls = self.run_fill([(False, code, "draft", True, "ready")], plan)
                self.assertEqual(result, ("", False))
                self.assertEqual(calls, [("select", {"ref": "e1", "epoch": 1, "value": "draft"})])

    def test_stale_ref_can_retry_the_same_value_against_a_fresh_field(self):
        result, calls = self.run_fill([
            (False, "stale_observation", "", True, "ready"),
            (True, "", "2026-01-02", True, "ready")])
        self.assertEqual(result, ("2026-01-02", True))
        self.assertEqual([a for a, _ in calls], ["fill", "fill", "wait_for"])
        self.assertEqual([args["value"] for action, args in calls if action == "fill"],
                         ["2026-01-02", "2026-01-02"])
        self.assertEqual(calls[1][1]["epoch"], 2)

    def test_old_or_pending_or_blocked_receipts_are_not_verified(self):
        for fresh, render in ((False, "ready"), (True, "pending"), (True, "blocked")):
            with self.subTest(fresh=fresh, render=render):
                result, _ = self.run_fill([(True, "", "2026-01-02", fresh, render)])
                self.assertFalse(result[1])

    def test_ambiguous_or_conflicting_or_unavailable_inputs_are_not_guessed(self):
        executor = GoalTurnExecutor.__new__(GoalTurnExecutor)
        plan = PagePlan("/records/1", "Fixture", highlight="Expiry", fill_field="expiry")
        first = observation().elements[0]
        cases = [
            [first, replace(first, ref="e2", name="Expiry")],
            [replace(first, attrs={"field": "other"}, name="Expiry")],
            [replace(first, attrs={"field": "expiry", "disabled": "true"})],
            [replace(first, attrs={"field": "expiry", "readonly": "true"})],
            [replace(first, attrs={}, name="Expiry"), replace(first, ref="e2", attrs={}, name="Expiry")],
        ]
        for elements in cases:
            with self.subTest(elements=elements):
                obs = observation()
                obs.elements = elements
                self.assertIsNone(executor._find_input(SimpleNamespace(obs=obs), plan))

    def test_unverified_or_missing_fill_does_not_complete_a_handoff(self):
        resolved = Resolved("handoff", page=PagePlan("/records/1", "Fixture", fill="Value"))
        self.assertFalse(_achieved(resolved, _PageOutcome(reached=True, highlighted="Field")))
        self.assertFalse(_achieved(resolved, _PageOutcome(reached=True, filled="Value",
                                                        fill_verified=False)))
        self.assertTrue(_achieved(resolved, _PageOutcome(reached=True, filled="Value",
                                                       fill_verified=True)))


if __name__ == "__main__":
    unittest.main()
