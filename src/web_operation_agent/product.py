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

from .application import BoundApplication, SourceCapability
from .answering import render_structured_query_answer
from .answer_view import render_payload, render_sources
from .panel_view import panel_limits
from .browser import TransportError
from .business import BusinessToolResult, BusinessToolRuntime
from .conversation import ConversationState
from .embedded_agent import ApprovalGate, ApprovalGrant
from .identity import OperationContext, PrincipalError
from .ledger import SessionLedger
from .model import require_chat_port
from .mutation import MutationJournalDurability
from .privacy import redact_sensitive_assignments, redact_session_handoffs
from .remote import (RemoteBrowserClient, RemoteProtocolError,
                     RemoteSessionChannel, ensure_safe_browser_payload)
from .session import RunResult, ServerAgentSession, TaskSpec


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class AgentTurnStatus(str, Enum):
    """A transport-level description of how the Agent loop ended."""

    FINISHED = "finished"
    NEEDS_INPUT = "needs_input"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"
    FAILED = "failed"


class AgentTurnErrorCode(str, Enum):
    INVALID_MESSAGE = "invalid_message"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    STALE_MESSAGE = "stale_message"
    INVALID_RUN_ID = "invalid_run_id"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    APPLICATION_MISMATCH = "application_mismatch"
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
    page_answer_evidence: bool = True
    #: How many pre-dispatch action-policy refusals a turn survives. The product already
    #: writes the model a recovery message naming the refused tool; a limit of one ended
    #: the turn before that message could be read, so a deployment that disabled any page
    #: action lost every turn in which the model reached for one. Two recoveries is enough
    #: to tell a model that adapts from one that keeps retrying.
    policy_block_limit: int | None = 3
    no_progress_repeat_limit: int | None = 2
    plain_business_answer_min_page_actions: int | None = None
    approval_ttl_s: int = 120
    keep_observations: int = 3
    allow_application_entry: bool = False
    allow_business_mutations: bool = False
    allow_clarification: bool = True
    #: Permit only application-issued, transient-until-submit form drafts. Direct model
    #: fill/select/submit policy remains independent and may stay disabled.
    allow_form_prefill: bool = False
    #: Let a deployment remove model-only bookends from pure read navigation. The session
    #: still requires one exact read-only Skill match, a current typed route, and a fresh
    #: browser receipt at that route; this flag grants neither a route nor a mutation.
    deterministic_read_navigation: bool = False
    #: Ask the person before the assistant leaves the current page, with one confirmation
    #: and no decline (owner ruling D7). Leaving a page can discard what was typed there or
    #: let the application save it on the way out, and loses the page's state. On by
    #: default; a person who does not want the switch stops the task.
    confirm_navigation: bool = True
    require_identity: bool = False
    deployment_profile: str = "production"
    max_user_answer_chars: int = 4_000
    #: What this deployment's compiled source lets the turn do, as one value handed over by
    #: the application distribution. It is a capability rather than a set of flags because
    #: the flags only make sense together, and because a turn that spreads them can pick
    #: three of four -- which is exactly what this field replaced: the product turn built a
    #: `SourceCapability` of its own out of two of the adapter's four settings, so an
    #: adapter that declared cited runtime lookup got a deployment without it.
    source: SourceCapability = SourceCapability.none()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ledger_path", Path(self.ledger_path))
        if not isinstance(self.supported_scope, str) or not self.supported_scope.strip():
            raise ValueError("supported_scope must be a non-empty string")
        if (not isinstance(self.scope_first_policy, bool)
                or not isinstance(self.page_answer_evidence, bool)
                or not isinstance(self.allow_application_entry, bool)
                or not isinstance(self.allow_business_mutations, bool)
                or not isinstance(self.allow_clarification, bool)
                or not isinstance(self.allow_form_prefill, bool)
                or not isinstance(self.deterministic_read_navigation, bool)
                or not isinstance(self.confirm_navigation, bool)
                or not isinstance(self.require_identity, bool)):
            raise ValueError("turn policy flags must be booleans")
        for name in ("step_budget", "wall_budget_s", "approval_ttl_s",
                     "keep_observations", "max_user_answer_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.approval_ttl_s > 600:
            raise ValueError("approval_ttl_s cannot exceed 600 seconds")
        if self.max_user_answer_chars > panel_limits()["result_max_chars"]:
            raise ValueError("max_user_answer_chars cannot exceed "
                             f"{panel_limits()['result_max_chars']}")
        if self.max_user_answer_chars < 512:
            raise ValueError("max_user_answer_chars must be at least 512")
        if (isinstance(self.transport_timeout_s, bool)
                or not isinstance(self.transport_timeout_s, (int, float))
                or not math.isfinite(self.transport_timeout_s)
                or self.transport_timeout_s <= 0):
            raise ValueError("transport_timeout_s must be a positive finite number")
        if not isinstance(self.disabled_actions, frozenset) \
                or any(not isinstance(item, str) for item in self.disabled_actions):
            raise ValueError("disabled_actions must be a frozenset of tool names")
        if not isinstance(self.source, SourceCapability):
            raise TypeError("source must be a SourceCapability")
        if self.deployment_profile not in {"production", "test"}:
            raise ValueError("deployment_profile must be production or test")
        if self.allow_business_mutations and not self.require_identity:
            raise ValueError(
                "business mutations require a verified session identity")
        if self.policy_block_limit is not None and (
                isinstance(self.policy_block_limit, bool)
                or not isinstance(self.policy_block_limit, int)
                or self.policy_block_limit < 1):
            raise ValueError("policy_block_limit must be a positive integer or None")
        if self.no_progress_repeat_limit is not None and (
                isinstance(self.no_progress_repeat_limit, bool)
                or not isinstance(self.no_progress_repeat_limit, int)
                or self.no_progress_repeat_limit < 2):
            raise ValueError(
                "no_progress_repeat_limit must be at least two or None")
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
    max_chars: int


def _plain(value: object) -> str:
    """An application value placed in a fixed sentence: one line, no markup it did not write."""
    text = " ".join(str(value or "").split())[:120]
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", text)


def render_neutral_user_answer(context: AgentAnswerContext) -> str:
    """Prefer model prose and otherwise describe the terminal state without a success claim."""
    status = _status_for(context.agent.terminal_reason)
    if status is AgentTurnStatus.INDETERMINATE:
        return "The action outcome is uncertain. It was not retried automatically."
    if context.agent.terminal_reason == "provider_error":
        chinese = bool(re.search(r"[\u3400-\u9fff]", context.message.text))
        code = context.agent.provider_error_code or "provider_error"
        if code == "provider_rate_limited":
            return ("模型服务当前请求过多，请稍后重试。" if chinese else
                    "The model service is rate limited. Try again shortly.")
        if code == "provider_authentication":
            return ("模型服务配置无效，请联系管理员检查凭据。" if chinese else
                    "The model service credentials are invalid. Contact the administrator.")
        if code in {"provider_unavailable", "provider_transport",
                    "provider_circuit_open", "provider_capacity"}:
            return ("模型服务暂时不可用，请稍后重试。" if chinese else
                    "The model service is temporarily unavailable. Try again shortly.")
        return ("模型服务未能完成本次请求，请稍后重试。" if chinese else
                "The model service could not complete this request. Try again shortly.")
    if context.agent.terminal_reason == "business_mutation_blocked":
        if re.search(r"[\u3400-\u9fff]", context.message.text):
            return "同一业务对象已有正在执行或结果待确认的操作。本次没有再次提交，请先核对上一笔操作。"
        return ("An operation for the same business object is still running or unresolved. "
                "Nothing was submitted again; check the earlier operation first.")
    if context.agent.terminal_reason == "business_mutation_refused":
        # Each rule the application can refuse by gets its own sentence. "A business rule
        # refused this operation" is true and useless: the reader cannot tell whether to
        # change the request, change the record, or stop.
        if re.search(r"[\u3400-\u9fff]", context.message.text):
            if context.agent.detail == "duplicate":
                return "已存在同名或同邮箱记录，本次没有重复创建。"
            if context.agent.detail == "nothing_to_invoice":
                subject = _plain(context.agent.refusal_subject)
                return (f"销售订单 {subject} 没有可开票的内容，本次没有执行任何更改。" if subject
                        else "这张订单没有可开票的内容，本次没有执行任何更改。")
            return "业务规则不允许执行这项操作，本次没有提交任何更改。"
        if context.agent.detail == "duplicate":
            return "A record with the same name or email already exists. Nothing was created."
        if context.agent.detail == "nothing_to_invoice":
            subject = _plain(context.agent.refusal_subject)
            return (f"Sales order {subject} has nothing left to invoice, so it was not executed."
                    if subject else
                    "That order has nothing left to invoice, so it was not executed.")
        return "A business rule refused this operation. No change was submitted."
    if context.agent.terminal_reason == "permission_denied":
        projection = (context.business_result.query_answer
                      if context.business_result is not None else None)
        if projection is not None:
            chinese = bool(re.search(r"[\u3400-\u9fff]", context.message.text))
            notice = (
                "> **已回答目前可以确认的部分**\n"
                "> 后续读取因权限不足没有执行；以上只包含本轮此前成功读取的结果。"
                if chinese else
                "> **Answered the part supported by current data**\n"
                "> A later read was not permitted and was not carried out; the result above "
                "contains only data read successfully earlier in this task.")
            remaining = context.max_chars - len(notice) - 2
            if remaining >= 512:
                earlier = render_structured_query_answer(
                    projection, user_text=context.message.text,
                    max_chars=remaining).text
                return earlier + "\n\n" + notice
            return notice
        prior_write_completed = bool(
            context.agent.business_control.get("mutation_completed", 0))
        if re.search(r"[\u3400-\u9fff]", context.message.text):
            if prior_write_completed:
                return ("本轮先前的操作已经完成，但后续操作因权限不足没有执行。"
                        "请先核对已完成的结果；如需继续，请联系管理员授予权限。")
            return ("当前账号没有执行这项操作的权限，这项操作没有执行。"
                    "如确实需要，请联系管理员授予权限。")
        if prior_write_completed:
            return ("An earlier action in this task completed, but the later operation was "
                    "not permitted and was not carried out. Review the completed result; "
                    "ask an administrator for access if you need to continue.")
        return ("Your account is not permitted to perform this operation, so that operation "
                "was not carried out. Ask an administrator for access if you need it.")
    if context.agent.terminal_reason == "approval_failed":
        if re.search(r"[\u3400-\u9fff]", context.message.text):
            return "本次确认已失效或与当前操作不再匹配，因此没有执行写入。请核对当前目标后重新提交。"
        return ("This confirmation expired or no longer matched the current action, so no "
                "write was performed. Review the target and submit it again.")
    if context.agent.terminal_reason == "approval_expired":
        if re.search(r"[\u3400-\u9fff]", context.message.text):
            return "本次确认已过期，因此没有执行写入。请核对当前目标后重新提交。"
        return ("This confirmation expired, so no write was performed. Review the target "
                "and submit it again.")
    if context.agent.terminal_reason == "navigation_not_confirmed":
        if re.search(r"[\u3400-\u9fff]", context.message.text):
            return "页面切换没有得到确认，助手没有离开当前页面。需要时请再说一次。"
        return ("The page switch was not confirmed, so the assistant stayed on the current "
                "page. Ask again when you are ready.")
    if context.agent.terminal_reason == "no_progress":
        if re.search(r"[\u3400-\u9fff]", context.message.text):
            return "同一步骤没有带来新结果，我已停止重复操作。请补充目标信息后重试。"
        return ("The same step produced no new result, so I stopped repeating it. "
                "Add more detail to the goal and try again.")
    if status is AgentTurnStatus.FINISHED and context.agent.verified_answer is not None:
        return render_payload(context.agent.verified_answer, user_text=context.message.text,
                              max_chars=context.max_chars)
    projection = (context.business_result.query_answer
                  if context.business_result is not None else None)
    if (status is AgentTurnStatus.FINISHED
            and context.agent.business_answer_projected and projection is not None):
        return render_structured_query_answer(
            projection, user_text=context.message.text,
            max_chars=context.max_chars).text
    if status is AgentTurnStatus.NEEDS_INPUT and context.agent.clarification is not None:
        clarification = context.agent.clarification
        question = str(clarification["question"])
        choices = clarification.get("choices") or ()
        return "\n".join([question, *(f"{index}. {choice['label']}"
                                      for index, choice in enumerate(choices, 1))])
    if context.agent.agent_said.strip():
        return context.agent.agent_said.strip()
    return {
        AgentTurnStatus.FINISHED: "The agent finished the turn without a user-facing answer.",
        AgentTurnStatus.NEEDS_INPUT: "The agent needs more information to continue.",
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
        clarification = self.agent.clarification
        public_clarification = None
        if clarification is not None:
            public_clarification = {
                key: clarification[key] for key in (
                    "schema_version", "clarification_id", "task_id", "question",
                    "response_kind")
            }
            public_clarification["choices"] = [
                {"choice_id": item["choice_id"], "label": item["label"]}
                for item in clarification.get("choices") or ()]
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
                "answer_projection": (
                    business.query_answer.to_json()
                    if (self.agent.business_answer_projected
                        and business.query_answer is not None) else None),
            } if business is not None else None),
            "answer_fallback_used": self.answer_fallback_used,
            "clarification": public_clarification,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


