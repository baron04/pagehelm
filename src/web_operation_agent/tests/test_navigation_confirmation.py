"""The assistant asks before it leaves the current page (P7.5 owner ruling D7).

Leaving a page can discard what the person typed there or let the application save it on the
way out -- Odoo 18 saves a changed form when its view is left, which is how a prefilled
customer draft became a customer nobody approved (P7.5 CR-02) -- and it loses the page's own
state. The server sees none of that, so every switch is confirmed, whatever the application.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from web_operation_agent import schemas as S
from web_operation_agent.business.runtime import FormDraft, FormDraftField
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.panel_view import panel_contract
from web_operation_agent.schemas import ActionReceipt
from web_operation_agent.execution.session import NavigationPlan, ServerAgentSession, TaskSpec
from web_operation_agent.tests.route_fixture import FIXTURE_ROUTE_PORT
from web_operation_agent.tests.test_session import (ApplicationIssuedFormPrefill, _Browser,
                                                    _Chat, _Control, _obs)


@dataclass
class _Grant:
    decision: str
    plan_digest: str
    approval_id: str = "approval-nav"


class _Person:
    """Answers each confirmation the way a test says, and remembers what it was asked."""

    def __init__(self, *decisions: str, on_ask=None):
        self.decisions = list(decisions)
        self.asked: list[NavigationPlan] = []
        self.on_ask = on_ask

    def __call__(self, plan, *, context=None):
        self.asked.append(plan)
        if self.on_ask is not None:
            self.on_ask()
        decision = self.decisions.pop(0) if self.decisions else "approved"
        return _Grant(decision=decision, plan_digest=plan.content_digest)


class _StaleFirstBrowser(_Browser):
    """The first dispatch is refused before it reaches the page, as when the document was
    replaced while the model decided; the next one navigates."""

    def execute(self, action, args=None, expect_navigation=False):
        if not self.done:
            self.done.append((action, args))
            return ActionReceipt(
                schema_version=1, command_id="c-stale", action=action,
                args_echo=dict(args or {}), ok=False, error_code="stale_observation",
                url_before=self.pages[0].url, url_after=self.pages[0].url,
                navigated=False, observation=self.pages[0])
        return super().execute(action, args, expect_navigation)


def _session(tmp, *, chat, browser, person, cancelled=None, task="open the other page"):
    session = ServerAgentSession(
        task=TaskSpec("T", task), chat=chat, browser=browser,
        control=_Control([[], [], [], []]),
        ledger=SessionLedger(Path(tmp) / "audit.jsonl", "navigation"),
        navigation_confirmation=person, cancelled=cancelled)
    session.entry_observation = browser.pages[0]
    return session


def _events(tmp) -> list[str]:
    return [json.loads(line)["event"]
            for line in (Path(tmp) / "audit.jsonl").read_text().splitlines()]


class TheSessionAsksBeforeLeavingThePage(unittest.TestCase):
    def test_a_confirmed_switch_is_dispatched_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            person = _Person("approved")
            browser = _Browser([_obs(1), _obs(2, "http://app/y")])
            session = _session(tmp, chat=_Chat([("goto", {"url": "/y"})]),
                               browser=browser, person=person)

            result = session.run()

            self.assertEqual(browser.done[0], ("goto", {"url": "/y"}))
            self.assertEqual([(plan.action, plan.url) for plan in person.asked],
                             [("goto", "/y")])
            self.assertEqual(result.navigation_control,
                             {"requested": 1, "confirmed": 1, "not_confirmed": 0})
            events = _events(tmp)
            self.assertLess(events.index("navigation_confirmed"),
                            events.index("action_receipt"))
            # A page switch is not a write approval, and nothing that counts approvals as
            # writes may see it as one.
            self.assertEqual(result.business_control["approval_requested"], 0)

    def test_an_unconfirmed_switch_is_never_dispatched(self):
        for decision in ("expired", "denied"):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as tmp:
                browser = _Browser([_obs(1), _obs(2, "http://app/y")])
                session = _session(tmp, chat=_Chat([("goto", {"url": "/y"})]),
                                   browser=browser, person=_Person(decision))

                result = session.run()

                self.assertEqual(result.terminal_reason, "navigation_not_confirmed")
                self.assertEqual(browser.done, [])
                self.assertEqual(result.navigation_control["not_confirmed"], 1)
                self.assertIn("navigation_not_confirmed", _events(tmp))

    def test_stopping_while_the_card_waits_ends_the_turn_without_leaving(self):
        stopped = [False]
        with tempfile.TemporaryDirectory() as tmp:
            browser = _Browser([_obs(1), _obs(2, "http://app/y")])
            person = _Person("denied", on_ask=lambda: stopped.__setitem__(0, True))
            session = _session(tmp, chat=_Chat([("goto", {"url": "/y"})]),
                               browser=browser, person=person,
                               cancelled=lambda: stopped[0])

            result = session.run()

            self.assertEqual(result.terminal_reason, "user_cancelled")
            self.assertEqual(browser.done, [])

    def test_opening_the_page_already_shown_is_not_a_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            person = _Person()
            # The current page is /y, and its own link points at /y.
            browser = _Browser([_obs(1, "http://app/y"), _obs(2, "http://app/y")])
            session = _session(tmp, chat=_Chat([("goto", {"url": "/y"})]),
                               browser=browser, person=person)

            session.run()

            self.assertEqual(person.asked, [])
            self.assertEqual(browser.done[0][0], "goto")

    def test_a_retry_after_a_pre_dispatch_refusal_is_not_asked_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            person = _Person("approved")
            browser = _StaleFirstBrowser([_obs(1), _obs(2, "http://app/y")])
            session = _session(
                tmp, chat=_Chat([("goto", {"url": "/y"}), ("goto", {"url": "/y"})]),
                browser=browser, person=person)

            session.run()

            self.assertEqual(len(person.asked), 1)
            self.assertEqual([action for action, _args in browser.done], ["goto", "goto"])

    def test_a_switch_that_reached_the_page_is_asked_about_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            person = _Person("approved", "approved")
            browser = _Browser([_obs(1), _obs(2, "http://app/y"), _obs(3, "http://app/x")])
            session = _session(
                tmp, chat=_Chat([("goto", {"url": "/y"}), ("back", {})]),
                browser=browser, person=person)

            session.run()

            self.assertEqual([plan.action for plan in person.asked], ["goto", "back"])

    def test_opening_the_form_for_a_prefill_is_a_switch_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            form = ApplicationIssuedFormPrefill._form
            for decision, dispatched in (("approved", ["goto", "fill"]), ("expired", [])):
                with self.subTest(decision=decision):
                    person = _Person(decision)
                    browser = _Browser([_obs(1), form(2), form(3)])
                    session = ServerAgentSession(
                        task=TaskSpec("T", "填写客户但不要提交"), chat=_Chat([]),
                        browser=browser, control=_Control([[], [], []]),
                        ledger=SessionLedger(Path(tmp) / f"{decision}.jsonl", "prefill"),
                        application_routes=FIXTURE_ROUTE_PORT, allow_form_prefill=True,
                        navigation_confirmation=person)
                    draft = FormDraft(route="/accounting/do/createCustomer",
                                      fields=(FormDraftField("name", "客户名称", "NovaForge"),))

                    _page, reason, _detail = session._apply_form_draft(
                        draft, browser.pages[0], step=1)

                    self.assertEqual([plan.url for plan in person.asked],
                                     ["/accounting/do/createCustomer"])
                    self.assertEqual([action for action, _args in browser.done], dispatched)
                    self.assertEqual(reason,
                                     "" if decision == "approved" else "navigation_not_confirmed")


class TheCardIsItsOwnKind(unittest.TestCase):
    def test_a_navigation_request_round_trips_and_an_unknown_kind_is_refused(self):
        request = S.ApprovalRequest(
            schema_version=1, approval_id="approval-nav", effect="离开当前页面，打开另一个页面",
            targets=("将打开：/y",), expires_at=2_000_000_000_000, kind="navigation")
        self.assertEqual(S.ApprovalRequest.from_json(request.to_json()), request)
        legacy = request.to_json()
        del legacy["kind"]
        self.assertEqual(S.ApprovalRequest.from_json(legacy).kind, "write",
                         "a request recorded before kinds existed was a write approval")
        with self.assertRaises(S.SchemaError):
            S.ApprovalRequest(schema_version=1, approval_id="approval-x", effect="e",
                              targets=("t",), expires_at=2_000_000_000_000, kind="maybe")

    def test_the_panel_contract_and_the_wire_type_know_the_same_kinds(self):
        kinds = {name for name in panel_contract()["approval_kinds"] if name != "comment"}
        self.assertEqual(kinds, set(S.APPROVAL_KINDS))
        self.assertFalse(panel_contract()["approval_kinds"]["navigation"]["declinable"])
        self.assertTrue(panel_contract()["approval_kinds"]["write"]["declinable"])


if __name__ == "__main__":
    unittest.main()
