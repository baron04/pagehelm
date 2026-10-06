"""Form description owns a bounded fresh read without replaying its exact opener."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.form_entries import FormEntryTools
from web_operation_agent.business.forms import ApplicationFormTools
from web_operation_agent.execution.budget import TurnBudget
from web_operation_agent.execution.form_tools import describe_form
from web_operation_agent.execution.runstate import _Next, _Turn, RunResult
from web_operation_agent.schemas import ActionReceipt
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.business.test_form_entries import Port, context, page
from web_operation_agent.tests.business.test_generic_forms import _Forms, _observation


class FormPort(Port, _Forms):
    required_scopes = frozenset({"forms:prepare"})

    def __init__(self):
        Port.__init__(self)
        _Forms.__init__(self)
        self.accepted_receipt = None

    def accept_entry(self, entry, before, receipt, observation, *, context):
        if (observation.render_state != "ready" or observation.url != before.url
                or observation.page_route != before.page_route):
            raise BusinessToolRequestError("the exact application form changed")
        self.accepted_receipt = receipt
        return Port.accept_entry(self, entry, before, receipt, observation, context=context)


class FormEntryReadinessTests(unittest.TestCase):
    def setup_form(self, refreshes):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        port, ctx = FormPort(), context()
        before = replace(page(), url="https://app.test/companies/new",
                         page_route="/companies/new")
        tools = FormEntryTools(port, application_id="fixture", require_write_guard=True)
        opaque = tools.discover(before, {}, context=ctx)["entries"][0]["entry_id"]
        entry = tools.plan(before, {"entry_id": opaque}, context=ctx)
        opened = replace(_observation(epoch=2), page_route=before.page_route)
        opener = ActionReceipt(1, "opener", "click", ok=True,
            args_echo={"ref": entry.control.ref, "epoch": before.epoch},
            write_guard_held=True, observation=opened)
        tools.acknowledged(entry, before, opener, context=ctx)
        actions = []
        cancelled = [False]
        def execute(action, args, *, timeout_s):
            actions.append((action, args))
            result = refreshes.pop(0)
            if callable(result):
                return result(action, args)
            return ActionReceipt(1, "read-" + str(len(actions)), action, ok=True,
                                 args_echo=args, observation=result)
        writes = SimpleNamespace(write_outcome=lambda *a, **k: {"state": "none"},
                                 settle_write=lambda result: ("", ""))
        ledger = SessionLedger(Path(self.temp.name)/"audit.jsonl", "readiness")
        session = SimpleNamespace(_form_entry_tools=tools,
            _application_form_tools=ApplicationFormTools(port, application_id="fixture"),
            _approvals=SimpleNamespace(context=lambda *a: ctx),
            browser=SimpleNamespace(execute_with_budget=execute), _browser_attempts=0,
            _cancel_requested=lambda: cancelled[0], _writes=writes,
            _turn_budget=TurnBudget(steps=1, seconds=30), ledger=ledger)
        session._reply = lambda turn, call, feedback: (
            turn.messages.append({"content": feedback}) or _Next())
        session._finish = lambda result, started, reason, detail, **kw: SimpleNamespace(
            terminal_reason=reason, detail=detail)
        turn = _Turn(RunResult("readiness", "T", "U", ""), time.time(), [], opened, step=1)
        return session, turn, port, opener, actions, cancelled

    def describe(self, session, turn):
        return describe_form(session, turn, {}, "describe_form", {"field_query": "company name"})

    def fresh(self, epoch, ready="ready"):
        return replace(_observation(epoch=epoch), render_state=ready,
                       page_route="/companies/new")

    def test_ready_receipt_gets_one_independent_read_and_mints_usable_fresh_bindings(self):
        session, turn, port, opener, actions, _ = self.setup_form([self.fresh(3)])
        self.assertIsNone(self.describe(session, turn).finished)
        self.assertEqual(actions, [("observe", {})])
        self.assertIs(port.accepted_receipt, opener)
        self.assertEqual((port.accepted, port.released), (1, 1))
        self.assertEqual(turn.obs.epoch, 3)
        descriptor = json.loads(turn.messages[-1]["content"].split("\n", 1)[1])
        field = descriptor["fields"][0]
        draft, _ = session._application_form_tools.prepare(turn.obs, {
            "form_id": descriptor["form_id"],
            "values": [{"field_id": field["field_id"], "value": "NovaForge"}]},
            context=session._approvals.context())
        self.assertEqual(draft.fields[0].value, "NovaForge")
        with self.assertRaises(BusinessToolRequestError):
            session._application_form_tools.prepare(opener.observation, {
                "form_id": descriptor["form_id"],
                "values": [{"field_id": field["field_id"], "value": "NovaForge"}]},
                context=session._approvals.context())
        self.assertEqual(len(session.ledger.of_kind("form_entry_bound")), 1)

    def test_only_pending_readiness_uses_bounded_read_only_waits(self):
        session, turn, port, _, actions, _ = self.setup_form([
            self.fresh(3, "pending"), self.fresh(4, "pending"), self.fresh(5)])
        self.assertIsNone(self.describe(session, turn).finished)
        self.assertEqual(actions, [("observe", {}), ("wait_for", {"ms": 100}),
                                   ("wait_for", {"ms": 250})])
        self.assertEqual(port.accepted, 1)
        self.assertTrue(all(e["fresh_observation"] for e in session.ledger.of_kind("action_receipt")))

    def test_pending_deadline_is_bounded_and_never_prepares_values(self):
        session, turn, port, _, actions, _ = self.setup_form([
            self.fresh(epoch, "pending") for epoch in range(3, 7)])
        self.assertIsNone(self.describe(session, turn).finished)
        self.assertEqual(len(actions), 4)
        self.assertEqual(port.accepted, 0)
        self.assertIn("still loading", turn.messages[-1]["content"])
        self.assertFalse(session.ledger.of_kind("application_form_described"))

    def test_failed_or_stale_read_is_terminal_without_entry_admission(self):
        for ok, epoch in ((False, 3), (True, 2)):
            def receipt(action, args):
                return ActionReceipt(1, "bad-read", action, ok=ok, args_echo=args,
                                     observation=self.fresh(epoch))
            session, turn, port, _, actions, _ = self.setup_form([receipt])
            with self.subTest(ok=ok, epoch=epoch):
                self.assertEqual(self.describe(session, turn).finished.terminal_reason, "transport_error")
                self.assertEqual(port.accepted, 0)
                self.assertEqual(actions, [("observe", {})])

    def test_blocked_or_changed_form_refuses_exact_admission_without_another_read(self):
        for change in ({"render_state": "blocked"}, {"url": "https://app.test/another"}):
            session, turn, port, _, actions, _ = self.setup_form([replace(self.fresh(3), **change)])
            with self.subTest(change=change):
                self.assertIsNone(self.describe(session, turn).finished)
                self.assertEqual(actions, [("observe", {})])
                self.assertEqual((port.accepted, port.released), (0, 1))
                self.assertIsNone(session._form_entry_tools.pending)
                self.assertIn("exact application form changed", turn.messages[-1]["content"])

    def test_cancellation_and_uncertain_write_stop_before_any_form_admission(self):
        session, turn, port, _, actions, cancelled = self.setup_form([self.fresh(3)])
        cancelled[0] = True
        self.assertEqual(self.describe(session, turn).finished.terminal_reason, "user_cancelled")
        self.assertEqual(actions, [])
        self.assertEqual(port.accepted, 0)
        session, turn, port, _, actions, _ = self.setup_form([self.fresh(3)])
        session._writes.settle_write = lambda result: ("indeterminate_write", "unknown external write")
        self.assertEqual(self.describe(session, turn).finished.terminal_reason, "indeterminate_write")
        self.assertEqual(actions, [("observe", {})])
        self.assertEqual(port.accepted, 0)
        self.assertEqual(len(session.ledger.of_kind("action_receipt")), 1)

    def test_actor_change_never_refreshes_or_transfers_the_acknowledged_entry(self):
        session, turn, port, _, actions, _ = self.setup_form([self.fresh(3)])
        session._approvals.context = lambda *a: context("another")
        self.assertIsNone(self.describe(session, turn).finished)
        self.assertEqual(actions, [])
        self.assertEqual((port.accepted, port.released), (0, 1))
        self.assertIsNone(session._form_entry_tools.pending)
        self.assertIn("actor or turn changed", turn.messages[-1]["content"])

    def test_shared_deadline_keeps_the_late_read_audit_without_admission_or_another_read(self):
        session, turn, port, _, actions, _ = self.setup_form([self.fresh(3)])
        clock = [0.0]
        session._turn_budget = TurnBudget(steps=1, seconds=1, clock=lambda: clock[0])
        original = session.browser.execute_with_budget
        def late(*args, **kwargs):
            receipt = original(*args, **kwargs)
            clock[0] = 2.0
            return receipt
        session.browser.execute_with_budget = late
        self.assertEqual(self.describe(session, turn).finished.terminal_reason, "wall_clock_budget")
        self.assertEqual(actions, [("observe", {})])
        self.assertEqual(port.accepted, 0)
        self.assertEqual(turn.obs.epoch, 3)
        self.assertEqual(len(session.ledger.of_kind("action_receipt")), 1)


if __name__ == "__main__":
    unittest.main()
