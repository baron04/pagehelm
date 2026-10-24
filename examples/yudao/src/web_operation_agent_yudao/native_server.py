"""The assistant host for a public yudao site: each signed-in person's session, read as them.

yudao keeps a person's sign-in as a bearer token in the browser, not a cookie. The page's
loader (`deploy/cloud/yudao-loader.js`) adds that token to the one request the runtime makes
to `/bootstrap`; this host asks yudao whose token it is, and opens (or renews) a session that
reads yudao with that same token, so the assistant sees exactly what the person may see. The
token is held in memory for the session only: it is not written to disk or logged, and a
renewal installs the page's newer one. Writes are offered only where the deployment enables
them (`--native-writes`): reviewed typed operations, each after its own approval.

A person has one session, whichever pages they have it open on: one conversation, which any
of those pages may ask in, each turn running on the page that asked it. The session's
history and the conversation the model reads are kept on disk under the session's stable
identifier, so a session that was let go -- its sign-in lapsed while nobody was using it, or
the host restarted -- comes back as it was the next time the person asks.

The runtime and the session traffic go through the core's gateway; nginx publishes both the
gateway and this bootstrap endpoint on the site's own origin.

    python -m web_operation_agent_yudao.native_server --api-base http://server:48080 \\
        --public-origin https://yudao.rscl.cc --state-dir /var/lib/woa-yudao \\
        --source-release v2026.09 --native-read-policy /opt/native-policy/fence.json \\
        --native-page-policy /opt/native-policy/pages.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.live import (LiveConversationRunner, LiveProductCapacityError,
                                                  LiveProductServer)
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.modeling.client import ChatModel, SpendMeter
from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent.modeling.spend import DailyAllowance
from web_operation_agent.state.conversation import SQLiteConversationState
from web_operation_agent.state.identity import PrincipalError
from web_operation_agent.state.store import SQLiteSessionStore

from .application import YudaoApplication, YudaoIdentityPort
from .client import YudaoClient, YudaoReadRefused
from .conventions import REFERENCE_CONVENTIONS
from .repository import repository_source
from .turn import yudao_turn_config
from .ui_source import UiSource

_MODEL_ENV_KEYS = ("AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_API_KEY", "AGENT_OPENAI_MODEL",
                   "AGENT_PROVIDER")
_MAX_BODY = 4096


def _api_docs(api_base: str, *, wait_s: float = 600, every_s: float = 5,
              timeout_s: float = 120) -> dict:
    """yudao's API documentation, waited for: the server may still be starting when this
    host does, and a host that exits on the first refusal only restarts into it again.

    One read is given two minutes. A server that has just started builds the document on
    its first request, which was measured at 32 seconds on the deployment host; with the
    former 20 seconds the first turn after every restart failed."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            with urllib.request.urlopen(f"{api_base.rstrip('/')}/v3/api-docs/all", timeout=timeout_s) as r:
                return json.load(r)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(every_s)


