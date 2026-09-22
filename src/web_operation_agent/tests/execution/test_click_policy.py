"""An application may allow some clicks its deployment otherwise disables -- and only those.

The policy sees the current observation and the one element named; nothing it refuses may
reach the browser, and a policy that fails or is absent leaves click where the action
policy put it.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web_operation_agent.execution.click_policy import ClickPolicy
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.execution.session import LabApparatus, ServerAgentSession, TaskSpec
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control
from web_operation_agent.tests.support.route_fixture import FIXTURE_ROUTE_PORT


def _page(epoch: int = 1, url: str = "http://app/list") -> PageObservation:
    return PageObservation(
        schema_version=1, epoch=epoch, url=url, title="List", landmarks=["List"],
        elements=[ElementRef("e1", "button", "Kanban", "button", epoch),
                  ElementRef("e2", "button", "Archive", "button", epoch),
                  ElementRef("e3", "tab", "Details", "a", epoch, {"href": "#"})])


class _Reviewed:
    """Allow view switches and tabs on /list; refuse everything else."""

    def decide(self, observation, element):
        if element.role == "tab":
            return ""
        if not observation.url.endswith("/list"):
            return "this page has not been reviewed for clicks"
        return "" if element.name in {"Kanban", "List"} else f"{element.name!r} is not reviewed"


class _Broken:
    def decide(self, observation, element):
        raise RuntimeError("policy bug")


def _session(tmp, chat, browser, policy):
    session = ServerAgentSession(
        task=TaskSpec("T", "switch to kanban"), lab=LabApparatus(arm="S"), chat=chat,
        browser=browser, control=_Control([[] for _ in range(8)]),
        ledger=SessionLedger(Path(tmp) / "audit.jsonl", "click"),
        disabled_actions=frozenset({"click", "fill", "select", "submit"}),
        click_policy=policy, policy_block_limit=5, application_routes=FIXTURE_ROUTE_PORT)
    session.entry_observation = browser.pages[0]
    return session


class ClickPolicyNarrowsADisabledClick(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = self.directory.name

    def tearDown(self):
        self.directory.cleanup()

    def test_allowed_elements_dispatch_and_refused_ones_never_reach_the_browser(self):
        chat = _Chat([("click", {"ref": "e2"}), ("click", {"ref": "e1"}),
                      ("click", {"ref": "e3"}), ("finish", {"done": False, "why": "stop"})])
        browser = _Browser([_page(1), _page(2), _page(3), _page(4)])
        session = _session(self.tmp, chat, browser, _Reviewed())

        session.run()

        # The refusal observes a fresh page (the ordinary policy-block path); the two
        # allowed clicks are the only clicks the browser ever received.
        self.assertEqual([action for action, _ in browser.done], ["observe", "click", "click"])
        decisions = session.ledger.of_kind("click_policy_decision")
        self.assertEqual([(d["element_name"], d["allowed"]) for d in decisions],
                         [("Archive", False), ("Kanban", True), ("Details", True)])
        self.assertIn("'Archive' is not reviewed", chat.seen[1][-1]["content"])
        self.assertIn("click", {t["function"]["name"] for t in session.tool_schemas()})

    def test_an_unreviewed_page_refuses_even_a_reviewed_name(self):
        chat = _Chat([("click", {"ref": "e1"}), ("finish", {"done": False, "why": "stop"})])
        browser = _Browser([_page(1, "http://app/form"), _page(2, "http://app/form")])
        session = _session(self.tmp, chat, browser, _Reviewed())
        session.run()
        self.assertNotIn("click", [action for action, _ in browser.done])
        self.assertIn("not been reviewed", chat.seen[1][-1]["content"])

    def test_a_failing_policy_and_an_unknown_element_fail_closed(self):
        for policy, ref in ((_Broken(), "e1"), (_Reviewed(), "e99")):
            with self.subTest(policy=type(policy).__name__, ref=ref):
                chat = _Chat([("click", {"ref": ref}), ("finish", {"done": False, "why": "s"})])
                browser = _Browser([_page(1), _page(2)])
                session = _session(self.tmp, chat, browser, policy)
                session.run()
                self.assertNotIn("click", [action for action, _ in browser.done])
                self.assertFalse(session.ledger.of_kind("click_policy_decision")[0]["allowed"])

    def test_without_a_policy_click_stays_disabled_and_absent(self):
        chat = _Chat([("finish", {"done": False, "why": "stop"})])
        session = _session(self.tmp, chat, _Browser([_page(1)]), None)
        self.assertNotIn("click", {t["function"]["name"] for t in session.tool_schemas()})

    def test_a_policy_only_narrows_a_disabled_click(self):
        self.assertIsInstance(_Reviewed(), ClickPolicy)
        with self.assertRaisesRegex(ValueError, "only narrows a disabled click"):
            AgentTurnConfig(supported_scope="Read.", ledger_path=Path(self.tmp) / "a.jsonl",
                            disabled_actions=frozenset({"fill"}), click_policy=_Reviewed())
        with self.assertRaisesRegex(TypeError, "ClickPolicy"):
            AgentTurnConfig(supported_scope="Read.", ledger_path=Path(self.tmp) / "a.jsonl",
                            click_policy=object())


if __name__ == "__main__":
    unittest.main()


class AnyElementWithAnHrefIsAPageSwitch(unittest.TestCase):
    def test_a_menuitem_link_asks_for_confirmation_like_a_link(self):
        from web_operation_agent.execution.page_actions import _clicked_link
        page = PageObservation(
            schema_version=1, epoch=1, url="http://app/x", title="X", landmarks=[],
            elements=[ElementRef("e1", "menuitem", "Sales", "a", 1, {"href": "/sales"}),
                      ElementRef("e2", "tab", "Terms", "a", 1, {"href": "#"}),
                      ElementRef("e3", "button", "Kanban", "button", 1),
                      ElementRef("e4", "link", "Plain", "a", 1, {"href": "/plain"})])
        self.assertEqual(_clicked_link({"ref": "e1"}, page), "/sales")
        self.assertIsNone(_clicked_link({"ref": "e2"}, page))
        self.assertIsNone(_clicked_link({"ref": "e3"}, page))
        self.assertEqual(_clicked_link({"ref": "e4"}, page), "/plain")
