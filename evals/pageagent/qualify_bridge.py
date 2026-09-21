"""Qualify the pinned PageAgent bridge in real Chromium without a model call."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from evals.pageagent.business_host import PageAgentBusinessProxy
from evals.pageagent.host import PageAgentProviderContract, PageAgentProviderProxy
from web_operation_agent.business.runtime import (BusinessSkill, BusinessToolResult,
                                          BusinessToolRuntime)
from web_operation_agent.state.identity import OperationContext, PrincipalContext


def atomic_write_json(path: Path, value: Any) -> None:
    """Write a private JSON artifact completely before publishing its final name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _completion(action: dict[str, Any], *, sequence: int) -> str:
    arguments = {
        "evaluation_previous_goal": "The prior state was inspected.",
        "memory": "Use only the current synthetic page.",
        "next_goal": "Execute the scripted qualification action.",
        "action": action,
    }
    return json.dumps({
        "id": f"qualification-{sequence}",
        "object": "chat.completion",
        "created": 1_788_912_000 + sequence,
        "model": "pageagent-qualification-model",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call-{sequence}",
                    "type": "function",
                    "function": {
                        "name": "AgentOutput",
                        "arguments": json.dumps(arguments),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    })


def _config(*, prompt: str, capability: str, tools: list[dict[str, Any]],
            instructions: str = "Operate only on the synthetic qualification fixture.") -> dict[str, Any]:
    return {
        "prompt": prompt,
        "model": "pageagent-qualification-model",
        "language": "en-US",
        "max_steps": 4,
        "max_page_actions": 2,
        "wall_ms": 10_000,
        "navigation_mode": "spa",
        "capability_profile": capability,
        "system_prompt": "",
        "instructions": instructions,
        "tools": tools,
        "run_token": "qualification-run-token",
        "request_options": {
            "temperature": 0,
            "top_p": 1,
            "max_completion_tokens": 512,
            "parallel_tool_calls": False,
        },
    }


class _ScriptedMeter:
    def __init__(self, client: "_ScriptedClient"):
        self.client = client

    def run_report(self, _run_id: str) -> dict[str, Any]:
        used = self.client.responses[:len(self.client.requests)]
        usages = [item["usage"] for item in used]
        request_digests = [hashlib.sha256(json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest() for item in self.client.requests]
        evidence_digest = (
            "sha256:" + hashlib.sha256(json.dumps(
                request_digests, separators=(",", ":")).encode("utf-8")).hexdigest()
            if request_digests else None)
        return {
            "model_calls": len(used),
            "usage_unavailable_calls": 0,
            "provider_reported_models": sorted({item["model"] for item in used}),
            "provider_request_ids_digest": evidence_digest,
            "known_input_tokens": sum(item["prompt_tokens"] for item in usages),
            "known_output_tokens": sum(item["completion_tokens"] for item in usages),
            "known_cached_tokens": sum(
                item.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                for item in usages),
            "known_reasoning_tokens": sum(
                item.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
                for item in usages),
            "known_cost_cny": 0.0,
            "total_cost_cny": 0.0,
            "spend_ledger_slice_digest": evidence_digest,
        }


class _ScriptedClient:
    model = "pageagent-qualification-model"

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        self.meter = _ScriptedMeter(self)

    def request_completion(self, body: dict[str, Any], _run_id: str,
                           *, timeout_s: float) -> dict[str, Any]:
        if not 0 < timeout_s <= 10:
            raise ValueError("qualification provider deadline is invalid")
        encoded_body = json.dumps(body, ensure_ascii=False, sort_keys=True)
        self.requests.append({
            "model": body["model"],
            "tool_names": sorted(tool["function"]["name"] for tool in body["tools"]),
            "contains_ask_user": "ask_user" in encoded_body,
            "contains_execute_javascript": "execute_javascript" in encoded_body,
            "contains_query_companies": "query_companies" in encoded_body,
            "contains_open_allowed_route": "open_allowed_route" in encoded_body,
        })
        if len(self.requests) > len(self.responses):
            raise ValueError("PageAgent made an unplanned provider call")
        return self.responses[len(self.requests) - 1]


class _QualificationExecutor:
    def __init__(self, calls: list[dict[str, Any]]):
        self.calls = calls

    @staticmethod
    def tool_schemas() -> tuple[dict[str, Any], ...]:
        return ({
            "type": "function",
            "function": {
                "name": "query_companies",
                "description": "Return current synthetic companies.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name_contains": {
                            "type": "string", "description": "Optional company filter."},
                        "limit": {
                            "type": "integer", "minimum": 1, "maximum": 50,
                            "description": "Maximum result count."},
                    },
                    "additionalProperties": False,
                },
            },
        },)

    @staticmethod
    def tool_effects() -> dict[str, str]:
        return {"query_companies": "read"}

    def execute(self, name: str, args: dict[str, Any], *,
                context: OperationContext) -> BusinessToolResult:
        self.calls.append({
            "name": name,
            "args": args,
            "operation_context_digest": context.binding_digest,
        })
        return BusinessToolResult(
            content={
                "complete": True,
                "companies": [{
                    "name": "Synthetic Acme", "domain": "acme.example.com",
                    "ui_path": "/object/company/synthetic-acme",
                }],
            },
            audit={"returned_count": 1},
            allowed_routes=("/object/company/synthetic-acme",),
        )


