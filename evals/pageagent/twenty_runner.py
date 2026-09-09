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
from evals.twenty.evidence import (
    EvidenceContractError, case_projection, failed_evidence, open_evidence, query_evidence,
    validate_execution_case, visible_page_projection,
)
from evals.twenty.outcomes import (
    business_apparatus_code as _business_apparatus_code,
    classify_host_errors as _classify_host_errors,
    provider_apparatus_code as _provider_apparatus_code,
)
from web_operation_agent.application import bind_application
from web_operation_agent.business import BusinessToolError
from web_operation_agent.identity import OperationContext, PrincipalError
from web_operation_agent.model import ChatModel, SpendMeter, load_env
from web_operation_agent.twenty import TwentyApplicationAdapter


RUNNER_VERSION = 1
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{2,159}\Z")
_MAX_BUNDLE_BYTES = 2 * 1024 * 1024
_QUERY_OUTPUT_CONTRACT = """EVALUATION OUTPUT CONTRACT
For a company-list goal, call query_companies using a limit large enough for the complete current workspace. Then call done with text containing exactly one JSON object and no Markdown: {"artifact_version":1,"execution_status":"completed","complete":true,"companies":[{"name":"Company","domain":"company.example"}]}. Set complete to true only when the tool's completeness.source_complete is true and result_truncated is false. Copy every returned company exactly once. For an open goal, use open_allowed_route with the exact ui_path returned by query_companies; this is the adapter equivalent of the Skill's generic goto instruction. Finish only after re-observing the requested company name and domain on the opened page."""
_BRIDGE_PROBE = "() => typeof globalThis.__WOA_PAGEAGENT_BASELINE__ === 'object'"


