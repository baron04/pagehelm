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
from typing import Any, Callable
from urllib.parse import urlsplit

from web_operation_agent import python_is_supported, supported_python_text
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.composition.live import LiveConversationRunner, LiveProductSession
from web_operation_agent.modeling.client import ChatModel, ProviderError, SpendMeter, load_env
from web_operation_agent.business.mutation import SQLiteMutationJournal
from web_operation_agent.execution.product import (AgentTurnExecution,
                                         AgentTurnExecutor)

from ..adapter import OdooApplicationAdapter
from ..turn import odoo_turn_config

_MODEL_ENV_KEYS = ("AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_API_KEY",
                   "AGENT_OPENAI_MODEL", "AGENT_PROVIDER")
# The interactive demo is routinely used for a full acceptance session. Fifteen minutes made
# its server-bound principal expire while the person was still testing, even though Odoo's
# own browser login and the Runtime's default session remained valid. Production renewal is a
# deployment concern (P7.3); the bounded demo uses the registry's one-hour session ceiling.
_PRINCIPAL_LIFETIME_S = 60 * 60
#: The walkthrough the scripted mode performs: read the typed API, then open exactly the
#: route that result authorised. Nothing here is Odoo trivia -- it is the product's contract.
_SCRIPTED_TASK = "Open the customer record for Acme Corporation."
_SCRIPTED_REPLIES: tuple[tuple[str, dict], ...] = (
    ("load_skill", {"skill_id": "odoo.sales.customers-query"}),
    ("query_customers", {"name_contains": "Acme Corporation"}),
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
        #: Every tool surface this run was offered, in order. A gate that only checks a tool
        #: *worked* cannot tell "the deployment offers it" from "the script happened to name
        #: something the session still accepts"; this is the surface a model would see.
        self.tool_surfaces: list[list[str]] = []

    def complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                 max_tokens: int | None = None, options: dict | None = None,
                 timeout_s: float | None = None) -> dict[str, Any]:
        self.calls += 1
        self.tool_surfaces.append(sorted(
            tool["function"]["name"] for tool in (tools or [])))
        if self.calls > len(self.replies):
            name, arguments = "finish", {"done": False, "why": "Script exhausted."}
        else:
            name, arguments = self.replies[self.calls - 1]
        if name is None:
            # A plain answer with no tool call. This is how a tool-calling assistant ends a
            # typed read, and the product projects the structured result over it; a script
            # that could only call tools could not reach that path at all.
            return {"content": str(arguments)}
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
    source_commit: str = ""
    #: The scripted walkthrough. A caller overrides both together -- a script that answers a
    #: different task than the one submitted is the one mistake this apparatus can make
    #: without any test noticing.
    scripted_task: str = _SCRIPTED_TASK
    scripted_replies: tuple[tuple[str, dict], ...] = _SCRIPTED_REPLIES
    keep_open_seconds: float = 3.0
    turn_timeout_s: float = 120.0
    #: Enable the adapter's writes. A journal path is required with it -- a mutation with
    #: nowhere to record itself is the one composition this project refuses outright.
    allow_mutations: bool = False
    mutation_journal_path: Path | None = None
    #: Approve the write by clicking the panel's own approve button, in the page, as a user
    #: would. Approving anywhere else would test a path the product does not have: the
    #: decision must come from the page session that holds the lease (see SECURITY.md).
    #: Only ever with `scripted`, so the thing being approved is a fixed proposal.
    approve_in_page: bool = False
    #: Confirm each page switch the panel asks about, as the person who asked for the page
    #: would. Only with `scripted`: a person running the demo confirms it themselves.
    confirm_navigation_in_page: bool = False
    #: Experimental P8.6 surface. Production adapters keep it off until their deployed
    #: modules have passed the model-free form qualification.
    allow_generic_forms: bool = False
    #: Qualification-only TLS seam for a loopback gateway whose private CA is not installed
    #: in Chromium. The caller must enforce loopback; ordinary demos keep verification on.
    browser_ignore_https_errors: bool = False
    #: A dynamic no-provider script may consume server-minted opaque IDs. Fixed walkthroughs
    #: continue to use `scripted_replies`; this seam is not exposed by the demo CLI.
    scripted_chat_factory: Callable[[ProviderEgressPolicy], ScriptedOdooChat] | None = None
    #: Hidden evaluator run after the last turn while the page is still alive. Its result is
    #: an artifact projection, never Agent context.
    final_page_probe: Callable[[Any], dict] | None = None

    def __post_init__(self) -> None:
        for name in ("api_key_path", "storage_state_path", "output_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser())
        if self.source_root is not None:
            object.__setattr__(self, "source_root", Path(self.source_root).expanduser())
            if not self.source_release:
                raise ValueError("a source root must be bound to an exact release")
        elif self.source_commit:
            raise ValueError("a source commit requires a source root")
        if self.model_env is not None:
            object.__setattr__(self, "model_env", Path(self.model_env).expanduser())
        if self.mutation_journal_path is not None:
            object.__setattr__(self, "mutation_journal_path",
                               Path(self.mutation_journal_path).expanduser())
        if self.allow_mutations and self.mutation_journal_path is None:
            raise ValueError("mutations require a durable journal path")
        if self.approve_in_page and not (self.allow_mutations and self.scripted):
            raise ValueError(
                "in-page approval only applies to a scripted mutation walkthrough")
        if self.confirm_navigation_in_page and not self.scripted:
            raise ValueError("in-page page-switch confirmation only applies to a scripted run")
        if self.scripted_chat_factory is not None and not self.scripted:
            raise ValueError("a scripted chat factory requires scripted mode")
        if self.scripted_chat_factory is not None and not callable(
                self.scripted_chat_factory):
            raise TypeError("scripted_chat_factory must be callable")
        if self.final_page_probe is not None and not callable(self.final_page_probe):
            raise TypeError("final_page_probe must be callable")
        if self.final_page_probe is not None and not self.scripted:
            raise ValueError("a final page probe requires scripted qualification mode")
        if (not isinstance(self.allow_generic_forms, bool)
                or not isinstance(self.browser_ignore_https_errors, bool)):
            raise ValueError("demo form and TLS flags must be booleans")
        if self.browser_ignore_https_errors and not self.scripted:
            raise ValueError("ignoring browser HTTPS errors requires scripted qualification")
        if self.browser_ignore_https_errors and urlsplit(self.base_url).hostname not in {
                "127.0.0.1", "::1", "localhost"}:
            raise ValueError(
                "ignoring browser HTTPS errors is restricted to a loopback qualification")
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
        if config.scripted_chat_factory is not None:
            chat = config.scripted_chat_factory(egress)
            if not isinstance(chat, ScriptedOdooChat):
                raise TypeError("scripted_chat_factory must return ScriptedOdooChat")
            return chat, None
        return ScriptedOdooChat(config.scripted_replies, egress_policy=egress), None
    # The same file the rest of the configuration comes from, unless one was named. A
    # provider credential that had to be exported by hand while every other setting came
    # from `.env` is the kind of inconsistency that reads as a broken command.
    loaded = load_env(config.model_env) if config.model_env is not None else _dotenv()
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


def _runtime_source_retrievals(ledger_path: Path) -> list[dict]:
    """Every cited source lookup this turn made, read back from its own audit ledger.

    The citation keys are carried out whole (`path:start-end`), because "a lookup happened"
    and "the lookup cited bytes that are still there" are different claims and only the
    second one is worth anything. The queries themselves are not carried: they are user-
    derived text, and the gate has no reason to publish them.
    """
    if not ledger_path.exists():
        return []
    retrievals = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") != "runtime_source_retrieved":
            continue
        evidence = row.get("evidence") or []
        retrievals.append({
            "lines": len(evidence),
            "citations": row.get("citations", 0),
            "source_digest": row.get("source_digest", ""),
            # `_emit` prefixes every evidence line with `[path:start-end]`, so the citation a
            # model was shown is recoverable from the ledger without trusting the retriever.
            "citation_keys": sorted({line[1:line.index("]")] for line in evidence
                                     if line.startswith("[") and "]" in line}),
            "licensed_view_routes": row.get("licensed_view_routes") or [],
        })
    return retrievals


#: The audit events a gate reads back to say what a turn actually did with the application.
#: Read from the ledger rather than from the model's prose: a turn that *said* it created an
#: invoice and one that did are indistinguishable in an answer string.
_AUDIT_PROJECTIONS = ("business_tool_result", "business_mutation_proposed",
                      "business_mutation_result", "business_mutation_rejected",
                      "business_mutation_denied", "business_mutation_indeterminate",
                      "business_tool_rejected")


def _audit_projection(ledger_path: Path) -> list[dict]:
    """What each typed tool call did, in order, with its own completeness signals."""
    if not ledger_path.exists():
        return []
    rows = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") not in _AUDIT_PROJECTIONS:
            continue
        audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
        rows.append({key: value for key, value in {
            "event": row.get("event"),
            "tool": row.get("tool") or audit.get("operation", ""),
            "operation": audit.get("operation", ""),
            "matched_count": audit.get("matched_count"),
            "source_total": audit.get("source_total"),
            "records_scanned": audit.get("records_scanned"),
            "source_complete": audit.get("source_complete"),
            "collection_ui_path_shows_this_scope": audit.get(
                "collection_ui_path_shows_this_scope"),
            "readback_verified": audit.get("readback_verified"),
            "records_created": audit.get("records_created"),
            "records_written": audit.get("records_written"),
            "reason": str(row.get("reason") or "")[:300],
        }.items() if value is not None and value != ""})
    return rows


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
        "business_control": dict(execution.agent.business_control),
        "write_refusals": execution.agent.write_refusals,
        "success_claim": execution.agent.success_claim,
    }


