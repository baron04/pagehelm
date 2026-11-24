"""Application-neutral turn harness: binding, action fence, receipts, budget, and settlement."""
from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass, field, fields
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from ..business.approvals import PAGE_OPERATIONS, ApprovalWatch, OperationPlan
from ..business.control import ApprovalGate, ApprovalGrant
from ..business.draft_steps import DraftStepAdmission
from ..business.tools.contracts import BusinessToolResult
from ..business.writes import WriteWatch
from ..composition.binding import BoundApplication
from ..modeling.chat import require_chat_port
from ..privacy import redact_sensitive_assignments, redact_session_handoffs
from ..schemas.browser import ActionReceipt, PageObservation
from ..state.conversation import ConversationState
from ..state.identity import PrincipalError
from ..state.ledger import SessionLedger
from ..transport.browser import RemoteBrowserClient as _RealRemoteBrowserClient
from ..transport.browser import TransportError, TurnCancelled
from ..transport.panel_view import describe_step, step_facts
from ..transport.protocol import RemoteProtocolError
from .action_admission import ActionAdmission
from .budget import TurnBudget
from .click_policy import click_policy_refusal
from .model_loop_contracts import PageActionOutcome
from .product import (
    AgentAnswerContext,
    AgentTurnError,
    AgentTurnErrorCode,
    AgentTurnExecution,
    AgentTurnMessage,
    AgentTurnStatus,
    _status_for,
    render_neutral_user_answer,
)
from .receipt import TurnReceipt
from .runstate import RunResult
from .turn_trace import TurnTrace

RemoteBrowserClient = _RealRemoteBrowserClient

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CJK = re.compile(r"[\u3400-\u9fff]")

_MISSING = object()
_OPTIONAL_NONE_KEYS = frozenset({
    "forced_summary_at",
    "pending_delivery",
    "pending_numeric_finish",
    "settled_action_observation",
    "handoff",
    "prepared_knowledge",
    "source_ledger",
    "handoff_target_key",
    "handoff_mark",
})
_OPTIONAL_FALSE_KEYS = frozenset({"review_completion"})


def _make_browser_client(channel, *, timeout_s: float, turn_id: str):
    if RemoteBrowserClient is not _RealRemoteBrowserClient:
        return RemoteBrowserClient(channel, timeout_s=timeout_s, turn_id=turn_id)
    from ..goal import executor as _goal_executor
    factory = getattr(_goal_executor, "RemoteBrowserClient", _RealRemoteBrowserClient)
    return factory(channel, timeout_s=timeout_s, turn_id=turn_id)


