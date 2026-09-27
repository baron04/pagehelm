from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.modeling.client import ProviderError, require_chat_port
from testapps.contacts.app import (DEMO_API_TOKEN, DEMO_SESSION_COOKIE,
                                   DEMO_SESSION_VALUE, MinimalContactsApplication)
from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.demo import (DemoConfig, READ_WALKTHROUGH, ScriptedDemoChat,
                                    WRITE_WALKTHROUGH, _allow_operation_in_page, run_demo)
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.business.runtime import (BusinessMutationIndeterminate,
                                          BusinessToolError)
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.state.identity import PrincipalError
from web_operation_agent.composition.live import LiveConversationRunner, LiveProductSession
from web_operation_agent.business.mutation import MemoryMutationJournal, SQLiteMutationJournal
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor
from web_operation_agent.tests.support.adapter_contract import assert_adapter_contract


def _classify_woa_outcome(*, terminal_reason: str, events=(), boundary_error_code=None):
    """Project typed host failures for this synthetic integration's boundary tests."""
    if boundary_error_code in {"identity_unavailable", "identity_binding", "session_changed"}:
        return "credential_or_identity", boundary_error_code
    if boundary_error_code:
        return "transport_contract", boundary_error_code
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("event") == "provider_failed":
            code = event.get("provider_error_code") or "provider_error"
            if code not in {"model_call_budget", "provider_deadline", "cancelled"}:
                return (code if code in {"ledger_failure", "transport_contract"}
                        else "provider_outage"), str(code)
        if event.get("event") in {
                "business_mutation_blocked", "business_mutation_failed",
                "business_mutation_permission_denied", "business_mutation_rejected",
                "business_tool_permission_denied", "business_tool_rejected"}:
            code = event.get("error_code") or "business_tool"
            if code in {"business_authentication", "business_identity"}:
                return "credential_or_identity", str(code)
            if code not in {
                    "business_request", "business_call_budget", "route_call_budget",
                    "route_refused", "approval_denied", "approval_expired",
                    "indeterminate_write", "cancelled", "business_mutation_blocked",
                    "business_mutation_refused", "business_permission_denied"}:
                return "transport_contract", str(code)
    if terminal_reason == "provider_error":
        return "provider_outage", terminal_reason
    if terminal_reason in {"control_plane_error", "transport_error"}:
        return "transport_contract", terminal_reason
    if terminal_reason in {
            "action_policy_budget", "agent_finished", "agent_refused", "approval_denied",
            "approval_expired", "business_mutation_blocked", "business_mutation_refused",
            "indeterminate_write", "needs_input", "no_progress", "permission_denied",
            "step_budget", "token_budget", "user_cancelled", "wall_clock_budget"}:
        return None, terminal_reason
    return "transport_contract", terminal_reason or "execution_failed"


def _chromium_launches() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


class MinimalDemoUnitTests(unittest.TestCase):
    def test_evaluation_entry_and_probe_are_bounded(self):
        self.assertEqual(DemoConfig().start_path, "/contacts")
        self.assertIsInstance(DemoConfig().source, SourceCapability)
        with self.assertRaisesRegex(ValueError, "start_path"):
            DemoConfig(start_path="/contacts/unknown")
        with self.assertRaisesRegex(TypeError, "final_page_probe"):
            DemoConfig(final_page_probe="not callable")
        with self.assertRaisesRegex(TypeError, "extra_skills"):
            DemoConfig(extra_skills=("not a Skill",))
        with self.assertRaisesRegex(ValueError, "click"):
            DemoConfig(start_path="/contacts/new", allow_page_click=True)
        self.assertTrue(DemoConfig(allow_page_click=True).allow_page_click)

    def test_second_adapter_binds_and_reads_its_live_typed_api(self):
        with MinimalContactsApplication() as application:
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN)
            bound = assert_adapter_contract(self, adapter)
            self.assertTrue(adapter.health()["passed"])
            self.assertEqual(bound.descriptor.application_id, "minimal.contacts")
            self.assertEqual(
                [item.skill_id for item in bound.business_tools.relevant(
                    "Open Grace Hopper's contact")],
                ["minimal.contacts.query"],
            )
            result = bound.business_tools.execute(
                "list_contacts", {"name_contains": "Grace"},
                {"minimal.contacts.query"})
            self.assertEqual(result.content["matched_count"], 1)
            self.assertTrue(result.content["completeness"]["source_complete"])
            self.assertEqual(result.allowed_routes, ("/contacts/grace-hopper",))

    def test_application_serves_the_same_origin_controller(self):
        with MinimalContactsApplication() as application:
            with urllib.request.urlopen(
                    application.origin + "/woa/page-controller.js", timeout=2) as response:
                source = response.read().decode("utf-8")
            self.assertIn("export class PageController", source)
            self.assertIn("export function getElementByIndex", source)

    def test_scripted_apparatus_implements_the_chat_contract_and_cancels(self):
        chat = require_chat_port(ScriptedDemoChat(), role="minimal demo chat")
        reply = chat.complete([], [], "plain")
        self.assertEqual(reply["tool_calls"][0]["function"]["name"], "load_skill")
        with self.assertRaises(ProviderError) as raised:
            chat.complete_cancellable([], [], "cancelled", cancelled=lambda: True)
        self.assertEqual(raised.exception.code, "provider_cancelled")


class MinimalDemoBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get("WOA_REQUIRE_MINIMAL_DEMO_BROWSER") == "1"
        if not _chromium_launches():
            if required:
                raise RuntimeError("Chromium is required for minimal-demo qualification")
            raise unittest.SkipTest("Chromium is unavailable")

    def test_scripted_walkthrough_uses_the_real_gateway_and_opens_api_route(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(DemoConfig(
                scripted=True,
                headless=True,
                output_dir=Path(directory),
                keep_open_seconds=0,
                turn_timeout_s=30,
            ))
            self.assertEqual(summary["mode"], "scripted_apparatus")
            self.assertEqual(summary["model_usage"]["provider_calls"], 0)
            self.assertEqual(summary["model_usage"]["scripted_decisions"],
                             len(READ_WALKTHROUGH))
            self.assertEqual(len(summary["turns"]), 1)
            self.assertEqual(summary["turns"][0]["status"], "finished")
            self.assertEqual(summary["turns"][0]["terminal_reason"], "agent_finished")
            self.assertGreaterEqual(summary["turns"][0]["page_actions"], 1)
            self.assertTrue(summary["last_url"].endswith("/contacts/grace-hopper"))
            self.assertIn("Grace Hopper", summary["last_page_text"])
            self.assertIn("grace@example.test", summary["last_page_text"])
            audit = next(Path(directory).glob("*/audit.jsonl"))
            events = [json.loads(line)["event"] for line in audit.read_text().splitlines()]
            # The request matches exactly one read Skill, so the server loads it before the
            # first model call; the script's own load_skill then gets the same card back.
            self.assertIn("business_skill_auto_loaded", events)
            self.assertIn("business_tool_result", events)
            self.assertIn("action_receipt", events)

    def test_evaluation_probe_reads_new_form_after_turn_without_exposing_values(self):
        script = (("finish", {"done": False, "why": "probe only"}),)
        with tempfile.TemporaryDirectory() as directory, patch(
                "testapps.contacts.demo._chat",
                return_value=(ScriptedDemoChat(script), None)):
            summary = run_demo(DemoConfig(
                tasks=("Inspect this form",), headless=True,
                start_path="/contacts/new", output_dir=Path(directory),
                keep_open_seconds=0, turn_timeout_s=30,
                final_page_probe=lambda page: {"form_visible": page.locator(
                    "#contact-create").is_visible()},
            ))
        self.assertEqual(summary["panel"]["final_page_probe"], {"form_visible": True})
        self.assertEqual(summary["application_state"]["records_created"], 0)

    def test_list_view_marker_is_rendered_by_the_second_application(self):
        script = (("finish", {"done": False, "why": "probe only"}),)
        with tempfile.TemporaryDirectory() as directory, patch(
                "testapps.contacts.demo._chat",
                return_value=(ScriptedDemoChat(script), None)):
            summary = run_demo(DemoConfig(
                tasks=("Inspect contacts",), headless=True,
                start_path="/contacts", output_dir=Path(directory),
                keep_open_seconds=0, turn_timeout_s=30,
                final_page_probe=lambda page: {"list_rendered": page.locator(
                    "main#app").get_attribute("data-active-view") == "List"},
            ))
        self.assertEqual(summary["panel"]["final_page_probe"],
                         {"list_rendered": True})

    def test_record_view_marker_is_rendered_after_spa_navigation(self):
        from playwright.sync_api import sync_playwright

        with MinimalContactsApplication() as application, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                context.add_cookies([{"name": DEMO_SESSION_COOKIE,
                                      "value": DEMO_SESSION_VALUE,
                                      "url": application.origin}])
                page = context.new_page()
                page.goto(application.origin + "/contacts", wait_until="domcontentloaded")
                self.assertEqual(page.locator("main#app").get_attribute("data-active-view"),
                                 "List")
                page.locator('a[href="/contacts/grace-hopper"]').click()
                page.wait_for_url("**/contacts/grace-hopper")
                self.assertEqual(page.locator("main#app").get_attribute("data-active-view"),
                                 "Detail")
            finally:
                browser.close()

    def test_create_view_marker_and_name_field_are_visible_without_submitting(self):
        from playwright.sync_api import sync_playwright

        with MinimalContactsApplication() as application, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                context.add_cookies([{"name": DEMO_SESSION_COOKIE,
                                      "value": DEMO_SESSION_VALUE,
                                      "url": application.origin}])
                page = context.new_page()
                page.goto(application.origin + "/contacts", wait_until="domcontentloaded")
                before = len(application.contacts)
                page.get_by_role("link", name="New contact").click()
                page.wait_for_url("**/contacts/new")
                self.assertEqual(page.locator("main#app").get_attribute("data-active-view"),
                                 "Create")
                self.assertTrue(page.get_by_role("textbox", name="Name").is_visible())
                self.assertEqual(len(application.contacts), before)
            finally:
                browser.close()

    def test_evaluation_probe_rejects_raw_page_values(self):
        script = (("finish", {"done": False, "why": "probe only"}),)
        with tempfile.TemporaryDirectory() as directory, patch(
                "testapps.contacts.demo._chat",
                return_value=(ScriptedDemoChat(script), None)):
            with self.assertRaisesRegex(TypeError, "boolean verdicts"):
                run_demo(DemoConfig(
                    tasks=("Inspect this form",), headless=True,
                    start_path="/contacts/new", output_dir=Path(directory),
                    keep_open_seconds=0, turn_timeout_s=30,
                    final_page_probe=lambda page: {"name": page.locator(
                        "#contact-create input[name=name]").input_value()},
                ))

    def test_default_product_renders_a_computed_answer_without_page_actions(self):
        script = (
            ("load_skill", {"skill_id": "minimal.contacts.query"}),
            ("list_contacts", {}),
            ("compute", {"operation": "count", "dataset_ref": "call2-query#1"}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete", "conclusions": [
                    {"kind": "aggregate", "evidence_ref": "calc#3"}]}}),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
                "testapps.contacts.demo._chat", return_value=(ScriptedDemoChat(script), None)):
            summary = run_demo(DemoConfig(
                tasks=("How many contacts are there?",), headless=True,
                output_dir=Path(directory), keep_open_seconds=0, turn_timeout_s=30))
        turn = summary["turns"][0]
        self.assertEqual(turn["status"], "finished")
        self.assertIn("Count", turn["answer"])
        self.assertIn("3", turn["answer"])
        self.assertNotIn("grace@example.test", turn["answer"])
        self.assertEqual(turn["page_actions"], 0)

    def test_a_goal_turn_opens_a_desk_and_selects_its_tab_in_the_real_browser(self):
        # The same core turn that drives Odoo, on this structurally different application:
        # one declaration against the Contacts index, the desk opened by its route, the
        # tab selected by its visible name, and the reply rendered from the settled page.
        script = (("declare_goal", {"deliverables": [
            {"kind": "page", "page": {"key": "desk-partner"}, "section": "Terms"}]}),)

        def probe(page) -> dict[str, bool]:
            return {"terms_tab_active": page.evaluate(
                "() => document.querySelector('[data-workflow-desk=\"partner\"]')"
                "?.dataset.activeTab === 'terms'")}

        with tempfile.TemporaryDirectory() as directory, patch(
                "testapps.contacts.demo._chat", return_value=(ScriptedDemoChat(script), None)):
            summary = run_demo(DemoConfig(
                tasks=("看看 Partner desk 的 Terms。",), headless=True, goal_turns=True,
                scripted=False, output_dir=Path(directory), keep_open_seconds=0,
                turn_timeout_s=30, final_page_probe=probe))
            events = [json.loads(line) for line in next(Path(directory).glob("*/audit.jsonl")).read_text().splitlines()]
        turn = summary["turns"][0]
        self.assertEqual(turn["status"], "finished")
        self.assertIn("Partner desk · Terms", turn["answer"])
        self.assertTrue(summary["panel"]["final_page_probe"]["terms_tab_active"])
        self.assertTrue(any(e["event"] == "goal_page_settled" and e["reached"] for e in events))
        self.assertFalse(any(e["event"] == "business_tool_result" for e in events))

    def test_a_delete_handoff_opens_the_record_menu_and_marks_delete_in_the_real_browser(self):
        # "Delete" is an item of the record's collapsed "More" menu, as many applications
        # keep a record's operations. The turn opens the record, opens the one menu the page
        # declares collapsed, marks Delete and presses nothing; the person deletes.
        script = (("declare_goal", {"deliverables": [
            {"kind": "handoff", "entity": "contacts", "record": "Grace Hopper",
             "action": "Delete"}], "write_intent": True}),)

        def probe(page) -> dict[str, bool]:
            return page.evaluate("""() => {
                const item = document.querySelector('[data-delete-contact]');
                const toggle = document.querySelector('[data-record-menu]');
                return {menu_open: toggle?.getAttribute('aria-expanded') === 'true',
                        delete_marked: item?.style.getPropertyPriority('outline') === 'important'};
            }""")

        with tempfile.TemporaryDirectory() as directory, patch(
                "testapps.contacts.demo._chat", return_value=(ScriptedDemoChat(script), None)):
            summary = run_demo(DemoConfig(
                tasks=("Delete Grace Hopper.",), headless=True, goal_turns=True,
                scripted=False, output_dir=Path(directory), keep_open_seconds=0,
                turn_timeout_s=30, final_page_probe=probe))
            events = [json.loads(line) for line in next(Path(directory).glob("*/audit.jsonl")).read_text().splitlines()]
        turn = summary["turns"][0]
        self.assertEqual(turn["status"], "finished", turn["answer"])
        self.assertIn("opened the “More” menu and highlighted “Delete”", turn["answer"])
        self.assertEqual(summary["panel"]["final_page_probe"],
                         {"menu_open": True, "delete_marked": True})
        self.assertTrue(summary["last_url"].endswith("/contacts/grace-hopper"))
        self.assertEqual(summary["application_state"]["records_deleted"], 0)
        self.assertEqual(summary["application_state"]["write_requests"], 0)
        tools = [e["tool"] for e in events if e["event"] == "tool_call"]
        self.assertEqual([t for t in tools if t in ("reveal", "highlight", "click")],
                         ["reveal", "highlight"])
        self.assertEqual([e["menu"] for e in events if e["event"] == "goal_menu_revealed"],
                         ["More"])

    def test_page_answer_uses_a_real_observation_without_calling_the_contacts_api(self):
        script = (("observe", {"answer_evidence": True}),
                  ("finish", {"outcome": "answered", "answer": {
                      "version": 1, "coverage": "complete", "conclusions": [
                          {"kind": "page_observation", "evidence_ref": "obs1-page#1"}]}}))
        with tempfile.TemporaryDirectory() as directory, patch(
                "testapps.contacts.demo._chat", return_value=(ScriptedDemoChat(script), None)):
            summary = run_demo(DemoConfig(
                tasks=("Quote the first text on this page.",), headless=True,
                output_dir=Path(directory), keep_open_seconds=0, turn_timeout_s=30))
            events = [json.loads(line) for line in next(Path(directory).glob("*/audit.jsonl")).read_text().splitlines()]
        self.assertEqual(summary["turns"][0]["status"], "finished")
        self.assertIn("source: page observation", summary["turns"][0]["answer"])
        self.assertFalse(any(row["event"] == "business_tool_result" for row in events))
        self.assertEqual(sum(row["event"] == "page_answer_evidence" for row in events), 1)


