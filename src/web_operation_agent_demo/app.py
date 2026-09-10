"""Run a synthetic first-party application through the real embedded Agent path.

The application, its typed API, Skill and same-origin PageController are intentionally small.
They demonstrate the public composition boundary without requiring Twenty, OAuth, a source
checkout or a database.  ``--scripted`` selects a deterministic model substitute and is
therefore an apparatus check, not evidence of model quality.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from web_operation_agent.application import (
    APPLICATION_ADAPTER_VERSION,
    AnonymousIdentityPort,
    ApplicationDescriptor,
    ApplicationPorts,
    DeclaredRoutePort,
    ConservativeMutationReconciler,
    DeclaredDataClassifier,
    ResultPostconditionVerifier,
    bind_application,
)
from web_operation_agent.business import (
    BusinessMutationSpec,
    BusinessSkill,
    BusinessToolError,
    BusinessToolResult,
    BusinessToolRuntime,
)
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.live import LiveConversationRunner, LiveProductSession
from web_operation_agent.model import ChatModel, ProviderError, SpendMeter, load_env
from web_operation_agent.product import AgentTurnConfig, AgentTurnExecution, AgentTurnExecutor


DEMO_CONTACTS = (
    {"id": "ada-lovelace", "name": "Ada Lovelace", "email": "ada@example.test"},
    {"id": "grace-hopper", "name": "Grace Hopper", "email": "grace@example.test"},
    {"id": "alan-turing", "name": "Alan Turing", "email": "alan@example.test"},
)
_MODEL_ENV_KEYS = (
    "AGENT_OPENAI_BASE_URL",
    "AGENT_OPENAI_API_KEY",
    "AGENT_OPENAI_MODEL_PRO",
    "AGENT_PROVIDER",
)
_SKILL = {
    "schema_version": 1,
    "skill_id": "minimal.contacts.query",
    "title": "Read contacts",
    "intent": "List or open contacts from the current synthetic workspace.",
    "triggers": [
        "contact", "contacts", "address book", "people", "person",
        "联系人", "通讯录", "人员",
    ],
    "instructions": [
        "Call list_contacts for current data; never answer from the Skill or page text.",
        "Use name_contains only when the user names a contact.",
        "To open a contact, require exactly one match and call goto with its exact ui_path.",
        "Treat completeness.source_complete=true as the only basis for an exhaustive list.",
    ],
    "tool_names": ["list_contacts"],
    "provenance": ["minimal-demo://contacts/v1"],
    "read_only": True,
}
_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_contacts",
        "description": "Read current contacts from the delegated synthetic workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "name_contains": {
                    "type": "string",
                    "description": "Optional case-insensitive contact-name filter.",
                },
            },
            "additionalProperties": False,
        },
    },
}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def _html(source_contacts: Sequence[Mapping[str, str]]) -> bytes:
    contacts = json.dumps(source_contacts, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Minimal Contacts</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; color: #172033; background: #f4f7fb; }}
    header {{ height: 64px; display: flex; align-items: center; gap: 12px; padding: 0 32px;
      color: white; background: #172554; box-shadow: 0 1px 12px #0f172a24; }}
    header strong {{ font-size: 17px; letter-spacing: .01em; }}
    header span {{ color: #bfdbfe; font-size: 13px; }}
    main {{ width: min(900px, calc(100% - 48px)); margin: 44px auto; }}
    .eyebrow {{ color: #2563eb; font-size: 12px; font-weight: 750; letter-spacing: .12em;
      text-transform: uppercase; }}
    h1 {{ margin: 8px 0 10px; font-size: clamp(30px, 4vw, 46px); letter-spacing: -.04em; }}
    .lede {{ margin: 0 0 28px; color: #64748b; max-width: 620px; line-height: 1.6; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 16px; }}
    .card {{ display: block; padding: 22px; color: inherit; text-decoration: none; background: white;
      border: 1px solid #dbe4f0; border-radius: 18px; box-shadow: 0 12px 35px #3341550d;
      transition: transform .15s ease, border-color .15s ease; }}
    .card:hover {{ transform: translateY(-2px); border-color: #93c5fd; }}
    .name {{ display: block; font-weight: 760; font-size: 18px; margin-bottom: 8px; }}
    .email {{ color: #64748b; }}
    .back {{ display: inline-block; margin-bottom: 28px; color: #2563eb; text-decoration: none; }}
    .record {{ max-width: 620px; padding: 32px; background: white; border: 1px solid #dbe4f0;
      border-radius: 22px; box-shadow: 0 16px 44px #33415512; }}
    dt {{ margin-top: 22px; color: #64748b; font-size: 12px; font-weight: 750;
      letter-spacing: .09em; text-transform: uppercase; }}
    dd {{ margin: 7px 0 0; font-size: 18px; }}
  </style>
</head>
<body>
  <header><strong>Northstar Contacts</strong><span>Synthetic local workspace</span></header>
  <main id="app"></main>
  <script>
    const contacts = {contacts};
    const app = document.querySelector('#app');
    function render() {{
      const match = /^\\/contacts\\/([^/]+)$/.exec(location.pathname);
      if (match) {{
        const contact = contacts.find(item => item.id === match[1]);
        if (!contact) {{ app.innerHTML = '<h1>Contact not found</h1>'; return; }}
        app.innerHTML = `<a class="back" href="/contacts" data-route>← All contacts</a>
          <section class="record"><div class="eyebrow">Contact record</div>
          <h1>${{contact.name}}</h1><dl><dt>Email</dt><dd>${{contact.email}}</dd>
          <dt>Record ID</dt><dd>${{contact.id}}</dd></dl></section>`;
        document.title = `${{contact.name}} · Minimal Contacts`;
        return;
      }}
      app.innerHTML = `<div class="eyebrow">Developer demo</div><h1>Contacts</h1>
        <p class="lede">A small first-party app whose current records come from a typed API.
        Ask the embedded assistant to list a contact or open a record.</p><section class="grid">${{
          contacts.map(item => `<a class="card" href="/contacts/${{item.id}}" data-route>
            <span class="name">${{item.name}}</span><span class="email">${{item.email}}</span></a>`).join('')
        }}</section>`;
      document.title = 'Minimal Contacts';
    }}
    addEventListener('popstate', render);
    addEventListener('click', event => {{
      const link = event.target.closest('a[data-route]');
      if (!link) return;
      event.preventDefault(); history.pushState({{}}, '', link.href); render();
    }});
    render();
  </script>
</body>
</html>
""".encode("utf-8")


