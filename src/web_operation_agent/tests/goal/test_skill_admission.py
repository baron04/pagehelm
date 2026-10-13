"""Reversible admission preserves ordinary state and has one bounded selection."""
import unittest
from contextlib import nullcontext
from types import SimpleNamespace as NS
from unittest.mock import patch

from testapps.contacts.adapter import _ContactsToolExecutor
from testapps.contacts.packages import contact_operations
from web_operation_agent.goal.skill_admission import (
    SkillProposalYield, check_yield, consult_execution,
)
from web_operation_agent.skills import SkillArtifact, SkillRegistry
from web_operation_agent.tests.support.business import _Chat


class SkillAdmissionPolicyTests(unittest.TestCase):
    def fixture(self, *, count=1, matching=True):
        operations = contact_operations(_ContactsToolExecutor("http://127.0.0.1:1",
            api_token="synthetic-token", allow_mutations=True), True)
        skills = tuple(SkillArtifact.from_files("fixture", {
            "SKILL.md": f"---\nname: contact-guide-{n}\ndescription: Maintain contacts.\n---\nUse current facts.\n".encode(),
            "agents/woa.toml": b'schema_version = 1\nscope_entities = ["contacts"]\nwrite_operations = ["create_contact"]\n',
        }) for n in range(count))
        operations = NS(skills=SkillRegistry(skills), tools=operations.tools)
        events = []
        state = NS(skill_consulted=False, skill_limit="", mutation_outcomes=[], step=1,
            scope_parts=[{"entity": "contacts"}] if matching else [],
            references=("ordinary-reference",), candidates=("ordinary-candidate",), index="ordinary-index",
            write_intent=False, unsupported="ordinary-limit", run_id="admission",
            message=NS(text="Create Synthetic Aspen."), cancelled=lambda: False,
            receipt=NS(stage=lambda name: nullcontext()), budget=NS(remaining=lambda: 20),
            ledger=NS(append=lambda name, **kwargs: events.append((name, kwargs))),
            chat=_Chat([("select_skills", {"skill_ids": [skills[0].skill_id]})]))
        executor = NS(application=NS(ports=NS(operations=operations)),
                      config=NS(allow_business_mutations=True))
        return executor, state, skills, events

    def test_more_than_three_matches_use_all_candidate_identities_without_truncation(self):
        executor, state, skills, _ = self.fixture(count=5)
        class Chat(_Chat):
            def complete(inner, messages, tools, run_id, **kwargs):
                enum = tools[0]["function"]["parameters"]["properties"]["skill_ids"]["items"]["enum"]
                self.assertEqual(enum, [s.skill_id for s in skills])
                self.assertEqual(kwargs["max_tokens"], 200)
                return super().complete(messages, tools, run_id, **kwargs)
        state.chat = Chat(state.chat.script)
        with patch("web_operation_agent.goal.skill_execution.execute_skill_plan", return_value="owned") as run:
            self.assertEqual(consult_execution(executor, state), "owned")
        self.assertEqual(run.call_args.args[2], (skills[0].skill_id,))
        self.assertEqual(len(state.chat.messages), 1)

    def test_yield_restores_planning_state_and_never_reconsults(self):
        executor, state, _, events = self.fixture()
        original = (state.references, state.candidates, state.index, state.write_intent, state.unsupported)
        def yield_plan(*args, **kwargs):
            state.references, state.candidates, state.index = ("proposal-reference",), ("proposal-candidate",), "proposal-index"
            state.write_intent, state.unsupported = True, "proposal-limit"
            raise SkillProposalYield("Not installed")
        with patch("web_operation_agent.goal.skill_execution.execute_skill_plan", side_effect=yield_plan) as run:
            self.assertIsNone(consult_execution(executor, state))
            self.assertIsNone(consult_execution(executor, state))
        self.assertEqual(run.call_count, 1)
        self.assertEqual((state.references, state.candidates, state.index, state.write_intent, state.unsupported), original)
        self.assertEqual(state.skill_limit, "Not installed")
        self.assertEqual([name for name, _ in events], ["goal_skill_consulted", "goal_skill_yielded"])

    def test_disabled_writes_and_prior_mutation_never_offer_selection(self):
        for disabled in (True, False):
            executor, state, _, _ = self.fixture()
            executor.config.allow_business_mutations = not disabled
            state.mutation_outcomes = [] if disabled else [{"verified": True}]
            self.assertIsNone(consult_execution(executor, state))
            self.assertFalse(state.skill_consulted)
            self.assertFalse(state.chat.messages)

    def test_yield_cannot_share_a_response_with_an_operation_or_take_arguments(self):
        def call(name, args):
            return {"function": {"name": name, "arguments": args}}
        for calls in ([call("continue_goal", {}), call("create_contact", {})],
                      [call("continue_goal", {"reason": "not an accepted argument"})]):
            with self.assertRaises(ValueError):
                check_yield({"tool_calls": calls})
        with self.assertRaises(SkillProposalYield):
            check_yield({"tool_calls": [call("continue_goal", {})]})
