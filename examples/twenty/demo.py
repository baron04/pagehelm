#!/usr/bin/env python3
"""Run the embedded Web Operation Agent against a provisioned Twenty CRM instance.

This example is an application composition, not a test harness. It launches one browser,
injects the thin runtime, and keeps one server-side Agent available for sequential panel
messages. The emitted records describe how each Agent turn ended; they do not certify that
the user's business goal was achieved.
"""
from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.identity import PrincipalContext
from web_operation_agent.live import LiveConversationRunner, LiveProductSession, LiveTurnOutcome
from web_operation_agent.model import ChatModel, SpendMeter, load_env
from web_operation_agent.mutation import SQLiteMutationJournal
from web_operation_agent.privacy import redact_sensitive_assignments, redact_session_handoffs
from web_operation_agent.product import AgentTurnConfig, AgentTurnExecution, AgentTurnExecutor
from web_operation_agent.twenty import TwentyApplicationAdapter
from web_operation_agent.application import bind_application


_MODEL_ENV_KEYS = (
    "AGENT_OPENAI_BASE_URL",
    "AGENT_OPENAI_API_KEY",
    "AGENT_OPENAI_MODEL_PRO",
    "AGENT_PROVIDER",
)
_DISABLED_BROWSER_ACTIONS = frozenset({"click", "fill", "select", "submit", "back"})
_READ_ONLY_SCOPE = (
    "Read current Twenty CRM company data and open only routes returned by the delegated "
    "application API. Do not modify records or submit browser forms."
)
_MUTATION_SCOPE = (
    "Read current Twenty CRM company data and open only routes returned by the delegated "
    "application API. The Agent may propose creating one user-specified company through the "
    "typed API; every proposal requires a fresh confirmation. Do not submit browser forms or "
    "modify any other record."
)
_PRINCIPAL_LIFETIME_S = 3_600


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _state_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else Path.cwd() / candidate


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative finite number")
    return parsed


