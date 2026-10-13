"""A structurally different REST adapter proves both entry points use one write chain."""
import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from testapps.contacts.app import MinimalContactsApplication, DEMO_API_TOKEN, DEMO_SESSION_COOKIE, DEMO_ACTORS
from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.goal import ContactsGoalPort
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.errors import (BusinessMutationBlockedError,
    BusinessMutationRefusedError, BusinessToolUnavailableError)
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.composition import bind_application
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor, AgentTurnStatus
from web_operation_agent.execution import turn
from web_operation_agent.execution.events import TurnEvents
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.goal.skill_plan import bind_references
from web_operation_agent.business.effects import RequestedEffect, canonical
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.support.skill_chat import SkillDeclarationChat as _WireChat
from web_operation_agent.tests.goal.test_complete_task import effect, intent, wire
from web_operation_agent.tests.goal.test_repository_answers import Lookup


class _Chat(_WireChat):
    """Explicit independent intent and draft scripts; never interpret request text."""
    def __init__(self, script, *, expected=None):
        super().__init__(script)
        self.expected = expected

    def complete(self, messages, tools, run_id, **kwargs):
        names = {t["function"]["name"] for t in tools}
        if names == {"declare_skill_intent"}:
            if self.expected is None:
                raise AssertionError("fixture requires an explicit independent intent")
            self.messages.append([dict(m) for m in messages])
            self.tool_names.append(names)
            return wire(("declare_skill_intent", self.expected))
        reply = super().complete(messages, tools, run_id, **kwargs)
        return reply


