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
from dataclasses import dataclass, replace
from pathlib import Path

from web_operation_agent import schemas as S
from web_operation_agent.business.runtime import FormDraft, FormDraftField
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.transport.panel_view import panel_contract
from web_operation_agent.schemas import ActionReceipt
from web_operation_agent.execution.session import NavigationPlan, ServerAgentSession, TaskSpec
from web_operation_agent.execution.business_tools import apply_form_draft
from web_operation_agent.tests.support.answer import _turn
from web_operation_agent.tests.support.route_fixture import FIXTURE_ROUTE_PORT
from web_operation_agent.tests.support.execution import (
    _Browser, _Chat, _Control, _obs, form_page,
)


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


class _MenuBrowser:
    """A page with a menu toggle (e1), a link inside the menu (e2) and a script-routed
    control (e3). With `close_first`, the first switch is refused as stale: answering the
    confirmation closed the menu it was in."""

    def __init__(self, *, close_first=False, menu_navigates=False, stale_moves_to=""):
        self.url = "http://app/x"
        self.close_first = close_first
        self.menu_navigates = menu_navigates
        self.stale_moves_to = stale_moves_to
        self.done = []
        self.pages = [self._page(1)]

    def _page(self, epoch):
        return S.PageObservation(
            schema_version=1, epoch=epoch, url=self.url, title="T", landmarks=["T"],
            elements=[S.ElementRef("e1", "button", "Apps", "button", epoch),
                      S.ElementRef("e2", "menuitem", "Sales", "a", epoch, {"href": "/y"}),
                      S.ElementRef("e3", "button", "Open", "button", epoch)])

    def observe(self):
        return self._page(len(self.done) + 1)

    def execute(self, action, args=None, expect_navigation=False):
        self.done.append((action, dict(args or {})))
        before = self.url
        ref = (args or {}).get("ref")
        if self.close_first and len(self.done) == 1:
            code = "stale_observation"
            if self.stale_moves_to:
                self.url = self.stale_moves_to
            navigated = self.url != before
        else:
            code = ""
            if action == "goto":
                self.url = "http://app" + (args or {})["url"]
            elif ref == "e2":
                self.url = "http://app/y"
            elif ref == "e1" and self.menu_navigates:
                self.url = "http://app/elsewhere"
            navigated = self.url != before
        return ActionReceipt(
            schema_version=1, command_id=f"c{len(self.done)}", action=action,
            args_echo=dict(args or {}), ok=not code, error_code=code, url_before=before,
            url_after=self.url, navigated=navigated,
            observation=self._page(len(self.done) + 1))

    def console(self):
        return []

    def close(self):
        pass


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
    def test_pending_navigation_becomes_reportable_only_after_the_view_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = _obs(2, "http://app/y")
            pending.render_state = "pending"
            ready = _obs(3, "http://app/y")
            ready.title = "Destination"
            browser = _Browser([_obs(1), pending, ready])
            session = _session(
                tmp,
                chat=_Chat([("click", {"ref": "e1"}), ("observe", {}),
                            ("finish", {"outcome": "acted"})]),
                browser=browser, person=_Person("approved"))
            session._answer_mode = True

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.verified_answer["action"],
                             {"kind": "navigation", "url": "http://app/y"})
            self.assertEqual([name for name, _ in browser.done], ["click", "observe"])

    def test_successful_link_click_can_be_reported_as_completed_navigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            browser = _Browser([_obs(1), _obs(2, "http://app/y")])
            session = _session(
                tmp,
                chat=_Chat([("click", {"ref": "e1"}),
                            ("finish", {"outcome": "acted"})]),
                browser=browser, person=_Person("approved"))
            session._answer_mode = True

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.verified_answer["action"],
                             {"kind": "navigation", "url": "http://app/y"})

    def test_non_navigating_click_cannot_claim_completed_navigation(self):
        class NoNavigation(_Browser):
            def execute(self, action, args=None, expect_navigation=False):
                receipt = super().execute(action, args, expect_navigation)
                return replace(receipt, navigated=False, url_after="http://app/x",
                               observation=_obs(2))

        with tempfile.TemporaryDirectory() as tmp:
            browser = NoNavigation([_obs(1), _obs(2)])
            session = _session(
                tmp,
                chat=_Chat([("click", {"ref": "e1"}),
                            ("finish", {"outcome": "acted"})] * 2),
                browser=browser, person=_Person("approved"))
            session._answer_mode = True

            result = session.run()

            self.assertNotEqual(result.terminal_reason, "agent_finished")
            self.assertIsNone(result.verified_answer)

    def test_read_only_scroll_preserves_completed_navigation_receipt(self):
        class Scrolling(_Browser):
            def execute(self, action, args=None, expect_navigation=False):
                receipt = super().execute(action, args, expect_navigation)
                return (replace(receipt, navigated=False)
                        if action == "scroll" else receipt)

        with tempfile.TemporaryDirectory() as tmp:
            browser = Scrolling([_obs(1), _obs(2, "http://app/y"),
                                 _obs(3, "http://app/y")])
            session = _session(
                tmp,
                chat=_Chat([("click", {"ref": "e1"}),
                            ("scroll", {"direction": "down", "pages": 1}),
                            ("finish", {"outcome": "acted"})]),
                browser=browser, person=_Person("approved"))
            session._answer_mode = True

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.verified_answer["action"],
                             {"kind": "navigation", "url": "http://app/y"})

    def test_failed_followup_click_does_not_erase_completed_navigation(self):
        class SecondClickFails(_Browser):
            def execute(self, action, args=None, expect_navigation=False):
                receipt = super().execute(action, args, expect_navigation)
                if len(self.done) == 2:
                    return replace(receipt, ok=False, error_code="stale_observation",
                                   navigated=False, observation=self.pages[1])
                return receipt

        with tempfile.TemporaryDirectory() as tmp:
            browser = SecondClickFails([_obs(1), _obs(2, "http://app/y")])
            session = _session(
                tmp,
                chat=_Chat([("click", {"ref": "e1"}),
                            ("click", {"ref": "e1"}),
                            ("finish", {"outcome": "acted"})]),
                browser=browser, person=_Person("approved"))
            session._answer_mode = True

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.verified_answer["action"],
                             {"kind": "navigation", "url": "http://app/y"})

    def test_dom_changing_read_only_click_has_interaction_receipt(self):
        class TabClick(_Browser):
            def execute(self, action, args=None, expect_navigation=False):
                receipt = super().execute(action, args, expect_navigation)
                return replace(receipt, navigated=False, observation=self.pages[1])

        with tempfile.TemporaryDirectory() as tmp:
            after = _obs(2)
            after.content = "Terms tab selected"
            browser = TabClick([_obs(1), after])
            session = _session(
                tmp,
                chat=_Chat([("click", {"ref": "e1"}),
                            ("finish", {"outcome": "acted"})]),
                browser=browser, person=_Person("approved"))
            session._answer_mode = True

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.verified_answer["action"]["kind"],
                             "page_interaction")

    def test_answer_repair_distinguishes_control_refs_from_evidence_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            chat = _Chat([
                ("click", {"ref": "e1"}),
                ("finish", {"outcome": "acted", "answer": {
                    "version": 1, "coverage": "complete", "conclusions": [{
                        "kind": "page_observation", "evidence_ref": "e1",
                        "statement": "The page is open"}]}}),
                ("finish", {"outcome": "acted"}),
            ])
            session = _session(tmp, chat=chat,
                               browser=_Browser([_obs(1), _obs(2, "http://app/y")]),
                               person=_Person("approved"))
            session._answer_mode = True

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertIn("Element refs such as e1 are browser controls",
                          str(chat.seen[-1]))

    def test_answer_repair_after_action_cannot_dispatch_another_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            browser = _Browser([_obs(1), _obs(2, "http://app/y")])
            chat = _Chat([
                ("click", {"ref": "e1"}),
                ("finish", {"outcome": "acted", "answer": {
                    "version": 1, "coverage": "complete", "conclusions": [{
                        "kind": "page_observation", "evidence_ref": "e1"}]}}),
                ("click", {"ref": "e1"}),
                ("finish", {"outcome": "acted"}),
            ])
            session = _session(tmp, chat=chat, browser=browser,
                               person=_Person("approved"))
            session._answer_mode = True

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual([action for action, _ in browser.done], ["click"])
            self.assertEqual(len(session.ledger.of_kind("finalizing_tool_refused")), 1)
            self.assertEqual(result.verified_answer["action"]["url"],
                             "http://app/y")

    def test_answer_repair_after_action_may_still_compute(self):
        # An answer refusal can name compute as its repair; it touches no page, so the
        # finalizing turn runs it rather than refusing the repair it asked for.
        with tempfile.TemporaryDirectory() as tmp:
            chat = _Chat([
                ("click", {"ref": "e1"}),
                ("finish", {"outcome": "acted", "answer": {
                    "version": 1, "coverage": "complete", "conclusions": [{
                        "kind": "page_observation", "evidence_ref": "e1"}]}}),
                ("compute", {"operation": "count", "dataset_ref": "query#1"}),
                ("finish", {"outcome": "acted"}),
            ])
            session = _session(tmp, chat=chat,
                               browser=_Browser([_obs(1), _obs(2, "http://app/y")]),
                               person=_Person("approved"))
            session._answer_mode = True
            read = _turn()
            for ref in read.ids():
                session.evidence.record(read.get(ref))

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(session.ledger.of_kind("finalizing_tool_refused"), [])
            self.assertEqual(len(session.ledger.of_kind("answer_computed")), 1)

    def test_answer_repair_can_cite_latest_page_without_another_browser_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            after = _obs(2, "http://app/y")
            after.content = "Terms tab selected"
            browser = _Browser([_obs(1), after])
            chat = _Chat([
                ("click", {"ref": "e1"}),
                ("finish", {"outcome": "acted", "answer": {
                    "version": 1, "coverage": "complete", "conclusions": [{
                        "kind": "page_observation", "evidence_ref": "e1"}]}}),
                ("finish", {"outcome": "acted", "answer": {
                    "version": 1, "coverage": "complete", "conclusions": [{
                        "kind": "page_observation", "evidence_ref": "obs2-page#1"}]}}),
            ])
            session = _session(tmp, chat=chat, browser=browser,
                               person=_Person("approved"))
            session._answer_mode = True

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual([action for action, _ in browser.done], ["click"])
            self.assertIn("Terms tab selected", result.agent_said)
            self.assertEqual(len(session.ledger.of_kind("page_answer_evidence")), 1)

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

    def test_a_menu_link_the_answer_closed_is_opened_by_its_confirmed_address(self):
        # Found on live Odoo: answering the card is a click on the page, the application
        # closes its open menu on any click outside it, and the menu item the person agreed
        # to open is gone by dispatch. The person confirmed that destination and nothing was
        # dispatched, so the server goes there by the link's address in the same step.
        with tempfile.TemporaryDirectory() as tmp:
            person = _Person("approved")
            browser = _MenuBrowser(close_first=True)
            session = _session(tmp, chat=_Chat([
                ("click", {"ref": "e2"}),
                ("finish", {"done": True, "why": "opened"}),
            ]), browser=browser, person=person)

            session.run()

            self.assertEqual(len(person.asked), 1)
            self.assertEqual([action for action, _args in browser.done], ["click", "goto"])
            self.assertEqual(browser.done[1][1], {"url": "/y"})
            self.assertEqual(browser.url, "http://app/y")
            opened = session.ledger.of_kind("confirmed_switch_opened_by_address")
            self.assertEqual([event["reason"] for event in opened],
                             ["confirmation_closed_the_menu"])

    def test_the_address_is_used_only_where_it_is_exactly_what_was_confirmed(self):
        cases = {
            # The page itself moved on while the card waited: the model re-plans.
            "the page moved": dict(browser=_MenuBrowser(close_first=True,
                                                        stale_moves_to="http://app/z"),
                                   first={"ref": "e2"}),
            # A script-routed control has no address to go to.
            "no address": dict(browser=_MenuBrowser(close_first=True),
                               first={"ref": "e3", "expect_navigation": True}),
        }
        for reason, case in cases.items():
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                session = _session(tmp, chat=_Chat([
                    ("click", case["first"]), ("finish", {"done": False, "why": "stop"}),
                ]), browser=case["browser"], person=_Person("approved"))

                session.run()

                self.assertEqual([action for action, _args in case["browser"].done],
                                 ["click"])
                self.assertEqual(
                    session.ledger.of_kind("confirmed_switch_opened_by_address"), [])

    def test_an_answer_is_not_spent_by_an_action_in_between(self):
        # Reopening the menu, or any other action that does not leave the page, leaves the
        # person's answer standing for the switch it was given for.
        with tempfile.TemporaryDirectory() as tmp:
            browser = _MenuBrowser()
            session = _session(tmp, chat=_Chat([]), browser=browser,
                               person=_Person("approved"))
            approvals = session._approvals
            page = browser.observe()
            self.assertEqual(approvals.confirm_navigation(1, page, action="click", url="/y"),
                             ("", ""))

            def receipt(*, navigated=False, code=""):
                return ActionReceipt(schema_version=1, command_id="c", action="click",
                                     args_echo={}, ok=not code, error_code=code,
                                     navigated=navigated)

            approvals.settle_navigation(receipt(code="stale_observation"), navigating=True)
            approvals.settle_navigation(receipt(), navigating=False)
            self.assertTrue(approvals.holds_confirmation(action="click", url="/y"))
            approvals.settle_navigation(receipt(navigated=True), navigating=False)
            self.assertFalse(approvals.holds_confirmation(action="click", url="/y"))

    def test_an_answer_for_a_control_with_no_address_is_spent_by_anything(self):
        # A script-routed control has no known destination, so a standing answer for it
        # would cover the next such control wherever it went.
        with tempfile.TemporaryDirectory() as tmp:
            person = _Person("approved", "approved")
            first = {"ref": "e3", "expect_navigation": True}
            session = _session(tmp, chat=_Chat([
                ("click", first), ("click", {"ref": "e1"}), ("click", first),
                ("finish", {"done": False, "why": "stop"}),
            ]), browser=_MenuBrowser(close_first=True), person=person)

            session.run()

            self.assertEqual(len(person.asked), 2)

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
            form = form_page
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

                    _page, reason, _detail = apply_form_draft(
                        session, draft, browser.pages[0], step=1)

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
