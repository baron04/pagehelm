"""The demo drives real approval cards with synthetic REST data and no model."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.app import (MinimalContactsApplication, DEMO_API_TOKEN,
                                   DEMO_SESSION_COOKIE, DEMO_ACTORS)
from testapps.contacts.goal import ContactsGoalPort
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.composition import bind_application
from web_operation_agent.composition.live import LiveConversationRunner, LiveProductSession
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.execution.events import TurnEvents
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.tests import browser_only
from web_operation_agent.tests.support.skill_chat import SkillDeclarationChat
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent_yudao.reads.client import YudaoAccount
from web_operation_agent_yudao.host.demo import YudaoSession, _next_outcome, _session_sources


class DemoSourceTests(unittest.TestCase):
    def test_missing_visible_result_keeps_normal_outcome_and_marks_measurement_incomplete(self):
        events = TurnEvents()
        events.accepted("normal-turn")
        result = SimpleNamespace(message=SimpleNamespace(message_id="normal-turn"))
        outcome = SimpleNamespace(error="", result=result)
        conversation = SimpleNamespace(next_outcome=lambda **kwargs: outcome)
        with patch("web_operation_agent_yudao.host.demo._observe_final_result", side_effect=TimeoutError):
            returned = _next_outcome(None, conversation, 1, [], turn_events=events)
        self.assertIs(returned, outcome)
        facts = events.snapshot("normal-turn")
        self.assertFalse(facts["delivery_complete"])
        self.assertNotIn("final_result_visible_observed", [e["kind"] for e in facts["events"]])

    def test_absent_lookup_is_never_attached_as_a_compiled_rule_source(self):
        session = YudaoSession("http://synthetic.invalid", YudaoAccount("synthetic", "unused"),
                               {}, (), Path("/tmp/unused"))
        for supplied in (None, SourceCapability.none()):
            capability, rules = _session_sources(replace(session, source=supplied))
            self.assertEqual(capability, SourceCapability.none())
            self.assertIsNone(rules)
        compiled = SimpleNamespace(rules=lambda entities: ({}, {}), release="synthetic")
        capability, rules = _session_sources(replace(session, source=compiled))
        self.assertIs(rules, compiled)
        self.assertEqual(capability, SourceCapability.none())
        with self.assertRaises(TypeError):
            _session_sources(replace(session, source=compiled, executor_mode="hybrid"))


@browser_only
class DemoApprovalTests(unittest.TestCase):
    def test_two_consecutive_cards_then_read_settle_without_waiting_for_all_cards_to_detach(self):
        self._run("approved", 2)

    def test_declining_first_card_stops_the_remaining_operations(self):
        self._run("denied", 0)

    def _run(self, decision, expected_writes):
        from playwright.sync_api import sync_playwright
        playwright = self.enterContext(sync_playwright())
        with tempfile.TemporaryDirectory() as tmp:
            app = MinimalContactsApplication().start()
            self.addCleanup(app.close)
            adapter = MinimalContactsAdapter(app.origin, api_token=DEMO_API_TOKEN,
                allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
            bound = bind_application(adapter)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="demo-approval-proof")
            sid = "minimal.contacts.contact-maintenance"
            class Chat(SkillDeclarationChat):
                def usage(self): return {"provider_calls": 0}
            chat = Chat((("select_skills", {"skill_ids": [sid]}),
                [("create_contact", {"request_intent": "execute", "inputs": {
                    "name": {"source": 0, "text": "Synthetic Bay"}, "email": {"source": 0, "text": "bay@example.test"}}}),
                 ("create_contact", {"request_intent": "execute", "inputs": {
                    "name": {"source": 0, "text": "Synthetic Vale"}, "email": {"source": 0, "text": "vale@example.test"}}}),
                 ("declare_goal", {"deliverables": [{"kind": "fact", "entity": "contact",
                    "record": "Synthetic Bay", "fields": ["email"]}]}), ("finish_skill_task", {})]))
            holder = {}
            observed = TurnEvents()
            def runner(channel):
                executor = GoalTurnExecutor(channel=channel, config=AgentTurnConfig(
                    supported_scope="Contacts", ledger_path=Path(tmp)/"audit.jsonl",
                    require_identity=True, deployment_profile="test", allow_business_mutations=True,
                    step_budget=12, wall_budget_s=30), application=bound, chat=chat,
                    goal_port=ContactsGoalPort(adapter), control=ReadOnlyControl(),
                    run_id_factory=lambda m: "demo-approval-proof", readings=1, turn_events=observed)
                holder["conversation"] = LiveConversationRunner(channel, executor=executor)
                return holder["conversation"]
            live = LiveProductSession(allowed_origin=app.origin,
                page_controller_module=bound.descriptor.page_controller_module,
                runner_factory=runner, principal=principal).start()
            self.addCleanup(live.close)
            browser = playwright.chromium.launch()
            self.addCleanup(browser.close)
            context = browser.new_context(viewport={"width": 1440, "height": 960})
            context.add_cookies([{"name": DEMO_SESSION_COOKIE, "value": DEMO_ACTORS[0].session,
                                 "url": app.origin}])
            context.add_init_script(script=live.bootstrap_script)
            page = context.new_page()
            page.goto(app.origin+"/contacts")
            page.wait_for_function("window.__WOA_RUNTIME_READY__ !== undefined")
            page.evaluate("() => window.__WOA_RUNTIME_READY__.then(r => r.submitTask('Create Synthetic Bay with bay@example.test and Synthetic Vale with vale@example.test, then show the email of Synthetic Bay.'))")
            cards = []
            outcome = _next_outcome(page, holder["conversation"], 30, [], write_cards=cards,
                                    on_write_card=lambda card: decision, turn_events=observed)
            self.assertFalse(outcome.error, outcome.error)
            events = list(map(json.loads, (Path(tmp)/"audit.jsonl").read_text().splitlines()))
            self.assertEqual(len(cards), 2 if decision == "approved" else 1,
                             (outcome.result.user_answer, [e for e in events if e["event"] == "skill_declaration_refused"]))
            self.assertEqual(app.write_requests, expected_writes)
            self.assertEqual(outcome.result.terminal_reason,
                             "agent_finished" if decision == "approved" else "approval_denied")
            if decision == "approved":
                self.assertIn("bay@example.test", outcome.result.user_answer)
            self.assertEqual(sum(e["event"] == "business_mutation_execution_started" for e in events), expected_writes)
            self.assertEqual(chat.usage()["provider_calls"], 0)
            facts = observed.snapshot(outcome.result.message.message_id)
            self.assertEqual(facts["events"][0]["kind"], "request_received")
            kinds = [e["kind"] for e in facts["events"]]
            self.assertEqual(kinds.count("first_write_card_visible_observed"), 1)
            self.assertEqual(kinds.count("final_result_visible_observed"), 1)
            self.assertEqual(kinds.count("approval_driver_wait_begin"), len(cards))
            self.assertEqual(kinds.count("approval_driver_wait_end"), len(cards))
            self.assertLess(kinds.index("first_write_card_visible_observed"), kinds.index("approval_driver_wait_begin"))
            self.assertEqual(len(facts["receipts"]), expected_writes)
            self.assertEqual(facts["unbound_mutations"], 0)
