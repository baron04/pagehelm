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
import sqlite3
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .durable import SQLiteSessionStore, DurableStateError, principal_scope
from .browser import TransportError, TurnCancelled
from .identity import PrincipalContext, PrincipalError, PrincipalRevokedError
from .panel_view import (collapse_timeline, panel_contract, panel_limits,
                         progress_message, project_event, project_history_record)
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
    CancellationRequest,
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


_MAX_EVENT_BYTES = 256 * 1024
_MAX_MESSAGE_ID_LENGTH = 128
_MAX_ORIGIN_LENGTH = 2048
_MAX_PAGE_URL_LENGTH = 4096


def _now_ms() -> int:
    return int(time.time() * 1000)


def _origin(value: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_ORIGIN_LENGTH:
        raise RemoteProtocolError("expected a bounded absolute http(s) origin", status=400)
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
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
    application_id: str = ""
    principal_digest: str = ""
    expires_at: int = 0
    session_expires_at: int = 0


@dataclass(frozen=True, slots=True)
class RemoteSessionLimits:
    """Hard per-session bounds for the in-process transport state."""

    session_ttl_s: int = 3600
    max_events: int = 2048
    max_messages: int = 128
    max_queued_messages: int = 8
    max_observations: int = 2048
    max_actions: int = 512
    max_approvals: int = 128
    max_runtime_instances: int = 64

    def __post_init__(self) -> None:
        ranges = {
            "session_ttl_s": (1, 86_400),
            "max_events": (3, 100_000),
            "max_messages": (1, 10_000),
            "max_queued_messages": (1, 1_000),
            "max_observations": (1, 100_000),
            "max_actions": (1, 10_000),
            "max_approvals": (1, 10_000),
            "max_runtime_instances": (1, 10_000),
        }
        for name, (minimum, maximum) in ranges.items():
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not minimum <= value <= maximum):
                raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
        if self.max_queued_messages > self.max_messages:
            raise ValueError("max_queued_messages cannot exceed max_messages")