if __name__ == "__main__":
    unittest.main()


class TheWriteWalkthroughInARealBrowser(unittest.TestCase):
    """The whole chain as a person sees it, through the panel's own approval control.

    This is the one test that cannot be faked into passing: the approval has to come from
    the page session holding the lease, so a composition that found some other way to
    approve would be qualifying a product this repository does not ship.
    """

    @classmethod
    def setUpClass(cls):
        required = os.environ.get("WOA_REQUIRE_MINIMAL_DEMO_BROWSER") == "1"
        if not _chromium_launches():
            if required:
                raise RuntimeError("Chromium is required for minimal-demo qualification")
            raise unittest.SkipTest("Chromium is unavailable")

    def test_one_approval_creates_one_contact_and_opens_its_record(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(DemoConfig(
                scripted=True, headless=True, allow_mutations=True,
                output_dir=Path(directory), keep_open_seconds=0, turn_timeout_s=45,
            ))
            turn = summary["turns"][0]
            self.assertEqual(turn["status"], "finished")
            self.assertEqual(summary["model_usage"]["provider_calls"], 0)
            self.assertTrue(summary["last_url"].endswith("/contacts/radia-perlman"))
            # The record page shows the contact that did not exist when the page loaded,
            # which is the only evidence that the write reached the application.
            self.assertIn("Radia Perlman", summary["last_page_text"])
            self.assertIn("radia@example.test", summary["last_page_text"])
            self.assertIn("Create exactly one contact", turn["answer"])
            self.assertIn("Radia Perlman", turn["answer"])
            self.assertIn("Opened the page", turn["answer"])
            self.assertEqual(summary["mutation_journal"][0]["state"], "completed")
            audit = next(Path(directory).glob("*/audit.jsonl"))
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            kinds = [row["event"] for row in events]
            self.assertEqual(kinds.count("business_mutation_proposed"), 1,
                             "one approval, asked for exactly once")
            # Opening the new record works the page, so the turn asks for leave first: one
            # card, which can be declined, recorded apart from the write approval.
            operations = summary["operation_confirmations"]
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0]["decline_controls"], 1)
            self.assertIn("Opens pages, clicks, fills and chooses", operations[0]["text"])
            self.assertEqual(kinds.count("approval_requested"), 1)
            self.assertEqual(kinds.count("operation_confirmation_requested"), 1)
            self.assertEqual(turn["status"], "finished")
            self.assertEqual(kinds.count("business_mutation_result"), 1)
            self.assertNotIn("business_mutation_indeterminate", kinds)
            written = next(row for row in events
                           if row["event"] == "business_mutation_result")
            self.assertTrue(written["audit"]["readback_verified"])
            # The proposal is in the trail; the values the user approved are not, beyond
            # what the redacted projection carries.
            proposed = next(row for row in events
                            if row["event"] == "business_mutation_proposed")
            self.assertEqual(proposed["effect"], "Create exactly one contact")
            self.assertEqual(sorted(proposed["proposal"]["argument_names"]),
                             ["email", "name"])
            self.assertNotIn("radia@example.test", json.dumps(proposed["proposal"]))

    def _run_write(self, directory, **changes):
        return run_demo(DemoConfig(
            scripted=True, headless=True, allow_mutations=True,
            output_dir=Path(directory), keep_open_seconds=0, turn_timeout_s=45,
            **changes,
        ))

    def test_visible_proposal_cancelled_without_approval_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = self._run_write(directory, approval_decision="cancelled")
        self.assertIn("Create exactly one contact", summary["approvals"][0]["text"])
        self.assertIn("Name: Radia Perlman", summary["approvals"][0]["text"])
        self.assertIn("Email: radia@example.test", summary["approvals"][0]["text"])
        self.assertEqual(summary["application_state"]["post_requests"], 0)
        self.assertEqual(summary["application_state"]["records_created"], 0)
        self.assertEqual(summary["turns"][0]["terminal_reason"], "user_cancelled")
        self.assertEqual(summary["mutation_journal"][0]["state"], "prepared")

    def test_visible_denial_is_terminal_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = self._run_write(directory, approval_decision="denied")
        self.assertEqual(summary["application_state"]["post_requests"], 0)
        self.assertEqual(summary["application_state"]["records_created"], 0)
        self.assertEqual(summary["turns"][0]["terminal_reason"], "approval_denied")
        self.assertEqual(summary["mutation_journal"][0]["state"], "prepared")

    def test_expired_visible_approval_cannot_write(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = self._run_write(
                directory, approval_decision="expired", approval_ttl_s=1)
        self.assertEqual(summary["application_state"]["post_requests"], 0)
        self.assertEqual(summary["application_state"]["records_created"], 0)
        self.assertEqual(summary["turns"][0]["terminal_reason"], "approval_expired")
        # A card nobody answered ends the turn as a stop does, and says nothing more about
        # the timeout (owner ruling, 2026-09-25); the panel shows no line for it at all.
        self.assertEqual(summary["turns"][0]["answer"], "The requested action was cancelled.")
        self.assertEqual(summary["mutation_journal"][0]["state"], "prepared")
        # Nothing was pressed: past the deadline the card is gone, controls and all, which is
        # what keeps a person out of the dead-end this used to leave them in (P7.5 CR-38).
        self.assertEqual(summary["approvals"][0]["controls_after_expiry"], 0)

    def test_browser_replay_of_the_same_approval_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = self._run_write(directory, replay_approval=True)
        replay = summary["approvals"][0]["replay"]
        self.assertTrue(replay["found"])
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["payload"]["duplicate"])
        self.assertEqual(summary["application_state"]["post_requests"], 1)
        self.assertEqual(summary["application_state"]["records_created"], 1)
        self.assertEqual(summary["mutation_journal"][0]["state"], "completed")

    def test_lost_application_response_is_indeterminate_and_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = self._run_write(directory, write_fault="drop_response")
        self.assertEqual(summary["turns"][0]["terminal_reason"], "indeterminate_write")
        self.assertEqual(summary["application_state"]["post_requests"], 1)
        self.assertEqual(summary["application_state"]["records_created"], 1)
        self.assertEqual(summary["mutation_journal"][0]["state"], "indeterminate")
        self.assertEqual(
            summary["mutation_journal"][0]["reconciliation"]["phase"], "dispatch")

    def test_acknowledged_write_with_failed_readback_is_indeterminate(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = self._run_write(directory, write_fault="fail_readback")
        self.assertEqual(summary["turns"][0]["terminal_reason"], "indeterminate_write")
        self.assertEqual(summary["application_state"]["post_requests"], 1)
        self.assertEqual(summary["application_state"]["records_created"], 1)
        self.assertEqual(summary["mutation_journal"][0]["state"], "indeterminate")
        reconciliation = summary["mutation_journal"][0]["reconciliation"]
        self.assertEqual(reconciliation["phase"], "readback")
        self.assertEqual(reconciliation["record_id"], "radia-perlman")

    def test_two_browser_sessions_cannot_dispatch_the_same_business_key(self):
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as directory, \
                MinimalContactsApplication(write_fault="hold_before_commit") as application:
            root = Path(directory)
            lives = []
            conversations = []

            def start_session(index: int):
                adapter = MinimalContactsAdapter(
                    application.origin, api_token=DEMO_API_TOKEN,
                    allow_mutations=True,
                    mutation_journal=SQLiteMutationJournal(root / "mutations.sqlite3"))
                bound = bind_application(adapter)
                principal = adapter.ports.identity.bind(
                    adapter.identity.page_identity(
                        f"{DEMO_SESSION_COOKIE}={DEMO_SESSION_VALUE}"),
                    delegation_id=f"concurrent-{index}")
                chat = ScriptedDemoChat(WRITE_WALKTHROUGH)

                def runner_factory(channel):
                    executor = AgentTurnExecutor(
                        channel=channel,
                        config=AgentTurnConfig(
                            supported_scope="Create one confirmed synthetic contact.",
                            ledger_path=root / f"audit-{index}.jsonl",
                            step_budget=8, wall_budget_s=30, transport_timeout_s=10,
                            disabled_actions=frozenset(
                                {"click", "fill", "select", "submit", "back"}),
                            plain_business_answer_min_page_actions=0,
                            require_identity=True, allow_business_mutations=True,
                            deployment_profile="production",
                        ),
                        chat=chat, application=bound, control=ReadOnlyControl(),
                        run_id_factory=lambda _message: f"concurrent-{index}")
                    conversation = LiveConversationRunner(channel, executor=executor)
                    conversations.append(conversation)
                    return conversation

                live = LiveProductSession(
                    allowed_origin=bound.descriptor.origin,
                    page_controller_module=bound.descriptor.page_controller_module,
                    runner_factory=runner_factory,
                    navigation_mode=bound.descriptor.navigation_mode,
                    principal=principal,
                ).start()
                lives.append(live)
                return live

            first, second = start_session(1), start_session(2)
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    contexts = []
                    try:
                        pages = []
                        for live in (first, second):
                            context = browser.new_context(viewport={"width": 1280,
                                                                    "height": 900})
                            context.add_cookies([{"name": DEMO_SESSION_COOKIE,
                                                  "value": DEMO_SESSION_VALUE,
                                                  "url": application.origin}])
                            contexts.append(context)
                            context.add_init_script(script=live.bootstrap_script)
                            page = context.new_page()
                            page.goto(application.origin + "/contacts",
                                      wait_until="domcontentloaded", timeout=30_000)
                            page.wait_for_function(
                                "window.__WOA_RUNTIME_READY__ !== undefined", timeout=30_000)
                            page.evaluate(
                                "async () => { await window.__WOA_RUNTIME_READY__; return true; }")
                            pages.append(page)

                        pages[0].evaluate(
                            "task => window.__WOA_RUNTIME_READY__.then(r => r.submitTask(task))",
                            "Create Radia Perlman at radia@example.test")
                        pages[0].get_by_test_id("woa-approve").wait_for(
                            state="visible", timeout=20_000)
                        pages[0].wait_for_timeout(700)
                        pages[0].get_by_test_id("woa-approve").click()
                        self.assertTrue(application.post_received.wait(5),
                                        "first application dispatch did not start")

                        pages[1].evaluate(
                            "task => window.__WOA_RUNTIME_READY__.then(r => r.submitTask(task))",
                            "Create Radia Perlman at radia@example.test")
                        pages[1].get_by_test_id("woa-approve").wait_for(
                            state="visible", timeout=20_000)
                        pages[1].wait_for_timeout(700)
                        pages[1].get_by_test_id("woa-approve").click()
                        blocked = conversations[1].next_outcome(timeout_s=10).result
                        self.assertEqual(blocked.terminal_reason,
                                         "business_mutation_blocked")
                        self.assertEqual(blocked.status.value, "refused")
                        self.assertIn("Nothing was submitted again", blocked.user_answer)
                        self.assertEqual(application.post_requests, 1)

                        application.release_post.set()
                        # The completed write opens the new record, and the page switch is
                        # confirmed first, as the person on that page would.
                        pages[0].locator(
                            'section.woa-approval[data-kind="operate"]').wait_for(
                                state="visible", timeout=15_000)
                        _allow_operation_in_page(pages[0])
                        completed = conversations[0].next_outcome(timeout_s=15).result
                        self.assertEqual(completed.terminal_reason, "agent_finished")
                        self.assertEqual(application.post_requests, 1)
                        self.assertEqual(application.records_created, 1)
                        second_events = [json.loads(line) for line in
                                         (root / "audit-2.jsonl").read_text().splitlines()]
                        blocked_event = next(
                            item for item in second_events
                            if item["event"] == "business_mutation_blocked")
                        self.assertEqual(blocked_event["reason"],
                                         "business_key_unresolved")
                        self.assertFalse(blocked_event["dispatched"])
                        self.assertFalse(blocked_event["side_effect_possible"])
                        self.assertFalse(any(
                            item["event"] == "business_mutation_execution_started"
                            for item in second_events))
                    finally:
                        application.release_post.set()
                        for context in contexts:
                            context.close()
                        browser.close()
            finally:
                application.release_post.set()
                for live in lives:
                    live.close()
                    live.wait(timeout_s=10)


class TheWriteChainAnAuthorCopies(unittest.TestCase):
    """All application ports on a template a person is meant to read, then imitate.

    An example with only a read left every author to invent the hard half for themselves.
    Each test here is one step of the chain and the specific failure it exists to prevent.
    """

    def _composed(self, application, *, allow_mutations=True):
        adapter = MinimalContactsAdapter(
            application.origin, api_token=DEMO_API_TOKEN,
            allow_mutations=allow_mutations,
            mutation_journal=MemoryMutationJournal() if allow_mutations else None)
        return adapter, assert_adapter_contract(self, adapter)

    def _principal(self, adapter):
        page = adapter.identity.page_identity(
            f"{DEMO_SESSION_COOKIE}={DEMO_SESSION_VALUE}")
        return adapter.ports.identity.bind(page, delegation_id="contacts-test")

    def test_all_ports_are_the_applications_own(self):
        """A stock implementation in a port is a decision; an author should see which."""
        with MinimalContactsApplication() as application:
            adapter, bound = self._composed(application)
            ports = bound.ports
            self.assertTrue(ports.identity.required)
            self.assertEqual(ports.routes.application_id, "minimal.contacts")
            self.assertEqual(ports.postconditions.supported_tools, {"create_contact"})
            self.assertEqual(ports.reconciliation.supported_tools, {"create_contact"})
            self.assertIn("create_contact", ports.data.tool_names)
            self.assertEqual(ports.forms.required_scopes, {"contacts:write"})

    def test_registered_tools_are_available_independently_of_contact_skills(self):
        with MinimalContactsApplication() as application:
            adapter, bound = self._composed(application)
            runtime = bound.business_tools
            self.assertIn("create_contact", runtime.tool_names)
            writer = {item["function"]["name"] for item in runtime.available_schemas(
                allow_mutations=True, actor_scopes=("contacts:read", "contacts:write"))}
            reader = {item["function"]["name"] for item in runtime.available_schemas(
                allow_mutations=True, actor_scopes=("contacts:read",))}
            self.assertIn("create_contact", writer)
            self.assertIn("list_contacts", reader)
            self.assertNotIn("create_contact", reader)
            self.assertNotIn("create_contact", {
                item["function"]["name"] for item in runtime.available_schemas(
                    allow_mutations=False, actor_scopes=("contacts:read", "contacts:write"))})

    def test_the_two_credentials_have_to_name_the_same_actor(self):
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            principal = self._principal(adapter)
            self.assertEqual(principal.actor_id, "ada-lovelace")
            self.assertEqual(sorted(principal.scopes),
                             ["contacts:read", "contacts:write"])

    def test_a_page_assertion_from_somewhere_else_is_refused(self):
        """The whole point of binding: neither credential may authorise a write alone."""
        from web_operation_agent.state.identity import (IdentityAssertion,
                                                  PRINCIPAL_CONTEXT_VERSION)
        import time
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            now = int(time.time() * 1000)
            somebody_else = IdentityAssertion(
                schema_version=PRINCIPAL_CONTEXT_VERSION, source="page_session",
                application_id="minimal.contacts", tenant_id="synthetic",
                workspace_id="northstar", actor_id="grace-hopper",
                issued_at=now, expires_at=now + 60_000)
            with self.assertRaisesRegex(PrincipalError, "different principals"):
                adapter.ports.identity.bind(somebody_else, delegation_id="contacts-test")

    def test_a_read_only_composition_never_offers_the_write(self):
        with MinimalContactsApplication() as application:
            adapter, bound = self._composed(application, allow_mutations=False)
            # The lookups are reads under the actor's own context: a record reference, the
            # page entries it may open, and the goal candidates composed from those two and
            # the entity reads, with no authority of its own.
            self.assertEqual(sorted(bound.business_tools.tool_names), [
                "describe_entity", "discover_entities", "find_page_entries", "list_contacts",
                "query_entity_records", "resolve_goal_candidates", "resolve_record_reference",
            ])
            self.assertEqual(bound.business_tools.mutation_tool_names, frozenset())
            self.assertEqual(bound.ports.postconditions.supported_tools, frozenset())
            self.assertEqual(bound.ports.reconciliation.supported_tools, frozenset())

    def test_the_proposal_states_exactly_what_the_user_is_approving(self):
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            executor = adapter.business_tools._executor
            spec = executor.prepare_mutation(
                "create_contact", {"name": "  Radia   Perlman ",
                                   "email": " Radia@Example.TEST "})
            self.assertEqual(spec.args, {"name": "Radia Perlman",
                                         "email": "radia@example.test"})
            self.assertEqual(spec.targets,
                             ("Name: Radia Perlman", "Email: radia@example.test"))
            self.assertEqual(len(application.contacts), 3, "a proposal writes nothing")

    def test_a_duplicate_is_refused_before_the_user_is_asked(self):
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            executor = adapter.business_tools._executor
            with self.assertRaisesRegex(BusinessToolError, "already exists"):
                executor.prepare_mutation(
                    "create_contact", {"name": "grace  HOPPER",
                                       "email": "other@example.test"})
            self.assertEqual(len(application.contacts), 3)

    def test_the_write_is_read_back_under_the_same_actor(self):
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            executor = adapter.business_tools._executor
            result = executor.execute_mutation(
                "create_contact", {"name": "Radia Perlman",
                                   "email": "radia@example.test"})
            self.assertTrue(result.content["readback_verified"])
            self.assertEqual(result.content["contact"]["id"], "radia-perlman")
            self.assertEqual(result.allowed_routes, ("/contacts/radia-perlman",))
            self.assertEqual(len(application.contacts), 4)
            verified = adapter.ports.postconditions.verify(
                "create_contact", {"name": "Radia Perlman",
                                   "email": "radia@example.test"}, result)
            self.assertTrue(verified.verified)

    def test_a_readback_that_disagrees_is_indeterminate_not_a_failure(self):
        """A failure invites a retry, and something is already there."""
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            executor = adapter.business_tools._executor
            original = executor._existing

            def drifted(name):
                rows = original(name)
                return [{**row, "email": "somebody-else@example.test"} for row in rows]

            executor._existing = lambda name: (
                [] if not application.contacts or len(application.contacts) == 3
                else drifted(name))
            with self.assertRaises(BusinessMutationIndeterminate) as raised:
                executor.execute_mutation(
                    "create_contact", {"name": "Radia Perlman",
                                       "email": "radia@example.test"})
            self.assertEqual(raised.exception.phase, "readback")

    def test_a_lost_response_is_indeterminate_and_the_adapter_does_not_retry(self):
        with MinimalContactsApplication(write_fault="drop_response") as application:
            adapter, _bound = self._composed(application)
            executor = adapter.business_tools._executor
            with self.assertRaisesRegex(
                    BusinessMutationIndeterminate, "did not report an outcome") as lost:
                executor.execute_mutation(
                    "create_contact", {"name": "Radia Perlman",
                                       "email": "radia@example.test"})
            self.assertEqual(lost.exception.phase, "dispatch")
            self.assertEqual(application.post_requests, 1)
            self.assertEqual(application.records_created, 1)

    def test_a_failed_readback_after_ack_is_indeterminate_and_not_reposted(self):
        with MinimalContactsApplication(write_fault="fail_readback") as application:
            adapter, _bound = self._composed(application)
            executor = adapter.business_tools._executor
            with self.assertRaisesRegex(
                    BusinessMutationIndeterminate, "readback was unavailable") as unread:
                executor.execute_mutation(
                    "create_contact", {"name": "Radia Perlman",
                                       "email": "radia@example.test"})
            self.assertEqual(unread.exception.phase, "readback")
            self.assertEqual(unread.exception.record_id, "radia-perlman")
            self.assertEqual(application.post_requests, 1)
            self.assertEqual(application.records_created, 1)

    def test_reconciliation_reports_evidence_and_never_a_verdict(self):
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            principal = self._principal(adapter)
            outcome = adapter.ports.reconciliation.reconcile(
                "create_contact", {"name": "Grace Hopper", "email": "grace@example.test"},
                evidence={}, principal=principal)
            self.assertEqual(outcome.outcome, "unknown")
            self.assertTrue(outcome.requires_human)
            self.assertIn("1 contacts currently carry the name", outcome.reason)

    def test_the_application_refuses_a_write_carrying_only_the_browser_session(self):
        """A page that could write with its own cookie is one CSRF from an unasked write."""
        import urllib.error
        with MinimalContactsApplication() as application:
            request = urllib.request.Request(
                application.origin + "/api/contacts",
                json.dumps({"name": "Mallory", "email": "m@example.test"}).encode(),
                {"Cookie": f"{DEMO_SESSION_COOKIE}={DEMO_SESSION_VALUE}",
                 "Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 401)
            self.assertEqual(len(application.contacts), 3)


# ------------------------------------------------------------------------ identity scope
#: A northstar actor must never meet any of this: the other workspace's id, and the values
#: only its records carry. Record names are not listed -- two of them repeat on purpose.
_OTHER_WORKSPACE_MARKERS = ("southwind", "katherine", "radia@")
_READ_ONLY = "rosalind-franklin"


def _audit_events(directory) -> list[dict]:
    audit = next(Path(directory).glob("*/audit.jsonl"))
    return [json.loads(line) for line in audit.read_text().splitlines()]


def _replays(directory, events: list[dict], summary: dict) -> list[dict]:
    """The redacted replay projection of every turn in the run, built from its own ledger."""
    from web_operation_agent.state.traceview import build_trace_case

    cases = []
    for index, run_id in enumerate(dict.fromkeys(event["run_id"] for event in events)):
        ledger = Path(directory) / f"replay-{index}.jsonl"
        ledger.write_text("".join(json.dumps(event) + "\n" for event in events
                                  if event["run_id"] == run_id))
        turn = summary["turns"][index]
        result = Path(directory) / f"replay-{index}.json"
        result.write_text(json.dumps({
            "product_run_id": run_id, "task": turn["task"],
            "agent": {"agent_said": turn["answer"],
                      "terminal_reason": turn["terminal_reason"]}}))
        cases.append(build_trace_case(case_id=f"turn-{index}", label=turn["task"],
                                      result_path=result, ledger_path=ledger,
                                      mode="scripted"))
    return cases


class TheApplicationsOwnAuthorityInARealBrowser(unittest.TestCase):
    """I01 and I02: who the actor is decides what the product may do and may say.

    Everything here runs the real gateway, panel and product turn in Chromium against the
    synthetic application, whose actors and workspaces are its own. The scripted decider
    fixes the Agent's choices; it cannot approve anything or reach past a refusal.
    """

    @classmethod
    def setUpClass(cls):
        required = os.environ.get("WOA_REQUIRE_MINIMAL_DEMO_BROWSER") == "1"
        if not _chromium_launches():
            if required:
                raise RuntimeError("Chromium is required for minimal-demo qualification")
            raise unittest.SkipTest("Chromium is unavailable")

    def _run(self, directory, script, tasks, **changes):
        chat = ScriptedDemoChat(script)
        with patch("testapps.contacts.demo._chat", return_value=(chat, None)):
            summary = run_demo(DemoConfig(
                tasks=tuple(tasks), headless=True, output_dir=Path(directory),
                keep_open_seconds=0, turn_timeout_s=45, capture_panel=True, **changes))
        return summary, chat

    def assertNothingFromTheOtherWorkspace(self, surfaces: dict, markers=_OTHER_WORKSPACE_MARKERS):
        for surface, text in surfaces.items():
            folded = text.casefold()
            for marker in markers:
                self.assertNotIn(marker, folded, f"{marker!r} reached {surface}")

    def _surfaces(self, directory, summary, chat) -> dict:
        events = _audit_events(directory)
        return {
            "model context": "\n".join(chat.seen),
            "audit ledger": json.dumps(events, ensure_ascii=False),
            "replay projection": json.dumps(_replays(directory, events, summary),
                                            ensure_ascii=False),
            "user answers": json.dumps([turn.get("answer") for turn in summary["turns"]],
                                       ensure_ascii=False),
            "page": summary["last_page_text"] + summary["panel"]["page_text_after_reload"],
            "live panel": summary["panel"]["live"]["text"],
            "replayed panel": summary["panel"]["replayed"]["text"],
        }

    # -- I01 ------------------------------------------------------------------------------
    def test_a_read_only_actor_s_write_is_refused_by_tool_authorization_and_not_rerouted(self):

        script = (
            ("load_skill", {"skill_id": "minimal.contacts-create"}),
            ("list_contacts", {"name_contains": "Radia"}),
            # The same effect by way of the page, before the tool: refused before dispatch.
            ("click", {"ref": "e1"}),
            ("create_contact", {"name": "Radia Perlman", "email": "radia@example.test"}),
            # Never asked for: the refusal ends the turn, so no page route follows it.
            ("fill", {"ref": "e1", "value": "Radia Perlman"}),
            ("submit", {"ref": "e1"}),
        )
        with tempfile.TemporaryDirectory() as directory:
            summary, chat = self._run(
                directory, script, ("Create a contact for Radia Perlman at radia@example.test.",),
                actor=_READ_ONLY, allow_mutations=True)
            events = _audit_events(directory)

        turn = summary["turns"][0]
        self.assertEqual(turn["status"], "refused")
        self.assertEqual(turn["terminal_reason"], "permission_denied")
        self.assertIn("not permitted", turn["answer"])
        self.assertIn("that operation was not carried out", turn["answer"])
        self.assertIn("not permitted", summary["panel"]["live"]["text"])
        self.assertEqual(summary["panel"]["live"]["state"], "refused")
        self.assertEqual(summary["panel"]["live"]["approval_cards"], 0)
        self.assertEqual(chat.calls, 4, "nothing was asked of the model after the refusal")

        state = summary["application_state"]
        self.assertEqual(state["write_requests"], 0, "no write reached the application")
        self.assertEqual(state["records_created"], 0)
        self.assertEqual(state["workspace_counts"], {"northstar": 3, "southwind": 3})
        self.assertEqual(summary["mutation_journal"], [])

        kinds = [event["event"] for event in events]
        self.assertNotIn("business_mutation_proposed", kinds)
        self.assertNotIn("business_mutation_execution_started", kinds)
        blocked = [event for event in events if event["event"] == "action_policy_blocked"]
        self.assertEqual([event["tool"] for event in blocked], ["click"])
        self.assertFalse(blocked[0]["dispatched"])
        self.assertFalse(any(event["event"] == "action_receipt"
                             and event.get("tool") in {"click", "fill", "submit"}
                             for event in events))
        denied = next(event for event in events
                      if event["event"] == "business_mutation_permission_denied")
        self.assertEqual(denied["layer"], "tool_authorization")
        self.assertEqual(denied["required_scope"], "contacts:write")
        self.assertEqual(denied["error_code"], "business_permission_denied")
        self.assertFalse(denied["dispatched"])
        self.assertFalse(denied["side_effect_possible"])
        self.assertEqual(denied["identity"]["scopes"], ["contacts:read"])
        # Scored as the product's behaviour, not written off as a broken apparatus.
        self.assertEqual(_classify_woa_outcome(terminal_reason=turn["terminal_reason"],
                                              events=events),
                         (None, "permission_denied"))

    def test_the_application_refuses_even_when_the_integration_granted_too_much(self):
        """Defence in depth: a principal forged with the write scope still cannot write."""

        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(DemoConfig(
                scripted=True, headless=True, allow_mutations=True, actor=_READ_ONLY,
                forged_scopes=("contacts:read", "contacts:write"),
                output_dir=Path(directory), keep_open_seconds=0, turn_timeout_s=45))
            events = _audit_events(directory)

        turn = summary["turns"][0]
        self.assertIn("Create exactly one contact", summary["approvals"][0]["text"])
        self.assertEqual(turn["terminal_reason"], "permission_denied")
        self.assertIn("not permitted", turn["answer"])
        state = summary["application_state"]
        self.assertEqual(state["write_requests"], 1)
        self.assertEqual(state["refused_writes"], 1, "the application itself said no")
        self.assertEqual(state["post_requests"], 0)
        self.assertEqual(state["records_created"], 0)
        self.assertEqual(summary["mutation_journal"][0]["state"], "failed")
        denied = next(event for event in events
                      if event["event"] == "business_mutation_permission_denied")
        self.assertEqual(denied["layer"], "application")
        self.assertTrue(denied["dispatched"])
        self.assertFalse(denied["side_effect_possible"])
        self.assertNotIn("business_mutation_indeterminate",
                         [event["event"] for event in events])
        self.assertEqual(_classify_woa_outcome(terminal_reason=turn["terminal_reason"],
                                              events=events),
                         (None, "permission_denied"))

    def test_a_refused_read_is_handed_back_and_reveals_no_record(self):
        """The refusal continues the turn, and no refused record reaches any surface (D13).

        The turn used to end here, which also threw away whatever this account may see. It
        now goes on: what the refusal takes away is the page as a source of facts, not the
        turn. The model may still move around the application -- a principal narrower than
        the application does not make the application's own pages secret -- but nothing it
        was refused can become an answer, and an actor the application refuses outright
        reaches nothing at all.
        """

        script = (
            ("load_skill", {"skill_id": "minimal.contacts.query"}),
            ("list_contacts", {}),
            # The model tries the page next. It may go there, and what it sees there cannot
            # become a fact in the answer.
            ("goto", {"url": "/contacts/grace-hopper"}),
            ("wait_for", {"ms": 500}),
            ("finish", {"outcome": "acted"}),
        )
        cases = (
            # The framework's scope check: the principal carries no read scope.
            ("tool_authorization", _READ_ONLY, ("contacts:write",), False),
            # The application's own: the principal claims read, the application refuses it.
            ("application", "hedy-lamarr", ("contacts:read",), True),
        )
        for layer, actor, scopes, dispatched in cases:
            with self.subTest(layer), tempfile.TemporaryDirectory() as directory:
                summary, chat = self._run(directory, script, ("List my contacts.",),
                                          actor=actor, forged_scopes=scopes)
                events = _audit_events(directory)
                turn = summary["turns"][0]
                self.assertEqual(chat.calls, len(script), "the refusal comes back to the model")
                if layer == "application":
                    # This actor is refused every read, its pages included, so the turn has
                    # nothing to answer from and ends on the refusal, as it did before.
                    self.assertEqual(turn["terminal_reason"], "permission_denied")
                    self.assertIn("not permitted", turn["answer"])
                    # Its goto is refused before dispatch; the one page action the script
                    # still takes is the wait after it, and nothing was opened.
                    receipts = [row for row in events if row["event"] == "action_receipt"]
                    self.assertEqual([row["tool"] for row in receipts], ["wait_for"])
                    self.assertFalse(any(row["navigated"] for row in receipts))
                else:
                    # The principal is narrower than the application, which still shows this
                    # person its own pages. The turn reports what it did and states the part
                    # it was refused; no refused record is in it.
                    self.assertEqual(turn["terminal_reason"], "agent_finished")
                    self.assertIn("does not have permission", turn["answer"])
                self.assertNotIn("Grace Hopper", turn["answer"])
                self.assertNotIn("business_tool_result", [e["event"] for e in events])
                self.assertEqual([e["event"] for e in events].count(
                    "page_answer_evidence"), 0, "the page is not a source after a refusal")
                denied = next(event for event in events
                              if event["event"] == "business_tool_permission_denied")
                self.assertEqual(denied["layer"], layer)
                self.assertEqual(denied["dispatched"], dispatched)
                self.assertFalse(denied["side_effect_possible"])
                self.assertTrue(denied["turn_continued"])
                self.assertEqual(_classify_woa_outcome(terminal_reason=turn["terminal_reason"],
                                                      events=events)[0], None,
                                 "a refused read is product behaviour, not an apparatus fault")
                if layer == "application":
                    # The application refuses this actor every read, its pages included, so
                    # no record may reach anything. (A principal merely narrower than the
                    # application is a different case: the page still shows what the
                    # application lets the actor see.)
                    self.assertNothingFromTheOtherWorkspace({
                        "model context": "\n".join(chat.seen),
                        "audit ledger": json.dumps(events, ensure_ascii=False),
                        "live panel": summary["panel"]["live"]["text"],
                        "page": summary["last_page_text"],
                    }, markers=("@example.test", "southwind"))

    # -- I02 ------------------------------------------------------------------------------
    def test_a_same_name_record_in_another_workspace_reaches_no_surface(self):
        script = (
            ("load_skill", {"skill_id": "minimal.contacts.query"}),
            ("list_contacts", {"name_contains": "Grace Hopper"}),
            ("compute", {"operation": "count", "dataset_ref": "call2-query#1"}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete", "conclusions": [
                    {"kind": "aggregate", "evidence_ref": "calc#3"}]}}),
            ("load_skill", {"skill_id": "minimal.contacts.query"}),
            ("list_contacts", {"name_contains": "Grace Hopper"}),
            ("goto", {"url": "/contacts/grace-hopper"}),
            ("wait_for", {"ms": 500}),
            ("finish", {"outcome": "acted"}),
        )
        with tempfile.TemporaryDirectory() as directory:
            summary, chat = self._run(
                directory, script,
                ("How many contacts named Grace Hopper can I see?",
                 "Open Grace Hopper's contact record."))
            surfaces = self._surfaces(directory, summary, chat)
            events = _audit_events(directory)

        counted, opened = summary["turns"]
        self.assertEqual(counted["status"], "finished")
        # A count of a search by name is headed by the name it searched for.
        self.assertIn('**Records matching "Grace Hopper"**：1', counted["answer"])
        self.assertEqual(opened["status"], "finished")
        self.assertTrue(summary["last_url"].endswith("/contacts/grace-hopper"))
        self.assertIn("grace@example.test", summary["last_page_text"])
        self.assertEqual([event["audit"]["returned_count"] for event in events
                          if event["event"] == "business_tool_result"], [1, 1])
        self.assertNothingFromTheOtherWorkspace(surfaces)

    def test_absence_here_is_claimed_only_for_the_scope_this_actor_can_search(self):
        script = (
            ("load_skill", {"skill_id": "minimal.contacts.query"}),
            ("list_contacts", {"name_contains": "Katherine Johnson"}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete", "conclusions": [
                    {"kind": "not_found", "evidence_ref": "call2-query#1"}]}}),
        )
        with tempfile.TemporaryDirectory() as directory:
            summary, chat = self._run(directory, script,
                                      ("Find Katherine Johnson's contact.",))
            surfaces = self._surfaces(directory, summary, chat)

        turn = summary["turns"][0]
        self.assertEqual(turn["status"], "finished")
        self.assertEqual(chat.calls, 3)
        self.assertEqual(turn["page_actions"], 0)
        self.assertIn("Not found", turn["answer"])
        self.assertIn("Katherine Johnson", turn["answer"])
        self.assertIn("the current account can access", turn["answer"])
        self.assertIn("not a claim that it exists nowhere", turn["answer"])
        self.assertNotIn("does not exist", turn["answer"])
        self.assertIn("the current account can access", summary["panel"]["live"]["text"])
        # The name is the user's own words; what only the other workspace holds is not.
        self.assertNothingFromTheOtherWorkspace(
            surfaces, markers=("southwind", "katherine@", "radia"))

    def test_a_same_name_record_elsewhere_does_not_count_as_already_existing_here(self):
        with tempfile.TemporaryDirectory() as directory:
            chat = ScriptedDemoChat(WRITE_WALKTHROUGH)
            with patch("testapps.contacts.demo._chat", return_value=(chat, None)):
                summary = run_demo(DemoConfig(
                    scripted=True, headless=True, allow_mutations=True,
                    output_dir=Path(directory), keep_open_seconds=0, turn_timeout_s=45))
            events = _audit_events(directory)

        from testapps.contacts.app import SOUTHWIND_CONTACTS
        from testapps.contacts.demo import workspace_digest

        self.assertEqual(summary["turns"][0]["status"], "finished")
        # The duplicate check read this workspace, found no Radia Perlman, and let the
        # proposal through, although the other workspace has one.
        self.assertEqual([event["audit"]["returned_count"] for event in events
                          if event["event"] == "business_tool_result"], [0])
        self.assertEqual([event["event"] for event in events].count(
            "business_mutation_proposed"), 1)
        state = summary["application_state"]
        self.assertEqual(state["post_requests"], 1)
        self.assertEqual(state["records_created"], 1)
        self.assertEqual(state["workspace_counts"], {"northstar": 4, "southwind": 3})
        self.assertEqual(state["workspace_digests"]["southwind"],
                         workspace_digest([dict(item) for item in SOUTHWIND_CONTACTS]))
        self.assertEqual(summary["mutation_journal"][0]["state"], "completed")
        self.assertIn("radia@example.test", summary["last_page_text"])
        self.assertNothingFromTheOtherWorkspace({
            "model context": "\n".join(chat.seen),
            "audit ledger": json.dumps(events, ensure_ascii=False),
        }, markers=("southwind", "katherine"))


