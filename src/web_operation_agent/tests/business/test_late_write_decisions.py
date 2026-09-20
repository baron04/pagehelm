"""Delayed control decisions must affect the next dispatch and every terminal path.

Synthetic in-memory faults drive the real session. No real duplicate write is claimed.
"""
import unittest

from web_operation_agent.schemas import ElementRef
from web_operation_agent.tests.execution.test_session import _Chat, _Control
from web_operation_agent.tests.business.test_write_semantics import (
    _Browser, _run, PERMIT, REFUSAL, SUBMIT, FINISH,
)
from web_operation_agent.tests.session_fixture import SessionFixture


class LateDecisions(unittest.TestCase):
    def test_control_loss_after_only_observing_is_not_an_unknown_write(self):
        result, _ = _run(_Chat([("observe", {}), FINISH]), _Browser([{}]),
                        _Control([[], []], unreadable_after=1))
        self.assertEqual(result.terminal_reason, "control_plane_error")

    def test_empty_trace_does_not_make_a_timeout_retryable(self):
        for code in ("navigation_timeout", "element_not_actionable", "page_error"):
            for next_call in (SUBMIT, ("observe", {})):
                with self.subTest(code=code, next_call=next_call):
                    result, session = _run(_Chat([SUBMIT, next_call, FINISH]),
                        _Browser([{"ok": False, "error_code": code}, {}]),
                        _Control([[], [], [PERMIT]]))
                    self.assertEqual(result.terminal_reason, "indeterminate_write")
                    self.assertEqual(len(session.browser.done), 1)
                    receipts = session.ledger.of_kind("action_receipt")
                    self.assertEqual(receipts[0]["command_id"], "c1")
                    self.assertEqual(receipts[0]["write_attribution"], "unattributed")

    def test_a_late_permit_before_second_dispatch_stops_the_dispatch(self):
        control = _Control([[], []])

        class Chat(_Chat):
            def complete(self, *args, **kwargs):
                if len(self.seen) == 1:
                    control._events.append(PERMIT)
                return super().complete(*args, **kwargs)

        result, session = _run(Chat([SUBMIT, SUBMIT, FINISH]), _Browser([{}, {}]), control)
        self.assertEqual(result.terminal_reason, "indeterminate_write")
        self.assertEqual(len(session.browser.done), 1)

    def test_pending_write_survives_observation_transport_loss(self):
        result, _ = _run(_Chat([SUBMIT, ("observe", {}), FINISH]),
                        _Browser([{}, {"raise": True}]), _Control([[], [PERMIT]]))
        self.assertEqual(result.terminal_reason, "indeterminate_write")

    def test_pending_write_blocks_click_and_get_navigation_but_allows_observation(self):
        class LinkedBrowser(_Browser):
            def execute(self, *args, **kwargs):
                receipt = super().execute(*args, **kwargs)
                receipt.observation.elements.append(
                    ElementRef("e2", "link", "Next", "a", receipt.observation.epoch,
                               {"href": "/y"}))
                return receipt

        # `/y` is the exact link exposed by the synthetic current page.  An invented route is
        # rejected before dispatch by the page-grounding boundary and therefore does not test
        # the pending-write interlock.
        for action in (SUBMIT, ("goto", {"url": "/y"}), ("back", {})):
            browser = LinkedBrowser([{}, {}, {}]) if action[0] == "goto" else _Browser([{}, {}, {}])
            result, session = _run(_Chat([SUBMIT, ("observe", {}), action, FINISH]),
                browser, _Control([[], [PERMIT], []]))
            self.assertEqual(result.terminal_reason, "indeterminate_write")
            self.assertEqual([a for a, _ in session.browser.done], ["click", "observe"])

    def test_late_refusal_is_reconciled_even_on_step_budget_exit(self):
        class Control(_Control):
            def outcome(self):
                # Delivered after the sixth action, when no further model call is allowed.
                if self.reads >= 7 and REFUSAL not in self._events:
                    self._events.append(REFUSAL)
                return super().outcome()

        result, _ = _run(_Chat([SUBMIT] + [("observe", {})] * 5), _Browser([{}] * 6),
                        Control([[], [PERMIT]] + [[]] * 5))
        self.assertEqual(result.terminal_reason, "indeterminate_write")
        self.assertEqual(result.write_refusals, 1)

    def test_late_mixed_decisions_override_finish(self):
        control = _Control([[], [PERMIT]])

        class Chat(_Chat):
            def complete(self, *args, **kwargs):
                if len(self.seen) == 1:
                    control._events.append(REFUSAL)
                return super().complete(*args, **kwargs)

        result, _ = _run(Chat([SUBMIT, FINISH]), _Browser([{}]), control)
        self.assertEqual(result.terminal_reason, "indeterminate_write")
        self.assertEqual(result.write_refusals, 1)
        self.assertEqual(result.success_claim, "unverified")

    def test_a_refusal_before_a_later_permit_is_not_a_partial_write(self):
        result, _ = _run(_Chat([SUBMIT, SUBMIT, FINISH]), _Browser([{}, {}]),
                        _Control([[], [REFUSAL], [PERMIT]]))
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(result.success_claim, "unverified")