#: The panel's own approve control, inside the assistant's shadow root. Reached exactly as
#: a user reaches it: there is no other way to approve, because the decision has to come
#: from the page session that holds the lease.
#: The panel's own approve control. Playwright pierces the open shadow root, and its click
#: is a real one: the card refuses an event a script synthesised, which is precisely the
#: case this walkthrough must not be allowed to take.
_APPROVE_CONTROL = 'section.woa-approval[data-kind="write"] [data-testid="woa-approve"]'
#: A page-switch confirmation: the assistant asks before leaving the page. Its one control
#: carries the same test id as a write approval, which is why the write control above is
#: scoped to its own kind of card.
_NAVIGATION_CARD = 'section.woa-approval[data-kind="navigation"]'


def _capture(page, shots: Path, name: str) -> str:
    """Save what a person would have seen, next to the ledger that says what happened.

    A demo is the one artifact whose whole job is to be looked at, and this one produced
    nothing to look at: whether the panel rendered could only be settled by a person being
    in front of the window at the time. When the window does not appear -- a run started
    from a non-interactive session puts it on another desktop, and there is no way to
    attach to it afterwards -- there was no evidence at all.

    Never raises. A screenshot that fails must not end a run that is otherwise working, and
    the missing file is itself the report.
    """
    try:
        shots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shots / f"{name}.png"))
        return f"{name}.png"
    except Exception:
        return ""


