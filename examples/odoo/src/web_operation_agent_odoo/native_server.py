"""Single-instance ordinary-browser host for the Odoo integration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from web_operation_agent.application import bind_application
from web_operation_agent.conversation import (SQLiteConversationState,
                                               prune_sqlite_conversations)
from web_operation_agent.durable import (SQLiteExpiringNonceStore,
                                         SQLitePrincipalRevocations,
                                         SQLiteSessionStore)
from web_operation_agent.egress import ProviderEgressPolicy
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.live import (LiveConversationRunner, LiveProductCapacityError,
                                      LiveProductServer, LiveProductSession)
from web_operation_agent.model import (ChatModel, KeyedLimitedChat,
                                       KeyedProviderAdmission, ProviderRuntimeGuard,
                                       SpendMeter, load_env)
from web_operation_agent.mutation import SQLiteMutationJournal
from web_operation_agent.observability import ObservedChat, RuntimeObservability
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.product import AgentTurnExecutor

from .turn import odoo_turn_config
from web_operation_agent.remote import RemoteProtocolError
from web_operation_agent.identity import PrincipalContext, PrincipalError

from .adapter import OdooApplicationAdapter
from .demo.demo import ScriptedOdooChat
from .native import (MAX_EVENT_BYTES, OdooDelegationDirectory, OdooSessionEvent,
                     OdooSessionEventError, SignedOdooSessionVerifier,
                     load_session_secret)


_MODEL_ENV_KEYS = (
    "AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_API_KEY",
    "AGENT_OPENAI_MODEL", "AGENT_PROVIDER",
)
_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
_MAINTENANCE_INTERVAL_S = 60 * 60


class OdooAuthorityMismatchError(OdooSessionEventError):
    """A valid Odoo browser assertion does not match delegated API authority."""


@dataclass(frozen=True, slots=True)
class OdooNativeConfig:
    rpc_base_url: str
    public_origin: str
    public_agent_url: str
    delegation_dir: Path
    session_secret_path: Path
    state_dir: Path
    gateway_host: str = "127.0.0.1"
    trusted_gateway_network: bool = False
    gateway_port: int = 8766
    bootstrap_host: str = "127.0.0.1"
    bootstrap_port: int = 8767
    allow_private_http: bool = False
    allow_mutations: bool = False
    scripted: bool = False
    model_env: Path | None = None
    model: str | None = None
    principal_lifetime_s: int = 900
    max_sessions: int = 10
    max_sessions_per_actor: int = 3
    #: How long a session survives with no word from its browser. It guards against
    #: sessions whose page is gone, not against a person reading something else: a
    #: backgrounded tab has its timers throttled, and at 90s that alone was enough to have
    #: the assistant taken away from someone who had merely looked at another window.
    browser_idle_timeout_s: int = 900
    max_active_requests: int = 3
    max_active_requests_per_actor: int = 2
    session_lease_ms: int = 5_000
    turn_timeout_s: int = 120
    otlp_endpoint: str = ""
    alert_webhook: str = ""
    cost_alert_cny: float | None = None

    def __post_init__(self) -> None:
        for name in ("delegation_dir", "session_secret_path", "state_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser())
        if self.model_env is not None:
            object.__setattr__(self, "model_env", Path(self.model_env).expanduser())
        for name in ("gateway_host", "bootstrap_host"):
            if getattr(self, name) not in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
                raise ValueError(f"{name} must be loopback or explicit all-interfaces")
        if not isinstance(self.trusted_gateway_network, bool):
            raise ValueError("trusted_gateway_network must be boolean")
        if self.gateway_host == "0.0.0.0" and not self.trusted_gateway_network:
            raise ValueError("all-interface gateway requires trusted_gateway_network")
        for name in ("gateway_port", "bootstrap_port"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
                raise ValueError(f"{name} must be an integer from 0 to 65535")
        for name, maximum in (
                ("principal_lifetime_s", 3600), ("max_sessions", 10_000),
                ("max_sessions_per_actor", 10_000),
                ("max_active_requests", 10_000),
                ("max_active_requests_per_actor", 10_000),
                ("turn_timeout_s", 600)):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 1 <= value <= maximum):
                raise ValueError(f"{name} must be an integer from 1 to {maximum}")
        if self.max_active_requests_per_actor > self.max_active_requests:
            raise ValueError("per-actor provider capacity cannot exceed global capacity")
        if self.max_sessions_per_actor > self.max_sessions:
            raise ValueError("per-actor session capacity cannot exceed global capacity")
        if (isinstance(self.browser_idle_timeout_s, bool)
                or not isinstance(self.browser_idle_timeout_s, int)
                or not 30 <= self.browser_idle_timeout_s <= 86_400):
            raise ValueError("browser_idle_timeout_s must be from 30 to 86400")
        if (isinstance(self.session_lease_ms, bool)
                or not 1_000 <= self.session_lease_ms <= 60_000):
            raise ValueError("session_lease_ms must be an integer from 1000 to 60000")


@dataclass(slots=True)
class _ActiveSession:
    session: LiveProductSession
    principal: PrincipalContext
    adapter: OdooApplicationAdapter
    api_key_path: Path
    scopes: tuple[str, ...]
    allow_mutations: bool


class _BootstrapHTTPServer:
    """Signed ingress only; the reverse proxy exposes bootstrap but not revoke."""

    def __init__(self, owner: "OdooNativeHost", host: str, port: int):
        class Handler(BaseHTTPRequestHandler):
            server_version = "WOAOdooBootstrap/1"

            def log_message(self, _format, *_args):
                return

            def _json(self, status: int, payload: dict) -> None:
                body = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")) + "\n").encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                if self.path == "/healthz":
                    self._json(200, {"ready": True})
                elif self.path == "/internal/metrics":
                    self._json(200, owner.operational_snapshot())
                else:
                    self._json(404, {"error": "not_found"})

            def do_POST(self):
                if self.path not in {"/bootstrap", "/internal/revoke"}:
                    self._json(404, {"error": "not_found"})
                    return
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if content_type != "application/json" or not 1 <= length <= MAX_EVENT_BYTES:
                    self._json(400, {"error": "invalid_session_event"})
                    return
                try:
                    envelope = json.loads(self.rfile.read(length))
                    payload = (owner.bootstrap(envelope) if self.path == "/bootstrap"
                               else owner.revoke(envelope))
                except OdooAuthorityMismatchError:
                    self._json(403, {"error": "identity_authority_mismatch",
                                     "retryable": False})
                except OdooSessionEventError:
                    self._json(401, {"error": "invalid_session_event"})
                except LiveProductCapacityError:
                    self._json(429, {"error": "assistant_capacity", "retryable": True})
                except RemoteProtocolError as exc:
                    if exc.code.startswith("session_busy_"):
                        self._json(409, {"error": exc.code, "retryable": True})
                    else:
                        self._json(503, {"error": "assistant_unavailable", "retryable": True})
                except Exception:
                    self._json(503, {"error": "assistant_unavailable", "retryable": True})
                else:
                    self._json(200, payload)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        shown = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        return f"http://{shown}:{port}"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="woa-odoo-bootstrap", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is None:
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._thread = None


class OdooNativeHost:
    """Bind signed Odoo page sessions to per-user delegated API credentials."""

    def __init__(self, config: OdooNativeConfig):
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.delegations = OdooDelegationDirectory(config.delegation_dir)
        self.session_store = SQLiteSessionStore(
            config.state_dir / "session-store.sqlite3",
            lease_ms=config.session_lease_ms)
        self.event_nonces = SQLiteExpiringNonceStore(
            self.session_store, namespace="odoo_session_event")
        self.verifier = SignedOdooSessionVerifier(
            load_session_secret(config.session_secret_path),
            nonce_consumer=lambda nonce, expires, now: self.event_nonces.consume(
                nonce, expires_at=expires, now_ms=now))
        self.revocations = SQLitePrincipalRevocations(self.session_store)
        self.mutation_journal = SQLiteMutationJournal(
            config.state_dir / "mutation-journal.sqlite3")
        self.provider_guard = ProviderRuntimeGuard(
            max_concurrent_requests=config.max_active_requests)
        self.principal_admission = KeyedProviderAdmission(
            config.max_active_requests_per_actor)
        self.telemetry = RuntimeObservability(
            service_name="web-operation-agent-odoo",
            otlp_endpoint=config.otlp_endpoint,
            alert_webhook=config.alert_webhook,
            cost_alert_cny=config.cost_alert_cny)
        self.server = LiveProductServer(
            max_sessions=config.max_sessions,
            max_sessions_per_actor=config.max_sessions_per_actor,
            browser_idle_timeout_s=config.browser_idle_timeout_s,
            host=config.gateway_host, port=config.gateway_port,
            public_base_url=config.public_agent_url,
            allow_private_network_bind=config.trusted_gateway_network,
            durable_store=self.session_store,
            principal_validator=self.revocations.validate,
            provider_status=self.provider_guard.status)
        self.bootstrap_server = _BootstrapHTTPServer(
            self, config.bootstrap_host, config.bootstrap_port)
        self._sessions: dict[tuple[str, str, str], _ActiveSession] = {}
        self._runtime_actor: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self._unresolved_alerted = False

    def _new_chat(self, run_dir: Path, adapter, principal_key: str):
        egress = ProviderEgressPolicy(
            session_handoff_keys=adapter.ports.routes.session_handoff_query_keys)
        if self.config.scripted:
            delegate = ScriptedOdooChat(egress_policy=egress)
        else:
            loaded = load_env(self.config.model_env) if self.config.model_env else {}
            env = {key: value for key, value in loaded.items() if key in _MODEL_ENV_KEYS}
            for key in _MODEL_ENV_KEYS:
                if os.environ.get(key):
                    env[key] = os.environ[key]
            meter = SpendMeter(
                ledger=run_dir / "model-spend.jsonl",
                batch_id=f"odoo-native-{run_dir.name}")
            delegate = ChatModel(
                meter, env=env, model=self.config.model, max_completion_tokens=2_048,
                timeout=self.config.turn_timeout_s, egress_policy=egress,
                runtime_guard=self.provider_guard)
        limited = KeyedLimitedChat(
            delegate, self.principal_admission, principal_key,
            wait_observer=self.telemetry.model_wait)
        return ObservedChat(limited, self.telemetry)

    def _adapter_and_principal(self, event: OdooSessionEvent):
        grant = self.delegations.resolve(event)
        allow_mutations = self.config.allow_mutations and grant.allow_mutations
        adapter = OdooApplicationAdapter(
            base_url=self.config.rpc_base_url,
            public_origin=self.config.public_origin,
            api_key_path=grant.api_key_path,
            page_controller_module=(
                self.config.public_origin
                + "/woa_runtime/static/src/page-controller.js"),
            allow_private_http=self.config.allow_private_http,
            allow_mutations=allow_mutations,
            mutation_journal=self.mutation_journal if allow_mutations else None)
        try:
            principal = adapter.bind_principal(
                event.page_identity(lifetime_s=self.config.principal_lifetime_s),
                delegation_id="odoo-native-" + uuid.uuid4().hex,
                lifetime_s=self.config.principal_lifetime_s,
                scopes=grant.scopes)
        except PrincipalError as exc:
            raise OdooAuthorityMismatchError(
                "browser and delegated API authority do not match") from exc
        return adapter, principal, allow_mutations, grant

    def _renew_active(self, active: _ActiveSession, principal: PrincipalContext,
                      grant, allow_mutations: bool) -> None:
        """Install an already reauthenticated identity while holding only local state."""
        if (grant.api_key_path != active.api_key_path
                or grant.scopes != active.scopes
                or allow_mutations != active.allow_mutations):
            active.session.close("delegated_authority_changed")
            raise OdooAuthorityMismatchError(
                "delegated authority changed; a new browser session is required")
        principal = replace(
            principal, delegation_id=active.principal.delegation_id)
        active.session.renew_principal(principal)
        active.principal = principal

    def bootstrap(self, envelope: object) -> dict:
        event = self.verifier.verify(envelope)
        if event.event != "bootstrap":
            raise OdooSessionEventError("bootstrap endpoint requires a bootstrap event")
        key = (event.database, event.actor_id, event.runtime_instance_id)
        runtime_key = (event.database, event.runtime_instance_id)
        # Both application identity RPCs run before the host-wide lock. A slow Odoo user
        # must not serialize bootstrap and logout for every other actor.
        try:
            adapter, principal, allow_mutations, delegation = (
                self._adapter_and_principal(event))
        except OdooAuthorityMismatchError:
            # A valid page identity that no longer matches delegated API authority must
            # close any capability previously issued for this browser document.
            with self._lock:
                previous = self._sessions.pop(key, None)
                self._runtime_actor.pop(runtime_key, None)
                if previous is not None:
                    previous.session.close("identity_authority_mismatch")
            raise
        with self._lock:
            if self._closed:
                raise RuntimeError("Odoo native host is closed")
            previous_actor = self._runtime_actor.get(runtime_key)
            if previous_actor is not None and previous_actor != event.actor_id:
                self._revoke_actor(event.database, previous_actor, "application_actor_changed")
            previous = self._sessions.get(key)
            if previous is not None:
                try:
                    previous.session.channel.validate_live()
                except Exception:
                    previous.session.close("native_bootstrap_replaced")
                    self._sessions.pop(key, None)
                else:
                    try:
                        self._renew_active(
                            previous, principal, delegation, allow_mutations)
                    except RemoteProtocolError as exc:
                        # The session is mid-turn or holding a confirmation, so its
                        # authority cannot be swapped underneath it. That is not a reason
                        # to refuse the page: it is the same page coming back, its current
                        # assertion is still live, and refusing left it unable to start at
                        # all -- while the confirmation it was refused over could only be
                        # answered from the panel it was refused. It reconnects to its own
                        # session and renews when the turn is done.
                        if not str(exc.code).startswith("session_busy_"):
                            raise
                    return self._browser_bootstrap(previous.session)

            bound = bind_application(adapter)
            stable = hashlib.sha256(
                f"{event.database}\0{event.actor_id}\0{event.runtime_instance_id}"
                .encode("utf-8")).hexdigest()[:40]
            run_dir = self.config.state_dir / "runs" / (
                time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            chat = self._new_chat(
                run_dir, adapter, principal.actor_authority_digest)

            def runner_factory(channel):
                def ledger_factory(path, run_id, *, base=None):
                    return SessionLedger(
                        path, run_id, base=base, event_sink=self.telemetry)

                executor = AgentTurnExecutor(
                    channel=channel,
                    config=odoo_turn_config(
                        adapter=adapter, run_dir=run_dir,
                        allow_mutations=allow_mutations,
                        scripted=self.config.scripted,
                        turn_timeout_s=self.config.turn_timeout_s),
                    chat=chat, application=bound, control=ReadOnlyControl(),
                    run_id_factory=lambda _message: "odoo-" + uuid.uuid4().hex[:16],
                    ledger_factory=ledger_factory,
                    conversation_state=SQLiteConversationState(
                        self.config.state_dir / "conversation-state.sqlite3",
                        "odoo-" + stable))
                return LiveConversationRunner(channel, executor=executor)

            session = self.server.create_session(
                allowed_origin=bound.descriptor.origin,
                page_controller_module=bound.descriptor.page_controller_module,
                runner_factory=runner_factory,
                session_id="odoo-" + stable,
                navigation_mode=bound.descriptor.navigation_mode,
                sensitive_query_keys=bound.ports.routes.session_handoff_query_keys,
                principal=principal).start()
            self._sessions[key] = _ActiveSession(
                session=session, principal=principal, adapter=adapter,
                api_key_path=delegation.api_key_path, scopes=delegation.scopes,
                allow_mutations=allow_mutations)
            self._runtime_actor[runtime_key] = event.actor_id
            return self._browser_bootstrap(session)

    def _browser_bootstrap(self, session: LiveProductSession) -> dict:
        now_ms = int(time.time() * 1000)
        lifetime_ms = max(1, session.grant.expires_at - now_ms)
        renewal_lead_ms = min(300_000, max(1_000, lifetime_ms // 3))
        renew_at = max(now_ms + 1, session.grant.expires_at - renewal_lead_ms)
        return {
            "schema_version": 1,
            "renew_at": renew_at,
            "config": self.server.gateway.bootstrap_config(
                session.grant,
                page_controller_module=session.page_controller_module,
                navigation_mode=session.navigation_mode,
                sensitive_query_keys=session.sensitive_query_keys),
        }

    def _revoke_actor(self, database: str, actor_id: str, reason: str) -> int:
        self.revocations.revoke_actor(
            application_id="odoo", tenant_id=database, actor_id=actor_id,
            reason=reason)
        matches = [
            (key, value) for key, value in self._sessions.items()
            if key[0] == database and key[1] == actor_id
        ]
        for key, active in matches:
            self.revocations.revoke(active.principal, reason=reason)
            active.session.close("principal_revoked")
            self._sessions.pop(key, None)
            self._runtime_actor.pop((key[0], key[2]), None)
        return len(matches)

    def revoke(self, envelope: object) -> dict:
        event = self.verifier.verify(envelope)
        if event.event != "revoke":
            raise OdooSessionEventError("revoke endpoint requires a revoke event")
        with self._lock:
            count = self._revoke_actor(
                event.database, event.actor_id, "application_logout")
        return {"revoked": count}

    def _forget_closed_sessions(self) -> None:
        """Drop the entries of sessions that have closed without a logout.

        A tab closed without logging out is reaped by the server after its idle timeout, but
        its entry here stayed until the actor logged out: `active_sessions` counted every such
        tab, and maintenance, which prunes run ledgers only while no session is active, never
        pruned them. The tab-to-actor record is kept, so a different actor later signing in
        on that tab still revokes the previous one.
        """
        # Reading the count is what makes the server close sessions idle past the browser
        # timeout; it reaps lazily.
        self.server.session_count
        with self._lock:
            for key, active in list(self._sessions.items()):
                try:
                    active.session.channel.validate_live()
                except Exception:
                    self._sessions.pop(key, None)

    def operational_snapshot(self) -> dict:
        snapshot = self.telemetry.snapshot()
        self._forget_closed_sessions()
        with self._lock:
            active_sessions = len(self._sessions)
        unresolved = len(self.mutation_journal.unresolved())
        snapshot.update({
            "active_sessions": active_sessions,
            "session_capacity": self.config.max_sessions,
            "principal_session_capacity": self.config.max_sessions_per_actor,
            "unresolved_mutations": unresolved,
            "durable_state_bytes": sum(
                path.stat().st_size for path in self.config.state_dir.glob("*.sqlite3")
                if path.is_file() and not path.is_symlink()),
        })
        return snapshot

    def _maintenance_once(self) -> None:
        now = int(time.time() * 1000)
        cutoff = now - _RETENTION_MS
        try:
            unresolved = len(self.mutation_journal.unresolved())
            if unresolved and not self._unresolved_alerted:
                self.telemetry.alert("unresolved_mutations")
            self._unresolved_alerted = bool(unresolved)
            self.mutation_journal.sweep(now_ms=now, retain_resolved_ms=_RETENTION_MS)
            self.session_store.prune(older_than_ms=cutoff)
            self.event_nonces.prune(now_ms=now)
            self.revocations.prune(older_than_ms=cutoff, now_ms=now)
            prune_sqlite_conversations(
                self.config.state_dir / "conversation-state.sqlite3",
                older_than_ms=cutoff, now_ms=now)
            self.telemetry.reap_stale_runs(max_age_s=900)
            self._forget_closed_sessions()
            # An unresolved write can refer to a run ledger. Keep all ledgers while any
            # such row exists; this conservative rule avoids deleting reconciliation evidence.
            with self._lock:
                active_sessions = bool(self._sessions)
            if not unresolved and not active_sessions:
                runs = self.config.state_dir / "runs"
                if runs.is_dir() and not runs.is_symlink():
                    for path in runs.iterdir():
                        if (path.is_dir() and not path.is_symlink()
                                and int(path.stat().st_mtime * 1000) < cutoff):
                            shutil.rmtree(path)
        except Exception:
            self.telemetry.maintenance_failed()

    def _maintenance_loop(self) -> None:
        while not self._maintenance_stop.wait(_MAINTENANCE_INTERVAL_S):
            self._maintenance_once()

    def start(self) -> "OdooNativeHost":
        self.server.start()
        self.bootstrap_server.start()
        self._maintenance_once()
        if self._maintenance_thread is None:
            self._maintenance_thread = threading.Thread(
                target=self._maintenance_loop, name="woa-odoo-maintenance", daemon=True)
            self._maintenance_thread.start()
        return self

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._sessions.clear()
            self._runtime_actor.clear()
        self._maintenance_stop.set()
        if self._maintenance_thread is not None:
            self._maintenance_thread.join(timeout=5)
            self._maintenance_thread = None
        self.bootstrap_server.close()
        self.server.close()
        self.telemetry.close()

    def __enter__(self) -> "OdooNativeHost":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m web_operation_agent_odoo.native_server",
        description="Serve Odoo's authenticated ordinary-browser assistant.")
    parser.add_argument("--rpc-base-url", required=True)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--public-agent-url", required=True)
    parser.add_argument("--delegation-dir", required=True)
    parser.add_argument("--session-secret", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--gateway-host", default="127.0.0.1")
    parser.add_argument("--trusted-gateway-network", action="store_true")
    parser.add_argument("--gateway-port", type=int, default=8766)
    parser.add_argument("--bootstrap-host", default="127.0.0.1")
    parser.add_argument("--bootstrap-port", type=int, default=8767)
    parser.add_argument("--allow-private-http", action="store_true")
    parser.add_argument("--allow-mutations", action="store_true")
    parser.add_argument("--scripted", action="store_true")
    parser.add_argument("--model-env")
    parser.add_argument("--model")
    parser.add_argument("--otlp-endpoint", default="")
    parser.add_argument("--alert-webhook", default="")
    parser.add_argument("--cost-alert-cny", type=float, default=(
        float(os.environ["WOA_COST_ALERT_CNY"])
        if os.environ.get("WOA_COST_ALERT_CNY") else None))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = OdooNativeConfig(
        rpc_base_url=args.rpc_base_url,
        public_origin=args.public_origin,
        public_agent_url=args.public_agent_url,
        delegation_dir=Path(args.delegation_dir),
        session_secret_path=Path(args.session_secret),
        state_dir=Path(args.state_dir),
        gateway_host=args.gateway_host,
        trusted_gateway_network=args.trusted_gateway_network,
        gateway_port=args.gateway_port,
        bootstrap_host=args.bootstrap_host,
        bootstrap_port=args.bootstrap_port,
        allow_private_http=args.allow_private_http,
        allow_mutations=args.allow_mutations,
        scripted=args.scripted,
        model_env=Path(args.model_env) if args.model_env else None,
        model=args.model,
        otlp_endpoint=args.otlp_endpoint,
        alert_webhook=args.alert_webhook,
        cost_alert_cny=args.cost_alert_cny)
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _number, _frame: stopped.set())
    with OdooNativeHost(config) as host:
        print(json.dumps({
            "status": "READY",
            "application": config.public_origin,
            "gateway_listener": host.server.base_url,
            "bootstrap_listener": host.bootstrap_server.base_url,
            "browser_uses_playwright": False,
        }, ensure_ascii=False, sort_keys=True), flush=True)
        stopped.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
