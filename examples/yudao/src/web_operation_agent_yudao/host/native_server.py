"""The assistant host for a public yudao site: each signed-in person's session, read as them.

yudao keeps a person's sign-in as a bearer token in the browser, not a cookie. The page's
loader (`deploy/cloud/yudao-loader.js`) adds that token to the one request the runtime makes
to `/bootstrap`; this host asks yudao whose token it is, and opens (or renews) a session that
reads yudao with that same token, so the assistant sees exactly what the person may see. The
token is held in memory for the session only: it is not written to disk or logged, and a
renewal installs the page's newer one. Writes are offered only where the deployment enables
them (`--native-writes`): reviewed typed operations, each after its own approval.

Each page a person asks from holds its own session and its own conversation, so two pages,
two browsers or two devices signed in as them work at the same time without taking each
other's turn. The session's history and the conversation the model reads are kept on disk
under the session's stable identifier, so a session that was let go -- its sign-in lapsed
while nobody was using it, or the host restarted -- comes back as it was the next time that
page asks.

The runtime and the session traffic go through the core's gateway; nginx publishes both the
gateway and this bootstrap endpoint on the site's own origin.

    python -m web_operation_agent_yudao.native_server --api-base http://server:48080 \\
        --public-origin https://yudao.rscl.cc --state-dir /var/lib/woa-yudao \\
        --source-release v2026.09 --native-read-policy /opt/native-policy/fence.json \\
        --native-page-policy /opt/native-policy/pages.json
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import signal
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.mutation import MutationJournalError, SQLiteMutationJournal
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.live import (LiveConversationRunner, LiveProductCapacityError,
                                                  LiveProductServer)
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.product import AgentTurnError, AgentTurnErrorCode, AgentTurnStatus
from web_operation_agent.modeling.client import ChatModel, ProviderConnectionPool, SpendMeter
from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent.modeling.provider import ProviderRuntimeGuard
from web_operation_agent.modeling.spend import DailyAllowance, SQLiteSpendCap
from web_operation_agent.state.conversation import SQLiteConversationState, prune_sqlite_conversations
from web_operation_agent.state.identity import PrincipalError
from web_operation_agent.state.store import SQLiteSessionStore

from .application import YudaoApplication, YudaoIdentityPort
from ..reads.client import YudaoClient, YudaoReadRefused
from ..conventions import REFERENCE_CONVENTIONS
from ..rules.repository import repository_source
from .turn import yudao_turn_config
from ..rules.ui_source import UiSource

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


def native_write_options(environment) -> dict:
    """Normal integration opt-in, shared by the local host and deployed CLI."""
    value = environment.get("WOA_NATIVE_WRITES", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("WOA_NATIVE_WRITES must be true or false")
    return {"native_writes": value == "true"}


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
    #: Live sessions one person may hold across pages, browsers and devices. Beyond it the
    #: least recently active idle one is let go; busy ones are never taken away.
    sessions_per_actor: int = 8
    #: What one person may spend on the model per day, in CNY, counted on Beijing time.
    daily_allowance_cny: float = 5.0
    principal_lifetime_s: int = 600
    turn_timeout_s: int = 120
    model: str | None = None
    model_env: dict = field(default_factory=dict)
    native_read_policy: Path | None = None
    native_page_policy: Path | None = None
    native_page_admission: str = "read-only-window"
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
    #: Integration opt-in: typed writes and approved native submission; ordinary submit stays disabled.
    native_writes: bool = False
    #: Offer existing preparation tools while refusing every mutation dispatch.
    native_write_preparation: bool = False
    #: A native form or row control pressed by the program after a fresh approval, under a
    #: permit that releases exactly the requests the approved card shows. Needs the
    #: read-only-window profile and its guard; typed Skill writes stay as they are.
    native_submit: bool = False
    #: Optional component-backed query/location batches; ordinary form boundaries remain.
    location_bindings: bool = False
    #: Whether a loaded Skill's text reaches the slim turn's model. Off only to measure
    #: what that text changes; the operations and every check on a write are the same.
    slim_skill_knowledge: bool = True
    #: Internal cold-start test switch; production CLI always warms actor metadata.
    warm_index: bool = True
    #: Local development UI debug trace flag; must stay False in production/cloud.
    local_dev_mode: bool = False
    #: Days each turn's record of what the model was given and answered is kept beside
    #: the session's audit (`runs/<session>/turns/`); 0 writes none.
    turn_trace_days: int = 14
    #: Process-wide concurrent provider calls bound across all sessions.
    max_concurrent_model_requests: int = 4
    #: Seconds to wait for in-flight write dispatch + readback during SIGTERM drain.
    shutdown_drain_timeout_s: float = 15.0
    #: Days before inactive durable session and conversation rows are pruned.
    session_retention_days: int = 30
    #: Size threshold (bytes) triggering rotation of an active `audit.jsonl`.
    audit_max_bytes: int = 10 * 1024 * 1024
    #: Days rotated audit segments are kept before pruning non-retained rows.
    audit_retention_days: int = 90
    #: Maximum rotated audit segments retained per audit log directory.
    audit_max_segments: int = 10
    #: Optional process-wide daily and monthly reference spend caps (CNY). None disables.
    daily_total_cap_cny: float | None = None
    monthly_total_cap_cny: float | None = None
    #: User-facing source lookup and reading over the configured release. Off for the
    #: deployment (D3): implementation questions get the prompt's fixed reply, and the
    #: build-time scanners use the release tree through their own CLIs, not this switch.
    runtime_source: bool = False

    def __post_init__(self):
        from ..executor.model_page_policy import DEPLOYMENT_OPTIONAL_HANDLERS
        if self.native_page_admission not in ("reviewed", "method-fence", "read-only-window"):
            raise ValueError("unknown native_page_admission profile")
        if (not isinstance(self.native_page_disabled_handlers, frozenset)
                or not self.native_page_disabled_handlers <= DEPLOYMENT_OPTIONAL_HANDLERS):
            raise ValueError("native_page_disabled_handlers must be an immutable optional-handler set")
        if type(self.native_edit_prefill) is not bool:
            raise ValueError("native_edit_prefill must be explicit")
        if not isinstance(self.native_writes, bool):
            raise ValueError("native writes must be explicitly enabled")
        if type(self.native_write_preparation) is not bool:
            raise ValueError("native_write_preparation must be explicit")
        if self.native_write_preparation and (self.native_writes or self.location_bindings):
            raise ValueError("preparation requires disabled native writes and bindings")
        if type(self.location_bindings) is not bool:
            raise ValueError("location_bindings must be explicit")
        if self.location_bindings and self.native_writes:
            raise ValueError("location bindings do not enable business writes")
        if type(self.native_submit) is not bool:
            raise ValueError("native_submit must be explicit")
        if self.native_writes and self.native_page_admission == "read-only-window":
            object.__setattr__(self, "native_submit", True)
            object.__setattr__(self, "native_edit_prefill", True)
        if self.native_submit and not (self.native_writes and self.native_page_admission == "read-only-window"
                                       and self.native_read_policy is not None):
            raise ValueError("native submit needs native writes under the read-only-window profile and its guard")
        if (self.native_page_admission == "read-only-window" and self.native_writes
                and not self.native_submit):
            raise ValueError("read-only-window does not permit business writes")
        if not isinstance(self.slim_skill_knowledge, bool):
            raise ValueError("slim_skill_knowledge is on or off")
        if not isinstance(self.warm_index, bool):
            raise ValueError("warm_index is on or off")
        if not isinstance(self.runtime_source, bool):
            raise ValueError("runtime_source is on or off")
        if type(self.local_dev_mode) is not bool:
            raise ValueError("local_dev_mode must be explicit")
        if (isinstance(self.turn_trace_days, bool) or not isinstance(self.turn_trace_days, int)
                or not 0 <= self.turn_trace_days <= 90):
            raise ValueError("turn_trace_days must be a whole number of days from 0 to 90")
        if (isinstance(self.sessions_per_actor, bool)
                or not isinstance(self.sessions_per_actor, int)
                or not 1 <= self.sessions_per_actor <= 64):
            raise ValueError("sessions_per_actor must be a whole number from 1 to 64")
        if self.sessions_per_actor > self.max_sessions:
            raise ValueError("sessions_per_actor cannot exceed max_sessions")
        if (isinstance(self.max_concurrent_model_requests, bool)
                or not isinstance(self.max_concurrent_model_requests, int)
                or not 1 <= self.max_concurrent_model_requests <= 1000):
            raise ValueError("max_concurrent_model_requests must be an integer from 1 to 1000")
        if (isinstance(self.shutdown_drain_timeout_s, bool)
                or not isinstance(self.shutdown_drain_timeout_s, (int, float))
                or not math.isfinite(self.shutdown_drain_timeout_s)
                or not 0 <= self.shutdown_drain_timeout_s <= 300):
            raise ValueError("shutdown_drain_timeout_s must be a finite number from 0 to 300")
        if (isinstance(self.session_retention_days, bool)
                or not isinstance(self.session_retention_days, int)
                or not 1 <= self.session_retention_days <= 365):
            raise ValueError("session_retention_days must be an integer from 1 to 365")
        if (isinstance(self.audit_max_bytes, bool)
                or not isinstance(self.audit_max_bytes, int)
                or not 1024 <= self.audit_max_bytes <= 1024 * 1024 * 1024):
            raise ValueError("audit_max_bytes must be an integer from 1024 to 1GiB")
        if (isinstance(self.audit_retention_days, bool)
                or not isinstance(self.audit_retention_days, int)
                or not 1 <= self.audit_retention_days <= 3650):
            raise ValueError("audit_retention_days must be an integer from 1 to 3650")
        if (isinstance(self.audit_max_segments, bool)
                or not isinstance(self.audit_max_segments, int)
                or not 1 <= self.audit_max_segments <= 100):
            raise ValueError("audit_max_segments must be an integer from 1 to 100")
        for cap_name in ("daily_total_cap_cny", "monthly_total_cap_cny"):
            cap = getattr(self, cap_name)
            if cap is not None and (
                    isinstance(cap, bool) or not isinstance(cap, (int, float))
                    or not math.isfinite(cap) or cap <= 0):
                raise ValueError(f"{cap_name} must be a positive finite number or None")


@dataclass
class _Active:
    session: object
    client: YudaoClient
    #: Renewals keep this delegation: a renewed principal may extend a session, never
    #: change whose authority it carries.
    delegation_id: str


class _PageWindow:
    """Resolves the guard window a session's work belongs to at call time.

    A turn's page-load passes, read evidence, refusals and submit permits are the
    ones the page holding the turn names as its runtime instance. Before the
    session's channel exists -- or once no page's lease is active -- the window is
    the page that opened the session.
    """

    def __init__(self, instance: str):
        self.instance = instance
        self.channel = None

    def __call__(self) -> str:
        channel = self.channel
        active = channel.active_instance if channel is not None else ""
        return active or self.instance


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
        self.rule_source = None
        if config.source_root and config.source_release and (Path(config.source_root) / "server").is_dir():
            from ..rules.source import YudaoSource
            self.rule_source = YudaoSource(config.source_root, release=config.source_release)
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
                guard=NativePageGuard(frozenset(), writing_gets=self._fenced,
                                      journal=config.state_dir / "native-guard.sqlite3"),
                resolve_actor=GuardIdentity(config.api_base), host=config.bootstrap_host,
                port=config.native_guard_port, application_window=self.live_forms,
                ingress_base=config.native_ingress_base).start()
            try:
                self.native_proxy.verify_ingress()
            except Exception:
                self.native_proxy.close()
                raise
        self.runtime_guard = ProviderRuntimeGuard(
            max_concurrent_requests=config.max_concurrent_model_requests)
        self._model_connection_pool = ProviderConnectionPool(
            max_idle=max(4, config.max_concurrent_model_requests))
        self.store = SQLiteSessionStore(config.state_dir / "session-store.sqlite3")
        self.server = LiveProductServer(
            max_sessions=config.max_sessions, max_sessions_per_actor=config.sessions_per_actor,
            host=config.gateway_host, port=config.gateway_port,
            public_base_url=config.public_agent_url or None,
            allow_private_network_bind=True, durable_store=self.store,
            provider_status=self.runtime_guard.status)
        #: (tenant, actor, page instance) -> that page's session.
        self._sessions: dict[tuple[str, str, str], _Active] = {}
        self.ledger = config.state_dir / "model-spend.jsonl"
        self.allowance = DailyAllowance(config.daily_allowance_cny, utc_offset_minutes=8 * 60)
        self.allowance.load(self.ledger)
        self.spend_cap: SQLiteSpendCap | None = None
        if config.daily_total_cap_cny is not None or config.monthly_total_cap_cny is not None:
            self.spend_cap = SQLiteSpendCap(
                config.state_dir / "spend-caps.sqlite3",
                daily_cap_cny=config.daily_total_cap_cny,
                monthly_cap_cny=config.monthly_total_cap_cny,
                utc_offset_minutes=8 * 60,
            )
            self.spend_cap.sync_from_ledger(self.ledger)
        self._mutation_journal = (
            SQLiteMutationJournal(config.state_dir / "mutations.sqlite3")
            if (config.native_writes or config.native_write_preparation) else None
        )
        self._lock = threading.RLock()
        self._writes_cond = threading.Condition(self._lock)
        self._draining = False
        self._inflight_writes = 0
        self._traces_pruned_at = 0.0
        self.prune_turn_traces()
        if config.warm_index and self.rule_source is not None:
            threading.Thread(target=self._warm_sources, name="yudao-warm-sources",
                             daemon=True).start()

    def _warm_sources(self) -> None:
        try:
            if self.rule_source is not None:
                self.rule_source.warm()
        except Exception:  # noqa: BLE001 -- live turns report source unavailability directly
            pass

    # -- bootstrap ------------------------------------------------------------------------
    def _fenced_gets(self, policy: dict) -> frozenset:
        """The scanned GET handlers the fence still refuses inside a window.

        Method-fence action admission retains all scanned writing handlers. Reviewed
        admission without the application's own window does too. With that window, a handler the scan
        traced to a mapped database statement is let through: the application refuses
        that statement itself, and the request succeeds whenever it had nothing to
        write (a default row that already exists). A handler traced anywhere else, or
        not traced to its end, stays refused -- the window covers the database only.
        """
        listed = frozenset(policy["writing_gets"])
        if self.config.native_page_admission in {"method-fence", "read-only-window"} or not self.live_forms:
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
        from .page_guard import WINDOW_KEY
        if self.draining:
            raise BootstrapRefused(503, "server_draining")
        # `instance` names the page asking: each page keeps its own conversation, so two
        # pages -- or two browsers, or two devices -- of one person never take each other's.
        # The name is also the page's guard window, so it must fit the window charset here
        # rather than fail every guarded request it later makes.
        if (not token or not tenant.isdigit() or not isinstance(instance, str)
                or len(instance) > 80 or not WINDOW_KEY.fullmatch(instance)):
            raise BootstrapRefused(401, "sign_in_required")
        client = YudaoClient.for_token(self.config.api_base, token, tenant)
        try:
            # Verify the browser token before assembling optional write tools: their
            # dictionary choices are read from yudao during application construction.
            page = YudaoIdentityPort(self.config.api_base, client).page_identity(token)
            key = (tenant, page.actor_id, instance)
            page_window = _PageWindow(instance)
            app = self._application(client, window=page_window)
            with self._lock:
                if self._draining:
                    raise BootstrapRefused(503, "server_draining")
                active = self._sessions.get(key)
                if active is not None:
                    try:
                        active.session.channel.validate_live()
                    except Exception:  # noqa: BLE001 -- a session that ended is replaced
                        active.session.close("bootstrap_replaced")
                        self._sessions.pop(key, None)
                    else:
                        # The same person, the page's newer token: the session goes on reading
                        # as them, under this page's own lease.
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
                session = self._open(app, principal, key, page_window)
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

    def _application(self, client: YudaoClient, *, window: str | _PageWindow = "") -> YudaoApplication:
        from .guard_proxy import GUARD_PATH
        forms = None
        if self.live_forms:
            from ..writes.live_forms import FenceEvidence, LiveFormPort
            proxy = self.native_proxy
            forms = LiveFormPort(FenceEvidence(
                proxy.guard, lambda: proxy.resolve_actor(client._login(), client.tenant),
                window))
        app = YudaoApplication(self.config.public_origin, None, self._load_api_docs,
                                api_base_url=self.config.api_base, ui=self.ui,
                                source=self.rule_source,
                                conventions=REFERENCE_CONVENTIONS, client=client,
                                native_tabs=self.native_proxy is not None,
                                native_navigation=False,
                                forms=forms,
                                page_write_guard=GUARD_PATH if self.native_proxy is not None else "",
                                native_writes=self.config.native_writes,
                                native_write_preparation=self.config.native_write_preparation,
                                writing_gets=self._fenced,
                                mutation_journal=self._mutation_journal,
                                write_tracker=self.track_inflight_write)
        return app

    @property
    def draining(self) -> bool:
        with self._lock:
            return self._draining

    @property
    def accepting_sessions(self) -> bool:
        with self._lock:
            return not self._draining

    @property
    def inflight_writes(self) -> int:
        with self._lock:
            return self._inflight_writes

    @contextmanager
    def track_inflight_write(self):
        with self._lock:
            self._inflight_writes += 1
        try:
            yield
        finally:
            with self._lock:
                self._inflight_writes -= 1
                self._writes_cond.notify_all()

    def begin_drain(self) -> None:
        with self._lock:
            self._draining = True
        server_drain = getattr(self.server, "begin_drain", None)
        if callable(server_drain):
            server_drain()

    def drain(self, timeout_s: float | None = None) -> dict:
        """Stop accepting new sessions/turns and wait boundedly for in-flight writes."""
        self.begin_drain()
        effective = (
            float(self.config.shutdown_drain_timeout_s)
            if timeout_s is None else max(0.0, float(timeout_s))
        )
        deadline = time.monotonic() + effective
        with self._writes_cond:
            while self._inflight_writes > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._writes_cond.wait(timeout=remaining)
            remaining_writes = self._inflight_writes
        marked = 0
        if (remaining_writes > 0 or effective == 0.0) and self._mutation_journal is not None:
            for intent in self._mutation_journal.unresolved():
                if intent.state != "dispatching":
                    continue
                try:
                    self._mutation_journal.finish(
                        intent.proposal_digest,
                        "indeterminate",
                        reconciliation={"phase": "shutdown_drain_timeout"},
                    )
                    marked += 1
                except MutationJournalError:
                    continue
                self._append_host_audit({
                    "t": time.time(),
                    "event": "business_mutation_indeterminate",
                    "proposal_digest": intent.proposal_digest,
                    "reason": "shutdown_drain_timeout",
                    "dispatches": 0,
                    "side_effect_possible": True,
                })
        return {
            "drained": remaining_writes == 0 and marked == 0,
            "remaining_writes": remaining_writes,
            "marked_indeterminate": marked,
        }

    def _append_host_audit(self, record: dict) -> None:
        audit_path = self.config.state_dir / "audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def prune_turn_traces(self) -> None:
        """Run periodic host cleanup (traces, sessions, conversations, audit rotation)."""
        self.cleanup()

    def cleanup(self, *, force: bool = False, now_ms: int | None = None) -> dict:
        """Prune expired sessions/conversations/traces and rotate audit logs safely."""
        now_s = time.time() if now_ms is None else now_ms / 1000.0
        current_ms = int(now_s * 1000) if now_ms is None else now_ms
        if not force and now_s - self._traces_pruned_at < 3600:
            return {"skipped": True}
        self._traces_pruned_at = now_s
        traces_removed = 0
        if self.config.turn_trace_days:
            from web_operation_agent.execution.turn_trace import prune_turn_traces
            traces_removed = prune_turn_traces(
                self.config.state_dir / "runs", self.config.turn_trace_days, now=now_s)
        cutoff_ms = current_ms - self.config.session_retention_days * 86_400_000
        sessions_removed = self.store.prune(older_than_ms=cutoff_ms)
        conversations_removed = prune_sqlite_conversations(
            self.config.state_dir / "conversation-state.sqlite3",
            older_than_ms=cutoff_ms,
            now_ms=current_ms,
        )
        journal_swept = (
            self._mutation_journal.sweep(now_ms=current_ms)
            if self._mutation_journal is not None else {}
        )
        unresolved_digests = (
            frozenset(item.proposal_digest for item in self._mutation_journal.unresolved())
            if self._mutation_journal is not None else frozenset()
        )
        audit_targets = [self.config.state_dir / "audit.jsonl"]
        runs_dir = self.config.state_dir / "runs"
        if runs_dir.is_dir():
            audit_targets.extend(sorted(runs_dir.glob("*/audit.jsonl")))
        rotated = 0
        pruned_segments = 0
        preserved_rows = 0
        for audit_path in audit_targets:
            r, p, k = _rotate_and_prune_audit(
                audit_path,
                max_bytes=self.config.audit_max_bytes,
                max_segments=self.config.audit_max_segments,
                retention_days=self.config.audit_retention_days,
                unresolved_digests=unresolved_digests,
                now_s=now_s,
            )
            rotated += r
            pruned_segments += p
            preserved_rows += k
        return {
            "skipped": False,
            "traces_removed": traces_removed,
            "sessions_removed": sessions_removed,
            "conversations_removed": conversations_removed,
            "journal_swept": journal_swept,
            "audit_rotated": rotated,
            "audit_segments_pruned": pruned_segments,
            "audit_preserved_rows": preserved_rows,
        }

    def _open(self, app: YudaoApplication, principal, key, page_window: _PageWindow):
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
        # All of a person's pages draw on one allowance, keyed by an opaque digest of who
        # they are, never their name.
        person = hashlib.sha256("\0".join(key[:2]).encode()).hexdigest()[:24]
        meter = SpendMeter(ledger=self.ledger, batch_id=f"yudao-native-{run_dir.name}",
                           allowance=self.allowance, allowance_key=person,
                           global_cap=self.spend_cap,
                           run_input_token_abort=16_000_000)
        chat = ChatModel(meter, env=dict(self.config.model_env), model=self.config.model,
                         max_completion_tokens=8192, timeout=self.config.turn_timeout_s,
                         runtime_guard=self.runtime_guard,
                         connection_pool=self._model_connection_pool,
                         egress_policy=ProviderEgressPolicy(
                             session_handoff_keys=bound.ports.routes.session_handoff_query_keys))

        def runner_factory(channel):
            channel.local_dev_mode = self.config.local_dev_mode
            # The window every page this session is opened on works through is the one
            # the turn's own page carries; resolve it against the channel once it exists.
            page_window.channel = channel
            from ..goal_compat.page_goals import NativePageGoalPort
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
                                           native_writes=self.config.native_writes or self.config.native_write_preparation)
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
            from ..executor.slim_loop import MODEL_STEPS, ActorKnowledge, YudaoTurnExecutor
            from ..executor.model_page_policy import NativeModelReadActions, NativeDraftSteps, ModelPageGoalPort
            page_review, page_actions = self.page_review, NativeModelReadActions
            action_options = {}
            if self.config.native_page_admission == "method-fence":
                from ..executor.method_fenced_actions import MethodFencedPageActions, MethodFencedPageReview
                page_review, page_actions = MethodFencedPageReview.from_review(page_review), MethodFencedPageActions
            elif self.config.native_page_admission == "read-only-window":
                from ..executor.read_only_window_actions import ReadOnlyWindowPageActions, ReadOnlyWindowPageReview
                page_review = ReadOnlyWindowPageReview.from_review(page_review)
                page_actions = ReadOnlyWindowPageActions
                action_options["guard_proxy"] = self.native_proxy
                action_options["window"] = page_window
            model_goal = ModelPageGoalPort(app.goal_port, page_review)
            action_policy = page_actions(app.origin, app.goal_data,
                self.ui.records if self.ui is not None else {}, model_goal, page_review, **action_options)
            draft_policy = NativeDraftSteps
            if self.config.native_page_admission == "read-only-window":
                from ..executor.read_only_window_actions import ReadOnlyWindowDraftSteps
                draft_policy = ReadOnlyWindowDraftSteps
            draft_steps = draft_policy(app.goal_data, app.ports.forms, page_review)
            mutation_prefill = None
            if (self.config.native_writes or self.config.native_write_preparation) and self.live_forms:
                from ..writes.mutation_prefill import YudaoMutationPrefill
                mutation_prefill = YudaoMutationPrefill(app.client, app.ports.forms, self.page_review)
            from ..writes.native_submit import YudaoSubmitPort
            submit_port = YudaoSubmitPort(app.client, self.native_proxy, self._load_api_docs,
                                          window=page_window, write_tracker=self.track_inflight_write)
            native_submit = submit_port if self.config.native_submit else None
            turn_config = replace(turn_config, action_policy=action_policy, click_policy=None,
                max_user_answer_chars=panel_limits()["result_max_chars"])
            executor = YudaoTurnExecutor(
                **common, config=replace(turn_config, step_budget=MODEL_STEPS),
                goal_port=model_goal, draft_steps=draft_steps,
                mutation_prefill=mutation_prefill,
                location_bindings=self.config.location_bindings,
                native_submit=native_submit, page_reads=submit_port.recent_reads,
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
            orig_execute = executor.execute

            def guarded_execute(message):
                if self.draining:
                    raise AgentTurnError(
                        AgentTurnErrorCode.RUNTIME_UNAVAILABLE,
                        "服务正在优雅停机，暂不接受新回合。",
                        retryable=True,
                    )
                if self.spend_cap is None:
                    return orig_execute(message)
                turn_id = str((message or {}).get("message_id") or "")
                res_id = f"{session_id}:{turn_id or uuid.uuid4().hex}"
                reservation = self.spend_cap.reserve(res_id)
                if not reservation.allowed:
                    notice = (
                        "今日平台参考费用已达上限，暂不接受新任务，请明日再试。"
                        if reservation.reason == "daily_cap_exceeded"
                        else "本月平台参考费用已达上限，暂不接受新任务。"
                    )
                    channel.publish(
                        "progress",
                        {
                            "state": "task_failed",
                            "message": notice,
                            "error_code": reservation.reason,
                            "retryable": False,
                        },
                        turn_id=turn_id,
                    )
                    return SimpleNamespace(
                        terminal_reason=reservation.reason,
                        user_answer=notice,
                        status=AgentTurnStatus.REFUSED,
                        message=SimpleNamespace(message_id=turn_id),
                    )
                try:
                    return orig_execute(message)
                finally:
                    self.spend_cap.release(res_id)

            executor.execute = guarded_execute
            return LiveConversationRunner(channel, executor=executor)

        session = self.server.create_session(
            allowed_origin=bound.descriptor.origin,
            page_controller_module=bound.descriptor.page_controller_module + (
                "?bindings=1" if self.config.location_bindings else ""),
            runner_factory=runner_factory, session_id=session_id,
            navigation_mode=bound.descriptor.navigation_mode,
            page_write_guard=bound.descriptor.page_write_guard,
            sensitive_query_keys=bound.ports.routes.session_handoff_query_keys,
            principal=principal).start()
        session.channel.read_only_mode = not self.config.native_writes
        if self.config.warm_index:
            knowledge = getattr(getattr(session.runner, "executor", None), "knowledge", None)
            threading.Thread(target=self._warm_index, args=(app, knowledge), name="yudao-warm-index",
                             daemon=True).start()
        return session

    @staticmethod
    def _warm_index(app: YudaoApplication, knowledge) -> None:
        """Warm the actor's menu/API descriptions and outline, without line-table reads."""
        fenced = (
            frozenset(getattr(getattr(app, "goal_data", None), "_writing_gets", ()))
            | frozenset(getattr(knowledge, "writing_gets", ()))
        )
        client = getattr(app, "client", None) or getattr(knowledge, "client", None)
        guard = (
            client.fenced_read_only(fenced)
            if hasattr(client, "fenced_read_only")
            else contextlib.nullcontext()
        )
        index = getattr(getattr(app, "goal_port", None), "outline_index", None)
        with guard:
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
        self._model_connection_pool.close_all()
        self.server.close()
        if self.native_proxy is not None:
            self.native_proxy.close()


