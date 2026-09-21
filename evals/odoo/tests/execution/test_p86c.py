import unittest
from pathlib import Path

from evals.odoo.domain.tasks import RunnerInput
from evals.odoo.execution.runner_pageagent import PageAgentRunnerConfig, run
from evals.odoo.p86c import _route_permit, judge


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
