"""What a host wires together to serve one Odoo turn, and the model-free way to check it.

Identity, the mutation journal, the control plane, the gateway and the browser session
belong to the host. Two pieces of that wiring are Odoo-specific and are the reason this file
exists rather than a paragraph in the README:

  * the principal comes from **two** sources that must agree -- the browser's storage state
    (a page-session assertion with no secret in it) and a per-user API key (the delegated
    actor). `OdooApplicationAdapter.bind_browser_session` does both and refuses a mismatch.
  * the provider egress policy is built from the adapter's declared session parameters.
    Composing it any other way is refused at construction, because the static baseline in
    `privacy.py` has never heard of Odoo's.

``--scripted`` runs a fixed walkthrough with no provider and no spend: it proves the whole
apparatus -- real Odoo, real Chromium, the real product turn -- without asserting anything
about a model.

    python -m web_operation_agent_odoo.demo --scripted --headless
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web_operation_agent.application import bind_application
from web_operation_agent.egress import ProviderEgressPolicy
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.identity import PrincipalContext
from web_operation_agent.live import LiveConversationRunner, LiveProductSession
from web_operation_agent.model import ChatModel, ProviderError, SpendMeter, load_env
from web_operation_agent.product import (AgentTurnConfig, AgentTurnExecution,
                                         AgentTurnExecutor)

from ..adapter import OdooApplicationAdapter

_MODEL_ENV_KEYS = ("AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_API_KEY",
                   "AGENT_OPENAI_MODEL_PRO", "AGENT_PROVIDER")
_PRINCIPAL_LIFETIME_S = 900
#: The walkthrough the scripted mode performs: read the typed API, then open exactly the
#: route that result authorised. Nothing here is Odoo trivia -- it is the product's contract.
_SCRIPTED_TASK = "Open the customer record for Acme Corporation."
_SCRIPTED_REPLIES: tuple[tuple[str, dict], ...] = (
    ("load_skill", {"skill_id": "odoo.sales.customers-query"}),
    ("query_customers", {"name_contains": "Acme"}),
    ("goto", {"url": "/odoo/customers/10"}),
    ("wait_for", {"text": "Acme"}),
    ("finish", {"done": True, "why": "Opened Acme Corporation's current customer record."}),
)


class ScriptedOdooChat:
    """Deterministic apparatus probe; it never contacts a provider or a spend ledger.

    It still carries an `egress_policy`. The composition gate is deliberately strict -- a chat
    with no declared egress vocabulary is refused rather than assumed harmless -- so a test
    double that stands in for the provider has to say what its boundary covers, exactly as
    the real client does. Declaring it here costs nothing and keeps the gate fail-closed.
    """

    model = "scripted-odoo/no-provider"

    def __init__(self, replies: tuple[tuple[str, dict], ...] = _SCRIPTED_REPLIES, *,
                 egress_policy: ProviderEgressPolicy | None = None):
        self.replies = replies
        self.egress_policy = egress_policy or ProviderEgressPolicy()
        self.calls = 0

    def complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                 max_tokens: int | None = None, options: dict | None = None,
                 timeout_s: float | None = None) -> dict[str, Any]:
        self.calls += 1
        if self.calls > len(self.replies):
            name, arguments = "finish", {"done": False, "why": "Script exhausted."}
        else:
            name, arguments = self.replies[self.calls - 1]
        return {"tool_calls": [{
            "id": f"scripted-call-{self.calls}",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]}

    def complete_cancellable(self, messages: list[dict], tools: list[dict] | None,
                             run_id: str, *, cancelled, max_tokens: int | None = None,
                             options: dict | None = None,
                             timeout_s: float | None = None) -> dict[str, Any]:
        if not callable(cancelled):
            raise TypeError("cancelled must be callable")
        if cancelled():
            raise ProviderError("the demo task was cancelled",
                                code="provider_cancelled", retryable=False)
        return self.complete(messages, tools, run_id, max_tokens=max_tokens,
                             options=options, timeout_s=timeout_s)

    def usage(self) -> dict:
        return {"mode": "scripted_apparatus", "provider_calls": 0,
                "scripted_decisions": self.calls, "cost_cny": 0.0}


@dataclass(frozen=True, slots=True)
class OdooDemoConfig:
    base_url: str
    api_key_path: Path
    storage_state_path: Path
    page_controller_module: str
    scripted: bool = False
    headless: bool = False
    tasks: tuple[str, ...] = ()
    output_dir: Path = Path(tempfile.gettempdir()) / "web-operation-agent-odoo"
    model_env: Path | None = None
    model: str | None = None
    entry_path: str = "/odoo/customers"
    #: An optional version-bound Odoo source tree. Supplying it is what turns source
    #: affordances on; without it the turn runs on runtime facts alone.
    source_root: Path | None = None
    source_release: str = ""
    #: The scripted walkthrough. A caller overrides both together -- a script that answers a
    #: different task than the one submitted is the one mistake this apparatus can make
    #: without any test noticing.
    scripted_task: str = _SCRIPTED_TASK
    scripted_replies: tuple[tuple[str, dict], ...] = _SCRIPTED_REPLIES
    keep_open_seconds: float = 3.0
    turn_timeout_s: float = 120.0

    def __post_init__(self) -> None:
        for name in ("api_key_path", "storage_state_path", "output_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser())
        if self.source_root is not None:
            object.__setattr__(self, "source_root", Path(self.source_root).expanduser())
            if not self.source_release:
                raise ValueError("a source root must be bound to an exact release")
        if self.model_env is not None:
            object.__setattr__(self, "model_env", Path(self.model_env).expanduser())
        if self.scripted and self.tasks:
            raise ValueError("scripted mode runs one fixed walkthrough and takes no --task")
        if self.headless and not (self.tasks or self.scripted):
            raise ValueError("headless mode requires --task or --scripted")
        if (not math.isfinite(self.keep_open_seconds) or self.keep_open_seconds < 0
                or not math.isfinite(self.turn_timeout_s) or self.turn_timeout_s <= 0):
            raise ValueError("demo timeouts are invalid")


def _chat(config: OdooDemoConfig, run_dir: Path, adapter: OdooApplicationAdapter):
    # The adapter decides the credential vocabulary; the composition only forwards it.
    # `AgentTurnExecutor` refuses to compose without it, for the scripted client too.
    egress = ProviderEgressPolicy(
        session_handoff_keys=adapter.ports.routes.session_handoff_query_keys)
    if config.scripted:
        return ScriptedOdooChat(config.scripted_replies, egress_policy=egress), None
    loaded = load_env(config.model_env) if config.model_env is not None else {}
    env = {key: value for key, value in loaded.items() if key in _MODEL_ENV_KEYS}
    for key in _MODEL_ENV_KEYS:
        if os.environ.get(key):
            env[key] = os.environ[key]
    meter = SpendMeter(ledger=run_dir / "model-spend.jsonl",
                       batch_id=f"odoo-demo-{run_dir.name}")
    return ChatModel(meter, env=env, model=config.model, max_completion_tokens=2_048,
                     timeout=max(1, int(config.turn_timeout_s)),
                     egress_policy=egress), meter


def _source_candidate_counts(ledger_path: Path) -> list[int]:
    """Candidates per delivery, read back from the audit ledger the turn just wrote."""
    if not ledger_path.exists():
        return []
    counts = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") == "source_affordances_delivered":
            counts.append(len(row.get("candidates") or []))
    return counts


def _outcome_json(outcome) -> dict:
    if outcome.error:
        return {"turn": outcome.turn, "task": outcome.task, "error": outcome.error}
    execution = outcome.result
    if not isinstance(execution, AgentTurnExecution):
        raise TypeError("the Odoo demo received an unexpected turn result")
    return {
        "turn": outcome.turn,
        "task": outcome.task,
        "status": execution.status.value,
        "terminal_reason": execution.terminal_reason,
        "answer": execution.user_answer,
        "steps": execution.agent.steps_used,
        "page_actions": execution.agent.page_actions,
    }


def _assert_session_unchanged(adapter: OdooApplicationAdapter, context,
                              expected_binding: str) -> None:
    import hmac
    current = adapter.page_session_binding(context.storage_state())
    if not hmac.compare_digest(current, expected_binding):
        raise RuntimeError("the Odoo browser session changed; start a new Agent session")


def _run_browser(config: OdooDemoConfig, live: LiveProductSession,
                 conversation: LiveConversationRunner, *,
                 adapter: OdooApplicationAdapter,
                 expected_binding: str) -> tuple[list[dict], str, str]:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    records: list[dict] = []
    last_url = last_text = ""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        try:
            context = browser.new_context(
                storage_state=str(config.storage_state_path),
                viewport={"width": 1440, "height": 1000})
            try:
                context.add_init_script(script=live.bootstrap_script)
                page = context.new_page()
                page.goto(config.base_url.rstrip("/") + config.entry_path,
                          wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_function("window.__WOA_RUNTIME_READY__ !== undefined",
                                       timeout=60_000)
                page.evaluate(
                    "async () => { await window.__WOA_RUNTIME_READY__; return true; }")
                page.locator("#woa-assistant").wait_for(state="visible", timeout=60_000)
                _assert_session_unchanged(adapter, context, expected_binding)
                print(json.dumps({
                    "event": "odoo_demo_ready",
                    "mode": "scripted_apparatus" if config.scripted else "provider",
                    "url": page.url,
                }, sort_keys=True), flush=True)
                tasks = config.tasks or (
                    (config.scripted_task,) if config.scripted else ())
                if tasks:
                    for task in tasks:
                        page.evaluate(
                            "task => window.__WOA_RUNTIME_READY__.then(runtime => "
                            "runtime.submitTask(task))", task)
                        record = _outcome_json(
                            conversation.next_outcome(timeout_s=config.turn_timeout_s))
                        records.append(record)
                        print(json.dumps(record, ensure_ascii=False, sort_keys=True),
                              flush=True)
                        _assert_session_unchanged(adapter, context, expected_binding)
                    last_url = page.evaluate("() => location.href")
                    last_text = page.locator("body").inner_text()[:2_000]
                    if config.keep_open_seconds and not config.headless:
                        page.wait_for_timeout(int(config.keep_open_seconds * 1000))
                else:
                    try:
                        while not page.is_closed():
                            for outcome in conversation.outcomes_nowait():
                                record = _outcome_json(outcome)
                                records.append(record)
                                print(json.dumps(record, ensure_ascii=False,
                                                 sort_keys=True), flush=True)
                            page.wait_for_timeout(250)
                    except KeyboardInterrupt:
                        pass
                    except PlaywrightError:
                        if not page.is_closed():
                            raise
                    if not page.is_closed():
                        last_url = page.evaluate("() => location.href")
                        last_text = page.locator("body").inner_text()[:2_000]
            finally:
                context.close()
        finally:
            browser.close()
    return records, last_url, last_text


def run_demo(config: OdooDemoConfig) -> dict:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("the Odoo demo requires Python 3.12")
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = config.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)

    adapter = OdooApplicationAdapter(
        base_url=config.base_url, api_key_path=config.api_key_path,
        page_controller_module=config.page_controller_module)
    bound = bind_application(adapter)
    health = adapter.health()
    if not health["passed"]:
        raise RuntimeError("the Odoo deployment is not reachable")
    principal: PrincipalContext = adapter.bind_browser_session(
        config.storage_state_path,
        delegation_id=f"odoo-apikey-{uuid.uuid4().hex}",
        lifetime_s=_PRINCIPAL_LIFETIME_S)
    expected_binding = adapter.page_session_binding(config.storage_state_path)
    # One compile per deployment release, not per turn: the capability is a value, and the
    # four session settings it carries only make sense together.
    capability = adapter.source_capability(config.source_root,
                                           release=config.source_release)
    chat, meter = _chat(config, run_dir, adapter)
    holder: dict[str, LiveConversationRunner] = {}

    def runner_factory(channel):
        executor = AgentTurnExecutor(
            channel=channel,
            config=AgentTurnConfig(
                supported_scope=(
                    "Read current Odoo records through the typed API and open only an exact "
                    "route returned by that API. Do not modify records or submit forms."),
                ledger_path=run_dir / "audit.jsonl",
                step_budget=10,
                wall_budget_s=max(1, int(config.turn_timeout_s)),
                transport_timeout_s=min(30, config.turn_timeout_s),
                disabled_actions=frozenset(
                    {"click", "fill", "select", "submit", "back"}),
                scope_first_policy=True,
                policy_block_limit=3,
                plain_business_answer_min_page_actions=0,
                require_identity=True,
                source_affordance_mode=capability.source_affordance_mode,
                max_user_answer_chars=8_000,
            ),
            chat=chat,
            application=bound,
            control=ReadOnlyControl(),
            source_map=capability.source_map,
            run_id_factory=lambda _message: "odoo-" + uuid.uuid4().hex[:16],
        )
        conversation = LiveConversationRunner(channel, executor=executor)
        holder["conversation"] = conversation
        return conversation

    live = LiveProductSession(
        allowed_origin=bound.descriptor.origin,
        page_controller_module=bound.descriptor.page_controller_module,
        runner_factory=runner_factory,
        navigation_mode=bound.descriptor.navigation_mode,
        sensitive_query_keys=bound.ports.routes.session_handoff_query_keys,
        principal=principal,
    ).start()
    try:
        records, last_url, last_text = _run_browser(
            config, live, holder["conversation"], adapter=adapter,
            expected_binding=expected_binding)
    finally:
        live.close()
        live.wait(timeout_s=10)

    usage = (chat.usage() if isinstance(chat, ScriptedOdooChat)
             else meter.report() if meter is not None else {})
    candidates = _source_candidate_counts(run_dir / "audit.jsonl")
    summary = {
        "schema_version": 1,
        "application": "odoo",
        "server_version": health.get("server_version", ""),
        "mode": "scripted_apparatus" if config.scripted else "provider",
        "identity_binding": "principal_bound",
        "principal_expires_at": principal.expires_at,
        "source_affordance_mode": capability.source_affordance_mode,
        "source_relations": (len(capability.source_map.relations)
                             if capability.source_map is not None else 0),
        # How many cited candidates the session actually delivered. Zero with a compiled map
        # is the failure this number exists to catch: it is what the product path did for as
        # long as the entry observation was assigned without deriving its scopes.
        "source_candidates_delivered": candidates,
        "turns": records,
        "last_url": last_url,
        "last_page_text": last_text,
        "model_usage": usage,
        "output": str(run_dir),
    }
    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    for private_path in (run_dir / "audit.jsonl", run_dir / "model-spend.jsonl",
                         summary_path):
        if private_path.exists():
            private_path.chmod(0o600)
    return summary


def _env_default(name: str, fallback: str = "") -> str:
    return os.environ.get(name, fallback)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web-operation-agent-odoo-demo",
        description="Run the embedded Agent against a live Odoo 18 deployment.",
        epilog=("Without --scripted, set AGENT_OPENAI_BASE_URL, AGENT_OPENAI_API_KEY and "
                "AGENT_OPENAI_MODEL_PRO. Scripted mode checks the apparatus only."))
    parser.add_argument("--base-url", default=_env_default(
        "WOA_ODOO_BASE_URL", "http://127.0.0.1:8069"))
    parser.add_argument("--api-key", default=_env_default("WOA_ODOO_API_KEY_FILE"),
                        help="0600 JSON file with database, login and api_key")
    parser.add_argument("--storage-state", default=_env_default("WOA_ODOO_STORAGE_STATE"),
                        help="Playwright storage state for a signed-in Odoo session")
    parser.add_argument("--page-controller-module", default=_env_default(
        "WOA_ODOO_PAGE_CONTROLLER_MODULE",
        "http://127.0.0.1:8069/woa_runtime/static/src/page-controller.js"))
    parser.add_argument("--entry-path", default="/odoo/customers")
    parser.add_argument("--source-root", default=_env_default("WOA_ODOO_SOURCE_ROOT"),
                        help="an Odoo source tree matching the deployed release")
    parser.add_argument("--source-release", default=_env_default("WOA_ODOO_SOURCE_RELEASE"),
                        help="the exact release identity the source tree is bound to")
    parser.add_argument("--scripted", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--output-dir", default=str(
        Path(tempfile.gettempdir()) / "web-operation-agent-odoo"))
    parser.add_argument("--model-env")
    parser.add_argument("--model")
    parser.add_argument("--keep-open-seconds", type=float, default=3.0)
    parser.add_argument("--turn-timeout", type=float, default=120.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.api_key or not args.storage_state:
            raise ValueError(
                "--api-key and --storage-state are required (see the package README)")
        summary = run_demo(OdooDemoConfig(
            base_url=args.base_url,
            api_key_path=Path(args.api_key),
            storage_state_path=Path(args.storage_state),
            page_controller_module=args.page_controller_module,
            entry_path=args.entry_path,
            source_root=Path(args.source_root) if args.source_root else None,
            source_release=args.source_release,
            scripted=args.scripted,
            headless=args.headless,
            tasks=tuple(args.task),
            output_dir=Path(args.output_dir),
            model_env=Path(args.model_env) if args.model_env else None,
            model=args.model,
            keep_open_seconds=args.keep_open_seconds,
            turn_timeout_s=args.turn_timeout,
        ))
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 — the CLI reports, it does not classify
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            message = "Chromium is unavailable; run: python -m playwright install chromium"
        print(json.dumps({"event": "odoo_demo_error",
                          "error": f"{type(exc).__name__}: {message}"},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
