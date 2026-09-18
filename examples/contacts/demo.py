"""The deployment composition: what a host wires together to serve one turn.

Identity, the mutation journal, the control plane, the gateway and the browser session
belong to the host, not to the adapter. A real deployment replaces the scripted chat with a
provider and the synthetic application with its own; the rest is the shape a composition
takes.

Run it from a checkout:  python -m examples.contacts.demo --help
"""
from __future__ import annotations

import argparse
import hashlib
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
from examples.contacts.app import (ACTORS_BY_ID, DEMO_SESSION_COOKIE,
                                   MinimalContactsApplication)
from web_operation_agent.application import bind_application
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.identity import bind_matching_identity
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
    ("finish", {"outcome": "acted"}),
)
WRITE_WALKTHROUGH: tuple[tuple[str, dict], ...] = (
    ("load_skill", {"skill_id": "minimal.contacts-create"}),
    ("list_contacts", {"name_contains": "Radia"}),
    ("create_contact", {"name": "Radia Perlman", "email": "radia@example.test"}),
    ("goto", {"url": "/contacts/radia-perlman"}),
    ("finish", {"outcome": "acted"}),
)


class ScriptedDemoChat:
    """Deterministic apparatus probe; it never contacts a provider or a spend ledger."""

    model = "scripted-demo/no-provider"

    def __init__(self, replies: tuple[tuple[str, dict], ...] = READ_WALKTHROUGH):
        self.calls = 0
        self.replies = replies
        #: Exactly what reached the "model": every message and tool schema, per decision.
        #: Kept in memory for qualification, which checks what a model would have seen.
        self.seen: list[str] = []

    def complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                 max_tokens: int | None = None, options: dict | None = None,
                 timeout_s: float | None = None) -> dict[str, Any]:
        self.calls += 1
        self.seen.append(json.dumps({"messages": messages, "tools": tools},
                                    ensure_ascii=False, default=str))
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
    #: Qualification-only approval behavior.  The public CLI keeps ``approved``; tests use
    #: the other values to drive the same visible card without a privileged server shortcut.
    approval_decision: str = "approved"
    approval_ttl_s: int = 120
    write_fault: str = ""
    replay_approval: bool = False
    #: Qualification-only: which synthetic user signs in. The public CLI keeps the writer.
    actor: str = "ada-lovelace"
    #: Qualification-only fault injection: the scopes a *misconfigured* integration would
    #: put on the principal, so a test can show the application's own permissions still
    #: refuse when the framework's scope check has been handed too much.
    forged_scopes: tuple[str, ...] = ()
    #: Qualification-only: read the panel after the last turn, then reload the page and
    #: read what the server replays into it.
    capture_panel: bool = False

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
        if self.approval_decision not in {"approved", "denied", "cancelled", "expired"}:
            raise ValueError("approval_decision is invalid")
        if (isinstance(self.approval_ttl_s, bool)
                or not isinstance(self.approval_ttl_s, int)
                or not 1 <= self.approval_ttl_s <= 600):
            raise ValueError("approval_ttl_s must be an integer from 1 to 600")
        if self.write_fault not in MinimalContactsApplication.WRITE_FAULTS:
            raise ValueError("write_fault is invalid")
        if self.replay_approval and self.approval_decision != "approved":
            raise ValueError("only an approved decision can be replayed")
        if self.actor not in ACTORS_BY_ID:
            raise ValueError("actor is not a synthetic demo user")
        if not isinstance(self.forged_scopes, tuple) or any(
                not isinstance(scope, str) for scope in self.forged_scopes):
            raise ValueError("forged_scopes must be a tuple of scope names")


#: The panel's own approve control, inside the assistant's shadow root. There is no other
#: way to approve: the decision has to come from the page session that holds the lease, and
#: an integration that found a way around that would be testing a product it does not ship.
#: The panel's own approve control. Playwright pierces the open shadow root, and its click
#: is a real one: the card refuses an event a script synthesised, which is precisely the
#: case this walkthrough must not be allowed to take.
_APPROVE_CONTROL = '[data-testid="woa-approve"]'
_DENY_CONTROL = '[data-testid="woa-deny"]'
#: Panel states in which a turn is still moving; anything else has settled.
_ACTIVE_PANEL_STATES = ("connecting", "queued", "observing", "acting", "stopping",
                        "reconnecting")


def _approve_in_page(page, timeout_s: float) -> None:
    """Click approve, as the person at the keyboard would, once the panel asks."""
    control = page.locator(_APPROVE_CONTROL)
    control.wait_for(state="visible", timeout=int(timeout_s * 1000))
    # The card also refuses a press it cannot prove was visible long enough to read. That
    # dwell is the point, so the walkthrough waits it out rather than working around it.
    page.wait_for_timeout(700)
    control.click()


