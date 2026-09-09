from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from evals.pageagent.scripted import (
    ScriptedProviderClient, SyntheticCompanyExecutor, SyntheticTwentyAdapter,
    company_ui_path, development_case, evaluate_run, query_answer, synthetic_business_runtime,
    synthetic_companies, write_storage_state,
)
from evals.pageagent.twenty_runner import (
    PageAgentTwentyConfig, PageAgentTwentyRunnerError, _bridge_reports_match,
    _business_apparatus_code, _classify_host_errors, _execution_case,
    _provider_apparatus_code, open_evidence, query_evidence, run_case,
)
from web_operation_agent.business import BusinessToolError


PROVIDER_URL = "https://provider.invalid/v1/chat/completions"


def _case(kind: str = "query") -> dict:
    task = {"artifact_version": 1, "kind": kind}
    if kind == "open":
        task["company_id"] = "11111111-1111-4111-8111-111111111111"
    if kind == "create":
        task.update({"name": "X", "domain": "x.test", "require_open": False})
    return {
        "artifact_version": 1,
        "kind": "twenty_execution_case",
        "set_id": "twenty-development",
        "split": "development",
        "task_id": "dev-query-one",
        "template_family_digest": "sha256:" + "a" * 64,
        "task_digest": "sha256:" + "b" * 64,
        "prompt": "List every company and domain.",
        "evaluator_task": task,
    }


def _results(companies: list[dict], *, complete: bool = True) -> list[dict]:
    return [{
        "companies": [{"name": company["name"], "domain": company["domain"],
                       "ui_path": company_ui_path(company)} for company in companies],
        "completeness": {"source_complete": complete, "result_truncated": not complete},
    }]


class EvidenceContractTests(unittest.TestCase):
    def setUp(self):
        self.companies = synthetic_companies(2)

    def test_query_answer_is_used_as_written_and_domains_are_normalized(self):
        answer = json.dumps({
            "artifact_version": 1,
            "execution_status": "completed",
            "complete": True,
            "companies": [
                {"name": self.companies[0]["name"],
                 "domain": "https://" + self.companies[0]["domain"].upper() + "/"},
                {"name": self.companies[1]["name"], "domain": self.companies[1]["domain"]},
            ],
        })
        evidence = query_evidence(answer, _results(self.companies))
        self.assertEqual(evidence["companies"][0], {
            "name": self.companies[0]["name"], "domain": self.companies[0]["domain"]})
        self.assertTrue(evidence["complete"])

    def test_query_answer_rejects_repairs_markdown_and_duplicate_rows(self):
        results = _results(self.companies)
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "strict JSON"):
            query_evidence("```json\n" + query_answer(self.companies) + "\n```", results)
        duplicate = json.dumps({
            "artifact_version": 1, "execution_status": "completed", "complete": True,
            "companies": [
                {"name": self.companies[0]["name"], "domain": self.companies[0]["domain"]},
                {"name": self.companies[0]["name"].upper(),
                 "domain": self.companies[0]["domain"].upper()},
            ],
        })
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "duplicate"):
            query_evidence(duplicate, results)

    def test_query_answer_must_restate_the_business_results_exactly(self):
        results = _results(self.companies)
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "restate"):
            query_evidence(query_answer(self.companies[:1]), results)
        invented = [*self.companies, {"name": "Invented Company", "domain": "invented.example"}]
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "restate"):
            query_evidence(query_answer(invented), results)
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "completeness"):
            query_evidence(query_answer(self.companies, complete=False), results)
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "completeness"):
            query_evidence(query_answer(self.companies),
                           _results(self.companies, complete=False))
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "no business result"):
            query_evidence(query_answer(self.companies), [])
        # A union of two partial disclosures is still a faithful restatement.
        split = [*_results(self.companies[:1], complete=False),
                 *_results(self.companies[1:], complete=True)]
        self.assertTrue(query_evidence(query_answer(self.companies), split)["complete"])

    def test_open_evidence_needs_disclosure_host_permit_and_visible_facts(self):
        target = self.companies[0]
        path = company_ui_path(target)
        results = _results(self.companies)
        visible = f"Company\n{target['name']}\nDomain\n{target['domain']}"
        evidence = open_evidence(
            path=path, visible_projection=visible, business_results=results,
            permitted_routes=[path])
        self.assertEqual(evidence["page"], {
            "path": path, "name": target["name"], "domain": target["domain"]})

        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "not visible"):
            open_evidence(path=path, visible_projection="Loading…",
                          business_results=results, permitted_routes=[path])
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "route Host"):
            open_evidence(path=path, visible_projection=visible,
                          business_results=results, permitted_routes=[])
        other = company_ui_path(self.companies[1])
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "route Host"):
            open_evidence(path=path, visible_projection=visible,
                          business_results=results, permitted_routes=[other])
        undisclosed = "/object/company/22222222-2222-4222-8222-222222222222"
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "disclosed"):
            open_evidence(path=undisclosed, visible_projection=visible,
                          business_results=results, permitted_routes=[undisclosed])

    def test_create_case_is_rejected_before_any_browser_or_model_work(self):
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "query and open only"):
            _execution_case(_case("create"))


