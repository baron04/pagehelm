"""Real-Chromium qualification of the PageAgent Twenty runner without a model call.

The built bundle runs in headless Chromium against a loopback synthetic page; provider calls are
answered by a scripted client and business calls by a synthetic Business Runtime. Local runs skip
when the bundle has not been built or Chromium cannot launch. Set
``WOA_REQUIRE_PAGEAGENT_BROWSER=1`` in qualification environments to make either condition fail.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from evals.pageagent.scripted import (
    ScriptedProviderClient, SyntheticCompanyExecutor, SyntheticTwentyAdapter,
    SyntheticTwentyPage, company_ui_path, development_case, evaluate_run, query_answer,
    synthetic_business_runtime, synthetic_companies, synthetic_state, write_storage_state,
)
from evals.pageagent.twenty_runner import PageAgentTwentyConfig, run_case
from web_operation_agent.business.runtime import (
    BusinessToolIdentityError, BusinessToolUnavailableError,
)


BUNDLE = Path(os.environ.get("WOA_PAGEAGENT_BUNDLE") or (
    Path(__file__).resolve().parents[2] / ".artifacts" / "evals" / "pageagent"
    / "pageagent-baseline.js"))


def _chromium_launches() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception:
        return False
    return True


class RunnerBrowserQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get("WOA_REQUIRE_PAGEAGENT_BROWSER") == "1"
        if not BUNDLE.is_file():
            if required:
                raise RuntimeError("PageAgent baseline bundle is not built")
            raise unittest.SkipTest("PageAgent baseline bundle is not built")
        if not _chromium_launches():
            if required:
                raise RuntimeError("Chromium is unavailable")
            raise unittest.SkipTest("Chromium is unavailable")
        cls.bundle_digest = "sha256:" + hashlib.sha256(BUNDLE.read_bytes()).hexdigest()

    def setUp(self):
        self.companies = synthetic_companies(3)
        self.page = SyntheticTwentyPage(self.companies).__enter__()
        self.directory = tempfile.TemporaryDirectory()
        self.state = write_storage_state(
            Path(self.directory.name) / "state.json", origin=self.page.origin)
        self.executor = SyntheticCompanyExecutor(self.companies)
        self.runtime = synthetic_business_runtime(self.executor)
        self.adapter = SyntheticTwentyAdapter(origin=self.page.origin, runtime=self.runtime)

    def tearDown(self):
        self.directory.cleanup()
        self.page.__exit__(None, None, None)

    def _config(self, **changes) -> PageAgentTwentyConfig:
        return PageAgentTwentyConfig(
            bundle_path=BUNDLE, bundle_digest=self.bundle_digest,
            storage_state_path=self.state, wall_time_s=60, max_steps=6, **changes)

    def _run(self, *, case, client, config=None, adapter=None, cancelled=None,
             approval_decider=None) -> dict:
        return run_case(
            case=case, adapter=adapter or self.adapter, client=client,
            config=config or self._config(), run_id="pageagent-browser-qualification",
            cancelled=cancelled, approval_decider=approval_decider)

    def _query_actions(self, answer: str | None = None) -> list[dict]:
        return [{"query_companies": {"limit": 50}},
                {"done": {"text": answer or query_answer(self.companies), "success": True}}]

    def _open_case(self) -> tuple[dict, dict]:
        case = development_case("dev-open-en-1", self.companies)
        target = next(company for company in self.companies
                      if company["name"] in case["prompt"])
        return case, target

    def _open_actions(self, target: dict, route: str) -> list[dict]:
        return [{"query_companies": {"name_contains": target["name"], "limit": 20}},
                {"open_allowed_route": {"route": route}},
                {"done": {"text": "Opened the requested company record.", "success": True}}]

    def test_query_success_passes_through_the_bundle_hosts_and_evaluator(self):
        case = development_case("dev-query-en-1", self.companies)
        client = ScriptedProviderClient(self._query_actions())
        result = self._run(case=case, client=client)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["bridge"]["upstream_version"], "1.12.4")
        self.assertEqual(result["bridge"]["metrics"], {
            "provider_calls": 2, "page_actions": 0, "business_calls": 1,
            "retries": 0, "steps": 2})
        self.assertEqual(result["provider"]["provider_attempts"], 2)
        self.assertEqual(result["provider"]["provider_responses"], 2)
        self.assertEqual(result["provider"]["accounting"]["model_calls"], 2)
        self.assertEqual(result["business"]["business_responses"], 1)
        self.assertTrue(result["session_binding_unchanged"])
        self.assertEqual(self.executor.calls[0]["args"], {"limit": 50})
        self.assertEqual(len(self.executor.calls[0]["operation_context_digest"]), 64)
        self.assertTrue(all(request["model"] == client.model for request in client.requests))
        self.assertEqual(sorted(evidence["name"] for evidence in result["evidence"]["companies"]),
                         sorted(company["name"] for company in self.companies))
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "PASS")

    def test_browser_only_query_uses_the_official_dom_surface_without_custom_tools(self):
        case = development_case("dev-query-en-1", self.companies)
        with SyntheticTwentyPage(self.companies, expose_company_list=True) as page:
            state = write_storage_state(
                Path(self.directory.name) / "browser-only-state.json", origin=page.origin)
            adapter = SyntheticTwentyAdapter(origin=page.origin, runtime=self.runtime)
            config = PageAgentTwentyConfig(
                bundle_path=BUNDLE, bundle_digest=self.bundle_digest,
                storage_state_path=state, wall_time_s=60, max_steps=4,
                capability_profile="browser_only")
            result = run_case(
                case=case, adapter=adapter,
                client=ScriptedProviderClient([
                    {"done": {"text": query_answer(self.companies), "success": True}},
                ]),
                config=config, run_id="pageagent-browser-only-query")

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["engine"]["name"], "pageagent")
        self.assertEqual(result["trial"]["business_calls"], 0)
        self.assertIsNone(result["business"]["skill_digest"])
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "PASS")

    def test_browser_only_open_clicks_a_visible_record_link(self):
        case, target = self._open_case()
        with SyntheticTwentyPage(self.companies, expose_company_list=True) as page:
            state = write_storage_state(
                Path(self.directory.name) / "browser-open-state.json", origin=page.origin)
            adapter = SyntheticTwentyAdapter(origin=page.origin, runtime=self.runtime)
            config = PageAgentTwentyConfig(
                bundle_path=BUNDLE, bundle_digest=self.bundle_digest,
                storage_state_path=state, wall_time_s=60, max_steps=5,
                capability_profile="browser_only")
            result = run_case(
                case=case, adapter=adapter,
                client=ScriptedProviderClient([
                    {"click_element_by_index": {"index": 0}},
                    {"done": {"text": "Opened.", "success": True}},
                ]),
                config=config, run_id="pageagent-browser-only-open")

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["evidence"]["page"]["path"], company_ui_path(target))
        self.assertIn(target["name"], result["evidence"]["page"]["visible_projection"])
        self.assertEqual(result["trial"]["business_calls"], 0)
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "PASS")

    def test_create_shows_exact_approval_and_dispatches_once(self):
        from evals.twenty.evaluator import evaluate

        executor = SyntheticCompanyExecutor(self.companies, allow_mutations=True)
        adapter = SyntheticTwentyAdapter(
            origin=self.page.origin, runtime=synthetic_business_runtime(executor))
        case = development_case("dev-create-zh-1", self.companies)
        expected = case["evaluator_task"]
        messages: list[str] = []
        result = self._run(
            case=case, adapter=adapter, config=self._config(allow_create=True),
            client=ScriptedProviderClient([
                {"create_company": {
                    "name": expected["name"], "domain": expected["domain"]}},
                {"done": {"text": "Created.", "success": True}},
            ]),
            approval_decider=lambda message: messages.append(message) or "approved")

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(executor.mutation_dispatches, 1)
        self.assertEqual(result["trial"]["approvals_requested"], 1)
        self.assertEqual(result["trial"]["approvals_approved"], 1)
        self.assertEqual(len(messages), 1)
        self.assertIn(expected["name"], messages[0])
        self.assertIn(expected["domain"], messages[0])
        verdict = evaluate(
            task=expected,
            before=synthetic_state(self.companies, role="baseline"),
            after=synthetic_state(executor.companies, role="observation"),
            evidence=result["evidence"])
        self.assertEqual(verdict["status"], "PASS", verdict)

    def test_create_is_denied_by_default_without_dispatch(self):
        executor = SyntheticCompanyExecutor(self.companies, allow_mutations=True)
        adapter = SyntheticTwentyAdapter(
            origin=self.page.origin, runtime=synthetic_business_runtime(executor))
        case = development_case("dev-create-zh-1", self.companies)
        expected = case["evaluator_task"]
        result = self._run(
            case=case, adapter=adapter, config=self._config(allow_create=True),
            client=ScriptedProviderClient([{"create_company": {
                "name": expected["name"], "domain": expected["domain"]}}]))

        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["error_code"], "approval_denied")
        self.assertEqual(executor.mutation_dispatches, 0)
        self.assertEqual(result["trial"]["approvals_denied"], 1)

    def test_create_then_open_requires_the_created_route_and_visible_readback(self):
        from evals.twenty.evaluator import evaluate

        case = development_case("dev-create-en-1", self.companies)
        expected = case["evaluator_task"]
        created_id = "99999999-9999-4999-8999-999999999999"
        created = {"id": created_id, "name": expected["name"],
                   "domain": expected["domain"]}
        executor = SyntheticCompanyExecutor(
            self.companies, allow_mutations=True, id_factory=lambda: created_id)
        with SyntheticTwentyPage([*self.companies, created]) as page:
            state = write_storage_state(
                Path(self.directory.name) / "create-open-state.json", origin=page.origin)
            adapter = SyntheticTwentyAdapter(
                origin=page.origin, runtime=synthetic_business_runtime(executor))
            config = PageAgentTwentyConfig(
                bundle_path=BUNDLE, bundle_digest=self.bundle_digest,
                storage_state_path=state, wall_time_s=60, max_steps=6,
                allow_create=True)
            route = company_ui_path(created)
            result = run_case(
                case=case, adapter=adapter,
                client=ScriptedProviderClient([
                    {"create_company": {
                        "name": expected["name"], "domain": expected["domain"]}},
                    {"open_allowed_route": {"route": route}},
                    {"done": {"text": "Created and opened.", "success": True}},
                ]),
                config=config, run_id="pageagent-create-open",
                approval_decider=lambda _message: "approved")

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["evidence"]["page"], {
            "path": route, "name": expected["name"], "domain": expected["domain"]})
        self.assertEqual(result["business"]["route_permits"], 1)
        verdict = evaluate(
            task=expected,
            before=synthetic_state(self.companies, role="baseline"),
            after=synthetic_state(executor.companies, role="observation"),
            evidence=result["evidence"])
        self.assertEqual(verdict["status"], "PASS", verdict)

    def test_wrong_answer_fails_even_though_every_tool_call_succeeded(self):
        case = development_case("dev-query-en-1", self.companies)
        for answer in (query_answer(self.companies[:2]),
                       "```json\n" + query_answer(self.companies) + "\n```"):
            with self.subTest(answer=answer[:40]):
                client = ScriptedProviderClient(self._query_actions(answer))
                result = self._run(case=case, client=client)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error_code"], "evidence_contract")
                self.assertEqual(result["bridge"]["success_claim"], True)
                self.assertEqual(result["business"]["business_responses"], 1)
                self.assertEqual(evaluate_run(case, self.companies, result)["status"], "FAIL")

    def test_open_success_navigates_only_through_a_permitted_route(self):
        case, target = self._open_case()
        route = company_ui_path(target)
        client = ScriptedProviderClient(self._open_actions(target, route))
        result = self._run(case=case, client=client)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["evidence"]["page"], {
            "path": route, "name": target["name"], "domain": target["domain"]})
        self.assertEqual(result["bridge"]["metrics"], {
            "provider_calls": 3, "page_actions": 1, "business_calls": 1,
            "retries": 0, "steps": 3})
        self.assertEqual(result["business"]["route_attempts"], 1)
        self.assertEqual(result["business"]["route_permits"], 1)
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "PASS")

    def test_unauthorized_route_is_refused_by_the_host_before_navigation(self):
        case, target = self._open_case()
        other = next(company for company in self.companies if company is not target)
        client = ScriptedProviderClient(self._open_actions(target, company_ui_path(other)))
        result = self._run(case=case, client=client)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["apparatus_code"])
        self.assertEqual(result["error_code"], "route_refused")
        self.assertEqual(result["bridge"]["status"], "error")
        self.assertEqual(result["bridge"]["metrics"]["page_actions"], 0)
        self.assertEqual(result["business"]["route_attempts"], 1)
        self.assertEqual(result["business"]["route_permits"], 0)
        self.assertEqual(client.dispatched, 2)

    def test_identity_failures_before_and_during_the_run_invalidate_the_trial(self):
        case = development_case("dev-query-en-1", self.companies)
        adapter = SyntheticTwentyAdapter(
            origin=self.page.origin, runtime=self.runtime,
            identity_failure=BusinessToolIdentityError(
                "page session and delegated API differ"))
        client = ScriptedProviderClient(self._query_actions())
        result = self._run(case=case, client=client, adapter=adapter)
        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "credential_or_identity")
        self.assertEqual(result["error_code"], "identity_binding")
        self.assertEqual(client.dispatched, 0)

        # The only interactive element on the synthetic list page replaces the session cookie.
        client = ScriptedProviderClient([
            {"click_element_by_index": {"index": 0}},
            {"query_companies": {"limit": 50}},
            {"done": {"text": query_answer(self.companies), "success": True}},
        ])
        result = self._run(case=case, client=client)
        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "credential_or_identity")
        self.assertEqual(result["error_code"], "session_changed")
        self.assertFalse(result["session_binding_unchanged"])
        self.assertEqual(result["bridge"]["success_claim"], True)
        self.assertEqual(result["evidence"]["execution_status"], "failed")

    def test_model_call_budget_is_an_engine_failure(self):
        case = development_case("dev-query-en-1", self.companies)
        client = ScriptedProviderClient(self._query_actions())
        result = self._run(case=case, client=client, config=self._config(max_model_calls=1))
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["apparatus_code"])
        self.assertEqual(result["error_code"], "model_call_budget")
        self.assertEqual(result["provider"]["provider_attempts"], 1)
        self.assertEqual(result["provider"]["provider_responses"], 1)
        self.assertEqual(client.dispatched, 1)
        self.assertEqual(result["bridge"]["status"], "error")

    def test_cancellation_is_enforced_by_the_hosts(self):
        case = development_case("dev-query-en-1", self.companies)
        cancelled = threading.Event()
        client = ScriptedProviderClient(
            self._query_actions(),
            after_call=lambda sequence: cancelled.set() if sequence == 1 else None)
        result = self._run(case=case, client=client, cancelled=cancelled)
        self.assertEqual((result["status"], result["error_code"]), ("failed", "cancelled"))
        self.assertIsNone(result["apparatus_code"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(client.dispatched, 1)
        self.assertEqual(result["business"]["business_responses"], 0)

    def test_provider_outage_invalidates_the_trial(self):
        case = development_case("dev-query-en-1", self.companies)
        client = ScriptedProviderClient(
            self._query_actions(), failures={1: "provider_transport"})
        result = self._run(case=case, client=client)
        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "provider_outage")
        self.assertEqual(result["error_code"], "provider_transport")
        self.assertEqual(result["provider"]["provider_attempts"], 1)
        self.assertEqual(result["provider"]["provider_responses"], 0)
        self.assertEqual(result["provider"]["accounting"]["usage_unavailable_calls"], 1)
        self.assertIsNone(result["provider"]["accounting"]["total_cost_cny"])
        self.assertEqual(result["business"]["business_attempts"], 0)

    def test_business_outage_invalidates_the_trial(self):
        case = development_case("dev-query-en-1", self.companies)
        executor = SyntheticCompanyExecutor(
            self.companies,
            failure=BusinessToolUnavailableError("synthetic API outage"))
        adapter = SyntheticTwentyAdapter(
            origin=self.page.origin, runtime=synthetic_business_runtime(executor))
        result = self._run(
            case=case, client=ScriptedProviderClient(self._query_actions()),
            adapter=adapter)
        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "transport_contract")
        self.assertEqual(result["error_code"], "business_unavailable")

    def test_artifact_is_schema_fixed_and_content_free(self):
        case, target = self._open_case()
        route = company_ui_path(target)
        client = ScriptedProviderClient(self._open_actions(target, route))
        result = self._run(case=case, client=client)
        self.assertEqual(result["status"], "completed", result)
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(case["prompt"], rendered)
        self.assertNotIn("synthetic-session-1", rendered)
        self.assertNotIn("replaced-session", rendered)
        self.assertNotIn(urlsplit(self.page.origin).netloc, rendered)
        for company in self.companies:
            if company is not target:
                self.assertNotIn(company["name"], rendered)
        self.assertRegex(result["run_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(result["runtime_trace_digest"], r"^sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
