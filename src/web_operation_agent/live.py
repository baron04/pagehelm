"""Product composition for first-party embedded Agent sessions.

Application adapters supply a runner and decide how their page includes the returned
bootstrap script. This module owns the common registry, capability grant, shared loopback
gateway, server worker lifecycle and optional replay projection. It has no knowledge of OFBiz, lab
tasks, hidden evaluator data or browser automation.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol, cast

from .durable import SQLiteSessionStore
from .transport.gateway import EmbeddedGateway
from .identity import PrincipalContext
from .privacy import redact_sensitive_assignments, redact_session_handoffs
from .execution.product import AgentTurnError
from .transport.channel import RemoteSessionChannel, RemoteSessionLimits, SessionGrant
from .transport.protocol import RemoteSessionClosed
from .transport.registry import RemoteSessionRegistry
from .traceview import build_trace_case, render_trace_html, write_trace_bundle

#: How long an idle event request is held open before it returns "nothing yet".
#:
#: It was 0.2s, which is not a long poll: it is polling. One panel sitting still put ten
#: requests a second through the gateway -- events and timeline both -- and every one of
#: them is a connection the browser sets up, the server answers with a keepalive comment,
#: and both sides tear down. A held request costs a parked thread and returns the instant
#: an event exists, so waiting longer makes the session quieter without making it slower.
_LONG_POLL_S = 20.0


class ProductRunner(Protocol):
    """Compatibility contract for a complete product-session worker."""

    def run_once(self): ...


class AgentTurnExecutor(Protocol):
    """Execute one already-dequeued wire message without owning the session loop."""

    def execute(self, message: dict, /) -> object: ...


class _LegacyTurnRunner(Protocol):
    def run_once(self, *, message: dict) -> object: ...


LegacyTurnRunnerFactory = Callable[[dict, int], _LegacyTurnRunner]


@dataclass(frozen=True)
class LiveTurnOutcome:
    turn: int
    result: object | None
    task: str
    error: str = ""


class LiveConversationRunner:
    """Host sequential panel turns and isolate one turn's failure from the next.

    New product integrations supply one ``AgentTurnExecutor`` instance for the whole
    conversation.  The optional factory path is a compatibility shim for older demos
    that still construct a runner per turn.
    """

    def __init__(self, channel: RemoteSessionChannel,
                 executor: AgentTurnExecutor | LegacyTurnRunnerFactory | None = None,
                 *, runner_factory: LegacyTurnRunnerFactory | None = None):
        if executor is not None and runner_factory is not None:
            raise TypeError("supply executor or runner_factory, not both")
        selected = executor if executor is not None else runner_factory
        execute = getattr(selected, "execute", None)
        if callable(execute):
            self.executor: AgentTurnExecutor | None = cast(AgentTurnExecutor, selected)
            self._legacy_runner_factory: LegacyTurnRunnerFactory | None = None
        elif callable(selected):
            # Preserve the former positional ``runner_factory`` API while keeping the
            # turn counter and message dequeue in this host.
            self.executor = None
            self._legacy_runner_factory = cast(LegacyTurnRunnerFactory, selected)
        else:
            raise TypeError("an AgentTurnExecutor is required")
        self.channel = channel
        self._outcomes: queue.Queue[LiveTurnOutcome] = queue.Queue()
        self.turns_completed = 0

    def _execute_turn(self, message: dict, turn: int) -> object:
        if self.executor is not None:
            return self.executor.execute(message)
        assert self._legacy_runner_factory is not None
        runner = self._legacy_runner_factory(message, turn)
        return runner.run_once(message=message)

    def _record(self, outcome: LiveTurnOutcome) -> None:
        self.turns_completed = outcome.turn
        self._outcomes.put(outcome)

    def run(self) -> tuple[LiveTurnOutcome, ...]:
        """Run until the remote session closes; never retry an individual turn."""
        outcomes: list[LiveTurnOutcome] = []
        while True:
            try:
                message = self.channel.wait_for_message(timeout_s=None)
            except RemoteSessionClosed:
                return tuple(outcomes)
            except TimeoutError:
                # Waiting without a deadline ends only when the session does. Letting this
                # escape ended the worker while the session stayed open, and every later
                # message was accepted by a session with nobody left to run it.
                continue
            turn = self.turns_completed + 1
            message_id = str(message.get("message_id") or "")
            try:
                try:
                    result = self._execute_turn(message, turn)
                    outcome = LiveTurnOutcome(
                        turn=turn, result=result, task=str(message.get("text") or ""))
                except RemoteSessionClosed:
                    # Closing the panel/session is normal lifecycle completion, including
                    # when it interrupts an in-flight executor.
                    return tuple(outcomes)
                except Exception as exc:
                    detail = redact_sensitive_assignments(redact_session_handoffs(
                        f"{type(exc).__name__}: {exc}"))[:800]
                    outcome = LiveTurnOutcome(
                        turn=turn, result=None, task=str(message.get("text") or ""),
                        error=detail)
                    outcomes.append(outcome)
                    self._record(outcome)
                    if isinstance(exc, AgentTurnError):
                        error_code = exc.code.value
                        retryable = exc.retryable
                        user_message = {
                            "observation_unavailable": "页面状态暂不可用，请稍后重新提交。",
                            "stale_message": "页面已发生变化，请检查当前页面后重新提交。",
                            "runtime_unavailable": "页面连接暂不可用，请稍后重试。",
                            "invalid_message": "请求格式无效，请重新输入。",
                        }.get(error_code, "本次任务执行失败，请查看运行记录。")
                    else:
                        error_code = "unexpected_error"
                        retryable = False
                        user_message = "本次任务执行失败，请查看运行记录。"
                    try:
                        self.channel.publish("progress", {
                            "state": "task_failed",
                            "message": user_message,
                            "error_code": error_code,
                            "retryable": retryable,
                        }, turn_id=message_id)
                    except RemoteSessionClosed:
                        # The outcome is already durable to this host.  The next dequeue
                        # observes closure after any messages already accepted by the channel.
                        pass
                    continue
                outcomes.append(outcome)
                self._record(outcome)
            finally:
                self.channel.complete_message(message_id)

    def run_once(self) -> tuple[LiveTurnOutcome, ...]:
        """Compatibility alias for callers that treated the session loop as one run."""
        return self.run()

    def close(self, reason: str = "host_stopped") -> None:
        """Close the channel owned by this conversation host."""
        self.channel.close(reason)

    def next_outcome(self, timeout_s: float | None = None) -> LiveTurnOutcome:
        try:
            return self._outcomes.get(timeout=timeout_s)
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for a completed conversation turn") from exc

    def outcomes_nowait(self) -> tuple[LiveTurnOutcome, ...]:
        found: list[LiveTurnOutcome] = []
        while True:
            try:
                found.append(self._outcomes.get_nowait())
            except queue.Empty:
                return tuple(found)


@dataclass(frozen=True)
class LiveSessionStatus:
    session_id: str
    gateway_url: str
    running: bool
    finished: bool
    failed: bool


class LiveProductSession:
    """Own one browser capability and one server-side product runner lifecycle."""

    def __init__(self, *, allowed_origin: str, page_controller_module: str,
                 runner_factory: Callable[[RemoteSessionChannel], ProductRunner],
                 session_id: str | None = None, sse_wait_s: float = _LONG_POLL_S,
                 navigation_mode: str = "document",
                 sensitive_query_keys: frozenset[str] = frozenset(),
                 principal: PrincipalContext | None = None,
                 session_limits: RemoteSessionLimits | None = None,
                 durable_store: SQLiteSessionStore | None = None,
                 registry: RemoteSessionRegistry | None = None,
                 gateway: EmbeddedGateway | None = None,
                 provider_status: Callable[[], dict] | None = None,
                 on_close: Callable[[str], None] | None = None):
        if (registry is None) != (gateway is None):
            raise ValueError("registry and gateway must be supplied together")
        if registry is not None and durable_store is not None:
            raise ValueError("durable_store belongs to the shared registry")
        if registry is not None and session_limits is not None:
            raise ValueError("session_limits belong to the shared registry")
        if provider_status is not None and not callable(provider_status):
            raise TypeError("provider_status must be callable or None")
        if gateway is not None and provider_status is not None:
            raise ValueError("provider_status belongs to the shared server when gateway is shared")
        self._owns_gateway = registry is None
        self.registry = registry or RemoteSessionRegistry(limits=session_limits, durable_store=durable_store)
        self._provider_status = provider_status
        self._thread: threading.Thread | None = None
        self._result = None
        self._error: BaseException | None = None
        self._closed = False
        self._close_lock = threading.Lock()
        self.gateway = gateway or EmbeddedGateway(
            self.registry, sse_wait_s=sse_wait_s,
            readiness_provider=self.readiness_snapshot)
        if self.gateway.registry is not self.registry:
            raise ValueError("shared gateway must use the supplied session registry")
        self.grant, self.channel = self.registry.create(
            allowed_origins=[allowed_origin],
            session_id=session_id or f"live-{uuid.uuid4().hex}",
            principal=principal,
        )
        self.page_controller_module = page_controller_module
        if navigation_mode not in {"document", "spa"}:
            self.registry.retire(self.grant.session_id, "invalid_session_configuration")
            if self._owns_gateway:
                self.gateway.close()
            raise ValueError("navigation_mode must be document or spa")
        self.navigation_mode = navigation_mode
        self.sensitive_query_keys = sensitive_query_keys
        try:
            self.runner = runner_factory(self.channel)
        except BaseException:
            self.registry.retire(self.grant.session_id, "runner_setup_failed")
            if self._owns_gateway:
                self.gateway.close()
            raise
        self._on_close = on_close

    @property
    def bootstrap_script(self) -> str:
        if self._thread is None:
            raise RuntimeError("live session must be started before requesting bootstrap")
        self.channel.validate_live()
        return self.gateway.bootstrap_script(
            self.grant, page_controller_module=self.page_controller_module,
            navigation_mode=self.navigation_mode,
            sensitive_query_keys=self.sensitive_query_keys)

    def start(self) -> "LiveProductSession":
        if self._thread is not None:
            return self
        self.channel.validate_live()
        self.gateway.start()

        def target() -> None:
            try:
                self._result = self.runner.run_once()
            except BaseException as exc:
                self._error = exc
            finally:
                # The worker is what answers messages. Once it is gone, a session that
                # stays open is a session that accepts questions it can never answer, and
                # the person at the panel waits on a turn that will never start. Closing
                # here turns that silence into an ending they can see and act on.
                # Only a worker that *failed* ends the session here. A worker that returns
                # normally has either finished the single run it was asked for -- the eval
                # apparatus does that -- or returned because the session was already
                # closed, and neither is a session that should be taken away.
                if self._error is not None and not self._closed:
                    self.close("worker_failed")

        self._thread = threading.Thread(
            target=target, name=f"{self.grant.session_id}-server-agent", daemon=True)
        self._thread.start()
        return self

    def wait(self, timeout_s: float | None = None):
        if self._thread is None:
            raise RuntimeError("live session has not been started")
        self._thread.join(timeout=timeout_s)
        if self._thread.is_alive():
            raise TimeoutError("live product session did not finish before its outer deadline")
        if self._error is not None:
            raise RuntimeError(
                f"live product worker failed: {type(self._error).__name__}: {self._error}") \
                from self._error
        return self._result

    def status(self) -> LiveSessionStatus:
        thread = self._thread
        return LiveSessionStatus(
            session_id=self.grant.session_id,
            gateway_url=self.gateway.base_url,
            running=bool(thread and thread.is_alive()),
            finished=bool(thread and not thread.is_alive() and self._error is None),
            failed=self._error is not None,
        )

    def renew_principal(self, principal: PrincipalContext) -> SessionGrant:
        """Install a host-reauthenticated principal without replacing the browser session."""
        self.channel.renew_principal(principal)
        self.grant = replace(
            self.grant,
            application_id=principal.application_id,
            principal_digest=principal.binding_digest,
            expires_at=principal.expires_at,
            session_expires_at=self.channel.expires_at,
        )
        return self.grant

    def readiness_snapshot(self) -> dict:
        """Return content-free liveness dependencies for the loopback admin endpoint."""
        thread = self._thread
        try:
            self.channel.validate_live()
            channel_ready = True
        except RemoteSessionClosed:
            channel_ready = False
        ready = (not self._closed and channel_ready and thread is not None
                 and thread.is_alive() and self._error is None)
        snapshot = {
            "ready": ready,
            "accepting_sessions": False,
            "session_count": 1 if channel_ready else 0,
            "session_capacity": 1,
        }
        if self._provider_status is not None:
            provider = self._provider_status()
            snapshot["provider"] = provider
            if isinstance(provider, dict) and provider.get("circuit_open") is True:
                snapshot["ready"] = False
        return snapshot

    def close(self, reason: str = "host_stopped") -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.registry.retire(self.grant.session_id, reason)
        if self._owns_gateway:
            self.gateway.close()
        if self._on_close is not None:
            self._on_close(self.grant.session_id)

    def __enter__(self) -> "LiveProductSession":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()


class LiveProductCapacityError(RuntimeError):
    """A new browser session was refused before any capability was issued."""

    code = "session_capacity"


class LiveProductServer:
    """Bounded shared gateway and registry for multiple local product sessions.

    This removes the one-listener-per-session topology while keeping the existing channel
    protocol. Optional SQLite storage preserves safe history and fences local owners.
    """

    def __init__(self, *, max_sessions: int = 100,
                 max_sessions_per_actor: int | None = None,
                 browser_idle_timeout_s: int = 300,
                 sse_wait_s: float = _LONG_POLL_S,
                 host: str = "127.0.0.1", port: int = 0,
                 public_base_url: str | None = None,
                 allow_private_network_bind: bool = False,
                 session_limits: RemoteSessionLimits | None = None,
                 durable_store: SQLiteSessionStore | None = None,
                 principal_validator: Callable[[PrincipalContext], None] | None = None,
                 provider_status: Callable[[], dict] | None = None):
        if (isinstance(max_sessions, bool) or not isinstance(max_sessions, int)
                or not 1 <= max_sessions <= 10_000):
            raise ValueError("max_sessions must be an integer from 1 to 10000")
        self.max_sessions = max_sessions
        per_actor = max_sessions if max_sessions_per_actor is None else max_sessions_per_actor
        if (isinstance(per_actor, bool) or not isinstance(per_actor, int)
                or not 1 <= per_actor <= max_sessions):
            raise ValueError("max_sessions_per_actor must be from 1 to max_sessions")
        if (isinstance(browser_idle_timeout_s, bool)
                or not isinstance(browser_idle_timeout_s, int)
                or not 30 <= browser_idle_timeout_s <= 86_400):
            raise ValueError("browser_idle_timeout_s must be from 30 to 86400")
        self.max_sessions_per_actor = per_actor
        self.browser_idle_timeout_ms = browser_idle_timeout_s * 1000
        if provider_status is not None and not callable(provider_status):
            raise TypeError("provider_status must be callable or None")
        self._provider_status = provider_status
        self.registry = RemoteSessionRegistry(
            limits=session_limits, durable_store=durable_store,
            principal_validator=principal_validator)
        self._sessions: dict[str, LiveProductSession] = {}
        self._lock = threading.RLock()
        self._closed = False
        self.gateway = EmbeddedGateway(
            self.registry, host=host, port=port, sse_wait_s=sse_wait_s,
            public_base_url=public_base_url,
            allow_private_network_bind=allow_private_network_bind,
            readiness_provider=self.readiness_snapshot)

    @property
    def base_url(self) -> str:
        return self.gateway.base_url

    @property
    def session_count(self) -> int:
        self._reap_expired()
        with self._lock:
            return len(self._sessions)

    def _reap_expired(self) -> int:
        now_ms = int(time.time() * 1000)
        with self._lock:
            expired = tuple(session for session in self._sessions.values()
                            if (session.channel.expired
                                or now_ms - session.channel.last_client_activity_ms
                                >= self.browser_idle_timeout_ms))
        for session in expired:
            reason = ("session_expired" if session.channel.expired
                      else "browser_inactive")
            session.close(reason)
        return len(expired)

    def readiness_snapshot(self) -> dict:
        """Return bounded service state without session identifiers or application content."""
        self._reap_expired()
        with self._lock:
            closed = self._closed
            session_count = len(self._sessions)
        snapshot = {
            "ready": not closed,
            "accepting_sessions": not closed and session_count < self.max_sessions,
            "session_count": session_count,
            "session_capacity": self.max_sessions,
        }
        if self._provider_status is not None:
            provider = self._provider_status()
            snapshot["provider"] = provider
            if isinstance(provider, dict) and provider.get("circuit_open") is True:
                snapshot["ready"] = False
                snapshot["accepting_sessions"] = False
        return snapshot

    def start(self) -> "LiveProductServer":
        with self._lock:
            if self._closed:
                raise RuntimeError("live product server is closed")
        self.gateway.start()
        return self

    def _session_closed(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def create_session(self, *, allowed_origin: str, page_controller_module: str,
                       runner_factory: Callable[[RemoteSessionChannel], ProductRunner],
                       session_id: str | None = None,
                       navigation_mode: str = "document",
                       sensitive_query_keys: frozenset[str] = frozenset(),
                       principal: PrincipalContext | None = None) -> LiveProductSession:
        with self._lock:
            if self._closed:
                raise RuntimeError("live product server is closed")
            self._reap_expired()
            if len(self._sessions) >= self.max_sessions:
                raise LiveProductCapacityError(
                    "live product server session capacity is exhausted")
            if principal is not None:
                actor_sessions = sum(
                    existing.channel.principal is not None
                    and existing.channel.principal.actor_authority_digest
                    == principal.actor_authority_digest
                    for existing in self._sessions.values())
                if actor_sessions >= self.max_sessions_per_actor:
                    raise LiveProductCapacityError(
                        "live product server actor session capacity is exhausted")
            session = LiveProductSession(
                allowed_origin=allowed_origin,
                page_controller_module=page_controller_module,
                runner_factory=runner_factory,
                session_id=session_id,
                navigation_mode=navigation_mode,
                sensitive_query_keys=sensitive_query_keys,
                principal=principal,
                registry=self.registry,
                gateway=self.gateway,
                on_close=self._session_closed,
            )
            self._sessions[session.grant.session_id] = session
            self.gateway.start()
            return session

    def close_session(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return False
        session.close()
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = tuple(self._sessions.values())
        for session in sessions:
            session.close()
        self.registry.retire_all("server_stopped")
        self.gateway.close()

    def __enter__(self) -> "LiveProductServer":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()


def render_live_replay(*, result_path: str | Path, ledger_path: str | Path,
                       output_path: str | Path, label: str, mode: str,
                       case_id: str = "live") -> dict:
    """Project one completed live run into the existing privacy-safe replay."""
    case = build_trace_case(
        case_id=case_id, label=label, mode=mode,
        result_path=Path(result_path), ledger_path=Path(ledger_path))
    output = Path(output_path)
    bundle_path = output.with_name("trace-data.json")
    bundle = write_trace_bundle([case], bundle_path)
    render_trace_html(bundle, output)
    return bundle
