"""Write outcomes preserve the difference between authorisation and application state.

The control gateway records `permit_consumed` when it decides to allow a request, before a
proxy forwards it. The event proves only that the permit was spent; it does not prove that the
request arrived, the application accepted it, or anything was committed. These deterministic
tests use synthetic faults and perform no real application write.
"""
import unittest

from web_operation_agent.controlplane import ControlPlaneClient
from web_operation_agent.grounding import (WRITE_MIXED, WRITE_NONE, WRITE_PERMITTED,
                                           WRITE_REFUSED, WRITE_UNKNOWN)

PERMIT = {"event": "permit_consumed", "webapp": "ordermgr", "uri": "addShippingAddress"}
REFUSAL = {"event": "write_blocked", "webapp": "ordermgr", "uri": "updateOrderContactMech"}
ALLOWED = {"event": "allowed", "webapp": "common-js", "uri": "SetTimeZoneFromBrowser"}


class OneWindowOneMeaning(unittest.TestCase):
    """`outcome_of` classifies a window of control decisions."""

    def test_a_permit_alone_is_permitted(self):
        self.assertEqual(ControlPlaneClient.outcome_of([PERMIT])["state"], WRITE_PERMITTED)

    def test_a_refusal_alone_is_refused(self):
        self.assertEqual(ControlPlaneClient.outcome_of([REFUSAL])["state"], WRITE_REFUSED)

    def test_nothing_relevant_is_none(self):
        self.assertEqual(ControlPlaneClient.outcome_of([ALLOWED])["state"], WRITE_NONE)

    def test_mixed_permit_and_refusal(self):
        """A permit and refusal together mean a partial or uncertain change, not success."""
        out = ControlPlaneClient.outcome_of([PERMIT, REFUSAL])
        self.assertEqual(out["state"], WRITE_MIXED)
        self.assertEqual((out["permitted"], out["refused"]), (1, 1))

    def test_every_decision_keeps_its_own_identity(self):
        """No collapsing: the report must be able to name what was allowed and what was not."""
        out = ControlPlaneClient.outcome_of([PERMIT, REFUSAL])
        self.assertEqual(out["permitted_requests"], ["ordermgr/addShippingAddress"])
        self.assertEqual(out["refused_requests"], ["ordermgr/updateOrderContactMech"])

    def test_two_permits_in_one_window_are_both_kept(self):
        other = dict(PERMIT, uri="createPostalAddress")
        out = ControlPlaneClient.outcome_of([PERMIT, other])
        self.assertEqual(out["permitted"], 2)
        self.assertEqual(len(out["permitted_requests"]), 2)


class WhatTheModelIsTold(unittest.TestCase):
    """The message after a write must state the authorisation, never promise a save."""

    def _rendered(self, state, ok=True, error_code=""):
        from web_operation_agent.schemas import (SCHEMA_VERSIONS, ActionReceipt, ElementRef,
                                                 PageObservation)
        from web_operation_agent.session import ServerAgentSession
        obs = PageObservation(schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=1,
                              url="http://app/x", title="T", landmarks=[],
                              elements=[ElementRef("e1", "link", "L", "a", 1)], messages=[])
        rec = ActionReceipt(schema_version=SCHEMA_VERSIONS["ActionReceipt"], command_id="c",
                            action="click", args_echo={}, ok=ok, error_code=error_code,
                            url_before="a", url_after="b", navigated=True)
        fixture = SessionFixture()
        try:
            return fixture.session()._render(obs, rec, "", {"state": state})
        finally:
            fixture.close()

    def test_it_does_not_say_the_action_wrote_to_the_application(self):
        text = self._rendered(WRITE_PERMITTED).lower()
        self.assertNotIn("wrote to the application", text)

    def test_it_reports_authorisation_without_claiming_forwarding(self):
        text = self._rendered(WRITE_PERMITTED).lower()
        self.assertIn("authorised", text)
        self.assertNotIn("authorised and sent", text)

    def test_it_asks_the_agent_to_check_rather_than_assume(self):
        """The finish prompt is worth keeping -- it is what stopped runs burning their whole
        budget -- but it must be conditional on the page, not on the permit."""
        text = self._rendered(WRITE_PERMITTED).lower()
        self.assertIn("check", text)
        self.assertIn("finish", text)

    def test_a_refusal_reports_only_that_request_was_not_forwarded(self):
        self.assertIn("That request was not forwarded", self._rendered(WRITE_REFUSED))
        self.assertNotIn("nothing was saved", self._rendered(WRITE_REFUSED))


