"""Same-language Han source lookup and distinguishable empty search feedback."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.session import LabApparatus, ServerAgentSession, TaskSpec
from web_operation_agent.planning.repository_source import RepositorySourceLookup
from web_operation_agent.planning.source_facts import CompiledSourceLookup, terms
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent.schemas import SCHEMA_VERSIONS, SourceMap, SourceRelation
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control, _obs
from web_operation_agent.tests.support.planning import _map
from web_operation_agent.tests.support.route_fixture import FIXTURE_ROUTE_PORT


class UnicodeSourceSearchTests(unittest.TestCase):
    def test_han_pairs_preserve_english_identifier_tokens(self):
        self.assertEqual(
            ["销售", "售订", "订单", "invoice", "status"],
            terms("销售订单 invoiceStatus"),
        )
        self.assertEqual(["税"], terms("税"))
        self.assertEqual([], terms("!!!"))

    def test_raw_and_compiled_search_find_chinese_source_with_verified_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src/menu.xml").write_text(
                '<menuitem name="销售订单状态" action="open_orders"/>\n',
                encoding="utf-8",
            )
            (root / "src/fields.py").write_text(
                'label = "销售订单状态"\n', encoding="utf-8",
            )
            (root / "tests").mkdir()
            (root / "tests/secret.py").write_text(
                'label = "秘密订单状态"\n', encoding="utf-8",
            )
            tree = SourceTree({
                "repository_id": "fixture/application", "release": "build:one",
                "allow_globs": ["src/**"], "deny_globs": ["tests/**"],
            }, root)
            raw = RepositorySourceLookup(tree, line_limit=8, overlap=2)
            raw_hit = raw.search("销售订单状态", limit=5)
            self.assertEqual({"src/menu.xml", "src/fields.py"},
                             {hit.evidence.path for hit in raw_hit})
            self.assertTrue(all(tree.verify(hit.evidence) for hit in raw_hit))
            self.assertEqual((), raw.search("秘密"))

            ref = tree.cite("src/fields.py", 1, 1, excerpt_chars=200)
            source_map = SourceMap(
                schema_version=SCHEMA_VERSIONS["SourceMap"],
                attestation_id="fixture", repository_id=tree.repository_id,
                release=tree.release,
                relations=[SourceRelation(
                    kind="service_entity", subject="order", object="state",
                    attrs={"string": "销售订单状态"}, evidence=(ref,),
                )],
            )
            hits = CompiledSourceLookup(source_map).search("订单状态")
            self.assertEqual(1, len(hits))
            self.assertTrue(tree.verify(hits[0].evidence))
            self.assertIn("销售订单状态", hits[0].text)

    def test_empty_tool_feedback_separates_unsearchable_and_unmatched_queries(self):
        for query, expected in (
            ("!!!", "No searchable source terms"),
            ("nonesuchlabel", "No matching source evidence"),
        ):
            with self.subTest(query=query), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                chat = _Chat([
                    ("lookup_source", {"query": query}),
                    ("finish", {"done": False, "why": "diagnostic complete"}),
                ])
                session = ServerAgentSession(
                    application_routes=FIXTURE_ROUTE_PORT,
                    task=TaskSpec("T", "Find the requested source fact"),
                    lab=LabApparatus(arm="S"), chat=chat,
                    browser=_Browser([_obs(1), _obs(2)]),
                    control=_Control([[], []]),
                    ledger=SessionLedger(root / "run.jsonl", "unicode-search"),
                    source=SourceCapability(
                        source_map=_map(root), source_contract=True,
                        runtime_source=True,
                    ),
                )
                session.entry_observation = _obs(1)
                session.run()
                self.assertEqual(2, len(chat.seen))
                self.assertIn(expected, str(chat.seen[1]))


if __name__ == "__main__":
    unittest.main()
