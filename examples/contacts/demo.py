"""The deployment composition: what a host wires together to serve one turn.

Identity, the mutation journal, the control plane, the gateway and the browser session
belong to the host, not to the adapter. A real deployment replaces the scripted chat with a
provider and the synthetic application with its own; the rest is the shape a composition
takes.

Run it from a checkout:  python -m examples.contacts.demo --help
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

from examples.contacts.adapter import MinimalContactsAdapter
from examples.contacts.app import (DEMO_API_TOKEN, DEMO_SESSION_COOKIE,
                                   DEMO_SESSION_VALUE,
                                   MinimalContactsApplication)
from web_operation_agent.application import bind_application
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.live import LiveConversationRunner, LiveProductSession
from web_operation_agent.model import ChatModel, ProviderError, SpendMeter, load_env
from web_operation_agent.mutation import MemoryMutationJournal
from web_operation_agent.product import AgentTurnConfig, AgentTurnExecution, AgentTurnExecutor

_MODEL_ENV_KEYS = (
    "AGENT_OPENAI_BASE_URL",
    "AGENT_OPENAI_API_KEY",
    "AGENT_OPENAI_MODEL",
    "AGENT_PROVIDER",
)


#: What the fixed walkthrough does, read-only and with the write enabled. The write script
#: is what an author reads to see the shape of the chain from the model's side: load the
#: Skill, look first, propose once, then open only the route the result handed back.
READ_WALKTHROUGH: tuple[tuple[str, dict], ...] = (
    ("load_skill", {"skill_id": "minimal.contacts.query"}),
    ("list_contacts", {"name_contains": "Grace Hopper"}),
    ("goto", {"url": "/contacts/grace-hopper"}),
    ("finish", {"done": True, "why": "Opened Grace Hopper's current contact record."}),
)
WRITE_WALKTHROUGH: tuple[tuple[str, dict], ...] = (
    ("load_skill", {"skill_id": "minimal.contacts-create"}),
    ("list_contacts", {"name_contains": "Radia"}),
    ("create_contact", {"name": "Radia Perlman", "email": "radia@example.test"}),
    ("goto", {"url": "/contacts/radia-perlman"}),
    ("finish", {"done": True, "why": "Created and opened Radia Perlman's contact."}),
)


class ScriptedDemoChat:
    """Deterministic apparatus probe; it never contacts a provider or a spend ledger."""

    model = "scripted-demo/no-provider"

    def __init__(self, replies: tuple[tuple[str, dict], ...] = READ_WALKTHROUGH):
        self.calls = 0
        self.replies = replies

    def complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                 max_tokens: int | None = None, options: dict | None = None,
                 timeout_s: float | None = None) -> dict[str, Any]:
        self.calls += 1
        replies = self.replies
        if self.calls > len(replies):
            name, arguments = "finish", {"done": False, "why": "Script exhausted."}
        else:
            name, arguments = replies[self.calls - 1]
        return {"tool_calls": [{
            "id": f"scripted-call-{self.calls}",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]}

    def complete_cancellable(
            self, messages: list[dict], tools: list[dict] | None, run_id: str, *, cancelled,
            max_tokens: int | None = None, options: dict | None = None,
            timeout_s: float | None = None) -> dict[str, Any]:
        if not callable(cancelled):
            raise TypeError("cancelled must be callable")
        if cancelled():
            raise ProviderError("the demo task was cancelled", code="provider_cancelled",
                                retryable=False)
        return self.complete(messages, tools, run_id, max_tokens=max_tokens,
                             options=options, timeout_s=timeout_s)

    def usage(self) -> dict:
        return {"mode": "scripted_apparatus", "provider_calls": 0,
                "scripted_decisions": self.calls, "cost_cny": 0.0}


@dataclass(frozen=True, slots=True)
class DemoConfig:
    scripted: bool = False
    headless: bool = False
    tasks: tuple[str, ...] = ()
    output_dir: Path = Path(tempfile.gettempdir()) / "web-operation-agent-minimal"
    model_env: Path | None = None
    model: str | None = None
    keep_open_seconds: float = 3.0
    turn_timeout_s: float = 90.0
    #: Offer the write. Off by default: the read-only composition is what most integrations
    #: start from, and a template that enabled writes by default would teach the wrong
    #: default.
    allow_mutations: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_dir", Path(self.output_dir).expanduser())
        if self.model_env is not None:
            object.__setattr__(self, "model_env", Path(self.model_env).expanduser())
        if self.scripted and self.tasks:
            raise ValueError("scripted mode uses one fixed walkthrough and does not accept --task")
        if self.headless and not (self.tasks or self.scripted):
            raise ValueError("headless mode requires --task or --scripted")
        if (not math.isfinite(self.keep_open_seconds) or self.keep_open_seconds < 0
                or not math.isfinite(self.turn_timeout_s) or self.turn_timeout_s <= 0):
            raise ValueError("demo timeouts are invalid")


#: The panel's own approve control, inside the assistant's shadow root. There is no other
#: way to approve: the decision has to come from the page session that holds the lease, and
#: an integration that found a way around that would be testing a product it does not ship.
#: The panel's own approve control. Playwright pierces the open shadow root, and its click
#: is a real one: the card refuses an event a script synthesised, which is precisely the
#: case this walkthrough must not be allowed to take.
_APPROVE_CONTROL = '[data-testid="woa-approve"]'


def _approve_in_page(page, timeout_s: float) -> None:
    """Click approve, as the person at the keyboard would, once the panel asks."""
    control = page.locator(_APPROVE_CONTROL)
    control.wait_for(state="visible", timeout=int(timeout_s * 1000))
    # The card also refuses a press it cannot prove was visible long enough to read. That
    # dwell is the point, so the walkthrough waits it out rather than working around it.
    page.wait_for_timeout(700)
    control.click()


def _chat(config: DemoConfig, run_dir: Path):
    if config.scripted:
        return ScriptedDemoChat(
            WRITE_WALKTHROUGH if config.allow_mutations else READ_WALKTHROUGH), None
    loaded = load_env(config.model_env) if config.model_env is not None else {}
    env = {key: value for key, value in loaded.items() if key in _MODEL_ENV_KEYS}
    for key in _MODEL_ENV_KEYS:
        if os.environ.get(key):
            env[key] = os.environ[key]
    meter = SpendMeter(ledger=run_dir / "model-spend.jsonl",
                       batch_id=f"minimal-demo-{run_dir.name}")
    return ChatModel(meter, env=env, model=config.model, max_completion_tokens=1024,
                     timeout=max(1, int(config.turn_timeout_s))), meter


def _outcome_json(outcome) -> dict:
    if outcome.error:
        return {"turn": outcome.turn, "task": outcome.task, "error": outcome.error}
    execution = outcome.result
    if not isinstance(execution, AgentTurnExecution):
        raise TypeError("minimal demo received an unexpected turn result")
    return {
        "turn": outcome.turn,
        "task": outcome.task,
        "status": execution.status.value,
        "terminal_reason": execution.terminal_reason,
        "answer": execution.user_answer,
        "steps": execution.agent.steps_used,
        "page_actions": execution.agent.page_actions,
    }


def _run_browser(config: DemoConfig, live: LiveProductSession,
                 conversation: LiveConversationRunner) -> tuple[list[dict], str, str]:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    records: list[dict] = []
    last_url = ""
    last_page_text = ""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 960})
            try:
                context.add_init_script(script=live.bootstrap_script)
                page = context.new_page()
                page.goto(live.grant.allowed_origins[0] + "/contacts",
                          wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_function("window.__WOA_RUNTIME_READY__ !== undefined",
                                       timeout=30_000)
                page.evaluate("async () => { await window.__WOA_RUNTIME_READY__; return true; }")
                page.locator("#woa-assistant").wait_for(state="visible", timeout=30_000)
                print(json.dumps({
                    "event": "minimal_demo_ready",
                    "mode": "scripted_apparatus" if config.scripted else "provider",
                    "url": page.url,
                }, sort_keys=True), flush=True)
                scripted_task = ("Create a contact for Radia Perlman at "
                                 "radia@example.test."
                                 if config.allow_mutations else
                                 "Open Grace Hopper's contact record.")
                tasks = config.tasks or ((scripted_task,) if config.scripted else ())
                if tasks:
                    for task in tasks:
                        page.evaluate(
                            "task => window.__WOA_RUNTIME_READY__.then(runtime => "
                            "runtime.submitTask(task))", task)
                        if config.allow_mutations and config.scripted:
                            _approve_in_page(page, config.turn_timeout_s)
                        outcome = conversation.next_outcome(timeout_s=config.turn_timeout_s)
                        record = _outcome_json(outcome)
                        records.append(record)
                        print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)
                    # Playwright may retain its prior URL briefly after history.pushState even
                    # though the document and runtime already observe the new route.
                    last_url = page.evaluate("() => location.href")
                    last_page_text = page.locator("main").inner_text()[:2_000]
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
                    last_url = (page.evaluate("() => location.href")
                                if not page.is_closed() else last_url)
                    last_page_text = (page.locator("main").inner_text()[:2_000]
                                      if not page.is_closed() else last_page_text)
            finally:
                context.close()
        finally:
            browser.close()
    return records, last_url, last_page_text


def run_demo(config: DemoConfig) -> dict:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("the minimal demo requires Python 3.12")
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = config.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    with MinimalContactsApplication() as application:
        journal = MemoryMutationJournal() if config.allow_mutations else None
        adapter = MinimalContactsAdapter(
            application.origin, api_token=DEMO_API_TOKEN,
            allow_mutations=config.allow_mutations, mutation_journal=journal)
        bound = bind_application(adapter)
        if not adapter.health()["passed"]:
            raise RuntimeError("the synthetic contacts application is not ready")
        # Both halves of the identity, resolved by the application itself and required to
        # agree. A real integration reads the cookie out of the browser context it is about
        # to drive; this demo knows its own synthetic session, which is the only part of
        # this a real application replaces.
        principal = adapter.ports.identity.bind(
            adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={DEMO_SESSION_VALUE}"),
            delegation_id=f"minimal-contacts-{uuid.uuid4().hex}")
        chat, meter = _chat(config, run_dir)
        holder: dict[str, LiveConversationRunner] = {}

        def runner_factory(channel):
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope=(
                        "Read current contacts through the typed API and open only an exact "
                        "route returned by that API. Do not modify records or submit forms."
                        if not config.allow_mutations else
                        "Read current contacts through the typed API, and create at most "
                        "the one contact the user confirms. Do not submit page forms."),
                    ledger_path=run_dir / "audit.jsonl",
                    step_budget=8,
                    wall_budget_s=max(1, int(config.turn_timeout_s)),
                    transport_timeout_s=min(30, config.turn_timeout_s),
                    disabled_actions=frozenset({"click", "fill", "select", "submit", "back"}),
                    scope_first_policy=True,
                    policy_block_limit=2,
                    plain_business_answer_min_page_actions=0,
                    max_user_answer_chars=8_000,
                    require_identity=True,
                    allow_business_mutations=config.allow_mutations,
                    # A synthetic application with an in-memory journal is not a
                    # deployment, and saying so is what keeps the production profile's
                    # durable-journal requirement meaningful.
                    deployment_profile="test" if config.allow_mutations else "production",
                ),
                chat=chat,
                application=bound,
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "minimal-" + uuid.uuid4().hex[:16],
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
            records, last_url, last_page_text = _run_browser(
                config, live, holder["conversation"])
        finally:
            live.close()
            live.wait(timeout_s=10)

    usage = (chat.usage() if isinstance(chat, ScriptedDemoChat)
             else meter.report() if meter is not None else {})
    summary = {
        "schema_version": 1,
        "application": "minimal.contacts",
        "mode": "scripted_apparatus" if config.scripted else "provider",
        "turns": records,
        "last_url": last_url,
        "last_page_text": last_page_text,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web-operation-agent-demo",
        description="Run the self-contained Minimal Contacts application and embedded Agent.",
        epilog=("Without --scripted, set AGENT_OPENAI_BASE_URL, AGENT_OPENAI_API_KEY and "
                "AGENT_OPENAI_MODEL. Scripted mode checks plumbing only."),
    )
    parser.add_argument("--scripted", action="store_true",
                        help="use the fixed zero-cost apparatus walkthrough")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--task", action="append", default=[],
                        help="submit a task after startup; repeat in provider mode")
    parser.add_argument("--output-dir", default=str(
        Path(tempfile.gettempdir()) / "web-operation-agent-minimal"))
    parser.add_argument("--model-env", help="optional dotenv file with AGENT_OPENAI_* values")
    parser.add_argument("--model", help="provider model override")
    parser.add_argument("--keep-open-seconds", type=float, default=3.0)
    parser.add_argument("--turn-timeout", type=float, default=90.0)
    parser.add_argument("--allow-mutations", action="store_true",
                        help="offer the write tool; the panel still asks before it runs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = DemoConfig(
            scripted=args.scripted,
            headless=args.headless,
            tasks=tuple(args.task),
            output_dir=Path(args.output_dir),
            model_env=Path(args.model_env) if args.model_env else None,
            model=args.model,
            keep_open_seconds=args.keep_open_seconds,
            turn_timeout_s=args.turn_timeout,
            allow_mutations=args.allow_mutations,
        )
        summary = run_demo(config)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            message = "Chromium is unavailable; run: python -m playwright install chromium"
        print(json.dumps({"event": "minimal_demo_error", "error":
                         f"{type(exc).__name__}: {message}"}, ensure_ascii=False,
                         sort_keys=True), file=sys.stderr, flush=True)
        return 2
    return 0