if __name__ == "__main__":
    unittest.main()


# -- the real loop -----------------------------------------------------------------------
import tempfile                                                              # noqa: E402
from pathlib import Path                                                     # noqa: E402

from web_operation_agent.browser import TransportError                       # noqa: E402
from web_operation_agent.controlplane import ControlPlaneError               # noqa: E402
from web_operation_agent.grounding import PlanState                          # noqa: E402
from web_operation_agent.tests.route_fixture import (                # noqa: E402
    FIXTURE_ROUTE_PORT, fixture_route_identity)
from web_operation_agent.ledger import SessionLedger                         # noqa: E402
from web_operation_agent.schemas import (SCHEMA_VERSIONS, ActionReceipt,     # noqa: E402
                                         ElementRef, OperationContract, PageObservation)
from web_operation_agent.session import ServerAgentSession, TaskSpec         # noqa: E402
from web_operation_agent.tests.test_session import _Chat, _Control           # noqa: E402
from web_operation_agent.tests.chat_port_contract import (                   # noqa: E402
    CancellableChatDouble)
from web_operation_agent.tests.session_fixture import SessionFixture

CONTRACT = OperationContract.from_json({
    "schema_version": 1, "task_ref": "T", "summary": "add the address",
    "steps": [{"intent": "open the order", "webapp": "ordermgr", "request": "orderview",
               "landmark": "Order", "citations": ["a.xml:1-2"]}],
    "mutation": {"webapp": "ordermgr", "request": "addShippingAddress",
                 "citations": ["a.xml:1-2"]},
    "citations": ["a.xml:1-2"]})


def _plan_state(value):
    return PlanState.of(value, route_identity=fixture_route_identity)


def _page(url="http://app/ordermgr/do/orderview", title="Order"):
    return PageObservation(schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=1, url=url,
                           title=title, landmarks=[title],
                           elements=[ElementRef("e1", "button", "Save", "button", 1)],
                           messages=[])


class _Browser:
    """Scripted receipts, so a timeout or a lost transport can be produced exactly."""

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
        return ActionReceipt(schema_version=SCHEMA_VERSIONS["ActionReceipt"],
                             command_id=f"c{len(self.done)}", action=action,
                             args_echo=dict(args or {}), ok=spec.get("ok", True),
                             error_code=spec.get("error_code", ""),
                             error_detail=spec.get("error_detail", ""),
                             url_before="http://app/a", url_after="http://app/b",
                             navigated=spec.get("navigated", True), observation=_page())

    def console(self):
        return []

    def close(self):
        pass


class _DeadControl(_Control):
    """Readable for the run's setup, then gone -- the case where nothing can be proved."""

    def outcome(self):
        raise ControlPlaneError("gateway unreachable")


def _run(chat, browser, control):
    with tempfile.TemporaryDirectory() as tmp:
        s = ServerAgentSession(
            task=TaskSpec(task_ref="T", prompt="change the delivery address"), arm="S",
            chat=chat, browser=browser, control=control,
            ledger=SessionLedger(Path(tmp) / "l.jsonl", "r1"),
            step_budget=6, wall_budget_s=120, run_id="r1",
            application_routes=FIXTURE_ROUTE_PORT)
        s.contract = CONTRACT
        s.contract_source = "compiled"
        s.plan_state = _plan_state(CONTRACT)
        s.entry_observation = browser.pages[0] if hasattr(browser, "pages") else _page()
        return s.run(), s


SUBMIT = ("click", {"ref": "e1", "expect_navigation": True})
FINISH = ("finish", {"done": True, "why": "saved the address"})


