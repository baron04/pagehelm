from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.pageagent.twenty_runner import (
    PageAgentTwentyConfig, PageAgentTwentyRunnerError, _bridge_reports_match,
    _execution_case, _provider_apparatus_code, open_evidence, query_evidence,
    run_case,
)
from web_operation_agent.application import APPLICATION_ADAPTER_VERSION, ApplicationDescriptor
from web_operation_agent.business import BusinessSkill, BusinessToolResult, BusinessToolRuntime
from web_operation_agent.identity import PRINCIPAL_CONTEXT_VERSION, PrincipalContext
from web_operation_agent.model import ChatModel
from web_operation_agent.twenty import TwentyApplicationAdapter


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


class EvidenceContractTests(unittest.TestCase):
    def test_query_answer_is_used_as_written_and_domains_are_normalized(self):
        answer = json.dumps({
            "artifact_version": 1,
            "execution_status": "completed",
            "complete": True,
            "companies": [
                {"name": "Acme", "domain": "https://Acme.Example/"},
                {"name": "Beta", "domain": "beta.example"},
            ],
        })
        evidence = query_evidence(answer)
        self.assertEqual(evidence["companies"][0], {
            "name": "Acme", "domain": "acme.example"})
        self.assertTrue(evidence["complete"])

    def test_query_answer_rejects_repairs_markdown_and_duplicate_rows(self):
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "strict JSON"):
            query_evidence('```json\n{"artifact_version":1}\n```')
        duplicate = json.dumps({
            "artifact_version": 1, "execution_status": "completed", "complete": True,
            "companies": [
                {"name": "Acme", "domain": "acme.example"},
                {"name": "ACME", "domain": "ACME.EXAMPLE"},
            ],
        })
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "duplicate"):
            query_evidence(duplicate)

    def test_open_evidence_needs_one_disclosed_route_and_visible_facts(self):
        results = [{"companies": [{
            "name": "Acme", "domain": "https://acme.example",
            "ui_path": "/object/company/11111111-1111-4111-8111-111111111111",
        }]}]
        evidence = open_evidence(
            path="/object/company/11111111-1111-4111-8111-111111111111",
            visible_projection="Company\nAcme\nDomain\nacme.example",
            business_results=results)
        self.assertEqual(evidence["page"], {
            "path": "/object/company/11111111-1111-4111-8111-111111111111",
            "name": "Acme", "domain": "acme.example"})

        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "not visible"):
            open_evidence(
                path=evidence["page"]["path"], visible_projection="Loading…",
                business_results=results)
        with self.assertRaisesRegex(PageAgentTwentyRunnerError, "disclosed"):
            open_evidence(
                path="/object/company/22222222-2222-4222-8222-222222222222",
                visible_projection="Acme acme.example", business_results=results)

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

    def test_provider_budget_and_deadline_are_product_failures_not_outages(self):
        self.assertIsNone(_provider_apparatus_code("model_call_budget"))
        self.assertIsNone(_provider_apparatus_code("provider_deadline"))
        self.assertEqual(_provider_apparatus_code("provider_transport"), "provider_outage")
        self.assertEqual(_provider_apparatus_code("ledger_failure"), "ledger_failure")

    def test_runner_binds_provider_business_identity_and_public_answer(self):
        class Executor:
            @staticmethod
            def tool_schemas():
                return ({"type": "function", "function": {
                    "name": "query_companies", "description": "Read companies.",
                    "parameters": {"type": "object", "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                        "additionalProperties": False}}},)

            @staticmethod
            def tool_effects():
                return {"query_companies": "read"}

            @staticmethod
            def execute(name, args, *, context):
                if name != "query_companies" or args != {"limit": 50}:
                    raise AssertionError("unexpected business call")
                return BusinessToolResult(
                    content={"companies": [{
                        "name": "Acme", "domain": "acme.example",
                        "ui_path": "/object/company/11111111-1111-4111-8111-111111111111",
                    }], "completeness": {
                        "source_complete": True, "result_truncated": False}},
                    audit={"returned_count": 1},
                    allowed_routes=(
                        "/object/company/11111111-1111-4111-8111-111111111111",))

        skill = BusinessSkill.from_bytes(json.dumps({
            "schema_version": 1, "skill_id": "twenty.test.query",
            "title": "Query companies", "intent": "Read current companies.",
            "triggers": ["company"], "instructions": ["Call query_companies."],
            "tool_names": ["query_companies"], "provenance": ["test:fixture"],
            "read_only": True,
        }).encode(), artifact_uri="memory://query.json")
        runtime = BusinessToolRuntime(
            (skill,), Executor(), application_id="twenty",
            required_scopes={"query_companies": "companies:read"})

        class Adapter(TwentyApplicationAdapter):
            def __init__(self):
                self.business_tools = runtime
                self.descriptor = ApplicationDescriptor(
                    schema_version=APPLICATION_ADAPTER_VERSION,
                    application_id="twenty", display_name="Twenty test",
                    origin="http://127.0.0.1:3020", navigation_mode="spa",
                    page_controller_module="http://127.0.0.1:3020/woa/controller.js",
                    catalog_sha256=runtime.catalog_sha256)

            @staticmethod
            def page_session_binding(_state, *, now_ms=None):
                return "same-binding"

            @staticmethod
            def bind_browser_session(_state, *, delegation_id, lifetime_s, now_ms):
                return PrincipalContext(
                    schema_version=PRINCIPAL_CONTEXT_VERSION,
                    application_id="twenty", tenant_id="tenant", workspace_id="workspace",
                    actor_id="actor", delegation_id=delegation_id, issued_at=now_ms,
                    expires_at=now_ms + lifetime_s * 1_000,
                    scopes=("companies:read",))

        class Meter:
            calls = 0

            def run_report(self, _run_id):
                return {
                    "model_calls": self.calls, "usage_unavailable_calls": 0,
                    "provider_reported_models": ["fixture-model"] if self.calls else [],
                    "provider_request_ids_digest": "sha256:" + "c" * 64 if self.calls else None,
                    "known_input_tokens": 10 * self.calls,
                    "known_output_tokens": 2 * self.calls, "known_cached_tokens": 0,
                    "known_reasoning_tokens": 0, "known_cost_cny": 0.0,
                    "total_cost_cny": 0.0,
                    "spend_ledger_slice_digest": "sha256:" + "d" * 64 if self.calls else None,
                }

        class Client(ChatModel):
            model = "fixture-model"

            def __init__(self):
                self.meter = Meter()

            def request_completion(self, body, _run_id, *, timeout_s):
                self.meter.calls += 1
                return {"model": self.model, "choices": [], "usage": {
                    "prompt_tokens": 10, "completion_tokens": 2}}

        class Locator:
            @staticmethod
            def wait_for(**_kwargs):
                return None

        class Page:
            def __init__(self):
                self.url = ""
                self.bindings = {}

            def on(self, *_args):
                return None

            def expose_function(self, name, callback):
                self.bindings[name] = callback

            def goto(self, url, **_kwargs):
                self.url = url

            @staticmethod
            def locator(_selector):
                return Locator()

            @staticmethod
            def add_script_tag(**_kwargs):
                return None

            def evaluate(self, script, config=None):
                if config is None:
                    return "unused"
                token = config["run_token"]
                body = {
                    "model": config["model"], "temperature": 0.0, "top_p": 1.0,
                    "max_completion_tokens": 2_048, "parallel_tool_calls": False,
                    "messages": [{"role": "user", "content": config["prompt"]}],
                }
                self.bindings["__WOA_PAGEAGENT_PROVIDER_FETCH__"]({
                    "run_token": token,
                    "url": "https://provider.invalid/v1/chat/completions",
                    "method": "POST", "body": json.dumps(body)})
                self.bindings["__WOA_PAGEAGENT_BUSINESS_TOOL__"]({
                    "run_token": token, "name": "query_companies", "args": {"limit": 50}})
                answer = json.dumps({
                    "artifact_version": 1, "execution_status": "completed",
                    "complete": True,
                    "companies": [{"name": "Acme", "domain": "acme.example"}],
                })
                return {
                    "bridge_version": 2, "upstream_version": "1.12.4",
                    "status": "completed", "success_claim": True,
                    "terminal_reason": None, "answer": answer,
                    "metrics": {"provider_calls": 1, "page_actions": 0,
                                "business_calls": 1, "retries": 0, "steps": 2},
                }

        page = Page()

        class BrowserContext:
            @staticmethod
            def new_page():
                return page

            @staticmethod
            def storage_state():
                return {"cookies": []}

            @staticmethod
            def close():
                return None

        class Browser:
            @staticmethod
            def new_context(**_kwargs):
                return BrowserContext()

            @staticmethod
            def close():
                return None

        class Chromium:
            @staticmethod
            def launch(**_kwargs):
                return Browser()

        class Playwright:
            chromium = Chromium()

        class Manager:
            @staticmethod
            def __enter__():
                return Playwright()

            @staticmethod
            def __exit__(*_args):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle.js"
            state = root / "state.json"
            bundle.write_text("bundle", encoding="utf-8")
            state.write_text("{}", encoding="utf-8")
            config = PageAgentTwentyConfig(
                bundle_path=bundle,
                bundle_digest="sha256:" + hashlib.sha256(b"bundle").hexdigest(),
                storage_state_path=state)
            with patch("playwright.sync_api.sync_playwright", return_value=Manager()):
                result = run_case(
                    case=_case(), adapter=Adapter(), client=Client(), config=config,
                    run_id="pageagent-test-run")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["session_binding_unchanged"])
        self.assertEqual(result["evidence"]["companies"], [
            {"name": "Acme", "domain": "acme.example"}])
        self.assertEqual(result["provider"]["provider_attempts"], 1)
        self.assertEqual(result["business"]["business_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
