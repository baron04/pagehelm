"""Synthetic write-outcome session fixtures; performs no real application writes."""
import tempfile
from pathlib import Path

from web_operation_agent.business.controlplane import ControlPlaneError
from web_operation_agent.execution.grounding import PlanState
from web_operation_agent.execution.session import LabApparatus, ServerAgentSession, TaskSpec
from web_operation_agent.schemas import (
    SCHEMA_VERSIONS, ActionReceipt, ElementRef, OperationContract, PageObservation,
)
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.execution import _Chat, _Control
from web_operation_agent.tests.support.route_fixture import (
    FIXTURE_ROUTE_PORT, fixture_route_identity,
)
from web_operation_agent.transport import TransportError


PERMIT = {"event": "permit_consumed", "webapp": "ordermgr", "uri": "addShippingAddress"}
REFUSAL = {"event": "write_blocked", "webapp": "ordermgr", "uri": "updateOrderContactMech"}
ALLOWED = {"event": "allowed", "webapp": "common-js", "uri": "SetTimeZoneFromBrowser"}
CONTRACT = OperationContract.from_json({
    "schema_version": 1, "task_ref": "T", "summary": "add the address",
    "steps": [{
        "intent": "open the order", "webapp": "ordermgr", "request": "orderview",
        "landmark": "Order", "citations": ["a.xml:1-2"],
    }],
    "mutation": {
        "webapp": "ordermgr", "request": "addShippingAddress",
        "citations": ["a.xml:1-2"],
    },
    "citations": ["a.xml:1-2"],
})
SUBMIT = ("click", {"ref": "e1", "expect_navigation": True})
FINISH = ("finish", {"done": True, "why": "saved the address"})


def _plan_state(value):
    return PlanState.of(value, route_identity=fixture_route_identity)


def _page(url="http://app/ordermgr/do/orderview", title="Order"):
    return PageObservation(
        schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=1, url=url,
        title=title, landmarks=[title],
        elements=[ElementRef("e1", "button", "Save", "button", 1)], messages=[],
    )


class _Browser:
    """Scripted receipts that can reproduce a timeout or lost transport."""

    def __init__(self, receipts):
        self.script = list(receipts)
        self.done = []

    def observe(self):
        return _page()

    def execute(self, action, args=None, expect_navigation=False):
        self.done.append((action, args))
        spec = self.script.pop(0) if self.script else {}
        if spec.get("raise"):
            raise TransportError("browser process ended while awaiting 'receipt'")
        return ActionReceipt(
            schema_version=SCHEMA_VERSIONS["ActionReceipt"],
            command_id=f"c{len(self.done)}", action=action,
            args_echo=dict(args or {}), ok=spec.get("ok", True),
            error_code=spec.get("error_code", ""),
            error_detail=spec.get("error_detail", ""),
            url_before="http://app/a", url_after="http://app/b",
            navigated=spec.get("navigated", True), observation=_page(),
        )

    def console(self):
        return []

    def close(self):
        pass


class _DeadControl(_Control):
    def outcome(self):
        raise ControlPlaneError("gateway unreachable")


def _run(chat, browser, control):
    with tempfile.TemporaryDirectory() as tmp:
        session = ServerAgentSession(
            task=TaskSpec(task_ref="T", prompt="change the delivery address"),
            lab=LabApparatus(arm="S"), chat=chat, browser=browser, control=control,
            ledger=SessionLedger(Path(tmp) / "l.jsonl", "r1"),
            step_budget=6, wall_budget_s=120, run_id="r1",
            application_routes=FIXTURE_ROUTE_PORT,
        )
        session.contract = CONTRACT
        session.contract_source = "compiled"
        session.plan_state = _plan_state(CONTRACT)
        session.entry_observation = (
            browser.pages[0] if hasattr(browser, "pages") else _page()
        )
        return session.run(), session


__all__ = [
    "ALLOWED", "CONTRACT", "FINISH", "PERMIT", "REFUSAL", "SUBMIT",
    "_Browser", "_DeadControl", "_Chat", "_Control", "_page", "_plan_state", "_run",
]
