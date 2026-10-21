"""Time spent waiting on a person's answer is theirs, not the turn's.

A turn has a wall-clock budget for the assistant's own work. A confirmation used to spend
it: with a two-minute turn budget, a card asked for after a minute of work could be
answered for at most a minute, whatever its own validity said. The wait now moves the
turn's deadline instead, and the operation waiting on the answer outlives that wait.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from web_operation_agent.business.approvals import ApprovalWatch
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.execution.product_support import _principal
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control, _obs


@dataclass
class _Grant:
    decision: str
    plan_digest: str
    approval_id: str = "approval-nav"


class _SlowPerson:
    """Takes `seconds` to approve, and remembers the context it was asked under."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.contexts = []

    def __call__(self, plan, *, context=None):
        self.contexts.append(context)
        time.sleep(self.seconds)
        return _Grant(decision="approved", plan_digest=plan.content_digest)


class WaitingOnAPersonMovesTheDeadline(unittest.TestCase):
    def test_the_wait_is_given_back_and_the_waiting_operation_outlives_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            person = _SlowPerson(0.2)
            watch = ApprovalWatch(
                ledger=SessionLedger(Path(tmp) / "audit.jsonl", "wait"), run_id="run",
                principal=_principal(), session_id="session", turn_id="turn",
                operation_confirmation=person, cancelled=lambda: False,
                operation_hold_s=30)
            deadline = int(time.time() * 1000) + 5_000
            watch.start_turn(deadline)

            reason, _ = watch.confirm_operation(1, _obs(1), action="goto", url="/y")

            self.assertEqual(reason, "")
            # The context the person answered under reaches past the turn's deadline by the
            # longest the answer may take.
            self.assertEqual(person.contexts[0].expires_at, deadline + 30_000)
            self.assertGreaterEqual(watch.waited_ms, 200)
            self.assertEqual(watch.deadline_ms, deadline + watch.waited_ms)
            # An operation that waits on nobody is held to the turn's deadline, as before.
            self.assertEqual(watch.context(2, "read").expires_at, watch.deadline_ms)

    def test_a_turn_is_not_ended_by_the_time_a_person_took(self):
        with tempfile.TemporaryDirectory() as tmp:
            browser = _Browser([_obs(1), _obs(2, "http://app/y")])
            session = ServerAgentSession(
                task=TaskSpec("T", "open the other page"),
                chat=_Chat([("goto", {"url": "/y"})]), browser=browser,
                control=_Control([[], [], [], []]),
                ledger=SessionLedger(Path(tmp) / "audit.jsonl", "wait"),
                operation_confirmation=_SlowPerson(0.4), wall_budget_s=0.3,
                operation_hold_s=30)
            session.entry_observation = browser.pages[0]

            result = session.run()

            self.assertEqual(browser.done[0], ("goto", {"url": "/y"}))
            self.assertNotEqual(result.terminal_reason, "wall_clock_budget")
            self.assertGreaterEqual(session._approvals.waited_ms, 400)


if __name__ == "__main__":
    unittest.main()