def _confirm_navigation(page, navigations: list[str] | None) -> None:
    """Confirm a page switch the panel is asking about, if it is asking and this run may."""
    if navigations is None:
        return
    card = page.locator(_NAVIGATION_CARD)
    try:
        if not card.count():
            return
        text = card.inner_text()
        page.wait_for_timeout(700)
        card.locator('[data-testid="woa-approve"]').click()
        card.wait_for(state="detached", timeout=10_000)
    except Exception:
        return
    navigations.append(text)


def _await_outcome(conversation: LiveConversationRunner, page, shots: Path, *,
                   timeout_s: float, taken: list[str], turn: int,
                   navigations: list[str] | None = None):
    """Wait for the turn, and photograph the approval card if one appears while waiting.

    The wait is the same one it replaces -- same total budget, same `TimeoutError` -- but
    split into short polls so the card, which is the one thing a person is here to see, is
    captured at the moment it is on screen rather than after the turn has moved on.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for a completed conversation turn")
        try:
            return conversation.next_outcome(timeout_s=min(0.5, remaining))
        except TimeoutError:
            pass
        _confirm_navigation(page, navigations)
        name = f"turn-{turn}-approval"
        if f"{name}.png" not in taken and _approval_on_screen(page):
            shot = _capture(page, shots, name)
            if shot:
                taken.append(shot)


def _approval_on_screen(page) -> bool:
    try:
        return page.locator(_APPROVE_CONTROL).is_visible()
    except Exception:
        return False


def _approve_in_page(page, timeout_s: float, navigations: list[str] | None = None) -> None:
    """Wait for the panel to ask, then click approve, as the person at the keyboard would.

    A scripted run still has to go through the real approval: the write path refuses a
    decision that did not come from the page session holding the lease, so approving any
    other way would qualify an apparatus the product does not ship.
    """
    control = page.locator(_APPROVE_CONTROL)
    deadline = time.monotonic() + timeout_s
    while not control.is_visible():
        # A page switch asked for on the way is confirmed; the write is what this waits for.
        _confirm_navigation(page, navigations)
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for the write approval card")
        page.wait_for_timeout(100)
    # The card also refuses a press it cannot prove was visible long enough to read. That
    # dwell is the point, so the walkthrough waits it out rather than working around it.
    page.wait_for_timeout(700)
    control.click()


def _assert_session_unchanged(adapter: OdooApplicationAdapter, context,
                              expected_binding: str) -> None:
    import hmac
    current = adapter.page_session_binding(context.storage_state())
    if not hmac.compare_digest(current, expected_binding):
        raise RuntimeError("the Odoo browser session changed; start a new Agent session")


def _notify_turn_boundary(hook: Callable[[int, dict], None] | None, *, turn: int,
                          total_turns: int, record: dict) -> None:
    """Give qualification code one safe seam between completed product turns.

    The record is copied so a fixture hook cannot rewrite the user-visible outcome. The
    final turn has no following observation to qualify, so calling the hook there would
    only mutate the fixture after the test had already ended.
    """
    if hook is not None and turn < total_turns:
        hook(turn, dict(record))


def _run_browser(config: OdooDemoConfig, live: LiveProductSession,
                 conversation: LiveConversationRunner, *,
                 adapter: OdooApplicationAdapter, expected_binding: str,
                 shots: Path,
                 turn_boundary_hook: Callable[[int, dict], None] | None = None,
                 ) -> tuple[list[dict], str, str, list[str], int, dict]:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    records: list[dict] = []
    taken: list[str] = []
    document_loads = 0
    last_url = last_text = ""
    final_probe: dict = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        try:
            context = browser.new_context(
                storage_state=str(config.storage_state_path),
                ignore_https_errors=config.browser_ignore_https_errors,
                viewport={"width": 1440, "height": 1000})
            try:
                context.add_init_script(script=live.bootstrap_script)
                # The panel remembers whether a viewer left it open, in `localStorage`. A
                # demo launches a throwaway profile, so there is never anything to remember
                # and every run starts collapsed behind a small unlabelled launcher -- which
                # reads, to someone opening the window for the first time, as nothing having
                # loaded at all. Seeding the remembered state is a demo concern only: a real
                # deployment has a profile, and honouring the viewer's own choice is right
                # there.
                context.add_init_script(
                    script="try { localStorage.setItem('woa.panel.open.v1', 'true') } "
                           "catch (error) { /* a profile that blocks site data still works */ }")
                page = context.new_page()
                def count_document_load(*_event: object) -> None:
                    nonlocal document_loads
                    document_loads += 1
                page.on("domcontentloaded", count_document_load)
                page.goto(config.base_url.rstrip("/") + config.entry_path,
                          wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_function("window.__WOA_RUNTIME_READY__ !== undefined",
                                       timeout=60_000)
                page.evaluate(
                    "async () => { await window.__WOA_RUNTIME_READY__; return true; }")
                page.locator("#woa-assistant").wait_for(state="visible", timeout=60_000)
                _assert_session_unchanged(adapter, context, expected_binding)
                # `wait_for(visible)` returning is what this used to report as "the panel is
                # up". It is not the same claim: a locator can be visible in a window nobody
                # can see. The file is.
                ready_shot = _capture(page, shots, "ready")
                if ready_shot:
                    taken.append(ready_shot)
                print(json.dumps({
                    "event": "odoo_demo_ready",
                    "mode": "scripted_apparatus" if config.scripted else "provider",
                    "url": page.url,
                    "screenshots": str(shots),
                }, sort_keys=True), flush=True)
                tasks = config.tasks or (
                    (config.scripted_task,) if config.scripted else ())
                if tasks:
                    for task in tasks:
                        page.evaluate(
                            "task => window.__WOA_RUNTIME_READY__.then(runtime => "
                            "runtime.submitTask(task))", task)
                        navigations = [] if config.confirm_navigation_in_page else None
                        if config.approve_in_page:
                            _approve_in_page(page, config.turn_timeout_s, navigations)
                        turn = len(records) + 1
                        record = _outcome_json(_await_outcome(
                            conversation, page, shots,
                            timeout_s=config.turn_timeout_s, taken=taken, turn=turn,
                            navigations=navigations))
                        if navigations is not None:
                            record["navigation_confirmations"] = len(navigations)
                        shot = _capture(page, shots, f"turn-{turn}")
                        if shot:
                            taken.append(shot)
                        records.append(record)
                        print(json.dumps(record, ensure_ascii=False, sort_keys=True),
                              flush=True)
                        _assert_session_unchanged(adapter, context, expected_binding)
                        # Qualification code may change an isolated fixture between turns
                        # to prove that historical object context is never reused as current
                        # fact. The CLI never exposes this hook, and the next turn still
                        # enters through the ordinary browser/product path.
                        _notify_turn_boundary(
                            turn_boundary_hook, turn=turn, total_turns=len(tasks),
                            record=record)
                    last_url = page.evaluate("() => location.href")
                    # Odoo's application chrome can exceed 2,000 characters before the
                    # opened record.  Capping there made the qualification claim the record
                    # was absent even after `wait_for` had observed it.  This text never goes
                    # to the model or the public artifact; it only supports local assertions.
                    last_text = page.locator("body").inner_text()[:20_000]
                    if config.keep_open_seconds and not config.headless:
                        page.wait_for_timeout(int(config.keep_open_seconds * 1000))
                else:
                    try:
                        while not page.is_closed():
                            for outcome in conversation.outcomes_nowait():
                                record = _outcome_json(outcome)
                                records.append(record)
                                shot = _capture(page, shots, f"turn-{len(records)}")
                                if shot:
                                    taken.append(shot)
                                print(json.dumps(record, ensure_ascii=False,
                                                 sort_keys=True), flush=True)
                            # The person is typing their own goals here, so the card can
                            # appear at any time; this is the only chance to photograph it.
                            name = f"turn-{len(records) + 1}-approval"
                            if f"{name}.png" not in taken and _approval_on_screen(page):
                                shot = _capture(page, shots, name)
                                if shot:
                                    taken.append(shot)
                            page.wait_for_timeout(250)
                    except KeyboardInterrupt:
                        pass
                    except PlaywrightError:
                        if not page.is_closed():
                            raise
                    if not page.is_closed():
                        last_url = page.evaluate("() => location.href")
                        last_text = page.locator("body").inner_text()[:20_000]
                if config.final_page_probe is not None and not page.is_closed():
                    candidate = config.final_page_probe(page)
                    if not isinstance(candidate, dict):
                        raise TypeError("final_page_probe must return a JSON object")
                    # Refuse a projection that cannot be persisted exactly. This evaluator
                    # is hidden from the Agent, but its artifact still needs a stable shape.
                    final_probe = json.loads(json.dumps(
                        candidate, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False))
            finally:
                context.close()
        finally:
            browser.close()
    return records, last_url, last_text, taken, document_loads, final_probe


def run_demo(config: OdooDemoConfig, *,
             turn_boundary_hook: Callable[[int, dict], None] | None = None) -> dict:
    if not python_is_supported():
        raise RuntimeError(f"the Odoo demo requires Python {supported_python_text()}")
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = config.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)

    journal = (SQLiteMutationJournal(config.mutation_journal_path)
               if config.allow_mutations else None)
    adapter = OdooApplicationAdapter(
        base_url=config.base_url, api_key_path=config.api_key_path,
        page_controller_module=config.page_controller_module,
        allow_generic_forms=config.allow_generic_forms,
        allow_mutations=config.allow_mutations, mutation_journal=journal)
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
                                           release=config.source_release,
                                           deployed_commit=config.source_commit)
    chat, meter = _chat(config, run_dir, adapter)
    holder: dict[str, LiveConversationRunner] = {}

    def runner_factory(channel):
        executor = AgentTurnExecutor(
            channel=channel,
            config=odoo_turn_config(
                adapter=adapter, run_dir=run_dir,
                allow_mutations=config.allow_mutations,
                scripted=config.scripted,
                turn_timeout_s=config.turn_timeout_s,
                source=capability),
            chat=chat,
            application=bound,
            control=ReadOnlyControl(),
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
        records, last_url, last_text, taken, document_loads, final_probe = _run_browser(
            config, live, holder["conversation"], adapter=adapter,
            expected_binding=expected_binding, shots=run_dir / "screens",
            turn_boundary_hook=turn_boundary_hook)
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
        # What the adapter's `runtime_source` declaration actually bought this turn: the
        # cited lookups it made, and the tool surface it was offered. Both were unobservable
        # from a deployment until the capability crossed into the product turn whole.
        "runtime_source_retrievals": _runtime_source_retrievals(run_dir / "audit.jsonl"),
        "tools_offered": sorted({name for surface in
                                 (chat.tool_surfaces if isinstance(chat, ScriptedOdooChat)
                                  else [])
                                 for name in surface}),
        # The demo's own evidence that it had something to show. An empty list after a
        # run that reported ready means the window never rendered.
        "screenshots": list(taken),
        "document_loads": document_loads,
        "document_reloads_after_entry": max(0, document_loads - 1),
        "final_page_probe": final_probe,
        "audit_projection": _audit_projection(run_dir / "audit.jsonl"),
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


#: Read once, so a CLI with a dozen defaults does not read the file a dozen times.
_DOTENV: dict[str, str] | None = None


def _dotenv() -> dict[str, str]:
    """The repository's ignored `.env`, which is where provisioning puts its answers.

    `scripts/qualify.sh` already reads this file for exactly these keys, and
    `deploy provision` prints them into it. Reading only the process environment meant the
    one command a person runs by hand was the one that made them export a dozen variables
    first -- the friction the provisioning step exists to remove.
    """
    global _DOTENV
    if _DOTENV is None:
        configured = os.environ.get("WOA_MODEL_ENV", "").strip()
        # demo/demo.py -> demo -> web_operation_agent_odoo -> src -> odoo -> examples -> root
        root = Path(__file__).resolve().parents[5]
        _DOTENV = load_env(Path(configured).expanduser() if configured else root / ".env")
    return _DOTENV


def _env_default(name: str, fallback: str = "") -> str:
    """A default from the environment first, then from that file.

    Process values win, so a deliberate `WOA_ODOO_BASE_URL=… python -m …` still does what
    it says.
    """
    if name in os.environ:
        return os.environ[name]
    return str(_dotenv().get(name, fallback)).strip() or fallback


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web-operation-agent-odoo-demo",
        description="Run the embedded Agent against a live Odoo 18 deployment.",
        epilog=("Without --scripted, set AGENT_OPENAI_BASE_URL, AGENT_OPENAI_API_KEY and "
                "AGENT_OPENAI_MODEL. Scripted mode checks the apparatus only."))
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
    parser.add_argument("--source-commit", default=_env_default("WOA_ODOO_SOURCE_COMMIT"),
                        help="optional Git commit matching the source tree, for local history")
    parser.add_argument("--scripted", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--output-dir", default=str(
        Path(tempfile.gettempdir()) / "web-operation-agent-odoo"))
    parser.add_argument("--model-env")
    parser.add_argument("--model")
    parser.add_argument("--allow-mutations", action="store_true",
                        help="offer the four typed write tools. Every write still needs a "
                             "fresh approval clicked in the page, and a durable journal is "
                             "required, so this opens the approval card rather than the "
                             "writes themselves")
    parser.add_argument("--mutation-journal", default=_env_default("WOA_MUTATION_JOURNAL"),
                        help="where the durable mutation journal lives; required with "
                             "--allow-mutations because a production write without one is "
                             "refused at composition")
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
            source_commit=args.source_commit,
            scripted=args.scripted,
            headless=args.headless,
            allow_mutations=args.allow_mutations,
            mutation_journal_path=(Path(args.mutation_journal)
                                   if args.mutation_journal else
                                   Path(args.output_dir) / "mutations.sqlite3"),
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
