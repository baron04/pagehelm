import unittest
from pathlib import Path
from types import SimpleNamespace

from evals.odoo.domain.tasks import RunnerInput
from evals.odoo.execution.runner_pageagent import PageAgentRunnerConfig, run
from evals.odoo.p86c import (
    COMPACT_PAGE_SYSTEM,
    _compact_context,
    _route_permit,
    _server_answer,
    judge,
)


class RoutePermitTests(unittest.TestCase):
    def test_only_an_exact_safe_licensed_odoo_route_is_accepted(self):
        allowed = {"/odoo/action-example.screen"}
        self.assertTrue(_route_permit(allowed, "/odoo/action-example.screen"))
        for route in (
                "/odoo/action-example.screen?token=x", "/odoo/../secret",
                "//elsewhere.invalid/x", "/odoo/action-other.screen"):
            with self.subTest(route=route):
                self.assertFalse(_route_permit(allowed, route))


class JudgeTests(unittest.TestCase):
    def test_answer_is_decided_by_the_frozen_concepts_not_the_agent_claim(self):
        task = {"kind": "answer", "oracle": {
            "all_concepts": [["negative discount"], ["markup", "mark-up"]]}}
        artifact = {
            "product_started": True,
            "bridge": {"status": "completed", "success_claim": False},
            "turns": [{"visible_answer": "A negative discount is a mark-up."}],
            "dialogs": 0,
            "page_errors": [],
        }
        self.assertTrue(judge(task, artifact)["passed"])

    def test_navigation_requires_the_observed_breadcrumb(self):
        task = {"kind": "navigation", "oracle": {
            "breadcrumb_contains": "Analytic Items"}}
        artifact = {
            "product_started": True,
            "bridge": {"status": "completed", "success_claim": True},
            "turns": [{"visible_answer": "done"}],
            "final": {"path": "/odoo/action-153", "breadcrumb": "Inbox"},
            "dialogs": 0,
            "page_errors": [],
        }
        self.assertEqual(judge(task, artifact)["failures"], ["wrong_breadcrumb"])

    def test_server_answer_does_not_require_a_browser_artifact(self):
        task = {"kind": "answer", "oracle": {
            "all_concepts": [["not paid"], ["paid"]]}}
        artifact = {
            "execution": "server_answer",
            "status": "completed",
            "answer": "The values include Not Paid and Paid.",
        }
        self.assertTrue(judge(task, artifact)["passed"])


class OptimizedExecutionTests(unittest.TestCase):
    def test_compact_page_prompt_is_application_neutral_and_bounded(self):
        self.assertLess(len(COMPACT_PAGE_SYSTEM), 1500)
        self.assertNotIn("Odoo", COMPACT_PAGE_SYSTEM)

    def test_optimized_evaluation_uses_the_exact_query_and_bounded_result_count(self):
        class Lookup:
            release = "release"

            def __init__(self):
                self.calls = []

            def search(self, query, *, limit=8, route=None):
                self.calls.append((query, limit, route))
                return ()

        raw = Lookup()
        context, knowledge = _compact_context(
            {"category": "field_semantics", "kind": "answer", "skill": "",
             "prompt": "List Payment Status values."},
            "B_raw", raw=raw, mixed=Lookup(), tree=object(), routes_by_evidence={})
        self.assertIsNotNone(knowledge)
        self.assertIn("Deployed-source evidence", context)
        self.assertEqual(raw.calls[0][0], "List Payment Status values.")
        self.assertEqual(raw.calls[0][1], 5)

    def test_server_answer_uses_one_small_completion_and_server_citations(self):
        class Client:
            def __init__(self):
                self.calls = []

            def complete(self, messages, tools, run_id, **kwargs):
                self.calls.append((messages, tools, run_id, kwargs))
                return {"content": "A negative discount is a markup."}

        client = Client()
        task = {"prompt": "What does a negative discount mean?"}
        knowledge = SimpleNamespace(calls=[{
            "citations": ["addons/example/models/item.py:10-12"]}])
        artifact = _server_answer(
            task, "C_mixed", "bounded fact", knowledge,
            client=client, run_id="run-1", timeout_s=20)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1], None)
        self.assertEqual(client.calls[0][3]["max_tokens"], 768)
        self.assertEqual(client.calls[0][3]["options"], {"thinking_budget": 256})
        self.assertIn("addons/example/models/item.py:10-12", artifact["answer"])

    def test_visible_page_control_does_not_trigger_source_retrieval(self):
        class Lookup:
            release = "release"

            def search(self, *_args, **_kwargs):
                raise AssertionError("visible control must not search source")

        context, knowledge = _compact_context(
            {"category": "visible_page_control", "kind": "navigation", "skill": ""},
            "C_mixed", raw=Lookup(), mixed=Lookup(), tree=object(),
            routes_by_evidence={})
        self.assertIsNone(knowledge)
        self.assertIn("visible interface", context)


class RunnerKnowledgeProfileTests(unittest.TestCase):
    @staticmethod
    def config(**changes):
        values = {
            "base_url": "https://app.invalid",
            "actors": {},
            "evidence_dir": Path("/unused/evidence"),
            "bundle_path": Path("/unused/bundle.js"),
            "bundle_sha256": "sha256:" + "0" * 64,
        }
        values.update(changes)
        return PageAgentRunnerConfig(**values)

    @staticmethod
    def call(config):
        return run(
            RunnerInput("t", "writer", "en-US", "/", ("do it",)), config,
            client=None, contract=None, run_id="run", playwright=None)

    def test_browser_only_rejects_a_route_authority(self):
        with self.assertRaisesRegex(ValueError, "browser-only"):
            self.call(self.config(route_handler=lambda _route: True))

    def test_route_profile_requires_a_server_route_authority(self):
        with self.assertRaisesRegex(ValueError, "requires a route handler"):
            self.call(self.config(capability_profile="knowledge_evaluation"))

    def test_lookup_only_profile_rejects_a_route_authority(self):
        with self.assertRaisesRegex(ValueError, "cannot receive a route handler"):
            self.call(self.config(
                capability_profile="knowledge_lookup",
                route_handler=lambda _route: True))

    def test_tools_and_handler_must_be_configured_together(self):
        with self.assertRaisesRegex(ValueError, "configured together"):
            self.call(self.config(
                capability_profile="knowledge_lookup",
                knowledge_tools=({"name": "lookup_source"},)))


if __name__ == "__main__":
    unittest.main()