class PermitThenReceiptTimeout(unittest.TestCase):
    """A spent permit without a browser receipt is indeterminate and cannot be retried."""

    def _go(self):
        chat = _Chat([SUBMIT, SUBMIT, FINISH])
        browser = _Browser([{"ok": False, "error_code": "navigation_timeout",
                             "error_detail": "no navigation within 90s"}])
        # The first window is consumed by the session's pre-loop watermark read.
        control = _Control([[], [PERMIT]])
        return _run(chat, browser, control)

    def test_the_run_stops_as_indeterminate(self):
        result, _ = self._go()
        self.assertEqual(result.terminal_reason, "indeterminate_write")

    def test_it_does_not_attempt_a_second_write(self):
        _, session = self._go()
        self.assertEqual(len(session.browser.done), 1,
                         "a write whose result is unknown must not be followed by another")

    def test_it_never_reports_the_task_finished(self):
        result, _ = self._go()
        self.assertNotEqual(result.terminal_reason, "agent_finished")

    def test_the_plan_is_not_declared_fulfilled(self):
        _, session = self._go()
        self.assertNotIn("fulfilled", session.plan_state.withdrawn_because)


class TransportAndControlLost(unittest.TestCase):
    """Losing both a write-capable action and control trace leaves dispatch uncertain."""

    def test_a_lost_write_capable_action_is_indeterminate(self):
        result, _ = _run(_Chat([SUBMIT, FINISH]), _Browser([{"raise": True}]),
                         _DeadControl([[], []]))
        self.assertEqual(result.terminal_reason, "indeterminate_write")

    def test_a_lost_read_only_action_is_still_a_transport_error(self):
        """A read-only transport failure cannot become an unknown write."""
        result, _ = _run(_Chat([("observe", {}), FINISH]), _Browser([{"raise": True}]),
                         _DeadControl([[], []]))
        self.assertEqual(result.terminal_reason, "transport_error")


class MixedWindowStopsTheRun(unittest.TestCase):
    """One request allowed and another refused in the same window is a partial write, which
    this project stops on rather than scores (AGENTS.md)."""

    def test_it_is_indeterminate(self):
        result, _ = _run(_Chat([SUBMIT, FINISH]), _Browser([{}]), _Control([[], [PERMIT, REFUSAL]]))
        self.assertEqual(result.terminal_reason, "indeterminate_write")

    def test_the_reason_names_both_decisions(self):
        result, _ = _run(_Chat([SUBMIT, FINISH]), _Browser([{}]), _Control([[], [PERMIT, REFUSAL]]))
        self.assertIn("refus", result.detail.lower())


class TheOrdinaryPathsStillWork(unittest.TestCase):
    """The repairs must not turn every run into an indeterminate one."""

    def test_a_clean_permitted_write_continues(self):
        result, session = _run(_Chat([SUBMIT, FINISH]), _Browser([{}, {}]), _Control([[], [PERMIT], []]))
        self.assertEqual(result.terminal_reason, "agent_finished")

    def test_a_clean_refusal_continues(self):
        result, _ = _run(_Chat([SUBMIT, ("finish", {"done": False, "why": "refused"})]),
                         _Browser([{}, {}]), _Control([[], [REFUSAL], []]))
        self.assertEqual(result.terminal_reason, "agent_refused")

    def test_a_proven_pre_dispatch_rejection_continues(self):
        result, _ = _run(_Chat([SUBMIT, FINISH]),
                         _Browser([{"ok": False, "error_code": "stale_ref"}, {}]),
                         _Control([[], [], []]))
        self.assertEqual(result.terminal_reason, "agent_finished")


