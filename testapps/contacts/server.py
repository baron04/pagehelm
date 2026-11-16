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

from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.app import DEMO_SESSION_COOKIE, DemoActor, MinimalContactsApplication
from testapps.contacts.demo import DemoConfig, _chat
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.state import SQLitePrincipalRevocations, SQLiteSessionStore
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.live import LiveConversationRunner, LiveProductServer, LiveProductSession
from web_operation_agent.business.mutation import SQLiteMutationJournal
from web_operation_agent.modeling.client import (KeyedLimitedChat, KeyedProviderAdmission,
                                       ProviderRuntimeGuard)
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor


def admit_armed_write(armed, method: str, path: str, query: str, body: bytes) -> dict:
    """The application's gate verdict for one marked write inside its window.

    Only the armed permit may say yes; nothing else the application or the host
    knows can widen what one approved click may cause.
    """
    if armed is None:
        return {"allowed": False, "code": "not_armed"}
    decision = armed.admit(method, path, query, body)
    if not decision.allowed:
        return {"allowed": False, "code": decision.code}
    return {"allowed": True, "code": "",
            "settle": lambda status, body: armed.settle(
                decision.sequence, status, body)}


@dataclass(frozen=True, slots=True)
class NativeServerConfig:
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
    adversarial_form_fixture: bool = False
    require_task_contract: bool = False
    model_loop: bool = False
    #: Off by default: arming the native write contract is the integration's choice.
    native_submit_writes: bool = False

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
        for name in ("max_active_requests", "max_active_requests_per_actor",
                     "max_sessions_per_actor"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 1 <= value <= 100):
                raise ValueError(f"{name} must be an integer from 1 to 100")
        if self.max_active_requests_per_actor > self.max_active_requests:
            raise ValueError(
                "max_active_requests_per_actor cannot exceed max_active_requests")
        if self.max_sessions_per_actor > self.max_sessions:
            raise ValueError("max_sessions_per_actor cannot exceed max_sessions")
        if not isinstance(self.adversarial_form_fixture, bool):
            raise ValueError("adversarial_form_fixture must be a boolean")
        if not isinstance(self.model_loop, bool):
            raise ValueError("model_loop must be a boolean")
        if not isinstance(self.require_task_contract, bool):
            raise ValueError("require_task_contract must be a boolean")
        if (isinstance(self.browser_idle_timeout_s, bool)
                or not isinstance(self.browser_idle_timeout_s, int)
                or not 30 <= self.browser_idle_timeout_s <= 86_400):
            raise ValueError("browser_idle_timeout_s must be from 30 to 86400")


class NativeContactsHost:
    """One local application, gateway and durable session host."""

    def __init__(self, config: NativeServerConfig):
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.application = MinimalContactsApplication(
            port=config.port,
            adversarial_form_fixture=config.adversarial_form_fixture)
        self.session_store = SQLiteSessionStore(
            config.state_dir / "session-store.sqlite3",
            lease_ms=config.session_lease_ms)
        self.revocations = SQLitePrincipalRevocations(self.session_store)
        self.mutation_journal = SQLiteMutationJournal(
            config.state_dir / "mutation-journal.sqlite3")
        self.provider_guard = ProviderRuntimeGuard(
            max_concurrent_requests=config.max_active_requests)
        self.principal_admission = KeyedProviderAdmission(
            config.max_active_requests_per_actor)
        self.server = LiveProductServer(
            max_sessions=config.max_sessions,
            max_sessions_per_actor=config.max_sessions_per_actor,
            browser_idle_timeout_s=config.browser_idle_timeout_s,
            durable_store=self.session_store,
            principal_validator=self.revocations.validate,
            public_base_url=config.public_agent_url,
            provider_status=self.provider_guard.status)
        self._sessions: dict[tuple[str, str], tuple[LiveProductSession, object]] = {}
        self._submit_ports: dict[str, object] = {}
        self._lock = threading.RLock()
        self._closed = False
        self.application._write_gate = self._admit_write
        self.application.install_assistant(
            bootstrap=self._bootstrap, revoke=self._revoke_actor,
            script_url=self.server.gateway.runtime_url)

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
        return KeyedLimitedChat(
            chat, self.principal_admission, principal_key), meter

    def _admit_write(self, session_key: str, method: str, path: str,
                     query: str, body: bytes) -> dict:
        port = self._submit_ports.get(session_key)
        return admit_armed_write(getattr(port, "armed", None), method, path, query, body)

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
                                  if self.config.allow_mutations else None),
                standard_skills=self.config.model_loop and self.config.allow_mutations)
            bound = bind_application(adapter)
            page = adapter.identity.page_identity(
                f"{DEMO_SESSION_COOKIE}={actor.session}")
            principal = adapter.identity.bind(
                page, delegation_id="contacts-native-" + uuid.uuid4().hex)
            run_dir = self.config.state_dir / "runs" / (
                time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            chat, _meter = self._new_chat(
                run_dir, principal.actor_authority_digest)

            def runner_factory(channel):
                executor_type = AgentTurnExecutor
                loop_options = {}
                action_policy = None
                if self.config.model_loop:
                    from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
                    from testapps.contacts.model_loop import ContactsModelGoalPort, contacts_profile
                    from testapps.contacts.read_actions import ContactsReadActions
                    executor_type = ModelDrivenTurnExecutor
                    goal_port = ContactsModelGoalPort(adapter, drafts=True, filters=True)
                    action_policy = ContactsReadActions(adapter, filters=True)
                    loop_options = {"goal_port": goal_port, "profile": contacts_profile(adapter, goal_port, drafts=True, filters=True), "verify": False}
                executor = executor_type(
                    channel=channel,
                    config=AgentTurnConfig(
                        supported_scope=(
                            "Read current contacts through the typed API and open only an "
                            "exact route returned by that API. Do not modify records or "
                            "submit forms. Reviewed temporary form entry and prefill are "
                            "allowed for the current actor; highlight Save for manual submission."
                            if not self.config.allow_mutations else
                            "Read current contacts through the typed API, and create at most "
                            "the one contact the user confirms. Do not submit page forms."),
                        ledger_path=run_dir / "audit.jsonl",
                        step_budget=8,
                        wall_budget_s=90,
                        transport_timeout_s=30,
                        disabled_actions=frozenset({"submit", "back"} if self.config.model_loop else
                            {"click", "fill", "select", "submit", "back"}),
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
                if self.config.model_loop and self.config.native_submit_writes:
                    from testapps.contacts.native_submit import ContactsSubmitPort
                    port = ContactsSubmitPort(adapter,
                        journal=self.application.write_journal,
                        reads=self.application.read_journal)
                    executor.native_submit = port
                    channel.read_only_mode = False
                    self._submit_ports[actor.session] = port
                return LiveConversationRunner(channel, executor=executor)

            stable = hashlib.sha256(
                f"{actor.session}\0{runtime_instance_id}".encode()).hexdigest()[:40]
            session = self.server.create_session(
                allowed_origin=bound.descriptor.origin,
                page_controller_module=bound.descriptor.page_controller_module,
                runner_factory=runner_factory,
                session_id="contacts-" + stable,
                navigation_mode=bound.descriptor.navigation_mode,
                page_write_guard="/api/page-window" if self.config.model_loop else "",
                sensitive_query_keys=bound.ports.routes.session_handoff_query_keys,
                principal=principal,
            ).start()
            session.channel.read_only_mode = not (
                self.config.allow_mutations or (self.config.model_loop and self.config.native_submit_writes))
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
                page_write_guard=session.page_write_guard),
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
        prog="python -m testapps.contacts.server",
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