def _decide_in_page(page, config: DemoConfig) -> dict:
    """Drive the visible approval card and return the exact user-visible proposal."""
    control = page.locator(_APPROVE_CONTROL)
    control.wait_for(state="visible", timeout=int(config.turn_timeout_s * 1000))
    card = control.locator("xpath=ancestor::*[@role='alertdialog']")
    visible = {"text": card.inner_text(), "decision": config.approval_decision}
    if config.approval_decision == "cancelled":
        page.evaluate("() => window.__WOA_RUNTIME_READY__.then(runtime => runtime.cancelTask())")
        return visible
    if config.approval_decision == "expired":
        page.wait_for_timeout((config.approval_ttl_s * 1000) + 250)
    else:
        page.wait_for_timeout(700)
    if config.approval_decision == "denied":
        page.locator(_DENY_CONTROL).click()
    else:
        control.click()
    return visible


#: A page-switch confirmation: the assistant asks before leaving the page, with one control.
_NAVIGATION_CARD = 'section.woa-approval[data-kind="navigation"]'


def _confirm_navigation_in_page(page) -> dict:
    """Confirm a page switch the panel asks about, as the person at the keyboard would.

    The card has one control and no decline. What it asked, and how many decline controls
    it offered, are kept so a walkthrough can show both.
    """
    card = page.locator(_NAVIGATION_CARD)
    visible = {"text": card.inner_text(),
               "decline_controls": card.locator(_DENY_CONTROL).count()}
    page.wait_for_timeout(700)
    card.locator(_APPROVE_CONTROL).click()
    page.locator(_NAVIGATION_CARD).wait_for(state="detached", timeout=10_000)
    return visible


def _next_outcome(page, conversation, timeout_s: float, navigations: list[dict]):
    """The turn's outcome, confirming each page switch it asks for on the way."""
    deadline = time.monotonic() + timeout_s
    while True:
        if page.locator(_NAVIGATION_CARD).count():
            navigations.append(_confirm_navigation_in_page(page))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for a completed conversation turn")
        try:
            return conversation.next_outcome(timeout_s=min(0.25, remaining))
        except TimeoutError:
            continue


def _page_text(page) -> str:
    """The application's own text: its main region, or the whole page it served instead."""
    return page.evaluate(
        "() => (document.querySelector('main') || document.body).innerText.slice(0, 2000)")


def _read_panel(page, task: str) -> dict:
    """What the person sees in the panel once the thread has settled on `task`."""
    window = page.locator(".woa-window")
    if not window.is_visible():
        page.locator(".woa-launcher").click()
    window.wait_for(state="visible", timeout=10_000)
    thread = page.locator(".woa-thread")
    thread.get_by_text(task, exact=False).first.wait_for(timeout=10_000)
    page.wait_for_function(
        """active => {
          const root = document.querySelector('#woa-assistant')?.shadowRoot;
          const state = root?.querySelector('.woa-window')?.dataset.state;
          return Boolean(state) && !active.includes(state);
        }""", arg=list(_ACTIVE_PANEL_STATES), timeout=10_000)
    page.wait_for_timeout(300)
    return {"text": thread.inner_text(),
            "state": window.get_attribute("data-state") or "",
            "approval_cards": page.locator(".woa-approval").count()}


def _chat(config: DemoConfig, run_dir: Path, *, runtime_guard=None):
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
                     timeout=max(1, int(config.turn_timeout_s)),
                     runtime_guard=runtime_guard), meter


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
                 conversation: LiveConversationRunner, session_value: str
                 ) -> tuple[list[dict], str, str, list[dict], dict, list[dict]]:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    records: list[dict] = []
    last_url = ""
    last_page_text = ""
    approvals: list[dict] = []
    navigations: list[dict] = []
    panel: dict = {}
    origin = live.grant.allowed_origins[0]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 960})
            try:
                # The user is signed in to the application before the assistant exists,
                # which is the only order a real deployment can have.
                context.add_cookies([{"name": DEMO_SESSION_COOKIE, "value": session_value,
                                      "url": origin}])
                context.add_init_script(script=live.bootstrap_script)
                page = context.new_page()
                page.goto(origin + "/contacts",
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
                        if config.replay_approval:
                            page.evaluate("""() => {
                              window.__woaApprovalPosts = [];
                              const original = window.fetch.bind(window);
                              window.fetch = async (url, options = {}) => {
                                if (String(url).endsWith('/approvals')) {
                                  window.__woaApprovalPosts.push({url: String(url), options: {
                                    method: options.method, headers: {...options.headers},
                                    body: options.body, credentials: options.credentials,
                                    cache: options.cache,
                                  }});
                                }
                                return original(url, options);
                              };
                            }""")
                        page.evaluate(
                            "task => window.__WOA_RUNTIME_READY__.then(runtime => "
                            "runtime.submitTask(task))", task)
                        if config.allow_mutations and config.scripted:
                            approvals.append(_decide_in_page(page, config))
                        outcome = _next_outcome(
                            page, conversation, config.turn_timeout_s, navigations)
                        if config.replay_approval:
                            replay = page.evaluate("""async () => {
                              const item = window.__woaApprovalPosts?.[0];
                              if (!item) return {found: false};
                              const response = await fetch(item.url, item.options);
                              return {found: true, status: response.status,
                                      payload: await response.json()};
                            }""")
                            approvals[-1]["replay"] = replay
                        record = _outcome_json(outcome)
                        records.append(record)
                        print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)
                    # Playwright may retain its prior URL briefly after history.pushState even
                    # though the document and runtime already observe the new route.
                    last_url = page.evaluate("() => location.href")
                    last_page_text = _page_text(page)
                    if config.capture_panel:
                        panel["live"] = _read_panel(page, tasks[-1])
                        # A full document load: the panel keeps no timeline of its own, so
                        # whatever it shows now is what the server replayed to it.
                        page.reload(wait_until="domcontentloaded", timeout=30_000)
                        page.wait_for_function("window.__WOA_RUNTIME_READY__ !== undefined",
                                               timeout=30_000)
                        page.evaluate(
                            "async () => { await window.__WOA_RUNTIME_READY__; return true; }")
                        panel["replayed"] = _read_panel(page, tasks[-1])
                        panel["page_text_after_reload"] = _page_text(page)
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
                    last_page_text = (_page_text(page)
                                      if not page.is_closed() else last_page_text)
            finally:
                context.close()
        finally:
            browser.close()
    return records, last_url, last_page_text, approvals, panel, navigations


