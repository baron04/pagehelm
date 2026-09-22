"""A reviewed Skill can open its own read-only routes and be cited, like source can.

Before this, a hand-written Skill could only describe a menu path for the model to click,
and once a typed read put the turn under the evidence contract, nothing a Skill said could
be cited -- the four-arm Odoo gate's Skill arm failed for exactly those two reasons.
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
