from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web_operation_agent.business.approvals import ApprovalWatch
from web_operation_agent.business.control import ApprovalGrant
from web_operation_agent.business.runtime import BusinessMutationProposal
from web_operation_agent.schemas import SCHEMA_VERSIONS, PageObservation
from web_operation_agent.state.ledger import SessionLedger


def _observation(url: str = "http://app.test/orders") -> PageObservation:
    return PageObservation(
        schema_version=SCHEMA_VERSIONS["PageObservation"],
        epoch=1,
        url=url,
        title="Orders",
        landmarks=["Orders"],
        elements=[],
        content="",
    )


def _proposal(nonce: str) -> BusinessMutationProposal:
    return BusinessMutationProposal(
        tool="update_order",
        args={"id": 1, "note": "updated"},
        effect="Update order",
        targets=("Order #1",),
        nonce=nonce,
    )


class ApprovalWatchTests(unittest.TestCase):
    def test_operation_consent_carries_across_turns_while_writes_stay_per_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "run-1")
            op_calls: list[str] = []
            write_calls: list[str] = []
            persisted: list[bool] = []

            def confirm_op(plan, *, context=None):
                op_calls.append(plan.action)
                return ApprovalGrant(
                    approval_id="op-1",
                    decision="approved",
                    plan_digest=plan.content_digest,
                    decided_at=1.0,
                )

            def approve_write(proposal, *, context=None, offer_prefill=False):
                write_calls.append(proposal.nonce)
                return ApprovalGrant(
                    approval_id=f"w-{len(write_calls)}",
                    decision="approved",
                    plan_digest=proposal.content_digest,
                    decided_at=1.0,
                )

            first_turn = ApprovalWatch(
                ledger=ledger,
                run_id="run-1",
                principal=None,
                session_id="s-1",
                turn_id="t-1",
                business_approval=approve_write,
                operation_confirmation=confirm_op,
                cancelled=lambda: False,
                initial_operation_granted=False,
                on_operation_granted=lambda: persisted.append(True),
            )
            obs = _observation()
            self.assertEqual(first_turn.confirm_operation(1, obs, action="click"), ("", ""))
            self.assertEqual(first_turn.confirm_operation(2, obs, action="fill"), ("", ""))
            self.assertEqual(op_calls, ["click"])
            self.assertEqual(persisted, [True])

            second_turn = ApprovalWatch(
                ledger=ledger,
                run_id="run-2",
                principal=None,
                session_id="s-1",
                turn_id="t-2",
                business_approval=approve_write,
                operation_confirmation=confirm_op,
                cancelled=lambda: False,
                initial_operation_granted=bool(persisted),
                on_operation_granted=lambda: persisted.append(True),
            )
            self.assertTrue(second_turn.operation_granted)
            self.assertEqual(second_turn.confirm_operation(1, obs, action="click"), ("", ""))
            self.assertEqual(op_calls, ["click"])

            # Every business write approval still asks per write.
            p1 = _proposal("1" * 32)
            p2 = _proposal("2" * 32)
            self.assertTrue(second_turn.approve_mutation(
                p1, step=2, tool=p1.tool, context=None, identity={}).approved)
            self.assertTrue(second_turn.approve_mutation(
                p2, step=3, tool=p2.tool, context=None, identity={}).approved)
            self.assertEqual(write_calls, ["1" * 32, "2" * 32])

            # invalidate_operation still revokes page operation consent within the turn.
            second_turn.invalidate_operation()
            self.assertFalse(second_turn.operation_granted)
            self.assertEqual(second_turn.confirm_operation(4, obs, action="click"), ("", ""))
            self.assertEqual(op_calls, ["click", "click"])


if __name__ == "__main__":
    unittest.main()