def workspace_digest(records) -> str:
    """A content-free fingerprint of one workspace's records, for before/after checks."""
    encoded = json.dumps(sorted(records, key=lambda item: item["id"]), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def run_demo(config: DemoConfig) -> dict:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("the minimal demo requires Python 3.12")
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = config.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    actor = ACTORS_BY_ID[config.actor]
    with MinimalContactsApplication(write_fault=config.write_fault) as application:
        journal = MemoryMutationJournal() if config.allow_mutations else None
        adapter = MinimalContactsAdapter(
            application.origin, api_token=actor.token,
            allow_mutations=config.allow_mutations, mutation_journal=journal)
        bound = bind_application(adapter)
        if not adapter.health()["passed"]:
            raise RuntimeError("the synthetic contacts application is not ready")
        # Both halves of the identity, resolved by the application itself and required to
        # agree. A real integration reads the cookie out of the browser context it is about
        # to drive; this demo knows its own synthetic session, which is the only part of
        # this a real application replaces.
        page = adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={actor.session}")
        delegation_id = f"minimal-contacts-{uuid.uuid4().hex}"
        if config.forged_scopes:
            # The fault being injected: an integration that put scopes on the principal the
            # application never granted. Both identities are still resolved and must agree.
            delegated, _granted = adapter.identity._whoami(
                {"Authorization": f"Bearer {actor.token}"}, "delegated_api")
            principal = bind_matching_identity(
                page, delegated, delegation_id=delegation_id, scopes=config.forged_scopes)
        else:
            principal = adapter.ports.identity.bind(page, delegation_id=delegation_id)
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
                    approval_ttl_s=config.approval_ttl_s,
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
            records, last_url, last_page_text, approvals, panel, navigations = _run_browser(
                config, live, holder["conversation"], actor.session)
        finally:
            live.close()
            live.wait(timeout_s=10)

    usage = (chat.usage() if isinstance(chat, ScriptedDemoChat)
             else meter.report() if meter is not None else {})
    journal_rows = []
    audit_path = run_dir / "audit.jsonl"
    if journal is not None and audit_path.exists():
        for event in (json.loads(line) for line in audit_path.read_text().splitlines()):
            if event.get("event") != "business_mutation_proposed":
                continue
            digest = str((event.get("proposal") or {}).get("digest") or "")
            intent = journal.get(digest) if digest else None
            if intent is not None:
                journal_rows.append({
                    "proposal_digest": intent.proposal_digest,
                    "state": intent.state,
                    "business_key": intent.business_key,
                    "reconciliation": json.loads(intent.reconciliation_json),
                })
    summary = {
        "schema_version": 1,
        "application": "minimal.contacts",
        "mode": "scripted_apparatus" if config.scripted else "provider",
        "actor": actor.actor,
        "turns": records,
        "last_url": last_url,
        "last_page_text": last_page_text,
        "approvals": approvals,
        "navigation_confirmations": navigations,
        "application_state": {
            "write_requests": application.write_requests,
            "refused_writes": application.refused_writes,
            "post_requests": application.post_requests,
            "records_created": application.records_created,
            "contact_count": len(application.contacts),
            # Counts and fingerprints only. The summary sits next to the actor's audit
            # trail, and another workspace's records have no business being in either.
            "workspace_counts": {name: len(items)
                                 for name, items in sorted(application.workspaces.items())},
            "workspace_digests": {name: workspace_digest(items)
                                  for name, items in sorted(application.workspaces.items())},
        },
        "model_usage": usage,
        "mutation_journal": journal_rows,
        "output": str(run_dir),
    }
    if panel:
        summary["panel"] = panel
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
    parser.add_argument("--actor", default="ada-lovelace", choices=sorted(ACTORS_BY_ID),
                        help="synthetic user to sign in as; rosalind-franklin may only read")
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
            actor=args.actor,
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