RunIdFactory = Callable[[AgentTurnMessage], str]
AnswerRenderer = Callable[[AgentAnswerContext], str]
#: Called as ``factory(path, run_id, base=...)``. The base mapping holds the session, turn
#: and tenant a record belongs to; a factory that drops it produces an audit trail nobody can
#: scope to an incident, so the argument is required rather than optional.
LedgerFactory = Callable[..., SessionLedger]


def _require_egress_covers_application(chat, application: BoundApplication | None) -> None:
    """An adapter's session parameters must reach the outbound provider boundary.

    `privacy.py` ships a static baseline that names no particular application. An adapter
    that declares `session_handoff_query_keys` is saying "my application also carries its
    session in these"; if the provider egress policy has not been told, the boundary is
    silently blind to exactly the secret the adapter warned about. Checked at composition,
    like the ChatPort shape, because a deployment that forgot would otherwise only find out
    by leaking.
    """
    if application is None:
        return
    declared = frozenset(application.ports.routes.session_handoff_query_keys)
    if not declared:
        return
    policy = getattr(chat, "egress_policy", None)
    covered = frozenset(getattr(getattr(policy, "handoffs", None), "query_keys", ()))
    missing = sorted(declared - covered)
    if missing:
        raise ValueError(
            "the provider egress policy does not cover this application's declared session "
            f"parameters: {missing}. Compose ChatModel with "
            "ProviderEgressPolicy(session_handoff_keys=ports.routes.session_handoff_query_keys)")