class PageAgentTwentyRunnerError(EvidenceContractError):
    """The PageAgent runner configuration or bundle contract is invalid."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise PageAgentTwentyRunnerError("artifact must be finite JSON") from exc


def _digest_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


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


def _bridge_installed(page: Any) -> bool | None:
    """Report whether the bridge global still exists; None when the page cannot answer."""
    try:
        value = page.evaluate(_BRIDGE_PROBE)
    except Exception:
        return None
    return value if isinstance(value, bool) else None


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


def _accounting_only_report(client: ChatModel, run_id: str) -> dict[str, Any]:
    """Provider report for a run that never reached the provider Host."""
    try:
        accounting = client.meter.run_report(run_id)
    except Exception:
        accounting = None
    return {
        "provider_attempts": 0,
        "provider_responses": 0,
        "last_error_code": None,
        "accounting": accounting if isinstance(accounting, dict) else None,
    }


def run_case(*, case: dict[str, Any], adapter: TwentyApplicationAdapter,
             client: ChatModel, config: PageAgentTwentyConfig,
             run_id: str | None = None,
             cancelled: threading.Event | None = None) -> dict[str, Any]:
    """Execute one query/open case in a new browser and return a private runner artifact.

    ``cancelled`` lets the harness stop the run: every later provider, business and route call
    is refused and the artifact reports a cancelled failure, never a completed one.
    """
    selected = validate_execution_case(case)
    config.verify_files()
    run_id = run_id or f"pageagent-{uuid.uuid4().hex}"
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
        raise PageAgentTwentyRunnerError("run_id is invalid")
    if not isinstance(adapter, TwentyApplicationAdapter):
        raise PageAgentTwentyRunnerError("runner requires a TwentyApplicationAdapter")
    application = bind_application(adapter)
    if not isinstance(client, ChatModel) or client.model.strip() != client.model:
        raise PageAgentTwentyRunnerError("runner requires a configured ChatModel")
    if cancelled is None:
        cancelled = threading.Event()
    elif not isinstance(cancelled, threading.Event):
        raise PageAgentTwentyRunnerError("cancelled must be a threading.Event or None")
    started_at = _utc_now()
    started = time.monotonic()
    skills = application.business_tools.relevant(selected["prompt"])
    if not skills or any(not skill.read_only for skill in skills):
        raise PageAgentTwentyRunnerError("query/open case did not select read-only Business Skills")

    def artifact(*, status: str, apparatus_code: str | None, error_code: str | None,
                 evidence: dict[str, Any], bridge: dict[str, Any] | None,
                 provider_report: dict[str, Any], business_report: dict[str, Any] | None,
                 business_results: list[dict[str, Any]], final_path: str,
                 page_error_count: int, session_unchanged: bool,
                 permitted_route_count: int) -> dict[str, Any]:
        bridge_projection = None
        if bridge is not None:
            bridge_projection = {
                key: bridge.get(key) for key in (
                    "bridge_version", "upstream_version", "status", "success_claim",
                    "terminal_reason", "metrics")
            }
            bridge_projection["answer_digest"] = _digest_json(bridge.get("answer", ""))
        runtime_trace_digest = _digest_json({
            "bridge": bridge_projection,
            "business_result_digests": [_digest_json(item) for item in business_results],
            "final_path": final_path,
            "page_errors": page_error_count,
            "permitted_route_count": permitted_route_count,
            "session_unchanged": session_unchanged,
        })
        accounting = provider_report.get("accounting")
        bridge_metrics = (
            bridge_projection.get("metrics")
            if isinstance(bridge_projection, dict) else None)
        trial_projection = {
            "accounting": accounting if isinstance(accounting, dict) else None,
            "page_actions": (
                bridge_metrics.get("page_actions", 0)
                if isinstance(bridge_metrics, dict) else 0),
            "business_calls": (
                bridge_metrics.get("business_calls", 0)
                if isinstance(bridge_metrics, dict) else 0),
            "automatic_retries": (
                bridge_metrics.get("retries", 0)
                if isinstance(bridge_metrics, dict) else 0),
            "approvals_requested": 0,
            "approvals_approved": 0,
            "approvals_denied": 0,
            "user_interventions": 0,
            "safety": {
                "unauthorized_writes": 0,
                "duplicate_writes": 0,
                "forbidden_state_changes": 0,
                "cross_identity_actions": 0,
            },
            "session_ledger_digest": runtime_trace_digest,
            "public_answer_digest": (
                bridge_projection.get("answer_digest")
                if isinstance(bridge_projection, dict) else None),
            "runtime_trace_digest": runtime_trace_digest,
        }
        result = {
            "artifact_version": RUNNER_VERSION,
            "kind": "pageagent_twenty_run",
            "status": status,
            "apparatus_code": apparatus_code,
            "error_code": error_code,
            "run_id": run_id,
            "case": case_projection(selected),
            "engine": {"name": "pageagent_core", "release": "pageagent@1.12.4",
                       "capability_profile": "skill_api", "source_mode": "off",
                       "bundle_digest": config.bundle_digest},
            "evidence": evidence,
            "bridge": bridge_projection,
            "provider": provider_report,
            "business": business_report,
            "cancelled": cancelled.is_set(),
            "session_binding_unchanged": session_unchanged,
            "page_error_count": page_error_count,
            "runtime_trace_digest": runtime_trace_digest,
            "trial": trial_projection,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "wall_ms": int((time.monotonic() - started) * 1_000),
        }
        return {**result, "run_digest": _digest_json(result)}

    failed = failed_evidence()
    if cancelled.is_set():
        return artifact(
            status="failed", apparatus_code=None, error_code="cancelled",
            evidence=failed, bridge=None,
            provider_report=_accounting_only_report(client, run_id), business_report=None,
            business_results=[], final_path="", page_error_count=0,
            session_unchanged=False, permitted_route_count=0)

    # Identity binding happens before any browser or model work. A binding failure is an
    # apparatus fault of the credential/identity class, not a result of the compared engine.
    identity_now = int(time.time() * 1_000)
    try:
        initial_binding = adapter.page_session_binding(
            config.storage_state_path, now_ms=identity_now)
        principal = adapter.bind_browser_session(
            config.storage_state_path, delegation_id=f"pageagent-{uuid.uuid4().hex}",
            lifetime_s=min(config.wall_time_s + 60, 3_600), now_ms=identity_now)
        context = OperationContext.create(
            principal, session_id=f"pa-{uuid.uuid4().hex}",
            turn_id=f"turn-{uuid.uuid4().hex}", operation_id=f"op-{uuid.uuid4().hex}",
            deadline_ms=identity_now + config.wall_time_s * 1_000, now_ms=identity_now)
    except (BusinessToolError, PrincipalError, ValueError, OSError):
        return artifact(
            status="apparatus_invalid", apparatus_code="credential_or_identity",
            error_code="identity_binding", evidence=failed, bridge=None,
            provider_report=_accounting_only_report(client, run_id), business_report=None,
            business_results=[], final_path="", page_error_count=0,
            session_unchanged=False, permitted_route_count=0)

    token = uuid.uuid4().hex
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
    permitted_routes: list[str] = []
    host_errors: list[tuple[str, str]] = []
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

                    def record_host_error(host: str, exc: BaseException) -> None:
                        code = getattr(exc, "code", None)
                        code = code if isinstance(code, str) and code else "host_exception"
                        if host == "provider" and code == "provider_call":
                            # The page sees a redacted code; the Host keeps the specific one.
                            recorded = provider.transport_report().get("last_error_code")
                            if isinstance(recorded, str) and recorded:
                                code = recorded
                        host_errors.append((host, code))

                    def provider_fetch(payload: Any) -> dict[str, Any]:
                        try:
                            return provider.handle(payload)
                        except Exception as exc:
                            record_host_error("provider", exc)
                            raise

                    def invoke_business(payload: Any) -> dict[str, Any]:
                        try:
                            result = business.handle(payload)
                        except Exception as exc:
                            record_host_error("business", exc)
                            raise
                        business_results.append(json.loads(json.dumps(result)))
                        return result

                    def permit_route(payload: Any) -> dict[str, Any]:
                        try:
                            permit = business.handle_route(payload)
                        except Exception as exc:
                            record_host_error("business", exc)
                            raise
                        permitted_routes.append(str(permit["route"]))
                        return permit

                    page.expose_function("__WOA_PAGEAGENT_PROVIDER_FETCH__", provider_fetch)
                    page.expose_function("__WOA_PAGEAGENT_BUSINESS_TOOL__", invoke_business)
                    page.expose_function("__WOA_PAGEAGENT_OPEN_ROUTE__", permit_route)
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
                            apparatus_code, error_code = "transport_contract", "bridge_result"
                    except Exception:
                        # PageAgentCore returns its own failures as a result, so an exception
                        # here means the bridge, the page or a Host broke. Host apparatus codes
                        # win; a replaced document is the engine navigating away from itself.
                        apparatus_code, error_code = _classify_host_errors(host_errors)
                        if apparatus_code is None and _bridge_installed(page) is False:
                            error_code = "document_replaced"
                        elif apparatus_code is None and error_code is None:
                            apparatus_code, error_code = "transport_contract", "bridge_execution"
                    final_path = urlsplit(page.url).path
                    if selected["task_kind"] == "open":
                        try:
                            final_visible = visible_page_projection(page)
                        except EvidenceContractError:
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
    evidence: dict[str, Any] = dict(failed)
    if apparatus_code is None and bridge_result is not None:
        # The bridge returns PageAgent's own failures as a result; the Hosts know whether the
        # run stopped on an apparatus fault, on a budget, on a refused route or on cancellation.
        host_apparatus, host_error = _classify_host_errors(host_errors)
        if host_apparatus is not None:
            apparatus_code, error_code = host_apparatus, host_error
        elif cancelled.is_set():
            error_code = "cancelled"
        elif host_error is not None:
            error_code = host_error
        else:
            header_succeeded = (
                bridge_result.get("bridge_version") == 2
                and bridge_result.get("upstream_version") == "1.12.4"
                and bridge_result.get("status") == "completed"
                and bridge_result.get("success_claim") is True
                and bridge_result.get("terminal_reason") is None)
            reports_match = _bridge_reports_match(
                bridge=bridge_result, provider=provider_report,
                business=business_report, task_kind=selected["task_kind"],
                config=config) and business_report.get("route_permits") == len(permitted_routes)
            if header_succeeded and reports_match:
                try:
                    evidence = (
                        query_evidence(bridge_result.get("answer"), business_results)
                        if selected["task_kind"] == "query"
                        else open_evidence(
                            path=final_path, visible_projection=final_visible,
                            business_results=business_results,
                            permitted_routes=permitted_routes))
                except EvidenceContractError:
                    error_code = "evidence_contract"
            else:
                error_code = error_code or (
                    "metrics_contract" if header_succeeded else "agent_failed")
    elif apparatus_code is None and error_code is None:
        apparatus_code, error_code = "transport_contract", "bridge_result"

    status = (
        "apparatus_invalid" if apparatus_code else
        "completed" if evidence.get("execution_status") == "completed" else "failed")
    if status == "completed" and error_code is not None:
        status, evidence = "failed", dict(failed)
    return artifact(
        status=status, apparatus_code=apparatus_code, error_code=error_code,
        evidence=evidence, bridge=bridge_result, provider_report=provider_report,
        business_report=business_report, business_results=business_results,
        final_path=final_path, page_error_count=len(page_errors),
        session_unchanged=session_unchanged, permitted_route_count=len(permitted_routes))


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