class RunnerBoundaryTests(unittest.TestCase):
    def test_bundle_is_bound_by_digest_and_browser_state_must_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "pageagent.js"
            state = root / "state.json"
            bundle.write_bytes(b"bundle")
            state.write_text("{}", encoding="utf-8")
            digest = "sha256:" + hashlib.sha256(b"bundle").hexdigest()
            config = PageAgentTwentyConfig(
                bundle_path=bundle, bundle_digest=digest, storage_state_path=state)
            self.assertEqual(config.verify_files(), b"bundle")
            changed = PageAgentTwentyConfig(
                bundle_path=bundle, bundle_digest="sha256:" + "0" * 64,
                storage_state_path=state)
            with self.assertRaisesRegex(PageAgentTwentyRunnerError, "digest mismatch"):
                changed.verify_files()

    def test_entry_path_rejects_noncanonical_browser_targets(self):
        defaults = {
            "bundle_path": Path("bundle.js"),
            "bundle_digest": "sha256:" + "0" * 64,
            "storage_state_path": Path("state.json"),
        }
        for path in ("//other.test/x", "/objects/../admin", "/objects?q=1",
                     "/objects#x", "/objects%2fadmin", "/objects\\admin"):
            with self.subTest(path=path), self.assertRaisesRegex(
                    PageAgentTwentyRunnerError, "relative application path"):
                PageAgentTwentyConfig(**defaults, entry_path=path)

    def test_bridge_metrics_must_match_both_server_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle.js"
            state = root / "state.json"
            bundle.write_text("x", encoding="utf-8")
            state.write_text("{}", encoding="utf-8")
            config = PageAgentTwentyConfig(
                bundle_path=bundle,
                bundle_digest="sha256:" + hashlib.sha256(b"x").hexdigest(),
                storage_state_path=state)
            bridge = {"metrics": {
                "provider_calls": 3, "page_actions": 1, "business_calls": 1,
                "retries": 0, "steps": 3}}
            provider = {"provider_attempts": 3, "provider_responses": 3,
                        "accounting": {"model_calls": 3}}
            business = {"business_attempts": 1, "business_responses": 1,
                        "route_permits": 1}
            self.assertTrue(_bridge_reports_match(
                bridge=bridge, provider=provider, business=business,
                task_kind="open", config=config))
            bridge["metrics"]["provider_calls"] = 2
            self.assertFalse(_bridge_reports_match(
                bridge=bridge, provider=provider, business=business,
                task_kind="open", config=config))

    def test_host_codes_separate_engine_failures_from_apparatus_faults(self):
        for code in ("model_call_budget", "provider_deadline", "cancelled", None, ""):
            self.assertIsNone(_provider_apparatus_code(code), code)
        for code in ("provider_transport", "provider_http", "provider_response",
                     "provider_usage", "provider_capacity", "provider_circuit_open",
                     "provider_call", "provider_error"):
            self.assertEqual(_provider_apparatus_code(code), "provider_outage", code)
        self.assertEqual(_provider_apparatus_code("ledger_failure"), "ledger_failure")
        self.assertEqual(_provider_apparatus_code("transport_contract"), "transport_contract")
        for code in ("business_call_budget", "route_call_budget", "route_refused",
                     "cancelled", None):
            self.assertIsNone(_business_apparatus_code(code), code)
        for code in ("business_tool", "tool_contract", "route_contract"):
            self.assertEqual(_business_apparatus_code(code), "transport_contract", code)
        self.assertEqual(_classify_host_errors([]), (None, None))
        self.assertEqual(_classify_host_errors([("provider", "model_call_budget")]),
                         (None, "model_call_budget"))
        self.assertEqual(_classify_host_errors([("business", "route_refused")]),
                         (None, "route_refused"))
        self.assertEqual(_classify_host_errors(
            [("business", "route_refused"), ("provider", "provider_transport")]),
            ("provider_outage", "provider_transport"))
        self.assertEqual(_classify_host_errors(
            [("business", "business_tool"), ("provider", "model_call_budget")]),
            ("transport_contract", "business_tool"))
        self.assertEqual(_classify_host_errors([("provider", "host_exception")]),
                         ("provider_outage", "host_exception"))