class TheApplicationsOwnAuthority(unittest.TestCase):
    """The same two boundaries without a browser: the application, then the adapter."""

    def _request(self, application, method, path, *, token="", cookie="", body=None,
                 origin=""):
        import urllib.error
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if cookie:
            headers["Cookie"] = f"{DEMO_SESSION_COOKIE}={cookie}"
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(
            application.origin + path,
            json.dumps(body).encode() if body is not None else None, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_the_application_refuses_a_read_only_actor_s_write_by_either_credential(self):
        from testapps.contacts.app import ACTORS_BY_ID
        reader = ACTORS_BY_ID[_READ_ONLY]
        with MinimalContactsApplication() as application:
            status, body = self._request(application, "GET", "/api/whoami", token=reader.token)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["permissions"], ["contacts:read"])
            radia = {"name": "Radia Perlman", "email": "radia@example.test"}
            self.assertEqual(self._request(application, "POST", "/api/contacts",
                                           token=reader.token, body=radia)[0], 403)
            self.assertEqual(self._request(application, "POST", "/api/contacts",
                                           cookie=reader.session, body=radia)[0], 401)
            self.assertEqual(application.refused_writes, 2)
            self.assertEqual(application.post_requests, 0)
            self.assertEqual(len(application.contacts), 3)

    def test_the_human_form_endpoint_requires_same_origin_and_application_write_access(self):
        from testapps.contacts.app import ACTORS_BY_ID, DEMO_SESSION_VALUE
        reader = ACTORS_BY_ID[_READ_ONLY]
        body = {"name": "Margaret Hamilton", "email": "margaret@example.test"}
        with MinimalContactsApplication() as application:
            self.assertEqual(self._request(
                application, "POST", "/api/browser/contacts",
                cookie=DEMO_SESSION_VALUE, body=body)[0], 403)
            self.assertEqual(self._request(
                application, "POST", "/api/browser/contacts",
                cookie=reader.session, origin=application.origin, body=body)[0], 403)
            status, payload = self._request(
                application, "POST", "/api/browser/contacts",
                cookie=DEMO_SESSION_VALUE, origin=application.origin, body=body)
            self.assertEqual(status, 201)
            self.assertEqual(json.loads(payload)["contact"]["name"], "Margaret Hamilton")

    def test_a_read_only_actor_binds_with_only_the_scope_the_application_grants(self):
        from testapps.contacts.app import ACTORS_BY_ID
        from web_operation_agent.business.runtime import BusinessToolPermissionError
        reader = ACTORS_BY_ID[_READ_ONLY]
        with MinimalContactsApplication() as application:
            adapter = MinimalContactsAdapter(
                application.origin, api_token=reader.token, allow_mutations=True,
                mutation_journal=MemoryMutationJournal())
            assert_adapter_contract(self, adapter)
            principal = adapter.ports.identity.bind(
                adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={reader.session}"),
                delegation_id="read-only")
            self.assertEqual(principal.scopes, ("contacts:read",))
            # And with the framework's check stepped around entirely, the application still
            # refuses, and says so in a type rather than in words.
            with self.assertRaises(BusinessToolPermissionError) as refused:
                adapter.business_tools._executor.execute_mutation(
                    "create_contact", {"name": "Radia Perlman",
                                       "email": "radia@example.test"})
            self.assertEqual(refused.exception.layer, "application")
            self.assertEqual(refused.exception.code, "business_permission_denied")
            self.assertEqual(application.records_created, 0)

    def test_the_application_refuses_a_read_to_an_actor_it_grants_nothing(self):
        from testapps.contacts.app import ACTORS_BY_ID
        from web_operation_agent.business.runtime import BusinessToolPermissionError
        from web_operation_agent.state.identity import PrincipalError
        nobody = ACTORS_BY_ID["hedy-lamarr"]
        with MinimalContactsApplication() as application:
            self.assertEqual(self._request(application, "GET", "/api/contacts",
                                           token=nobody.token)[0], 403)
            self.assertEqual(self._request(application, "GET", "/contacts",
                                           cookie=nobody.session)[0], 403)
            adapter = MinimalContactsAdapter(application.origin, api_token=nobody.token)
            with self.assertRaises(BusinessToolPermissionError) as refused:
                adapter.business_tools._executor.execute("list_contacts", {})
            self.assertEqual(refused.exception.layer, "application")
            # And an honest identity port gives her no principal to act with at all.
            with self.assertRaisesRegex(PrincipalError, "none of the scopes"):
                adapter.ports.identity.bind(
                    adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={nobody.session}"),
                    delegation_id="nobody")

    def test_each_workspace_sees_only_its_own_records_through_every_credential(self):
        from testapps.contacts.app import ACTORS_BY_ID
        with MinimalContactsApplication() as application:
            for actor, expected in (("ada-lovelace", "grace@example.test"),
                                    ("mary-jackson", "grace.hopper@southwind.test")):
                person = ACTORS_BY_ID[actor]
                _status, api = self._request(application, "GET", "/api/contacts",
                                             token=person.token)
                _status, page = self._request(application, "GET", "/contacts",
                                              cookie=person.session)
                for text in (api, page):
                    self.assertIn(expected, text)
                    other = ("southwind" if person.workspace == "northstar"
                             else "example.test")
                    self.assertNotIn(other, text.casefold())
            self.assertEqual(self._request(application, "GET", "/api/contacts")[0], 401)
            self.assertEqual(self._request(application, "GET", "/contacts")[0], 401)

    def test_the_adapter_s_duplicate_check_and_reconciliation_see_only_this_workspace(self):
        with MinimalContactsApplication() as application:
            adapter = MinimalContactsAdapter(
                application.origin, api_token=DEMO_API_TOKEN, allow_mutations=True,
                mutation_journal=MemoryMutationJournal())
            executor = adapter.business_tools._executor
            # Radia Perlman exists only in the other workspace: not a duplicate here.
            spec = executor.prepare_mutation(
                "create_contact", {"name": "Radia Perlman", "email": "radia@example.test"})
            self.assertEqual(spec.targets[0], "Name: Radia Perlman")
            self.assertEqual(
                [row["email"] for row in executor._existing("Grace Hopper")],
                ["grace@example.test"])
