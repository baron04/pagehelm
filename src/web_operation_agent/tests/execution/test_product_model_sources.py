"""Source evidence is release-bound, allowlisted and local to one model turn."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from web_operation_agent.answer.conclusion import ConclusionRefused
from web_operation_agent.answer.evidence import EvidenceUnknown
from web_operation_agent.answer.answer_view import source_release_label
from web_operation_agent.execution.model_sources import TurnSources
from web_operation_agent.privacy import SessionHandoffPolicy


class SourceToolsTests(unittest.TestCase):
    def setUp(self):
        from web_operation_agent.tests.execution.test_repository_source_turn import RepositorySourceTurns
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = RepositorySourceTurns()._source(Path(self.tmp.name), "src/policy.py",
            "batch_limit = 125\n# token=private-value\n")
        self.events = []
        self.tools = self.fresh()

    def fresh(self):
        return TurnSources(self.source, SessionHandoffPolicy.extending(()),
            SimpleNamespace(append=lambda event, **facts: self.events.append((event, facts))))

    def finish(self, **claim):
        return self.tools.finish({"status": "done", "answer": "unbound prose",
            "source_conclusions": [{"kind": "source_excerpt", "evidence_ref": "source#1",
                "statement": "The batch limit is 125.", "support": "batch_limit = 125", **claim}]},
            runtime_used=False, user_text="What batch limit?", max_chars=8000)

    def test_lookup_and_raw_read_share_exact_evidence_and_redact_credentials(self):
        found = self.tools.execute("lookup_source", {"query": "batch_limit"})
        self.assertIn("source#1", found.feedback)
        self.assertNotIn("private-value", found.feedback)
        self.assertEqual("According to the fixture-1 source: The batch limit is 125.",
                         self.finish())
        read = self.tools.execute("read_source", {"path": "src/policy.py", "start_line": 1, "end_line": 1})
        self.assertIn("batch_limit = 125", read.feedback)
        self.assertTrue(self.events)
        self.assertNotIn("private-value", str(self.events))

    def test_false_support_forged_citation_and_previous_turn_reference_are_refused(self):
        self.tools.execute("lookup_source", {"query": "batch_limit"})
        for claim in ({"support": "batch_limit = 500"}, {"statement": "See src/forged.py:99."},
                      {"evidence_ref": "source#999"}):
            with self.subTest(claim=claim), self.assertRaises((ConclusionRefused, EvidenceUnknown)):
                self.finish(**claim)
        self.tools = self.fresh()
        with self.assertRaises(ConclusionRefused):
            self.finish()

    def test_missing_ports_and_forbidden_paths_remain_typed(self):
        for args in ({"path": ".env", "start_line": 1, "end_line": 1},
                     {"path": "../policy.py", "start_line": 1, "end_line": 1},
                     {"path": "src/policy.py", "start_line": 1, "end_line": 121}):
            result = self.tools.execute("read_source", args)
            self.assertEqual("refused", result.state)
            self.assertTrue(result.error_code)
        history = self.tools.execute("lookup_source_history", {"kind": "owners"})
        self.assertEqual("source_history_unavailable", history.error_code)

    def test_source_only_finish_cannot_replace_evidence_with_free_prose(self):
        self.tools.execute("lookup_source", {"query": "batch_limit"})
        with self.assertRaises(ConclusionRefused):
            self.tools.finish({"status": "done", "answer": "The batch limit is 999."},
                runtime_used=False, user_text="limit?", max_chars=8000)

    def test_source_refusal_uses_bounded_server_text_and_release(self):
        self.tools.execute("lookup_source", {"query": "absent rule"})
        answer = self.tools.finish({"status": "cannot", "answer": "The rule always allows this."},
            runtime_used=False, user_text="Does this rule allow it?", max_chars=8000)
        self.assertIn("fixture-1", answer)
        self.assertIn("no direct basis", answer)
        self.assertNotIn("always allows", answer)
        self.assertNotIn("absent rule", answer)

    def test_source_version_label_cannot_add_markdown_or_a_new_line(self):
        self.assertEqual(r"v1 \[draft\]", source_release_label("v1\n[draft]"))
