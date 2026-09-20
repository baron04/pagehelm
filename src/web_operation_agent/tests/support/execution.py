"""Shared execution test doubles; this module contains no test cases."""
from __future__ import annotations

import json
from pathlib import Path

from web_operation_agent.business.controlplane import ControlPlaneClient, ControlPlaneError
from web_operation_agent.execution.grounding import PlanState
from web_operation_agent.execution.session import LabApparatus, ServerAgentSession, TaskSpec
from web_operation_agent.schemas import (SCHEMA_VERSIONS, ActionReceipt, ElementRef,
                                         PageObservation)
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.chat_port_contract import CancellableChatDouble
from web_operation_agent.tests.support.route_fixture import (
    FIXTURE_ROUTE_PORT, fixture_route_identity,
)


def observation(epoch: int = 1, url: str = "http://app/x") -> PageObservation:
    return PageObservation(
        schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=epoch, url=url,
        title="T", landmarks=["Head"],
        elements=[ElementRef("e1", "link", "Go", "a", epoch, {"href": "/y"})])


def page(epoch: int, url: str = "http://app/accounting/do/main",
         title: str = "Accounting Manager") -> PageObservation:
    return PageObservation(
        schema_version=1, epoch=epoch, url=url, title=title, landmarks=[title],
        elements=[ElementRef("e1", "link", "Go", "a", epoch)])


def form_page(epoch: int) -> PageObservation:
    return PageObservation(
        schema_version=1, epoch=epoch,
        url="http://app/accounting/do/createCustomer", title="New customer",
        landmarks=["New customer"], elements=[
            ElementRef("e2", "textbox", "Customer name", "input", epoch,
                       {"field": "name"}),
            ElementRef("e3", "textbox", "Email", "input", epoch,
                       {"field": "email", "type": "email"}),
            ElementRef("e4", "button", "Save", "button", epoch),
        ])


class ScriptedChat(CancellableChatDouble):
    def __init__(self, calls):
        self.script = list(calls)
        self.seen = []
        self.timeouts = []

        class _Meter:
            def report(self):
                return {"provider_calls": 0, "cost_cny": 0.0}
        self.meter = _Meter()

    def complete(self, messages, tools, run_id, max_tokens=None, options=None,
                 timeout_s=None):
        self.seen.append([dict(message) for message in messages])
        self.timeouts.append(timeout_s)
        if not self.script:
            return {"tool_calls": [{"id": "z", "function": {
                "name": "finish",
                "arguments": '{"done": false, "why": "out of script"}'}}]}
        name, args = self.script.pop(0)
        return {"tool_calls": [{"id": str(len(self.seen)), "function": {
            "name": name, "arguments": json.dumps(args)}}]}


class ScriptedBrowser:
    def __init__(self, pages):
        self.pages = list(pages)
        self.done = []

    def observe(self):
        return self.pages[0]

    def execute(self, action, args=None, expect_navigation=False):
        after = self.pages[min(len(self.done) + 1, len(self.pages) - 1)]
        self.done.append((action, args))
        return ActionReceipt(
            schema_version=1, command_id=f"c{len(self.done)}", action=action,
            args_echo=dict(args or {}), ok=True, url_before=self.pages[0].url,
            url_after=after.url, navigated=True, observation=after)

    def console(self):
        return []

    def close(self):
        pass


class ScriptedControl:
    """Scripted decision windows, one per action."""

    def __init__(self, windows, unreadable_after=None):
        self.windows = list(windows)
        self.unreadable_after = unreadable_after
        self.reads = 0
        self._events = []
        self.begun = []
        self.ended = []

    def since(self, seen):
        self.reads += 1
        if self.unreadable_after is not None and self.reads > self.unreadable_after:
            raise ControlPlaneError("gateway unreachable")
        window = self.windows.pop(0) if self.windows else []
        self._events += window
        return self._events[seen:], len(self._events)

    @staticmethod
    def outcome_of(events):
        return ControlPlaneClient.outcome_of(events)

    def outcome(self):
        outcome = self.outcome_of(self._events)
        return {
            "permit_consumed": outcome["permitted"],
            "write_blocked": outcome["refused"],
            "consumed_requests": outcome["permitted_requests"],
            "blocked_requests": outcome["refused_requests"],
            "trace_events": len(self._events),
        }

    def begin_run(self, run_id):
        self.begun.append(run_id)

    def end_run(self, run_id):
        self.ended.append(run_id)


def plan_state(value):
    return PlanState.of(value, route_identity=fixture_route_identity)


def session(tmp, chat, browser, control, contract=None):
    result = ServerAgentSession(
        task=TaskSpec(task_ref="T", prompt="do the thing"),
        lab=LabApparatus(arm="S"), chat=chat, browser=browser, control=control,
        ledger=SessionLedger(Path(tmp) / "l.jsonl", "r1"),
        step_budget=6, wall_budget_s=120, run_id="r1",
        application_routes=FIXTURE_ROUTE_PORT,
    )
    if contract is not None:
        result.contract = contract
        result.contract_source = "compiled"
        result.plan_state = plan_state(contract)
    result.entry_observation = browser.pages[0]
    return result


# Short aliases keep existing tests readable while ownership moves out of test modules.
_Browser = ScriptedBrowser
_Chat = ScriptedChat
_Control = ScriptedControl
_obs = observation
_page = page
_plan_state = plan_state
_session = session
