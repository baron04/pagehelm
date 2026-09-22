"""A reviewed Skill can open its own read-only routes, be cited, and go first.

Before this, a hand-written Skill could only describe a menu path for the model to click,
and once a typed read put the turn under the evidence contract, nothing a Skill said could
be cited -- the four-arm Odoo gate's Skill arm failed for exactly those two reasons. Skill
first is the third: the deployment's own knowledge is what a matched request starts from,
and source is the fallback for what no Skill covers.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.answer import answer_protocol
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.runtime import BusinessSkill, BusinessToolRuntime
from web_operation_agent.composition.ports import DeclaredRoutePort
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.execution.test_answer_product import _EvidenceExecutor
from web_operation_agent.tests.support.business import _Browser, _Chat, _Executor

ROUTES = DeclaredRoutePort("fixture", (r"/object/company(?:/[a-z0-9-]+)?", r"/terms"))


def _skill(routes=None, *, read_only=True) -> BusinessSkill:
    artifact = {
        "schema_version": 1, "skill_id": "fixture.terms-help", "title": "Payment terms help",
        "intent": "Explain and open the payment terms list.",
        "triggers": ["payment terms", "installments"],
        "instructions": ["Payment terms split an invoice into installments with due dates.",
                         "Quotation status means only orders still in draft."],
        "tool_names": ["query_companies"],
        "provenance": ["src:fixture@v1:skills/terms.json"], "read_only": read_only,
    }
    if routes is not None:
        artifact["routes"] = routes
    return BusinessSkill.from_bytes(json.dumps(artifact).encode(), artifact_uri="memory://terms")


class SkillRoutes(unittest.TestCase):
    def test_a_route_outside_the_application_contract_refuses_the_catalog(self):
        with self.assertRaisesRegex(ValueError, "outside the application's route contract"):
            BusinessToolRuntime((_skill(["/admin/delete"]),), _Executor(),
                                route_validator=ROUTES)
        with self.assertRaisesRegex(ValueError, "no route validator"):
            BusinessToolRuntime((_skill(["/terms"]),), _Executor())
        for bad in (["terms"], ["//evil/terms"], ["/terms#x"], ["/a b"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _skill(bad)

    def test_a_loaded_skill_route_opens_and_nothing_else_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            chat = _Chat([("load_skill", {"skill_id": "fixture.terms-help"}),
                          ("goto", {"url": "/object/company/other"}),
                          ("goto", {"url": "/terms"}),
                          ("finish", {"done": True, "why": "opened"})])
            browser = _Browser()
            session = ServerAgentSession(
                task=TaskSpec("T", "open the payment terms list"), chat=chat,
                browser=browser, control=ReadOnlyControl(),
                ledger=SessionLedger(Path(tmp) / "a.jsonl", "routes"),
                business_tools=BusinessToolRuntime((_skill(["/terms"]),), _Executor(),
                                                   route_validator=ROUTES),
                application_routes=ROUTES, step_budget=6)
            session.entry_observation = browser.observe()
            session.run()
            self.assertEqual(browser.actions, ["goto"])
            self.assertEqual(browser.url, "http://app.test/terms")
            self.assertEqual(len(session.ledger.of_kind("page_route_rejected")), 1)
            loaded = session.ledger.of_kind("business_skill_loaded")[0]
            self.assertEqual(loaded["routes"], ["/terms"])


class SkillFirst(unittest.TestCase):
    """The one read-only Skill a request's wording matched is loaded before the first call."""

    def _session(self, prompt, replies, *, skills=None, skill_first=True, source=None,
                 approval=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chat = _Chat(list(replies))
        browser = _Browser()
        session = ServerAgentSession(
            task=TaskSpec("T", prompt), chat=chat, browser=browser,
            control=ReadOnlyControl(),
            ledger=SessionLedger(Path(tmp.name) / "a.jsonl", "first"),
            business_tools=BusinessToolRuntime(
                tuple(skills if skills is not None else (_skill(["/terms"]),)),
                _EvidenceExecutor(), route_validator=ROUTES),
            application_routes=ROUTES, skill_first=skill_first, step_budget=6,
            business_mutation_approval=approval,
            **({"source": source} if source is not None else {}))
        session.entry_observation = browser.observe()
        return session, chat

    def test_a_unique_read_only_match_starts_the_turn_loaded(self):
        session, chat = self._session(
            "what do the payment terms mean?",
            [("finish", {"done": False, "why": "stop"})])
        session.run()

        opening = chat.messages[0][1]["content"]
        self.assertIn("BUSINESS SKILLS (loaded cards include their playbook", opening)
        self.assertIn("Payment terms split an invoice", opening)
        self.assertIn("statement_refs", opening)
        # Its tools are offered from the first call, and its routes are already open.
        self.assertIn("query_companies", chat.tool_names[0])
        self.assertEqual(session._business_routes, {"/terms"})
        self.assertEqual([ref for ref in session.evidence.ids() if ref.startswith("skill#")],
                         ["skill#1", "skill#2"])
        auto = session.ledger.of_kind("business_skill_auto_loaded")
        self.assertEqual([event["reason"] for event in auto], ["skill_first_unique_match"])
        self.assertEqual(auto[0]["routes"], ["/terms"])

    def test_loading_it_again_returns_the_same_refs_rather_than_new_ones(self):
        session, chat = self._session(
            "what do the payment terms mean?",
            [("load_skill", {"skill_id": "fixture.terms-help"}),
             ("finish", {"done": False, "why": "stop"})])
        session.run()

        self.assertIn("ALREADY LOADED", chat.messages[1][-1]["content"])
        self.assertIn("skill#1", chat.messages[1][-1]["content"])
        self.assertEqual([ref for ref in session.evidence.ids() if ref.startswith("skill#")],
                         ["skill#1", "skill#2"])
        self.assertEqual(session.ledger.of_kind("business_skill_loaded"), [])

    def test_it_does_not_fire_where_the_choice_is_not_the_server_s(self):
        from web_operation_agent.tests.support.golden_session import approve

        second = BusinessSkill.from_bytes(json.dumps({
            "schema_version": 1, "skill_id": "fixture.terms-report",
            "title": "Payment terms report", "intent": "Report on payment terms.",
            "triggers": ["payment terms"], "instructions": ["Read the terms report."],
            "tool_names": ["query_companies"],
            "provenance": ["src:fixture@v1:skills/report.json"], "read_only": True,
        }).encode(), artifact_uri="memory://report")
        cases = {
            "no trigger matched the wording": (
                "open the vendor desk page", None, True, None),
            "two Skills matched the same wording": (
                "what do the payment terms mean?", (_skill(["/terms"]), second), True, None),
            "the Skill that matched can write": (
                "what do the payment terms mean?", (_skill(read_only=False),), True, approve),
            "skill first is off": ("what do the payment terms mean?", None, False, None),
        }
        for reason, (prompt, skills, first, approval) in cases.items():
            with self.subTest(reason=reason):
                session, chat = self._session(
                    prompt, [("finish", {"done": False, "why": "stop"})],
                    skills=skills, skill_first=first, approval=approval)
                session.run()
                self.assertEqual(session.ledger.of_kind("business_skill_auto_loaded"), [])
                self.assertEqual(session._business_routes, set())
                self.assertNotIn("query_companies", chat.tool_names[0])
                self.assertIn("load_skill", chat.tool_names[0])

    def test_with_source_available_the_prompt_says_the_skill_goes_first(self):
        from web_operation_agent.composition.sources import SourceCapability
        from web_operation_agent.tests.support.source_fixture import (build_fixture_tree,
                                                                     fixture_source_map)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        build_fixture_tree(root)
        session, chat = self._session(
            "what do the payment terms mean?",
            [("finish", {"done": False, "why": "stop"})],
            source=SourceCapability(source_map=fixture_source_map(root),
                                    runtime_source=True))
        session.run()
        system = chat.messages[0][0]["content"]
        self.assertIn("use its\nstatements, routes and tools first", system)
        self.assertIn("lookup_source", system)


class SkillStatements(unittest.TestCase):
    def _answer(self, support):
        return {"outcome": "answered", "answer": {
            "version": 1, "coverage": "complete", "conclusions": [{
                "kind": "skill_statement", "evidence_ref": "skill#2",
                "statement": "Only draft orders count as quotations here.",
                "support": support}]}}

    def run_with(self, replies):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chat = _Chat([("load_skill", {"skill_id": "fixture.terms-help"}),
                      ("query_companies", {}), *replies])
        session = ServerAgentSession(
            task=TaskSpec("T", "how many quotations are there, meaning drafts?"), chat=chat,
            browser=_Browser(), control=ReadOnlyControl(),
            ledger=SessionLedger(Path(tmp.name) / "a.jsonl", "statements"),
            business_tools=BusinessToolRuntime((_skill(),), _EvidenceExecutor()),
            step_budget=6)
        session.entry_observation = session.browser.observe()
        return session, chat, session.run()

    def test_a_skill_instruction_is_citable_under_the_evidence_contract(self):
        session, chat, result = self.run_with(
            [("finish", self._answer("Quotation status means only orders still in draft"))])
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(result.verified_answer["conclusions"][0]["kind"], "skill_statement")
        self.assertIn("Only draft orders count as quotations", result.agent_said)
        # The typed read put the turn under the evidence contract; the Skill's refs were
        # handed back with the playbook and the finish schema names the new kind.
        self.assertIn("statement_refs", chat.messages[1][-1]["content"])
        self.assertTrue(session._answers.answer_mode)
        finish = next(t for t in session.tool_schemas() if t["function"]["name"] == "finish")
        self.assertIn("skill_statement", json.dumps(finish))

    def test_a_support_span_the_skill_never_said_is_refused(self):
        _session, chat, result = self.run_with(
            [("finish", self._answer("Quotation status includes sent quotations")),
             ("finish", {"outcome": "unable"})])
        self.assertIn("support is not present", chat.messages[3][-1]["content"])
        self.assertIsNone(result.verified_answer)

    def test_skill_evidence_is_not_application_data(self):
        session, _chat, _result = self.run_with([("finish", {"outcome": "unable"})])
        refs = [ref for ref in session.evidence.ids() if ref.startswith("skill#")]
        self.assertEqual(refs, ["skill#1", "skill#2"])
        schema = json.dumps(answer_protocol.finish_answer_tool(source=False, skill=False))
        self.assertNotIn("skill_statement", schema)


if __name__ == "__main__":
    unittest.main()
