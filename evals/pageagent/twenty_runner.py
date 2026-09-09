"""One-process PageAgentCore runner for read-only Twenty evaluation cases.

The runner receives a public execution case and ordinary application capabilities. It never
reads the database baseline, after-state, evaluator result, held-out task set, or another run.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from evals.pageagent.business_host import PageAgentBusinessProxy
from evals.pageagent.host import PageAgentProviderContract, PageAgentProviderProxy
from web_operation_agent.application import bind_application
from web_operation_agent.identity import OperationContext
from web_operation_agent.model import ChatModel, SpendMeter, load_env
from web_operation_agent.twenty import TwentyApplicationAdapter


RUNNER_VERSION = 1
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{2,159}\Z")
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.I)
_MAX_BUNDLE_BYTES = 2 * 1024 * 1024
_MAX_VISIBLE_BYTES = 512 * 1024
_QUERY_OUTPUT_CONTRACT = """EVALUATION OUTPUT CONTRACT
For a company-list goal, call query_companies using a limit large enough for the complete current workspace. Then call done with text containing exactly one JSON object and no Markdown: {"artifact_version":1,"execution_status":"completed","complete":true,"companies":[{"name":"Company","domain":"company.example"}]}. Set complete to true only when the tool's completeness.source_complete is true and result_truncated is false. Copy every returned company exactly once. For an open goal, use open_allowed_route with the exact ui_path returned by query_companies; this is the adapter equivalent of the Skill's generic goto instruction. Finish only after re-observing the requested company name and domain on the opened page."""


class PageAgentTwentyRunnerError(ValueError):
    """The runner input or deterministic output contract is invalid."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise PageAgentTwentyRunnerError("artifact must be finite JSON") from exc


def _digest_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value)):
        raise PageAgentTwentyRunnerError(f"{name} must be a bounded display string")
    return value.strip()


def _domain(value: Any) -> str:
    if value in (None, ""):
        return ""
    selected = _bounded_text(value, "company domain", 300).casefold()
    selected = re.sub(r"^https?://", "", selected).rstrip("/")
    if not _DOMAIN.fullmatch(selected):
        raise PageAgentTwentyRunnerError("company domain is invalid")
    return selected


def _execution_case(value: Any) -> dict[str, Any]:
    fields = {
        "artifact_version", "kind", "set_id", "split", "task_id",
        "template_family_digest", "task_digest", "prompt", "evaluator_task",
    }
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("artifact_version") != 1
            or value.get("kind") != "twenty_execution_case"):
        raise PageAgentTwentyRunnerError("execution case header is invalid")
    for name in ("template_family_digest", "task_digest"):
        if not isinstance(value.get(name), str) or not _DIGEST.fullmatch(value[name]):
            raise PageAgentTwentyRunnerError(f"execution case {name} is invalid")
    for name in ("set_id", "task_id"):
        if not isinstance(value.get(name), str) or not _ID.fullmatch(value[name]):
            raise PageAgentTwentyRunnerError(f"execution case {name} is invalid")
    if value.get("split") not in {"development", "test"}:
        raise PageAgentTwentyRunnerError("execution case split is invalid")
    prompt = _bounded_text(value.get("prompt"), "execution case prompt", 20_000)
    evaluator_task = value.get("evaluator_task")
    if not isinstance(evaluator_task, dict) or evaluator_task.get("artifact_version") != 1:
        raise PageAgentTwentyRunnerError("execution case evaluator task is invalid")
    task_kind = evaluator_task.get("kind")
    if task_kind not in {"query", "open"}:
        raise PageAgentTwentyRunnerError("PageAgent Twenty runner supports query and open only")
    expected = {"artifact_version", "kind"} if task_kind == "query" else {
        "artifact_version", "kind", "company_id"}
    if set(evaluator_task) != expected:
        raise PageAgentTwentyRunnerError("execution case evaluator task has unexpected fields")
    return {**value, "prompt": prompt, "task_kind": task_kind}


