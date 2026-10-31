"""A read-only goal-directed session on a local yudao deployment, driven in a browser.

The browser is signed in as the evaluation account before the assistant exists (its token
put where the admin UI keeps it), the assistant's Runtime is injected by the bootstrap
script, and each task is submitted through the panel as a person would. The server reads
through `YudaoGoalData` under the same account, bound to the browser's user by yudao's
own answer to "who am I" on both sides.

Model calls are metered into the project's spend ledger under the batch this run names.

    python -m web_operation_agent_yudao.demo --account evaluator --task "我负责的客户有哪些？"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.composition.live import LiveConversationRunner, LiveProductSession
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecution, AgentTurnExecutor
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.modeling.client import ChatModel, SpendMeter, load_env
from web_operation_agent.modeling.egress import ProviderEgressPolicy

from .accounts import add_account_arguments
from .application import YudaoApplication
from .conventions import REFERENCE_CONVENTIONS
from .client import YudaoAccount
from .ui_source import UiSource
from .repository import repository_source
from .turn import yudao_turn_config

_MODEL_ENV_KEYS = ("AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_API_KEY", "AGENT_OPENAI_MODEL",
                   "AGENT_PROVIDER")
_OPERATE_CARD = 'section.woa-approval[data-kind="operate"]'
_APPROVE_CONTROL = '[data-testid="woa-approve"]'
_ADMIN_RENDERED = "() => !!document.querySelector('.el-breadcrumb__item, .v-tags-view__item.is-active')"
#: web-storage-cache's "never expires" (31 Dec 9999), as the admin UI writes it.
_NEVER = 253402300799000


def _signed_in_script(origin: str, account: YudaoAccount, tokens: dict) -> str:
    """Put the browser's own session where the admin UI keeps it, on its origin only."""
    now = int(time.time() * 1000)

    def item(value):
        # The cache stores each value serialized, inside its own serialized record.
        return json.dumps({"c": now, "e": _NEVER, "v": json.dumps(value)})
    entries = {"ACCESS_TOKEN": item(tokens["accessToken"]),
               "REFRESH_TOKEN": item(tokens["refreshToken"]),
               "tenantId": item(int(account.tenant))}
    return ("(() => { if (location.origin !== %s) return;\n" % json.dumps(origin)
            + "".join(f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});\n"
                      for k, v in entries.items()) + "})();")