class _FakePage:
    """The subset of a Playwright page the runner touches, driven by a fake bridge."""

    def __init__(self, session: "_FakeBrowser"):
        self.session = session
        self.url = ""
        self.bindings: dict[str, object] = {}
        self.visible = ""

    def on(self, *_args):
        return None

    def expose_function(self, name, callback):
        self.bindings[name] = callback

    def goto(self, url, **_kwargs):
        self.url = url

    def locator(self, _selector):
        return self

    def wait_for(self, **_kwargs):
        return None

    def add_script_tag(self, **_kwargs):
        self.session.script_tags += 1

    def evaluate(self, script, config=None):
        if config is not None:
            return self.session.bridge(self, self.bindings, config)
        if "__WOA_PAGEAGENT_BASELINE__" in script:
            return self.session.bridge_installed
        return self.visible


class _FakeBrowser:
    """A fake sync_playwright() manager whose single page runs ``bridge``."""

    def __init__(self, *, bridge, storage_state: dict, bridge_installed: bool = True):
        self.bridge = bridge
        self.storage_state = storage_state
        self.bridge_installed = bridge_installed
        self.launches = 0
        self.script_tags = 0
        self.closed: list[str] = []
        self.page = _FakePage(self)
        session = self

        class Context:
            @staticmethod
            def new_page():
                return session.page

            @staticmethod
            def storage_state():
                return json.loads(json.dumps(session.storage_state))

            @staticmethod
            def close():
                session.closed.append("context")

        class Browser:
            @staticmethod
            def new_context(**_kwargs):
                return Context()

            @staticmethod
            def close():
                session.closed.append("browser")

        class Chromium:
            @staticmethod
            def launch(**_kwargs):
                session.launches += 1
                return Browser()

        class Playwright:
            chromium = Chromium()

        self._playwright = Playwright()

    def __enter__(self):
        return self._playwright

    def __exit__(self, *_args):
        return None


