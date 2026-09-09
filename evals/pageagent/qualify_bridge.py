"""Qualify the pinned PageAgent bridge in real Chromium without a model call."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from evals.twenty.fixture import atomic_write_json


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


def _config(*, prompt: str, capability: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "prompt": prompt,
        "model": "pageagent-qualification-model",
        "language": "en-US",
        "max_steps": 4,
        "wall_ms": 10_000,
        "capability_profile": capability,
        "instructions": "Operate only on the synthetic qualification fixture.",
        "tools": tools,
        "run_token": "qualification-run-token",
        "request_options": {
            "temperature": 0,
            "top_p": 1,
            "max_completion_tokens": 512,
            "parallel_tool_calls": False,
        },
    }


def _prepare_page(browser, bundle: Path, provider_actions: list[dict[str, Any]],
                  business_calls: list[dict[str, Any]]) -> tuple[Page, list[dict[str, Any]]]:
    page = browser.new_page()
    requests: list[dict[str, Any]] = []
    responses = [
        _completion(action, sequence=index + 1)
        for index, action in enumerate(provider_actions)
    ]

    def provider(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"run_token", "url", "method", "body"}:
            raise ValueError("provider bridge payload is invalid")
        body = json.loads(payload["body"])
        if (payload["run_token"] != "qualification-run-token"
                or payload["method"] != "POST"
                or not payload["url"].startswith("https://provider.invalid/")
                or body.get("model") != "pageagent-qualification-model"
                or body.get("parallel_tool_calls") is not False
                or body.get("temperature") != 0
                or body.get("top_p") != 1
                or body.get("max_completion_tokens") != 512):
            raise ValueError("provider bridge changed the frozen request contract")
        encoded_body = json.dumps(body, ensure_ascii=False, sort_keys=True)
        requests.append({
            "model": body["model"],
            "tool_names": sorted(tool["function"]["name"] for tool in body["tools"]),
            "contains_ask_user": "ask_user" in encoded_body,
            "contains_execute_javascript": "execute_javascript" in encoded_body,
            "contains_query_companies": "query_companies" in encoded_body,
        })
        if len(requests) > len(responses):
            raise ValueError("PageAgent made an unplanned provider call")
        return {"status": 200, "body": responses[len(requests) - 1]}

    def business(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"run_token", "name", "args"}:
            raise ValueError("business bridge payload is invalid")
        business_calls.append(payload)
        return {"complete": True, "companies": [{"name": "Synthetic Acme", "domain": "acme.example.com"}]}

    page.expose_function("__WOA_PAGEAGENT_PROVIDER_FETCH__", provider)
    page.expose_function("__WOA_PAGEAGENT_BUSINESS_TOOL__", business)
    page.set_content("""
<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>PageAgent qualification</title></head>
  <body>
    <main><button id="target" type="button">Not clicked</button><output id="state">idle</output></main>
    <script>document.querySelector('#target').addEventListener('click', () => { document.querySelector('#state').textContent = 'clicked' })</script>
  </body>
</html>
""")
    page.add_script_tag(path=str(bundle))
    return page, requests


def qualify(bundle: Path) -> dict[str, Any]:
    raw_bundle = bundle.read_bytes()
    if not raw_bundle or len(raw_bundle) > 2 * 1024 * 1024:
        raise ValueError("PageAgent baseline bundle is missing or oversized")
    business_calls: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            dom_page, dom_requests = _prepare_page(
                browser, bundle,
                [{"click_element_by_index": {"index": 0}},
                 {"done": {"text": "Clicked the target.", "success": True}}],
                business_calls,
            )
            dom_result = dom_page.evaluate(
                "config => globalThis.__WOA_PAGEAGENT_BASELINE__.execute(config)",
                _config(prompt="Click the only button.", capability="browser_only", tools=[]),
            )
            dom_state = dom_page.locator("#state").text_content()
            dom_page.close()

            tool_schema = {
                "name": "query_companies",
                "description": "Return current synthetic companies.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name_contains": {"type": "string", "description": "Optional company filter."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                                  "description": "Maximum result count."},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            }
            tool_page, tool_requests = _prepare_page(
                browser, bundle,
                [{"query_companies": {"name_contains": "Synthetic", "limit": 20}},
                 {"done": {"text": "Synthetic Acme — acme.example.com", "success": True}}],
                business_calls,
            )
            tool_result = tool_page.evaluate(
                "config => globalThis.__WOA_PAGEAGENT_BASELINE__.execute(config)",
                _config(prompt="List the companies.", capability="skill_api", tools=[tool_schema]),
            )
            tool_page.close()
            browser_version = browser.version
        finally:
            browser.close()
    expected_business = [{
        "run_token": "qualification-run-token",
        "name": "query_companies",
        "args": {"name_contains": "Synthetic", "limit": 20},
    }]
    checks = {
        "dom_action_changed_page": dom_state == "clicked",
        "dom_result_succeeded": dom_result.get("success_claim") is True,
        "dom_metrics_exact": dom_result.get("metrics") == {
            "provider_calls": 2, "page_actions": 1, "business_calls": 0,
            "retries": 0, "steps": 2,
        },
        "dom_provider_calls_exact": len(dom_requests) == 2,
        "dom_tool_boundary_exact": all(
            not request["contains_ask_user"]
            and not request["contains_execute_javascript"]
            and not request["contains_query_companies"]
            for request in dom_requests),
        "business_arguments_exact": business_calls == expected_business,
        "business_result_succeeded": tool_result.get("success_claim") is True,
        "business_metrics_exact": tool_result.get("metrics") == {
            "provider_calls": 2, "page_actions": 0, "business_calls": 1,
            "retries": 0, "steps": 2,
        },
        "business_provider_calls_exact": len(tool_requests) == 2,
        "business_tool_boundary_exact": all(
            not request["contains_ask_user"]
            and not request["contains_execute_javascript"]
            and request["contains_query_companies"]
            for request in tool_requests),
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
