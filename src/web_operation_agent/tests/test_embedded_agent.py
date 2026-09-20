from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from web_operation_agent.embedded_agent import (
    ApprovalDenied,
    ApprovalGate,
    ReadOnlyControl,
)
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.transport import RemoteSessionRegistry
from web_operation_agent.schemas import (
    ApprovalDecision,
    ClientHello,
    ControlPlan,
    ControlTicket,
)


ORIGIN = "http://app.test"
CAPABILITIES = (
    "dom.observe", "dom.click", "dom.fill", "dom.select", "dom.submit",
    "page.goto", "page.back", "page.wait",
)


def _connected_channel(session_id: str):
    registry = RemoteSessionRegistry()
    grant, channel = registry.create(
        allowed_origins=[ORIGIN], session_id=session_id)
    lease = channel.accept_hello(ClientHello(
        schema_version=1,
        session_id=grant.session_id,
        client_id="browser-1",
        runtime_instance_id="document-1",
        runtime="embedded-page-controller",
        capabilities=CAPABILITIES,
        page_origin=ORIGIN,
    ))["lease_id"]
    return grant, channel, lease


def _plan() -> ControlPlan:
    return ControlPlan("plan-1", "task-1", (ControlTicket(
        "order",
        "hiddenAcceptQuote",
        ({"quoteId": "Q-1042"},),
        {"status": "accepted"},
    ),))


class AuthorizationPrimitiveTests(unittest.TestCase):
    def test_approval_gate_rejects_credentials_before_the_ledger(self):
        registry = RemoteSessionRegistry()
        _, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="approval-secret")
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SessionLedger(Path(tmp) / "approval.jsonl", "approval-run")
            gate = ApprovalGate(channel, ledger)
            with self.assertRaisesRegex(Exception, "credential"):
                gate.approve_plan(
                    _plan(),
                    effect="Open callback",
                    targets=("/callback?access_token=raw-secret",),
                    ttl_s=1,
                )
            self.assertFalse(ledger.path.exists())

    def test_approval_binds_public_card_to_one_hidden_plan_digest(self):
        grant, channel, lease = _connected_channel("approval-test")
        plan = _plan()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SessionLedger(Path(tmp) / "approval.jsonl", "approval-run")
            gate = ApprovalGate(channel, ledger)
            answer = {}

            def wait_for_user():
                try:
                    answer["grant"] = gate.approve_plan(
                        plan,
                        effect="Accept supplier quote",
                        targets=("Quote Q-1042",),
                        ttl_s=5,
                    )
                except Exception as exc:
                    answer["error"] = exc

            worker = threading.Thread(target=wait_for_user)
            worker.start()
            event = next(item for item in channel.events_after(
                "e00000001", lease_id=lease, wait_s=1)
                         if item.kind == "approval_required")
            wire = json.dumps(event.to_json())
            self.assertIn("Accept supplier quote", wire)
            for hidden in ("hiddenAcceptQuote", "status", "accepted", "ControlPlan"):
                self.assertNotIn(hidden, wire)
            channel.accept_approval(ApprovalDecision(
                1,
                grant.session_id,
                event.payload["approval_id"],
                "approved",
            ), lease_id=lease)
            worker.join(timeout=2)

            self.assertFalse(worker.is_alive())
            self.assertNotIn("error", answer)
            self.assertEqual(answer["grant"].plan_digest, plan.content_digest)
            ledger_text = ledger.path.read_text()
            self.assertIn(plan.content_digest, ledger_text)
            self.assertNotIn("hiddenAcceptQuote", ledger_text)

    def test_denial_is_returned_or_raised_without_changing_the_plan(self):
        grant, channel, lease = _connected_channel("approval-denied")
        plan = _plan()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SessionLedger(Path(tmp) / "approval.jsonl", "approval-run")
            gate = ApprovalGate(channel, ledger)
            answer = {}

            def wait_for_user():
                try:
                    gate.approve_plan(
                        plan,
                        effect="Accept supplier quote",
                        targets=("Quote Q-1042",),
                        ttl_s=5,
                    )
                except ApprovalDenied as exc:
                    answer["denied"] = exc.grant

            worker = threading.Thread(target=wait_for_user)
            worker.start()
            event = next(item for item in channel.events_after(
                "e00000001", lease_id=lease, wait_s=1)
                         if item.kind == "approval_required")
            channel.accept_approval(ApprovalDecision(
                1,
                grant.session_id,
                event.payload["approval_id"],
                "denied",
            ), lease_id=lease)
            worker.join(timeout=2)

            self.assertFalse(worker.is_alive())
            denied = answer["denied"]
            self.assertEqual(denied.decision, "denied")
            self.assertEqual(denied.plan_digest, plan.content_digest)

    def test_read_only_control_never_reports_a_write(self):
        control = ReadOnlyControl()
        control.begin_run("run-1")
        self.assertEqual(control.since(0), ([], 0))
        self.assertEqual(control.outcome()["permit_consumed"], 0)
        self.assertEqual(control.outcome()["write_blocked"], 0)
        with self.assertRaisesRegex(ValueError, "cannot advance"):
            control.since(1)


if __name__ == "__main__":
    unittest.main()
