"""Lifecycle, capacity, lease, and durable-journal state for a remote channel."""
from __future__ import annotations

import sqlite3

from ..state.store import DurableStateError
from ..state.identity import PrincipalContext, PrincipalError, PrincipalRevokedError
from ..schemas import SCHEMA_VERSIONS, TaskEvent, canonical_json
from .protocol import (RemoteProtocolError, RemoteSessionClosed, _MAX_EVENT_BYTES,
                       _copy_json, _copy_wire)


class ChannelState:
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
        now = self._now_ms() if now_ms is None else now_ms
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
        now_ms = self._now_ms()
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
        return self._now_ms() >= self.expires_at

    def _remaining_lifetime_s(self) -> float:
        return max(0.0, (self.expires_at - self._now_ms()) / 1000)

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
        self._event_times[event.sequence] = self._now_ms()
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
            now_ms = self._now_ms()
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
