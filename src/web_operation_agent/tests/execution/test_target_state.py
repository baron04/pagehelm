"""A declared page state ends the turn on the server's word, and only when the page shows it.

The failure this protects against is the one Phase 8 recorded: the agent reached the page
the request named at step 2 and spent the other eight steps confirming it. These tests
script the model, so every model call after the page shows the target is one the product
must not have made; and every counterexample -- a partial multistep, a wrong link, a write
in the same window, a weak or invalid declaration -- must leave the turn running or refused.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from web_operation_agent.execution.session import LabApparatus, ServerAgentSession, TaskSpec
from web_operation_agent.execution.target import (MAX_TARGET_DECLARATIONS, TargetState,
                                                  parse_target, unmet_conditions)
from web_operation_agent.schemas import (SCHEMA_VERSIONS, ActionReceipt, ElementRef,
                                         PageObservation)
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.execution import _Control
from web_operation_agent.tests.support.execution import _Chat as _ScriptedChat
from web_operation_agent.tests.support.route_fixture import FIXTURE_ROUTE_PORT

ORIGIN = "http://app.test"
DESKS = "/contacts/workflows"
PARTNER = "/contacts/workflows/partner"
SUPPLIER = "/contacts/workflows/supplier"


class _Chat(_ScriptedChat):
    """The scripted model, keeping the tool surface it was offered at each call."""

    def __init__(self, calls):
        super().__init__(calls)
        self.tools: list[list[dict]] = []

    def complete(self, messages, tools, run_id, **kwargs):
        self.tools.append(list(tools or []))
        return super().complete(messages, tools, run_id, **kwargs)


def _page(epoch: int, path: str, *, active_tab: str = "overview") -> PageObservation:
    """The synthetic vendor workspace: two desk links, then two tabs on a desk."""
    if path == DESKS:
        elements = [ElementRef("e1", "link", "Partner desk", "a", epoch, {"href": PARTNER}),
                    ElementRef("e2", "link", "Supplier desk", "a", epoch, {"href": SUPPLIER})]
    else:
        elements = [
            ElementRef("e1", "link", "Vendor workspaces", "a", epoch, {"href": DESKS}),
            ElementRef("e2", "tab", "Overview", "button", epoch,
                       {"aria-selected": "true" if active_tab == "overview" else "false"}),
            ElementRef("e3", "tab", "Terms", "button", epoch,
                       {"aria-selected": "true" if active_tab == "terms" else "false"}),
        ]
    return PageObservation(
        schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=epoch,
        url=ORIGIN + path, title="Vendor desk", landmarks=["Vendor workspaces"],
        elements=elements, content=f"page {epoch} {active_tab}")


class _WorkspaceBrowser:
    """A browser whose desk links navigate and whose tabs change the page in place."""

    def __init__(self, start: str = DESKS, *, active_tab: str = "overview"):
        self.path = start
        self.active_tab = active_tab
        self.epoch = 1
        self.commands: list[tuple[str, dict]] = []

    def observe(self) -> PageObservation:
        return _page(self.epoch, self.path, active_tab=self.active_tab)

    def execute(self, action, args=None, expect_navigation=False) -> ActionReceipt:
        args = dict(args or {})
        self.commands.append((action, args))
        before_path, before_epoch = self.path, self.epoch
        if action == "click":
            current = _page(self.epoch, self.path, active_tab=self.active_tab)
            element = next((e for e in current.elements if e.ref == args.get("ref")), None)
            if element is not None and element.role == "link":
                self.path = element.attrs["href"]
            elif element is not None and element.role == "tab":
                self.active_tab = element.name.lower()
        elif action == "goto":
            self.path = str(args.get("url") or "").replace(ORIGIN, "")
        self.epoch += 1
        return ActionReceipt(
            schema_version=SCHEMA_VERSIONS["ActionReceipt"],
            command_id=f"c{len(self.commands)}", action=action, args_echo=args, ok=True,
            url_before=ORIGIN + before_path, url_after=ORIGIN + self.path,
            navigated=self.path != before_path, epoch_before=before_epoch,
            epoch_after=self.epoch,
            observation=_page(self.epoch, self.path, active_tab=self.active_tab))

    @staticmethod
    def console():
        return []

    def close(self):
        pass


def _session(tmp: str, chat, browser, *, prompt: str = "Open the partner desk.",
             control=None, **overrides) -> ServerAgentSession:
    session = ServerAgentSession(
        task=TaskSpec("T", prompt, supported_scope="Read only; click links and tabs."),
        lab=LabApparatus(arm="S"), chat=chat, browser=browser,
        control=control or _Control([[] for _ in range(12)]),
        ledger=SessionLedger(Path(tmp) / "audit.jsonl", "target"),
        step_budget=10, wall_budget_s=120, run_id="target",
        application_routes=FIXTURE_ROUTE_PORT, **overrides)
    session.entry_observation = browser.observe()
    return session


def _events(session, name: str) -> list[dict]:
    return [event for event in session.ledger.events if event.get("event") == name]


class TargetParsing(unittest.TestCase):
    def test_a_target_is_exact_or_refused(self):
        target = parse_target({"path": "/contacts/workflows/partner/",
                               "selected": ["Terms", " terms ", "Overview"]})
        self.assertEqual(target, TargetState("/contacts/workflows/partner/",
                                             ("Terms", "Overview")))
        for bad in ({}, {"path": ""}, {"path": "http://evil/x"}, {"path": "//evil"},
                    {"path": "/a/../b"}, {"path": "/a%2Fb"}, {"path": "/a b"},
                    {"path": "/x", "extra": 1}, {"selected": [""]},
                    {"selected": ["a"] * 5}, {"selected": "Terms"},
                    {"path": "/x", "selected": [{"name": "Terms"}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_target(bad)

    def test_a_tab_the_runtime_derived_as_selected_meets_the_target(self):
        # Odoo 18 marks the current notebook tab only by class; the Runtime reports it as
        # selected=true, and the target reads that like aria-selected.
        page = _page(1, PARTNER)
        page.elements[2] = ElementRef("e3", "tab", "Terms", "a", 1, {"selected": "true"})
        page.elements[1] = ElementRef("e2", "tab", "Overview", "a", 1, {})
        self.assertEqual(unmet_conditions(TargetState(PARTNER, ("Terms",)), page), ())
        self.assertTrue(unmet_conditions(TargetState(PARTNER, ("Overview",)), page))

    def test_unmet_conditions_are_exact_and_viewport_bound(self):
        target = TargetState(PARTNER, ("Terms",))
        self.assertEqual(len(unmet_conditions(target, _page(1, DESKS))), 2)
        self.assertEqual(unmet_conditions(target, _page(1, PARTNER)),
                         ('selected control "Terms"',))
        self.assertEqual(unmet_conditions(target, _page(1, PARTNER, active_tab="terms")), ())
        # A path is not "contains": the desks page does not satisfy a desk target, and a
        # desk does not satisfy the desks page.
        self.assertTrue(unmet_conditions(TargetState(DESKS), _page(1, PARTNER)))
        self.assertTrue(unmet_conditions(TargetState(PARTNER), _page(1, SUPPLIER)))
        self.assertEqual(unmet_conditions(TargetState(PARTNER + "/"), _page(1, PARTNER)), ())
        # A query in the target must match; a page's extra query is not asked about.
        self.assertTrue(unmet_conditions(TargetState(PARTNER + "?tab=terms"),
                                         _page(1, PARTNER)))
        # Only an explicit selected state counts; a tab that exists is not a selected tab.
        self.assertTrue(unmet_conditions(TargetState(selected=("Terms",)),
                                         _page(1, PARTNER, active_tab="overview")))

    def test_a_matching_url_with_the_previous_view_still_rendering_is_unmet(self):
        pending = _page(2, PARTNER)
        pending.render_state = "pending"
        self.assertEqual(unmet_conditions(TargetState(PARTNER), pending),
                         ("page rendering pending",))


class TargetSettlement(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = self.directory.name

    def tearDown(self):
        self.directory.cleanup()

    def test_spa_url_change_waits_for_the_destination_view_before_claiming_arrival(self):
        class DelayedView(_WorkspaceBrowser):
            def execute(self, action, args=None, expect_navigation=False):
                receipt = super().execute(action, args, expect_navigation)
                if action == "click":
                    old_view = _page(receipt.observation.epoch, DESKS)
                    old_view.url = ORIGIN + PARTNER
                    old_view.render_state = "pending"
                    return replace(receipt, observation=old_view)
                return receipt

        chat = _Chat([
            ("set_target", {"path": PARTNER}),
            ("click", {"ref": "e1"}),
            ("click", {"ref": "e1"}),
            ("observe", {}),
            ("finish", {"outcome": "acted"}),
        ])
        browser = DelayedView()
        session = _session(self.tmp, chat, browser)

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual([name for name, _ in browser.commands], ["click", "observe"])
        self.assertEqual(len(_events(session, "pending_page_action_refused")), 1)
        self.assertEqual(len(_events(session, "target_state_reached")), 1)
        self.assertEqual(_events(session, "target_state_reached")[0]["page_epoch"], 3)
        self.assertIn("page rendering pending", chat.seen[2][-1]["content"])

    def test_navigation_is_reported_on_the_receipt_and_closed_by_finish(self):
        chat = _Chat([
            ("set_target", {"path": PARTNER}),
            ("click", {"ref": "e1"}),
            ("finish", {"done": True, "why": "invented business result"}),
            # What the old loop spent its budget on. None of it may run.
            ("observe", {}), ("click", {"ref": "e2"}),
        ])
        browser = _WorkspaceBrowser()
        session = _session(self.tmp, chat, browser)

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(len(chat.seen), 3)
        self.assertEqual([command for command, _ in browser.commands], ["click"])
        # The checkpoint does not change the tool list -- a changed list would invalidate
        # the provider's cached prefix -- it tells the model the decision and the loop
        # refuses anything else (see the refusal test below).
        self.assertEqual(chat.tools[2], chat.tools[1])
        self.assertIn("TARGET REACHED", chat.seen[2][-1]["content"])
        self.assertIn("Only finish and set_target are available now",
                      chat.seen[2][-1]["content"])
        self.assertEqual(result.verified_answer["action"]["kind"], "target_state")
        self.assertEqual(result.verified_answer["action"]["url"], ORIGIN + PARTNER)
        self.assertTrue(result.verified_answer["action"]["acted"])
        self.assertIn("Reached the requested page state: " + ORIGIN + PARTNER,
                      result.agent_said)
        self.assertNotIn("invented", result.agent_said)
        reached = _events(session, "target_state_reached")
        self.assertEqual(len(reached), 1)
        self.assertEqual(reached[0]["page_actions"], 1)
        self.assertFalse(reached[0]["side_effect_possible"])
        # The user's language decides the sentence, never the model.
        zh = _session(self.tmp, _Chat([("set_target", {"path": PARTNER}),
                                       ("click", {"ref": "e1"}),
                                       ("finish", {"done": True, "why": "好了"})]),
                      _WorkspaceBrowser(), prompt="打开合作伙伴工作台。")
        self.assertIn("已到达请求的页面状态", zh.run().agent_said)

    def test_a_full_multistep_target_is_not_reached_by_its_first_half(self):
        chat = _Chat([
            ("set_target", {"path": PARTNER, "selected": ["Terms"]}),
            ("click", {"ref": "e1"}),   # the desk: path met, tab not
            ("click", {"ref": "e3"}),   # the Terms tab: both met
            ("finish", {"done": True, "why": "done"}),
        ])
        browser = _WorkspaceBrowser()
        session = _session(self.tmp, chat, browser,
                           prompt="Open the partner desk, then its Terms tab.")

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(len(chat.seen), 4)
        self.assertEqual(browser.active_tab, "terms")
        self.assertEqual(result.verified_answer["action"]["selected"], ["Terms"])
        self.assertIn("Selected: Terms", result.agent_said)
        # After the first click the model was told exactly what was still unmet, could
        # see which tab the page had selected, and still had the page tools.
        feedback = chat.seen[2][-1]["content"]
        self.assertIn('Still unmet: selected control "Terms"', feedback)
        self.assertIn('e2 tab "Overview" aria-selected=true', feedback)
        self.assertNotIn('e3 tab "Terms" aria-selected=true', feedback)
        self.assertIn("click", {tool["function"]["name"] for tool in chat.tools[2]})

    def test_a_half_declared_multistep_is_not_ended_by_the_server(self):
        # The paid gate's failure: the model declared only the desk for a request that
        # also asked for the Terms tab. The server may report the desk; it may not end
        # the turn. The model then declares the rest and the turn continues.
        chat = _Chat([
            ("set_target", {"path": PARTNER}),
            ("click", {"ref": "e1"}),
            ("set_target", {"selected": ["Terms"]}),
            ("click", {"ref": "e3"}),
            ("finish", {"done": True, "why": "done"}),
        ])
        browser = _WorkspaceBrowser()
        session = _session(self.tmp, chat, browser,
                           prompt="Open the partner desk, then its Terms tab.")

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(len(chat.seen), 5)
        self.assertEqual(browser.active_tab, "terms")
        self.assertEqual([event["acted"] for event in _events(session, "target_state_reached")],
                         [True, True])
        # A new declaration reopened the page tools and dropped the earlier receipt.
        self.assertIn("click", {tool["function"]["name"] for tool in chat.tools[3]})
        self.assertEqual(result.verified_answer["action"]["selected"], ["Terms"])
        self.assertIn("Selected: Terms", result.agent_said)

    def test_a_finish_with_stray_fields_or_a_bad_optional_answer_still_reports_the_action(self):
        # Both shapes came from the paid gate: a `reason` field beside the structured
        # outcome, and an answer citing an element ref. Neither may cost a repair once
        # the server has observed the target; the person reads the observed state.
        chat = _Chat([
            ("set_target", {"path": PARTNER, "selected": ["Terms"]}),
            ("click", {"ref": "e1"}),
            ("click", {"ref": "e3"}),
            ("finish", {"outcome": "acted", "reason": "the desk and tab are open",
                        "answer": {"version": 1, "coverage": "complete",
                                   "conclusions": [{"kind": "page_observation",
                                                    "evidence_ref": "e3",
                                                    "statement": "invented prose"}]}}),
            ("finish", {"outcome": "acted"}),
        ])
        browser = _WorkspaceBrowser()
        session = _session(self.tmp, chat, browser,
                           prompt="Open the partner desk, then its Terms tab.")

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(len(chat.seen), 4)
        self.assertEqual(_events(session, "answer_contract_refused"), [])
        self.assertEqual([event["fields"] for event in
                          _events(session, "acted_stray_fields_ignored")], [["reason"]])
        self.assertEqual([event["reason"] for event in
                          _events(session, "acted_answer_dropped")], ["EvidenceUnknown"])
        self.assertEqual(result.verified_answer["conclusions"], ())
        self.assertIn("Selected: Terms", result.agent_said)
        self.assertNotIn("invented", result.agent_said)
        self.assertNotIn("the desk and tab", result.agent_said)

    def test_a_bad_optional_answer_is_still_repaired_outside_a_checkpoint(self):
        # Without a declared target the existing contract stands: an acted outcome with
        # an answer that cites nothing recorded is refused and repaired, not dropped.
        chat = _Chat([
            ("click", {"ref": "e1"}),
            ("finish", {"outcome": "acted",
                        "answer": {"version": 1, "coverage": "complete",
                                   "conclusions": [{"kind": "page_observation",
                                                    "evidence_ref": "e1"}]}}),
            ("finish", {"outcome": "acted"}),
        ])
        session = _session(self.tmp, chat, _WorkspaceBrowser())

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(len(chat.seen), 3)
        self.assertEqual(len(_events(session, "answer_contract_refused")), 1)
        self.assertEqual(_events(session, "acted_answer_dropped"), [])
        self.assertEqual(result.verified_answer["action"]["kind"], "navigation")

    def test_a_tool_other_than_finish_or_set_target_is_refused_at_the_checkpoint(self):
        chat = _Chat([
            ("set_target", {"path": PARTNER}),
            ("click", {"ref": "e1"}),
            ("observe", {}),
            ("list_contacts", {}),
            ("finish", {"done": True, "why": "done"}),
        ])
        browser = _WorkspaceBrowser()
        session = _session(self.tmp, chat, browser)

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual([command for command, _ in browser.commands], ["click"])
        refused = _events(session, "target_checkpoint_tool_refused")
        self.assertEqual([event["tool"] for event in refused], ["observe", "list_contacts"])
        self.assertIn("TARGET REACHED", chat.seen[3][-1]["content"])

    def test_a_question_may_observe_after_a_page_target_without_claiming_completion(self):
        chat = _Chat([
            ("set_target", {"path": PARTNER}),
            ("click", {"ref": "e1"}),
            ("observe", {"answer_evidence": True}),
            ("finish", {"outcome": "unable"}),
        ])
        browser = _WorkspaceBrowser()
        session = _session(self.tmp, chat, browser,
                           prompt="Open the partner desk and tell me its terms.",
                           page_answer_evidence=True)

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_refused")
        self.assertEqual([action for action, _ in browser.commands], ["click", "observe"])
        self.assertEqual(_events(session, "target_checkpoint_tool_refused"), [])
        self.assertIn("Continue reading or observing", chat.seen[2][-1]["content"])

    def test_a_wrong_link_is_reported_unmet_and_a_corrected_target_may_be_reached(self):
        chat = _Chat([
            ("set_target", {"path": PARTNER}),
            ("click", {"ref": "e2"}),                    # supplier: wrong page
            ("click", {"ref": "e1"}),                    # back to the desks
            ("set_target", {"path": SUPPLIER}),          # new evidence changed the goal
            ("click", {"ref": "e2"}),
            ("finish", {"done": True, "why": "done"}),
        ])
        browser = _WorkspaceBrowser()
        session = _session(self.tmp, chat, browser)

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(len(chat.seen), 6)
        self.assertEqual(result.verified_answer["action"]["url"], ORIGIN + SUPPLIER)
        declared = _events(session, "target_state_declared")
        self.assertEqual([event["declaration"] for event in declared], [1, 2])
        self.assertIn("Still unmet: path " + PARTNER, chat.seen[2][-1]["content"])
        self.assertEqual(len(_events(session, "target_state_reached")), 1)

    def test_a_target_the_page_already_shows_needs_no_action(self):
        chat = _Chat([("set_target", {"path": PARTNER, "selected": ["Overview"]}),
                      ("finish", {"done": True, "why": "done"})])
        browser = _WorkspaceBrowser(PARTNER)
        session = _session(self.tmp, chat, browser)

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(len(chat.seen), 2)
        self.assertEqual(browser.commands, [])
        self.assertEqual(chat.tools[1], chat.tools[0])
        self.assertIn("TARGET REACHED", chat.seen[1][-1]["content"])
        self.assertFalse(result.verified_answer["action"]["acted"])
        self.assertIn("already in the requested state", result.agent_said)

    def test_a_write_in_the_same_window_is_settled_by_the_write_path_not_the_target(self):
        # The gateway refuses a write during the click that reaches the target page. The
        # page state matches, but the target is not reported reached: the refused write
        # is what the loop has to report and the model has to hear.
        refused = [{"event": "write_blocked", "webapp": "contacts", "uri": "save"}]
        chat = _Chat([
            ("set_target", {"path": PARTNER}),
            ("click", {"ref": "e1"}),
            ("finish", {"done": False, "why": "the write was refused"}),
        ])
        browser = _WorkspaceBrowser()
        session = _session(self.tmp, chat, browser,
                           control=_Control([[], refused, [], [], [], []]))

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_refused")
        self.assertEqual(len(chat.seen), 3)
        self.assertEqual(_events(session, "target_state_reached"), [])
        self.assertFalse(session._target_reached)
        self.assertEqual(result.write_refusals, 1)

    def test_invalid_and_excess_declarations_are_refused_without_dispatch(self):
        script = [("set_target", {"path": "http://evil.test/x"})]
        script += [("set_target", {"path": f"/never/{i}"}) for i in range(MAX_TARGET_DECLARATIONS + 1)]
        script += [("finish", {"done": False, "why": "stop"})]
        chat = _Chat(script)
        browser = _WorkspaceBrowser()
        session = _session(self.tmp, chat, browser)

        result = session.run()

        self.assertEqual(result.terminal_reason, "agent_refused")
        self.assertEqual(browser.commands, [])
        refused = _events(session, "target_state_refused")
        self.assertEqual([event["reason"] for event in refused],
                         ["invalid", "declaration_limit"])
        self.assertIn("TARGET REFUSED", chat.seen[1][-1]["content"])
        self.assertIn("at most", chat.seen[-1][-1]["content"])

    def test_the_tool_and_prompt_are_absent_for_a_mutation_request(self):
        from web_operation_agent.business.runtime import BusinessToolRuntime
        from web_operation_agent.business.control import ReadOnlyControl
        from web_operation_agent.tests.support.business import _MutationExecutor, _write_skill

        skill = _write_skill(Path(self.tmp) / "create.json")
        chat = _Chat([("finish", {"done": False, "why": "stop"})])
        session = _session(
            self.tmp, chat, _WorkspaceBrowser(), prompt="Create company Acme.",
            control=ReadOnlyControl(),
            business_tools=BusinessToolRuntime((skill,), _MutationExecutor()),
            business_mutation_approval=lambda proposal, context=None: None)
        self.assertTrue(session._explicit_mutation_intent)
        self.assertFalse(session.target_settlement_available())
        self.assertNotIn("set_target", {t["function"]["name"] for t in session.tool_schemas()})
        session.run()
        self.assertNotIn("set_target", chat.seen[0][0]["content"])

    def test_a_where_question_is_answered_by_opening_the_page(self):
        # The owner's call: "where is X maintained?" gets its answer and the page opened.
        chat = _Chat([("finish", {"done": False, "why": "stop"})])
        _session(self.tmp, chat, _WorkspaceBrowser(),
                 prompt="Where are partner desks kept?").run()
        self.assertIn("A question about where something is kept", chat.seen[0][0]["content"])

    def test_a_pending_target_is_kept_in_view_after_context_trimming(self):
        chat = _Chat([
            ("set_target", {"path": PARTNER, "selected": ["Terms"]}),
            ("observe", {}), ("observe", {}), ("observe", {}), ("observe", {}),
            ("finish", {"done": False, "why": "stop"}),
        ])
        session = _session(self.tmp, chat, _WorkspaceBrowser(), keep_observations=1)
        session.run()
        last_feedback = chat.seen[-1][-1]["content"]
        self.assertIn("TARGET (declared)", last_feedback)
        self.assertIn("path " + PARTNER, last_feedback)


if __name__ == "__main__":
    unittest.main()
