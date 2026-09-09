"""Model-free apparatus for Web Operation Agent runner tests and qualification.

``ScriptedWoaChat`` answers the product Agent loop from a fixed script with content-free
accounting. ``SimulatedRuntime`` plays the embedded browser runtime over the real remote
session channel for a synthetic SPA whose list page exposes no company data, so a run
exercises the real hello/lease, observation, claim and result protocol without Chromium.
Nothing here contacts a model, a spend ledger or a Twenty instance.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from evals.pageagent.scripted import SCRIPTED_MODEL, SESSION_COOKIE, ScriptedMeter
from web_operation_agent.model import ChatModel, ProviderError
from web_operation_agent.privacy import (
    SENSITIVE_CREDENTIAL_NAME, SESSION_HANDOFF_QUERY_KEYS, redact_sensitive_assignments,
    redact_session_handoffs,
)
from web_operation_agent.remote import RemoteProtocolError, RemoteSessionChannel, SessionGrant
from web_operation_agent.schemas import (
    SCHEMA_VERSIONS, ActionEnvelope, ActionReceipt, ActionResultEnvelope, CancellationRequest,
    ClientHello, ObservationEnvelope, PageObservation,
)


RUNTIME_CAPABILITIES = (
    "dom.observe", "dom.click", "dom.fill", "dom.select", "dom.submit",
    "page.goto", "page.back", "page.wait",
)
_RECORD_PATH = re.compile(r"/object/company/([^/]+)\Z")


class _WoaScriptedMeter(ScriptedMeter):
    def report(self) -> dict[str, Any]:
        with self.client.lock:
            return {"provider_calls": self.client.dispatched, "cost_cny": 0.0}


class ScriptedWoaChat(ChatModel):
    """Answer ``ServerAgentSession`` model calls from a fixed reply script.

    ``replies`` holds ``(tool_name, arguments)`` pairs; a ``None`` tool name returns the
    arguments as plain assistant content, which is how the product loop accepts a final
    business answer. ``failures`` maps a 1-based call sequence to a provider error code
    raised instead of a reply. The class keeps the ``ChatModel`` type the product boundary
    expects but never runs the real constructor, so it holds no endpoint, credential or
    ledger; a shared scripted meter projects its calls the way ``SpendMeter.run_report`` does.
    """

    def __init__(self, replies: Sequence[tuple[str | None, Any]], *,
                 model: str = SCRIPTED_MODEL,
                 failures: dict[int, str] | None = None,
                 after_call: Callable[[int], None] | None = None):
        self.model = model
        self.replies = list(replies)
        self.failures = dict(failures or {})
        self.after_call = after_call
        self.requests: list[dict[str, Any]] = []
        self.responded: list[dict[str, Any]] = []
        self.dispatched = 0
        self.lock = threading.Lock()
        self.meter = _WoaScriptedMeter(self)

    def complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                 max_tokens: int | None = None, options: dict | None = None,
                 timeout_s: float | None = None) -> dict[str, Any]:
        with self.lock:
            self.dispatched += 1
            sequence = self.dispatched
        self.requests.append({
            "sequence": sequence,
            "run_id": run_id,
            "tool_names": sorted(
                tool.get("function", {}).get("name", "") for tool in (tools or [])
                if isinstance(tool, dict)),
            "message_count": len(messages),
            "timeout_s": timeout_s,
        })
        try:
            if sequence in self.failures:
                raise ProviderError(
                    "scripted provider failure", code=self.failures[sequence],
                    retryable=False, request_id=uuid.uuid4().hex)
            if sequence > len(self.replies):
                name, arguments = "finish", {
                    "done": False, "why": "The scripted model has no further step."}
            else:
                name, arguments = self.replies[sequence - 1]
            if name is None:
                reply: dict[str, Any] = {"content": str(arguments)}
            else:
                reply = {"tool_calls": [{
                    "id": f"call-{sequence}",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }]}
            with self.lock:
                self.responded.append({
                    "model": self.model,
                    "usage": {
                        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                        "prompt_tokens_details": {"cached_tokens": 0},
                        "completion_tokens_details": {"reasoning_tokens": 5},
                    },
                })
            return reply
        finally:
            if self.after_call is not None:
                self.after_call(sequence)


class RecordingBusinessRuntime:
    """Delegate to the product ``BusinessToolRuntime`` and keep successful read results.

    The runner needs the exact companies and routes its own tool results disclosed so the
    public answer and the final page can be checked against them. The wrapper changes no
    behaviour; it copies each successful result's content after the product returned it.
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self.results: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def execute(self, name: str, args: Any, loaded_ids: set[str], *,
                context: Any = None) -> Any:
        result = self._inner.execute(name, args, loaded_ids, context=context)
        self.results.append(json.loads(json.dumps(result.content, ensure_ascii=False)))
        return result