@dataclass
class ModelTurnState(MutableMapping[str, Any]):
    """Explicit typed state for one model-driven turn loop (with mapping compatibility)."""

    reads: list[Any] = field(default_factory=list)
    marked: str = ""
    trace: list[str] = field(default_factory=list)
    touched: list[Any] = field(default_factory=list)
    done: set[Any] = field(default_factory=set)
    stalled: int = 0
    writes: Any = None
    written: list[str] = field(default_factory=list)
    corrections: int = 0
    final: str = ""
    skills: set[str] = field(default_factory=set)
    observation_cache: dict[str, Any] | None = None
    sources: Any = None
    prepared: bool = False
    prepared_knowledge: str | None = None
    runtime_used: bool = False
    issued_targets: list[Any] = field(default_factory=list)
    read_sequence: int = 0
    read_only: bool = False
    tool_texts: list[str] = field(default_factory=list)
    model_calls: int = 0
    tool_calls: int = 0
    forced_summary_at: int | None = None
    forced_summary_kind: str = ""
    pending_delivery: tuple[dict[str, Any], Any] | None = None
    pending_numeric_finish: tuple[dict[str, Any], tuple[str, ...]] | None = None
    delivery_corrections: dict[Any, int] = field(default_factory=dict)
    query_answers: dict[str, Any] = field(default_factory=dict)
    query_answer_sources: dict[str, str] = field(default_factory=dict)
    query_answer_sequence: int = 0
    empty_reads: dict[str, int] = field(default_factory=dict)
    page_failures: int = 0
    failure_counts: dict[Any, int] = field(default_factory=dict)
    blocked_failures: set[Any] = field(default_factory=set)
    failed_reads: dict[Any, str] = field(default_factory=dict)
    failed_repeats: dict[Any, int] = field(default_factory=dict)
    not_executed: list[tuple[str, str, str]] = field(default_factory=list)
    handoff: tuple[str, str] | None = None
    review_completion: bool = False
    completion_reviews: int = 0
    numbers_sent_back: bool = False
    # Native submit and value-source fields
    source_ledger: Any = None
    source_checked: set[Any] = field(default_factory=set)
    source_checked_values: set[str] = field(default_factory=set)
    form_baselines: dict[Any, Any] = field(default_factory=dict)
    form_record_baselines: dict[Any, Any] = field(default_factory=dict)
    submit_targets: dict[Any, Any] = field(default_factory=dict)
    unrestorable_forms: dict[Any, Any] = field(default_factory=dict)
    handoff_target_key: Any = None
    handoff_button: str = ""
    handoff_mark: Any = None
    submit_started: bool = False
    submit_handoff_blocked: str = ""
    restored_form_labels: frozenset[str] = frozenset()
    settled_action_observation: dict[str, Any] | None = None
    form_field_outcomes: dict[str, Any] = field(default_factory=dict)
    finish_tables: dict[str, Any] = field(default_factory=dict)
    unverified_companion_effects: list[dict[str, Any]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    _TYPED_FIELDS = frozenset({
        "reads", "marked", "trace", "touched", "done", "stalled", "writes", "written",
        "corrections", "final", "skills", "observation_cache", "sources", "prepared",
        "prepared_knowledge", "runtime_used", "issued_targets", "read_sequence", "read_only",
        "tool_texts", "model_calls", "tool_calls", "forced_summary_at", "forced_summary_kind",
        "pending_delivery", "pending_numeric_finish",
        "delivery_corrections", "query_answers",
        "query_answer_sources", "query_answer_sequence", "empty_reads", "page_failures",
        "failure_counts", "blocked_failures", "failed_reads", "failed_repeats", "not_executed",
        "handoff", "review_completion", "completion_reviews", "numbers_sent_back",
        "source_ledger", "source_checked", "source_checked_values", "form_baselines",
        "form_record_baselines", "submit_targets", "unrestorable_forms", "handoff_target_key",
        "handoff_button", "handoff_mark", "submit_started", "submit_handoff_blocked",
        "restored_form_labels", "settled_action_observation", "form_field_outcomes",
        "finish_tables", "unverified_companion_effects",
    })

    def _present(self, key: str) -> bool:
        if key in self._TYPED_FIELDS:
            val = getattr(self, key)
            if key in _OPTIONAL_NONE_KEYS:
                return val is not None
            if key in _OPTIONAL_FALSE_KEYS:
                return bool(val)
            return True
        return key in self.extras

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._present(key)

    def __getitem__(self, key: str) -> Any:
        if key in self._TYPED_FIELDS:
            if key in _OPTIONAL_NONE_KEYS and getattr(self, key) is None:
                raise KeyError(key)
            if key in _OPTIONAL_FALSE_KEYS and not getattr(self, key):
                raise KeyError(key)
            return getattr(self, key)
        return self.extras[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._TYPED_FIELDS:
            setattr(self, key, value)
        else:
            self.extras[key] = value

    def __delitem__(self, key: str) -> None:
        if key in self._TYPED_FIELDS:
            if not self._present(key):
                raise KeyError(key)
            if key in _OPTIONAL_NONE_KEYS:
                setattr(self, key, None)
                return
            if key in _OPTIONAL_FALSE_KEYS:
                setattr(self, key, False)
                return
            raise KeyError(key)
        del self.extras[key]

    def pop(self, key: str, default: Any = _MISSING) -> Any:
        if key in self._TYPED_FIELDS:
            if not self._present(key):
                if default is _MISSING:
                    raise KeyError(key)
                return default
            val = getattr(self, key)
            if key in _OPTIONAL_NONE_KEYS:
                setattr(self, key, None)
                return val
            if key in _OPTIONAL_FALSE_KEYS:
                setattr(self, key, False)
                return val
            return val
        if default is _MISSING:
            return self.extras.pop(key)
        return self.extras.pop(key, default)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._TYPED_FIELDS:
            if not self._present(key):
                return default
            return getattr(self, key)
        return self.extras.get(key, default)

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key in self._TYPED_FIELDS:
            if not self._present(key):
                setattr(self, key, default)
            return getattr(self, key)
        return self.extras.setdefault(key, default)

    def __iter__(self):
        for name in self._TYPED_FIELDS:
            if self._present(name):
                yield name
        yield from self.extras

    def __len__(self) -> int:
        count = sum(1 for name in self._TYPED_FIELDS if self._present(name))
        return count + len(self.extras)


@dataclass
class HarnessTurnContext:
    """Per-turn execution context owned by `TurnHarness`."""

    message: AgentTurnMessage
    run_id: str
    ledger: SessionLedger
    browser: Any
    writes: WriteWatch
    approvals: ApprovalWatch
    obs: PageObservation | None
    started_at: float
    receipt: TurnReceipt = field(default_factory=TurnReceipt)
    budget: TurnBudget | None = None
    model_stop: threading.Event = field(default_factory=threading.Event)
    model_futures: list = field(default_factory=list)
    step: int = 0
    page_actions: int = 0
    refused: bool = False
    terminal_reason: str = ""
    detail: str = ""
    chinese: bool = False
    write_intent: bool = False
    mutation_outcomes: list = field(default_factory=list)
    cancelled: Callable[[], bool] = lambda: False
    clarification: dict | None = None
    source_answer: str = ""
    source_complete: bool = True
    answer_targets: tuple = ()
    chat: object = None
    skill_limit: str = ""
    chosen: tuple = ()
    # ARC-08 / ARC-12 structured telemetry
    first_route_kind: str = ""
    stop_reason: str = ""
    tool_timings: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    model_calls: int = 0
    # ARC-06 / UX-02 / UX-05 terminal progress payloads
    finish_table: dict[str, Any] | None = None
    input_options: list[dict[str, str]] = field(default_factory=list)
    handoff_payload: dict[str, Any] | None = None


def _sanitize_trace_label(value: str, *, max_len: int = 120) -> str:
    cleaned = redact_sensitive_assignments(redact_session_handoffs(str(value or ""))).strip()
    return cleaned[:max_len]


def _build_debug_trace(
    *,
    s: HarnessTurnContext,
    wall_seconds: float,
    stop_reason: str,
    stage_timings: dict[str, float],
    receipt_report: dict[str, Any],
) -> dict[str, Any]:
    """Build a bounded, redacted local-dev ARC-08 trace summary."""
    trace_summary: dict[str, Any] = {
        "wall_ms": max(0, int(round(max(0.0, wall_seconds) * 1000))),
        "model_calls": max(0, int(s.model_calls)),
    }
    first_route = _sanitize_trace_label(s.first_route_kind, max_len=80)
    if first_route:
        trace_summary["first_route_kind"] = first_route
    clean_stop = _sanitize_trace_label(stop_reason, max_len=120)
    if clean_stop:
        trace_summary["stop_reason"] = clean_stop
    stage_ms = {
        str(k)[:64]: max(0, int(round(max(0.0, float(v)) * 1000)))
        for k, v in stage_timings.items()
        if isinstance(k, str) and k.strip()
    }
    if stage_ms:
        trace_summary["stage_ms"] = stage_ms
    accounting = receipt_report.get("accounting") or {}
    tokens: dict[str, int] = {}
    for src_key, dst_key in (("known_input_tokens", "input"), ("known_output_tokens", "output")):
        val = accounting.get(src_key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
            tokens[dst_key] = int(val)
    if tokens:
        tokens["total"] = tokens.get("input", 0) + tokens.get("output", 0)
        trace_summary["tokens"] = tokens
    chain = []
    for entry in s.tool_timings[:40]:
        tool_name = _sanitize_trace_label(str(entry.get("tool") or ""), max_len=80)
        if not tool_name:
            continue
        item: dict[str, Any] = {
            "step": max(0, int(entry.get("step", 0))),
            "tool": tool_name,
            "elapsed_ms": max(0, int(round(max(0.0, float(entry.get("seconds", 0.0))) * 1000))),
            "ok": bool(entry.get("ok", True)),
        }
        err = _sanitize_trace_label(str(entry.get("error_code") or ""), max_len=80)
        if err:
            item["error_code"] = err
        chain.append(item)
    if chain:
        trace_summary["tool_chain"] = chain
    recorded_checks = []
    for chk in getattr(s, "checks", ())[:20]:
        if not isinstance(chk, dict):
            continue
        check_name = _sanitize_trace_label(str(chk.get("check") or ""), max_len=80)
        outcome = _sanitize_trace_label(str(chk.get("outcome") or ""), max_len=40)
        if not check_name or not outcome:
            continue
        recorded_checks.append({
            "check": check_name,
            "outcome": outcome,
            "attempt": max(1, int(chk.get("attempt", 1))),
            "answer_preserved": bool(chk.get("answer_preserved", False)),
        })
    if recorded_checks:
        trace_summary["checks"] = recorded_checks
    return trace_summary


class TurnHarness:
    """Own turn binding, observation tracking, action fencing, receipts, budget, and settlement."""

    def __init__(
        self,
        *,
        channel,
        config,
        chat,
        application: BoundApplication,
        control,
        run_id_factory,
        ledger_factory=SessionLedger,
        conversation_state: ConversationState | None = None,
        disabled_page_actions: frozenset[str] = frozenset(),
        turn_events=None,
        permits_offline_start: bool = False,
    ):
        if config.allow_business_mutations:
            from ..business.mutation import MutationJournalDurability
            if (
                application.ports.operations is None
                or not config.require_identity
                or config.deployment_profile == "production"
                and application.business_tools.mutation_journal_durability
                is not MutationJournalDurability.DURABLE
            ):
                raise ValueError(
                    "goal writes require registered operations, identity and a durable journal"
                )
        self.channel = channel
        self.config = config
        self.chat = require_chat_port(chat, role="executor chat")
        if not isinstance(application, BoundApplication):
            raise TypeError("application must be a BoundApplication")
        self.application = application
        self.control = control
        self.run_id_factory = run_id_factory
        self.ledger_factory = ledger_factory
        self.conversation_state = conversation_state or ConversationState()
        self.disabled_page_actions = frozenset(disabled_page_actions)
        self.turn_events = turn_events
        self.permits_offline_start = bool(permits_offline_start)
        # Per-turn action fence and receipt state
        self.tool_receipts: list[ActionReceipt] = []
        self.verified_action_commands: dict[str, str] = {}
        self.admission: ActionAdmission | None = None
        self.draft_admission: DraftStepAdmission | None = None
        self.admission_turn: str | None = None
        self.held_handoff: dict[str, Any] | None = None
        self.page_action_outcome: PageActionOutcome | None = None
        self.uncertain_page_dispatch: bool = False

    @classmethod
    def detached(cls) -> "TurnHarness":
        """Create a minimal unattached harness for unit tests using `__new__`."""
        obj = cls.__new__(cls)
        obj.channel = None
        obj.config = None
        obj.chat = None
        obj.application = None
        obj.control = None
        obj.run_id_factory = None
        obj.ledger_factory = SessionLedger
        obj.conversation_state = ConversationState()
        obj.disabled_page_actions = frozenset()
        obj.turn_events = None
        obj.permits_offline_start = False
        obj.tool_receipts = []
        obj.verified_action_commands = {}
        obj.admission = None
        obj.draft_admission = None
        obj.admission_turn = None
        obj.held_handoff = None
        obj.page_action_outcome = None
        obj.uncertain_page_dispatch = False
        return obj

    def _publish_step(self, facts: dict, turn_id: str) -> None:
        """Publish one bounded step indicator onto the turn progress timeline."""
        if self.channel is None or not turn_id:
            return
        text = describe_step(facts)
        if not text:
            return
        try:
            self.channel.publish(
                "progress",
                {"state": "step", "message": text, "step": step_facts(facts)},
                turn_id=turn_id,
            )
        except Exception:
            pass

    def publish_read_page(self, turn_id: str, page: int | None = None) -> None:
        """Publish a paged-read step notification (`read_page`) onto the turn timeline."""
        facts: dict[str, Any] = {"stage": "read_page"}
        if isinstance(page, int) and not isinstance(page, bool) and page >= 1:
            facts["page"] = page
        self._publish_step(facts, turn_id)

    def reset_turn_fence(self) -> None:
        """Reset turn-scoped fence, handoff, and receipt tracking at turn start."""
        self.tool_receipts = []
        self.verified_action_commands = {}
        self.admission = None
        self.draft_admission = None
        self.admission_turn = None
        self.held_handoff = None
        self.page_action_outcome = None
        self.uncertain_page_dispatch = False

    @staticmethod
    def note_first_route(s: Any, route_kind: str, *, trace: TurnTrace | None = None) -> None:
        """Record the first tool/route kind dispatched during this turn."""
        clean = str(route_kind or "").strip()[:80]
        if clean and hasattr(s, "first_route_kind") and not s.first_route_kind:
            s.first_route_kind = clean
        if trace is not None and clean:
            trace.note_first_route(clean)

    @staticmethod
    def set_stop_reason(s: Any, reason: str, *, trace: TurnTrace | None = None) -> None:
        """Record the programmatic stop reason for this turn."""
        clean = str(reason or "").strip()[:120]
        if clean and hasattr(s, "stop_reason"):
            s.stop_reason = clean
        if trace is not None and clean:
            trace.set_stop_reason(clean)

    @staticmethod
    def record_tool_timing(
        s: Any,
        *,
        step: int,
        tool: str,
        seconds: float,
        ok: bool = True,
        error_code: str = "",
        trace: TurnTrace | None = None,
    ) -> None:
        """Append one structural tool-timing entry onto context and optional TurnTrace."""
        entry = {
            "step": int(step),
            "tool": str(tool or "")[:80],
            "seconds": round(max(0.0, float(seconds)), 3),
            "ok": bool(ok),
        }
        if error_code:
            entry["error_code"] = str(error_code)[:80]
        timings = getattr(s, "tool_timings", None)
        if isinstance(timings, list):
            timings.append(entry)
        if trace is not None:
            trace.record_tool_timing(
                step=step, tool=tool, seconds=seconds, ok=ok, error_code=error_code
            )

    def _continued(self, message: AgentTurnMessage) -> tuple[str, str] | None:
        pending = self.conversation_state.pending_clarification
        asked = self.conversation_state.last_user_text()
        if not pending or not asked or pending.get("response_kind") != "single_choice":
            return None
        reply = " ".join(message.text.split()).casefold()
        chosen = next(
            (c for c in pending["choices"] if reply.strip("。.!！ ") == c["choice_id"].casefold()),
            None,
        )
        if chosen is None:
            chosen = next(
                (c for c in pending["choices"] if c["record_ref"].casefold() in reply), None
            )
        if chosen is None:
            return None
        return (
            (
                f"{asked}（指的是 {chosen['record_ref']}）"
                if _CJK.search(asked)
                else f"{asked} (meaning {chosen['record_ref']})"
            ),
            chosen["record_ref"],
        )

    def bind_turn(self, message: dict) -> tuple[HarnessTurnContext, Any]:
        """Validate origin/identity/observation and create a fresh `HarnessTurnContext`."""
        self.reset_turn_fence()
        turn_message = AgentTurnMessage.from_wire(message)
        dropped = (
            self.conversation_state.clear()
            if self.channel.take_new_conversation()
            else -1
        )
        if not self.application.descriptor.owns_url(turn_message.page_url):
            raise AgentTurnError(
                AgentTurnErrorCode.APPLICATION_MISMATCH,
                "the task belongs to a different application origin",
                retryable=False,
            )
        principal = getattr(self.channel, "principal", None)
        if principal is None and self.config.require_identity:
            raise AgentTurnError(
                AgentTurnErrorCode.IDENTITY_UNAVAILABLE,
                "this application requires a verified session identity",
                retryable=False,
            )
        try:
            self.application.ports.identity.validate(
                principal, application_id=self.application.descriptor.application_id
            )
        except PrincipalError as exc:
            raise AgentTurnError(
                AgentTurnErrorCode.IDENTITY_UNAVAILABLE,
                "the session identity is invalid, expired, or belongs to another application",
                retryable=False,
            ) from exc
        entry = self.channel.latest_observation_envelope
        if entry is None and not self.permits_offline_start:
            raise AgentTurnError(
                AgentTurnErrorCode.OBSERVATION_UNAVAILABLE,
                "the browser has not supplied a page observation",
                retryable=True,
            )
        if entry is not None and turn_message.page_url != entry.observation.url:
            raise AgentTurnError(
                AgentTurnErrorCode.STALE_MESSAGE,
                "the task was submitted from a page other than the latest observation",
                retryable=True,
            )
        run_id = self.run_id_factory(turn_message)
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise AgentTurnError(
                AgentTurnErrorCode.INVALID_RUN_ID,
                "run_id_factory returned an invalid run identifier",
                retryable=False,
            )
        started_at = time.time()
        ledger = self.ledger_factory(
            self.config.ledger_path,
            run_id,
            base={
                "session_id": self.channel.session_id,
                "turn_id": turn_message.message_id,
                "tenant_digest": (
                    principal.audit_projection().get("tenant_digest", "")
                    if principal is not None
                    else ""
                ),
            },
        )
        if dropped >= 0:
            ledger.append("new_conversation", dropped_turns=dropped)
        if principal is not None:
            ledger.append(
                "turn_identity_bound",
                message_id=turn_message.message_id,
                identity=principal.audit_projection(),
            )
        browser = _make_browser_client(
            self.channel,
            timeout_s=self.config.transport_timeout_s,
            turn_id=turn_message.message_id,
        )
        try:
            if entry is not None:
                browser.start()
        except TransportError as exc:
            ledger.append("turn_start_failed", error_type=type(exc).__name__)
            raise AgentTurnError(
                AgentTurnErrorCode.RUNTIME_UNAVAILABLE,
                "the browser runtime was unavailable for this turn",
                retryable=True,
                run_id=run_id,
            ) from exc
        current = self.channel.latest_observation_envelope if entry is not None else None
        if entry is not None and (
            current is None
            or current.observation_id != entry.observation_id
            or current.page_epoch != entry.page_epoch
            or current.observation.content_digest != entry.observation.content_digest
        ):
            ledger.append("turn_binding_rejected", message_id=turn_message.message_id)
            raise AgentTurnError(
                AgentTurnErrorCode.STALE_MESSAGE,
                "the page changed while the turn was being bound",
                retryable=True,
                run_id=run_id,
            )
        gate = ApprovalGate(self.channel, ledger)
        cancelled = lambda: self.channel.message_cancelled(turn_message.message_id)  # noqa: E731

        def confirm_operation(plan: OperationPlan, *, context=None) -> ApprovalGrant:
            chinese = bool(_CJK.search(turn_message.text))
            effect = (
                "在页面上帮你完成这件事" if chinese else "Work the page to do this for you"
            )
            targets = (
                ("会打开页面、点击、填写和选择",)
                if chinese
                else ("Opens pages, clicks, fills and chooses",)
            )
            self._publish_step({"stage": "awaiting_approval"}, turn_message.message_id)
            try:
                return gate.decide_plan(
                    plan,
                    effect=effect,
                    targets=targets,
                    ttl_s=self.config.operation_approval_ttl_s,
                    context=context,
                    kind="operate",
                )
            except RemoteProtocolError as exc:
                if exc.code != "approval_expired":
                    raise
                return ApprovalGrant(
                    approval_id="",
                    decision="expired",
                    plan_digest=plan.content_digest,
                    decided_at=time.time(),
                    operation_context_digest=(context.binding_digest if context else ""),
                )

        def approve_business(proposal, *, context=None, offer_prefill=False):
            self._publish_step({"stage": "awaiting_approval"}, turn_message.message_id)
            try:
                return gate.decide_plan(
                    proposal,
                    effect=proposal.effect,
                    targets=proposal.targets,
                    ttl_s=self.config.approval_ttl_s,
                    context=context,
                    offer_prefill=offer_prefill,
                )
            except RemoteProtocolError as exc:
                if exc.code != "approval_expired":
                    raise
                return ApprovalGrant(
                    approval_id="",
                    decision="expired",
                    plan_digest=proposal.content_digest,
                    decided_at=time.time(),
                    operation_context_digest=(context.binding_digest if context else ""),
                )

        approvals = ApprovalWatch(
            ledger=ledger,
            run_id=run_id,
            principal=principal,
            session_id=(self.channel.session_id if principal is not None else ""),
            turn_id=(turn_message.message_id if principal is not None else ""),
            business_approval=(
                approve_business if self.config.allow_business_mutations else None
            ),
            operation_confirmation=(
                confirm_operation if self.config.confirm_page_operations else None
            ),
            initial_operation_granted=self.conversation_state.page_operation_granted,
            on_operation_granted=self.conversation_state.grant_page_operation,
            cancelled=cancelled,
            approval_hold_s=self.config.approval_ttl_s,
            operation_hold_s=self.config.operation_approval_ttl_s,
        )
        writes = WriteWatch(
            control=self.control,
            ledger=ledger,
            run_id=run_id,
            write_guard_declared=bool(self.application.descriptor.page_write_guard),
        )
        continued = self._continued(turn_message)
        chosen: tuple = ()
        if continued is not None:
            ledger.append("goal_clarification_answered", message_id=turn_message.message_id)
            text, name = continued
            turn_message, chosen = (
                AgentTurnMessage(
                    message_id=turn_message.message_id,
                    text=text,
                    page_url=turn_message.page_url,
                ),
                (name,),
            )
        state = HarnessTurnContext(
            message=turn_message,
            run_id=run_id,
            ledger=ledger,
            browser=browser,
            writes=writes,
            approvals=approvals,
            obs=current.observation if current is not None else None,
            started_at=started_at,
            chinese=bool(_CJK.search(turn_message.text)),
            cancelled=cancelled,
            chosen=chosen,
        )
        state.receipt.started -= max(0.0, time.time() - started_at)
        state.budget = TurnBudget(
            steps=self.config.step_budget,
            seconds=self.config.wall_budget_s,
            cancelled=cancelled,
            clock=lambda: time.monotonic() - approvals.waited_ms / 1000,
        )
        state.budget.started -= max(0.0, time.time() - started_at)
        self.channel.publish(
            "progress", {"state": "agent_started"}, turn_id=turn_message.message_id
        )
        return state, current

    def dispatch_browser(
        self,
        s: HarnessTurnContext,
        action: str,
        args: dict,
        *,
        navigating: bool = False,
        admitted: bool = False,
    ) -> ActionReceipt | None:
        """One page command under write control, with its receipt on the record."""
        if s.terminal_reason:
            return None
        s.budget.check()
        click_refusal = ""
        if admitted and action == "click":
            pass
        elif (
            action == "click"
            and action in self.disabled_page_actions
            and self.config.click_policy is not None
        ):
            click_refusal = click_policy_refusal(
                self.config.click_policy, s.obs, args.get("ref")
            )
            s.ledger.append(
                "click_policy_decision",
                step=s.step,
                allowed=not click_refusal,
                reason=click_refusal[:200],
            )
        if action in self.disabled_page_actions and not (
            action == "click"
            and (
                admitted
                or (self.config.click_policy is not None and not click_refusal)
            )
        ):
            s.terminal_reason, s.detail = (
                "agent_refused",
                "the deployment disabled this page action",
            )
            s.ledger.append("goal_action_disabled", action=action)
            return None
        if action in PAGE_OPERATIONS:
            reason, detail = s.approvals.confirm_operation(
                s.step + 1, s.obs, action=action, url=str(args.get("url") or "")
            )
            if reason:
                s.terminal_reason, s.detail = reason, detail
                return None
        s.budget.check()
        s.step += 1
        s.ledger.append("tool_call", step=s.step, tool=action, args=dict(args))
        s.writes.expect_decision(False)
        if action == "wait_for":
            self._publish_step({"stage": "settle"}, s.message.message_id)
        try:
            with s.receipt.stage("wait" if action == "wait_for" else "ui"):
                receipt = s.browser.execute_with_budget(
                    action,
                    args,
                    timeout_s=s.budget.check(),
                    expect_navigation=navigating,
                )
        except TurnCancelled as exc:
            s.terminal_reason, s.detail = "user_cancelled", str(exc)[:200]
            return None
        except TransportError as exc:
            s.terminal_reason, s.detail = "transport_error", str(exc)[:200]
            return None
        s.approvals.settle_operation()
        outcome = s.writes.write_outcome(receipt, s.step, action_may_write=False)
        s.ledger.append(
            "write_outcome",
            step=s.step,
            **{k: v for k, v in outcome.items() if k != "detail"},
        )
        s.ledger.append(
            "action_receipt",
            step=s.step,
            tool=action,
            command_id=receipt.command_id,
            ok=receipt.ok,
            error_code=receipt.error_code,
            url_before=receipt.url_before,
            url_after=receipt.url_after,
            navigated=receipt.navigated,
            write="none",
        )
        if action not in ("observe", "wait_for", "form_model"):
            s.page_actions += 1
        s.refused = receipt.error_code == "capability_refused"
        if receipt.observation is not None:
            s.obs = receipt.observation
        s.budget.check()
        return receipt

    def settle(
        self,
        s: HarnessTurnContext,
        current,
        text: str,
        *,
        decision_strategy: str,
        verifies_finish: bool,
        trace: TurnTrace | None = None,
        trace_steps=None,
    ) -> AgentTurnExecution:
        """Finalize budget, control plane, audit records, TurnTrace, and progress delivery."""
        exhausted = s.budget.projection()["terminal_reason"]
        if exhausted:
            s.terminal_reason = exhausted
            s.stop_reason = exhausted
            text = (
                "本次任务已达到执行限额，未继续执行。"
                if s.chinese
                else "The turn budget was reached; execution stopped."
            )
        if s.cancelled():
            s.terminal_reason = "user_cancelled"
            s.stop_reason = "user_cancelled"
            text = "操作已停止。" if s.chinese else "The task was stopped."
        if s.mutation_outcomes and s.terminal_reason != "agent_finished":
            confirmed = "、".join(item["effect"] for item in s.mutation_outcomes)
            text = (
                (
                    "已完成并核对的操作仍然有效："
                    if s.chinese
                    else "These verified effects remain completed: "
                )
                + confirmed
                + "\n\n"
                + text
            )
        if s.source_answer:
            text = "\n\n".join(part for part in (text, s.source_answer) if part)
            if not s.source_complete and s.terminal_reason in ("", "agent_finished"):
                s.terminal_reason = "agent_refused"
            if len(text) > self.config.max_user_answer_chars:
                s.terminal_reason = "agent_refused"
                text = (
                    (
                        "回答超过展示长度，以下仅为部分结果，请缩小问题范围。\n\n"
                        if s.chinese
                        else "The answer exceeds the display limit; only part is shown. Please narrow the request.\n\n"
                    )
                    + text
                )
        release = s.writes.end_control()
        if release is not None and not s.terminal_reason:
            s.terminal_reason, s.detail = "control_plane_error", str(release)[:200]
        terminal = s.terminal_reason or "agent_finished"
        stop_reason = (s.stop_reason or terminal) if not (exhausted or s.cancelled()) else terminal
        wall = round(time.time() - s.started_at, 3)
        agent = RunResult(
            run_id=s.run_id,
            task_ref=s.run_id,
            arm=decision_strategy,
            terminal_reason=terminal,
            steps_used=s.step,
            wall_seconds=wall,
            agent_said=text[: self.config.max_user_answer_chars],
            page_actions=s.page_actions,
            operation_control=s.approvals.operation_result(),
            success_claim=(
                "not_claimed"
                if terminal != "agent_finished" or s.write_intent or not verifies_finish
                else "verified"
            ),
            detail=s.detail,
            clarification=s.clarification if terminal == "needs_clarification" else None,
        )
        s.ledger.append(
            "run_finished",
            task_ref=s.run_id,
            arm=decision_strategy,
            terminal_reason=terminal,
            steps_used=s.step,
            wall_seconds=wall,
            agent_said=text[:300],
            page_actions=s.page_actions,
            operation_control=agent.operation_control or {},
        )
        status = _status_for(terminal)
        user_answer = redact_sensitive_assignments(redact_session_handoffs(text)).strip()
        user_answer = user_answer[: self.config.max_user_answer_chars]
        fallback = False
        if not user_answer:
            fallback = True
            user_answer = render_neutral_user_answer(
                AgentAnswerContext(
                    message=s.message,
                    agent=agent,
                    business_result=None,
                    max_chars=self.config.max_user_answer_chars,
                )
            )
        execution = AgentTurnExecution(
            schema_version=1,
            run_id=s.run_id,
            session_id=self.channel.session_id,
            message=s.message,
            entry_observation_id=current.observation_id if current is not None else None,
            entry_observation_digest=(
                current.observation.content_digest if current is not None else None
            ),
            status=status,
            terminal_reason=terminal,
            user_answer=user_answer,
            agent=agent,
            business_result=(
                BusinessToolResult(
                    {"outcomes": s.mutation_outcomes},
                    {"verified_outcomes": len(s.mutation_outcomes)},
                )
                if s.mutation_outcomes
                else None
            ),
            answer_fallback_used=fallback,
            answer_targets=(
                tuple(
                    target
                    for target in s.answer_targets
                    if terminal == "agent_finished"
                    and not fallback
                    and target.target.label in user_answer
                )
            ),
            started_at=s.started_at,
            finished_at=time.time(),
        )
        self.conversation_state.record(
            message_id=s.message.message_id,
            user_text=s.message.text,
            assistant_text=user_answer,
            status=status.value,
            business_result=execution.business_result,
            clarification=s.clarification if status == AgentTurnStatus.NEEDS_INPUT else None,
        )
        s.ledger.append(
            "conversation_state_updated",
            retained_turns=self.conversation_state.turn_count,
            context_digest=self.conversation_state.content_digest,
        )
        s.ledger.append(
            "product_turn_finished",
            status=status.value,
            terminal_reason=terminal,
            answer_fallback_used=fallback,
            business_result_digest=(
                execution.business_result.content_digest if execution.business_result else ""
            ),
        )
        receipt_report = s.receipt.report(self.chat, s.run_id)
        s.ledger.append("goal_run_receipt", budget=s.budget.projection(), **receipt_report)
        stage_timings = {
            name: round(float(info.get("occupied_seconds", 0.0)), 3)
            for name, info in (receipt_report.get("stages") or {}).items()
            if isinstance(info, dict)
        }
        s.ledger.append(
            "turn_timing",
            first_route_kind=s.first_route_kind or "none",
            stop_reason=stop_reason,
            stage_timings=stage_timings,
            tool_timings=list(s.tool_timings),
            wall_seconds=wall,
            model_calls=s.model_calls,
        )
        turn_refusals: list[dict] = []
        read_refusals = getattr(self.channel, "turn_approval_refusals", None)
        if callable(read_refusals):
            turn_refusals = list(read_refusals(s.message.message_id) or ())
        if turn_refusals:
            s.ledger.append("approval_gate_refusals", cards=turn_refusals)
        if trace is not None:
            for item in turn_refusals:
                trace.record_approval_refusal(
                    approval_id=item["approval_id"],
                    kind=item["kind"],
                    count=item["count"],
                    last_code=item["last_code"],
                )
            if s.first_route_kind:
                trace.note_first_route(s.first_route_kind)
            trace.set_stop_reason(stop_reason)
            for stage_name, seconds in stage_timings.items():
                if stage_name not in trace.stage_timings:
                    trace.record_stage(stage_name, seconds)
            principal = s.approvals.principal
            identity = principal.audit_projection() if principal is not None else {}
            try:
                trace.write(
                    run_id=s.run_id,
                    turn_id=s.message.message_id,
                    prompt=s.message.text,
                    start_route=(
                        urlsplit(current.observation.url).path if current is not None else ""
                    ),
                    terminal_reason=execution.terminal_reason,
                    wall_seconds=execution.agent.wall_seconds,
                    answer=execution.user_answer,
                    steps=trace_steps if trace_steps is not None else getattr(s, "_trace_steps", ()),
                    actor={
                        **{
                            key: identity[key]
                            for key in ("application_id", "tenant_digest", "actor_digest")
                            if key in identity
                        },
                        **(
                            {"actor_authority_digest": principal.actor_authority_digest}
                            if principal is not None
                            else {}
                        ),
                    },
                )
            except OSError as exc:
                s.ledger.append("turn_trace_failed", error_type=type(exc).__name__)
        progress_payload: dict[str, Any] = {
            "state": status.value,
            "terminal_reason": terminal,
            "message": user_answer,
            "answer_targets": [target.to_json() for target in execution.answer_targets],
        }
        if status == AgentTurnStatus.FINISHED and getattr(s, "finish_table", None):
            progress_payload["table"] = s.finish_table
        if status == AgentTurnStatus.NEEDS_INPUT and getattr(s, "input_options", None):
            progress_payload["input_options"] = list(s.input_options)
        if status in {AgentTurnStatus.FINISHED, AgentTurnStatus.NEEDS_INPUT} and getattr(s, "handoff_payload", None):
            progress_payload["handoff"] = s.handoff_payload
        if bool(getattr(self.channel, "local_dev_mode", False)):
            progress_payload["debug_trace"] = _build_debug_trace(
                s=s,
                wall_seconds=wall,
                stop_reason=stop_reason,
                stage_timings=stage_timings,
                receipt_report=receipt_report,
            )
        try:
            self.channel.publish("progress", progress_payload, turn_id=s.message.message_id)
        except Exception as exc:
            s.ledger.append(
                "terminal_delivery_lost",
                status=status.value,
                error_type=type(exc).__name__,
            )
        return execution