def _configured(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return value if value and value.strip() else None


def configuration_environment(
        environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Merge the optional dotenv file with process values taking precedence."""
    process_env = dict(os.environ if environ is None else environ)
    model_env = _configured(process_env, "WOA_MODEL_ENV")
    loaded = load_env(Path(model_env).expanduser()) if model_env else {}
    return {**loaded, **process_env}


def _origin(value) -> tuple[str, str | None, int | None]:
    default_port = 80 if value.scheme.casefold() == "http" else 443
    return value.scheme.casefold(), value.hostname, value.port or default_port


def _add_configured_argument(
        parser: argparse.ArgumentParser, flag: str, env: Mapping[str, str], env_name: str,
        *, help_text: str) -> None:
    default = _configured(env, env_name)
    parser.add_argument(
        flag,
        default=default,
        required=default is None,
        help=f"{help_text} (or {env_name})",
    )


@dataclass(frozen=True, slots=True)
class TwentyDemoConfig:
    base_url: str
    entry_path: str
    storage_state_path: Path
    oauth_token_path: Path
    oauth_registration_path: Path
    page_controller_module: str
    audit_ledger_path: Path
    spend_ledger_path: Path
    output_root: Path
    mutation_journal_path: Path | None = None
    model_env_path: Path | None = None
    model: str | None = None
    browser_executable: Path | None = None
    allow_mutations: bool = False
    headless: bool = False
    tasks: tuple[str, ...] = ()
    keep_open_seconds: float = 3.0
    step_budget: int = 10
    wall_budget_s: int = 240
    transport_timeout_s: float = 60.0
    turn_timeout_s: float = 300.0
    approval_ttl_s: int = 180

    def __post_init__(self) -> None:
        for name in (
                "storage_state_path", "oauth_token_path", "oauth_registration_path",
                "audit_ledger_path", "spend_ledger_path", "output_root"):
            object.__setattr__(self, name, Path(getattr(self, name)))
        if self.model_env_path is not None:
            object.__setattr__(self, "model_env_path", Path(self.model_env_path))
        if self.browser_executable is not None:
            object.__setattr__(self, "browser_executable", Path(self.browser_executable))
        if self.mutation_journal_path is not None:
            object.__setattr__(
                self, "mutation_journal_path", Path(self.mutation_journal_path))
        if not isinstance(self.allow_mutations, bool) or not isinstance(self.headless, bool):
            raise ValueError("demo mode flags must be booleans")
        if not self.entry_path.startswith("/") or self.entry_path.startswith("//"):
            raise ValueError("entry_path must be a same-origin absolute path")
        base = urlsplit(self.base_url)
        module = urlsplit(self.page_controller_module)
        if base.scheme not in {"http", "https"} or not base.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if module.scheme not in {"http", "https"} or not module.netloc:
            raise ValueError("page_controller_module must be an absolute HTTP(S) URL")
        base_origin = _origin(base)
        module_origin = _origin(module)
        if module_origin != base_origin:
            raise ValueError("page_controller_module must use the Twenty origin")
        audit_location = self.audit_ledger_path.resolve(strict=False)
        spend_location = self.spend_ledger_path.resolve(strict=False)
        if audit_location == spend_location:
            raise ValueError("audit and spend ledgers must be different files")
        input_locations = {
            self.storage_state_path.resolve(strict=False),
            self.oauth_token_path.resolve(strict=False),
            self.oauth_registration_path.resolve(strict=False),
        }
        if audit_location in input_locations or spend_location in input_locations:
            raise ValueError("ledger paths must not overwrite authentication inputs")
        if self.allow_mutations and self.mutation_journal_path is None:
            raise ValueError("mutation-enabled sessions require a durable mutation journal")
        if (self.mutation_journal_path is not None
                and self.mutation_journal_path.resolve(strict=False) in input_locations | {
                    audit_location, spend_location}):
            raise ValueError("mutation journal must use a dedicated path")
        for name in ("step_budget", "wall_budget_s", "approval_ttl_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("transport_timeout_s", "turn_timeout_s"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be a positive finite number")
        if (isinstance(self.keep_open_seconds, bool)
                or not isinstance(self.keep_open_seconds, (int, float))
                or not math.isfinite(self.keep_open_seconds)
                or self.keep_open_seconds < 0):
            raise ValueError("keep_open_seconds must be a nonnegative finite number")
        if self.headless and not self.tasks:
            raise ValueError("headless mode requires at least one --task")
        if self.headless and self.allow_mutations:
            raise ValueError("mutation-enabled demo sessions require a visible browser")
        if any(not isinstance(task, str) or not task.strip() for task in self.tasks):
            raise ValueError("tasks must be non-empty strings")


def build_parser(environ: Mapping[str, str] | None = None) -> argparse.ArgumentParser:
    env = configuration_environment(environ)
    parser = argparse.ArgumentParser(
        prog="web-operation-agent-twenty-demo",
        description="Run the embedded Agent against an already provisioned Twenty CRM.",
        epilog="Run ./demo.sh --check first to diagnose prerequisites without launching.",
    )
    _add_configured_argument(
        parser, "--base-url", env, "WOA_TWENTY_BASE_URL",
        help_text="Twenty origin, for example https://crm.example.test",
    )
    _add_configured_argument(
        parser, "--storage-state", env, "WOA_TWENTY_STORAGE_STATE",
        help_text="Playwright storage-state JSON for the delegated test user",
    )
    _add_configured_argument(
        parser, "--oauth-token", env, "WOA_TWENTY_OAUTH_TOKEN",
        help_text="Twenty OAuth token JSON for the same delegated test user",
    )
    _add_configured_argument(
        parser, "--oauth-registration", env, "WOA_TWENTY_OAUTH_REGISTRATION",
        help_text="Twenty OAuth client-registration JSON",
    )
    _add_configured_argument(
        parser, "--page-controller-module", env, "WOA_TWENTY_PAGE_CONTROLLER_MODULE",
        help_text="same-origin URL of the PageController ES module",
    )
    _add_configured_argument(
        parser, "--audit-ledger", env, "WOA_AUDIT_LEDGER",
        help_text="append-only Agent event ledger",
    )
    _add_configured_argument(
        parser, "--spend-ledger", env, "WOA_SPEND_LEDGER",
        help_text="append-only model usage and cost ledger",
    )
    _add_configured_argument(
        parser, "--output-dir", env, "WOA_OUTPUT_DIR",
        help_text="directory under which this session writes redacted summaries",
    )
    parser.add_argument(
        "--mutation-journal",
        default=_configured(env, "WOA_MUTATION_JOURNAL"),
        help="durable SQLite mutation state (required with --allow-mutations; or WOA_MUTATION_JOURNAL)",
    )
    parser.add_argument(
        "--entry-path",
        default=_configured(env, "WOA_TWENTY_ENTRY_PATH") or "/settings/profile",
        help="initial same-origin Twenty path (or WOA_TWENTY_ENTRY_PATH)",
    )
    parser.add_argument(
        "--model-env",
        default=_configured(env, "WOA_MODEL_ENV"),
        help="optional dotenv file with AGENT_OPENAI_* values (or WOA_MODEL_ENV)",
    )
    parser.add_argument(
        "--model",
        default=_configured(env, "WOA_MODEL"),
        help="model override (or WOA_MODEL; otherwise AGENT_OPENAI_MODEL_PRO)",
    )
    parser.add_argument(
        "--browser-executable",
        default=_configured(env, "WOA_BROWSER_EXECUTABLE"),
        help="optional Chromium executable (or WOA_BROWSER_EXECUTABLE)",
    )
    parser.add_argument(
        "--allow-mutations",
        action="store_true",
        help="load the company-create Skill and allow confirmed typed-API writes",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--task", action="append", default=[],
        help="submit a task after startup; repeat for sequential turns",
    )
    parser.add_argument(
        "--keep-open-seconds", type=_nonnegative_float,
        default=_configured(env, "WOA_KEEP_OPEN_SECONDS") or "3",
        help="visible-browser delay after scripted tasks (or WOA_KEEP_OPEN_SECONDS)",
    )
    parser.add_argument(
        "--step-budget", type=_positive_int,
        default=_configured(env, "WOA_STEP_BUDGET") or "10",
    )
    parser.add_argument(
        "--wall-budget", type=_positive_int,
        default=_configured(env, "WOA_WALL_BUDGET_SECONDS") or "240",
    )
    parser.add_argument(
        "--transport-timeout", type=_positive_float,
        default=_configured(env, "WOA_TRANSPORT_TIMEOUT_SECONDS") or "60",
    )
    parser.add_argument(
        "--turn-timeout", type=_positive_float,
        default=_configured(env, "WOA_TURN_TIMEOUT_SECONDS") or "300",
    )
    parser.add_argument(
        "--approval-ttl", type=_positive_int,
        default=_configured(env, "WOA_APPROVAL_TTL_SECONDS") or "180",
    )
    return parser


def parse_config(
        argv: Sequence[str] | None = None,
        environ: Mapping[str, str] | None = None) -> TwentyDemoConfig:
    namespace = build_parser(environ).parse_args(argv)
    return TwentyDemoConfig(
        base_url=namespace.base_url,
        entry_path=namespace.entry_path,
        storage_state_path=_path(namespace.storage_state),
        oauth_token_path=_path(namespace.oauth_token),
        oauth_registration_path=_path(namespace.oauth_registration),
        page_controller_module=namespace.page_controller_module,
        audit_ledger_path=_path(namespace.audit_ledger),
        spend_ledger_path=_path(namespace.spend_ledger),
        output_root=_path(namespace.output_dir),
        mutation_journal_path=(
            _state_path(namespace.mutation_journal) if namespace.mutation_journal else None),
        model_env_path=_path(namespace.model_env) if namespace.model_env else None,
        model=namespace.model,
        browser_executable=(
            _path(namespace.browser_executable) if namespace.browser_executable else None),
        allow_mutations=namespace.allow_mutations,
        headless=namespace.headless,
        tasks=tuple(namespace.task),
        keep_open_seconds=namespace.keep_open_seconds,
        step_budget=namespace.step_budget,
        wall_budget_s=namespace.wall_budget,
        transport_timeout_s=namespace.transport_timeout,
        turn_timeout_s=namespace.turn_timeout,
        approval_ttl_s=namespace.approval_ttl,
    )


def validate_inputs(config: TwentyDemoConfig) -> None:
    required = {
        "storage state": config.storage_state_path,
        "OAuth token": config.oauth_token_path,
        "OAuth registration": config.oauth_registration_path,
    }
    if config.model_env_path is not None:
        required["model environment"] = config.model_env_path
    if config.browser_executable is not None:
        required["browser executable"] = config.browser_executable
    missing = [f"{label}: {path}" for label, path in required.items() if not path.is_file()]
    if missing:
        raise RuntimeError("required demo input is unavailable: " + "; ".join(missing))


def build_adapter(config: TwentyDemoConfig) -> TwentyApplicationAdapter:
    journal = (SQLiteMutationJournal(config.mutation_journal_path)
               if config.allow_mutations and config.mutation_journal_path is not None
               else None)
    return TwentyApplicationAdapter(
        base_url=config.base_url,
        token_path=config.oauth_token_path,
        registration_path=config.oauth_registration_path,
        allow_mutations=config.allow_mutations,
        mutation_journal=journal,
    )


def build_turn_config(config: TwentyDemoConfig) -> AgentTurnConfig:
    return AgentTurnConfig(
        supported_scope=_MUTATION_SCOPE if config.allow_mutations else _READ_ONLY_SCOPE,
        ledger_path=config.audit_ledger_path,
        step_budget=config.step_budget,
        wall_budget_s=config.wall_budget_s,
        transport_timeout_s=config.transport_timeout_s,
        disabled_actions=_DISABLED_BROWSER_ACTIONS,
        scope_first_policy=True,
        policy_block_limit=2,
        plain_business_answer_min_page_actions=0,
        approval_ttl_s=config.approval_ttl_s,
        allow_business_mutations=config.allow_mutations,
        require_identity=True,
        max_user_answer_chars=20_000,
    )


def model_environment(
        config: TwentyDemoConfig,
        environ: Mapping[str, str] | None = None) -> dict[str, str]:
    process_env = os.environ if environ is None else environ
    loaded = load_env(config.model_env_path) if config.model_env_path is not None else {}
    values = {key: loaded[key] for key in _MODEL_ENV_KEYS if _configured(loaded, key)}
    for key in _MODEL_ENV_KEYS:
        if _configured(process_env, key) is not None:
            values[key] = process_env[key]
    return values


def build_chat(
        config: TwentyDemoConfig,
        *, environ: Mapping[str, str] | None = None,
        batch_id: str) -> tuple[ChatModel, SpendMeter]:
    meter = SpendMeter(ledger=config.spend_ledger_path, batch_id=batch_id)
    chat = ChatModel(
        meter,
        env=model_environment(config, environ),
        model=config.model,
        max_completion_tokens=1024,
        timeout=config.wall_budget_s,
    )
    return chat, meter


def build_conversation(
        channel, *, config: TwentyDemoConfig, adapter: TwentyApplicationAdapter, chat,
        run_prefix: str) -> LiveConversationRunner:
    application = bind_application(adapter)
    executor = AgentTurnExecutor(
        channel=channel,
        config=build_turn_config(config),
        chat=chat,
        business_tools=application.business_tools,
        control=ReadOnlyControl(),
        run_id_factory=lambda _message: f"{run_prefix}-{uuid.uuid4().hex[:12]}",
        attestation=None,
    )
    return LiveConversationRunner(channel, executor=executor)


def build_live_session(
        config: TwentyDemoConfig, adapter: TwentyApplicationAdapter, chat,
        *, run_prefix: str, conversation_holder: dict[str, LiveConversationRunner],
        principal: PrincipalContext,
        session_id: str | None = None) -> LiveProductSession:
    def runner_factory(channel):
        conversation = build_conversation(
            channel, config=config, adapter=adapter, chat=chat, run_prefix=run_prefix)
        conversation_holder["conversation"] = conversation
        return conversation

    application = bind_application(adapter)
    page_controller_module = application.descriptor.validate_page_controller(
        config.page_controller_module)
    runtime_guard = getattr(chat, "runtime_guard", None)
    provider_status = getattr(runtime_guard, "status", None)
    return LiveProductSession(
        allowed_origin=application.descriptor.origin,
        page_controller_module=page_controller_module,
        runner_factory=runner_factory,
        session_id=session_id or f"twenty-live-{uuid.uuid4().hex[:12]}",
        navigation_mode=application.descriptor.navigation_mode,
        principal=principal,
        provider_status=provider_status if callable(provider_status) else None,
    )


def _safe(value):
    return redact_sensitive_assignments(redact_session_handoffs(value))


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temporary, "x", encoding="utf-8") as handle:
        json.dump(_safe(value), handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _append_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(_safe(value), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def summarize_outcome(outcome: LiveTurnOutcome) -> dict:
    if outcome.error:
        return {
            "schema_version": 1,
            "event": "turn_error",
            "turn": outcome.turn,
            "task": outcome.task,
            "error": outcome.error,
        }
    execution = outcome.result
    if not isinstance(execution, AgentTurnExecution):
        raise TypeError("conversation returned an unexpected turn result")
    return {
        "schema_version": 1,
        "event": "agent_turn_terminal",
        "turn": outcome.turn,
        "task": outcome.task,
        "run_id": execution.run_id,
        "status": execution.status.value,
        "terminal_reason": execution.terminal_reason,
        "user_answer": execution.user_answer,
        "steps_used": execution.agent.steps_used,
        "page_actions": execution.agent.page_actions,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
    }


def _record_outcome(path: Path, outcome: LiveTurnOutcome) -> dict:
    record = summarize_outcome(outcome)
    _append_json(path, record)
    print(json.dumps(_safe(record), ensure_ascii=False, sort_keys=True), flush=True)
    return record


def _assert_browser_session_unchanged(
        context, *, adapter: TwentyApplicationAdapter,
        live: LiveProductSession, expected_binding: str) -> None:
    try:
        current = adapter.page_session_binding(context.storage_state())
    except Exception as exc:
        live.close()
        raise RuntimeError("the Twenty browser session is no longer valid") from exc
    if not hmac.compare_digest(current, expected_binding):
        live.close()
        raise RuntimeError("the Twenty browser session changed; start a new Agent session")


def _wait_for_outcome(
        conversation: LiveConversationRunner, *, context,
        config: TwentyDemoConfig, adapter: TwentyApplicationAdapter,
        live: LiveProductSession, expected_binding: str) -> LiveTurnOutcome:
    deadline = time.monotonic() + config.turn_timeout_s
    while True:
        _assert_browser_session_unchanged(
            context, adapter=adapter, live=live,
            expected_binding=expected_binding)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for the current Agent turn")
        try:
            return conversation.next_outcome(timeout_s=min(0.25, remaining))
        except TimeoutError:
            continue


def _run_browser(config: TwentyDemoConfig, live: LiveProductSession,
                 conversation: LiveConversationRunner, outcomes_path: Path,
                 *, adapter: TwentyApplicationAdapter,
                 expected_session_binding: str) -> list[dict]:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    records: list[dict] = []
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        launch_options: dict = {"headless": config.headless}
        if config.browser_executable is not None:
            launch_options["executable_path"] = str(config.browser_executable)
        if not config.headless:
            launch_options["args"] = ["--start-maximized"]
        browser = playwright.chromium.launch(**launch_options)
        try:
            context_options: dict = {"storage_state": str(config.storage_state_path)}
            if config.headless:
                context_options["viewport"] = {"width": 1440, "height": 1000}
            else:
                context_options["no_viewport"] = True
            context = browser.new_context(**context_options)
            try:
                context.add_init_script(script=live.bootstrap_script)
                page = context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)[:500]))
                page.goto(
                    config.base_url.rstrip("/") + config.entry_path,
                    wait_until="domcontentloaded",
                    timeout=int(config.transport_timeout_s * 1000),
                )
                page.wait_for_function(
                    "window.__WOA_RUNTIME_READY__ !== undefined",
                    timeout=int(config.transport_timeout_s * 1000),
                )
                page.evaluate(
                    "async () => { await window.__WOA_RUNTIME_READY__; return true; }")
                page.locator("#woa-assistant").wait_for(
                    state="visible", timeout=int(config.transport_timeout_s * 1000))
                _assert_browser_session_unchanged(
                    context, adapter=adapter, live=live,
                    expected_binding=expected_session_binding)
                ready = {
                    "schema_version": 1,
                    "event": "demo_ready",
                    "application": "twenty",
                    "mode": "mutation_enabled" if config.allow_mutations else "read_only",
                    "entry_url": page.url,
                    "gateway_url": live.gateway.base_url,
                    "output": str(outcomes_path.parent),
                }
                print(json.dumps(_safe(ready), ensure_ascii=False, sort_keys=True), flush=True)

                if config.tasks:
                    for task in config.tasks:
                        page.evaluate(
                            "task => window.__WOA_RUNTIME_READY__.then("
                            "runtime => runtime.submitTask(task))",
                            task,
                        )
                        records.append(_record_outcome(outcomes_path, _wait_for_outcome(
                            conversation, context=context, config=config, adapter=adapter,
                            live=live, expected_binding=expected_session_binding,
                        )))
                        page.wait_for_function(
                            """() => {
                              const input = document.querySelector('#woa-assistant')?.shadowRoot
                                ?.querySelector('input');
                              return input && !input.disabled;
                            }""",
                            timeout=int(config.transport_timeout_s * 1000),
                        )
                    if config.keep_open_seconds and not config.headless:
                        page.wait_for_timeout(int(config.keep_open_seconds * 1000))
                else:
                    try:
                        while not page.is_closed():
                            _assert_browser_session_unchanged(
                                context, adapter=adapter, live=live,
                                expected_binding=expected_session_binding)
                            for outcome in conversation.outcomes_nowait():
                                records.append(_record_outcome(outcomes_path, outcome))
                            page.wait_for_timeout(250)
                    except KeyboardInterrupt:
                        pass
                    except PlaywrightError:
                        if not page.is_closed():
                            raise
                    if not page.is_closed():
                        for outcome in conversation.outcomes_nowait():
                            records.append(_record_outcome(outcomes_path, outcome))
            finally:
                context.close()
        finally:
            browser.close()
    if page_errors:
        print(json.dumps({
            "schema_version": 1,
            "event": "page_errors_observed",
            "count": len(page_errors),
        }, sort_keys=True), flush=True)
    return records


def run(config: TwentyDemoConfig, *, environ: Mapping[str, str] | None = None) -> dict:
    validate_inputs(config)
    adapter = build_adapter(config)
    if not adapter.health().get("passed"):
        raise RuntimeError("the configured Twenty instance did not pass its health check")
    identity_now_ms = int(time.time() * 1000)
    expected_session_binding = adapter.page_session_binding(
        config.storage_state_path, now_ms=identity_now_ms)
    principal = adapter.bind_browser_session(
        config.storage_state_path,
        delegation_id=f"twenty-oauth-{uuid.uuid4().hex}",
        lifetime_s=_PRINCIPAL_LIFETIME_S,
        now_ms=identity_now_ms,
    )

    run_tag = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = config.output_root / run_tag
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    outcomes_path = run_dir / "outcomes.jsonl"
    chat, meter = build_chat(config, environ=environ, batch_id=f"twenty-demo-{run_tag}")
    holder: dict[str, LiveConversationRunner] = {}
    live = build_live_session(
        config, adapter, chat, run_prefix=f"twenty-{run_tag}",
        conversation_holder=holder, principal=principal,
    )
    records: list[dict] = []
    worker_error = ""
    live.start()
    try:
        records = _run_browser(
            config, live, holder["conversation"], outcomes_path,
            adapter=adapter, expected_session_binding=expected_session_binding)
    finally:
        live.close()
        try:
            live.wait(timeout_s=config.turn_timeout_s)
        except Exception as exc:
            worker_error = _safe(f"{type(exc).__name__}: {exc}")[:800]

    terminal_counts: dict[str, int] = {}
    for record in records:
        state = str(record.get("status") or record.get("event") or "unknown")
        terminal_counts[state] = terminal_counts.get(state, 0) + 1
    summary = {
        "schema_version": 1,
        "application": "twenty",
        "session_id": live.grant.session_id,
        "identity_binding": "principal_bound",
        "principal_expires_at": principal.expires_at,
        "mode": "mutation_enabled" if config.allow_mutations else "read_only",
        "turn_count": len(records),
        "terminal_counts": terminal_counts,
        "worker_error": worker_error,
        "model_usage": meter.report(),
        "output": str(run_dir),
    }
    _write_json(run_dir / "summary.json", summary)
    print(json.dumps(_safe(summary), ensure_ascii=False, sort_keys=True), flush=True)
    if worker_error:
        raise RuntimeError("the server-side conversation worker did not close cleanly")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = parse_config(argv)
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("the Twenty demo requires Python 3.12")
        run(config)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        detail = _safe(f"{type(exc).__name__}: {exc}")[:800]
        print(json.dumps({
            "schema_version": 1,
            "event": "demo_error",
            "error": detail,
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