_PRESERVED_AUDIT_EVENTS = frozenset({
    "business_mutation_indeterminate",
    "run_started",
    "session_started",
    "policy_bound",
})
_FINGERPRINT_KEYS = (
    "policy_fingerprint",
    "catalog_sha256",
    "policy_digest",
    "source_digest",
    "skill_digest",
)


def _is_preserved_audit_line(raw_line: str, unresolved_digests: frozenset[str]) -> bool:
    line = raw_line.strip()
    if not line:
        return False
    try:
        row = json.loads(line)
    except ValueError:
        return False
    if not isinstance(row, dict):
        return False
    if row.get("proposal_digest") in unresolved_digests:
        return True
    if row.get("event") in _PRESERVED_AUDIT_EVENTS:
        return True
    return any(bool(row.get(key)) for key in _FINGERPRINT_KEYS)


def _rotate_and_prune_audit(
    audit_path: Path,
    *,
    max_bytes: int,
    max_segments: int,
    retention_days: int,
    unresolved_digests: frozenset[str],
    now_s: float,
) -> tuple[int, int, int]:
    rotated = 0
    pruned = 0
    preserved = 0
    parent = audit_path.parent
    if not parent.is_dir():
        return 0, 0, 0
    if audit_path.is_file():
        try:
            size = audit_path.stat().st_size
        except OSError:
            size = 0
        if size >= max_bytes:
            stamp = int(now_s * 1000)
            seq = 0
            while True:
                candidate = parent / f"audit.{stamp}-{seq}.jsonl"
                if not candidate.exists():
                    break
                seq += 1
            os.replace(audit_path, candidate)
            rotated = 1

    segments = sorted(
        p for p in parent.glob("audit.*.jsonl")
        if p.name != "audit.retained.jsonl" and p.is_file()
    )
    cutoff_s = now_s - retention_days * 86400
    keep_recent = set(segments[-max_segments:]) if max_segments > 0 else set()
    retained_path = parent / "audit.retained.jsonl"
    seen_hashes: set[str] = set()
    if retained_path.is_file():
        for existing in retained_path.read_text(encoding="utf-8").splitlines():
            if existing.strip():
                seen_hashes.add(hashlib.sha256(existing.strip().encode("utf-8")).hexdigest())

    for segment in segments:
        try:
            mtime = segment.stat().st_mtime
        except OSError:
            continue
        if segment in keep_recent and mtime >= cutoff_s:
            continue
        to_retain: list[str] = []
        for raw_line in segment.read_text(encoding="utf-8").splitlines():
            if _is_preserved_audit_line(raw_line, unresolved_digests):
                stripped = raw_line.strip()
                digest = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
                if digest not in seen_hashes:
                    seen_hashes.add(digest)
                    to_retain.append(stripped)
        if to_retain:
            fd = os.open(retained_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                for item in to_retain:
                    fh.write(item + "\n")
            preserved += len(to_retain)
        segment.unlink(missing_ok=True)
        pruned += 1
    return rotated, pruned, preserved


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
                self._json(200, {
                    "ok": True,
                    "draining": host.draining,
                    "sessions": host.server.session_count,
                })
            elif self.path == "/readyz":
                draining = host.draining
                self._json(503 if draining else 200, {
                    "ready": not draining,
                    "accepting_sessions": not draining,
                    "draining": draining,
                    "sessions": host.server.session_count,
                })
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
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    raise BootstrapRefused(400, "invalid_request")
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
    ap.add_argument("--native-page-admission", choices=("reviewed", "method-fence", "read-only-window"), default="read-only-window",
                    help="allow unregistered ordinary page controls under the method fence; "
                         "keeps target binding and refuses write controls")
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
    ap.add_argument("--location-bindings", action="store_true",
                    help="enable component-backed location batches, excluding business form entry")
    ap.add_argument("--native-write-preparation", action="store_true",
                    help="offer existing preparation tools; mutation dispatch remains disabled")
    ap.add_argument("--native-submit", action="store_true",
                    help="after a fresh approval press the native form or row control once under a permit that "
                         "releases only the card's requests; requires --native-writes and read-only-window")
    ap.add_argument("--native-writes", action="store_true", default=native_write_options(os.environ)["native_writes"],
                    help="enable reviewed typed Skill writes and approved native submission with actor-bound readback; "
                         "ordinary browser submission remains disabled")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-sessions", type=int, default=YudaoHostConfig.max_sessions)
    ap.add_argument("--daily-allowance-cny", type=float,
                    default=YudaoHostConfig.daily_allowance_cny)
    ap.add_argument("--daily-total-cap-cny", type=float, default=None)
    ap.add_argument("--monthly-total-cap-cny", type=float, default=None)
    ap.add_argument("--shutdown-drain-timeout-s", type=float,
                    default=YudaoHostConfig.shutdown_drain_timeout_s)
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
            native_page_admission=a.native_page_admission,
            native_page_disabled_handlers=frozenset(a.native_page_disable),
            native_edit_prefill=a.native_edit_prefill,
            native_money_policy=a.native_money_policy,
            native_filter_semantics=a.native_filter_semantics,
            native_guard_port=a.native_guard_port,
            native_writes=a.native_writes,
            native_write_preparation=a.native_write_preparation,
            native_submit=a.native_submit,
            location_bindings=a.location_bindings,
            max_sessions=a.max_sessions,
            daily_allowance_cny=a.daily_allowance_cny,
            daily_total_cap_cny=a.daily_total_cap_cny,
            monthly_total_cap_cny=a.monthly_total_cap_cny,
            shutdown_drain_timeout_s=a.shutdown_drain_timeout_s,
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
    signal.signal(signal.SIGTERM, lambda *_: (host.begin_drain(), stop.set()))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    print(f"yudao assistant: gateway {host.server.base_url}, bootstrap :{host.config.bootstrap_port}",
          flush=True)
    try:
        while not stop.wait(60):
            host.forget_closed()
            host.cleanup()
    except KeyboardInterrupt:
        pass
    host.drain()
    http.shutdown()
    host.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
