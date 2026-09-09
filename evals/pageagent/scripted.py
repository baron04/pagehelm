"""Scripted provider, synthetic Business Runtime and loopback page for PageAgent runner tests.

Nothing in this module contacts an external model, a spend ledger or a real Twenty instance.
The synthetic company set is generated per test, so the harness encodes no real company name,
record ID, domain or evaluator answer.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from web_operation_agent.application import APPLICATION_ADAPTER_VERSION, ApplicationDescriptor
from web_operation_agent.business import (BusinessSkill, BusinessToolError, BusinessToolResult,
                                          BusinessToolRuntime)
from web_operation_agent.identity import PRINCIPAL_CONTEXT_VERSION, PrincipalContext
from web_operation_agent.model import ChatModel
from web_operation_agent.twenty import QUERY_COMPANIES_SCHEMA, TwentyApplicationAdapter


SCRIPTED_MODEL = "pageagent-scripted-model"
SESSION_COOKIE = "woa_synthetic_session"
SYNTHETIC_FIXTURE = {
    "container": "synthetic-twenty-db",
    "database": "synthetic",
    "db_user": "synthetic",
    "workspace_schema": "workspace_synthetic",
}


def agent_output_completion(action: dict[str, Any], *, sequence: int,
                            model: str = SCRIPTED_MODEL) -> dict[str, Any]:
    """Return one OpenAI-compatible completion carrying a PageAgent ``AgentOutput`` action."""
    arguments = {
        "evaluation_previous_goal": "The prior state was inspected.",
        "memory": "Use only the current synthetic page and tool results.",
        "next_goal": "Execute the scripted action.",
        "action": action,
    }
    return {
        "id": f"scripted-{sequence}",
        "object": "chat.completion",
        "created": 1_788_912_000 + sequence,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call-{sequence}",
                    "type": "function",
                    "function": {"name": "AgentOutput", "arguments": json.dumps(arguments)},
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
    }


class ScriptedProviderError(RuntimeError):
    """A scripted transport failure carrying only a content-free code."""

    def __init__(self, code: str):
        super().__init__("scripted provider failure")
        self.code = code


class ScriptedMeter:
    """Project scripted calls the way ``SpendMeter.run_report`` does, without any ledger."""

    def __init__(self, client: "ScriptedProviderClient"):
        self.client = client

    def run_report(self, _run_id: str) -> dict[str, Any]:
        client = self.client
        with client.lock:
            calls = client.dispatched
            responded = list(client.responded)
        unavailable = calls - len(responded)
        usages = [item["usage"] for item in responded]
        request_ids = [f"scripted-request-{index}" for index in range(1, calls + 1)]
        evidence_digest = (
            "sha256:" + hashlib.sha256(json.dumps(
                request_ids, separators=(",", ":")).encode("utf-8")).hexdigest()
            if calls else None)
        return {
            "model_calls": calls,
            "usage_unavailable_calls": unavailable,
            "provider_reported_models": sorted({item["model"] for item in responded}),
            "provider_request_ids_digest": evidence_digest,
            "known_input_tokens": sum(item["prompt_tokens"] for item in usages),
            "known_output_tokens": sum(item["completion_tokens"] for item in usages),
            "known_cached_tokens": sum(
                item.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                for item in usages),
            "known_reasoning_tokens": sum(
                item.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
                for item in usages),
            "known_cost_cny": 0.0,
            "total_cost_cny": None if unavailable else 0.0,
            "spend_ledger_slice_digest": evidence_digest,
        }


class ScriptedProviderClient(ChatModel):
    """Answer PageAgent provider calls from a fixed action script.

    The class keeps the ``ChatModel`` type required by the runner boundary but never runs the
    real constructor, so it holds no endpoint, credential or ledger. ``failures`` maps a
    1-based call sequence to a transport code raised instead of a response; ``after_call``
    runs after every dispatched call, which lets a test cancel a run at an exact point.
    """

    def __init__(self, actions: list[dict[str, Any]], *, model: str = SCRIPTED_MODEL,
                 failures: dict[int, str] | None = None,
                 after_call: Callable[[int], None] | None = None):
        self.model = model
        self.responses = [
            agent_output_completion(action, sequence=index + 1, model=model)
            for index, action in enumerate(actions)
        ]
        self.failures = dict(failures or {})
        self.after_call = after_call
        self.requests: list[dict[str, Any]] = []
        self.responded: list[dict[str, Any]] = []
        self.dispatched = 0
        self.lock = threading.Lock()
        self.meter = ScriptedMeter(self)

    def request_completion(self, body: dict[str, Any], _run_id: str,
                           *, timeout_s: float | None = None) -> dict[str, Any]:
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or timeout_s <= 0):
            raise ValueError("scripted provider requires a positive deadline")
        with self.lock:
            self.dispatched += 1
            sequence = self.dispatched
        tools = body.get("tools") if isinstance(body.get("tools"), list) else []
        self.requests.append({
            "sequence": sequence,
            "model": body.get("model"),
            "tool_names": sorted(
                tool.get("function", {}).get("name", "") for tool in tools
                if isinstance(tool, dict)),
            "message_count": len(body.get("messages") or []),
        })
        try:
            if sequence in self.failures:
                raise ScriptedProviderError(self.failures[sequence])
            if sequence > len(self.responses):
                raise ScriptedProviderError("provider_call")
            response = self.responses[sequence - 1]
            with self.lock:
                self.responded.append(response)
            return response
        finally:
            if self.after_call is not None:
                self.after_call(sequence)


def synthetic_companies(count: int = 3) -> list[dict[str, str]]:
    """Generate distinct synthetic company rows with fresh random record IDs."""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 50:
        raise ValueError("count must be an integer from 1 to 50")
    return [{
        "id": str(uuid.uuid4()),
        "name": f"Synthetic Company {index:02d}",
        "domain": f"company-{index:02d}.synthetic.example",
    } for index in range(1, count + 1)]


def company_ui_path(company: dict[str, str]) -> str:
    return f"/object/company/{company['id']}"


def evaluator_projection(companies: list[dict[str, str]]) -> dict[str, Any]:
    """Return the evaluator-side projection of the synthetic set for baseline/observation states."""
    return {
        "companies": [{
            "id": company["id"],
            "name": company["name"],
            "domain": company["domain"],
            "created_at": "2026-09-09T00:00:00+00:00",
            "updated_at": "2026-09-09T00:00:00+00:00",
            "deleted_at": None,
        } for company in companies],
        "guards": {
            "people": {"row_count": 0, "digest": "sha256:" + "0" * 64},
            "opportunities": {"row_count": 0, "digest": "sha256:" + "0" * 64},
        },
    }


class SyntheticCompanyExecutor:
    """Serve ``query_companies`` from an in-memory synthetic set with the product result shape."""

    def __init__(self, companies: list[dict[str, str]], *, complete: bool = True,
                 failure: str | None = None):
        self.companies = [dict(company) for company in companies]
        self.complete = complete
        self.failure = failure
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def tool_schemas() -> tuple[dict[str, Any], ...]:
        return (json.loads(json.dumps(QUERY_COMPANIES_SCHEMA)),)

    @staticmethod
    def tool_effects() -> dict[str, str]:
        return {"query_companies": "read"}

    def execute(self, name: str, args: dict[str, Any], *,
                context: Any = None) -> BusinessToolResult:
        self.calls.append({
            "name": name,
            "args": dict(args),
            "operation_context_digest": getattr(context, "binding_digest", None),
        })
        if name != "query_companies":
            raise BusinessToolError("unknown synthetic business tool")
        if self.failure:
            raise BusinessToolError(self.failure)
        needle = str(args.get("name_contains", "")).casefold().strip()
        limit = args.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BusinessToolError("limit must be an integer from 1 to 50")
        rows = [{
            "name": company["name"], "domain": company["domain"],
            "ui_path": company_ui_path(company),
        } for company in self.companies if not needle or needle in company["name"].casefold()]
        visible = rows[:limit]
        result_truncated = len(rows) > limit or not self.complete
        return BusinessToolResult(
            content={
                "companies": visible,
                "matched_count": len(rows),
                "source_total": len(self.companies),
                "scope": "synthetic_workspace",
                "completeness": {
                    "source_total_known": True,
                    "source_complete": self.complete,
                    "result_truncated": result_truncated,
                    "pages_scanned": 1,
                    "records_scanned": len(self.companies),
                    "incomplete_reason": "" if self.complete else "synthetic_bounded_scan",
                },
            },
            audit={"returned_count": len(visible), "matched_count": len(rows)},
            allowed_routes=tuple(row["ui_path"] for row in visible),
        )


def synthetic_business_runtime(executor: SyntheticCompanyExecutor) -> BusinessToolRuntime:
    skill = BusinessSkill.from_bytes(json.dumps({
        "schema_version": 1,
        "skill_id": "synthetic.companies-query",
        "title": "Query synthetic companies",
        "intent": "Read the companies currently visible to the synthetic workspace user.",
        "triggers": ["company", "companies", "公司"],
        "instructions": [
            "Call query_companies to obtain current workspace data.",
            "When the user asks to open one company, call query_companies with their name and "
            "then open_allowed_route with that result's exact ui_path.",
        ],
        "tool_names": ["query_companies"],
        "provenance": ["synthetic:companies"],
        "read_only": True,
    }).encode("utf-8"), artifact_uri="memory://synthetic/companies-query.json")
    return BusinessToolRuntime(
        (skill,), executor, application_id="twenty",
        required_scopes={"query_companies": "companies:read"})


def session_binding(storage_state: str | Path | dict[str, Any]) -> str:
    """Digest the synthetic session cookie the way the real adapter digests Twenty's."""
    if isinstance(storage_state, dict):
        payload: Any = storage_state
    else:
        try:
            payload = json.loads(Path(storage_state).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessToolError("synthetic browser storage state is unavailable") from exc
    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    values = [
        cookie.get("value") for cookie in (cookies if isinstance(cookies, list) else [])
        if isinstance(cookie, dict) and cookie.get("name") == SESSION_COOKIE
    ]
    if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
        raise BusinessToolError("synthetic browser storage state must contain one session cookie")
    return hashlib.sha256(values[0].encode("utf-8")).hexdigest()


def write_storage_state(path: Path, *, origin: str,
                        session_value: str = "synthetic-session-1") -> Path:
    """Write a Playwright storage state holding one synthetic (non-HttpOnly) session cookie."""
    host = urlsplit(origin).hostname or "127.0.0.1"
    state = {
        "cookies": [{
            "name": SESSION_COOKIE, "value": session_value, "domain": host, "path": "/",
            "expires": -1, "httpOnly": False, "secure": False, "sameSite": "Lax",
        }],
        "origins": [],
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    return path


class SyntheticTwentyAdapter(TwentyApplicationAdapter):
    """Keep the Twenty adapter interface without OAuth files, Metadata API or network access."""

    def __init__(self, *, origin: str, runtime: BusinessToolRuntime,
                 identity_failure: Exception | None = None):
        self.origin = origin
        self.page_controller_module = origin + "/woa/page-controller.js"
        self.business_tools = runtime
        self.identity_failure = identity_failure
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id="twenty",
            display_name="Synthetic Twenty",
            origin=origin,
            navigation_mode="spa",
            page_controller_module=self.page_controller_module,
            catalog_sha256=runtime.catalog_sha256,
        )

    def page_session_binding(self, storage_state: str | Path | dict, *,
                             now_ms: int | None = None) -> str:
        return session_binding(storage_state)

    def bind_browser_session(self, storage_state_path: str | Path, *,
                             delegation_id: str, lifetime_s: int = 300,
                             now_ms: int | None = None) -> PrincipalContext:
        if self.identity_failure is not None:
            raise self.identity_failure
        session_binding(storage_state_path)
        now = int(time.time() * 1000) if now_ms is None else now_ms
        return PrincipalContext(
            schema_version=PRINCIPAL_CONTEXT_VERSION,
            application_id="twenty",
            tenant_id="synthetic-tenant",
            workspace_id="synthetic-workspace",
            actor_id="synthetic-actor",
            delegation_id=delegation_id,
            issued_at=now,
            expires_at=now + lifetime_s * 1_000,
            scopes=("companies:read",),
        )


_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Synthetic Twenty</title></head>
  <body>
    <main id="view"></main>
    <script>
      const COMPANIES = __COMPANIES__
      const SESSION_COOKIE = __SESSION_COOKIE__
      function node(tag, text) {
        const element = document.createElement(tag)
        element.textContent = text
        return element
      }
      function render() {
        const view = document.getElementById('view')
        view.replaceChildren()
        const match = /^\\/object\\/company\\/([^/]+)$/.exec(location.pathname)
        if (match) {
          const company = COMPANIES.find((item) => item.id === match[1])
          if (!company) {
            view.append(node('h1', 'Record not found'))
            return
          }
          view.append(node('h1', company.name), node('p', 'Domain'), node('p', company.domain))
          return
        }
        view.append(node('h1', 'Companies'))
        const button = node('button', 'Switch session')
        button.type = 'button'
        button.id = 'switch-session'
        button.addEventListener('click', () => {
          document.cookie = `${SESSION_COOKIE}=replaced-session; path=/`
          view.append(node('p', 'Session switched'))
        })
        view.append(button, node('p', 'Company records are loaded through the workspace API.'))
      }
      window.addEventListener('popstate', render)
      render()
    </script>
  </body>
</html>
"""


class SyntheticTwentyPage:
    """Serve one SPA document for every path on a loopback port, without company data on the list page."""

    def __init__(self, companies: list[dict[str, str]]):
        document = _PAGE_TEMPLATE.replace(
            "__COMPANIES__", json.dumps(companies, ensure_ascii=False)
        ).replace("__SESSION_COOKIE__", json.dumps(SESSION_COOKIE))
        self._document = document.encode("utf-8")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.origin = ""

    def __enter__(self) -> "SyntheticTwentyPage":
        document = self._document

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server naming
                if self.path.startswith("/favicon"):
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(document)))
                self.end_headers()
                self.wfile.write(document)

            def log_message(self, *_args: Any) -> None:
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        self.origin = f"http://127.0.0.1:{server.server_address[1]}"
        return self

    def __exit__(self, *_args: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


DEVELOPMENT_TASKSET = (
    Path(__file__).resolve().parents[1] / "twenty" / "tasksets"
    / "twenty-companies-development-v1.json")


def synthetic_state(companies: list[dict[str, str]], *, role: str) -> dict[str, Any]:
    """Build an evaluator baseline/observation state for the synthetic company set."""
    from evals.twenty.evaluator import make_state

    projection = evaluator_projection(companies)
    return make_state(
        fixture=SYNTHETIC_FIXTURE, companies=projection["companies"],
        guards=projection["guards"], captured_at="2026-09-09T00:00:00+00:00", role=role)


def development_case(task_id: str, companies: list[dict[str, str]]) -> dict[str, Any]:
    """Instantiate one public development task against the synthetic baseline."""
    from evals.twenty.taskset import instantiate_task

    task_set = json.loads(DEVELOPMENT_TASKSET.read_text(encoding="utf-8"))
    return instantiate_task(task_set, task_id, synthetic_state(companies, role="baseline"))


def evaluate_run(case: dict[str, Any], companies: list[dict[str, str]],
                 result: dict[str, Any]) -> dict[str, Any]:
    """Score a runner artifact with the independent evaluator against unchanged synthetic state."""
    from evals.twenty.evaluator import evaluate

    return evaluate(
        task=case["evaluator_task"],
        before=synthetic_state(companies, role="baseline"),
        after=synthetic_state(companies, role="observation"),
        evidence=result["evidence"])


def query_answer(companies: list[dict[str, str]], *, complete: bool = True) -> str:
    """Render the strict evidence JSON a faithful engine would return for ``companies``."""
    return json.dumps({
        "artifact_version": 1,
        "execution_status": "completed",
        "complete": complete,
        "companies": [{"name": company["name"], "domain": company["domain"]}
                      for company in companies],
    })
