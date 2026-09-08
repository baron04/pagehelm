"""Application-neutral execution of one server Agent turn.

The surrounding application chooses the model, tool catalog, control implementation,
scope, and presentation of structured tool facts.  This module binds that choice to the
latest browser observation and runs the existing Agent loop.  It reports what the loop did;
it does not decide whether an application-level goal was achieved.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .browser import TransportError
from .business import BusinessToolResult, BusinessToolRuntime
from .embedded_agent import ApprovalGate
from .ledger import SessionLedger
from .privacy import redact_sensitive_assignments, redact_session_handoffs
from .remote import (RemoteBrowserClient, RemoteProtocolError,
                     RemoteSessionChannel, ensure_safe_browser_payload)
from .session import RunResult, ServerAgentSession, TaskSpec


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class AgentTurnStatus(str, Enum):
    """A transport-level description of how the Agent loop ended."""

    FINISHED = "finished"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"
    FAILED = "failed"


class AgentTurnErrorCode(str, Enum):
    INVALID_MESSAGE = "invalid_message"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    STALE_MESSAGE = "stale_message"
    INVALID_RUN_ID = "invalid_run_id"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    EXECUTION_FAILED = "execution_failed"


class AgentTurnError(RuntimeError):
    """A typed product-boundary failure raised before a result can be returned."""

    def __init__(self, code: AgentTurnErrorCode, message: str, *, retryable: bool,
                 run_id: str = ""):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.run_id = run_id

    def to_json(self) -> dict:
        return {
            "code": self.code.value,
            "message": str(self),
            "retryable": self.retryable,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class AgentTurnMessage:
    message_id: str
    text: str
    page_url: str

    @classmethod
    def from_wire(cls, value: object) -> "AgentTurnMessage":
        if not isinstance(value, dict) or set(value) != {"message_id", "text", "page_url"}:
            raise AgentTurnError(
                AgentTurnErrorCode.INVALID_MESSAGE,
                "turn message requires exactly message_id, text, and page_url",
                retryable=False,
            )
        message_id = value.get("message_id")
        text = value.get("text")
        page_url = value.get("page_url")
        parsed = urlsplit(page_url) if isinstance(page_url, str) else None
        valid_url = bool(parsed and parsed.scheme in {"http", "https"} and parsed.netloc)
        if (not isinstance(message_id, str) or not message_id.strip()
                or len(message_id) > 200 or not isinstance(text, str) or not text.strip()
                or len(text) > 2_000 or not valid_url):
            raise AgentTurnError(
                AgentTurnErrorCode.INVALID_MESSAGE,
                "turn message contains an invalid identifier, task, or page URL",
                retryable=False,
            )
        try:
            ensure_safe_browser_payload(value)
        except RemoteProtocolError as exc:
            raise AgentTurnError(
                AgentTurnErrorCode.INVALID_MESSAGE,
                "turn message contains data that cannot cross the browser boundary",
                retryable=False,
            ) from exc
        return cls(message_id=message_id, text=text, page_url=page_url)


@dataclass(frozen=True, slots=True)
class AgentTurnConfig:
    """Immutable policy and audit location for one application-selected turn."""

    supported_scope: str
    ledger_path: Path
    step_budget: int = 8
    wall_budget_s: int = 240
    transport_timeout_s: float = 60.0
    disabled_actions: frozenset[str] = frozenset()
    scope_first_policy: bool = True
    policy_block_limit: int | None = 1
    plain_business_answer_min_page_actions: int | None = None
    approval_ttl_s: int = 120
    keep_observations: int = 3
    allow_application_entry: bool = False
    allow_business_mutations: bool = False
    max_user_answer_chars: int = 4_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "ledger_path", Path(self.ledger_path))
        if not isinstance(self.supported_scope, str) or not self.supported_scope.strip():
            raise ValueError("supported_scope must be a non-empty string")
        if (not isinstance(self.scope_first_policy, bool)
                or not isinstance(self.allow_application_entry, bool)
                or not isinstance(self.allow_business_mutations, bool)):
            raise ValueError("turn policy flags must be booleans")
        for name in ("step_budget", "wall_budget_s", "approval_ttl_s",
                     "keep_observations", "max_user_answer_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.approval_ttl_s > 600:
            raise ValueError("approval_ttl_s cannot exceed 600 seconds")
        if self.max_user_answer_chars > 20_000:
            raise ValueError("max_user_answer_chars cannot exceed 20000")
        if (isinstance(self.transport_timeout_s, bool)
                or not isinstance(self.transport_timeout_s, (int, float))
                or not math.isfinite(self.transport_timeout_s)
                or self.transport_timeout_s <= 0):
            raise ValueError("transport_timeout_s must be a positive finite number")
        if not isinstance(self.disabled_actions, frozenset) \
                or any(not isinstance(item, str) for item in self.disabled_actions):
            raise ValueError("disabled_actions must be a frozenset of tool names")
        if self.policy_block_limit is not None and (
                isinstance(self.policy_block_limit, bool)
                or not isinstance(self.policy_block_limit, int)
                or self.policy_block_limit < 1):
            raise ValueError("policy_block_limit must be a positive integer or None")
        minimum = self.plain_business_answer_min_page_actions
        if minimum is not None and (
                isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0):
            raise ValueError(
                "plain_business_answer_min_page_actions must be nonnegative or None")


@dataclass(frozen=True, slots=True)
class AgentAnswerContext:
    message: AgentTurnMessage
    agent: RunResult
    business_result: BusinessToolResult | None


def render_neutral_user_answer(context: AgentAnswerContext) -> str:
    """Prefer model prose and otherwise describe the terminal state without a success claim."""
    status = _status_for(context.agent.terminal_reason)
    if status is AgentTurnStatus.INDETERMINATE:
        return "The action outcome is uncertain. It was not retried automatically."
    if context.agent.agent_said.strip():
        return context.agent.agent_said.strip()
    return {
        AgentTurnStatus.FINISHED: "The agent finished the turn without a user-facing answer.",
        AgentTurnStatus.REFUSED: "The agent could not complete the request.",
        AgentTurnStatus.CANCELLED: "The requested action was cancelled.",
        AgentTurnStatus.INDETERMINATE: (
            "The action outcome is uncertain. It was not retried automatically."),
        AgentTurnStatus.FAILED: "The task ended before the agent could finish it.",
    }[status]


@dataclass(frozen=True, slots=True)
class AgentTurnExecution:
    schema_version: int
    run_id: str
    session_id: str
    message: AgentTurnMessage
    entry_observation_id: str
    entry_observation_digest: str
    status: AgentTurnStatus
    terminal_reason: str
    user_answer: str
    agent: RunResult
    business_result: BusinessToolResult | None
    answer_fallback_used: bool
    started_at: float
    finished_at: float

    def to_json(self) -> dict:
        business = self.business_result
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "message_id": self.message.message_id,
            "entry_observation_id": self.entry_observation_id,
            "entry_observation_digest": self.entry_observation_digest,
            "status": self.status.value,
            "terminal_reason": self.terminal_reason,
            "user_answer": self.user_answer,
            "agent": {
                "terminal_reason": self.agent.terminal_reason,
                "steps_used": self.agent.steps_used,
                "wall_seconds": self.agent.wall_seconds,
                "page_actions": self.agent.page_actions,
                "policy_blocks": self.agent.policy_blocks,
            },
            "business_result": ({
                "content_digest": business.content_digest,
                "route_count": len(business.allowed_routes),
            } if business is not None else None),
            "answer_fallback_used": self.answer_fallback_used,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


RunIdFactory = Callable[[AgentTurnMessage], str]
AnswerRenderer = Callable[[AgentAnswerContext], str]
LedgerFactory = Callable[[Path, str], SessionLedger]


class AgentTurnExecutor:
    """Execute one application-selected task without owning the channel lifecycle."""

    def __init__(self, *, channel: RemoteSessionChannel, config: AgentTurnConfig, chat,
                 business_tools: BusinessToolRuntime | None, control,
                 run_id_factory: RunIdFactory,
                 answer_renderer: AnswerRenderer = render_neutral_user_answer,
                 ledger_factory: LedgerFactory = SessionLedger,
                 attestation=None):
        self.channel = channel
        self.config = config
        self.chat = chat
        self.business_tools = business_tools
        self.control = control
        self.run_id_factory = run_id_factory
        self.answer_renderer = answer_renderer
        self.ledger_factory = ledger_factory
        self.attestation = attestation

    def run_once(self, message: dict | None = None) -> AgentTurnExecution:
        if message is None:
            raise AgentTurnError(
                AgentTurnErrorCode.INVALID_MESSAGE,
                "a turn message must be supplied by the conversation host",
                retryable=False,
            )
        return self.execute(message)

    def execute(self, message: dict) -> AgentTurnExecution:
        turn_message = AgentTurnMessage.from_wire(message)
        entry = self.channel.latest_observation_envelope
        if entry is None:
            raise AgentTurnError(
                AgentTurnErrorCode.OBSERVATION_UNAVAILABLE,
                "the browser has not supplied a page observation",
                retryable=True,
            )
        if turn_message.page_url != entry.observation.url:
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
        ledger = self.ledger_factory(self.config.ledger_path, run_id)
        approval_gate = ApprovalGate(self.channel, ledger)
        browser = RemoteBrowserClient(
            self.channel, timeout_s=self.config.transport_timeout_s)
        try:
            browser.start()
        except TransportError as exc:
            ledger.append("turn_start_failed", error_type=type(exc).__name__)
            raise AgentTurnError(
                AgentTurnErrorCode.RUNTIME_UNAVAILABLE,
                "the browser runtime was unavailable for this turn",
                retryable=True,
                run_id=run_id,
            ) from exc

        current = self.channel.latest_observation_envelope
        if (current is None or current.observation_id != entry.observation_id
                or current.page_epoch != entry.page_epoch
                or current.observation.content_digest
                != entry.observation.content_digest):
            ledger.append(
                "turn_binding_rejected", message_id=turn_message.message_id,
                expected_observation_id=entry.observation_id,
                actual_observation_id=(current.observation_id if current is not None else ""),
            )
            raise AgentTurnError(
                AgentTurnErrorCode.STALE_MESSAGE,
                "the page changed while the turn was being bound",
                retryable=True,
                run_id=run_id,
            )

        task = TaskSpec(
            task_ref=run_id,
            prompt=turn_message.text,
            entry_path=urlsplit(turn_message.page_url).path or "/",
            supported_scope=self.config.supported_scope,
        )

        def approve_business_mutation(proposal):
            return approval_gate.decide_plan(
                proposal,
                effect=proposal.effect,
                targets=proposal.targets,
                ttl_s=self.config.approval_ttl_s,
            )

        session = ServerAgentSession(
            task=task,
            arm="U",
            chat=self.chat,
            browser=browser,
            control=self.control,
            ledger=ledger,
            attestation=self.attestation,
            source_contract=False,
            step_budget=self.config.step_budget,
            wall_budget_s=self.config.wall_budget_s,
            run_id=run_id,
            keep_observations=self.config.keep_observations,
            disabled_actions=self.config.disabled_actions,
            scope_first_policy=self.config.scope_first_policy,
            policy_block_limit=self.config.policy_block_limit,
            business_tools=self.business_tools,
            business_mutation_approval=(
                approve_business_mutation
                if self.config.allow_business_mutations else None),
            plain_business_answer_min_page_actions=(
                self.config.plain_business_answer_min_page_actions),
            allow_application_entry=self.config.allow_application_entry,
        )
        session.entry_observation = current.observation
        try:
            session.plan()
            self.channel.publish("progress", {
                "state": "agent_started",
                "message": "The agent is working on the request.",
            })
            agent = session.run()
        except Exception as exc:
            ledger.append("turn_execution_failed", error_type=type(exc).__name__)
            raise AgentTurnError(
                AgentTurnErrorCode.EXECUTION_FAILED,
                "the Agent turn failed before it produced a terminal result",
                retryable=False,
                run_id=run_id,
            ) from exc

        status = _status_for(agent.terminal_reason)
        answer_context = AgentAnswerContext(
            message=turn_message,
            agent=agent,
            business_result=session.latest_business_tool_result,
        )
        fallback_used = False
        try:
            rendered = self.answer_renderer(answer_context)
            if not isinstance(rendered, str) or not rendered.strip():
                raise ValueError("answer renderer returned no text")
        except Exception as exc:
            fallback_used = True
            ledger.append("answer_render_failed", error_type=type(exc).__name__)
            rendered = render_neutral_user_answer(answer_context)
        user_answer = redact_sensitive_assignments(
            redact_session_handoffs(rendered)).strip()[:self.config.max_user_answer_chars]
        if not user_answer:
            fallback_used = True
            user_answer = render_neutral_user_answer(answer_context)

        execution = AgentTurnExecution(
            schema_version=1,
            run_id=run_id,
            session_id=self.channel.session_id,
            message=turn_message,
            entry_observation_id=current.observation_id,
            entry_observation_digest=current.observation.content_digest,
            status=status,
            terminal_reason=agent.terminal_reason,
            user_answer=user_answer,
            agent=agent,
            business_result=session.latest_business_tool_result,
            answer_fallback_used=fallback_used,
            started_at=started_at,
            finished_at=time.time(),
        )
        ledger.append(
            "product_turn_finished",
            status=status.value,
            terminal_reason=agent.terminal_reason,
            answer_fallback_used=fallback_used,
            business_result_digest=(
                session.latest_business_tool_result.content_digest
                if session.latest_business_tool_result is not None else ""),
        )
        try:
            self.channel.publish("progress", {
                "state": status.value,
                "terminal_reason": agent.terminal_reason,
                "message": user_answer[:800],
            })
        except Exception as exc:
            # The browser or event transport may disappear after a write or after the
            # Agent has already finished. Delivery loss cannot erase the execution or
            # make it retryable.
            ledger.append(
                "terminal_delivery_lost", status=status.value,
                error_type=type(exc).__name__)
        return execution


def _status_for(terminal_reason: str) -> AgentTurnStatus:
    if terminal_reason == "agent_finished":
        return AgentTurnStatus.FINISHED
    if terminal_reason == "agent_refused":
        return AgentTurnStatus.REFUSED
    if terminal_reason == "approval_denied":
        return AgentTurnStatus.CANCELLED
    if terminal_reason == "indeterminate_write":
        return AgentTurnStatus.INDETERMINATE
    return AgentTurnStatus.FAILED
