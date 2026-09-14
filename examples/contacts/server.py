"""Ordinary-browser host for the authenticated Minimal Contacts example.

Unlike ``demo.py``, this entry point does not launch or inject a Playwright browser.  The
application's own HttpOnly login cookie selects the actor, a server hook binds it to that
actor's delegated credential, and the authenticated page fetches one ephemeral Runtime
bootstrap.  It is a local synthetic reference, not a production identity provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import signal
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from examples.contacts.adapter import MinimalContactsAdapter
from examples.contacts.app import DEMO_SESSION_COOKIE, DemoActor, MinimalContactsApplication
from examples.contacts.demo import DemoConfig, _chat
from web_operation_agent.application import bind_application
from web_operation_agent.durable import (SQLitePrincipalRevocations, SQLiteSessionStore)
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.live import LiveConversationRunner, LiveProductServer, LiveProductSession
from web_operation_agent.mutation import SQLiteMutationJournal
from web_operation_agent.model import ProviderError, ProviderRuntimeGuard, require_chat_port
from web_operation_agent.product import AgentTurnConfig, AgentTurnExecutor


class _PrincipalRequestAdmission:
    """Keep one authenticated actor from occupying every provider slot."""

    def __init__(self, maximum: int):
        self.maximum = maximum
        self._condition = threading.Condition()
        self._active: dict[str, int] = {}

    def acquire(self, key: str, timeout_s: float, *, cancelled=None) -> None:
        deadline = time.monotonic() + max(0.001, float(timeout_s))
        with self._condition:
            while self._active.get(key, 0) >= self.maximum:
                if cancelled is not None and cancelled():
                    raise ProviderError(
                        "provider call cancelled while waiting for principal capacity",
                        code="provider_cancelled", retryable=False, dispatched=False)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderError(
                        "principal provider capacity was unavailable before the deadline",
                        code="provider_capacity", retryable=True, dispatched=False)
                self._condition.wait(min(.05, remaining))
            self._active[key] = self._active.get(key, 0) + 1

    def release(self, key: str) -> None:
        with self._condition:
            count = self._active.get(key, 0)
            if count <= 0:
                raise RuntimeError("principal provider slot released without acquisition")
            if count == 1:
                self._active.pop(key, None)
            else:
                self._active[key] = count - 1
            self._condition.notify_all()

    def active(self, key: str) -> int:
        with self._condition:
            return self._active.get(key, 0)


class _PrincipalLimitedChat:
    """ChatPort decorator for per-principal admission; provider health stays delegated."""

    def __init__(self, delegate, admission: _PrincipalRequestAdmission, key: str):
        self._delegate = require_chat_port(delegate, role="principal-limited chat delegate")
        self._admission = admission
        self._key = key
        self.model = delegate.model
        self.meter = getattr(delegate, "meter", None)
        self.options = getattr(delegate, "options", None)
        self.egress_policy = getattr(delegate, "egress_policy", None)

    def complete(self, messages, tools, run_id, max_tokens=None, options=None,
                 timeout_s=None):
        timeout = float(timeout_s if timeout_s is not None else 180)
        self._admission.acquire(self._key, timeout)
        try:
            return self._delegate.complete(
                messages, tools, run_id, max_tokens=max_tokens, options=options,
                timeout_s=timeout_s)
        finally:
            self._admission.release(self._key)

    def complete_cancellable(self, messages, tools, run_id, *, cancelled,
                             max_tokens=None, options=None, timeout_s=None):
        timeout = float(timeout_s if timeout_s is not None else 180)
        self._admission.acquire(self._key, timeout, cancelled=cancelled)
        try:
            return self._delegate.complete_cancellable(
                messages, tools, run_id, cancelled=cancelled,
                max_tokens=max_tokens, options=options, timeout_s=timeout_s)
        finally:
            self._admission.release(self._key)


@dataclass(frozen=True, slots=True)
class NativeServerConfig:
    state_dir: Path
    port: int = 8765
    scripted: bool = False
    model_env: Path | None = None
    model: str | None = None
    allow_mutations: bool = False
    max_sessions: int = 10
    public_agent_url: str | None = None
    session_lease_ms: int = 5_000
    max_active_requests: int = 3
    max_active_requests_per_actor: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_dir", Path(self.state_dir).expanduser())
        if self.model_env is not None:
            object.__setattr__(self, "model_env", Path(self.model_env).expanduser())
        if isinstance(self.port, bool) or not 0 <= self.port <= 65535:
            raise ValueError("port must be an integer from 0 to 65535")
        if isinstance(self.max_sessions, bool) or not 1 <= self.max_sessions <= 100:
            raise ValueError("max_sessions must be an integer from 1 to 100")
        if (isinstance(self.session_lease_ms, bool)
                or not 1_000 <= self.session_lease_ms <= 60_000):
            raise ValueError("session_lease_ms must be an integer from 1000 to 60000")
        for name in ("max_active_requests", "max_active_requests_per_actor"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 1 <= value <= 100):
                raise ValueError(f"{name} must be an integer from 1 to 100")
        if self.max_active_requests_per_actor > self.max_active_requests:
            raise ValueError(
                "max_active_requests_per_actor cannot exceed max_active_requests")


class NativeContactsHost:
    """One local application, gateway and durable session host."""

    def __init__(self, config: NativeServerConfig):
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.application = MinimalContactsApplication(port=config.port)
        self.session_store = SQLiteSessionStore(
            config.state_dir / "session-store.sqlite3",
            lease_ms=config.session_lease_ms)
        self.revocations = SQLitePrincipalRevocations(self.session_store)
        self.mutation_journal = SQLiteMutationJournal(
            config.state_dir / "mutation-journal.sqlite3")
        self.provider_guard = ProviderRuntimeGuard(
            max_concurrent_requests=config.max_active_requests)
        self.principal_admission = _PrincipalRequestAdmission(
            config.max_active_requests_per_actor)
        self.server = LiveProductServer(
            max_sessions=config.max_sessions,
            durable_store=self.session_store,
            principal_validator=self.revocations.validate,
            public_base_url=config.public_agent_url,
            provider_status=self.provider_guard.status)
        self._sessions: dict[tuple[str, str], tuple[LiveProductSession, object]] = {}
        self._lock = threading.RLock()
        self._closed = False
        self.application.install_assistant(
            bootstrap=self._bootstrap, revoke=self._revoke_actor)

    @property
    def origin(self) -> str:
        return self.application.origin

    def _new_chat(self, run_dir: Path, principal_key: str):
        demo_config = DemoConfig(
            scripted=self.config.scripted,
            model_env=self.config.model_env,
            model=self.config.model,
            allow_mutations=self.config.allow_mutations,
            output_dir=run_dir.parent,
        )
        chat, meter = _chat(
            demo_config, run_dir, runtime_guard=self.provider_guard)
        return _PrincipalLimitedChat(
            chat, self.principal_admission, principal_key), meter

    def _bootstrap(self, actor: DemoActor, runtime_instance_id: str) -> dict:
        key = (actor.actor, runtime_instance_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("native host is closed")
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

            adapter = MinimalContactsAdapter(
                self.application.origin, api_token=actor.token,
                allow_mutations=self.config.allow_mutations,
                mutation_journal=(self.mutation_journal
                                  if self.config.allow_mutations else None))
            bound = bind_application(adapter)
            page = adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={actor.session}")
            principal = adapter.identity.bind(
                page, delegation_id="contacts-native-" + uuid.uuid4().hex)
            run_dir = self.config.state_dir / "runs" / (
                time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            chat, _meter = self._new_chat(run_dir, principal.binding_digest)

            def runner_factory(channel):
                executor = AgentTurnExecutor(
                    channel=channel,
                    config=AgentTurnConfig(
                        supported_scope=(
                            "Read current contacts through the typed API and open only an "
                            "exact route returned by that API. Do not modify records or "
                            "submit forms."
                            if not self.config.allow_mutations else
                            "Read current contacts through the typed API, and create at most "
                            "the one contact the user confirms. Do not submit page forms."),
                        ledger_path=run_dir / "audit.jsonl",
                        step_budget=8,
                        wall_budget_s=90,
                        transport_timeout_s=30,
                        disabled_actions=frozenset(
                            {"click", "fill", "select", "submit", "back"}),
                        scope_first_policy=True,
                        policy_block_limit=2,
                        plain_business_answer_min_page_actions=0,
                        max_user_answer_chars=8_000,
                        require_identity=True,
                        allow_business_mutations=self.config.allow_mutations,
                        deployment_profile="production",
                    ),
                    chat=chat,
                    application=bound,
                    control=ReadOnlyControl(),
                    run_id_factory=lambda _message: "native-" + uuid.uuid4().hex[:16],
                )
                return LiveConversationRunner(channel, executor=executor)

            stable = hashlib.sha256(
                f"{actor.session}\0{runtime_instance_id}".encode()).hexdigest()[:40]
            session = self.server.create_session(
                allowed_origin=bound.descriptor.origin,
                page_controller_module=bound.descriptor.page_controller_module,
                runner_factory=runner_factory,
                session_id="contacts-" + stable,
                navigation_mode=bound.descriptor.navigation_mode,
                sensitive_query_keys=bound.ports.routes.session_handoff_query_keys,
                principal=principal,
            ).start()
            self._sessions[key] = (session, principal)
            return self._browser_bootstrap(session)

    def _browser_bootstrap(self, session: LiveProductSession) -> dict:
        return {
            "schema_version": 1,
            "runtime_module": self.server.gateway.runtime_url,
            "config": self.server.gateway.bootstrap_config(
                session.grant,
                page_controller_module=session.page_controller_module,
                navigation_mode=session.navigation_mode,
                sensitive_query_keys=session.sensitive_query_keys),
        }

    def _revoke_actor(self, actor: DemoActor) -> None:
        with self._lock:
            matches = [(key, value) for key, value in self._sessions.items()
                       if key[0] == actor.actor]
            for key, (session, principal) in matches:
                self.revocations.revoke(principal, reason="application_logout")
                session.close("principal_revoked")
                self._sessions.pop(key, None)

    def start(self) -> "NativeContactsHost":
        self.server.start()
        self.application.start()
        return self

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._sessions.clear()
        self.application.close()
        self.server.close()

    def __enter__(self) -> "NativeContactsHost":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m examples.contacts.server",
        description="Serve Minimal Contacts with application-native assistant bootstrap.")
    parser.add_argument("--state-dir", default=str(
        Path(tempfile.gettempdir()) / "web-operation-agent-native-contacts"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--scripted", action="store_true")
    parser.add_argument("--model-env")
    parser.add_argument("--model")
    parser.add_argument("--allow-mutations", action="store_true")
    parser.add_argument("--max-sessions", type=int, default=10)
    parser.add_argument("--public-agent-url")
    parser.add_argument("--session-lease-ms", type=int, default=5_000)
    parser.add_argument("--max-active-requests", type=int, default=3)
    parser.add_argument("--max-active-requests-per-actor", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = NativeServerConfig(
        state_dir=Path(args.state_dir), port=args.port, scripted=args.scripted,
        model_env=Path(args.model_env) if args.model_env else None,
        model=args.model, allow_mutations=args.allow_mutations,
        max_sessions=args.max_sessions, public_agent_url=args.public_agent_url,
        session_lease_ms=args.session_lease_ms,
        max_active_requests=args.max_active_requests,
        max_active_requests_per_actor=args.max_active_requests_per_actor)
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _number, _frame: stopped.set())
    with NativeContactsHost(config) as host:
        print(json.dumps({
            "status": "READY",
            "application": host.origin,
            "login": {
                "writer": host.origin + "/demo/login/ada-lovelace",
                "reader": host.origin + "/demo/login/rosalind-franklin",
            },
            "gateway_listener": host.server.base_url,
            "browser_uses_playwright": False,
        }, ensure_ascii=False, sort_keys=True), flush=True)
        stopped.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
