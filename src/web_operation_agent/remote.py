"""In-process state and browser adapter for the embedded remote runtime.

The HTTP gateway and the server agent share these objects in one Python process.  The
gateway accepts browser facts and wakes the synchronous ``ServerAgentSession`` adapter;
the adapter publishes exactly one action and blocks for its idempotent result.  No model,
source material, control plan or evaluator crosses this boundary.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .browser import TransportError
from .privacy import (SESSION_HANDOFF_QUERY_KEYS, SENSITIVE_CREDENTIAL_NAME,
                      has_unredacted_sensitive_assignment,
                      has_unredacted_session_handoff,
                      redact_sensitive_assignments, redact_session_handoffs)
from .schemas import (
    SCHEMA_VERSIONS,
    ActionCommand,
    ActionEnvelope,
    ActionReceipt,
    ActionResultEnvelope,
    ApprovalDecision,
    ApprovalRequest,
    ClientHello,
    ObservationEnvelope,
    PageObservation,
    RECEIPT_ERRORS,
    SchemaError,
    TaskEvent,
    canonical_json,
    parse_event_id,
)


class RemoteProtocolError(ValueError):
    """A remote message conflicts with established session state."""

    def __init__(self, message: str, *, code: str = "protocol_error", status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


class RemoteSessionClosed(RemoteProtocolError):
    def __init__(self, message: str = "remote session is closed"):
        super().__init__(message, code="session_closed", status=410)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RemoteProtocolError("expected an absolute http(s) origin", status=400)
    return f"{parsed.scheme}://{parsed.netloc}"


def _event_sequence(event_id: str) -> int:
    try:
        return parse_event_id(event_id, name="Last-Event-ID", allow_empty=True)
    except SchemaError as exc:
        raise RemoteProtocolError(str(exc), status=400) from exc


def _remote_args_echo(value, key: str = ""):
    """Mirror the Web Runtime's outbound argument redaction for receipt verification."""
    if (key == "value" or SENSITIVE_CREDENTIAL_NAME.search(key)
            or key.lower() in SESSION_HANDOFF_QUERY_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {child: _remote_args_echo(item, str(child))
                for child, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_remote_args_echo(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_assignments(redact_session_handoffs(value))
    return value


def _safe_payload(value) -> None:
    if (has_unredacted_session_handoff(value, url_decode_depth=3)
            or has_unredacted_sensitive_assignment(value, url_decode_depth=3)):
        raise RemoteProtocolError(
            "browser payload contains an unredacted credential value",
            code="privacy_boundary",
            status=400,
        )


def ensure_safe_browser_payload(value) -> None:
    """Validate data before a product component records and publishes it to the browser."""
    _safe_payload(value)


def _copy_json(value):
    """Detach a JSON value from its caller before it enters shared session state."""
    return json.loads(canonical_json(value))


def _copy_wire(value):
    """Round-trip one wire value so frozen dataclasses do not retain mutable children."""
    return type(value).from_json(_copy_json(value.to_json()))


@dataclass(frozen=True)
class SessionGrant:
    """The one-time bootstrap material an application shell gives its embedded runtime."""

    session_id: str
    client_token: str = field(repr=False)
    allowed_origins: tuple[str, ...]


class RemoteSessionChannel:
    """Thread-safe journal and rendezvous point for one server-owned Agent session."""

    def __init__(self, session_id: str, allowed_origins: tuple[str, ...]):
        self.session_id = session_id
        self.allowed_origins = frozenset(allowed_origins)
        self._condition = threading.Condition(threading.RLock())
        self._hello: ClientHello | None = None
        self._active_lease_id = ""
        self._active_runtime_instance_id = ""
        self._runtime_leases: dict[str, str] = {}
        self._lease_clients: dict[str, str] = {}
        self._retired_runtime_instances: set[str] = set()
        self._latest_observation: ObservationEnvelope | None = None
        self._observation_digests: dict[str, str] = {}
        self._actions: dict[str, ActionEnvelope] = {}
        self._action_observations: dict[str, ObservationEnvelope] = {}
        self._results: dict[str, ActionResultEnvelope] = {}
        self._action_claims: dict[str, str] = {}
        self._pending_action_id = ""
        self._events: list[TaskEvent] = []
        self._messages: deque[dict] = deque()
        self._message_digests: dict[str, str] = {}
        self._approvals: dict[str, ApprovalRequest] = {}
        self._approval_decisions: dict[str, ApprovalDecision] = {}
        self._pending_approval_id = ""
        self._closed = False
        self._append_event("session_started", {"state": "waiting_for_runtime"})

    @property
    def latest_event_id(self) -> str:
        with self._condition:
            return self._events[-1].event_id if self._events else ""

    @property
    def latest_observation_envelope(self) -> ObservationEnvelope | None:
        with self._condition:
            return (_copy_wire(self._latest_observation)
                    if self._latest_observation is not None else None)

    @property
    def pending_action(self) -> ActionEnvelope | None:
        with self._condition:
            action = self._actions.get(self._pending_action_id)
            return _copy_wire(action) if action is not None else None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RemoteSessionClosed()

    def _append_event(self, kind: str, payload: dict, *, turn_id: str = "") -> TaskEvent:
        with self._condition:
            sequence = len(self._events) + 1
            event = TaskEvent(
                schema_version=SCHEMA_VERSIONS["TaskEvent"],
                event_id=f"e{sequence:08d}",
                session_id=self.session_id,
                sequence=sequence,
                kind=kind,
                turn_id=turn_id,
                payload=_copy_json(payload),
            )
            self._events.append(event)
            self._condition.notify_all()
            return _copy_wire(event)

    def _require_active_lease(self, lease_id: str) -> None:
        if not isinstance(lease_id, str) or not lease_id:
            raise RemoteProtocolError("X-WOA-Lease is required", code="invalid_lease", status=401)
        if lease_id != self._active_lease_id:
            raise RemoteProtocolError(
                "the runtime lease has been replaced by a newer document",
                code="runtime_replaced",
                status=409,
            )

    def validate_active_lease(self, lease_id: str) -> None:
        with self._condition:
            self._require_active_lease(lease_id)

    # -- browser -> server ----------------------------------------------------------
    def accept_hello(self, hello: ClientHello) -> dict:
        with self._condition:
            self._ensure_open()
            hello = _copy_wire(hello)
            if hello.session_id != self.session_id:
                raise RemoteProtocolError("hello belongs to another session", status=400)
            if _origin(hello.page_origin) not in self.allowed_origins:
                raise RemoteProtocolError("page origin is outside this session", code="origin_refused", status=403)
            cursor = _event_sequence(hello.last_event_id)
            if cursor > len(self._events):
                raise RemoteProtocolError("Last-Event-ID is ahead of this session")
            if self._hello is not None and hello.client_id != self._hello.client_id:
                raise RemoteProtocolError("another browser client already owns this session")
            runtime_instance_id = hello.runtime_instance_id
            if runtime_instance_id in self._retired_runtime_instances:
                raise RemoteProtocolError(
                    "this runtime instance was replaced by a newer document",
                    code="runtime_replaced",
                )
            existing_lease = self._runtime_leases.get(runtime_instance_id)
            if existing_lease:
                if existing_lease != self._active_lease_id:
                    raise RemoteProtocolError(
                        "this runtime instance no longer owns the session",
                        code="runtime_replaced",
                    )
                prior = self._hello
                stable = ("session_id", "client_id", "runtime", "runtime_instance_id",
                          "capabilities", "page_origin")
                if prior is not None and any(getattr(prior, name) != getattr(hello, name)
                                             for name in stable):
                    raise RemoteProtocolError("runtime instance changed its hello contract")
                self._hello = hello
                return {"accepted": True, "lease_id": existing_lease,
                        "server_event_id": self.latest_event_id}

            if self._active_runtime_instance_id:
                self._retired_runtime_instances.add(self._active_runtime_instance_id)
            lease_id = secrets.token_urlsafe(24)
            self._active_runtime_instance_id = runtime_instance_id
            self._active_lease_id = lease_id
            self._runtime_leases[runtime_instance_id] = lease_id
            self._lease_clients[lease_id] = hello.client_id
            self._hello = hello
            self._condition.notify_all()
            return {"accepted": True, "lease_id": lease_id,
                    "server_event_id": self.latest_event_id}

    def _validate_observation(self, envelope: ObservationEnvelope) -> tuple[bool, str]:
        if envelope.session_id != self.session_id:
            raise RemoteProtocolError("observation belongs to another session", status=400)
        if _origin(envelope.observation.url) not in self.allowed_origins:
            raise RemoteProtocolError("observation URL is outside this session", code="origin_refused", status=403)
        _safe_payload(envelope.to_json())
        fingerprint = envelope.content_digest
        previous = self._observation_digests.get(envelope.observation_id)
        if previous is not None:
            if previous != fingerprint:
                raise RemoteProtocolError("observation_id was reused with different content")
            return True, fingerprint
        latest = self._latest_observation
        if latest is not None and envelope.page_epoch <= latest.page_epoch:
            raise RemoteProtocolError(
                f"observation epoch {envelope.page_epoch} is not newer than {latest.page_epoch}",
                code="stale_observation",
            )
        return False, fingerprint

    def _record_observation(self, envelope: ObservationEnvelope) -> bool:
        duplicate, fingerprint = self._validate_observation(envelope)
        if duplicate:
            return True
        self._observation_digests[envelope.observation_id] = fingerprint
        self._latest_observation = envelope
        self._condition.notify_all()
        return False

    def accept_observation(self, envelope: ObservationEnvelope, *, lease_id: str) -> dict:
        with self._condition:
            self._ensure_open()
            self._require_active_lease(lease_id)
            envelope = _copy_wire(envelope)
            duplicate = self._record_observation(envelope)
            return {"accepted": True, "duplicate": duplicate}

    def accept_result(self, result: ActionResultEnvelope, *, lease_id: str) -> dict:
        with self._condition:
            self._require_active_lease(lease_id)
            result = _copy_wire(result)
            if result.session_id != self.session_id:
                raise RemoteProtocolError("result belongs to another session", status=400)
            action = self._actions.get(result.action_id)
            if action is None:
                raise RemoteProtocolError("result names an unknown action", code="unknown_action", status=404)
            for field in ("turn_id", "based_on_observation_id", "page_epoch",
                          "command_digest"):
                if getattr(result, field) != getattr(action, field):
                    raise RemoteProtocolError(f"result {field} does not match its action")
            if result.receipt is not None:
                if result.receipt.action != action.command.action:
                    raise RemoteProtocolError("receipt action does not match its command")
                if result.receipt.args_echo != _remote_args_echo(action.command.args):
                    raise RemoteProtocolError("receipt arguments do not match its command")
                based_on = self._action_observations.get(action.action_id)
                if (based_on is None
                        or result.receipt.url_before != based_on.observation.url):
                    raise RemoteProtocolError(
                        "receipt url_before does not match its based-on observation")
            _safe_payload(result.to_json())
            prior = self._results.get(result.action_id)
            if prior is not None:
                if prior.content_digest != result.content_digest:
                    raise RemoteProtocolError("action_id was replayed with a conflicting result")
                return {"accepted": True, "duplicate": True}
            self._ensure_open()
            claimed_lease = self._action_claims.get(result.action_id)
            if result.status in {"completed", "indeterminate"}:
                if not claimed_lease:
                    raise RemoteProtocolError(
                        "an executed action result requires a prior claim",
                        code="action_not_claimed",
                    )
                # A full-page navigation replaces the JavaScript context.  Its new active
                # lease may finish the action claimed by the old document, but a different
                # browser client can never take over that claim.
                if self._lease_clients.get(claimed_lease) != self._lease_clients.get(lease_id):
                    raise RemoteProtocolError(
                        "the active browser client does not own this action claim",
                        code="claim_owner_mismatch",
                        status=403,
                    )
            if result.observation_after is not None:
                self._record_observation(result.observation_after)
            self._results[result.action_id] = result
            if self._pending_action_id == result.action_id:
                self._pending_action_id = ""
            self._condition.notify_all()
            return {"accepted": True, "duplicate": False}

    def claim_action(self, claim: dict, *, lease_id: str) -> dict:
        """Atomically grant one live document the right to execute one action once."""
        with self._condition:
            self._ensure_open()
            self._require_active_lease(lease_id)
            if not isinstance(claim, dict) or set(claim) != {"action_id"}:
                raise RemoteProtocolError(
                    "claim requires exactly action_id", code="invalid_claim", status=400)
            action_id = claim.get("action_id")
            if not isinstance(action_id, str) or not action_id:
                raise RemoteProtocolError(
                    "claim action_id must be a non-empty string",
                    code="invalid_claim",
                    status=400,
                )
            action = self._actions.get(action_id)
            if action is None:
                raise RemoteProtocolError(
                    "claim names an unknown action", code="unknown_action", status=404)
            if action_id in self._results:
                raise RemoteProtocolError(
                    "action already has a result", code="action_completed")
            if self._pending_action_id != action_id:
                raise RemoteProtocolError(
                    "action is no longer pending", code="action_not_pending")
            previous = self._action_claims.get(action_id)
            if previous:
                if previous == lease_id:
                    return {"claimed": True, "duplicate": True}
                return {"claimed": False, "reason": "already_claimed"}
            if action.deadline <= _now_ms():
                raise RemoteProtocolError(
                    "action deadline expired before claim",
                    code="deadline_expired",
                )
            self._action_claims[action_id] = lease_id
            return {"claimed": True, "duplicate": False}

    def accept_message(self, message: dict, *, lease_id: str) -> dict:
        with self._condition:
            self._ensure_open()
            self._require_active_lease(lease_id)
            allowed = {"message_id", "text", "page_url"}
            if not isinstance(message, dict) or set(message) != allowed:
                raise RemoteProtocolError("message requires exactly message_id, text, and page_url", status=400)
            message_id, text = message["message_id"], message["text"]
            if not isinstance(message_id, str) or not message_id:
                raise RemoteProtocolError("message_id must be a non-empty string", status=400)
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise RemoteProtocolError("message text must contain 1-2000 characters", status=400)
            if _origin(message["page_url"]) not in self.allowed_origins:
                raise RemoteProtocolError("message page URL is outside this session", status=403)
            _safe_payload(message)
            fingerprint = hashlib.sha256(repr(sorted(message.items())).encode()).hexdigest()
            prior = self._message_digests.get(message_id)
            if prior is not None:
                if prior != fingerprint:
                    raise RemoteProtocolError("message_id was reused with different content")
                return {"accepted": True, "duplicate": True}
            self._message_digests[message_id] = fingerprint
            self._messages.append(_copy_json(message))
            self._condition.notify_all()
            return {"accepted": True, "duplicate": False}

    def accept_approval(self, decision: ApprovalDecision, *, lease_id: str) -> dict:
        """Accept one idempotent response from the currently authenticated page session."""
        with self._condition:
            self._ensure_open()
            self._require_active_lease(lease_id)
            decision = _copy_wire(decision)
            if decision.session_id != self.session_id:
                raise RemoteProtocolError("approval belongs to another session", status=400)
            request = self._approvals.get(decision.approval_id)
            if request is None:
                raise RemoteProtocolError(
                    "approval response names an unknown request",
                    code="unknown_approval",
                    status=404,
                )
            previous = self._approval_decisions.get(decision.approval_id)
            if previous is not None:
                if previous.content_digest != decision.content_digest:
                    raise RemoteProtocolError(
                        "approval response conflicts with the recorded decision",
                        code="approval_conflict",
                    )
                return {"accepted": True, "duplicate": True}
            if request.expires_at <= _now_ms():
                raise RemoteProtocolError(
                    "approval request expired before the decision arrived",
                    code="approval_expired",
                )
            if self._pending_approval_id != decision.approval_id:
                raise RemoteProtocolError(
                    "approval request is no longer pending",
                    code="approval_not_pending",
                )
            self._approval_decisions[decision.approval_id] = decision
            self._pending_approval_id = ""
            self._append_event("progress", {
                "state": "approval_recorded",
                "approval_id": decision.approval_id,
                "decision": decision.decision,
            })
            self._condition.notify_all()
            return {"accepted": True, "duplicate": False}

    # -- server -> browser ----------------------------------------------------------
    def enqueue_action(self, action: ActionEnvelope) -> TaskEvent:
        with self._condition:
            self._ensure_open()
            action = _copy_wire(action)
            if action.session_id != self.session_id:
                raise RemoteProtocolError("action belongs to another session", status=400)
            _safe_payload(action.to_json())
            if self._hello is None:
                raise RemoteProtocolError("cannot act before the embedded runtime hello")
            capability = {
                "observe": "dom.observe", "click": "dom.click", "fill": "dom.fill",
                "select": "dom.select", "submit": "dom.submit", "goto": "page.goto",
                "back": "page.back", "wait_for": "page.wait",
            }[action.command.action]
            if capability not in self._hello.capabilities:
                raise RemoteProtocolError(
                    f"embedded runtime does not advertise {capability}",
                    code="capability_refused",
                )
            previous = self._actions.get(action.action_id)
            if previous is not None:
                if previous.content_digest != action.content_digest:
                    raise RemoteProtocolError("action_id was reused with different content")
                event = next(event for event in self._events
                             if event.kind == "action"
                             and event.payload["action_id"] == action.action_id)
                return _copy_wire(event)
            if self._pending_action_id:
                raise RemoteProtocolError("another action is already in flight", code="action_in_flight")
            observation = self._latest_observation
            if observation is None:
                raise RemoteProtocolError("cannot act before the first observation")
            if (action.based_on_observation_id != observation.observation_id
                    or action.page_epoch != observation.page_epoch):
                raise RemoteProtocolError("action is based on a stale observation", code="stale_observation")
            if action.deadline <= _now_ms():
                raise RemoteProtocolError("action deadline has already expired", code="deadline_expired")
            self._actions[action.action_id] = action
            self._action_observations[action.action_id] = _copy_wire(observation)
            self._pending_action_id = action.action_id
            return self._append_event("action", action.to_json(), turn_id=action.turn_id)

    def publish(self, kind: str, payload: dict, *, turn_id: str = "") -> TaskEvent:
        if kind == "action":
            raise ValueError("use enqueue_action for action events")
        with self._condition:
            self._ensure_open()
            _safe_payload(payload)
            return self._append_event(kind, _copy_json(payload), turn_id=turn_id)

    def request_approval(self, request: ApprovalRequest) -> TaskEvent:
        """Publish one user-visible request while retaining no ControlPlan on the wire."""
        with self._condition:
            self._ensure_open()
            request = _copy_wire(request)
            _safe_payload(request.to_json())
            if request.expires_at <= _now_ms():
                raise RemoteProtocolError(
                    "cannot publish an already expired approval",
                    code="approval_expired",
                )
            previous = self._approvals.get(request.approval_id)
            if previous is not None:
                if previous.content_digest != request.content_digest:
                    raise RemoteProtocolError(
                        "approval_id was reused with different content",
                        code="approval_conflict",
                    )
                return next(_copy_wire(event) for event in self._events
                            if event.kind == "approval_required"
                            and event.payload["approval_id"] == request.approval_id)
            if self._pending_approval_id:
                raise RemoteProtocolError(
                    "another approval is already pending",
                    code="approval_in_flight",
                )
            self._approvals[request.approval_id] = request
            self._pending_approval_id = request.approval_id
            return self._append_event("approval_required", request.to_json())

    def events_after(self, event_id: str = "", *, lease_id: str,
                     wait_s: float = 0) -> list[TaskEvent]:
        after = _event_sequence(event_id)
        with self._condition:
            self._require_active_lease(lease_id)
            if after > len(self._events):
                raise RemoteProtocolError("Last-Event-ID is ahead of this session")
            if len(self._events) <= after and wait_s > 0:
                self._condition.wait_for(
                    lambda: (self._closed or len(self._events) > after
                             or lease_id != self._active_lease_id),
                    timeout=wait_s,
                )
            self._require_active_lease(lease_id)
            if len(self._events) <= after:
                self._ensure_open()
            return [_copy_wire(event) for event in self._events[after:]]

    # -- synchronous server adapter waits ------------------------------------------
    def wait_for_client(self, timeout_s: float) -> ClientHello:
        with self._condition:
            ready = self._condition.wait_for(lambda: self._closed or self._hello is not None,
                                             timeout=timeout_s)
            if self._hello is not None:
                return _copy_wire(self._hello)
            self._ensure_open()
            if not ready:
                raise TimeoutError("timed out waiting for embedded runtime hello")
            raise TimeoutError("timed out waiting for embedded runtime hello")

    def wait_for_observation(self, *, newer_than: int = -1, timeout_s: float) -> ObservationEnvelope:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._closed or (self._latest_observation is not None
                                         and self._latest_observation.page_epoch > newer_than),
                timeout=timeout_s,
            )
            if (self._latest_observation is not None
                    and self._latest_observation.page_epoch > newer_than):
                return _copy_wire(self._latest_observation)
            self._ensure_open()
            if not ready:
                raise TimeoutError("timed out waiting for a fresh page observation")
            raise TimeoutError("timed out waiting for a fresh page observation")

    def wait_for_result(self, action_id: str, *, timeout_s: float) -> ActionResultEnvelope:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._closed or action_id in self._results,
                timeout=timeout_s,
            )
            if action_id in self._results:
                return _copy_wire(self._results[action_id])
            self._ensure_open()
            if not ready:
                raise TimeoutError(f"timed out waiting for result of {action_id}")
            raise TimeoutError(f"timed out waiting for result of {action_id}")

    def wait_for_message(self, *, timeout_s: float | None = None) -> dict:
        with self._condition:
            ready = self._condition.wait_for(lambda: self._closed or bool(self._messages),
                                             timeout=timeout_s)
            if self._messages:
                return _copy_json(self._messages.popleft())
            self._ensure_open()
            if not ready:
                raise TimeoutError("timed out waiting for a user message")
            raise TimeoutError("timed out waiting for a user message")

    def wait_for_approval(self, approval_id: str, *, timeout_s: float) -> ApprovalDecision:
        with self._condition:
            request = self._approvals.get(approval_id)
            if request is None:
                raise RemoteProtocolError(
                    "cannot wait for an unknown approval",
                    code="unknown_approval",
                    status=404,
                )
            remaining = max(0.0, (request.expires_at - _now_ms()) / 1000)
            ready = self._condition.wait_for(
                lambda: self._closed or approval_id in self._approval_decisions,
                timeout=min(timeout_s, remaining),
            )
            decision = self._approval_decisions.get(approval_id)
            if decision is not None:
                return _copy_wire(decision)
            self._ensure_open()
            if request.expires_at <= _now_ms():
                raise RemoteProtocolError(
                    "approval request expired while awaiting a decision",
                    code="approval_expired",
                )
            if not ready:
                raise TimeoutError(f"timed out waiting for approval {approval_id}")
            raise TimeoutError(f"timed out waiting for approval {approval_id}")

    def close(self, reason: str = "session_closed") -> None:
        with self._condition:
            if self._closed:
                return
            self._append_event("terminal", {"reason": reason})
            self._closed = True
            self._condition.notify_all()


class RemoteSessionRegistry:
    """Own capability-token hashes and session channels; raw tokens are never retained."""

    def __init__(self):
        self._lock = threading.RLock()
        self._channels: dict[str, RemoteSessionChannel] = {}
        self._token_hashes: dict[str, bytes] = {}

    def create(self, *, allowed_origins: list[str] | tuple[str, ...],
               session_id: str | None = None) -> tuple[SessionGrant, RemoteSessionChannel]:
        sid = session_id or f"s-{uuid.uuid4().hex}"
        if not sid or len(sid) > 128 or any(not (c.isalnum() or c in "-_") for c in sid):
            raise ValueError("session_id must contain only letters, digits, '-' and '_'")
        origins = tuple(dict.fromkeys(_origin(value) for value in allowed_origins))
        if not origins:
            raise ValueError("at least one allowed origin is required")
        token = secrets.token_urlsafe(32)
        channel = RemoteSessionChannel(sid, origins)
        with self._lock:
            if sid in self._channels:
                raise ValueError(f"session {sid!r} already exists")
            self._channels[sid] = channel
            self._token_hashes[sid] = hashlib.sha256(token.encode()).digest()
        return SessionGrant(sid, token, origins), channel

    def authenticate(self, session_id: str, bearer: str, origin: str) -> RemoteSessionChannel:
        with self._lock:
            channel = self._channels.get(session_id)
            wanted = self._token_hashes.get(session_id)
        if channel is None or wanted is None:
            raise RemoteProtocolError("unknown session", code="unknown_session", status=404)
        got = hashlib.sha256(bearer.encode()).digest()
        if not hmac.compare_digest(got, wanted):
            raise RemoteProtocolError("invalid session credential", code="unauthorized", status=401)
        if origin not in channel.allowed_origins:
            raise RemoteProtocolError("request origin is outside this session",
                                      code="origin_refused", status=403)
        return channel

    def origin_allowed(self, session_id: str, origin: str) -> bool:
        with self._lock:
            channel = self._channels.get(session_id)
        return channel is not None and origin in channel.allowed_origins


class RemoteBrowserClient:
    """Synchronous browser interface consumed unchanged by ``ServerAgentSession``."""

    def __init__(self, channel: RemoteSessionChannel, *, timeout_s: float = 90):
        self.channel = channel
        self.timeout_s = timeout_s
        self.command_count = 0
        self.relogins = 0
        self._delivered_observation: ObservationEnvelope | None = None

    def _deliver(self, envelope: ObservationEnvelope) -> PageObservation:
        self._delivered_observation = _copy_wire(envelope)
        return _copy_wire(envelope).observation

    def _fresh_fact_receipt(self, *, action_id: str, action: str, args: dict,
                            before: ObservationEnvelope,
                            fresh: ObservationEnvelope) -> ActionReceipt:
        """Report a page change detected before dispatch, without inventing execution."""
        self._delivered_observation = _copy_wire(fresh)
        observe_only = action == "observe"
        return ActionReceipt(
            schema_version=SCHEMA_VERSIONS["ActionReceipt"],
            command_id=action_id,
            action=action,
            args_echo=_remote_args_echo(args),
            ok=observe_only,
            error_code="" if observe_only else "stale_observation",
            error_detail=("" if observe_only else
                          "the page changed before this action was dispatched"),
            url_before=before.observation.url,
            url_after=fresh.observation.url,
            navigated=before.observation.url != fresh.observation.url,
            epoch_before=before.page_epoch,
            epoch_after=fresh.page_epoch,
            observation=_copy_wire(fresh).observation,
        )

    def start(self, **_ignored) -> dict:
        try:
            hello = self.channel.wait_for_client(self.timeout_s)
            observation = self.channel.wait_for_observation(timeout_s=self.timeout_s)
        except (TimeoutError, RemoteProtocolError) as exc:
            raise TransportError(str(exc)) from exc
        self._deliver(observation)
        return {"ok": True, "client_id": hello.client_id,
                "url": observation.observation.url, "embedded": True}

    def observe(self) -> PageObservation:
        try:
            observation = self.channel.wait_for_observation(timeout_s=self.timeout_s)
            return self._deliver(observation)
        except (TimeoutError, RemoteProtocolError) as exc:
            raise TransportError(str(exc)) from exc

    def execute(self, action: str, args: dict | None = None, *,
                expect_navigation: bool = False) -> ActionReceipt:
        observation = self._delivered_observation
        if observation is None:
            raise TransportError("no page observation has been delivered to the server Agent")
        self.command_count += 1
        action_id = f"a-{uuid.uuid4().hex}"
        turn_id = f"t-{self.command_count:06d}"
        command = ActionCommand(command_id=action_id, action=action, args=args or {},
                                expect_navigation=expect_navigation)
        envelope = ActionEnvelope(
            schema_version=SCHEMA_VERSIONS["ActionEnvelope"],
            session_id=self.channel.session_id,
            turn_id=turn_id,
            action_id=action_id,
            based_on_observation_id=observation.observation_id,
            page_epoch=observation.page_epoch,
            deadline=_now_ms() + int(self.timeout_s * 1000),
            command_digest=command.content_digest,
            command=command,
        )
        latest = self.channel.latest_observation_envelope
        if (latest is not None
                and (latest.page_epoch != observation.page_epoch
                     or latest.observation_id != observation.observation_id)):
            return self._fresh_fact_receipt(
                action_id=action_id, action=action, args=args or {},
                before=observation, fresh=latest)
        try:
            self.channel.enqueue_action(envelope)
            result = self.channel.wait_for_result(action_id, timeout_s=self.timeout_s)
        except (TimeoutError, RemoteProtocolError) as exc:
            if isinstance(exc, RemoteProtocolError) and exc.code == "stale_observation":
                fresh = self.channel.latest_observation_envelope
                if fresh is not None:
                    return self._fresh_fact_receipt(
                        action_id=action_id, action=action, args=args or {},
                        before=observation, fresh=fresh)
            raise TransportError(str(exc)) from exc
        if result.observation_after is not None:
            self._deliver(result.observation_after)
        if result.status == "indeterminate":
            raise TransportError(result.error_detail or "remote action result is indeterminate")
        if result.status == "completed":
            assert result.receipt is not None
            return result.receipt
        assert result.observation_after is not None
        fresh = result.observation_after.observation
        return ActionReceipt(
            schema_version=SCHEMA_VERSIONS["ActionReceipt"],
            command_id=action_id,
            action=action,
            args_echo=_remote_args_echo(args or {}),
            ok=False,
            error_code=(result.error_code
                        if result.error_code in RECEIPT_ERRORS else "page_error"),
            error_detail=result.error_detail,
            url_before=observation.observation.url,
            url_after=fresh.url,
            navigated=observation.observation.url != fresh.url,
            epoch_before=observation.page_epoch,
            epoch_after=result.observation_after.page_epoch,
            observation=fresh,
        )

    def console(self) -> list[str]:
        return []

    def close(self) -> None:
        self.channel.close()