class MinimalContactsApplication:
    """Loopback first-party application and typed API backed by synthetic immutable data."""

    def __init__(self, *, contacts: Sequence[Mapping[str, str]] = DEMO_CONTACTS):
        self.contacts = tuple({key: str(value) for key, value in item.items()}
                              for item in contacts)
        if not self.contacts or any(set(item) != {"id", "name", "email"}
                                    for item in self.contacts):
            raise ValueError("contacts must contain id, name, and email")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def _reply(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                if path == "/healthz":
                    self._reply(200, _json_bytes({"ready": True}), "application/json")
                elif path == "/api/contacts":
                    self._reply(200, _json_bytes({"contacts": owner.contacts}),
                                "application/json; charset=utf-8")
                elif path == "/woa/page-controller.js":
                    source = files("web_operation_agent_demo").joinpath(
                        "assets/page_controller.js").read_bytes()
                    self._reply(200, source, "text/javascript; charset=utf-8")
                elif path == "/contacts" or path.startswith("/contacts/"):
                    self._reply(200, _html(owner.contacts), "text/html; charset=utf-8")
                elif path == "/":
                    self.send_response(302)
                    self.send_header("Location", "/contacts")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._reply(404, b"not found\n", "text/plain; charset=utf-8")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "MinimalContactsApplication":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever, name="minimal-contacts", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        if self._thread is None:
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._thread = None

    def __enter__(self) -> "MinimalContactsApplication":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()


class _ContactsToolExecutor:
    def __init__(self, origin: str):
        self.origin = origin.rstrip("/")

    @staticmethod
    def tool_schemas() -> tuple[dict, ...]:
        return (_TOOL_SCHEMA,)

    @staticmethod
    def tool_effects() -> dict[str, str]:
        return {"list_contacts": "read"}

    def execute(self, name: str, args: dict, *, context=None) -> BusinessToolResult:
        if name != "list_contacts" or set(args) - {"name_contains"}:
            raise BusinessToolError("invalid contact query")
        query = args.get("name_contains", "")
        if not isinstance(query, str) or len(query) > 120:
            raise BusinessToolError("name_contains must be a string of at most 120 characters")
        try:
            with urllib.request.urlopen(self.origin + "/api/contacts", timeout=3) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise BusinessToolError("the contacts API is unavailable") from exc
        source = payload.get("contacts") if isinstance(payload, dict) else None
        if not isinstance(source, list):
            raise BusinessToolError("the contacts API returned an invalid document")
        matched = []
        for raw in source:
            if not isinstance(raw, dict) or set(raw) != {"id", "name", "email"}:
                raise BusinessToolError("the contacts API returned an invalid record")
            if query and query.casefold() not in str(raw["name"]).casefold():
                continue
            matched.append({
                "id": str(raw["id"]),
                "name": str(raw["name"]),
                "email": str(raw["email"]),
                "ui_path": f"/contacts/{raw['id']}",
            })
        routes = tuple(item["ui_path"] for item in matched)
        return BusinessToolResult(
            content={
                "contacts": matched,
                "matched_count": len(matched),
                "source_total": len(source),
                "scope": "synthetic_local_workspace",
                "completeness": {
                    "source_complete": True,
                    "result_truncated": False,
                    "incomplete_reason": "",
                },
            },
            audit={
                "application": "minimal-contacts",
                "operation": name,
                "returned_count": len(matched),
                "source_complete": True,
            },
            allowed_routes=routes,
        )

    @staticmethod
    def prepare_mutation(_name: str, _args: dict, *, context=None) -> BusinessMutationSpec:
        raise BusinessToolError("the minimal demo is read-only")

    @staticmethod
    def execute_mutation(_name: str, _args: dict, *, context=None) -> BusinessToolResult:
        raise BusinessToolError("the minimal demo is read-only")


class MinimalContactsAdapter:
    """A second application adapter implemented only through the public SPI."""

    def __init__(self, origin: str):
        skill = BusinessSkill.from_bytes(
            _json_bytes(_SKILL), artifact_uri="package://minimal-demo/contacts-query.json")
        data = DeclaredDataClassifier((
            ("list_contacts", (
                "synthetic.contact", "business.identifier", "application.route")),
        ))
        postconditions = ResultPostconditionVerifier(())
        routes = DeclaredRoutePort(
            application_id="minimal.contacts",
            tool_route_patterns=(r"/contacts/[a-z0-9-]+",),
        )
        self.business_tools = BusinessToolRuntime(
            (skill,), _ContactsToolExecutor(origin), application_id="minimal.contacts",
            data_classifier=data, postcondition_verifier=postconditions,
            route_validator=routes)
        self.ports = ApplicationPorts(
            identity=AnonymousIdentityPort(),
            routes=routes,
            postconditions=postconditions,
            reconciliation=ConservativeMutationReconciler(),
            data=data,
        )
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id="minimal.contacts",
            display_name="Minimal Contacts",
            origin=origin,
            navigation_mode="spa",
            page_controller_module=origin + "/woa/page-controller.js",
            catalog_sha256=self.business_tools.catalog_sha256,
        )

    def health(self) -> dict:
        try:
            with urllib.request.urlopen(self.descriptor.origin + "/healthz", timeout=2) as response:
                payload = json.load(response)
            passed = response.status == 200 and payload == {"ready": True}
        except (OSError, ValueError, urllib.error.URLError):
            passed = False
        return {"passed": passed, "application_id": self.descriptor.application_id}