def _business_proxy(calls: list[dict[str, Any]]) -> PageAgentBusinessProxy:
    skill = BusinessSkill.from_bytes(json.dumps({
        "schema_version": 1,
        "skill_id": "fixture.companies-query",
        "title": "Query synthetic companies",
        "intent": "Read current synthetic company data.",
        "triggers": ["companies"],
        "instructions": ["Call query_companies and use its current result."],
        "tool_names": ["query_companies"],
        "provenance": ["src:fixture@v1:companies.json#companies"],
        "read_only": True,
    }).encode("utf-8"), artifact_uri="memory://fixture/companies-query.json")
    runtime = BusinessToolRuntime(
        (skill,), _QualificationExecutor(calls), application_id="fixture",
        required_scopes={"query_companies": "companies:read"})
    now = int(time.time() * 1000)
    principal = PrincipalContext(
        schema_version=1, application_id="fixture", tenant_id="synthetic-tenant",
        workspace_id="synthetic-workspace", actor_id="synthetic-actor",
        delegation_id="synthetic-delegation", issued_at=now,
        expires_at=now + 30_000, scopes=("companies:read",))
    context = OperationContext.create(
        principal, session_id="qualification-session", turn_id="qualification-turn",
        operation_id="qualification-operation", deadline_ms=now + 10_000,
        now_ms=now)
    return PageAgentBusinessProxy(
        run_token="qualification-run-token", runtime=runtime,
        loaded_skill_ids=(skill.skill_id,), context=context,
        expected_catalog_sha256=runtime.catalog_sha256, max_business_calls=1,
        max_route_calls=1)