def _browser_login(origin: str, account: YudaoAccount) -> dict:
    request = urllib.request.Request(
        f"{origin}/admin-api/system/auth/login", method="POST",
        data=json.dumps({"username": account.username, "password": account.password}).encode(),
        headers={"tenant-id": account.tenant, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    if payload.get("code") != 0:
        raise RuntimeError("the browser could not sign in to yudao")
    return payload["data"]


def _next_outcome(page, conversation, timeout_s: float, operations: list, *, write_cards=None,
                  on_write_card=None, started=None, turn_events=None):
    deadline = time.monotonic() + timeout_s
    answered = set()
    first_card = False
    def observed(outcome):
        if turn_events is not None and not outcome.error:
            try:
                _observe_final_result(page, outcome.result, min(15, max(.1, deadline-time.monotonic())), turn_events)
            except Exception:
                # Keep the acknowledged normal outcome even when its UI endpoint is
                # unavailable. Missing measurement is explicit, never a fake timestamp.
                turn_events.incomplete(outcome.result.message.message_id)
        return outcome
    while True:
        # A terminal result can arrive while the panel is still projecting its last
        # card. It takes precedence over another UI decision.
        try:
            return observed(conversation.next_outcome(timeout_s=0))
        except TimeoutError:
            pass
        if page.locator(_OPERATE_CARD).count():
            card = page.locator(_OPERATE_CARD)
            approval_id = card.get_attribute("data-approval-id")
            if not approval_id:
                raise RuntimeError("the panel did not expose the active approval identity")
            if approval_id not in answered:
                answered.add(approval_id)
                operations.append(card.inner_text())
                if turn_events:
                    turn_events.begin_approval_driver(approval_id)
                try:
                    page.wait_for_timeout(700)
                    card.locator(_APPROVE_CONTROL).click()
                except BaseException:
                    if turn_events:
                        turn_events.end_approval_driver(approval_id)
                    raise
        writes = page.locator('section.woa-approval[data-kind="write"]')
        if writes.count() and on_write_card is not None:
            card = writes.first
            approval_id = card.get_attribute("data-approval-id")
            if not approval_id:
                raise RuntimeError("the panel did not expose the active approval identity")
            if approval_id in answered:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for the turn's outcome")
                try:
                    return observed(conversation.next_outcome(timeout_s=min(0.25, remaining)))
                except TimeoutError:
                    continue
            answered.add(approval_id)
            visible = _observe_write_card(page, card, min(15, max(.1, deadline-time.monotonic())))
            if turn_events is not None:
                if not first_card:
                    turn_events.emit("first_write_card_visible_observed", "existing-browser-observer")
                    first_card = True
                turn_events.begin_approval_driver(approval_id)
            try:
                decision = on_write_card(visible)
            except BaseException:
                if turn_events:
                    turn_events.end_approval_driver(approval_id)
                raise
            if decision not in {"approved", "denied", "prefill"}:
                if turn_events:
                    turn_events.end_approval_driver(approval_id)
                raise ValueError("the isolated approval apparatus returned an unknown decision")
            if decision == "prefill" and not visible["offer_prefill"]:
                if turn_events:
                    turn_events.end_approval_driver(approval_id)
                raise ValueError("the visible write card did not offer form prefill")
            if write_cards is not None:
                write_cards.append({**visible, "decision": decision,
                    "seconds": time.monotonic() - started if started is not None else None})
            button = card.locator({"approved": _APPROVE_CONTROL,
                                   "denied": '[data-testid="woa-deny"]',
                                   "prefill": '[data-testid="woa-prefill"]'}[decision])
            button.scroll_into_view_if_needed()
            try:
                button.click()
            except BaseException:
                if turn_events:
                    turn_events.end_approval_driver(approval_id)
                raise
            # A following approval may replace this card without an empty DOM frame.
            # Poll by approval identity; never wait for every write card to disappear.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for the turn's outcome")
        try:
            return observed(conversation.next_outcome(timeout_s=min(0.25, remaining)))
        except TimeoutError:
            continue


def _observe_write_card(page, card, timeout_s):
    deadline = time.monotonic() + timeout_s
    def remaining_ms():
        return max(1, (deadline-time.monotonic())*1000)
    button = card.locator(_APPROVE_CONTROL)
    button.scroll_into_view_if_needed(timeout=remaining_ms())
    # The existing full-content gate must settle before the policy reads it.
    page.wait_for_timeout(min(600, remaining_ms()))
    visible = card.evaluate("""n => {
        const r=n.getBoundingClientRect();
        const button=n.querySelector('[data-testid="woa-approve"]');
        if (r.width<=0 || r.height<=0 || r.bottom<=0 || r.top>=innerHeight || !button || button.disabled)
            return null;
        const prefill=n.querySelector('[data-testid="woa-prefill"]');
        return {text:n.innerText, effect:n.querySelector('.woa-approval__effect').innerText,
            offer_prefill:!!prefill && !prefill.disabled && prefill.getBoundingClientRect().width>0,
            targets:[...n.querySelectorAll('.woa-approval__targets li')].map(n=>n.innerText),
            details:[...n.querySelectorAll('.woa-approval__detail')].map(n=>({kind:n.dataset.kind,
                label:n.querySelector('strong').textContent,text:n.querySelector('pre').textContent}))};
    }""", timeout=remaining_ms())
    if not visible or time.monotonic() > deadline:
        raise RuntimeError("the complete bound write card is not visible and actionable within its observation bound")
    return visible


def _observe_final_result(page, result, timeout_s, events):
    """Read the settled result on the bound normal turn, not merely its server terminal."""
    turn_id = result.message.message_id
    deadline = time.monotonic() + timeout_s
    def remaining_ms():
        return max(1, (deadline-time.monotonic())*1000)
    # Playwright's existing locator crosses the panel's open shadow root.
    turn = page.locator(f'.woa-turn[data-turn-id="{turn_id}"][data-settled="true"]')
    result = turn.locator('.woa-result, .woa-outcome')
    result.wait_for(state="visible", timeout=remaining_ms())
    result.locator('.woa-md__caret').wait_for(state="detached", timeout=remaining_ms())
    if not result.evaluate("n => { const r=n.getBoundingClientRect(); return r.bottom>0 && r.top<innerHeight; }", timeout=remaining_ms()):
        raise RuntimeError("the settled result was rendered outside the visible viewport")
    result.inner_text(timeout=remaining_ms())
    events.emit("final_result_visible_observed", "existing-browser-observer", turn_id=turn_id)


@dataclass(frozen=True)
class YudaoSession:
    """One browser session: signed in, the assistant injected, the tasks submitted in turn."""
    origin: str
    account: YudaoAccount
    api_docs: dict
    tasks: tuple[str, ...]
    run_dir: Path
    api_base: str = ""
    ui: UiSource | None = None
    model_env: dict = field(default_factory=dict)
    model: str | None = None
    start_path: str = "/index"
    turn_timeout_s: float = 120
    headed: bool = False
    #: Fixes the date relative periods count from (an evaluation's reference day).
    today: date | None = None
    #: The deployment's source (`YudaoSource`), when its rules are offered.
    source: object = None
    #: The organisation's conventions file; None runs without them.
    conventions: Path | None = REFERENCE_CONVENTIONS
    batch_id: str = ""
    executor_mode: str = "goal"
    native_writes: bool = False
    spend_ledger: Path | None = None
    batch_cost_stop: float | None = None
    model_options: dict | None = None
    step_budget: int = 12
    max_completion_tokens: int = 4000

    def __post_init__(self):
        if self.executor_mode not in {"goal", "agent", "hybrid"}:
            raise ValueError("executor_mode must be goal, agent or hybrid")
        if not isinstance(self.native_writes, bool):
            raise TypeError("native writes must be explicitly enabled")
        for value in (self.step_budget, self.max_completion_tokens):
            if type(value) is not int or value < 1:
                raise ValueError("session budgets must be positive integers")


def run_session(session: YudaoSession, *, chat=None, meter=None, on_write_card=None, on_turn_event=None) -> dict:
    """Run the tasks; return each turn's record, the final page's address, the addresses
    the runtime reported along the way, and what the model calls cost."""
    api_base = (session.api_base or session.origin).rstrip("/")
    source, rule_source = _session_sources(session)
    from web_operation_agent.business.mutation import SQLiteMutationJournal
    session.run_dir.mkdir(parents=True, exist_ok=True)
    app = YudaoApplication(session.origin, session.account, session.api_docs,
                           api_base_url=api_base, ui=session.ui, today=session.today,
                           source=rule_source,
                           conventions=session.conventions, native_writes=session.native_writes,
                           mutation_journal=SQLiteMutationJournal(session.run_dir / "mutations.sqlite3") if session.native_writes else None)
    bound = bind_application(app)
    if not app.health()["passed"]:
        raise RuntimeError("yudao is not reachable under this account")
    run_dir = session.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    tokens = _browser_login(session.origin, session.account)
    principal = app.ports.identity.bind(app.identity.page_identity(tokens["accessToken"]),
                                        delegation_id="yudao-" + uuid.uuid4().hex)
    meter = meter or SpendMeter(ledger=session.spend_ledger or run_dir / "spend.jsonl",
        batch_id=session.batch_id or f"yudao-demo-{run_dir.name}", batch_cost_stop=session.batch_cost_stop)
    chat = chat or ChatModel(meter, env=dict(session.model_env), model=session.model, options=session.model_options,
                     max_completion_tokens=session.max_completion_tokens, timeout=int(session.turn_timeout_s),
                     egress_policy=ProviderEgressPolicy(
                         session_handoff_keys=bound.ports.routes.session_handoff_query_keys))
    holder: dict = {}
    from web_operation_agent.execution.events import TurnEvents
    turn_events = TurnEvents(on_turn_event)

    def runner_factory(channel):
        common = dict(
            channel=channel,
            turn_events=turn_events,
            chat=chat, application=bound, control=ReadOnlyControl(),
            run_id_factory=lambda _m: "yudao-" + uuid.uuid4().hex[:16])
        config = replace(yudao_turn_config(run_dir, timeout_s=int(session.turn_timeout_s),
            source=source, native_writes=session.native_writes), step_budget=session.step_budget)
        if session.executor_mode == "agent":
            executor = AgentTurnExecutor(
                **common, config=config)
        elif session.executor_mode == "hybrid":
            turn_config = config
            executor = GoalTurnExecutor(
                **common, config=turn_config, disabled_page_actions=turn_config.disabled_actions,
                repository_answers=True,
                goal_port=app.goal_port)
        else:
            executor = GoalTurnExecutor(
                **common, config=AgentTurnConfig(
                    supported_scope="Read this account's available application records and open their pages.",
                    ledger_path=run_dir / "audit.jsonl", wall_budget_s=int(session.turn_timeout_s),
                    transport_timeout_s=min(30, session.turn_timeout_s), require_identity=True,
                    allow_business_mutations=session.native_writes, step_budget=session.step_budget,
                    time_zone="Asia/Shanghai"),
                goal_port=app.goal_port)
        holder["conversation"] = LiveConversationRunner(channel, executor=executor)
        return holder["conversation"]

    live = LiveProductSession(
        allowed_origin=bound.descriptor.origin,
        page_controller_module=bound.descriptor.page_controller_module,
        runner_factory=runner_factory, navigation_mode=bound.descriptor.navigation_mode,
        sensitive_query_keys=bound.ports.routes.session_handoff_query_keys,
        principal=principal).start()
    records: list[dict] = []
    visited: list[str] = []
    final_path = breadcrumb_last = ""
    active_tabs: list[str] = []
    audit_offset = len((run_dir / "audit.jsonl").read_text().splitlines()) if (run_dir / "audit.jsonl").exists() else 0
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not session.headed)
            context = browser.new_context(viewport={"width": 1440, "height": 960},
                                          timezone_id="Asia/Shanghai")
            context.add_init_script(script=_signed_in_script(session.origin, session.account, tokens))
            context.add_init_script(script=live.bootstrap_script)
            page = context.new_page()

            def seen(frame):
                if frame == page.main_frame:
                    visited.append(_address(frame.url))
            page.on("framenavigated", seen)
            _open(page, session.origin + session.start_path)
            for task in session.tasks:
                _stage("the previous task to settle", lambda: page.wait_for_function(
                    "async () => { const r = await window.__WOA_RUNTIME_READY__; "
                    "return !r.stored.taskInFlight && !r.stored.inFlight }", timeout=60_000))
                # Submitted without waiting on it: the turn's outcome, bounded below, is the wait.
                started = time.monotonic()
                page.evaluate("task => { window.__WOA_RUNTIME_READY__.then(r => r.submitTask(task)); "
                              "return true; }", task)
                operations: list = []
                write_cards: list = []
                outcome = _next_outcome(page, holder["conversation"], session.turn_timeout_s + 30,
                                        operations, write_cards=write_cards, on_write_card=on_write_card,
                                        started=started, turn_events=turn_events)
                result = outcome.result
                record = {"task": task, "error": outcome.error} if outcome.error else {
                    "task": task, "status": result.status.value if isinstance(result, AgentTurnExecution) else "",
                    "terminal_reason": getattr(result, "terminal_reason", ""),
                    "answer": getattr(result, "user_answer", ""), "url": page.url,
                    "operations": len(operations), "write_cards": write_cards,
                    "seconds": time.monotonic() - started,
                    "business_result": result.business_result.content if result.business_result else None,
                    "verified_answer": result.agent.verified_answer}
                record["page_state"] = {"path": _address(page.url), "breadcrumb_last": _breadcrumb_last(page),
                    "active_tabs": _active_tabs(page), "rendered": page.evaluate(_ADMIN_RENDERED)}
                audit_lines = (run_dir / "audit.jsonl").read_text().splitlines()
                events = [json.loads(line) for line in audit_lines[audit_offset:]]
                audit_offset = len(audit_lines)
                record["skill_evidence"] = [
                    {"event": e["event"], "sequence": n, **{k: e[k] for k in
                        ("step", "skill_id", "skill_sha256", "resource", "operations", "intent") if k in e}}
                    for n, e in enumerate(events) if e["event"] in
                    {"business_skill_loaded", "business_skill_auto_loaded", "business_skill_resource_loaded", "skill_task_declared"}]
                record["mutation_evidence"] = [
                    {"event": e["event"], "sequence": n, **{k: e[k] for k in
                        ("step", "tool", "proposal", "proposal_digest", "result_digest", "audit", "reconciliation", "completed", "dispatched") if k in e}}
                    for n, e in enumerate(events) if e["event"] in
                    {"business_mutation_proposed", "business_mutation_execution_started", "business_mutation_result",
                     "business_mutation_indeterminate", "business_mutation_cancelled_before_dispatch"}]
                observation = turn_events.snapshot(turn_events.latest_turn_id)
                record["passive_turn_events"] = observation["events"]
                record["turn_event_delivery_complete"] = observation["delivery_complete"]
                record["dispatch_receipts"] = observation["receipts"]
                record["dispatch_coverage_complete"] = (
                    bound.business_tools.mutation_tool_names <= bound.ports.operations.observed_mutation_operations
                    and observation["unbound_mutations"] == 0)
                records.append(record)
                page.screenshot(path=str(run_dir / f"turn-{len(records)}.png"))
            # A route change still settling after the turn's outcome is part of it.
            page.wait_for_timeout(1000)
            final_path = _address(page.url)
            breadcrumb_last = _breadcrumb_last(page)
            active_tabs = _active_tabs(page)
            browser.close()
    finally:
        live.close()
        live.wait(timeout_s=10)
    spend = {**meter.report(), "model": getattr(chat, "model", None), "provider": getattr(chat, "provider", None),
             "cost_cny": None if meter.unmetered_calls else round(meter.batch_cost(), 6),
             "cost_basis": "reported usage at registered public reference rates; not the provider invoice",
             "usage_complete": not bool(meter.unmetered_calls)}
    summary = {"run_dir": str(run_dir), "records": records, "final_path": final_path,
               "breadcrumb_last": breadcrumb_last, "active_tabs": active_tabs,
               "visited": visited, "spend": spend}
    (run_dir / "records.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    return summary


def _session_sources(session: YudaoSession):
    """The legacy compiled-rule port and repository lookup are distinct contracts."""
    if session.executor_mode == "goal":
        rules = session.source
        if isinstance(rules, SourceCapability):
            if rules != SourceCapability.none():
                raise ValueError("goal mode accepts compiled Yudao rules; use hybrid for repository lookup")
            rules = None
        if rules is not None and (not callable(getattr(rules, "rules", None))
                                  or not isinstance(getattr(rules, "release", None), str)):
            raise TypeError("goal source must implement the compiled rule contract")
        return SourceCapability.none(), rules
    if session.source is not None and not isinstance(session.source, SourceCapability):
        raise TypeError("agent and hybrid sources must be SourceCapability")
    return session.source or SourceCapability.none(), None


class SessionStageTimeout(RuntimeError):
    """A browser wait that ran out, named by what it waited for."""


def _stage(what: str, wait):
    try:
        return wait()
    except Exception as exc:  # Playwright's TimeoutError, or a page that closed under it
        raise SessionStageTimeout(f"waiting for {what}: {type(exc).__name__}") from exc


def _open(page, url: str) -> None:
    """Load the admin UI with the assistant ready; a load whose assistant never became
    ready is reloaded once."""
    for attempt in (1, 2):
        try:
            _stage("the admin UI to load", lambda: page.goto(
                url, wait_until="domcontentloaded", timeout=60_000))
            _stage("the assistant's runtime to load", lambda: page.wait_for_function(
                "window.__WOA_RUNTIME_READY__ !== undefined", timeout=60_000))
            _stage("the assistant's runtime to be ready", lambda: page.wait_for_function(
                "async () => { await window.__WOA_RUNTIME_READY__; return true; }", timeout=60_000))
            _stage("the assistant's panel", lambda: page.locator("#woa-assistant").wait_for(
                state="visible", timeout=60_000))
            _stage("the admin page to render", lambda: page.wait_for_function(
                _ADMIN_RENDERED, timeout=30_000))
            return
        except SessionStageTimeout:
            if attempt == 2:
                raise


def verify_browser_ready(origin: str, account: YudaoAccount, start_path: str) -> None:
    """Check the actor's admin page in a real browser before any paid model turn.

    The deployed UI may have an API origin baked into its bundle that differs from the
    reachable server URL. An HTTP health check cannot detect that broken page.
    """
    tokens = _browser_login(origin, account)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_init_script(script=_signed_in_script(origin, account, tokens))
            page = context.new_page()
            permission_url = origin.rstrip("/") + "/admin-api/system/auth/get-permission-info"

            def load_with_permission():
                with page.expect_response(lambda response: response.url.startswith(permission_url),
                                          timeout=15_000) as received:
                    page.goto(origin.rstrip("/") + start_path,
                              wait_until="domcontentloaded", timeout=30_000)
                if received.value.status != 200:
                    raise RuntimeError("the admin UI's permission request failed")

            _stage("the admin UI's same-origin permission request", load_with_permission)
            _stage("the admin page to render", lambda: page.wait_for_function(
                _ADMIN_RENDERED, timeout=15_000))
        finally:
            browser.close()


def _breadcrumb_last(page) -> str:
    """The page's own name, as a person reads it: the breadcrumb's last item, or -- on a
    detail page, whose route is hidden from the menu and so has no breadcrumb -- the name of
    the active tab the admin UI opens for every page."""
    try:
        return page.evaluate(
            "() => { const items = document.querySelectorAll('.el-breadcrumb__item'); "
            "const last = items.length ? items[items.length - 1].innerText.trim() : ''; "
            "if (last) return last; "
            "const tab = document.querySelector('.v-tags-view__item.is-active'); "
            "return tab ? tab.innerText.trim() : ''; }") or ""
    except Exception:  # noqa: BLE001 -- a page gone mid-read has no breadcrumb to report
        return ""


def _active_tabs(page) -> list[str]:
    """Visible active tabs as the application rendered them, independent of the agent."""
    try:
        return page.locator(".v-tags-view__item.is-active").all_inner_texts()
    except Exception:  # noqa: BLE001 -- a page gone mid-read has no tabs to report
        return []


def _address(url: str) -> str:
    """A page's path and query, as the application routes it."""
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--origin", default="http://localhost:48088")
    parser.add_argument("--api-base", default="", help="where the API and its documentation are "
                        "reachable from the server, when not at the origin")
    add_account_arguments(parser)
    parser.add_argument("--ui-source", type=Path, help="checkout of yudao-ui-admin-vue3")
    parser.add_argument("--source-root", type=Path, help="directory holding the deployed release's "
                        "server/ and ui/ checkouts, whose rules are then offered")
    parser.add_argument("--source-release", default="", help="the release those checkouts are")
    parser.add_argument("--no-conventions", action="store_true",
                        help="run without the deployment's reviewed conventions")
    parser.add_argument("--model-env", type=Path, help="env file holding the model provider settings")
    parser.add_argument("--model", default=None)
    parser.add_argument("--executor", choices=("goal", "agent", "hybrid"), default="goal")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--start-path", default="/index")
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--turn-timeout-s", type=float, default=120)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    api_base = (args.api_base or args.origin).rstrip("/")
    summary = run_session(YudaoSession(
        origin=args.origin, account=YudaoAccount.load(args.account, args.accounts),
        api_docs=read_api_docs(api_base), tasks=tuple(args.task),
        run_dir=args.run_dir or Path.cwd() / ".artifacts" / f"yudao-demo-{uuid.uuid4().hex[:8]}",
        api_base=api_base, ui=ui_source(args.ui_source, args.source_root),
        source=(repository_source(args.source_root, args.source_release) if args.executor != "goal"
                else source_rules(args.source_root, args.source_release)), executor_mode=args.executor,
        conventions=None if args.no_conventions else REFERENCE_CONVENTIONS,
        model_env=model_env(args.model_env), model=args.model, start_path=args.start_path,
        turn_timeout_s=args.turn_timeout_s, headed=args.headed))
    for record in summary["records"]:
        print(json.dumps(record, ensure_ascii=False), flush=True)
    print(json.dumps({"run_dir": summary["run_dir"], **summary["spend"]}, ensure_ascii=False))
    return 0


def ui_source(ui: Path | None, root: Path | None) -> UiSource | None:
    """The admin UI's source: the checkout named, else the one under the source root."""
    if ui is None and root is not None and (root / "ui").is_dir():
        ui = root / "ui"
    return UiSource.read(ui) if ui else None


def source_rules(root: Path | None, release: str):
    """The deployment's source rules, when a source root is given; it must name its release."""
    if root is None:
        return None
    if not release:
        raise SystemExit("--source-release is required with --source-root")
    from .source import YudaoSource
    return YudaoSource(root, release=release)


def read_api_docs(api_base: str) -> dict:
    with urllib.request.urlopen(f"{api_base.rstrip('/')}/v3/api-docs/all", timeout=60) as response:
        return json.load(response)


def model_env(path: Path | None) -> dict:
    """The model provider's settings from an env file, and nothing else in it."""
    loaded = load_env(path) if path else {}
    return {k: v for k, v in loaded.items() if k in _MODEL_ENV_KEYS}


if __name__ == "__main__":
    sys.exit(main())
