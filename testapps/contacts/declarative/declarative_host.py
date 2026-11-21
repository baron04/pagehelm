"""Reusable gateway, durable SQLite session store, and actor revocation host."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import threading
import time
from typing import Any, Callable
import uuid

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.mutation import SQLiteMutationJournal
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.live import LiveConversationRunner, LiveProductServer, LiveProductSession
from web_operation_agent.modeling.client import KeyedLimitedChat, KeyedProviderAdmission, ProviderRuntimeGuard
from web_operation_agent.state import SQLitePrincipalRevocations, SQLiteSessionStore
from .declarative_writes import admit_armed_write


@dataclass(frozen=True, slots=True)
class DeclarativeHostConfig:
    """Validated runtime configuration for ``DeclarativeSessionHost``."""

    state_dir: Path
    port: int = 8765
    scripted: bool = False
    model_env: Path | None = None
    model: str | None = None
    allow_mutations: bool = False
    max_sessions: int = 10
    max_sessions_per_actor: int = 3
    browser_idle_timeout_s: int = 90
    public_agent_url: str | None = None
    session_lease_ms: int = 5_000
    max_active_requests: int = 3
    max_active_requests_per_actor: int = 2
    require_task_contract: bool = False
    model_loop: bool = False
    native_submit_writes: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_dir", Path(self.state_dir).expanduser())
        if self.model_env is not None:
            object.__setattr__(self, "model_env", Path(self.model_env).expanduser())
        if isinstance(self.port, bool) or not 0 <= self.port <= 65535:
            raise ValueError("port must be an integer from 0 to 65535")
        if isinstance(self.max_sessions, bool) or not 1 <= self.max_sessions <= 100:
            raise ValueError("max_sessions must be an integer from 1 to 100")
        if isinstance(self.session_lease_ms, bool) or not 1_000 <= self.session_lease_ms <= 60_000:
            raise ValueError("session_lease_ms must be an integer from 1000 to 60000")
        for name in ("max_active_requests", "max_active_requests_per_actor", "max_sessions_per_actor"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
                raise ValueError(f"{name} must be an integer from 1 to 100")
        if self.max_active_requests_per_actor > self.max_active_requests:
            raise ValueError("max_active_requests_per_actor cannot exceed max_active_requests")
        if self.max_sessions_per_actor > self.max_sessions:
            raise ValueError("max_sessions_per_actor cannot exceed max_sessions")
        if not isinstance(self.model_loop, bool):
            raise ValueError("model_loop must be a boolean")
        if not isinstance(self.require_task_contract, bool):
            raise ValueError("require_task_contract must be a boolean")
        if (
            isinstance(self.browser_idle_timeout_s, bool)
            or not isinstance(self.browser_idle_timeout_s, int)
            or not 30 <= self.browser_idle_timeout_s <= 86_400
        ):
            raise ValueError("browser_idle_timeout_s must be from 30 to 86400")


class DeclarativeSessionHost:
    """Reusable local gateway, durable session store, revocation, and write gate host."""

    def __init__(
        self,
        config: DeclarativeHostConfig,
        *,
        application: Any = None,
        chat_factory: Callable[[DeclarativeHostConfig, Path, ProviderRuntimeGuard], tuple[Any, Any]] | None = None,
    ):
        self.config = config
        self.application = application
        self._chat_factory = chat_factory
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session_store = SQLiteSessionStore(
            config.state_dir / "session-store.sqlite3",
            lease_ms=config.session_lease_ms,
        )
        self.revocations = SQLitePrincipalRevocations(self.session_store)
        self.mutation_journal = SQLiteMutationJournal(
            config.state_dir / "mutation-journal.sqlite3"
        )
        self.provider_guard = ProviderRuntimeGuard(
            max_concurrent_requests=config.max_active_requests
        )
        self.principal_admission = KeyedProviderAdmission(
            config.max_active_requests_per_actor
        )
        self.server = LiveProductServer(
            max_sessions=config.max_sessions,
            max_sessions_per_actor=config.max_sessions_per_actor,
            browser_idle_timeout_s=config.browser_idle_timeout_s,
            durable_store=self.session_store,
            principal_validator=self.revocations.validate,
            public_base_url=config.public_agent_url,
            provider_status=self.provider_guard.status,
        )
        self._sessions: dict[tuple[str, str], tuple[LiveProductSession, object]] = {}
        self._submit_ports: dict[str, object] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _make_turn_config(self, **kwargs):
        from web_operation_agent.execution.product import AgentTurnConfig

        return AgentTurnConfig(**kwargs)

    def _new_chat(self, run_dir: Path, principal_key: str):
        if self._chat_factory is None:
            raise RuntimeError("DeclarativeSessionHost requires chat_factory or subclass _new_chat()")
        chat, meter = self._chat_factory(self.config, run_dir, self.provider_guard)
        return KeyedLimitedChat(chat, self.principal_admission, principal_key), meter

    def _admit_write(self, session_key: str, method: str, path: str, query: str, body: bytes) -> dict:
        port = self._submit_ports.get(session_key)
        return admit_armed_write(getattr(port, "armed", None), method, path, query, body)

    def open_or_reuse_session(
        self,
        *,
        actor_id: str,
        runtime_instance_id: str,
        session_token: str,
        session_prefix: str,
        adapter_factory: Callable[[], Any],
        page_credential: str,
        delegation_prefix: str,
        supported_scope: str,
        goal_port_factory: Callable[[Any], Any] | None = None,
        profile_factory: Callable[[Any, Any], Any] | None = None,
        action_policy_factory: Callable[[Any], Any] | None = None,
        submit_port_factory: Callable[[Any], Any] | None = None,
        page_write_guard: str = "",
        submit_port_key: str = "",
    ) -> dict:
        key = (actor_id, runtime_instance_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("session host is closed")
            previous = self._sessions.get(key)
            if previous is not None:
                session = previous[0]
                try:
                    session.channel.validate_live()
                except Exception:
                    session.close("native_bootstrap_replaced")
                    self._sessions.pop(key, None)
                else:
                    return self._browser_bootstrap(session)

            adapter = adapter_factory()
            bound = bind_application(adapter)
            page = adapter.identity.page_identity(page_credential)
            principal = adapter.identity.bind(
                page,
                delegation_id=f"{delegation_prefix}-{uuid.uuid4().hex}",
            )
            run_dir = self.config.state_dir / "runs" / (
                time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            )
            run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            chat, _meter = self._new_chat(run_dir, principal.actor_authority_digest)

            def runner_factory(channel):
                from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
                from web_operation_agent.execution.product import AgentTurnExecutor

                executor_type = AgentTurnExecutor
                loop_options: dict[str, Any] = {}
                action_policy = None
                if self.config.model_loop:
                    if goal_port_factory is None or profile_factory is None:
                        raise ValueError("model_loop requires goal_port_factory and profile_factory")
                    executor_type = ModelDrivenTurnExecutor
                    goal_port = goal_port_factory(adapter)
                    if action_policy_factory is not None:
                        action_policy = action_policy_factory(adapter)
                    loop_options = {
                        "goal_port": goal_port,
                        "profile": profile_factory(adapter, goal_port),
                        "verify": False,
                    }
                executor = executor_type(
                    channel=channel,
                    config=self._make_turn_config(
                        supported_scope=supported_scope,
                        ledger_path=run_dir / "audit.jsonl",
                        step_budget=8,
                        wall_budget_s=90,
                        transport_timeout_s=30,
                        disabled_actions=frozenset(
                            {"submit", "back"}
                            if self.config.model_loop
                            else {"click", "fill", "select", "submit", "back"}
                        ),
                        scope_first_policy=True,
                        policy_block_limit=2,
                        plain_business_answer_min_page_actions=0,
                        max_user_answer_chars=8_000,
                        require_task_contract=self.config.require_task_contract,
                        require_identity=True,
                        allow_form_prefill=True,
                        click_policy=None if self.config.model_loop else bound.ports.forms,
                        action_policy=action_policy,
                        allow_business_mutations=self.config.allow_mutations,
                        deployment_profile="production",
                    ),
                    chat=chat,
                    application=bound,
                    control=ReadOnlyControl(),
                    run_id_factory=lambda _message: "native-" + uuid.uuid4().hex[:16],
                    **loop_options,
                )
                if self.config.model_loop and self.config.native_submit_writes and submit_port_factory is not None:
                    port = submit_port_factory(adapter)
                    executor.native_submit = port
                    channel.read_only_mode = False
                    self._submit_ports[submit_port_key or session_token] = port
                return LiveConversationRunner(channel, executor=executor)

            stable = hashlib.sha256(f"{session_token}\0{runtime_instance_id}".encode()).hexdigest()[:40]
            self.server.close_session(f"{session_prefix}-{stable}")
            session = self.server.create_session(
                allowed_origin=bound.descriptor.origin,
                page_controller_module=bound.descriptor.page_controller_module,
                runner_factory=runner_factory,
                session_id=f"{session_prefix}-{stable}",
                navigation_mode=bound.descriptor.navigation_mode,
                page_write_guard=page_write_guard if self.config.model_loop else "",
                sensitive_query_keys=bound.ports.routes.session_handoff_query_keys,
                principal=principal,
            ).start()
            session.channel.read_only_mode = not (
                self.config.allow_mutations or (self.config.model_loop and self.config.native_submit_writes)
            )
            self._sessions[key] = (session, principal)
            return self._browser_bootstrap(session)

    def _browser_bootstrap(self, session: LiveProductSession) -> dict:
        return {
            "schema_version": 1,
            "config": self.server.gateway.bootstrap_config(
                session.grant,
                page_controller_module=session.page_controller_module,
                navigation_mode=session.navigation_mode,
                sensitive_query_keys=session.sensitive_query_keys,
                page_write_guard=session.page_write_guard,
            ),
        }

    def _revoke_actor_id(self, actor_id: str) -> None:
        with self._lock:
            matches = [(k, v) for k, v in self._sessions.items() if k[0] == actor_id]
            for key, (session, principal) in matches:
                self.revocations.revoke(principal, reason="application_logout")
                session.close("principal_revoked")
                self._sessions.pop(key, None)

    def start(self) -> "DeclarativeSessionHost":
        self.server.start()
        if self.application is not None and hasattr(self.application, "start"):
            self.application.start()
        return self

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._sessions.clear()
        if self.application is not None and hasattr(self.application, "close"):
            self.application.close()
        self.server.close()

    def __enter__(self) -> "DeclarativeSessionHost":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()


__all__ = [
    "DeclarativeHostConfig",
    "DeclarativeSessionHost",
    "admit_armed_write",
]