def _prepare_page(browser, bundle: Path, provider_actions: list[dict[str, Any]],
                  business_calls: list[dict[str, Any]]) -> tuple[
                      Page, list[dict[str, Any]], PageAgentProviderProxy,
                      PageAgentBusinessProxy]:
    page = browser.new_page()
    responses = [
        json.loads(_completion(action, sequence=index + 1))
        for index, action in enumerate(provider_actions)
    ]
    client = _ScriptedClient(responses)
    proxy = PageAgentProviderProxy(
        run_token="qualification-run-token", run_id="qualification-run",
        client=client,
        contract=PageAgentProviderContract(
            model=client.model, temperature=0, top_p=1,
            max_completion_tokens=512, max_model_calls=len(responses), wall_time_s=10),
    )
    business_proxy = _business_proxy(business_calls)

    page.expose_function("__WOA_PAGEAGENT_PROVIDER_FETCH__", proxy.handle)
    page.expose_function("__WOA_PAGEAGENT_BUSINESS_TOOL__", business_proxy.handle)
    page.expose_function("__WOA_PAGEAGENT_OPEN_ROUTE__", business_proxy.handle_route)
    document = """
<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>PageAgent qualification</title></head>
  <body>
    <main><button id="target" type="button">Not clicked</button><output id="state">idle</output></main>
    <script>document.querySelector('#target').addEventListener('click', () => { document.querySelector('#state').textContent = 'clicked' })</script>
  </body>
</html>
"""
    page.route("https://fixture.example/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=document))
    page.goto("https://fixture.example/start")
    page.add_script_tag(path=str(bundle))
    return page, client.requests, proxy, business_proxy


def qualify(bundle: Path) -> dict[str, Any]:
    raw_bundle = bundle.read_bytes()
    if not raw_bundle or len(raw_bundle) > 2 * 1024 * 1024:
        raise ValueError("PageAgent baseline bundle is missing or oversized")
    business_calls: list[dict[str, Any]] = []
    route_business_calls: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            dom_page, dom_requests, dom_proxy, dom_business_proxy = _prepare_page(
                browser, bundle,
                [{"click_element_by_index": {"index": 0}},
                 {"done": {"text": "Clicked the target.", "success": True}}],
                business_calls,
            )
            dom_result = dom_page.evaluate(
                "config => globalThis.__WOA_PAGEAGENT_BASELINE__.execute(config)",
                _config(prompt="Click the only button.", capability="browser_only", tools=[]),
            )
            dom_host = dom_proxy.report()
            dom_state = dom_page.locator("#state").text_content()
            dom_page.close()

            tool_page, tool_requests, tool_proxy, tool_business_proxy = _prepare_page(
                browser, bundle,
                [{"query_companies": {"name_contains": "Synthetic", "limit": 20}},
                 {"done": {"text": "Synthetic Acme — acme.example.com", "success": True}}],
                business_calls,
            )
            tool_result = tool_page.evaluate(
                "config => globalThis.__WOA_PAGEAGENT_BASELINE__.execute(config)",
                _config(
                    prompt="List the companies.", capability="skill_api",
                    tools=list(tool_business_proxy.tool_specs),
                    instructions=tool_business_proxy.instructions),
            )
            tool_host = tool_proxy.report()
            tool_business_host = tool_business_proxy.report()
            tool_page.close()

            route_page, route_requests, route_proxy, route_business_proxy = _prepare_page(
                browser, bundle,
                [{"query_companies": {"name_contains": "Synthetic", "limit": 20}},
                 {"open_allowed_route": {"route": "/object/company/synthetic-acme"}},
                 {"done": {"text": "Opened Synthetic Acme.", "success": True}}],
                route_business_calls,
            )
            route_result = route_page.evaluate(
                "config => globalThis.__WOA_PAGEAGENT_BASELINE__.execute(config)",
                _config(
                    prompt="Open Synthetic Acme.", capability="skill_api",
                    tools=list(route_business_proxy.tool_specs),
                    instructions=route_business_proxy.instructions),
            )
            route_host = route_proxy.report()
            route_business_host = route_business_proxy.report()
            route_path = route_page.evaluate("location.pathname")
            route_page.close()
            browser_version = browser.version
        finally:
            browser.close()
    expected_business = {
        "name": "query_companies",
        "args": {"name_contains": "Synthetic", "limit": 20},
    }
    checks = {
        "dom_action_changed_page": dom_state == "clicked",
        "dom_result_succeeded": dom_result.get("success_claim") is True,
        "dom_metrics_exact": dom_result.get("metrics") == {
            "provider_calls": 2, "page_actions": 1, "business_calls": 0,
            "retries": 0, "steps": 2,
        },
        "dom_provider_calls_exact": len(dom_requests) == 2,
        "dom_host_accounting_exact": (
            dom_host["provider_attempts"] == 2
            and dom_host["provider_responses"] == 2
            and dom_host["accounting"]["model_calls"] == 2),
        "dom_tool_boundary_exact": all(
            not request["contains_ask_user"]
            and not request["contains_execute_javascript"]
            and not request["contains_query_companies"]
            and not request["contains_open_allowed_route"]
            for request in dom_requests),
        "browser_only_business_unavailable": dom_business_proxy.report()["business_attempts"] == 0,
        "business_arguments_exact": (
            len(business_calls) == 1
            and {key: business_calls[0][key] for key in expected_business} == expected_business
            and len(business_calls[0]["operation_context_digest"]) == 64),
        "business_host_exact": (
            tool_business_host["business_attempts"] == 1
            and tool_business_host["business_responses"] == 1
            and tool_business_host["route_attempts"] == 0
            and tool_business_host["route_permits"] == 0
            and tool_business_host["allowed_route_count"] == 1
            and tool_business_host["last_error_code"] is None),
        "business_result_succeeded": tool_result.get("success_claim") is True,
        "business_metrics_exact": tool_result.get("metrics") == {
            "provider_calls": 2, "page_actions": 0, "business_calls": 1,
            "retries": 0, "steps": 2,
        },
        "business_provider_calls_exact": len(tool_requests) == 2,
        "business_host_accounting_exact": (
            tool_host["provider_attempts"] == 2
            and tool_host["provider_responses"] == 2
            and tool_host["accounting"]["model_calls"] == 2),
        "business_tool_boundary_exact": all(
            not request["contains_ask_user"]
            and not request["contains_execute_javascript"]
            and request["contains_query_companies"]
            and request["contains_open_allowed_route"]
            for request in tool_requests),
        "route_arguments_exact": (
            len(route_business_calls) == 1
            and {key: route_business_calls[0][key] for key in expected_business}
            == expected_business),
        "route_changed_spa_path": route_path == "/object/company/synthetic-acme",
        "route_result_succeeded": route_result.get("success_claim") is True,
        "route_metrics_exact": route_result.get("metrics") == {
            "provider_calls": 3, "page_actions": 1, "business_calls": 1,
            "retries": 0, "steps": 3,
        },
        "route_host_accounting_exact": (
            len(route_requests) == 3
            and route_host["provider_attempts"] == 3
            and route_host["provider_responses"] == 3
            and route_host["accounting"]["model_calls"] == 3),
        "route_host_permit_exact": (
            route_business_host["business_attempts"] == 1
            and route_business_host["business_responses"] == 1
            and route_business_host["route_attempts"] == 1
            and route_business_host["route_permits"] == 1
            and route_business_host["allowed_route_count"] == 1
            and route_business_host["last_error_code"] is None),
    }
    if not all(checks.values()):
        raise ValueError("PageAgent bridge qualification checks did not all pass")
    return {
        "artifact_version": 1,
        "kind": "pageagent_bridge_qualification",
        "status": "READY",
        "upstream_release": "pageagent@1.12.4",
        "bundle_digest": "sha256:" + hashlib.sha256(raw_bundle).hexdigest(),
        "browser_release": f"chromium@{browser_version}",
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = qualify(Path(args.bundle))
    if args.output:
        atomic_write_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