class BootstrapRefused(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


@dataclass(frozen=True)
class YudaoHostConfig:
    api_base: str
    public_origin: str
    state_dir: Path
    #: Where the gateway is published on the site ("https://yudao.rscl.cc/woa-agent").
    public_agent_url: str = ""
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 48091
    bootstrap_host: str = "0.0.0.0"
    bootstrap_port: int = 48092
    ui_source: Path | None = None
    source_root: Path | None = None
    source_release: str = ""
    source_commits: tuple[tuple[str, str], ...] = ()
    #: One per signed-in person; a session is about a megabyte. What bounds use is model
    #: spend, which the daily allowance bounds per person.
    max_sessions: int = 300
    #: What one person may spend on the model per day, in CNY, counted on Beijing time.
    daily_allowance_cny: float = 5.0
    principal_lifetime_s: int = 600
    turn_timeout_s: int = 180
    model: str | None = None
    model_env: dict = field(default_factory=dict)
    native_read_policy: Path | None = None
    native_page_policy: Path | None = None
    #: Explicit deployment scope; it overrides generated menu and exact handler grants.
    native_page_disabled_handlers: frozenset[str] = frozenset()
    native_edit_prefill: bool = True
    native_money_policy: Path | None = None
    #: What each published query parameter's condition means, compiled from this release.
    #: Not the removed page-filtering `--native-filter-policy` of the declaration layer.
    native_filter_semantics: Path | None = None
    native_guard_port: int = 48093
    #: Internal address of the same routing front, when its public URL is host-only.
    native_ingress_base: str | None = None
    #: Only reviewed server-side typed mutations; browser submission stays disabled.
    native_writes: bool = False
    #: Whether a loaded Skill's text reaches the slim turn's model. Off only to measure
    #: what that text changes; the operations and every check on a write are the same.
    slim_skill_knowledge: bool = True
    #: Internal cold-start test switch; production CLI always warms actor metadata.
    warm_index: bool = True
    #: Days each turn's record of what the model was given and answered is kept beside
    #: the session's audit (`runs/<session>/turns/`); 0 writes none.
    turn_trace_days: int = 14
    #: User-facing source lookup and reading over the configured release. Off for the
    #: deployment (D3): implementation questions get the prompt's fixed reply, and the
    #: build-time scanners use the release tree through their own CLIs, not this switch.
    runtime_source: bool = False

    def __post_init__(self):
        from .model_page_policy import DEPLOYMENT_OPTIONAL_HANDLERS
        if (not isinstance(self.native_page_disabled_handlers, frozenset)
                or not self.native_page_disabled_handlers <= DEPLOYMENT_OPTIONAL_HANDLERS):
            raise ValueError("native_page_disabled_handlers must be an immutable optional-handler set")
        if type(self.native_edit_prefill) is not bool:
            raise ValueError("native_edit_prefill must be explicit")
        if not isinstance(self.native_writes, bool):
            raise ValueError("native writes must be explicitly enabled")
        if not isinstance(self.slim_skill_knowledge, bool):
            raise ValueError("slim_skill_knowledge is on or off")
        if not isinstance(self.warm_index, bool):
            raise ValueError("warm_index is on or off")
        if not isinstance(self.runtime_source, bool):
            raise ValueError("runtime_source is on or off")
        if (isinstance(self.turn_trace_days, bool) or not isinstance(self.turn_trace_days, int)
                or not 0 <= self.turn_trace_days <= 90):
            raise ValueError("turn_trace_days must be a whole number of days from 0 to 90")


@dataclass
class _Active:
    session: object
    client: YudaoClient
    #: Renewals keep this delegation: a renewed principal may extend a session, never
    #: change whose authority it carries.
    delegation_id: str


class YudaoHost:
    def __init__(self, config: YudaoHostConfig):
        from .deployment_validation import validate_native_deployment
        validated = validate_native_deployment(config)
        self.page_review = (replace(validated.page_review,
            disabled_handlers=config.native_page_disabled_handlers,
            allow_edit_prefill=config.native_edit_prefill) if validated.page_review is not None else None)
        self.money_review = validated.money_review
        self.filter_semantics = validated.filter_semantics
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._api_documents = None
        self._documents_lock = threading.Lock()
        self.ui = UiSource.read(config.ui_source) if config.ui_source else None
        self.source = repository_source(config.source_root, config.source_release,
                                        commits=config.source_commits,
                                        runtime=config.runtime_source)
        self.native_proxy = None
        self.live_forms = False
        self._fenced = frozenset()
        if config.native_read_policy is not None:
            from .guard_identity import GuardIdentity
            from .guard_proxy import NativeGuardProxy
            from .page_guard import NativePageGuard
            policy = validated.policy
            self.live_forms = validated.live_forms
            self._fenced = self._fenced_gets(policy)
            self.native_proxy = NativeGuardProxy(
                upstream=config.api_base, origin=config.public_origin,
                guard=NativePageGuard(frozenset(), writing_gets=self._fenced_gets(policy),
                                      journal=config.state_dir / "native-guard.sqlite3"),
                resolve_actor=GuardIdentity(config.api_base), host=config.bootstrap_host,
                port=config.native_guard_port, application_window=self.live_forms,
                ingress_base=config.native_ingress_base).start()
            try:
                self.native_proxy.verify_ingress()
            except Exception:
                self.native_proxy.close()
                raise
        self.store = SQLiteSessionStore(config.state_dir / "session-store.sqlite3")
        self.server = LiveProductServer(
            max_sessions=config.max_sessions, max_sessions_per_actor=1,
            host=config.gateway_host, port=config.gateway_port,
            public_base_url=config.public_agent_url or None,
            allow_private_network_bind=True, durable_store=self.store)
        #: (tenant, actor) -> that person's session.
        self._sessions: dict[tuple[str, str], _Active] = {}
        self.ledger = config.state_dir / "model-spend.jsonl"
        self.allowance = DailyAllowance(config.daily_allowance_cny, utc_offset_minutes=8 * 60)
        self.allowance.load(self.ledger)
        self._lock = threading.RLock()
        self._traces_pruned_at = 0.0
        self.prune_turn_traces()

    # -- bootstrap ------------------------------------------------------------------------
    def _fenced_gets(self, policy: dict) -> frozenset:
        """The scanned GET handlers the fence still refuses inside a window.

        Without the application's own window, all of them. With it, a handler the scan
        traced to a mapped database statement is let through: the application refuses
        that statement itself, and the request succeeds whenever it had nothing to
        write (a default row that already exists). A handler traced anywhere else, or
        not traced to its end, stays refused -- the window covers the database only.
        """
        listed = frozenset(policy["writing_gets"])
        if not self.live_forms:
            return listed
        reasons = policy["scan"].get("reasons") if isinstance(policy.get("scan"), dict) else None
        reasons = reasons if isinstance(reasons, dict) else {}
        return frozenset(path for path in listed
                         if not re.search(r"Mapper\.\w+$", str(reasons.get(path, ""))))

    def _warm_api_docs(self) -> None:
        try:
            with self._documents_lock:
                if self._api_documents is None:
                    documents = _api_docs(self.config.api_base)
                    if isinstance(documents, dict):
                        self._api_documents = documents
        except (OSError, ValueError):
            pass  # A turn reads it again and says so if it is still unavailable.

    def _load_api_docs(self) -> dict:
        from web_operation_agent.goal.data import DataRefused
        with self._documents_lock:
            if self._api_documents is None:
                try:
                    documents = _api_docs(self.config.api_base, wait_s=0)
                except (OSError, ValueError) as exc:
                    raise DataRefused("the application API documentation is unavailable",
                                      read_failed=True) from exc
                if not isinstance(documents, dict):
                    raise DataRefused("the application API documentation is invalid",
                                      read_failed=True)
                self._api_documents = documents
            return self._api_documents

    def bootstrap(self, token: str, tenant: str, instance: str) -> dict:
        # `instance` names the page asking; the session is the person's, whichever page.
        if not token or not tenant.isdigit() or not instance or len(instance) > 80:
            raise BootstrapRefused(401, "sign_in_required")
        client = YudaoClient.for_token(self.config.api_base, token, tenant)
        try:
            # Verify the browser token before assembling optional write tools: their
            # dictionary choices are read from yudao during application construction.
            page = YudaoIdentityPort(self.config.api_base, client).page_identity(token)
            app = self._application(client)
            key = (tenant, page.actor_id)
            with self._lock:
                active = self._sessions.get(key)
                if active is not None:
                    try:
                        active.session.channel.validate_live()
                    except Exception:  # noqa: BLE001 -- a session that ended is replaced
                        active.session.close("bootstrap_replaced")
                        self._sessions.pop(key, None)
                    else:
                        # The same person, the page's newer token: the session goes on reading
                        # as them.
                        active.client.renew(token)
                        principal = app.ports.identity.bind(
                            page, delegation_id=active.delegation_id,
                            lifetime_s=self.config.principal_lifetime_s)
                        try:
                            active.session.renew_principal(principal)
                        except Exception as exc:  # noqa: BLE001 -- mid-turn: renewed when it is done
                            if not str(getattr(exc, "code", "")).startswith("session_busy_"):
                                raise
                        return self._browser_bootstrap(active.session)
                principal = app.ports.identity.bind(
                    page, delegation_id="yudao-" + uuid.uuid4().hex,
                    lifetime_s=self.config.principal_lifetime_s)
                session = self._open(app, principal, key)
                self._sessions[key] = _Active(session=session, client=client,
                                              delegation_id=principal.delegation_id)
                return self._browser_bootstrap(session)
        except PrincipalError as exc:
            raise BootstrapRefused(401, "sign_in_required") from exc
        except YudaoReadRefused as exc:
            if exc.code == 401:
                raise BootstrapRefused(401, "sign_in_required") from exc
            if exc.code == 403:
                raise BootstrapRefused(403, "access_denied") from exc
            raise

    def _application(self, client: YudaoClient) -> YudaoApplication:
        from .guard_proxy import GUARD_PATH
        from web_operation_agent.business.mutation import SQLiteMutationJournal
        forms = None
        if self.live_forms:
            from .live_forms import FenceEvidence, LiveFormPort
            proxy = self.native_proxy
            forms = LiveFormPort(FenceEvidence(
                proxy.guard, lambda: proxy.resolve_actor(client._login(), client.tenant)))
        app = YudaoApplication(self.config.public_origin, None, self._load_api_docs,
                                api_base_url=self.config.api_base, ui=self.ui,
                                conventions=REFERENCE_CONVENTIONS, client=client,
                                native_tabs=self.native_proxy is not None,
                                native_navigation=False,
                                forms=forms,
                                page_write_guard=GUARD_PATH if self.native_proxy is not None else "",
                                native_writes=self.config.native_writes,
                                mutation_journal=SQLiteMutationJournal(self.config.state_dir / "mutations.sqlite3")
                                    if self.config.native_writes else None)
        return app

    def prune_turn_traces(self) -> None:
        """Delete turn records past their retention; at most once an hour."""
        if not self.config.turn_trace_days or time.time() - self._traces_pruned_at < 3600:
            return
        self._traces_pruned_at = time.time()
        from web_operation_agent.execution.turn_trace import prune_turn_traces
        prune_turn_traces(self.config.state_dir / "runs", self.config.turn_trace_days)

    def _open(self, app: YudaoApplication, principal, key):
        bound = bind_application(app)
        stable = hashlib.sha256("\0".join(key).encode()).hexdigest()[:40]
        session_id = "yudao-" + stable
        # A session let go without being closed here (its sign-in lapsed) is replaced by
        # the one it becomes again.
        self.server.close_session(session_id)
        run_dir = (self.config.state_dir / "runs"
                   / (time.strftime("%Y%m%d-%H%M%S") + "-" + stable[:8] + "-" + uuid.uuid4().hex[:6]))
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.prune_turn_traces()
        # All of a person's tabs draw on one allowance, keyed by an opaque digest of who
        # they are, never their name.
        person = hashlib.sha256("\0".join(key).encode()).hexdigest()[:24]
        meter = SpendMeter(ledger=self.ledger, batch_id=f"yudao-native-{run_dir.name}",
                           allowance=self.allowance, allowance_key=person)
        chat = ChatModel(meter, env=dict(self.config.model_env), model=self.config.model,
                         max_completion_tokens=8192, timeout=self.config.turn_timeout_s,
                         egress_policy=ProviderEgressPolicy(
                             session_handoff_keys=bound.ports.routes.session_handoff_query_keys))

        def runner_factory(channel):
            from .page_goals import NativePageGoalPort
            if isinstance(app.goal_port, NativePageGoalPort):
                from web_operation_agent.transport import RemoteProtocolError
                from web_operation_agent.goal.data import DataRefused
                def page_reader(context):
                    try:
                        return channel.page_for_read(context).observation
                    except (PrincipalError, RemoteProtocolError) as exc:
                        raise DataRefused("当前页面读取身份或租约已失效。", read_failed=True) from exc
                app.goal_port.page_reader = page_reader
            turn_config = yudao_turn_config(run_dir, timeout_s=self.config.turn_timeout_s,
                                           source=self.source, click_policy=app.native_tab_policy,
                                           form_prefill=self.live_forms,
                                           page_filtering=False,
                                           native_writes=self.config.native_writes)
            common = dict(channel=channel, chat=chat, application=bound, control=ReadOnlyControl(),
                run_id_factory=lambda _m: "yudao-" + uuid.uuid4().hex[:16],
                conversation_state=SQLiteConversationState(
                    self.config.state_dir / "conversation-state.sqlite3", session_id))
            # A measured prototype: the goal turn's protocol with a model-driven loop
            # deciding its steps (slim_loop.py). Every page command still runs under
            # the turn's leave, the write watch and the application's own window; with
            # native writes it may also propose a loaded Skill's typed operations.
            from web_operation_agent.goal.conventions import load_conventions
            from web_operation_agent.transport.panel_view import panel_limits
            from .slim_loop import MODEL_STEPS, ActorKnowledge, SlimTurnExecutor
            from .model_page_policy import NativeModelReadActions, NativeDraftSteps, ModelPageGoalPort
            model_goal = ModelPageGoalPort(app.goal_port, self.page_review)
            action_policy = NativeModelReadActions(app.origin, app.goal_data,
                self.ui.records if self.ui is not None else {}, model_goal, self.page_review)
            draft_steps = NativeDraftSteps(app.goal_data, app.ports.forms, self.page_review)
            mutation_prefill = None
            if self.config.native_writes and self.live_forms:
                from .mutation_prefill import YudaoMutationPrefill
                mutation_prefill = YudaoMutationPrefill(app.client, app.ports.forms, self.page_review)
            turn_config = replace(turn_config, action_policy=action_policy, click_policy=None,
                max_user_answer_chars=panel_limits()["result_max_chars"])
            executor = SlimTurnExecutor(
                **common, config=replace(turn_config, step_budget=MODEL_STEPS + 4),
                goal_port=model_goal, draft_steps=draft_steps,
                mutation_prefill=mutation_prefill,
                disabled_page_actions=frozenset({"submit", "back", "apply_facet"}),
                knowledge=ActorKnowledge(app.client, self._load_api_docs, self._fenced,
                                         time_zone=app.goal_data.time_zone(),
                                         codes=app.goal_data.code_names,
                                         columns=app.goal_data.column_names,
                                         query_choices=app.goal_data.query_choices,
                                         money_review=self.money_review,
                                         filter_semantics=self.filter_semantics),
                conventions=load_conventions(REFERENCE_CONVENTIONS),
                time_zone=app.goal_data.time_zone(),
                skill_knowledge=self.config.slim_skill_knowledge,
                turn_traces=self.config.turn_trace_days > 0)
            return LiveConversationRunner(channel, executor=executor)

        session = self.server.create_session(
            allowed_origin=bound.descriptor.origin,
            page_controller_module=bound.descriptor.page_controller_module,
            runner_factory=runner_factory, session_id=session_id,
            navigation_mode=bound.descriptor.navigation_mode,
            page_write_guard=bound.descriptor.page_write_guard,
            sensitive_query_keys=bound.ports.routes.session_handoff_query_keys,
            principal=principal).start()
        if self.config.warm_index:
            knowledge = getattr(getattr(session.runner, "executor", None), "knowledge", None)
            threading.Thread(target=self._warm_index, args=(app, knowledge), name="yudao-warm-index",
                             daemon=True).start()
        return session

    @staticmethod
    def _warm_index(app: YudaoApplication, knowledge) -> None:
        """Warm the actor's menu/API descriptions and outline, without line-table reads."""
        index = app.goal_port.outline_index
        for build in (getattr(knowledge, "_load", None), index):
            try:
                if build is not None:
                    build()
            except Exception:  # noqa: BLE001 -- see the docstring
                pass

    def _browser_bootstrap(self, session) -> dict:
        now_ms = int(time.time() * 1000)
        lifetime_ms = max(1, session.grant.expires_at - now_ms)
        lead_ms = min(300_000, max(1_000, lifetime_ms // 3))
        return {"schema_version": 1, "renew_at": max(now_ms + 1, session.grant.expires_at - lead_ms),
                "config": self.server.gateway.bootstrap_config(
                    session.grant, page_controller_module=session.page_controller_module,
                    navigation_mode=session.navigation_mode,
                    page_write_guard=session.page_write_guard,
                    sensitive_query_keys=session.sensitive_query_keys)}

    def forget_closed(self) -> None:
        with self._lock:
            for key, active in list(self._sessions.items()):
                try:
                    active.session.channel.validate_live()
                except Exception:  # noqa: BLE001 -- closed or expired
                    self._sessions.pop(key, None)

    def close(self) -> None:
        self.server.close()
        if self.native_proxy is not None:
            self.native_proxy.close()


def _handler(host: YudaoHost):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):  # requests carry tokens; nothing is logged
            pass

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                self._json(200, {"ok": True, "sessions": host.server.session_count})
            else:
                self._json(404, {"error": "not_found"})

        def do_POST(self):
            if self.path != "/bootstrap":
                self._json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > _MAX_BODY:
                    raise BootstrapRefused(413, "too_large")
                body = json.loads(self.rfile.read(length) or b"{}")
                auth = self.headers.get("Authorization") or ""
                token = auth[7:].strip() if auth.startswith("Bearer ") else ""
                payload = host.bootstrap(token, str(self.headers.get("tenant-id") or ""),
                                         str(body.get("runtime_instance_id") or ""))
                self._json(200, payload)
            except BootstrapRefused as exc:
                self._json(exc.status, {"error": exc.code})
            except LiveProductCapacityError:
                self._json(503, {"error": "assistant_busy"})
            except Exception as exc:  # noqa: BLE001 -- no detail leaves the host
                # The person is told only that the assistant is unavailable; the operator
                # needs to know which failure it was.
                print(f"yudao assistant: bootstrap failed: {type(exc).__name__}: {exc}"[:400],
                      file=sys.stderr, flush=True)
                self._json(503, {"error": "assistant_unavailable"})
    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api-base", required=True)
    ap.add_argument("--public-origin", required=True)
    ap.add_argument("--public-agent-url", default="")
    ap.add_argument("--state-dir", type=Path, required=True)
    ap.add_argument("--ui-source", type=Path)
    ap.add_argument("--source-root", type=Path)
    ap.add_argument("--source-release", default="")
    ap.add_argument("--source-commit", action="append", default=[], metavar="REPOSITORY=FULL_COMMIT",
                    help="repeat for ui/server repositories, or use root for a single Git repository")
    ap.add_argument("--runtime-source", action="store_true",
                    help="offer user-facing source lookup and reading over --source-root; off by "
                         "default (D3), and build-time scanners never read this flag")
    ap.add_argument("--gateway-port", type=int, default=48091)
    ap.add_argument("--bootstrap-port", type=int, default=48092)
    ap.add_argument("--native-read-policy", type=Path)
    ap.add_argument("--native-page-policy", type=Path,
                    help="release-reviewed model page handlers; required for slim")
    ap.add_argument("--native-page-disable", action="append", default=[],
                    choices=("query", "paging", "fields", "lines"),
                    help="repeat to disable an optional page handler before menu/exact grants; API reads remain available")
    ap.add_argument("--no-native-edit-prefill", action="store_false", dest="native_edit_prefill", default=True,
                    help="allow only forms with fresh native create-mode evidence to be prefilled; existing/unknown modes remain manual")
    ap.add_argument("--native-money-policy", type=Path,
                    help="optional release-bound fields verified against same-actor page values")
    ap.add_argument("--native-filter-semantics", type=Path,
                    help="optional release-bound query parameter semantics compiled from the server source")
    ap.add_argument("--native-guard-port", type=int, default=48093)
    ap.add_argument("--native-writes", action="store_true",
                    help="enable reviewed typed Skill writes with fresh approval and actor-bound readback; "
                         "does not enable browser submission")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-sessions", type=int, default=YudaoHostConfig.max_sessions)
    ap.add_argument("--daily-allowance-cny", type=float,
                    default=YudaoHostConfig.daily_allowance_cny)
    ap.add_argument("--turn-trace-days", type=int, default=YudaoHostConfig.turn_trace_days,
                    help="days to keep each turn's record of what the model was given and answered; 0 keeps none")
    a = ap.parse_args(argv)
    commits = tuple(tuple(value.split("=", 1)) for value in a.source_commit)
    if any(len(item) != 2 for item in commits):
        ap.error("--source-commit requires REPOSITORY=FULL_COMMIT")
    env = {k: os.environ[k] for k in _MODEL_ENV_KEYS if os.environ.get(k)}
    try:
        config = YudaoHostConfig(
            api_base=a.api_base, public_origin=a.public_origin,
            public_agent_url=a.public_agent_url or a.public_origin.rstrip("/") + "/woa-agent",
            state_dir=a.state_dir, ui_source=a.ui_source, source_root=a.source_root,
            source_release=a.source_release, source_commits=commits, gateway_port=a.gateway_port,
            bootstrap_port=a.bootstrap_port, model=a.model, model_env=env,
            native_read_policy=a.native_read_policy, native_page_policy=a.native_page_policy,
            native_page_disabled_handlers=frozenset(a.native_page_disable),
            native_edit_prefill=a.native_edit_prefill,
            native_money_policy=a.native_money_policy,
            native_filter_semantics=a.native_filter_semantics,
            native_guard_port=a.native_guard_port,
            native_writes=a.native_writes,
            max_sessions=a.max_sessions,
            daily_allowance_cny=a.daily_allowance_cny,
            turn_trace_days=a.turn_trace_days,
            runtime_source=a.runtime_source)
        from .deployment_validation import validate_native_deployment
        validate_native_deployment(config)
    except ValueError as exc:
        ap.error(str(exc))
    if "AGENT_OPENAI_API_KEY" not in env:
        raise SystemExit("AGENT_OPENAI_API_KEY is not set")
    # A model without a registered price cannot open a session: say so at start, once,
    # instead of answering every person with "unavailable".
    from web_operation_agent.modeling.spend import UnpricedModel, price_for
    try:
        price_for(a.model or env.get("AGENT_OPENAI_MODEL", ""))
    except UnpricedModel as exc:
        raise SystemExit(str(exc)) from exc
    host = YudaoHost(config)
    host.server.start()
    # Read the API documentation once in the background, so the first person to ask after
    # a start waits for a document already on its way instead of starting the slow read.
    threading.Thread(target=host._warm_api_docs, daemon=True).start()
    http = ThreadingHTTPServer((host.config.bootstrap_host, host.config.bootstrap_port), _handler(host))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    threading.Thread(target=http.serve_forever, daemon=True).start()
    print(f"yudao assistant: gateway {host.server.base_url}, bootstrap :{host.config.bootstrap_port}",
          flush=True)
    try:
        while not stop.wait(60):
            host.forget_closed()
    except KeyboardInterrupt:
        pass
    http.shutdown()
    host.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