class RemoteSessionChannel:
    """Thread-safe journal and rendezvous point for one server-owned Agent session."""

    def __init__(self, session_id: str, allowed_origins: tuple[str, ...],
                 principal: PrincipalContext | None = None,
                 limits: RemoteSessionLimits | None = None,
                 durable_store: SQLiteSessionStore | None = None,
                 principal_validator: Callable[[PrincipalContext], None] | None = None):
        self.session_id = session_id
        self.allowed_origins = frozenset(allowed_origins)
        self.principal = principal
        if principal_validator is not None and not callable(principal_validator):
            raise TypeError("principal_validator must be callable or None")
        if principal_validator is not None and principal is None:
            raise ValueError("principal_validator requires a principal-bound session")
        self._principal_validator = principal_validator
        self.limits = limits or RemoteSessionLimits()
        if not isinstance(self.limits, RemoteSessionLimits):
            raise TypeError("limits must be RemoteSessionLimits or None")
        created_at = _now_ms()
        self._last_client_activity_ms = created_at
        limit_expiry = created_at + self.limits.session_ttl_s * 1000
        self.expires_at = (min(limit_expiry, principal.expires_at)
                           if principal is not None else limit_expiry)
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
        self._result_digests: dict[str, str] = {}
        self._action_claims: dict[str, str] = {}
        self._pending_action_id = ""
        self._events: list[TaskEvent] = []
        self._messages: deque[dict] = deque()
        self._message_digests: dict[str, str] = {}
        self._cancelled_message_ids: set[str] = set()
        self._completed_message_ids: set[str] = set()
        self._cancelled_action_ids: set[str] = set()
        self._active_message_id = ""
        self._approvals: dict[str, ApprovalRequest] = {}
        self._approval_decisions: dict[str, ApprovalDecision] = {}
        self._pending_approval_id = ""
        self._event_times: dict[int, int] = {}
        self._new_conversation_pending = False
        self._durable_store = durable_store
        if durable_store is not None and principal is None:
            raise ValueError("durable sessions require a freshly authenticated principal")
        self._owner = (durable_store.acquire(session_id, principal_scope(principal, allowed_origins))
                       if durable_store else None)
        self.recovered_history = (durable_store.history(self._owner) if durable_store else [])
        self._heartbeat_stop = threading.Event()
        self._closed = False
        self._close_reason = ""
        self._append_event("session_started", {
            "state": "waiting_for_runtime",
            "application_id": (principal.application_id if principal else ""),
            "principal_digest": (principal.binding_digest if principal else ""),
        })

        if self.recovered_history:
            self._append_event("progress", {
                "state": "session_recovered",
                "message": "会话已重新连接。此前未完成的任务没有重跑，请检查结果后提交新任务；写入仍需重新确认。"})
        if durable_store is not None:
            self._heartbeat = threading.Thread(target=self._renew_owner, daemon=True)
            self._heartbeat.start()

    def _renew_owner(self):
        while not self._heartbeat_stop.wait(self._durable_store.lease_ms / 3000):
            try:
                self.validate_live()
            except (RemoteSessionClosed, DurableStateError, OSError, sqlite3.Error):
                with self._condition:
                    self._closed = True
                    self._close_reason = "owner_fenced"
                    self._condition.notify_all()
                return

    @property
    def latest_event_id(self) -> str:
        with self._condition:
            return self._events[-1].event_id if self._events else ""

    @property
    def close_reason(self) -> str:
        """Return the bounded reason code for operator and host lifecycle decisions."""
        with self._condition:
            return self._close_reason

    @property
    def last_client_activity_ms(self) -> int:
        with self._condition:
            return self._last_client_activity_ms

    def touch_client_activity(self, *, now_ms: int | None = None) -> None:
        """Record an authenticated browser request, never an internal owner heartbeat."""
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("client activity time must be a non-negative integer")
        with self._condition:
            self._ensure_open()
            self._last_client_activity_ms = max(self._last_client_activity_ms, now)

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
            detail = (f": {self._close_reason}" if self._close_reason else "")
            raise RemoteSessionClosed(f"remote session is closed{detail}")
        if self._durable_store is not None:
            try:
                self._durable_store.renew(self._owner)
            except (DurableStateError, sqlite3.Error, OSError) as exc:
                self._closed = True
                self._heartbeat_stop.set()
                raise RemoteSessionClosed("durable session owner was fenced") from exc
        now_ms = _now_ms()
        if self.principal is not None and now_ms >= self.principal.expires_at:
            self._terminate("principal_expired")
            raise RemoteSessionClosed("session principal is no longer valid")
        if now_ms >= self.expires_at:
            self._terminate("session_expired")
            raise RemoteSessionClosed("remote session expired")
        if self.principal is not None:
            try:
                self.principal.require_live(now_ms=now_ms)
                if self._principal_validator is not None:
                    self._principal_validator(self.principal)
            except PrincipalRevokedError as exc:
                self._terminate("principal_revoked")
                raise RemoteSessionClosed("session principal has been revoked") from exc
            except PrincipalError as exc:
                self._terminate("principal_expired")
                raise RemoteSessionClosed("session principal is no longer valid") from exc
            except Exception as exc:
                self._terminate("principal_validation_failed")
                raise RemoteSessionClosed("session principal validation failed closed") from exc

    @property
    def expired(self) -> bool:
        return _now_ms() >= self.expires_at

    def _remaining_lifetime_s(self) -> float:
        return max(0.0, (self.expires_at - _now_ms()) / 1000)

    def _bounded_wait(self, timeout_s: float) -> float:
        return min(timeout_s, self._remaining_lifetime_s())

    def _new_event(self, kind: str, payload: dict, *, turn_id: str = "") -> TaskEvent:
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
        if len(canonical_json(event.to_json()).encode("utf-8")) > _MAX_EVENT_BYTES:
            raise RemoteProtocolError(
                "task event exceeds 256 KiB", code="payload_too_large", status=413)
        if self._durable_store is not None:
            # Store user-visible progress only. No action/approval capability survives restart.
            persisted = {"kind": kind, "turn_id": turn_id,
                         "payload": (_copy_json(payload) if kind == "progress"
                                     else {"state": "not_replayable"})}
            try:
                self._durable_store.append(self._owner, persisted)
            except (DurableStateError, sqlite3.Error, OSError) as exc:
                self._closed = True
                self._heartbeat_stop.set()
                self._condition.notify_all()
                raise RemoteSessionClosed("durable event commit failed") from exc
        self._events.append(event)
        self._event_times[event.sequence] = _now_ms()
        self._condition.notify_all()
        return _copy_wire(event)

    def _terminate(self, reason: str) -> None:
        if self._closed:
            return
        if (not isinstance(reason, str) or not reason or len(reason) > 100
                or any(not (char.isalnum() or char in "-_") for char in reason)):
            reason = "session_closed"
        self._close_reason = reason
        if len(self._events) < self.limits.max_events:
            self._new_event("terminal", {"reason": reason})
        self._closed = True
        self._heartbeat_stop.set()
        if self._durable_store is not None:
            self._durable_store.release(self._owner)
        self._condition.notify_all()

    def _capacity_exhausted(self, resource: str) -> None:
        self._terminate(f"{resource}_capacity_exhausted")
        raise RemoteSessionClosed(f"remote session {resource} capacity exhausted")

    def validate_live(self) -> None:
        with self._condition:
            self._ensure_open()

    def renew_principal(self, principal: PrincipalContext) -> None:
        """Replace a short-lived assertion after the trusted host reauthenticates it.

        Capabilities, leases and durable history stay attached to the same logical actor.
        Renewal is refused while a turn or approval is in flight because their authority is
        bound to the old principal digest.
        """
        if not isinstance(principal, PrincipalContext):
            raise TypeError("principal must be a PrincipalContext")
        with self._condition:
            self._ensure_open()
            previous = self.principal
            if previous is None:
                raise PrincipalError("an unbound session cannot acquire a principal")
            stable = ("application_id", "tenant_id", "workspace_id", "actor_id",
                      "delegation_id", "scopes")
            if any(getattr(principal, name) != getattr(previous, name) for name in stable):
                raise PrincipalError("renewed principal changed the bound authority")
            now_ms = _now_ms()
            principal.require_live(now_ms=now_ms)
            if principal.issued_at < previous.issued_at:
                raise PrincipalError("renewed principal was issued before the current principal")
            if principal.expires_at <= previous.expires_at:
                raise PrincipalError("renewed principal does not extend the current principal")
            blocker = ("turn" if self._active_message_id else
                       "action" if self._pending_action_id else
                       "approval" if self._pending_approval_id else "")
            if blocker:
                raise RemoteProtocolError(
                    f"principal renewal requires an idle session ({blocker} in flight)",
                    code=f"session_busy_{blocker}", status=409)
            if self._principal_validator is not None:
                self._principal_validator(principal)
            self.principal = principal
            self.expires_at = min(
                now_ms + self.limits.session_ttl_s * 1000, principal.expires_at)
            self._condition.notify_all()

    def _append_event(self, kind: str, payload: dict, *, turn_id: str = "") -> TaskEvent:
        with self._condition:
            if kind != "terminal" and len(self._events) >= self.limits.max_events - 1:
                self._capacity_exhausted("event")
            if len(self._events) >= self.limits.max_events:
                self._capacity_exhausted("event")
            return self._new_event(kind, payload, turn_id=turn_id)

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
            if self.principal is None:
                if hello.application_id or hello.principal_digest:
                    raise RemoteProtocolError(
                        "session has no principal binding",
                        code="principal_mismatch", status=403)
            elif (hello.application_id != self.principal.application_id
                  or hello.principal_digest != self.principal.binding_digest):
                raise RemoteProtocolError(
                    "runtime principal binding does not match the session",
                    code="principal_mismatch", status=403)
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
                          "capabilities", "page_origin", "application_id",
                          "principal_digest")
                if prior is not None and any(getattr(prior, name) != getattr(hello, name)
                                             for name in stable):
                    raise RemoteProtocolError("runtime instance changed its hello contract")
                self._hello = hello
                return {"accepted": True, "lease_id": existing_lease,
                        "server_event_id": self.latest_event_id,
                        "panel": self._panel_configuration()}

            if len(self._runtime_leases) >= self.limits.max_runtime_instances:
                self._capacity_exhausted("runtime_instance")
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
                    "server_event_id": self.latest_event_id,
                    "panel": self._panel_configuration()}

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
        if len(self._observation_digests) >= self.limits.max_observations:
            self._capacity_exhausted("observation")
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
            prior_digest = self._result_digests.get(result.action_id)
            if prior_digest is not None:
                if prior_digest != result.content_digest:
                    raise RemoteProtocolError("action_id was replayed with a conflicting result")
                return {"accepted": True, "duplicate": True}
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
            self._result_digests[result.action_id] = result.content_digest
            self._action_observations.pop(result.action_id, None)
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
            if action_id in self._result_digests:
                raise RemoteProtocolError(
                    "action already has a result", code="action_completed")
            if action.turn_id in self._cancelled_message_ids:
                raise RemoteProtocolError(
                    "the task was cancelled before action claim",
                    code="turn_cancelled",
                )
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
            if (not isinstance(message_id, str) or not message_id
                    or len(message_id) > _MAX_MESSAGE_ID_LENGTH
                    or any(not (char.isalnum() or char in "-_") for char in message_id)):
                raise RemoteProtocolError(
                    "message_id must contain 1-128 letters, digits, '-' or '_'",
                    status=400,
                )
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise RemoteProtocolError("message text must contain 1-2000 characters", status=400)
            if (not isinstance(message["page_url"], str)
                    or len(message["page_url"]) > _MAX_PAGE_URL_LENGTH):
                raise RemoteProtocolError("message page_url exceeds 4096 characters", status=400)
            if _origin(message["page_url"]) not in self.allowed_origins:
                raise RemoteProtocolError("message page URL is outside this session", status=403)
            _safe_payload(message)
            fingerprint = hashlib.sha256(repr(sorted(message.items())).encode()).hexdigest()
            prior = self._message_digests.get(message_id)
            if prior is not None:
                if prior != fingerprint:
                    raise RemoteProtocolError("message_id was reused with different content")
                return {"accepted": True, "duplicate": True}
            if len(self._message_digests) >= self.limits.max_messages:
                self._capacity_exhausted("message")
            if len(self._messages) >= self.limits.max_queued_messages:
                raise RemoteProtocolError(
                    "session message queue is full",
                    code="backpressure",
                    status=429,
                )
            if self._durable_store is not None and not self._durable_store.message(
                    self._owner, message_id, fingerprint):
                return {"accepted": True, "duplicate": True}
            self._message_digests[message_id] = fingerprint
            self._messages.append(_copy_json(message))
            # The panel replays its timeline from this event stream.  Without the goal here
            # a full page navigation would leave the user looking at progress lines that
            # belong to a task they can no longer see the text of.
            self._append_event("progress", {
                "state": "message_accepted", "message_id": message_id, "text": text,
            }, turn_id=message_id)
            self._condition.notify_all()
            return {"accepted": True, "duplicate": False}

    def accept_cancellation(self, request: CancellationRequest, *, lease_id: str) -> dict:
        with self._condition:
            self._ensure_open()
            self._require_active_lease(lease_id)
            request = _copy_wire(request)
            if request.session_id != self.session_id:
                raise RemoteProtocolError(
                    "cancellation belongs to another session", status=400)
            if request.message_id not in self._message_digests:
                raise RemoteProtocolError(
                    "cancellation names an unknown message",
                    code="unknown_message", status=404)
            if request.message_id in self._completed_message_ids:
                return {"accepted": False, "duplicate": False, "completed": True}
            duplicate = request.message_id in self._cancelled_message_ids
            if not duplicate:
                self._cancelled_message_ids.add(request.message_id)
                pending = self._actions.get(self._pending_action_id)
                if (pending is not None
                        and pending.turn_id == request.message_id
                        and pending.action_id not in self._action_claims):
                    self._cancelled_action_ids.add(pending.action_id)
                    self._pending_action_id = ""
                if self._active_message_id == request.message_id \
                        and self._pending_approval_id:
                    approval_id = self._pending_approval_id
                    self._approval_decisions[approval_id] = ApprovalDecision(
                        schema_version=SCHEMA_VERSIONS["ApprovalDecision"],
                        session_id=self.session_id,
                        approval_id=approval_id,
                        decision="denied",
                        principal_digest=self._approvals[approval_id].principal_digest,
                        turn_id=self._approvals[approval_id].turn_id,
                        operation_id=self._approvals[approval_id].operation_id,
                    )
                    self._pending_approval_id = ""
                queued = next((held for held in self._messages
                               if held["message_id"] == request.message_id), None)
                if queued is not None:
                    # Nothing has started, so nothing has to wind down: the message leaves
                    # the queue here and the turn ends now. Publishing only `cancelling`
                    # for a message no turn had picked up left the panel stopping forever,
                    # because the terminal event it was waiting for could only come from a
                    # turn that was never going to run.
                    self._messages.remove(queued)
                    self._completed_message_ids.add(request.message_id)
                    if self._durable_store is not None:
                        try:
                            self._durable_store.transition(
                                self._owner, request.message_id, "completed")
                        except (DurableStateError, sqlite3.Error, OSError):
                            self._closed = True
                            self._heartbeat_stop.set()
                    self._append_event("progress", {
                        "state": "cancelled",
                        "message": progress_message("cancelled"),
                        "message_id": request.message_id,
                    }, turn_id=request.message_id)
                else:
                    self._append_event("progress", {
                        "state": "cancelling",
                        "message_id": request.message_id,
                    }, turn_id=request.message_id)
                self._condition.notify_all()
            return {"accepted": True, "duplicate": duplicate}

    def request_new_conversation(self, *, lease_id: str) -> dict:
        """Record that the user started a new conversation.

        Only the model's reference history ends here.  The event log stays whole, which is
        why this appends a marker instead of deleting anything: the timeline above the
        divider is still the record of what this session did.
        """
        with self._condition:
            self._ensure_open()
            self._require_active_lease(lease_id)
            if self._active_message_id:
                raise RemoteProtocolError(
                    "a task is still running in this conversation",
                    code="turn_in_flight", status=409)
            self._new_conversation_pending = True
            self._append_event("progress", {"state": "new_conversation"})
            self._condition.notify_all()
            return {"accepted": True}

    def take_new_conversation(self) -> bool:
        """Consume a pending boundary; the host calls this before composing a turn."""
        with self._condition:
            pending = self._new_conversation_pending
            self._new_conversation_pending = False
            return pending

    def message_cancelled(self, message_id: str) -> bool:
        with self._condition:
            try:
                self._ensure_open()
            except RemoteSessionClosed:
                return True
            return message_id in self._cancelled_message_ids

    def complete_message(self, message_id: str) -> None:
        with self._condition:
            if message_id and self._durable_store is not None:
                try:
                    self._durable_store.transition(self._owner, message_id, "completed")
                except (DurableStateError, sqlite3.Error, OSError):
                    self._closed = True
                    self._heartbeat_stop.set()
            if message_id:
                self._completed_message_ids.add(message_id)
            if self._active_message_id == message_id:
                self._active_message_id = ""
            # A confirmation belongs to the turn that raised it. Once that turn is over
            # there is nothing left for an answer to reach, so the session stops offering
            # it rather than leaving a card the panel can only be refused for.
            pending = self._approvals.get(self._pending_approval_id)
            if pending is not None and message_id and pending.turn_id == message_id:
                self._pending_approval_id = ""
            self._condition.notify_all()

    def accept_approval(self, decision: ApprovalDecision, *, lease_id: str) -> dict:
        """Accept one idempotent response from the currently authenticated page session.

        The panel renders inside the host application page, so this cannot distinguish a
        decision a person made from one a script in that page made.  That limit is stated
        in SECURITY.md rather than papered over here with evidence that would not survive
        an attacker who shares the panel's realm.
        """
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
            if (decision.principal_digest != request.principal_digest
                    or decision.turn_id != request.turn_id
                    or decision.operation_id != request.operation_id):
                raise RemoteProtocolError(
                    "approval response does not match its operation context",
                    code="approval_context_mismatch",
                    status=403,
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
            }, turn_id=request.turn_id)
            self._condition.notify_all()
            return {"accepted": True, "duplicate": False}

    # -- panel projection ------------------------------------------------------------
    def _panel_configuration(self) -> dict:
        """Every bound and every user-visible string the panel renders by.

        The panel holds no copy of these.  F8b was two sets of constants drifting apart;
        one of them is now a file the server reads and the panel is handed.
        """
        contract = panel_contract()
        return {
            "protocol": contract["protocol"],
            "limits": panel_limits(),
            "labels": dict(contract["label_keys"]),
            "notices": dict(contract["notices"]),
            "panel_states": dict(contract["panel_states"]),
            "progress_states": _copy_json(contract["progress_states"]),
            "layout": dict(contract["layout"]),
            "approval_visibility": dict(contract["approval_visibility"]),
            "terminal_error_codes": list(contract["terminal_error_codes"]),
            "terminal_panel_state_by_code": dict(contract["terminal_panel_state_by_code"]),
            "session_expires_at": self.expires_at,
            "principal_expires_at": (self.principal.expires_at
                                     if self.principal is not None else 0),
        }

    def panel_timeline(self, *, lease_id: str, after: str = "", before: str = "",
                       limit: int = 80, wait_s: float = 0) -> dict:
        """Return the server's own projection of this session onto the panel's timeline.

        The panel keeps no history of its own: a full page navigation replays from here
        (F8a), and every bound it renders by comes from the server (F8b).  Paging is by the
        same event cursor the runtime already uses -- ``after`` follows the live edge,
        ``before`` walks backwards into history.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise RemoteProtocolError("timeline limit must be an integer from 1 to 500",
                                      status=400)
        if after and before:
            raise RemoteProtocolError("timeline takes after or before, not both", status=400)
        with self._condition:
            self._require_active_lease(lease_id)
            cursor = _event_sequence(after)
            if cursor > len(self._events):
                raise RemoteProtocolError("timeline cursor is ahead of this session")
            if after and len(self._events) <= cursor and wait_s > 0:
                self._condition.wait_for(
                    lambda: (self._closed or len(self._events) > cursor
                             or lease_id != self._active_lease_id),
                    timeout=self._bounded_wait(wait_s),
                )
                self._require_active_lease(lease_id)
            return self._panel_timeline(after=after, before=before, limit=limit)

    def _panel_timeline(self, *, after: str, before: str, limit: int) -> dict:
        """Project the event log for the panel; the caller holds ``self._condition``."""
        entries: list[dict] = []
        replayed: list[dict] = []
        for record in self.recovered_history:
            projected = project_history_record(record, index=len(replayed) + 1)
            if projected is not None:
                replayed.append(projected)
        live = [projected for projected in
                (project_event(event, at=self._event_times.get(event.sequence, 0))
                 for event in self._events) if projected is not None]
        whole = collapse_timeline(replayed + live)

        if after:
            floor = _event_sequence(after)
            entries = [entry for entry in whole if entry["sequence"] > floor]
            has_more = False
        elif before:
            ceiling = _event_sequence(before)
            older = [entry for entry in whole
                     if entry["sequence"] < ceiling or entry["sequence"] == 0]
            entries = older[-limit:]
            has_more = len(older) > limit
        else:
            entries = whole[-limit:]
            has_more = len(whole) > limit

        payload = {
            "schema_version": SCHEMA_VERSIONS["TaskEvent"],
            "protocol": panel_contract()["protocol"],
            "entries": entries,
            "cursor": self._events[-1].event_id if self._events else "",
            "oldest": entries[0]["id"] if entries else "",
            "has_more": has_more,
            "closed": self._closed,
            "close_reason": self._close_reason,
            "pending_approval": None,
        }
        if self._pending_approval_id:
            pending = self._approvals[self._pending_approval_id]
            # A card whose deadline has passed is not something to ask the person for. The
            # turn that raised it is on its way to ending; the panel is told what happened
            # by that ending, not by a control it cannot use.
            if pending.expires_at > _now_ms():
                payload["pending_approval"] = pending.to_json()
        return payload

    # -- server -> browser ----------------------------------------------------------
    def enqueue_action(self, action: ActionEnvelope) -> TaskEvent:
        with self._condition:
            self._ensure_open()
            action = _copy_wire(action)
            if action.session_id != self.session_id:
                raise RemoteProtocolError("action belongs to another session", status=400)
            if self.principal is not None and action.turn_id != self._active_message_id:
                raise RemoteProtocolError(
                    "action does not belong to the active principal-bound turn",
                    code="turn_mismatch", status=403)
            if action.turn_id in self._cancelled_message_ids:
                raise RemoteProtocolError(
                    "the task was cancelled before action dispatch",
                    code="turn_cancelled",
                )
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
            if len(self._actions) >= self.limits.max_actions:
                self._capacity_exhausted("action")
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
            event = self._append_event("action", action.to_json(), turn_id=action.turn_id)
            self._actions[action.action_id] = action
            self._action_observations[action.action_id] = _copy_wire(observation)
            self._pending_action_id = action.action_id
            return event

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
            if self.principal is None:
                if request.principal_digest or request.turn_id or request.operation_id:
                    raise RemoteProtocolError(
                        "session has no principal-bound approval context",
                        code="approval_context_mismatch", status=403)
            elif (request.principal_digest != self.principal.binding_digest
                  or request.turn_id != self._active_message_id
                  or not request.operation_id
                  or request.expires_at > self.principal.expires_at):
                raise RemoteProtocolError(
                    "approval context does not match the active principal and turn",
                    code="approval_context_mismatch", status=403)
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
            if len(self._approvals) >= self.limits.max_approvals:
                self._capacity_exhausted("approval")
            if self._pending_approval_id:
                raise RemoteProtocolError(
                    "another approval is already pending",
                    code="approval_in_flight",
                )
            event = self._append_event(
                "approval_required", request.to_json(), turn_id=request.turn_id)
            self._approvals[request.approval_id] = request
            self._pending_approval_id = request.approval_id
            self._condition.notify_all()
            return event

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
                    timeout=self._bounded_wait(wait_s),
                )
            self._require_active_lease(lease_id)
            if len(self._events) <= after:
                self._ensure_open()
            return [_copy_wire(event) for event in self._events[after:]]

    # -- synchronous server adapter waits ------------------------------------------
    def wait_for_client(self, timeout_s: float) -> ClientHello:
        with self._condition:
            ready = self._condition.wait_for(lambda: self._closed or self._hello is not None,
                                             timeout=self._bounded_wait(timeout_s))
            if self._hello is not None:
                self._ensure_open()
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
                timeout=self._bounded_wait(timeout_s),
            )
            if (self._latest_observation is not None
                    and self._latest_observation.page_epoch > newer_than):
                self._ensure_open()
                return _copy_wire(self._latest_observation)
            self._ensure_open()
            if not ready:
                raise TimeoutError("timed out waiting for a fresh page observation")
            raise TimeoutError("timed out waiting for a fresh page observation")

    def wait_for_result(self, action_id: str, *, timeout_s: float) -> ActionResultEnvelope:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: (self._closed or action_id in self._results
                         or action_id in self._cancelled_action_ids),
                timeout=self._bounded_wait(timeout_s),
            )
            if action_id in self._results:
                return _copy_wire(self._results.pop(action_id))
            if action_id in self._cancelled_action_ids:
                raise RemoteProtocolError(
                    "the task was cancelled before browser execution",
                    code="turn_cancelled",
                )
            self._ensure_open()
            if not ready:
                raise TimeoutError(f"timed out waiting for result of {action_id}")
            raise TimeoutError(f"timed out waiting for result of {action_id}")

    def wait_for_message(self, *, timeout_s: float | None = None) -> dict:
        """Wait for the next message this session should run.

        With no timeout this means *until the session ends*, which is longer than the
        lifetime the session had when the wait began: a renewal extends that lifetime while
        the caller is parked here. Returning `TimeoutError` at the old expiry ended the
        worker thread of a session that was still perfectly alive, and the session went on
        accepting messages nobody would ever run -- the person saw their question arrive
        and then nothing at all, for as long as they were willing to wait.
        """
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._condition:
            while True:
                remaining_lifetime = self._remaining_lifetime_s()
                effective_timeout = (
                    remaining_lifetime if deadline is None
                    else min(max(0.0, deadline - time.monotonic()), remaining_lifetime))
                self._condition.wait_for(
                    lambda: self._closed or (bool(self._messages)
                                             and not self._active_message_id),
                    timeout=effective_timeout,
                )
                if self._messages and not self._active_message_id:
                    self._ensure_open()
                    message = _copy_json(self._messages.popleft())
                    if self._durable_store is not None:
                        self._durable_store.transition(
                            self._owner, message["message_id"], "running")
                    self._active_message_id = message["message_id"]
                    return message
                # Raises if the session really is over; otherwise it outlived this wait.
                self._ensure_open()
                if deadline is not None and time.monotonic() >= deadline:
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
                timeout=min(self._bounded_wait(timeout_s), remaining),
            )
            decision = self._approval_decisions.get(approval_id)
            if decision is not None:
                return _copy_wire(decision)
            self._ensure_open()
            if request.expires_at <= _now_ms():
                # Nobody can answer it any more, so the session stops offering it. Leaving
                # it pending meant every later projection of the timeline handed the panel
                # a card that could only be refused -- it came back after a reload, and
                # answering it said the confirmation was no longer valid.
                if self._pending_approval_id == approval_id:
                    self._pending_approval_id = ""
                    self._condition.notify_all()
                raise RemoteProtocolError(
                    "approval request expired while awaiting a decision",
                    code="approval_expired",
                )
            if not ready:
                raise TimeoutError(f"timed out waiting for approval {approval_id}")
            raise TimeoutError(f"timed out waiting for approval {approval_id}")

    def close(self, reason: str = "session_closed") -> None:
        with self._condition:
            self._terminate(reason)


class RemoteSessionRegistry:
    """Own capability-token hashes and session channels; raw tokens are never retained."""

    def __init__(self, *, limits: RemoteSessionLimits | None = None,
                 durable_store: SQLiteSessionStore | None = None,
                 principal_validator: Callable[[PrincipalContext], None] | None = None):
        self.durable_store = durable_store
        if principal_validator is not None and not callable(principal_validator):
            raise TypeError("principal_validator must be callable or None")
        self.principal_validator = principal_validator
        self.limits = limits or RemoteSessionLimits()
        if not isinstance(self.limits, RemoteSessionLimits):
            raise TypeError("limits must be RemoteSessionLimits or None")
        self._lock = threading.RLock()
        self._channels: dict[str, RemoteSessionChannel] = {}
        self._token_hashes: dict[str, bytes] = {}

    def create(self, *, allowed_origins: list[str] | tuple[str, ...],
               session_id: str | None = None,
               principal: PrincipalContext | None = None
               ) -> tuple[SessionGrant, RemoteSessionChannel]:
        sid = session_id or f"s-{uuid.uuid4().hex}"
        if not sid or len(sid) > 128 or any(not (c.isalnum() or c in "-_") for c in sid):
            raise ValueError("session_id must contain only letters, digits, '-' and '_'")
        origins = tuple(dict.fromkeys(_origin(value) for value in allowed_origins))
        if not origins:
            raise ValueError("at least one allowed origin is required")
        if principal is not None:
            if not isinstance(principal, PrincipalContext):
                raise TypeError("principal must be a PrincipalContext or None")
            principal.require_live(now_ms=_now_ms())
        token = secrets.token_urlsafe(32)
        self.reap_expired()
        channel = RemoteSessionChannel(
            sid, origins, principal, self.limits,
            durable_store=self.durable_store,
            principal_validator=self.principal_validator if principal is not None else None)
        with self._lock:
            if sid in self._channels:
                raise ValueError(f"session {sid!r} already exists")
            self._channels[sid] = channel
            self._token_hashes[sid] = hashlib.sha256(token.encode()).digest()
        return SessionGrant(
            sid, token, origins,
            application_id=(principal.application_id if principal else ""),
            principal_digest=(principal.binding_digest if principal else ""),
            expires_at=(principal.expires_at if principal else 0),
            session_expires_at=channel.expires_at,
        ), channel

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
        try:
            channel.validate_live()
        except RemoteSessionClosed:
            self.retire(session_id, "session_expired")
            raise
        return channel

    def origin_allowed(self, session_id: str, origin: str) -> bool:
        with self._lock:
            channel = self._channels.get(session_id)
        if channel is not None and channel.expired:
            self.retire(session_id, "session_expired")
            return False
        return channel is not None and origin in channel.allowed_origins

    @property
    def session_count(self) -> int:
        self.reap_expired()
        with self._lock:
            return len(self._channels)

    def reap_expired(self) -> int:
        with self._lock:
            session_ids = tuple(
                session_id for session_id, channel in self._channels.items()
                if channel.expired)
        return sum(self.retire(session_id, "session_expired")
                   for session_id in session_ids)

    def retire(self, session_id: str, reason: str = "session_retired") -> bool:
        """Revoke one capability and release its in-memory channel state."""
        with self._lock:
            channel = self._channels.pop(session_id, None)
            self._token_hashes.pop(session_id, None)
        if channel is None:
            return False
        channel.close(reason)
        return True

    def retire_all(self, reason: str = "registry_closed") -> int:
        with self._lock:
            session_ids = tuple(self._channels)
        return sum(self.retire(session_id, reason) for session_id in session_ids)


class RemoteBrowserClient:
    """Synchronous browser interface consumed unchanged by ``ServerAgentSession``."""

    def __init__(self, channel: RemoteSessionChannel, *, timeout_s: float = 90,
                 turn_id: str = ""):
        self.channel = channel
        self.timeout_s = timeout_s
        self.turn_id = turn_id
        self.command_count = 0
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
        turn_id = self.turn_id or f"t-{self.command_count:06d}"
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
            if isinstance(exc, RemoteProtocolError) and exc.code == "turn_cancelled":
                raise TurnCancelled(str(exc)) from exc
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