class WhatTheRunSaysAboutTheAgentsClaim(unittest.TestCase):
    """`agent_finished` means the agent stopped saying it was done. Whether that claim
    agrees with enforcement evidence is a separate fact. Repeating a contradicted claim must
    not turn it into a confirmed success."""

    def test_a_finish_with_no_write_evidence_is_unverified_not_confirmed(self):
        result, _ = _run(_Chat([FINISH]), _Browser([{}]), _Control([[], []]))
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(result.success_claim, "unverified")

    def test_a_claim_the_enforcement_point_contradicts_is_disconfirmed(self):
        """Every write refused, none permitted: the run may not present that as success."""
        result, _ = _run(_Chat([SUBMIT, FINISH, FINISH]), _Browser([{}, {}, {}]),
                         _Control([[], [REFUSAL], [], []]))
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(result.success_claim, "disconfirmed")
        self.assertTrue(result.challenged_false_success,
                        "the first claim must have been challenged before the second stood")

    def test_declining_to_claim_success_is_its_own_state(self):
        result, _ = _run(_Chat([("finish", {"done": False, "why": "cannot"})]),
                         _Browser([{}]), _Control([[], []]))
        self.assertEqual(result.success_claim, "not_claimed")

    def test_authorisation_counts_alone_cannot_confirm_success(self):
        """Counts cannot confirm a save. Future same-actor UI verification is allowed;
        the hidden evaluator remains outside the product boundary."""
        from web_operation_agent.session import ServerAgentSession
        s = object.__new__(ServerAgentSession)
        seen = set()
        for done in (True, False):
            for refusals in (0, 1, 3):
                for permits in (0, 1, 2):
                    s.write_refusals, s.writes_permitted = refusals, permits
                    seen.add(ServerAgentSession._success_claim(s, done))
        self.assertEqual(seen, {"not_claimed", "unverified", "disconfirmed"})


class ThePageOnlyArmIsNotToldItHasSource(unittest.TestCase):
    """Describing the control arm's own plan as compiled from source would have named it the
    treatment inside its own prompt."""

    def _preamble(self, source):
        from web_operation_agent.session import ServerAgentSession, _PLAN_MARK
        s = object.__new__(ServerAgentSession)
        s.contract_source = source
        s.contract = CONTRACT
        s.plan_state = _plan_state(CONTRACT)
        return ServerAgentSession._plan_block(s) or ""

    def test_the_source_arm_is_told_the_plan_came_from_source(self):
        self.assertIn("source code", self._preamble("compiled"))

    def test_the_page_only_arm_is_not(self):
        text = self._preamble("page")
        self.assertNotIn("compiled from this application's source code", text)
        self.assertIn("no access to", text)

    def test_both_are_told_the_page_decides(self):
        for source in ("compiled", "page"):
            self.assertIn("the page in front of you decides", self._preamble(source))


class TheEvidenceThePlannerHadIsRecorded(unittest.TestCase):
    """A contract can only be judged against the facts the planner actually saw. The ledger
    recorded how many lines there were and not what they said, so reading a result afterwards
    meant re-running a retriever whose query expander is a model call and need not agree."""

    def test_the_lines_are_written_not_just_counted(self):
        """Drives the real planning stage and reads the ledger it wrote."""
        import json as _json
        from web_operation_agent.contract import EvidenceBundle
        from web_operation_agent.session import ServerAgentSession

        class _PlanChat(CancellableChatDouble):
            def __init__(self):
                class _M:
                    def report(self_inner):
                        return {"provider_calls": 0, "cost_cny": 0.0}
                self.meter = _M()

            def complete(self, messages, tools, run_id, max_tokens=None, options=None, timeout_s=None):
                return {"content": _json.dumps({
                    "task_ref": "T", "summary": "s", "entry_candidates": [], "steps": [],
                    "mutation": None, "citations": []}),
                    "finish_reason": "stop", "reasoning": ""}

        fixture = SessionFixture()
        try:
            session = fixture.session(
                application_routes=FIXTURE_ROUTE_PORT, chat=_PlanChat(),
                task=TaskSpec(task_ref="T", prompt="do the thing"), plan_from_page=True)
            session.entry_observation = _page()
            session._plan()
            events = session.ledger.of_kind("evidence_retrieved")
            self.assertEqual(len(events), 1)
            self.assertIn("evidence", events[0],
                          "the ledger must record what the planner saw, not only how much")
        finally:
            fixture.close()

    def test_the_count_and_the_content_do_not_collide(self):
        """`stats` already carries a `lines` count; the content needs its own key."""
        from web_operation_agent.contract import EvidenceBundle
        b = EvidenceBundle(task_ref="T", lines=["[a.xml:1-2] a fact"])
        b.stats = {"lines": 1, "citations": 1}
        payload = {**b.stats, "evidence": list(b.lines)}
        self.assertEqual(payload["lines"], 1)
        self.assertEqual(payload["evidence"], ["[a.xml:1-2] a fact"])