class AgentTurnExecutor:
    """Execute one application-selected task without owning the channel lifecycle."""

    def __init__(self, *, channel: RemoteSessionChannel, config: AgentTurnConfig, chat,
                 business_tools: BusinessToolRuntime | None = None,
                 application: BoundApplication | None = None, control,
                 run_id_factory: RunIdFactory,
                 answer_renderer: AnswerRenderer = render_neutral_user_answer,
                 ledger_factory: LedgerFactory = SessionLedger,
                 conversation_state: ConversationState | None = None):
        self.channel = channel
        self.config = config
        # Checked here, not on the first model call: a wrong chat shape is a wiring error and
        # must surface where the executor is composed.
        self.chat = require_chat_port(chat, role="executor chat")
        if application is not None and not isinstance(application, BoundApplication):
            raise TypeError("application must be a BoundApplication or None")
        if application is not None and business_tools is not None:
            raise ValueError("pass either application or business_tools, not both")
        self.application = application
        self.business_tools = (application.business_tools
                               if application is not None else business_tools)
        if (application is not None and application.ports.identity.required
                and not self.config.require_identity):
            raise ValueError(
                "the application identity port requires require_identity=True")
        _require_egress_covers_application(self.chat, application)
        if self.config.allow_business_mutations:
            if self.business_tools is None or not self.business_tools.has_mutations:
                raise ValueError(
                    "business mutations require a configured mutation tool")
            if (self.config.deployment_profile == "production"
                    and self.business_tools.mutation_journal_durability
                    is not MutationJournalDurability.DURABLE):
                raise ValueError(
                    "production business mutations require a durable mutation journal")
        self.control = control
        self.run_id_factory = run_id_factory
        self.answer_renderer = answer_renderer
        self.ledger_factory = ledger_factory
        # No local coherence check: `SourceCapability.__post_init__` is the one place that
        # refuses an incoherent combination, and a second copy here is how the two drift.
        if conversation_state is not None and not isinstance(
                conversation_state, ConversationState):
            raise TypeError("conversation_state must be a ConversationState or None")
        self.conversation_state = conversation_state or ConversationState()

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
        # "New conversation" means the model's reference history ends here. It is consumed
        # at the start of a turn rather than when the user clicks, so it can never land
        # between planning and execution of a turn already under way.
        dropped_on_new_conversation = (self.conversation_state.clear()
                            if self.channel.take_new_conversation() else -1)
        if self.application is not None:
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
        if self.application is not None:
            try:
                self.application.ports.identity.validate(
                    principal,
                    application_id=self.application.descriptor.application_id)
            except PrincipalError as exc:
                raise AgentTurnError(
                    AgentTurnErrorCode.IDENTITY_UNAVAILABLE,
                    "the session identity is invalid, expired, or belongs to another application",
                    retryable=False,
                ) from exc
        elif principal is not None:
            try:
                principal.require_live()
                application_id = (self.business_tools.application_id
                                  if self.business_tools is not None else "")
                principal.require_live(application_id=application_id or None)
            except PrincipalError as exc:
                raise AgentTurnError(
                    AgentTurnErrorCode.IDENTITY_UNAVAILABLE,
                    "the session identity is invalid, expired, or belongs to another application",
                    retryable=False,
                ) from exc
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
        ledger = self.ledger_factory(
            self.config.ledger_path, run_id,
            base={"session_id": self.channel.session_id,
                  "turn_id": turn_message.message_id,
                  # The same digest the identity projection already publishes, so a line
                  # can be scoped to a tenant without the ledger holding a tenant name.
                  "tenant_digest": (principal.audit_projection().get("tenant_digest", "")
                                    if principal is not None else "")})
        if dropped_on_new_conversation >= 0:
            ledger.append("new_conversation", dropped_turns=dropped_on_new_conversation)
        if principal is not None:
            ledger.append(
                "turn_identity_bound",
                message_id=turn_message.message_id,
                identity=principal.audit_projection(),
            )
        approval_gate = ApprovalGate(self.channel, ledger)
        browser = RemoteBrowserClient(
            self.channel, timeout_s=self.config.transport_timeout_s,
            turn_id=turn_message.message_id)
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

        def approve_business_mutation(
                proposal, *, context: OperationContext | None = None):
            try:
                return approval_gate.decide_plan(
                    proposal,
                    effect=proposal.effect,
                    targets=proposal.targets,
                    ttl_s=self.config.approval_ttl_s,
                    context=context,
                )
            except RemoteProtocolError as exc:
                if exc.code != "approval_expired":
                    raise
                return ApprovalGrant(
                    approval_id="", decision="expired",
                    plan_digest=proposal.content_digest, decided_at=time.time(),
                    operation_context_digest=(context.binding_digest if context else ""))

        def confirm_navigation(plan, *, context: OperationContext | None = None):
            chinese = bool(re.search(r"[\u3400-\u9fff]", turn_message.text))
            if plan.action == "back":
                destination = "上一页" if chinese else "the previous page"
            else:
                parts = urlsplit(plan.url) if plan.url else None
                destination = ((parts.path or "/") if parts is not None
                               else ("另一个页面" if chinese else "another page"))
            effect = ("离开当前页面，打开另一个页面" if chinese
                      else "Leave the current page and open another one")
            # The card goes to the browser, which refuses a credential-looking value; the
            # request is shown as the person would recognise it, with any such value masked.
            request = redact_sensitive_assignments(
                redact_session_handoffs(turn_message.text))[:120]
            targets = ((f"将打开：{destination}", f"你的请求：{request}")
                       if chinese else
                       (f"Opens: {destination}", f"Your request: {request}"))
            try:
                return approval_gate.decide_plan(
                    plan, effect=effect, targets=targets,
                    ttl_s=self.config.approval_ttl_s, context=context, kind="navigation")
            except RemoteProtocolError as exc:
                if exc.code != "approval_expired":
                    raise
                return ApprovalGrant(
                    approval_id="", decision="expired",
                    plan_digest=plan.content_digest, decided_at=time.time(),
                    operation_context_digest=(context.binding_digest if context else ""))

        session = ServerAgentSession(
            task=task,
            chat=self.chat,
            browser=browser,
            control=self.control,
            ledger=ledger,
            # No `lab=`: a deployed turn has no arm to define, and the one keyword that
            # would inject a known-good plan is not reachable from here at all.
            #
            # The capability goes across whole. Rebuilding one here out of selected fields
            # is what dropped `runtime_source` on the floor: the adapter declared cited
            # runtime lookup and the deployment never offered the tool.
            source=self.config.source,
            step_budget=self.config.step_budget,
            wall_budget_s=self.config.wall_budget_s,
            run_id=run_id,
            keep_observations=self.config.keep_observations,
            disabled_actions=self.config.disabled_actions,
            scope_first_policy=self.config.scope_first_policy,
            page_answer_evidence=self.config.page_answer_evidence,
            policy_block_limit=self.config.policy_block_limit,
            no_progress_repeat_limit=self.config.no_progress_repeat_limit,
            business_tools=self.business_tools,
            business_mutation_approval=(
                approve_business_mutation
                if self.config.allow_business_mutations else None),
            navigation_confirmation=(
                confirm_navigation if self.config.confirm_navigation else None),
            allow_clarification=self.config.allow_clarification,
            allow_form_prefill=self.config.allow_form_prefill,
            deterministic_read_navigation=(
                self.config.deterministic_read_navigation),
            plain_business_answer_min_page_actions=(
                self.config.plain_business_answer_min_page_actions),
            max_agent_said_chars=self.config.max_user_answer_chars,
            allow_application_entry=self.config.allow_application_entry,
            application_routes=(
                self.application.ports.routes if self.application is not None else None),
            cancelled=lambda: self.channel.message_cancelled(turn_message.message_id),
            principal=principal,
            session_id=(self.channel.session_id if principal is not None else ""),
            turn_id=(turn_message.message_id if principal is not None else ""),
            conversation_context=self.conversation_state.model_context(),
        )
        session.adopt_entry(current.observation)
        try:
            session.plan()
            # No prose here. What the timeline says for this state is a user-visible
            # string, and those live in the panel contract with the rest of them.
            self.channel.publish("progress", {"state": "agent_started"},
                                 turn_id=turn_message.message_id)
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
            max_chars=self.config.max_user_answer_chars,
        )
        fallback_used = False
        try:
            permission_partial = (
                agent.terminal_reason == "permission_denied"
                and session.latest_business_tool_result is not None
                and session.latest_business_tool_result.query_answer is not None)
            authoritative_projection = (
                session.latest_business_tool_result.query_answer
                if ((agent.business_answer_projected or permission_partial)
                    and session.latest_business_tool_result is not None) else None)
            # A server-trusted renderer may customize ordinary terminal prose. It cannot
            # replace facts already projected by a typed query with model-authored prose.
            rendered = (render_neutral_user_answer(answer_context)
                        if (authoritative_projection is not None
                            or agent.verified_answer is not None
                            or status is AgentTurnStatus.NEEDS_INPUT
                            or agent.terminal_reason in {
                                "approval_expired", "approval_failed",
                                "business_mutation_blocked", "business_mutation_refused",
                                "navigation_not_confirmed", "no_progress",
                                "permission_denied"})
                        else self.answer_renderer(answer_context))
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

        projection = authoritative_projection
        if projection is not None:
            projection_rendering = render_structured_query_answer(
                projection, user_text=turn_message.text,
                max_chars=self.config.max_user_answer_chars)
            ledger.append(
                "structured_query_answer_rendered",
                business_result_digest=session.latest_business_tool_result.content_digest,
                projection_digest=projection.content_digest,
                returned_records=len(projection.records),
                presented_records=projection_rendering.presented_records,
                presentation_truncated=projection_rendering.presentation_truncated,
                source_complete=projection.source_complete,
                result_truncated=projection.result_truncated,
                model_facts_used=False,
                partial_due_to_permission=(
                    agent.terminal_reason == "permission_denied"),
            )

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
        self.conversation_state.record(
            message_id=turn_message.message_id,
            user_text=turn_message.text,
            assistant_text=user_answer,
            status=status.value,
            business_result=session.latest_business_tool_result,
            clarification=agent.clarification,
        )
        ledger.append(
            "conversation_state_updated",
            retained_turns=self.conversation_state.turn_count,
            context_digest=self.conversation_state.content_digest,
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
            source_details = (render_sources(agent.verified_answer, user_text=turn_message.text,
                                            max_chars=self.config.max_user_answer_chars)
                              if status is AgentTurnStatus.FINISHED and agent.verified_answer else "")
            source_details = redact_sensitive_assignments(redact_session_handoffs(source_details))
            self.channel.publish("progress", {
                "state": status.value,
                "terminal_reason": agent.terminal_reason,
                "message": user_answer,
                **({"source_details": source_details} if source_details else {}),
            }, turn_id=turn_message.message_id)
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
    if terminal_reason == "needs_input":
        return AgentTurnStatus.NEEDS_INPUT
    if terminal_reason == "agent_refused":
        return AgentTurnStatus.REFUSED
    if terminal_reason in {"business_mutation_blocked", "business_mutation_refused",
                           "permission_denied"}:
        return AgentTurnStatus.REFUSED
    if terminal_reason == "no_progress":
        return AgentTurnStatus.REFUSED
    if terminal_reason in {"approval_denied", "approval_expired", "user_cancelled",
                           "navigation_not_confirmed"}:
        return AgentTurnStatus.CANCELLED
    if terminal_reason == "indeterminate_write":
        return AgentTurnStatus.INDETERMINATE
    return AgentTurnStatus.FAILED
