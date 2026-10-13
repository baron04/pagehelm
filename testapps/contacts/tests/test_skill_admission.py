"""Admission through ordinary exits, with real actor-bound REST effects."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from testapps.contacts.app import MinimalContactsApplication, DEMO_API_TOKEN, DEMO_SESSION_COOKIE, DEMO_ACTORS
from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.goal import ContactsGoalPort
from testapps.contacts.tests.test_skill_execution import _Chat
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.composition import bind_application
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.goal.test_repository_answers import Lookup, source_reply


SID = "minimal.contacts.contact-maintenance"
SELECT = ("select_skills", {"skill_ids": [SID]})
CREATE = [("create_contact", {"name": "Synthetic Larch", "email": "larch@example.test"}),
          ("finish_skill_task", {})]
REQUEST = "Create Synthetic Larch with larch@example.test."
COUNT = ("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "contacts",
                                          "metric": {"op": "count"}}]})


class RecordingChat(_Chat):
    def __init__(self, script):
        super().__init__(script)
        self.inputs = []

    def complete(self, messages, tools, run_id, **kwargs):
        self.inputs.append(deepcopy({"messages": messages, "tools": tools, "options": kwargs}))
        return super().complete(messages, tools, run_id, **kwargs)


class SkillAdmissionTests(unittest.TestCase):
    def run_turn(self, script, *, request=REQUEST, writes=True, decision="approved", source=False):
        application = MinimalContactsApplication().start()
        self.addCleanup(application.close)
        adapter = MinimalContactsAdapter(application.origin, api_token=DEMO_API_TOKEN,
            allow_mutations=writes, mutation_journal=MemoryMutationJournal(), standard_skills=True)
        principal = adapter.identity.bind(adapter.identity.page_identity(
            f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"), delegation_id="admission-proof")
        channel = _Channel(page_url=application.origin + "/contacts", principal=principal, decision=decision)
        chat = RecordingChat(script)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            executor = GoalTurnExecutor(channel=channel,
                config=AgentTurnConfig(supported_scope="Contacts", ledger_path=path,
                    require_identity=True, deployment_profile="test", allow_business_mutations=writes,
                    source=SourceCapability(runtime_source=True, lookup=Lookup()) if source else SourceCapability.none()),
                application=bind_application(adapter), chat=chat, goal_port=ContactsGoalPort(adapter),
                control=ReadOnlyControl(), run_id_factory=lambda m: "admission-proof", readings=1,
                repository_answers=source)
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "m1", "text": request,
                    "page_url": application.origin + "/contacts"})
            events = [json.loads(line) for line in path.read_text().splitlines()]
        return result, application, channel, chat, events

    def assert_owned(self, result, application, channel, events):
        self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
        self.assertEqual(application.write_requests, 1)
        self.assertEqual(len(channel.approvals), 1)
        self.assertEqual(sum(e["event"] == "business_mutation_result" for e in events), 1)
        self.assertEqual(sum(e["event"] == "goal_skill_consulted" for e in events), 1)
        self.assertNotIn("goal_skill_yielded", [e["event"] for e in events])

    def test_empty_scope_can_enter_existing_verified_write_chain(self):
        result, app, channel, chat, events = self.run_turn([
            ("declare_scope", {"parts": [], "unsupported": "No declared business goal"}), SELECT, CREATE])
        self.assert_owned(result, app, channel, events)
        self.assertEqual(len(chat.inputs), 3)
        self.assertIn(REQUEST, chat.inputs[-1]["messages"][-1]["content"])

    def test_scope_change_preloads_matching_knowledge_without_selection_call(self):
        result, app, channel, chat, events = self.run_turn([
            ("declare_scope", {"parts": [{"kind": "handoff", "entity": "contacts"}], "write_intent": True}), CREATE])
        self.assert_owned(result, app, channel, events)
        self.assertEqual(len(chat.inputs), 2)

    def test_source_only_scope_has_admission_before_source_answer_and_keeps_source_delivery(self):
        result, app, channel, chat, events = self.run_turn([
            ("declare_scope", {"kind_0": "source", "about_0": "Interface engineering owner",
                               "terms_0": ["engineering owner"]}), SELECT, CREATE,
            ("declare_source_queries", {"queries_0": []}), source_reply()],
            source=True, request=REQUEST + " Explain the interface engineering owner.")
        self.assert_owned(result, app, channel, events)
        self.assertIn("Aurora team", result.user_answer)
        self.assertIn("goal_source_answer_verified", [e["event"] for e in events])

    def test_second_declaration_can_request_execution_after_nonchange_scope(self):
        result, app, channel, chat, events = self.run_turn([
            ("declare_scope", {"parts": [{"kind": "aggregate", "entity": "contacts"}]}),
            ("declare_goal", {**COUNT[1], "write_intent": True}), CREATE])
        self.assert_owned(result, app, channel, events)
        self.assertIn("goal_declared", [e["event"] for e in events])

    def test_nonexecution_yields_once_and_ordinary_model_inputs_match(self):
        scope = ("declare_scope", {"parts": [{"kind": "handoff", "entity": "contacts"}]})
        request = "Do not create a contact; count contacts instead."
        ordinary = self.run_turn([scope, COUNT], request=request, writes=False)
        candidate = self.run_turn([scope, ("continue_goal", {}), COUNT], request=request)
        result, app, channel, chat, events = candidate
        self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
        self.assertEqual(app.write_requests, 0)
        self.assertFalse(channel.approvals)
        # The ordinary post-scope wire is identical, including knowledge, values,
        # tool schema and model options. Consultation never enters its history.
        actual, expected = deepcopy(chat.inputs[-1]), deepcopy(ordinary[3].inputs[-1])
        # Only remaining wall time changes because the extra call used budget.
        actual["options"].pop("timeout_s", None)
        expected["options"].pop("timeout_s", None)
        self.assertEqual(actual, expected)
        self.assertEqual(sum(e["event"] == "goal_skill_consulted" for e in events), 1)
        self.assertEqual(sum(e["event"] == "goal_skill_yielded" for e in events), 1)

    def test_uncovered_requirement_yields_with_reason_and_zero_approval(self):
        result, app, channel, chat, events = self.run_turn([
            ("declare_scope", {"parts": [{"kind": "handoff", "entity": "contacts"}]}),
            ("report_skill_limit", {"unsupported": "The installed operations do not send mail."}), COUNT],
            request="Send mail to contacts, then count contacts.")
        self.assertEqual(app.write_requests, 0)
        self.assertFalse(channel.approvals)
        self.assertIn("do not send mail", result.user_answer)
        self.assertIn("goal_skill_yielded", [e["event"] for e in events])

    def test_rejected_approval_cannot_yield_back_or_dispatch(self):
        result, app, channel, chat, events = self.run_turn([
            ("declare_scope", {"parts": [{"kind": "handoff", "entity": "contacts"}]}), CREATE], decision="denied")
        self.assertEqual(app.write_requests, 0)
        self.assertEqual(len(channel.approvals), 1)
        self.assertNotIn("goal_skill_yielded", [e["event"] for e in events])

    def test_ordinary_query_and_assistant_question_do_not_consult(self):
        for script, request in (([("declare_scope", {"parts": [{"kind": "aggregate", "entity": "contacts"}]}), COUNT],
                                 "Count contacts."),
                                ([("declare_scope", {"parts": [], "about_assistant": True})], "What can you do?")):
            with self.subTest(request=request):
                result, app, channel, chat, events = self.run_turn(script, request=request)
                self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
                self.assertEqual(app.write_requests, 0)
                self.assertNotIn("goal_skill_consulted", [e["event"] for e in events])

    def test_invalid_proposal_stops_without_yield_or_a_third_correction(self):
        result, app, channel, chat, events = self.run_turn([
            ("declare_scope", {"parts": [{"kind": "handoff", "entity": "contacts"}]}),
            ("create_contact", {}), ("create_contact", {})])
        self.assertEqual(result.terminal_reason, "agent_refused", result.user_answer)
        self.assertEqual(app.write_requests, 0)
        self.assertFalse(channel.approvals)
        self.assertEqual(len(chat.inputs), 3)
        self.assertNotIn("goal_skill_yielded", [e["event"] for e in events])
        errors = [e for e in events if e["event"] == "skill_declaration_invalid"]
        self.assertEqual([e["correction_allowed"] for e in errors], [True, False])
        refused = next(e for e in events if e["event"] == "skill_declaration_refused")
        self.assertEqual(refused["error_code"], "business_request")
        self.assertFalse(refused["side_effect_possible"])

    def test_invalid_scope_yield_recovers_the_same_ordinary_declaration(self):
        invalid = ("declare_scope", {"parts": [{"kind": "unknown"}]})
        request = "Count contacts."
        ordinary = self.run_turn([invalid, invalid, COUNT], request=request, writes=False)
        candidate = self.run_turn([invalid, invalid, ("continue_goal", {}), COUNT], request=request)
        self.assertEqual(candidate[0].terminal_reason, "agent_finished")
        self.assertEqual(candidate[0].user_answer, ordinary[0].user_answer)
        self.assertEqual(candidate[1].write_requests, 0)
        self.assertFalse(candidate[2].approvals)
        actual, expected = deepcopy(candidate[3].inputs[-1]), deepcopy(ordinary[3].inputs[-1])
        actual["options"].pop("timeout_s", None)
        expected["options"].pop("timeout_s", None)
        self.assertEqual(actual, expected)
        self.assertEqual(sum(e["event"] == "goal_skill_consulted" for e in candidate[4]), 1)

    def test_invalid_source_scope_yield_keeps_dependency_refusal(self):
        invalid = ("declare_scope", {"kind_0": "unknown", "about_0": "Interface owner"})
        request = "Count contacts and explain the interface owner."
        ordinary = self.run_turn([invalid, invalid], request=request, writes=False, source=True)
        candidate = self.run_turn([invalid, invalid, ("continue_goal", {})], request=request, source=True)
        self.assertEqual(candidate[0].terminal_reason, "agent_refused")
        self.assertEqual(candidate[0].user_answer, ordinary[0].user_answer)
        self.assertEqual(candidate[1].write_requests, 0)
        self.assertFalse(candidate[2].approvals)
        self.assertEqual(len(candidate[3].inputs), 3)

    def test_invalid_scope_can_still_be_owned_by_one_verified_proposal(self):
        invalid = ("declare_scope", {"parts": [{"kind": "unknown"}]})
        result, app, channel, chat, events = self.run_turn([invalid, invalid, SELECT, CREATE])
        self.assert_owned(result, app, channel, events)
        self.assertEqual(len(chat.inputs), 4)