class SkillExecutionTests(unittest.TestCase):
    def test_reference_sets_bind_all_real_rest_receipts_and_reject_an_unverified_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            self.addCleanup(application.close)
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="reference-set-proof")
            channel = _Channel(page_url=application.origin + "/contacts", principal=principal)
            chat = _Chat([("select_skills", {"skill_ids": ["minimal.contacts.contact-maintenance"]}), [
                ("create_contact", {"name": "Synthetic Alder", "email": "alder@example.test"}),
                ("create_contact", {"name": "Synthetic Birch", "email": "birch@example.test"}),
                ("finish_skill_task", {})]])
            executor = GoalTurnExecutor(channel=channel,
                config=AgentTurnConfig(supported_scope="Contacts", ledger_path=Path(tmp)/"audit.jsonl",
                    require_identity=True, deployment_profile="test", allow_business_mutations=True),
                application=bind_application(adapter), chat=chat, goal_port=ContactsGoalPort(adapter),
                control=ReadOnlyControl(), run_id_factory=lambda m: "reference-set-proof", readings=1)
            handler = adapter.ports.operations.tools.get("create_contact").handler
            dispatch, receipts = handler.dispatch, {}
            def capture(*args, **kwargs):
                result = dispatch(*args, **kwargs)
                receipts[len(receipts)] = result.content
                return result
            with patch.object(handler, "dispatch", side_effect=capture), \
                    patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "m1", "text":
                    "Create Synthetic Alder with alder@example.test and Synthetic Birch with birch@example.test.",
                    "page_url": application.origin + "/contacts"})
            self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
            self.assertEqual(application.write_requests, 2)
            self.assertEqual(len(channel.approvals), 2)
            inputs = {"members": ["@result:1", "@result:0"], "note": "@result:0"}
            bound = bind_references(inputs, receipts, ("members",))
            self.assertEqual(bound, {"members": ["Synthetic Birch", "Synthetic Alder"], "note": "@result:0"})
            projected = RequestedEffect("contacts", "create", values_json=canonical({"members": bound["members"]}))
            reordered = RequestedEffect("contacts", "create", values_json=canonical({"members": list(reversed(bound["members"]))}))
            self.assertTrue(projected.same_change(reordered))
            incomplete = RequestedEffect("contacts", "create", values_json=canonical({"members": bound["members"][:1]}))
            self.assertFalse(projected.same_change(incomplete))
            with self.assertRaises(ValueError):
                bind_references(inputs, {**receipts, 1: {**receipts[1], "verified": False}}, ("members",))
            self.assertEqual(application.write_requests, 2)

    def test_default_single_host_preserves_full_values_in_real_rest_write(self):
        self._creation("single", 2)

    def test_registered_read_result_reaches_agent_before_real_rest_creation(self):
        self._creation("single", 3, lookup=True)

    def test_selection_feedback_recovers_native_admission_before_real_rest_creation(self):
        self._creation("single", 3, invalid_scope=True)

    def test_source_enabled_selection_preserves_real_rest_creation_without_navigation(self):
        self._creation("single", 2, source_scope=True)

    def test_accidental_knowledge_selection_for_read_request_has_no_rest_write_or_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            self.addCleanup(application.close)
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="selection-read")
            channel = _Channel(page_url=application.origin + "/contacts", principal=principal)
            chat = _Chat([("select_skills", {"skill_ids": ["minimal.contacts.contact-maintenance"]}),
                [("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "contacts",
                    "metric": {"op": "count"}}]}), ("finish_skill_task", {})]])
            executor = GoalTurnExecutor(channel=channel,
                config=AgentTurnConfig(supported_scope="Contacts", ledger_path=Path(tmp)/"audit.jsonl",
                    require_identity=True, deployment_profile="test", allow_business_mutations=True),
                application=bind_application(adapter), chat=chat, goal_port=ContactsGoalPort(adapter),
                control=ReadOnlyControl(), run_id_factory=lambda message: "selection-read", readings=1)
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "m1", "text": "Count contacts.",
                    "page_url": application.origin + "/contacts"})
            self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
            self.assertEqual(application.write_requests, 0)
            self.assertFalse(channel.approvals)
            self.assertIn("Count contacts.", str(chat.messages[-1]))

    def test_unread_selection_consults_once_then_recovers_ordinary_declaration_without_rest_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            self.addCleanup(application.close)
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="scope-stop")
            channel = _Channel(page_url=application.origin + "/contacts", principal=principal)
            chat = _Chat([("select_skills", {"skill_ids": []})] * 2 + [("continue_goal", {})] +
                [("declare_goal", {"deliverables": [], "unsupported": "No valid task declaration"})] * 2)
            executor = GoalTurnExecutor(channel=channel,
                config=AgentTurnConfig(supported_scope="Contacts", ledger_path=Path(tmp)/"audit.jsonl",
                    require_identity=True, deployment_profile="test", allow_business_mutations=True),
                application=bind_application(adapter), chat=chat, goal_port=ContactsGoalPort(adapter),
                control=ReadOnlyControl(), run_id_factory=lambda message: "scope-stop", readings=2)
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "m1", "text":
                    "Create Synthetic Willow with willow@example.test.",
                    "page_url": application.origin + "/contacts"})
            self.assertEqual(result.terminal_reason, "agent_refused")
            self.assertEqual(len(chat.messages), 5)
            self.assertEqual(chat.tool_names[:2], [{"declare_scope", "select_skills"}] * 2)
            self.assertEqual(chat.tool_names[2], {"continue_goal", "select_skills"})
            self.assertEqual(application.write_requests, 0)
            self.assertFalse(channel.approvals)
            events = [json.loads(line) for line in (Path(tmp)/"audit.jsonl").read_text().splitlines()]
            self.assertIn("goal_declared", {event["event"] for event in events})
            self.assertEqual(sum(event["event"] == "goal_skill_consulted" for event in events), 1)

    def test_selection_without_business_fields_preserves_full_anchored_rest_write(self):
        self._creation("anchored", 3)

    def _creation(self, mode, calls, *, lookup=False, invalid_scope=False, source_scope=False):
        with tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            self.addCleanup(application.close)
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="field-review-proof")
            channel = _Channel(page_url=application.origin+"/contacts", principal=principal)
            proposal = ("create_contact", {"name": "Synthetic Willow", "email": "willow@example.test"})
            script = [*([("select_skills", {"skill_ids": []})] if invalid_scope else []),
                ("select_skills", {"skill_ids": ["minimal.contacts.contact-maintenance"]}),
                *([("list_contacts", {"name_contains": "Synthetic Willow"})] if lookup else []),
                [proposal, ("finish_skill_task", {})]]
            chat = _Chat(script, expected=intent(effect({"name": "Synthetic Willow", "email": "willow@example.test"})))
            executor = GoalTurnExecutor(complete_request_check=mode, channel=channel, config=AgentTurnConfig(supported_scope="Contacts",
                ledger_path=Path(tmp)/"audit.jsonl", require_identity=True, deployment_profile="test",
                allow_business_mutations=True,
                source=SourceCapability(runtime_source=True, lookup=Lookup()) if source_scope else SourceCapability.none()),
                repository_answers=source_scope, application=bind_application(adapter), chat=chat,
                goal_port=ContactsGoalPort(adapter), control=ReadOnlyControl(),
                run_id_factory=lambda m: "field-review-proof", readings=1)
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "m1", "text":
                    "Create Synthetic Willow with willow@example.test.",
                    "page_url": application.origin+"/contacts"})
            self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
            self.assertEqual(len(chat.messages), calls)
            self.assertEqual(application.write_requests, 1)
            self.assertEqual(len(channel.approvals), 1)
            self.assertEqual({d.label for d in channel.approvals[0].details}, {"name", "email"})
            self.assertEqual([r["email"] for r in application.contacts if r["name"] == "Synthetic Willow"],
                             ["willow@example.test"])
            if source_scope:
                events = [json.loads(line) for line in (Path(tmp)/"audit.jsonl").read_text().splitlines()]
                declared = next(e for e in events if e["event"] == "goal_skill_selected")
                self.assertEqual(declared["skill_ids"], ["minimal.contacts.contact-maintenance"])
                self.assertFalse(any(e.get("tool") == "goto" for e in events))
                self.assertEqual(sum(e["event"] == "business_mutation_result" for e in events), 1)
            if lookup:
                feedback = next(message for message in chat.messages[-1] if message["role"] == "tool")
                self.assertIn('"current_facts"', feedback["content"])
                self.assertIn('"contacts":[]', feedback["content"])
                events = [json.loads(line) for line in (Path(tmp)/"audit.jsonl").read_text().splitlines()]
                self.assertEqual(sum(e["event"] == "skill_planning_read" for e in events), 1)

    def test_requested_extra_field_cannot_be_silently_omitted_before_rest_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            self.addCleanup(application.close)
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="coverage-proof")
            channel = _Channel(page_url=application.origin+"/contacts", principal=principal)
            proposal = ("create_contact", {"name": "Synthetic Elm", "email": "elm@example.test"})
            chat = _Chat([("select_skills", {"skill_ids": ["minimal.contacts.contact-maintenance"]}),
                proposal, proposal, ("finish_skill_task", {})], expected=intent(effect({"name": "Synthetic Elm", "email": "elm@example.test", "phone": "5550104"})))
            executor = GoalTurnExecutor(complete_request_check="anchored", channel=channel, config=AgentTurnConfig(supported_scope="Contacts",
                ledger_path=Path(tmp)/"audit.jsonl", require_identity=True, deployment_profile="test",
                allow_business_mutations=True), application=bind_application(adapter), chat=chat,
                goal_port=ContactsGoalPort(adapter), control=ReadOnlyControl(),
                run_id_factory=lambda m: "coverage-proof", readings=1)
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "m1", "text":
                    "Create Synthetic Elm with elm@example.test and phone 5550104.",
                    "page_url": application.origin+"/contacts"})
            self.assertEqual(result.terminal_reason, "agent_refused")
            self.assertEqual(application.write_requests, 0)
            self.assertFalse(channel.approvals)

    def test_user_source_binding_and_execute_intent_settle_before_approval_or_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            self.addCleanup(application.close)
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="source-binding-proof")
            channel = _Channel(page_url=application.origin+"/contacts", principal=principal)
            # Knowledge selection has no execute flag. The complete task binds
            # its own intent and the full name before approval.
            chat = _Chat([("select_skills", {"skill_ids": ["minimal.contacts.contact-maintenance"]}),
                [("create_contact", {"name": "合成杉木研究中心", "email": "cedar@example.test"}), ("finish_skill_task", {})]],
                expected=intent(effect({"name": "合成杉木研究中心", "email": "cedar@example.test"})))
            executor = GoalTurnExecutor(complete_request_check="anchored", channel=channel, config=AgentTurnConfig(supported_scope="Contacts",
                ledger_path=Path(tmp)/"audit.jsonl", require_identity=True, deployment_profile="test",
                allow_business_mutations=True), application=bind_application(adapter), chat=chat,
                goal_port=ContactsGoalPort(adapter), control=ReadOnlyControl(),
                run_id_factory=lambda m: "source-binding-proof", readings=1)
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "m1", "text":
                    "请创建合成杉木研究中心，邮箱cedar@example.test。", "page_url": application.origin+"/contacts"})
            self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
            self.assertEqual(application.write_requests, 1)
            self.assertEqual(len(channel.approvals), 1)
            self.assertEqual(len(chat.messages), 3)
            self.assertEqual([r["name"] for r in application.contacts if r["email"] == "cedar@example.test"],
                             ["合成杉木研究中心"])
            events = [json.loads(line) for line in (Path(tmp)/"audit.jsonl").read_text().splitlines()]
            self.assertIn("skill_inputs_bound", {e["event"] for e in events})
            self.assertNotIn("skill_literals_read", {e["event"] for e in events})

    def test_legacy_handoff_intent_recheck_precedes_approval_and_single_rest_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            self.addCleanup(application.close)
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="intent-recheck")
            channel = _Channel(page_url=application.origin+"/contacts", principal=principal)
            class Chat(_Chat):
                def complete(self, *args, **kwargs):
                    if len(self.messages) == 2:
                        if application.write_requests or channel.approvals:
                            raise AssertionError("handoff executed before intent recheck")
                    return super().complete(*args, **kwargs)
            chat = Chat([("select_skills", {"skill_ids": ["minimal.contacts.contact-maintenance"]}),
                [("declare_goal", {"deliverables": [{"kind": "handoff", "entity": "contacts",
                    "action": "create"}], "write_intent": False}), ("finish_skill_task", {})],
                [("create_contact", {"name": "Synthetic Cedar", "email": "cedar@example.test"}), ("finish_skill_task", {})]],
                expected=intent(effect({"name": "Synthetic Cedar", "email": "cedar@example.test"})))
            executor = GoalTurnExecutor(complete_request_check="anchored", channel=channel, config=AgentTurnConfig(supported_scope="Contacts",
                ledger_path=Path(tmp)/"audit.jsonl", require_identity=True, deployment_profile="test",
                allow_business_mutations=True), application=bind_application(adapter), chat=chat,
                goal_port=ContactsGoalPort(adapter), control=ReadOnlyControl(),
                run_id_factory=lambda m: "intent-recheck", readings=1)
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "m1", "text":
                    "Create Synthetic Cedar with cedar@example.test after showing me the confirmation.",
                    "page_url": application.origin+"/contacts"})
            self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
            self.assertEqual(application.write_requests, 1)
            self.assertEqual(len(channel.approvals), 1)
            self.assertEqual(len(chat.messages), 5)
            events = [json.loads(line) for line in (Path(tmp)/"audit.jsonl").read_text().splitlines()]
            self.assertEqual(sum(e["event"] == "skill_task_reconciled" for e in events), 1)

    def test_shortened_quoted_name_stops_before_approval_or_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            self.addCleanup(application.close)
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="complete-literal-proof")
            channel = _Channel(page_url=application.origin+"/contacts", principal=principal)
            chat = _Chat([("select_skills", {"skill_ids": ["minimal.contacts.contact-maintenance"]}),
                ("create_contact", {"name": "Synthetic Cedar", "email": "cedar@example.test"}),
                [("create_contact", {"name": "Synthetic Cedar", "email": "cedar@example.test"}), ("finish_skill_task", {})]],
                expected=intent(effect({"name": "Synthetic Cedar Research", "email": "cedar@example.test"})))
            executor = GoalTurnExecutor(complete_request_check="anchored", channel=channel, config=AgentTurnConfig(supported_scope="Contacts",
                ledger_path=Path(tmp)/"audit.jsonl", require_identity=True, deployment_profile="test",
                allow_business_mutations=True), application=bind_application(adapter), chat=chat,
                goal_port=ContactsGoalPort(adapter), control=ReadOnlyControl(),
                run_id_factory=lambda m: "complete-literal-proof", readings=1)
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "m1", "text":
                    'Create “Synthetic Cedar Research” with cedar@example.test.',
                    "page_url": application.origin+"/contacts"})
            self.assertEqual(result.terminal_reason, "agent_refused")
            self.assertEqual(application.write_requests, 0)
            self.assertFalse(channel.approvals)

    def test_static_knowledge_never_offers_writes_in_an_enabled_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            self.addCleanup(application.close)
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="knowledge-proof")
            channel = _Channel(page_url=application.origin+"/contacts", principal=principal)
            chat = _Chat([("declare_scope", {"parts": [{"kind": "fact", "entity": "contacts", "rules": True}],
                "skill_ids": ["minimal.contacts.contact-maintenance"]}),
                [("cite_skill_knowledge", {"skill_id": "minimal.contacts.contact-maintenance", "span":
                    "Creating needs a name and email provided by the person."}), ("finish_skill_task", {})]])
            executor = GoalTurnExecutor(complete_request_check="anchored", channel=channel, config=AgentTurnConfig(supported_scope="Contacts",
                ledger_path=Path(tmp)/"audit.jsonl", require_identity=True, deployment_profile="test",
                allow_business_mutations=True), application=bind_application(adapter), chat=chat,
                goal_port=ContactsGoalPort(adapter), control=ReadOnlyControl(),
                run_id_factory=lambda m: "knowledge-proof", readings=1)
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "m1", "text": "Explain the contact creation requirements",
                    "page_url": application.origin+"/contacts"})
            self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
            self.assertNotIn("create_contact", chat.tool_names[-1])
            self.assertEqual(application.write_requests, 0)
            self.assertFalse(channel.approvals)

    def test_proposals_collect_read_after_create_before_one_write_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            self.addCleanup(application.close)
            adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
            principal = adapter.identity.bind(adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="mixed-proof")
            channel = _Channel(page_url=application.origin+"/contacts", principal=principal)
            class Chat(_Chat):
                def complete(self, *args, **kwargs):
                    if self.script:
                        self_test.assertEqual(application.write_requests, 0)
                        self_test.assertFalse(channel.approvals)
                    return super().complete(*args, **kwargs)
            self_test = self
            chat = Chat([("select_skills", {"skill_ids": ["minimal.contacts.contact-maintenance"]}),
                ("create_contact", {"name": "Synthetic Ridge", "email": "ridge@example.test"}),
                ("declare_goal", {"deliverables": [{"kind": "fact", "entity": "contacts",
                    "record": "@result:0", "fields": ["Email"]}], "write_intent": False}), ("finish_skill_task", {})],
                expected=intent(effect({"name": "Synthetic Ridge", "email": "ridge@example.test"}),
                    goals=[{"after_effect": 1, "goal": {"deliverables": [{"kind": "fact", "entity": "contacts",
                        "record": "@result:0", "fields": ["Email"]}], "write_intent": False}}]))
            executor = GoalTurnExecutor(complete_request_check="anchored", channel=channel, config=AgentTurnConfig(supported_scope="Contacts",
                ledger_path=Path(tmp)/"audit.jsonl", require_identity=True, deployment_profile="test",
                allow_business_mutations=True), application=bind_application(adapter), chat=chat,
                goal_port=ContactsGoalPort(adapter), control=ReadOnlyControl(),
                run_id_factory=lambda m: "mixed-proof", readings=1)
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "m1", "text":
                    "Create Synthetic Ridge with ridge@example.test, then show its email.",
                    "page_url": application.origin+"/contacts"})
            self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
            self.assertEqual(application.write_requests, 1)
            self.assertEqual(len(channel.approvals), 1)
            events = list(map(json.loads, (Path(tmp)/"audit.jsonl").read_text().splitlines()))
            self.assertTrue(any(event["event"] == "goal_resolved" for event in events))
            self.assertGreater(result.user_answer.count("ridge@example.test"), 1)

    def test_goal_write_terminal_states_keep_native_effects_and_stop(self):
        scenarios = (
            ("denied", AgentTurnStatus.CANCELLED, "approval_denied", 0),
            ("blocked", AgentTurnStatus.REFUSED, "business_mutation_blocked", 0),
            ("refused", AgentTurnStatus.REFUSED, "business_mutation_refused", 0),
            ("readback_lost", AgentTurnStatus.INDETERMINATE, "indeterminate_write", 1),
        )
        for scenario, status, reason, count in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                application = MinimalContactsApplication().start()
                try:
                    adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                        allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
                    observed = TurnEvents()
                    native = adapter.ports.operations.tools.get("create_contact").handler.executor
                    if scenario in {"blocked", "refused"}:
                        error = (BusinessMutationBlockedError("pending synthetic write", reason="in_flight")
                            if scenario == "blocked" else
                            BusinessMutationRefusedError("synthetic precondition changed", reason="precondition_changed"))
                        native.execute_mutation = lambda *a, **kw: (_ for _ in ()).throw(error)
                    if scenario == "readback_lost":
                        existing, post = native._existing, native._post
                        sent = []
                        def dispatch(*args):
                            result = post(*args)
                            sent.append(True)
                            return result
                        def read(name):
                            if sent:
                                raise BusinessToolUnavailableError("synthetic readback unavailable")
                            return existing(name)
                        native._post, native._existing = dispatch, read
                    principal = adapter.identity.bind(adapter.identity.page_identity(
                        f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="skill-state-proof")
                    channel = _Channel(page_url=application.origin+"/contacts", principal=principal,
                        decision="denied" if scenario == "denied" else "approved")
                    sid = "minimal.contacts.contact-maintenance"
                    chat = _Chat([("select_skills", {"skill_ids": ["minimal.contacts.contact-maintenance"]}),
                        [("create_contact", {"name": name, "email": name.lower()+"@example.test"})
                         for name in ("SyntheticTerminal", "SyntheticLater")] + [("finish_skill_task", {})]],
                        expected=intent(*[effect({"name": name, "email": name.lower()+"@example.test"})
                            for name in ("SyntheticTerminal", "SyntheticLater")]))
                    executor = GoalTurnExecutor(complete_request_check="anchored", channel=channel, config=AgentTurnConfig(supported_scope="contacts",
                        ledger_path=Path(tmp)/"audit.jsonl", require_identity=True, deployment_profile="test",
                        allow_business_mutations=True), application=bind_application(adapter), chat=chat,
                        goal_port=ContactsGoalPort(adapter), control=ReadOnlyControl(),
                        run_id_factory=lambda m: "skill-state-proof", readings=1, turn_events=observed)
                    with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                        result = executor.execute({"message_id": "m1", "text": "Create SyntheticTerminal with syntheticterminal@example.test, then create SyntheticLater with syntheticlater@example.test",
                            "page_url": application.origin+"/contacts"})
                    self.assertEqual(result.status, status, result.user_answer)
                    self.assertEqual(result.terminal_reason, reason)
                    self.assertEqual(len(channel.approvals), 1)
                    self.assertEqual(sum(r["name"] == "SyntheticTerminal" for r in application.contacts), count)
                    self.assertFalse(any(r["name"] == "SyntheticLater" for r in application.contacts))
                    receipts = observed.snapshot("m1")["receipts"]
                    if scenario == "denied":
                        self.assertFalse(receipts)
                    else:
                        receipt, = receipts
                        self.assertEqual(receipt["dispatch_count"], count)
                        self.assertEqual(receipt["effect_status"], "applied" if count else "not-applicable")
                finally:
                    application.close()

    def test_known_unsupported_constraint_refuses_before_any_approval_or_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            try:
                adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                    allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
                principal = adapter.identity.bind(adapter.identity.page_identity(
                    f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="skill-constraint-proof")
                channel = _Channel(page_url=application.origin+"/contacts", principal=principal)
                sid = "minimal.contacts.contact-maintenance"
                chat = _Chat([("select_skills", {"skill_ids": ["minimal.contacts.contact-maintenance"]}),
                    [("report_skill_limit", {"unsupported": "This application has no owner assignment."}),
                     ("create_contact", {"name": "SyntheticOwner", "email": "owner@example.test"})]],
                    expected=intent(unsupported="This application has no owner assignment."))
                executor = GoalTurnExecutor(complete_request_check="anchored", channel=channel, config=AgentTurnConfig(supported_scope="contacts",
                    ledger_path=Path(tmp)/"audit.jsonl", require_identity=True, deployment_profile="test",
                    allow_business_mutations=True), application=bind_application(adapter), chat=chat,
                    goal_port=ContactsGoalPort(adapter), control=ReadOnlyControl(),
                    run_id_factory=lambda m: "skill-constraint-proof", readings=1)
                with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                    result = executor.execute({"message_id": "m1", "text": "Create SyntheticOwner assigned to another owner",
                        "page_url": application.origin+"/contacts"})
                self.assertEqual(result.status, AgentTurnStatus.REFUSED)
                self.assertFalse(channel.approvals)
                self.assertFalse(any(r["name"] == "SyntheticOwner" for r in application.contacts))
            finally:
                application.close()

    def test_agent_skill_creation_uses_same_rest_chain(self):
        with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"}), tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            try:
                adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                    allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
                bound = bind_application(adapter)
                observed = TurnEvents()
                principal = adapter.identity.bind(adapter.identity.page_identity(
                    f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="skill-agent-proof")
                channel = _Channel(page_url=application.origin+"/contacts", principal=principal)
                sid = "minimal.contacts.contact-maintenance"
                chat = _Chat([("load_skill", {"skill_id": sid}),
                    ("create_contact", {"name": "Synthetic Ocean", "email": "ocean@example.test",
                        "intent": "execute", "supporting_skill": sid}),
                    ("finish", {"done": True, "why": "Created and verified the contact."})])
                executor = AgentTurnExecutor(channel=channel, config=AgentTurnConfig(supported_scope="Create contacts",
                    ledger_path=Path(tmp)/"audit.jsonl", require_identity=True, deployment_profile="test",
                    allow_business_mutations=True), application=bound, chat=chat,
                    control=ReadOnlyControl(), run_id_factory=lambda m: "skill-agent-rest-proof", turn_events=observed)
                with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                    result = executor.execute({"message_id": "m1", "text": "Create Synthetic Ocean with ocean@example.test",
                                               "page_url": application.origin+"/contacts"})
                self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
                self.assertEqual(len(channel.approvals), 1)
                self.assertEqual({d.label for d in channel.approvals[0].details}, {"name", "email"})
                self.assertEqual(len([r for r in application.contacts if r["name"] == "Synthetic Ocean"]), 1)
                self._assert_dispatched(observed.snapshot("m1"))
            finally:
                application.close()

    def test_goal_skill_creation_uses_real_rest_readback_and_one_visible_full_card(self):
        with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"}), tempfile.TemporaryDirectory() as tmp:
            application = MinimalContactsApplication().start()
            try:
                adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
                    allow_mutations=True, mutation_journal=MemoryMutationJournal(), standard_skills=True)
                bound = bind_application(adapter)
                observed = TurnEvents()
                page = adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}")
                principal = adapter.identity.bind(page, delegation_id="skill-proof")
                channel = _Channel(page_url=application.origin+"/contacts", principal=principal)
                sid = "minimal.contacts.contact-maintenance"
                chat = _Chat([("select_skills", {"skill_ids": ["minimal.contacts.contact-maintenance"]}),
                    [("create_contact", {"name": "Synthetic River", "email": "river@example.test"}), ("finish_skill_task", {})]],
                    expected=intent(effect({"name": "Synthetic River", "email": "river@example.test"})))
                executor = GoalTurnExecutor(complete_request_check="anchored", channel=channel, config=AgentTurnConfig(supported_scope="contacts",
                    ledger_path=Path(tmp)/"audit.jsonl", require_identity=True, deployment_profile="test",
                    allow_business_mutations=True), application=bound, chat=chat,
                    goal_port=ContactsGoalPort(adapter), control=ReadOnlyControl(),
                    run_id_factory=lambda m: "skill-rest-proof", readings=1, turn_events=observed)
                with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                    result = executor.execute({"message_id": "m1", "text": "Create Synthetic River with river@example.test",
                                               "page_url": application.origin+"/contacts"})
                details = [e.get("detail") for e in map(json.loads, (Path(tmp)/"audit.jsonl").read_text().splitlines())
                           if e.get("event") == "skill_declaration_refused"]
                self.assertEqual(result.terminal_reason, "agent_finished", (result.user_answer, details))
                self.assertEqual(len(channel.approvals), 1)
                self.assertEqual({d.label for d in channel.approvals[0].details}, {"name", "email"})
                self.assertTrue(result.business_result.content["outcomes"][0]["verified"])
                self.assertEqual(len([r for r in application.contacts if r["name"] == "Synthetic River"]), 1)
                self._assert_dispatched(observed.snapshot("m1"))
            finally:
                application.close()

    def _assert_dispatched(self, observed):
        self.assertTrue(observed["delivery_complete"])
        self.assertEqual(observed["unbound_mutations"], 0)
        receipt, = observed["receipts"]
        self.assertEqual(receipt["dispatch_state"], "dispatched")
        self.assertEqual(receipt["dispatch_count"], 1)
        self.assertEqual(receipt["approval_consumed_count"], 1)
        self.assertEqual(receipt["effect_status"], "applied")
        self.assertTrue(receipt["approval_id"])
        self.assertEqual(receipt["source"], "application-mutation-boundary")
        self.assertEqual(observed["events"][0]["kind"], "request_received")
        kinds = {e["kind"] for e in observed["events"]}
        for stage in ("model", "application", "verification"):
            self.assertIn(stage+"_begin", kinds)
            self.assertIn(stage+"_end", kinds)