class ScriptedDemoChat:
    """Deterministic apparatus probe; it never contacts a provider or a spend ledger."""

    model = "scripted-demo/no-provider"

    def __init__(self):
        self.calls = 0

    def complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                 max_tokens: int | None = None, options: dict | None = None,
                 timeout_s: float | None = None) -> dict[str, Any]:
        self.calls += 1
        replies: tuple[tuple[str, dict], ...] = (
            ("load_skill", {"skill_id": "minimal.contacts.query"}),
            ("list_contacts", {"name_contains": "Grace Hopper"}),
            ("goto", {"url": "/contacts/grace-hopper"}),
            ("finish", {"done": True,
                        "why": "Opened Grace Hopper's current contact record."}),
        )
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


def _chat(config: DemoConfig, run_dir: Path):
    if config.scripted:
        return ScriptedDemoChat(), None
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
                tasks = config.tasks or (("Open Grace Hopper's contact record.",)
                                         if config.scripted else ())
                if tasks:
                    for task in tasks:
                        page.evaluate(
                            "task => window.__WOA_RUNTIME_READY__.then(runtime => "
                            "runtime.submitTask(task))", task)
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
        adapter = MinimalContactsAdapter(application.origin)
        bound = bind_application(adapter)
        if not adapter.health()["passed"]:
            raise RuntimeError("the synthetic contacts application is not ready")
        chat, meter = _chat(config, run_dir)
        holder: dict[str, LiveConversationRunner] = {}

        def runner_factory(channel):
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope=(
                        "Read current contacts through the typed API and open only an exact "
                        "route returned by that API. Do not modify records or submit forms."),
                    ledger_path=run_dir / "audit.jsonl",
                    step_budget=8,
                    wall_budget_s=max(1, int(config.turn_timeout_s)),
                    transport_timeout_s=min(30, config.turn_timeout_s),
                    disabled_actions=frozenset({"click", "fill", "select", "submit", "back"}),
                    scope_first_policy=True,
                    policy_block_limit=2,
                    plain_business_answer_min_page_actions=0,
                    max_user_answer_chars=8_000,
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
                "AGENT_OPENAI_MODEL_PRO. Scripted mode checks plumbing only."),
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
