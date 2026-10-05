"""Raw repository evidence reaches the existing agent loop without a compiled map."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.planning.repository_source import RepositorySourceLookup
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control, _obs


class RepositorySourceTurns(unittest.TestCase):
    def _source(self, root: Path, path: str, text: str):
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        lookup = RepositorySourceLookup(SourceTree({
            "repository_id": "fixture/repository", "release": "fixture-1",
            "allow_globs": ["src/**"], "deny_globs": ["**/.env*", "**/tests/**"],
        }, root))
        return SourceCapability(runtime_source=True, lookup=lookup)

    def _answer(self, statement: str, support: str):
        return {"outcome": "answered", "answer": {
            "version": 1, "coverage": "complete", "conclusions": [{
                "kind": "source_excerpt", "evidence_ref": "source#1",
                "statement": statement, "support": support,
            }]}}

    def test_cited_answers_in_two_different_source_shapes_use_fresh_observations(self):
        cases = (
            ("src/policy.py", "batch_limit = 125\n", "batch limit",
             "The declared batch limit is 125.", "batch_limit = 125"),
            ("src/CustomerDetails.vue", '<el-tab-pane label="回款" />\n', "回款",
             "客户详情提供回款页签。", '<el-tab-pane label="回款" />'),
        )
        for path, text, query, statement, support in cases:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = self._source(root, path, text)
                chat = _Chat([("lookup_source", {"query": query}),
                              ("finish", self._answer(statement, support))])
                browser = _Browser([_obs(1), _obs(2)])
                session = ServerAgentSession(
                    task=TaskSpec("source-question", statement), chat=chat, browser=browser,
                    control=_Control([[], [], []]), source=source,
                    ledger=SessionLedger(root / "audit.jsonl", "raw-source"))
                session.entry_observation = _obs(1)
                result = session.run()
                self.assertEqual("agent_finished", result.terminal_reason)
                conclusion = result.verified_answer["conclusions"][0]
                self.assertEqual(statement, conclusion["statement"])
                self.assertEqual("source_excerpt", conclusion["kind"])
                evidence = session.evidence.get("source#1")
                self.assertEqual(path, evidence.path)
                self.assertEqual("fixture-1", evidence.release)
                self.assertEqual(["observe"], [action for action, _ in browser.done])
                event = session.ledger.of_kind("runtime_source_retrieved")[0]
                self.assertEqual(source.content_digest, event["source_digest"])
                self.assertEqual(1, event["facts_shown"])
                # No compiler, route grammar or application adapter is needed to answer.
                self.assertIsNone(session.source_map)
                self.assertIsNone(session.application_routes)

    def test_source_bytes_do_not_license_a_navigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root, "src/routes.js", "const route = '/hidden/admin';\n")
            chat = _Chat([
                ("lookup_source", {"query": "hidden admin"}),
                ("open_source_route", {"url": "/hidden/admin"}),
                ("finish", self._answer("The source declares a route.", "const route")),
            ])
            browser = _Browser([_obs(1), _obs(2)])
            session = ServerAgentSession(
                task=TaskSpec("route-question", "What route does the source declare?"),
                chat=chat, browser=browser, control=_Control([[], [], [], []]), source=source,
                ledger=SessionLedger(root / "audit.jsonl", "raw-route"))
            session.entry_observation = _obs(1)
            session.run()
            self.assertEqual(["observe"], [action for action, _ in browser.done])

    def test_original_read_refreshes_already_bound_page_before_next_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root, "src/policy.py", "batch_limit = 125\n")
            chat = _Chat([
                ("read_source", {"path": "src/policy.py", "start_line": 1, "end_line": 1}),
                ("finish", self._answer("The declared batch limit is 125.", "batch_limit = 125")),
            ])
            browser = _Browser([_obs(1), _obs(2)])
            session = ServerAgentSession(
                task=TaskSpec("raw-read", "What batch limit does the code define?"),
                chat=chat, browser=browser, control=_Control([[], [], []]), source=source,
                ledger=SessionLedger(root / "audit.jsonl", "raw-read"))
            session.entry_observation = _obs(1)
            result = session.run()
            self.assertEqual("agent_finished", result.terminal_reason)
            self.assertEqual(["observe"], [action for action, _ in browser.done])
            self.assertEqual("read_source", session.ledger.of_kind("action_receipt")[0]["after_tool"])
            self.assertEqual("src/policy.py", session.evidence.get("source#1").path)


if __name__ == "__main__":
    unittest.main()