class SyntheticPageModel:
    """What the synthetic SPA shows at each path: no company data on the list page."""

    def __init__(self, companies: Sequence[dict[str, str]]):
        self.companies = [dict(company) for company in companies]

    def render(self, path: str) -> tuple[str, str]:
        match = _RECORD_PATH.fullmatch(path)
        if match:
            company = next(
                (item for item in self.companies if item["id"] == match.group(1)), None)
            if company is None:
                return "Record not found", "Record not found"
            return company["name"], f"{company['name']}\nDomain\n{company['domain']}"
        return "Companies", "Companies\nCompany records are loaded through the workspace API."


def args_echo(value: Any, key: str = "") -> Any:
    """Mirror the embedded runtime's outbound argument redaction for receipt verification."""
    if (key == "value" or SENSITIVE_CREDENTIAL_NAME.search(key)
            or key.lower() in SESSION_HANDOFF_QUERY_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {child: args_echo(item, str(child)) for child, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [args_echo(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_assignments(redact_session_handoffs(value))
    return value


class SimulatedRuntime:
    """Play the embedded runtime for one session over the real channel, without a browser."""

    def __init__(self, channel: RemoteSessionChannel, grant: SessionGrant, *, origin: str,
                 entry_path: str, page_model: SyntheticPageModel,
                 storage_state: dict[str, Any] | None = None,
                 client_id: str = "simulated-runtime", poll_s: float = 0.01):
        self.channel = channel
        self.grant = grant
        self.origin = origin
        self._storage_state = json.loads(json.dumps(storage_state or {"cookies": [], "origins": []}))
        self.page_model = page_model
        self.client_id = client_id
        self.poll_s = poll_s
        self.current_path = entry_path
        self.epoch = 0
        self.lease_id = ""
        self.active_message_id = ""
        self.served: set[str] = set()
        self.executed: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors: list[str] = []

    @property
    def current_url(self) -> str:
        return self.origin + self.current_path

    def _observation(self, caused_by_action_id: str = "") -> ObservationEnvelope:
        self.epoch += 1
        title, content = self.page_model.render(self.current_path)
        observation = PageObservation(
            schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=self.epoch,
            url=self.current_url, title=title, landmarks=[], elements=[], content=content)
        return ObservationEnvelope(
            schema_version=SCHEMA_VERSIONS["ObservationEnvelope"],
            session_id=self.channel.session_id,
            observation_id=f"{self.client_id}:{self.epoch}",
            page_epoch=self.epoch, observation=observation,
            caused_by_action_id=caused_by_action_id)

    def start(self) -> "SimulatedRuntime":
        hello = ClientHello(
            schema_version=SCHEMA_VERSIONS["ClientHello"],
            session_id=self.channel.session_id, client_id=self.client_id,
            runtime_instance_id=f"{self.client_id}-document-1",
            runtime="woa-simulated-runtime/1", capabilities=RUNTIME_CAPABILITIES,
            page_origin=self.origin, application_id=self.grant.application_id,
            principal_digest=self.grant.principal_digest)
        self.lease_id = self.channel.accept_hello(hello)["lease_id"]
        self.channel.accept_observation(self._observation(), lease_id=self.lease_id)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def observe_now(self) -> ObservationEnvelope:
        """Post a fresh observation the way ``submitTask`` does before a message."""
        envelope = self._observation()
        self.channel.accept_observation(envelope, lease_id=self.lease_id)
        return envelope

    def submit_task(self, text: str) -> str:
        """Bind one goal to a fresh observation and post it, exactly as ``submitTask`` does."""
        envelope = self.observe_now()
        message_id = f"message-{uuid.uuid4().hex[:12]}"
        self.channel.accept_message({
            "message_id": message_id, "text": text,
            "page_url": envelope.observation.url,
        }, lease_id=self.lease_id)
        self.active_message_id = message_id
        return message_id

    def cancel_active(self) -> dict[str, Any]:
        """Stop the submitted task the way the assistant panel's stop control does."""
        if not self.active_message_id:
            raise RuntimeError("no task has been submitted on this runtime")
        return self.cancel(self.active_message_id)

    def storage_state(self) -> dict[str, Any]:
        """Return the browser state the trusted host re-checks the session binding against."""
        return json.loads(json.dumps(self._storage_state))

    def replace_session_cookie(self, value: str = "replaced-session") -> None:
        """Simulate the page replacing its own session cookie mid-run."""
        for cookie in self._storage_state.get("cookies", []):
            if isinstance(cookie, dict) and cookie.get("name") == SESSION_COOKIE:
                cookie["value"] = value

    def cancel(self, message_id: str) -> dict[str, Any]:
        return self.channel.accept_cancellation(CancellationRequest(
            schema_version=SCHEMA_VERSIONS["CancellationRequest"],
            session_id=self.channel.session_id, message_id=message_id),
            lease_id=self.lease_id)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                action = self.channel.pending_action
            except RemoteProtocolError:
                return
            if action is None or action.action_id in self.served:
                time.sleep(self.poll_s)
                continue
            self.served.add(action.action_id)
            try:
                claim = self.channel.claim_action(
                    {"action_id": action.action_id}, lease_id=self.lease_id)
                if not claim.get("claimed"):
                    continue
                result = self._execute(action)
                self.channel.accept_result(result, lease_id=self.lease_id)
            except RemoteProtocolError as exc:
                self.errors.append(exc.code)
            except Exception as exc:  # a simulated runtime bug must surface in tests
                self.errors.append(type(exc).__name__)

    def _execute(self, action: ActionEnvelope) -> ActionResultEnvelope:
        command = action.command
        url_before = self.current_url
        ok, error_code, error_detail = True, "", ""
        if command.action == "goto":
            target = command.args.get("url")
            parsed = urlsplit(target) if isinstance(target, str) else None
            if (parsed is None or not target.startswith("/") or target.startswith("//")
                    or parsed.scheme or parsed.netloc):
                ok, error_code, error_detail = False, "origin_refused", "route is not same-origin"
            else:
                self.current_path = parsed.path
        elif command.action not in {"observe", "wait_for"}:
            ok, error_code = False, "element_not_actionable"
            error_detail = "the synthetic page has no actionable controls"
        self.executed.append({"action": command.action, "ok": ok, "path": self.current_path})
        envelope = self._observation(caused_by_action_id=action.action_id)
        receipt = ActionReceipt(
            schema_version=SCHEMA_VERSIONS["ActionReceipt"], command_id=action.action_id,
            action=command.action, args_echo=args_echo(command.args), ok=ok,
            error_code=error_code, error_detail=error_detail, url_before=url_before,
            url_after=envelope.observation.url,
            navigated=url_before != envelope.observation.url,
            epoch_before=action.page_epoch, epoch_after=envelope.page_epoch, elapsed_ms=1,
            observation=envelope.observation)
        return ActionResultEnvelope(
            schema_version=SCHEMA_VERSIONS["ActionResultEnvelope"],
            session_id=self.channel.session_id, turn_id=action.turn_id,
            action_id=action.action_id, based_on_observation_id=action.based_on_observation_id,
            page_epoch=action.page_epoch, command_digest=action.command_digest,
            status="completed", observation_after=envelope, receipt=receipt)
