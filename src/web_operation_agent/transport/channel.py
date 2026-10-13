"""The session grant, its bounds, and the one journal-and-rendezvous channel per session.

``RemoteSessionChannel`` is the thread-safe meeting point: the browser side accepts
observations, results, messages and approvals into it; the server Agent publishes exactly
one action at a time and waits on it. Lifetimes, leases, capability tokens and the
append-only event journal are this module's alone.
"""
from __future__ import annotations

import secrets
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..state.store import SQLiteSessionStore, principal_scope
from ..state.identity import PrincipalContext
from ..schemas import (
    ActionEnvelope,
    ActionResultEnvelope,
    ApprovalDecision,
    ApprovalRequest,
    ClientHello,
    ObservationEnvelope,
    TaskEvent,
)

from .channel_ingress import ChannelIngress
from .channel_panel import ChannelPanel
from .channel_server import ChannelServer
from .channel_state import ChannelState
from .protocol import _now_ms


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


class RemoteSessionChannel(ChannelIngress, ChannelPanel, ChannelServer, ChannelState):
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
        # Every page the session is open on holds a lease of its own. The active lease is the
        # page whose message the current (or last) turn runs: that page is observed and acts
        # for it, and the others follow the conversation and may send the next message.
        self._active_lease_id = ""
        self._runtime_leases: dict[str, str] = {}
        self._retired_runtime_instances: set[str] = set()
        self._lease_clients: dict[str, str] = {}
        self._lease_hellos: dict[str, ClientHello] = {}
        self._lease_observations: dict[str, ObservationEnvelope] = {}
        self._lease_seen: dict[str, int] = {}
        self._message_leases: dict[str, str] = {}
        #: The page each action or card was for, by event sequence: the turn's page when it
        #: was published.
        self._event_leases: dict[int, str] = {}
        self._latest_observation: ObservationEnvelope | None = None
        self._observation_digests: dict[str, str] = {}
        self._actions: dict[str, ActionEnvelope] = {}
        self._action_observations: dict[str, ObservationEnvelope] = {}
        # What each action's control is called on its page, for the panel's step line; kept
        # after the result, since the timeline is re-projected on every read.
        self._action_targets: dict[str, dict] = {}
        self._results: dict[str, ActionResultEnvelope] = {}
        self._result_digests: dict[str, str] = {}
        self._action_claims: dict[str, str] = {}
        self._pending_action_id = ""
        self._events: list[TaskEvent] = []
        self._messages: deque[dict] = deque()
        self.turn_events = None
        self._message_digests: dict[str, str] = {}
        self._cancelled_message_ids: set[str] = set()
        self._completed_message_ids: set[str] = set()
        self._cancelled_action_ids: set[str] = set()
        self._active_message_id = ""
        self._approvals: dict[str, ApprovalRequest] = {}
        self._approval_decisions: dict[str, ApprovalDecision] = {}
        self._pending_approval_id = ""
        self._event_times: dict[int, int] = {}
        self._event_durable_sequences: dict[int, int] = {}
        self._panel_entries: list[dict] = []
        self._panel_sequences: list[int] = []
        self._panel_projected = 0
        self._panel_observed_turns: set[str] = set()
        self._new_conversation_pending = False
        self._durable_store = durable_store
        if durable_store is not None and principal is None:
            raise ValueError("durable sessions require a freshly authenticated principal")
        self._owner = (durable_store.acquire(session_id, principal_scope(principal, allowed_origins))
                       if durable_store else None)
        self.recovered_history = (durable_store.history(self._owner) if durable_store else [])
        self._history_upper = (self.recovered_history[-1]["sequence"]
                               if self.recovered_history else 0)
        # Event ids and sequences count from one in every channel. What a panel was shown
        # under another channel -- before a restart, or by a session that has since ended --
        # cannot be told from this one's by id, so each timeline page says whose it is.
        self._incarnation = secrets.token_hex(8)
        self._heartbeat_stop = threading.Event()
        self._closed = False
        self._close_reason = ""
        self._append_event("session_started", {
            "state": "waiting_for_runtime",
            "application_id": (principal.application_id if principal else ""),
            "principal_digest": (principal.binding_digest if principal else ""),
        })

        # A session that ended quietly comes back as it was. Only one that was cut off in
        # the middle of a task says so: that task did not finish and is not rerun.
        if self.recovered_history and self._owner.interrupted:
            self._append_event("progress", {
                "state": "session_recovered",
                "message": "会话已重新连接。此前未完成的任务没有重跑，请检查结果后提交新任务；写入仍需重新确认。"})
        if durable_store is not None:
            self._heartbeat = threading.Thread(target=self._renew_owner, daemon=True)
            self._heartbeat.start()

    def _now_ms(self) -> int:
        """Read the facade clock so existing clock injection remains one boundary."""
        return _now_ms()
