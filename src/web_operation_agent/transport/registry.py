"""The multi-session registry: capability tokens in, live channels out.

The registry never stores a raw token -- only its SHA-256 digest, compared with
``hmac.compare_digest``. It is also the only place that retires channels wholesale.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import uuid
from collections.abc import Callable

from ..state.store import SQLiteSessionStore
from ..state.identity import PrincipalContext
from .channel import RemoteSessionChannel, RemoteSessionLimits, SessionGrant
from .panel_view import PanelSuggestion
from .protocol import RemoteProtocolError, RemoteSessionClosed, _now_ms, _origin


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
               principal: PrincipalContext | None = None,
               panel_suggestions: tuple[PanelSuggestion, ...] = ()
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
            principal_validator=self.principal_validator if principal is not None else None,
            panel_suggestions=panel_suggestions)
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

