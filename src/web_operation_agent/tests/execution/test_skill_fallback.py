"""Unmatched Skills are not a shortlist, and a read that repeats itself adds no fact.

Phase 8 §17.5: every page-only request fell through to the catalog fallback and was handed
five unrelated Skills as "candidates"; the page arm of the workflow gate then spent four to
nine of its ten steps loading them and rereading the same list between clicks. The fallback
stays -- it is how a data question whose wording misses every trigger still reaches a typed
read -- but it is presented as what it is, and a read whose content the turn already holds
is answered with a notice rather than the whole result again.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.runtime import (BusinessSkill, BusinessToolResult,
                                                  BusinessToolRuntime)
from web_operation_agent.execution.business_reads import MAX_REPEATED_READS
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.business import _Browser, _Chat

PAGED = {"type": "function", "function": {
    "name": "list_companies", "description": "Read one page of companies.",
    "parameters": {"type": "object", "properties": {"page": {"type": "integer"}},
                   "additionalProperties": False}}}


class _PagedExecutor:
    """Two pages of companies; the same page always returns the same content."""

    def __init__(self):
        self.calls = 0

    @staticmethod
    def tool_schemas():
        return (PAGED,)

    @staticmethod
    def tool_effects():
        return {"list_companies": "read"}

    def execute(self, name, args, *, context=None):
        self.calls += 1
        page = int(args.get("page", 1))
        names = {1: ["Acme", "Globex"], 2: ["Initech"]}.get(page, [])
        return BusinessToolResult(
            content={"companies": [{"name": item} for item in names], "page": page,
                     "matched_count": 3, "scope": "current_delegated_workspace"},
            audit={"application": "fixture", "operation": name,
                   "returned_count": len(names), "source_complete": True})


def _skill(path: Path) -> BusinessSkill:
    path.write_text(json.dumps({
        "schema_version": 1, "skill_id": "fixture.companies-list",
        "title": "List companies", "intent": "Read current companies page by page.",
        "triggers": ["companies", "公司"],
        "instructions": ["Call list_companies and trust its current result."],
        "tool_names": ["list_companies"],
        "provenance": ["src:fixture@v1:companies.json#companies"], "read_only": True,
    }) + "\n")
    return BusinessSkill.load(path)


class _Case(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)
        self.executor = _PagedExecutor()
        self.runtime = BusinessToolRuntime((_skill(self.tmp / "skill.json"),), self.executor)

    def tearDown(self):
        self.directory.cleanup()

    def _session(self, prompt: str, script) -> tuple[ServerAgentSession, _Chat]:
        chat = _Chat(script)
        session = ServerAgentSession(
            task=TaskSpec(task_ref="T", prompt=prompt,
                          supported_scope="Read the current application only."),
            chat=chat, browser=_Browser(), control=ReadOnlyControl(),
            ledger=SessionLedger(self.tmp / "audit.jsonl", "fallback"),
            business_tools=self.runtime, step_budget=10)
        session.entry_observation = session.browser.observe()
        return session, chat

    def _events(self, session, name):
        return [event for event in session.ledger.events if event.get("event") == name]


class UnmatchedSkillsAreAnIndex(_Case):
    def test_a_page_request_sees_an_unmatched_index_not_a_candidate_list(self):
        session, chat = self._session(
            "Open the vendor desk page.", [("finish", {"done": False, "why": "stop"})])
        session.run()

        opening = chat.messages[0][1]["content"]
        system = chat.messages[0][0]["content"]
        self.assertTrue(session._skills_unmatched)
        self.assertIn("BUSINESS SKILLS NOT MATCHED TO THIS REQUEST", opening)
        self.assertNotIn("BUSINESS SKILL CANDIDATES", opening)
        # The index names the Skill; it does not advertise the Skill's intent or tools.
        self.assertIn('"skill_id":"fixture.companies-list"', opening)
        self.assertNotIn("Read current companies page by page", opening)
        self.assertNotIn('"tools"', opening)
        self.assertIn("No Business Skill matched the wording of this request", system)
        # Still loadable: the capability surface is unchanged.
        self.assertIn("load_skill", chat.tool_names[0])
        candidates = self._events(session, "business_skill_candidates")[0]
        self.assertTrue(candidates["fallback_used"])
        self.assertTrue(candidates["presented_as_unmatched"])

    def test_a_matched_request_keeps_the_candidate_presentation(self):
        session, chat = self._session(
            "Which companies are there?", [("finish", {"done": False, "why": "stop"})])
        session.run()

        opening = chat.messages[0][1]["content"]
        self.assertFalse(session._skills_unmatched)
        self.assertIn("BUSINESS SKILL CANDIDATES", opening)
        self.assertNotIn("NOT MATCHED", opening)
        self.assertNotIn("No Business Skill matched", chat.messages[0][0]["content"])

    def test_an_unmatched_skill_once_loaded_shows_its_playbook(self):
        session, chat = self._session("Open the vendor desk page.", [
            ("load_skill", {"skill_id": "fixture.companies-list"}),
            ("finish", {"done": False, "why": "stop"}),
        ])
        session.run()

        self.assertIn("list_companies", chat.tool_names[1])
        self.assertIn("Call list_companies", chat.messages[1][-1]["content"])


class RepeatedReadsAddNoFact(_Case):
    def test_a_repeated_result_gets_a_notice_and_the_third_ends_the_turn(self):
        session, chat = self._session("Which companies are there?", [
            ("load_skill", {"skill_id": "fixture.companies-list"}),
            ("list_companies", {}),
            ("list_companies", {"page": 1}),    # same content under other arguments
            ("observe", {}),                     # a page change does not reset it
            ("list_companies", {}),
            ("list_companies", {}),
            ("finish", {"done": True, "why": "must not be reached"}),
        ])
        result = session.run()

        self.assertEqual(result.terminal_reason, "no_progress")
        self.assertNotIn("must not be reached", result.agent_said)
        self.assertEqual(self.executor.calls, 1 + MAX_REPEATED_READS)
        notice = chat.messages[3][-1]["content"]
        self.assertTrue(notice.startswith("SAME RESULT: this read returned exactly what "
                                          "step 2 returned"))
        self.assertNotIn("Acme", notice)
        self.assertIn("Acme", chat.messages[2][-1]["content"])
        repeated = self._events(session, "business_read_repeated")
        self.assertEqual([event["repeated"] for event in repeated],
                         list(range(1, MAX_REPEATED_READS + 1)))
        self.assertTrue(all(event["first_step"] == 2 for event in repeated))

    def test_a_new_page_of_results_is_a_new_fact(self):
        session, chat = self._session("Which companies are there?", [
            ("load_skill", {"skill_id": "fixture.companies-list"}),
            ("list_companies", {"page": 1}),
            ("list_companies", {"page": 2}),
            ("list_companies", {"page": 3}),
            ("finish", {"done": False, "why": "stop"}),
        ])
        session.run()

        self.assertEqual(self._events(session, "business_read_repeated"), [])
        self.assertIn("Initech", chat.messages[3][-1]["content"])

    def test_a_read_after_a_completed_write_is_a_check_not_a_repeat(self):
        from web_operation_agent.tests.support.golden_session import (
            MutationExecutor, ScriptedBrowser, ScriptedChat, approve,
            write_skill_artifact)

        skill = write_skill_artifact(self.tmp / "write-skill.json")
        executor = MutationExecutor()
        chat = ScriptedChat([
            ("load_skill", {"skill_id": skill.skill_id}),
            ("query_trays", {}),                     # preflight
            ("create_tray", {"name": "Tray B"}),     # approved, executed once
            ("query_trays", {}),                     # same content: a check, not a repeat
            ("query_trays", {}),                     # now it is a repeat
            ("finish", {"done": False, "why": "stop"}),
        ])
        browser = ScriptedBrowser()
        session = ServerAgentSession(
            task=TaskSpec(task_ref="T", prompt="Create tray Tray B.",
                          supported_scope="Create exactly one seed tray."),
            chat=chat, browser=browser, control=ReadOnlyControl(),
            ledger=SessionLedger(self.tmp / "write.jsonl", "write"),
            business_tools=BusinessToolRuntime((skill,), executor),
            business_mutation_approval=approve, step_budget=10)
        session.entry_observation = browser.observe()

        session.run()

        self.assertEqual(executor.executions, 1)
        repeated = self._events(session, "business_read_repeated")
        self.assertEqual([(event["step"], event["first_step"]) for event in repeated],
                         [(5, 4)])

if __name__ == "__main__":
    unittest.main()