def query_evidence(answer: Any) -> dict[str, Any]:
    """Parse the user-visible PageAgent answer without repairing or completing it."""
    answer = _bounded_text(answer, "PageAgent answer", 20_000)
    try:
        value = json.loads(answer)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise PageAgentTwentyRunnerError("query answer is not one strict JSON object") from exc
    fields = {"artifact_version", "execution_status", "complete", "companies"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("artifact_version") != 1
            or value.get("execution_status") != "completed"
            or not isinstance(value.get("complete"), bool)
            or not isinstance(value.get("companies"), list)
            or len(value["companies"]) > 1_000):
        raise PageAgentTwentyRunnerError("query answer does not satisfy the evidence schema")
    companies = []
    for row in value["companies"]:
        if not isinstance(row, dict) or set(row) != {"name", "domain"}:
            raise PageAgentTwentyRunnerError("query answer company has unexpected fields")
        companies.append({
            "name": _bounded_text(row.get("name"), "company name", 300),
            "domain": _domain(row.get("domain")),
        })
    identities = [(row["name"].casefold(), row["domain"]) for row in companies]
    if len(identities) != len(set(identities)):
        raise PageAgentTwentyRunnerError("query answer contains duplicate companies")
    return {**value, "companies": companies}


def _route_candidates(results: list[dict[str, Any]], path: str) -> list[dict[str, str]]:
    candidates = []
    for result in results:
        rows = result.get("companies") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("ui_path") == path:
                candidates.append({
                    "name": _bounded_text(row.get("name"), "company name", 300),
                    "domain": _domain(row.get("domain")),
                })
    unique = {(row["name"], row["domain"]): row for row in candidates}
    return list(unique.values())


