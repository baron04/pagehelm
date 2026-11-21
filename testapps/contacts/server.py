"""Ordinary-browser host for the authenticated Minimal Contacts example."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import signal
import tempfile
import threading

from testapps.contacts.adapter import (
    ContactsModelGoalPort, ContactsReadActions, ContactsSubmitPort,
    MinimalContactsAdapter, contacts_profile,
)
from testapps.contacts.app import DEMO_SESSION_COOKIE, DemoActor, MinimalContactsApplication
from testapps.contacts.declarative.declarative_host import DeclarativeHostConfig, DeclarativeSessionHost, admit_armed_write
from testapps.contacts.demo import DemoConfig, _chat
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.modeling.client import KeyedLimitedChat


@dataclass(frozen=True, slots=True)
class NativeServerConfig(DeclarativeHostConfig):
    adversarial_form_fixture: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.adversarial_form_fixture, bool):
            raise ValueError("adversarial_form_fixture must be a boolean")


class NativeContactsHost(DeclarativeSessionHost):
    """One local application, gateway, and durable session host for Minimal Contacts."""

    def __init__(self, config: NativeServerConfig):
        application = MinimalContactsApplication(port=config.port, adversarial_form_fixture=config.adversarial_form_fixture)
        super().__init__(config, application=application)
        application._write_gate = self._admit_write
        application.install_assistant(bootstrap=self._bootstrap, revoke=self._revoke_actor, script_url=self.server.gateway.runtime_url)

    @property
    def origin(self) -> str:
        return self.application.origin

    def _make_turn_config(self, **kwargs) -> AgentTurnConfig:
        return AgentTurnConfig(**kwargs)

    def _new_chat(self, run_dir: Path, principal_key: str):
        demo_config = DemoConfig(scripted=self.config.scripted, model_env=self.config.model_env, model=self.config.model, allow_mutations=self.config.allow_mutations, output_dir=run_dir.parent)
        chat, meter = _chat(demo_config, run_dir, runtime_guard=self.provider_guard)
        return KeyedLimitedChat(chat, self.principal_admission, principal_key), meter

    def _bootstrap(self, actor: DemoActor, runtime_instance_id: str) -> dict:
        supported_scope = (
            "Read current contacts through the typed API and open only an exact route returned by that API. "
            "Do not modify records or submit forms. Reviewed temporary form entry and prefill are allowed for "
            "the current actor; highlight Save for manual submission."
            if not self.config.allow_mutations
            else "Read current contacts through the typed API, and create at most the one contact the user "
            "confirms. Do not submit page forms."
        )
        return self.open_or_reuse_session(
            actor_id=actor.actor, runtime_instance_id=runtime_instance_id,
            session_token=actor.session, session_prefix="contacts",
            adapter_factory=lambda: MinimalContactsAdapter(
                self.application.origin, api_token=actor.token,
                allow_mutations=self.config.allow_mutations,
                mutation_journal=self.mutation_journal if self.config.allow_mutations else None,
                standard_skills=self.config.model_loop and self.config.allow_mutations,
            ),
            page_credential=f"{DEMO_SESSION_COOKIE}={actor.session}",
            delegation_prefix="contacts-native", supported_scope=supported_scope,
            goal_port_factory=lambda adapter: ContactsModelGoalPort(adapter, drafts=True, filters=True),
            profile_factory=lambda adapter, gp: contacts_profile(adapter, gp, drafts=True, filters=True),
            action_policy_factory=lambda adapter: ContactsReadActions(adapter, filters=True),
            submit_port_factory=lambda adapter: ContactsSubmitPort(adapter, journal=self.application.write_journal, reads=self.application.read_journal),
            page_write_guard="/api/page-window", submit_port_key=actor.session,
        )

    def _revoke_actor(self, actor: DemoActor) -> None:
        self._revoke_actor_id(actor.actor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m testapps.contacts.server", description="Serve Minimal Contacts with application-native assistant bootstrap.")
    parser.add_argument("--state-dir", default=str(Path(tempfile.gettempdir()) / "web-operation-agent-native-contacts"))
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
        model_env=Path(args.model_env) if args.model_env else None, model=args.model,
        allow_mutations=args.allow_mutations, max_sessions=args.max_sessions,
        public_agent_url=args.public_agent_url, session_lease_ms=args.session_lease_ms,
        max_active_requests=args.max_active_requests, max_active_requests_per_actor=args.max_active_requests_per_actor,
    )
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _number, _frame: stopped.set())
    with NativeContactsHost(config) as host:
        print(json.dumps({
            "status": "READY", "application": host.origin,
            "login": {"writer": host.origin + "/demo/login/ada-lovelace", "reader": host.origin + "/demo/login/rosalind-franklin"},
            "gateway_listener": host.server.base_url, "browser_uses_playwright": False,
        }, ensure_ascii=False, sort_keys=True), flush=True)
        stopped.wait()
    return 0


__all__ = ["NativeContactsHost", "NativeServerConfig", "admit_armed_write", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
