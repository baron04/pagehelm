"""The assistant host for a public yudao site: each signed-in person's session, read as them.

yudao keeps a person's sign-in as a bearer token in the browser, not a cookie. The page's
loader (`deploy/cloud/yudao-loader.js`) adds that token to the one request the runtime makes
to `/bootstrap`; this host asks yudao whose token it is, and opens (or renews) a session that
reads yudao with that same token, so the assistant sees exactly what the person may see. The
token is held in memory for the session only: it is not written to disk or logged, and a
renewal installs the page's newer one. Writes are not offered.

A person has one session, whichever pages they have it open on: one conversation, which any
of those pages may ask in, each turn running on the page that asked it. The session's
history and the conversation the model reads are kept on disk under the session's stable
identifier, so a session that was let go -- its sign-in lapsed while nobody was using it, or
the host restarted -- comes back as it was the next time the person asks.

The runtime and the session traffic go through the core's gateway; nginx publishes both the
gateway and this bootstrap endpoint on the site's own origin.

    python -m web_operation_agent_yudao.native_server --api-base http://server:48080 \\
        --public-origin https://yudao.rscl.cc --state-dir /var/lib/woa-yudao
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
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
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.execution.product import AgentTurnExecutor
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.modeling.client import ChatModel, SpendMeter
from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent.modeling.spend import DailyAllowance
from web_operation_agent.state.conversation import SQLiteConversationState
from web_operation_agent.state.identity import PrincipalError
from web_operation_agent.state.store import SQLiteSessionStore

from .application import YudaoApplication
from .client import YudaoClient
from .conventions import REFERENCE_CONVENTIONS
from .repository import repository_source
from .turn import yudao_turn_config
from .ui_source import UiSource

_MODEL_ENV_KEYS = ("AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_API_KEY", "AGENT_OPENAI_MODEL",
                   "AGENT_PROVIDER")
_MAX_BODY = 4096


def _api_docs(api_base: str, *, wait_s: float = 600, every_s: float = 5,
              timeout_s: float = 20) -> dict:
    """yudao's API documentation, waited for: the server may still be starting when this
    host does, and a host that exits on the first refusal only restarts into it again."""
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
    native_form_policy: Path | None = None
    native_create_policies: tuple[Path, ...] = ()
    native_filter_policies: tuple[Path, ...] = ()
    native_guard_port: int = 48093
    native_navigation: bool = False
    #: Explicit read-only page-planning pilot; never selected by browser messages or fallback.
    executor_mode: str = "goal"
    #: Reviewed menu/detail/tab handler effects; absent means this pilot stays disabled.
    native_action_effect_review: str = ""

    def __post_init__(self):
        if (not isinstance(self.native_action_effect_review, str)
                or len(self.native_action_effect_review) > 200
                or self.native_action_effect_review and not self.native_action_effect_review.strip()):
            raise ValueError("native action effects need a bounded deployment review")
        if self.native_action_effect_review and (self.executor_mode != "page-agent"
                or not self.native_navigation or self.native_read_policy is None or self.ui_source is None):
            raise ValueError("native action effects require page-agent, navigation, UI routes and a reviewed guard")
        if self.native_filter_policies and self.native_read_policy is None:
            raise ValueError("native page filters require a reviewed read guard")
        if not isinstance(self.native_navigation, bool) or self.native_navigation and self.native_read_policy is None:
            raise ValueError("native navigation requires a reviewed native read policy and write guard")
        if not isinstance(self.executor_mode, str) or self.executor_mode not in {"goal", "page-agent"}:
            raise ValueError("executor_mode must be goal or page-agent")
        if self.executor_mode == "page-agent" and self.native_read_policy is None:
            raise ValueError("page-agent requires a reviewed native read policy and write guard")


@dataclass
class _Active:
    session: object
    client: YudaoClient
    #: Renewals keep this delegation: a renewed principal may extend a session, never
    #: change whose authority it carries.
    delegation_id: str


class YudaoHost:
    def __init__(self, config: YudaoHostConfig):
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._api_documents = None
        self._documents_lock = threading.Lock()
        self.ui = UiSource.read(config.ui_source) if config.ui_source else None
        self.source = repository_source(config.source_root, config.source_release,
                                        commits=config.source_commits)
        self.native_proxy = None
        self.form_review = None
        self.form_tree = None
        self.create_reviews = ()
        self.filter_reviews = ()
        if config.native_form_policy is not None or config.native_create_policies:
            if config.native_read_policy is None or config.source_root is None:
                raise ValueError("native forms require the verified guard and reviewed raw source")
            from .repository import repository_tree
            from .form_entries import NativeFormReview, NativeCreateReview
            self.form_tree = repository_tree(config.source_root, config.source_release)
            if config.native_form_policy is not None:
                self.form_review = NativeFormReview.load(json.loads(config.native_form_policy.read_text()), self.form_tree)
            self.create_reviews = tuple(NativeCreateReview.load(json.loads(path.read_text()), self.form_tree)
                                        for path in config.native_create_policies)
            references = [review.reference for review in self.create_reviews]
            if len(references) != len(set(references)):
                raise ValueError("native create policies must have unique entry references")
        if config.native_read_policy is not None:
            from .guard_identity import GuardIdentity
            from .guard_proxy import NativeGuardProxy
            from .page_guard import NativePageGuard
            policy = json.loads(config.native_read_policy.read_text())
            if (not isinstance(policy, dict) or set(policy) != {"schema_version", "release", "read_paths"}
                    or policy["schema_version"] != 1 or not config.source_release
                    or policy["release"] != config.source_release or not self.ui or not self.ui.records
                    or not isinstance(policy["read_paths"], list)):
                raise ValueError("native read policy must match the deployment and its UI routes")
            if config.native_filter_policies:
                from .repository import repository_tree
                from .page_filters import NativePageFilterReview
                if config.source_root is None:
                    raise ValueError("native page filters require reviewed raw source")
                self.form_tree = self.form_tree or repository_tree(config.source_root, config.source_release)
                self.filter_reviews = tuple(NativePageFilterReview.load(json.loads(path.read_text()),
                    self.form_tree, policy["read_paths"]) for path in config.native_filter_policies)
                if len({r.route for r in self.filter_reviews}) != len(self.filter_reviews):
                    raise ValueError("native page filter routes must be unique")
            self.native_proxy = NativeGuardProxy(
                upstream=config.api_base, origin=config.public_origin,
                guard=NativePageGuard(frozenset(policy["read_paths"]),
                                      journal=config.state_dir / "native-guard.sqlite3"),
                resolve_actor=GuardIdentity(config.api_base), host=config.bootstrap_host,
                port=config.native_guard_port).start()
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

    # -- bootstrap ------------------------------------------------------------------------
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
        app = self._application(client)
        try:
            page = app.identity.page_identity(token)
        except PrincipalError as exc:
            raise BootstrapRefused(401, "sign_in_required") from exc
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

    def _application(self, client: YudaoClient) -> YudaoApplication:
        from .guard_proxy import GUARD_PATH
        forms = None
        if self.form_review is not None or self.create_reviews:
            from .form_entries import (NativeCustomerEntryVerifier, NativeCreateEntryVerifier,
                                       NativeEntryVerifiers, NativeEntryFormPort)
            verifiers = ([NativeCustomerEntryVerifier(self.form_tree, self.form_review, client)]
                         if self.form_review is not None else [])
            verifiers.extend(NativeCreateEntryVerifier(self.form_tree, review, client)
                             for review in self.create_reviews)
            forms = NativeEntryFormPort(NativeEntryVerifiers(*verifiers))
        app = YudaoApplication(self.config.public_origin, None, self._load_api_docs,
                                api_base_url=self.config.api_base, ui=self.ui,
                                conventions=REFERENCE_CONVENTIONS, client=client,
                                native_tabs=self.native_proxy is not None,
                                native_navigation=self.config.native_navigation,
                                forms=forms,
                                page_write_guard=GUARD_PATH if self.native_proxy is not None else "")
        if self.filter_reviews:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            from .page_filters import NativePageFilters
            app.goal_port.page_filters = NativePageFilters(app.goal_data, self.form_tree,
                self.filter_reviews, datetime.now(ZoneInfo(app.goal_data.time_zone())).date())
        return app

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
        # All of a person's tabs draw on one allowance, keyed by an opaque digest of who
        # they are, never their name.
        person = hashlib.sha256("\0".join(key).encode()).hexdigest()[:24]
        meter = SpendMeter(ledger=self.ledger, batch_id=f"yudao-native-{run_dir.name}",
                           allowance=self.allowance, allowance_key=person)
        chat = ChatModel(meter, env=dict(self.config.model_env), model=self.config.model,
                         max_completion_tokens=1024, timeout=self.config.turn_timeout_s,
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
                                           form_prefill=self.form_review is not None or bool(self.create_reviews),
                                           page_filtering=bool(self.filter_reviews),
                                           deterministic_reads=self.config.executor_mode == "page-agent")
            common = dict(channel=channel, chat=chat, application=bound, control=ReadOnlyControl(),
                run_id_factory=lambda _m: "yudao-" + uuid.uuid4().hex[:16],
                conversation_state=SQLiteConversationState(
                    self.config.state_dir / "conversation-state.sqlite3", session_id))
            if self.config.executor_mode == "page-agent":
                # The same Agent receives optional source evidence and reviewed form
                # entries. Source grants no actions; form review grants no submission.
                turn_config = replace(turn_config, require_page_route=True, require_task_contract=True,
                    defer_page_context=True,
                    data_reads=app.data_reads,
                    allow_application_entry=False, allow_page_facets=False,
                    disabled_actions=turn_config.disabled_actions | {"fill", "select", "submit", "back", "apply_facet"},
                    click_policy=app.native_tab_policy,
                    supported_scope=turn_config.supported_scope + " "
                        "Open pages from the current actor's catalog or exact record references, "
                        "then select enabled native record tabs from the fresh page. Runtime results "
                        "must verify every requested page state. Missing candidates are not proof "
                        "of absence; ask for clarification when ambiguous. If reviewed forms are "
                        "configured, find_form_entries discovers current openers; open_form_entry "
                        "uses one exact reviewed receipt, then describe/prepare/prefill and hand "
                        "over with submission left to the user. Arbitrary fill/select/click, "
                        "mutations and page filtering remain disabled.")
                if self.config.native_action_effect_review:
                    from .read_actions import NativeReadActions
                    from .list_actions import NativeListActions
                    policy = NativeReadActions(app.origin, app.goal_data, self.ui.records,
                        review=self.config.native_action_effect_review, form_entries=app.ports.forms,
                        list_actions=NativeListActions(self.form_tree, self.filter_reviews)
                            if self.filter_reviews else None)
                    scope = ("Open actor-catalog pages and exact actor-readable record details; "
                        "select native record tabs and verify fresh rendered state after each step. "
                        "Missing candidates are not proof of absence; clarify ambiguous targets. ")
                    if self.form_review is not None or self.create_reviews:
                        scope += ("For reviewed forms use find_form_entries, open_form_entry, "
                            "describe_form, prepare_form and prefill_form, then hand over the "
                            "highlighted submit control for manual submission. ")
                    if self.filter_reviews:
                        scope += ("Reviewed list query inputs, query buttons and pagination are "
                            "available only where the deployment qualifies their effects. ")
                    turn_config = replace(turn_config, click_policy=None, action_policy=policy,
                        supported_scope=yudao_turn_config(run_dir,
                            timeout_s=self.config.turn_timeout_s, source=self.source,
                            deterministic_reads=True).supported_scope + " " + scope +
                            "Other controls, arbitrary field entry and record mutations are unavailable.")
                executor = AgentTurnExecutor(**common, config=turn_config)
            else:
                executor = GoalTurnExecutor(**common, config=turn_config,
                    disabled_page_actions=turn_config.disabled_actions, repository_answers=True,
                    goal_port=app.goal_port)
            return LiveConversationRunner(channel, executor=executor)

        return self.server.create_session(
            allowed_origin=bound.descriptor.origin,
            page_controller_module=bound.descriptor.page_controller_module,
            runner_factory=runner_factory, session_id=session_id,
            navigation_mode=bound.descriptor.navigation_mode,
            page_write_guard=bound.descriptor.page_write_guard,
            sensitive_query_keys=bound.ports.routes.session_handoff_query_keys,
            principal=principal).start()

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
            except Exception:  # noqa: BLE001 -- no detail leaves the host
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
    ap.add_argument("--gateway-port", type=int, default=48091)
    ap.add_argument("--bootstrap-port", type=int, default=48092)
    ap.add_argument("--native-read-policy", type=Path)
    ap.add_argument("--native-form-policy", type=Path)
    ap.add_argument("--native-create-policy", type=Path, action="append", default=[])
    ap.add_argument("--native-filter-policy", type=Path, action="append", default=[])
    ap.add_argument("--native-guard-port", type=int, default=48093)
    ap.add_argument("--native-navigation", action="store_true")
    ap.add_argument("--native-action-effect-review", default="")
    ap.add_argument("--executor", choices=("goal", "page-agent"), default="goal")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-sessions", type=int, default=YudaoHostConfig.max_sessions)
    ap.add_argument("--daily-allowance-cny", type=float,
                    default=YudaoHostConfig.daily_allowance_cny)
    a = ap.parse_args(argv)
    commits = tuple(tuple(value.split("=", 1)) for value in a.source_commit)
    if any(len(item) != 2 for item in commits):
        ap.error("--source-commit requires REPOSITORY=FULL_COMMIT")
    env = {k: os.environ[k] for k in _MODEL_ENV_KEYS if os.environ.get(k)}
    if "AGENT_OPENAI_API_KEY" not in env:
        raise SystemExit("AGENT_OPENAI_API_KEY is not set")
    host = YudaoHost(YudaoHostConfig(
        api_base=a.api_base, public_origin=a.public_origin,
        public_agent_url=a.public_agent_url or a.public_origin.rstrip("/") + "/woa-agent",
        state_dir=a.state_dir, ui_source=a.ui_source, source_root=a.source_root,
        source_release=a.source_release, source_commits=commits, gateway_port=a.gateway_port,
        bootstrap_port=a.bootstrap_port, model=a.model, model_env=env,
        native_read_policy=a.native_read_policy, native_guard_port=a.native_guard_port,
        native_navigation=a.native_navigation,
        native_form_policy=a.native_form_policy, native_create_policies=tuple(a.native_create_policy),
        native_filter_policies=tuple(a.native_filter_policy),
        executor_mode=a.executor, native_action_effect_review=a.native_action_effect_review,
        max_sessions=a.max_sessions,
        daily_allowance_cny=a.daily_allowance_cny))
    host.server.start()
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