def open_evidence(*, path: Any, visible_projection: Any,
                  business_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Require route facts from the tool result and their presence in the rendered page."""
    path = _bounded_text(path, "final page path", 1_000)
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise PageAgentTwentyRunnerError("final page path is not a canonical record path")
    visible = _bounded_text(visible_projection, "visible page projection", _MAX_VISIBLE_BYTES)
    candidates = _route_candidates(business_results, path)
    if len(candidates) != 1:
        raise PageAgentTwentyRunnerError(
            "final route does not identify one company disclosed by a business result")
    company = candidates[0]
    folded = visible.casefold()
    if company["name"].casefold() not in folded:
        raise PageAgentTwentyRunnerError("opened company name is not visible")
    if company["domain"] and company["domain"] not in folded:
        raise PageAgentTwentyRunnerError("opened company domain is not visible")
    return {
        "artifact_version": 1,
        "execution_status": "completed",
        "page": {"path": path, **company},
    }


@dataclass(frozen=True, slots=True)
class PageAgentTwentyConfig:
    bundle_path: Path
    bundle_digest: str
    storage_state_path: Path
    entry_path: str = "/objects/companies"
    language: str = "en-US"
    max_steps: int = 10
    max_model_calls: int = 10
    max_page_actions: int = 10
    max_business_calls: int = 4
    wall_time_s: int = 240
    max_completion_tokens: int = 2_048
    temperature: float = 0.0
    top_p: float = 1.0
    browser_executable: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "bundle_path", Path(self.bundle_path))
        object.__setattr__(self, "storage_state_path", Path(self.storage_state_path))
        if self.browser_executable is not None:
            object.__setattr__(self, "browser_executable", Path(self.browser_executable))
        if not _DIGEST.fullmatch(self.bundle_digest):
            raise PageAgentTwentyRunnerError("bundle_digest is invalid")
        parsed_entry = urlsplit(self.entry_path)
        if (not self.entry_path.startswith("/") or self.entry_path.startswith("//")
                or parsed_entry.scheme or parsed_entry.netloc or parsed_entry.query
                or parsed_entry.fragment or "\\" in self.entry_path or "%" in parsed_entry.path
                or any(part == ".." for part in parsed_entry.path.split("/"))
                or len(self.entry_path) > 1_000
                or re.search(r"[\x00-\x20\x7f]", self.entry_path)):
            raise PageAgentTwentyRunnerError("entry_path must be a relative application path")
        if self.language not in {"en-US", "zh-CN"}:
            raise PageAgentTwentyRunnerError("language is unsupported")
        for name, lower, upper in (
                ("max_steps", 1, 100), ("max_model_calls", 1, 100),
                ("max_page_actions", 0, 200), ("max_business_calls", 1, 200),
                ("wall_time_s", 1, 3_600),
                ("max_completion_tokens", 1, 131_072)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise PageAgentTwentyRunnerError(f"{name} is invalid")
        for name, lower, upper in (("temperature", 0, 2), ("top_p", 0, 1)):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not lower <= value <= upper):
                raise PageAgentTwentyRunnerError(f"{name} is invalid")

    def verify_files(self) -> bytes:
        if not self.storage_state_path.is_file():
            raise PageAgentTwentyRunnerError("browser storage state is unavailable")
        try:
            bundle = self.bundle_path.read_bytes()
        except OSError as exc:
            raise PageAgentTwentyRunnerError("PageAgent bundle is unavailable") from exc
        if not bundle or len(bundle) > _MAX_BUNDLE_BYTES:
            raise PageAgentTwentyRunnerError("PageAgent bundle is empty or oversized")
        if "sha256:" + hashlib.sha256(bundle).hexdigest() != self.bundle_digest:
            raise PageAgentTwentyRunnerError("PageAgent bundle digest mismatch")
        return bundle


def _visible_projection(page: Any) -> str:
    value = page.evaluate("""() => {
      const visible = element => {
        const style = getComputedStyle(element)
        const rect = element.getBoundingClientRect()
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0
      }
      const pieces = [document.body?.innerText || '']
      for (const element of document.querySelectorAll('input, textarea, select, a, [aria-label], [title]')) {
        if (!visible(element)) continue
        for (const value of [element.value, element.textContent, element.getAttribute('aria-label'), element.getAttribute('title'), element.getAttribute('href')]) {
          if (typeof value === 'string' && value.trim()) pieces.push(value.trim())
        }
      }
      return pieces.join('\n')
    }""")
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_VISIBLE_BYTES:
        raise PageAgentTwentyRunnerError("visible page projection is invalid or oversized")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _bridge_reports_match(*, bridge: dict[str, Any], provider: dict[str, Any],
                          business: dict[str, Any], task_kind: str,
                          config: PageAgentTwentyConfig) -> bool:
    metrics = bridge.get("metrics")
    fields = {"provider_calls", "page_actions", "business_calls", "retries", "steps"}
    if (not isinstance(metrics, dict) or set(metrics) != fields
            or any(isinstance(metrics.get(name), bool) or not isinstance(metrics.get(name), int)
                   or metrics[name] < 0 for name in fields)):
        return False
    accounting = provider.get("accounting")
    if not isinstance(accounting, dict):
        return False
    exact = (
        metrics["provider_calls"] == provider.get("provider_attempts")
        == provider.get("provider_responses") == accounting.get("model_calls")
        and metrics["business_calls"] == business.get("business_attempts")
        == business.get("business_responses")
        and metrics["provider_calls"] >= 1
        and metrics["business_calls"] >= 1
        and metrics["provider_calls"] <= config.max_model_calls
        and metrics["page_actions"] <= config.max_page_actions
        and metrics["business_calls"] <= config.max_business_calls
        and metrics["steps"] <= config.max_steps
        and metrics["retries"] == 0
    )
    if task_kind == "open":
        exact = exact and business.get("route_permits", 0) >= 1 \
            and business.get("route_permits", 0) <= metrics["page_actions"]
    return bool(exact)


def _provider_apparatus_code(code: Any) -> str | None:
    if code == "ledger_failure":
        return "ledger_failure"
    if code == "transport_contract":
        return "transport_contract"
    if code in {
            "provider_call", "provider_http", "provider_transport", "provider_response",
            "provider_usage", "provider_capacity"}:
        return "provider_outage"
    return None


def run_case(*, case: dict[str, Any], adapter: TwentyApplicationAdapter,
             client: ChatModel, config: PageAgentTwentyConfig,
             run_id: str | None = None) -> dict[str, Any]:
    """Execute one query/open case in a new browser and return a private runner artifact."""
    selected = _execution_case(case)
    config.verify_files()
    run_id = run_id or f"pageagent-{uuid.uuid4().hex}"
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
        raise PageAgentTwentyRunnerError("run_id is invalid")
    if not isinstance(adapter, TwentyApplicationAdapter):
        raise PageAgentTwentyRunnerError("runner requires a TwentyApplicationAdapter")
    application = bind_application(adapter)
    if not isinstance(client, ChatModel) or client.model.strip() != client.model:
        raise PageAgentTwentyRunnerError("runner requires a configured ChatModel")
    started_at = _utc_now()
    started = time.monotonic()
    identity_now = int(time.time() * 1_000)
    initial_binding = adapter.page_session_binding(
        config.storage_state_path, now_ms=identity_now)
    principal = adapter.bind_browser_session(
        config.storage_state_path, delegation_id=f"pageagent-{uuid.uuid4().hex}",
        lifetime_s=min(config.wall_time_s + 60, 3_600), now_ms=identity_now)
    context = OperationContext.create(
        principal, session_id=f"pa-{uuid.uuid4().hex}",
        turn_id=f"turn-{uuid.uuid4().hex}", operation_id=f"op-{uuid.uuid4().hex}",
        deadline_ms=identity_now + config.wall_time_s * 1_000, now_ms=identity_now)
    skills = application.business_tools.relevant(selected["prompt"])
    if not skills or any(not skill.read_only for skill in skills):
        raise PageAgentTwentyRunnerError("query/open case did not select read-only Business Skills")
    token = uuid.uuid4().hex
    cancelled = threading.Event()
    provider = PageAgentProviderProxy(
        run_token=token, run_id=run_id, client=client,
        contract=PageAgentProviderContract(
            model=client.model, temperature=config.temperature, top_p=config.top_p,
            max_completion_tokens=config.max_completion_tokens,
            max_model_calls=config.max_model_calls, wall_time_s=config.wall_time_s),
        cancelled=cancelled)
    business = PageAgentBusinessProxy(
        run_token=token, runtime=application.business_tools,
        loaded_skill_ids=tuple(skill.skill_id for skill in skills), context=context,
        expected_catalog_sha256=application.descriptor.catalog_sha256,
        max_business_calls=config.max_business_calls,
        max_route_calls=config.max_page_actions, cancelled=cancelled)
    business_results: list[dict[str, Any]] = []
    page_errors: list[str] = []
    bridge_result: dict[str, Any] | None = None
    final_path = ""
    final_visible = ""
    session_unchanged = False
    error_code: str | None = None
    apparatus_code: str | None = None
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            launch: dict[str, Any] = {"headless": True}
            if config.browser_executable is not None:
                launch["executable_path"] = str(config.browser_executable)
            browser = playwright.chromium.launch(**launch)
            try:
                browser_context = browser.new_context(
                    storage_state=str(config.storage_state_path),
                    viewport={"width": 1440, "height": 1_000})
                try:
                    page = browser_context.new_page()
                    page.on("pageerror", lambda error: page_errors.append(type(error).__name__))
                    page.expose_function("__WOA_PAGEAGENT_PROVIDER_FETCH__", provider.handle)

                    def invoke_business(payload: Any) -> dict[str, Any]:
                        result = business.handle(payload)
                        business_results.append(json.loads(json.dumps(result)))
                        return result

                    page.expose_function("__WOA_PAGEAGENT_BUSINESS_TOOL__", invoke_business)
                    page.expose_function("__WOA_PAGEAGENT_OPEN_ROUTE__", business.handle_route)
                    page.goto(
                        application.descriptor.origin + config.entry_path,
                        wait_until="domcontentloaded", timeout=config.wall_time_s * 1_000)
                    page.locator("body").wait_for(
                        state="visible", timeout=config.wall_time_s * 1_000)
                    page.add_script_tag(path=str(config.bundle_path))
                    bridge_config = {
                        "prompt": selected["prompt"],
                        "model": client.model,
                        "language": config.language,
                        "max_steps": config.max_steps,
                        "max_page_actions": config.max_page_actions,
                        "wall_ms": config.wall_time_s * 1_000,
                        "navigation_mode": application.descriptor.navigation_mode,
                        "capability_profile": "skill_api",
                        "instructions": business.instructions + "\n" + _QUERY_OUTPUT_CONTRACT,
                        "tools": list(business.tool_specs),
                        "run_token": token,
                        "request_options": provider.contract.bridge_request_options(),
                    }
                    try:
                        value = page.evaluate(
                            "config => globalThis.__WOA_PAGEAGENT_BASELINE__.execute(config)",
                            bridge_config)
                        if isinstance(value, dict):
                            bridge_result = value
                        else:
                            error_code = "bridge_result"
                    except Exception:
                        try:
                            host_code = provider.report().get("last_error_code")
                        except Exception:
                            host_code = "ledger_failure"
                        apparatus_code = _provider_apparatus_code(host_code)
                        error_code = str(host_code or "bridge_execution")
                    final_path = urlsplit(page.url).path
                    if selected["task_kind"] == "open":
                        try:
                            final_visible = _visible_projection(page)
                        except PageAgentTwentyRunnerError:
                            error_code = error_code or "evidence_contract"
                    try:
                        current_binding = adapter.page_session_binding(
                            browser_context.storage_state())
                        session_unchanged = hmac.compare_digest(
                            initial_binding, current_binding)
                    except Exception:
                        session_unchanged = False
                    if not session_unchanged:
                        apparatus_code, error_code = (
                            "credential_or_identity", "session_changed")
                finally:
                    browser_context.close()
            finally:
                browser.close()
    except Exception:
        apparatus_code = apparatus_code or "transport_contract"
        error_code = error_code or "browser_transport"

    try:
        provider_report = provider.report()
    except Exception:
        apparatus_code = "ledger_failure"
        error_code = "ledger_failure"
        provider_report = {**provider.transport_report(), "accounting": None}
    business_report = business.report()
    evidence: dict[str, Any] = {"artifact_version": 1, "execution_status": "failed"}
    if apparatus_code is None and bridge_result is not None:
        header_succeeded = (
            bridge_result.get("bridge_version") == 2
            and bridge_result.get("upstream_version") == "1.12.4"
            and bridge_result.get("status") == "completed"
            and bridge_result.get("success_claim") is True
            and bridge_result.get("terminal_reason") is None)
        reports_match = _bridge_reports_match(
                bridge=bridge_result, provider=provider_report,
                business=business_report, task_kind=selected["task_kind"],
                config=config)
        successful = header_succeeded and reports_match
        if successful:
            try:
                evidence = (
                    query_evidence(bridge_result.get("answer"))
                    if selected["task_kind"] == "query"
                    else open_evidence(
                        path=final_path, visible_projection=final_visible,
                        business_results=business_results))
            except PageAgentTwentyRunnerError:
                error_code = "evidence_contract"
        else:
            error_code = error_code or (
                "metrics_contract" if header_succeeded else "agent_failed")
    elif apparatus_code is None:
        error_code = error_code or "bridge_result"

    bridge_projection = None
    if bridge_result is not None:
        bridge_projection = {
            key: bridge_result.get(key) for key in (
                "bridge_version", "upstream_version", "status", "success_claim",
                "terminal_reason", "metrics")
        }
        bridge_projection["answer_digest"] = _digest_json(bridge_result.get("answer", ""))
    runtime_trace_digest = _digest_json({
        "bridge": bridge_projection,
        "business_result_digests": [_digest_json(item) for item in business_results],
        "final_path": final_path,
        "page_errors": len(page_errors),
        "session_unchanged": session_unchanged,
    })
    status = (
        "apparatus_invalid" if apparatus_code else
        "completed" if evidence.get("execution_status") == "completed" else "failed")
    finished_at = _utc_now()
    result = {
        "artifact_version": RUNNER_VERSION,
        "kind": "pageagent_twenty_run",
        "status": status,
        "apparatus_code": apparatus_code,
        "error_code": error_code,
        "run_id": run_id,
        "case": {key: selected[key] for key in (
            "set_id", "split", "task_id", "task_digest", "template_family_digest")},
        "engine": {"name": "pageagent_core", "release": "pageagent@1.12.4",
                   "bundle_digest": config.bundle_digest},
        "evidence": evidence,
        "bridge": bridge_projection,
        "provider": provider_report,
        "business": business_report,
        "session_binding_unchanged": session_unchanged,
        "page_error_count": len(page_errors),
        "runtime_trace_digest": runtime_trace_digest,
        "started_at": started_at,
        "finished_at": finished_at,
        "wall_ms": int((time.monotonic() - started) * 1_000),
    }
    return {**result, "run_digest": _digest_json(result)}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PageAgentTwentyRunnerError(f"cannot read JSON input: {path.name}") from exc
    if not isinstance(value, dict):
        raise PageAgentTwentyRunnerError(f"JSON input must be an object: {path.name}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temporary, "x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _environment(path: Path | None, environ: Mapping[str, str]) -> dict[str, str]:
    loaded = load_env(path) if path is not None else {}
    keys = {"AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_API_KEY",
            "AGENT_OPENAI_MODEL_PRO", "AGENT_PROVIDER"}
    result = {key: value for key, value in loaded.items() if key in keys}
    result.update({key: environ[key] for key in keys if environ.get(key)})
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one read-only PageAgent Twenty case")
    parser.add_argument("--case", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--bundle-digest", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--storage-state", required=True)
    parser.add_argument("--oauth-token", required=True)
    parser.add_argument("--oauth-registration", required=True)
    parser.add_argument("--spend-ledger", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model-env")
    parser.add_argument("--model")
    parser.add_argument("--language", choices=("en-US", "zh-CN"), default="en-US")
    parser.add_argument("--entry-path", default="/objects/companies")
    parser.add_argument("--run-id")
    return parser


def main(argv: Sequence[str] | None = None,
         environ: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        env = _environment(Path(args.model_env) if args.model_env else None,
                           os.environ if environ is None else environ)
        model = args.model or env.get("AGENT_OPENAI_MODEL_PRO")
        meter = SpendMeter(ledger=Path(args.spend_ledger), batch_id="pageagent-twenty")
        client = ChatModel(meter, env=env, model=model, max_completion_tokens=2_048)
        adapter = TwentyApplicationAdapter(
            base_url=args.base_url, token_path=args.oauth_token,
            registration_path=args.oauth_registration)
        config = PageAgentTwentyConfig(
            bundle_path=Path(args.bundle), bundle_digest=args.bundle_digest,
            storage_state_path=Path(args.storage_state), entry_path=args.entry_path,
            language=args.language)
        result = run_case(
            case=_read_json(Path(args.case)), adapter=adapter, client=client,
            config=config, run_id=args.run_id)
        _write_json(Path(args.output), result)
        print(json.dumps({
            "status": result["status"], "run_id": result["run_id"],
            "run_digest": result["run_digest"],
        }, sort_keys=True))
        return 0 if result["status"] != "apparatus_invalid" else 2
    except Exception as exc:
        print(json.dumps({
            "status": "apparatus_invalid", "error": type(exc).__name__,
        }, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