def _pageagent_like_bridge(page: _FakePage, bindings: dict, config: dict) -> dict:
    """Mimic the real bridge: PageAgent's own failures come back as a result, never an exception."""
    metrics = {"provider_calls": 0, "page_actions": 0, "business_calls": 0,
               "retries": 0, "steps": 0}
    token = config["run_token"]
    business_names = {tool["name"] for tool in config["tools"]}
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(page.url))

    def result(status: str, success: bool, answer: str) -> dict:
        return {"bridge_version": 2, "upstream_version": "1.12.4", "status": status,
                "success_claim": success, "terminal_reason": None, "answer": answer,
                "metrics": metrics}

    try:
        for _step in range(config["max_steps"] + 1):
            body = {"model": config["model"], **config["request_options"],
                    "messages": [{"role": "user", "content": config["prompt"]}],
                    "tools": [{"type": "function", "function": {"name": name}}
                              for name in sorted(business_names)]}
            metrics["provider_calls"] += 1
            response = bindings["__WOA_PAGEAGENT_PROVIDER_FETCH__"]({
                "run_token": token, "url": PROVIDER_URL, "method": "POST",
                "body": json.dumps(body)})
            completion = json.loads(response["body"])
            arguments = json.loads(
                completion["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
            name, args = next(iter(arguments["action"].items()))
            metrics["steps"] += 1
            if name == "done":
                return result("completed", bool(args.get("success", True)), args.get("text", ""))
            if name == "open_allowed_route":
                permit = bindings["__WOA_PAGEAGENT_OPEN_ROUTE__"]({
                    "run_token": token, "route": args["route"]})
                page.url = origin + permit["route"]
                page.visible = page.session.visible_routes.get(permit["route"], "")
                metrics["page_actions"] += 1
            elif name in business_names:
                bindings["__WOA_PAGEAGENT_BUSINESS_TOOL__"]({
                    "run_token": token, "name": name, "args": args})
                metrics["business_calls"] += 1
            else:
                metrics["page_actions"] += 1
        return result("error", False, "Step count exceeded maximum limit")
    except Exception as exc:  # the real bridge sees a rejected binding promise
        return result("error", False, f"Error: {exc}")


class RunnerScenarioTests(unittest.TestCase):
    """Drive run_case through a fake Playwright with the same scripted provider and synthetic
    Business Runtime the real-browser qualification uses."""

    def setUp(self):
        self.companies = synthetic_companies(3)
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.bundle = root / "bundle.js"
        self.bundle.write_text("bundle", encoding="utf-8")
        self.origin = "http://127.0.0.1:3020"
        self.state = write_storage_state(root / "state.json", origin=self.origin)
        self.storage_state = json.loads(self.state.read_text(encoding="utf-8"))
        self.executor = SyntheticCompanyExecutor(self.companies)
        self.runtime = synthetic_business_runtime(self.executor)
        self.adapter = SyntheticTwentyAdapter(origin=self.origin, runtime=self.runtime)

    def tearDown(self):
        self.directory.cleanup()

    def _config(self, **changes) -> PageAgentTwentyConfig:
        return PageAgentTwentyConfig(
            bundle_path=self.bundle,
            bundle_digest="sha256:" + hashlib.sha256(b"bundle").hexdigest(),
            storage_state_path=self.state, wall_time_s=60, **changes)

    def _run(self, *, case, client, config=None, adapter=None, storage_state=None,
             bridge=_pageagent_like_bridge, bridge_installed=True, cancelled=None,
             visible_routes=None):
        browser = _FakeBrowser(
            bridge=bridge, storage_state=storage_state or self.storage_state,
            bridge_installed=bridge_installed)
        browser.visible_routes = visible_routes or {}
        with patch("playwright.sync_api.sync_playwright", return_value=browser):
            result = run_case(
                case=case, adapter=adapter or self.adapter, client=client,
                config=config or self._config(), run_id="pageagent-offline-run",
                cancelled=cancelled)
        return result, browser

    def _query_actions(self, answer: str | None = None) -> list[dict]:
        return [{"query_companies": {"limit": 50}},
                {"done": {"text": answer or query_answer(self.companies), "success": True}}]

    def _open_actions(self, target: dict, route: str) -> list[dict]:
        return [{"query_companies": {"name_contains": target["name"], "limit": 20}},
                {"open_allowed_route": {"route": route}},
                {"done": {"text": "Opened the requested company record.", "success": True}}]

    def test_query_success_binds_identity_hosts_and_passes_the_evaluator(self):
        case = development_case("dev-query-en-1", self.companies)
        client = ScriptedProviderClient(self._query_actions())
        result, browser = self._run(case=case, client=client)
        self.assertEqual(result["status"], "completed", result)
        self.assertIsNone(result["error_code"])
        self.assertTrue(result["session_binding_unchanged"])
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["provider"]["provider_attempts"], 2)
        self.assertEqual(result["provider"]["accounting"]["model_calls"], 2)
        self.assertEqual(result["business"]["business_attempts"], 1)
        self.assertEqual(result["bridge"]["metrics"]["business_calls"], 1)
        self.assertEqual(len(self.executor.calls[0]["operation_context_digest"]), 64)
        self.assertEqual(browser.closed, ["context", "browser"])
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "PASS")
        rendered = json.dumps(result)
        for company in self.companies:
            self.assertNotIn(company["id"], rendered)

    def test_wrong_or_repaired_answers_fail_without_repair(self):
        case = development_case("dev-query-en-1", self.companies)
        for answer in (query_answer(self.companies[:2]),
                       "```json\n" + query_answer(self.companies) + "\n```",
                       query_answer(self.companies, complete=False),
                       "Here are the companies: " + query_answer(self.companies)):
            with self.subTest(answer=answer[:40]):
                client = ScriptedProviderClient(self._query_actions(answer))
                result, _browser = self._run(case=case, client=client)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error_code"], "evidence_contract")
                self.assertEqual(result["evidence"], {
                    "artifact_version": 1, "execution_status": "failed"})
                self.assertEqual(evaluate_run(case, self.companies, result)["status"], "FAIL")

    def test_open_success_requires_disclosed_permitted_route_and_visible_page(self):
        case = development_case("dev-open-en-1", self.companies)
        target = next(company for company in self.companies
                      if company["name"] in case["prompt"])
        route = company_ui_path(target)
        client = ScriptedProviderClient(self._open_actions(target, route))
        result, _browser = self._run(
            case=case, client=client,
            visible_routes={route: f"{target['name']}\nDomain\n{target['domain']}"})
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["evidence"]["page"], {
            "path": route, "name": target["name"], "domain": target["domain"]})
        self.assertEqual(result["business"]["route_permits"], 1)
        self.assertEqual(result["bridge"]["metrics"]["page_actions"], 1)
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "PASS")

    def test_open_fails_when_the_visible_page_lacks_the_record_facts(self):
        case = development_case("dev-open-en-1", self.companies)
        target = next(company for company in self.companies
                      if company["name"] in case["prompt"])
        route = company_ui_path(target)
        client = ScriptedProviderClient(self._open_actions(target, route))
        result, _browser = self._run(
            case=case, client=client, visible_routes={route: "Loading…"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "evidence_contract")

    def test_unauthorized_route_is_refused_and_recorded_as_engine_failure(self):
        case = development_case("dev-open-en-1", self.companies)
        target = next(company for company in self.companies
                      if company["name"] in case["prompt"])
        other = next(company for company in self.companies if company is not target)
        client = ScriptedProviderClient(self._open_actions(target, company_ui_path(other)))
        result, browser = self._run(case=case, client=client)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["apparatus_code"])
        self.assertEqual(result["error_code"], "route_refused")
        self.assertEqual(result["business"]["route_attempts"], 1)
        self.assertEqual(result["business"]["route_permits"], 0)
        self.assertEqual(urlsplit(browser.page.url).path, "/objects/companies")
        self.assertEqual(result["bridge"]["status"], "error")

    def test_identity_failure_before_launch_is_a_credential_apparatus_fault(self):
        case = development_case("dev-query-en-1", self.companies)
        adapter = SyntheticTwentyAdapter(
            origin=self.origin, runtime=self.runtime,
            identity_failure=BusinessToolError("page session and delegated API differ"))
        client = ScriptedProviderClient(self._query_actions())
        result, browser = self._run(case=case, client=client, adapter=adapter)
        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "credential_or_identity")
        self.assertEqual(result["error_code"], "identity_binding")
        self.assertEqual(browser.launches, 0)
        self.assertEqual(result["provider"]["provider_attempts"], 0)
        self.assertEqual(result["provider"]["accounting"]["model_calls"], 0)
        self.assertIsNone(result["business"])
        self.assertEqual(client.dispatched, 0)

    def test_replaced_browser_session_invalidates_even_a_claimed_success(self):
        case = development_case("dev-query-en-1", self.companies)
        client = ScriptedProviderClient(self._query_actions())
        replaced = json.loads(json.dumps(self.storage_state))
        replaced["cookies"][0]["value"] = "replaced-session"
        result, _browser = self._run(case=case, client=client, storage_state=replaced)
        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "credential_or_identity")
        self.assertEqual(result["error_code"], "session_changed")
        self.assertFalse(result["session_binding_unchanged"])
        self.assertEqual(result["bridge"]["success_claim"], True)
        self.assertEqual(result["evidence"]["execution_status"], "failed")

    def test_model_call_budget_is_an_engine_failure_not_an_outage(self):
        case = development_case("dev-query-en-1", self.companies)
        client = ScriptedProviderClient(self._query_actions())
        result, _browser = self._run(
            case=case, client=client, config=self._config(max_model_calls=1))
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["apparatus_code"])
        self.assertEqual(result["error_code"], "model_call_budget")
        self.assertEqual(result["provider"]["provider_attempts"], 1)
        self.assertEqual(result["provider"]["provider_responses"], 1)
        self.assertEqual(client.dispatched, 1)

    def test_cancellation_stops_hosts_and_never_reports_completion(self):
        case = development_case("dev-query-en-1", self.companies)
        cancelled = threading.Event()
        cancelled.set()
        client = ScriptedProviderClient(self._query_actions())
        result, browser = self._run(case=case, client=client, cancelled=cancelled)
        self.assertEqual((result["status"], result["error_code"]), ("failed", "cancelled"))
        self.assertTrue(result["cancelled"])
        self.assertEqual(browser.launches, 0)
        self.assertEqual(client.dispatched, 0)

        cancelled = threading.Event()
        client = ScriptedProviderClient(
            self._query_actions(),
            after_call=lambda sequence: cancelled.set() if sequence == 1 else None)
        result, _browser = self._run(case=case, client=client, cancelled=cancelled)
        self.assertEqual((result["status"], result["error_code"]), ("failed", "cancelled"))
        self.assertIsNone(result["apparatus_code"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(client.dispatched, 1)
        self.assertEqual(result["business"]["business_responses"], 0)

    def test_provider_outage_invalidates_the_trial_with_unknown_cost(self):
        case = development_case("dev-query-en-1", self.companies)
        client = ScriptedProviderClient(
            self._query_actions(), failures={1: "provider_transport"})
        result, _browser = self._run(case=case, client=client)
        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "provider_outage")
        self.assertEqual(result["error_code"], "provider_transport")
        self.assertEqual(result["provider"]["provider_attempts"], 1)
        self.assertEqual(result["provider"]["provider_responses"], 0)
        accounting = result["provider"]["accounting"]
        self.assertEqual(accounting["model_calls"], 1)
        self.assertEqual(accounting["usage_unavailable_calls"], 1)
        self.assertIsNone(accounting["total_cost_cny"])
        self.assertEqual(result["bridge"]["status"], "error")

    def test_business_tool_failure_is_a_transport_apparatus_fault(self):
        case = development_case("dev-query-en-1", self.companies)
        executor = SyntheticCompanyExecutor(self.companies, failure="synthetic API outage")
        adapter = SyntheticTwentyAdapter(
            origin=self.origin, runtime=synthetic_business_runtime(executor))
        client = ScriptedProviderClient(self._query_actions())
        result, _browser = self._run(case=case, client=client, adapter=adapter)
        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "transport_contract")
        self.assertEqual(result["error_code"], "business_tool")

    def test_bridge_exceptions_distinguish_a_replaced_document_from_apparatus(self):
        case = development_case("dev-query-en-1", self.companies)

        def exploding_bridge(_page, _bindings, _config):
            raise RuntimeError("Execution context was destroyed")

        client = ScriptedProviderClient(self._query_actions())
        result, _browser = self._run(
            case=case, client=client, bridge=exploding_bridge, bridge_installed=False)
        self.assertEqual((result["status"], result["error_code"]), ("failed", "document_replaced"))
        self.assertIsNone(result["apparatus_code"])
        result, _browser = self._run(
            case=case, client=client, bridge=exploding_bridge, bridge_installed=True)
        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual((result["apparatus_code"], result["error_code"]),
                         ("transport_contract", "bridge_execution"))

    def test_query_without_a_business_call_cannot_complete(self):
        case = development_case("dev-query-en-1", self.companies)
        client = ScriptedProviderClient(
            [{"done": {"text": query_answer(self.companies), "success": True}}])
        result, _browser = self._run(case=case, client=client)
        self.assertEqual((result["status"], result["error_code"]), ("failed", "metrics_contract"))


if __name__ == "__main__":
    unittest.main()
