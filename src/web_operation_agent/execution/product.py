"""Application-neutral execution of one server Agent turn.

The surrounding application chooses the model, tool catalog, control implementation,
scope, and presentation of structured tool facts.  This module binds that choice to the
latest browser observation and runs the existing Agent loop.  It reports what the loop did;
it does not decide whether an application-level goal was achieved.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from ..composition.sources import SourceCapability
from ..goal.read_tools import DataReadTools
from .click_policy import ClickPolicy
from .action_admission import ApplicationActionPolicy
from ..answer.answering import render_structured_query_answer
from ..answer.answer_view import render_payload
from ..business.runtime import BusinessToolResult
from ..schemas import BROWSER_ACTIONS, POTENTIALLY_WRITING_ACTIONS, PageObservation
from ..transport.panel_view import panel_limits
from ..transport.protocol import RemoteProtocolError, ensure_safe_browser_payload
from .session import RunResult
from .page_context import DEFAULT_KEEP_OBSERVATIONS


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
    #: Both model requests (including planning and retries) and loop decisions are bounded
    #: by this limit. A completed last request may still dispatch its chosen tool.
    step_budget: int = 8
    wall_budget_s: int = 240
    transport_timeout_s: float = 60.0
    # Fail closed for every browser action that can initiate an application write. An
    # integration opts into a reviewed subset by supplying its complete disabled set.
    disabled_actions: frozenset[str] = POTENTIALLY_WRITING_ACTIONS
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
    #: Letting the assistant work the page waits longer than a write: working the page is
    #: reversible, and the person may be finishing something on the page first.
    operation_approval_ttl_s: int = 300
    keep_observations: int = DEFAULT_KEEP_OBSERVATIONS
    allow_application_entry: bool = False
    allow_business_mutations: bool = False
    allow_clarification: bool = True
    #: Permit application-issued form drafts and, when the adapter installs its optional
    #: form port, the generic current-page describe/prepare flow. Direct model
    #: fill/select/submit policy remains independent and may stay disabled.
    allow_form_prefill: bool = False
    #: Application-owned, read-only-window facet choices; off for every integration by default.
    allow_page_facets: bool = False
    page_facet_policy: Callable[[PageObservation], bool] | None = None
    #: Let a deployment remove model-only bookends from pure read navigation. The session
    #: still requires one exact read-only Skill match, a current typed route, and a fresh
    #: browser receipt at that route; this flag grants neither a route nor a mutation.
    deterministic_read_navigation: bool = False
    #: Skill first: a request whose wording matched exactly one read-only Business Skill
    #: starts with that Skill loaded -- playbook, routes and citable statements -- and the
    #: prompt tells the model to use it before source. Source stays the fallback for what
    #: no Skill covers. A write Skill is never loaded this way.
    skill_first: bool = True
    #: Ask the person, once per turn and before the first page operation, to let the
    #: assistant work the page: opening pages, clicking, filling and choosing. Declining it
    #: ends the turn with the page untouched; so does no answer. Changing data is still
    #: asked about change by change, whatever the answer here. Working the page can discard
    #: what was typed there and loses the page's own state, so it is on by default.
    confirm_page_operations: bool = True
    require_identity: bool = False
    #: Refresh the Agent's own observation before resolving data reads. A page
    #: query hint is not current merely because its declaration was current.
    observe_before_reads: bool = False
    #: Start with only the request and registered tools. Browser context and its control
    #: lease are acquired on the first page tool; repository/API answers need neither.
    defer_page_context: bool = False
    #: Optional actor-bound access to the existing readonly deterministic resolver.
    #: It shares this Agent, evidence store and budget; no nested model execution.
    data_reads: DataReadTools | None = None
    #: A step-by-step page turn may require the application's rendered main-page
    #: identity before reporting arrival; URL and selected controls alone cannot prove it.
    require_page_route: bool = False
    #: Freeze request clauses before results; require explicit evidence coverage of
    #: every declared question beside any observed action. Opt-in qualification.
    require_task_contract: bool = False
    deployment_profile: str = "production"
    #: Where the person is, as an IANA zone name, so "today" and "this month" mean their
    #: days. Empty leaves the turn on UTC and says so. An application that knows its user's
    #: zone (Odoo keeps one per user) should pass that one, so answers agree with what the
    #: application itself shows.
    time_zone: str = ""
    max_user_answer_chars: int = 4_000
    #: What this deployment's compiled source lets the turn do, as one value handed over by
    #: the application distribution. It is a capability rather than a set of flags because
    #: the flags only make sense together, and because a turn that spreads them can pick
    #: three of four -- which is exactly what this field replaced: the product turn built a
    #: `SourceCapability` of its own out of two of the adapter's four settings, so an
    #: adapter that declared cited runtime lookup got a deployment without it.
    source: SourceCapability = SourceCapability.none()
    #: An application's decision, per page and per element, to allow a click the action
    #: policy otherwise disables. See `execution.click_policy`. None keeps click as
    #: `disabled_actions` says.
    click_policy: ClickPolicy | None = None
    #: Optional application effects for generic browser tools. This replaces the
    #: legacy click policy, requires a guarded actor-bound session, and verifies results.
    action_policy: ApplicationActionPolicy | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ledger_path", Path(self.ledger_path))
        if self.action_policy is not None:
            if not isinstance(self.action_policy, ApplicationActionPolicy):
                raise TypeError("action_policy must implement ApplicationActionPolicy")
            if not self.require_identity or self.click_policy is not None:
                raise ValueError("action policy requires identity and replaces click_policy")
            if self.allow_page_facets:
                raise ValueError("action policy requires reviewed query tools, not the legacy facet policy")
        if self.click_policy is not None:
            if not isinstance(self.click_policy, ClickPolicy):
                raise TypeError("click_policy must implement ClickPolicy")
            if "click" not in self.disabled_actions:
                raise ValueError("a click policy only narrows a disabled click")
        if self.allow_page_facets and not callable(self.page_facet_policy):
            raise ValueError("page facets require an application page policy")
        if self.allow_page_facets and "apply_facet" in self.disabled_actions:
            raise ValueError("enabled page facets cannot disable apply_facet")
        if not isinstance(self.supported_scope, str) or not self.supported_scope.strip():
            raise ValueError("supported_scope must be a non-empty string")
        if (not isinstance(self.scope_first_policy, bool)
                or not isinstance(self.page_answer_evidence, bool)
                or not isinstance(self.allow_application_entry, bool)
                or not isinstance(self.allow_business_mutations, bool)
                or not isinstance(self.allow_clarification, bool)
                or not isinstance(self.allow_form_prefill, bool)
                or not isinstance(self.allow_page_facets, bool)
                or not isinstance(self.deterministic_read_navigation, bool)
                or not isinstance(self.skill_first, bool)
                or not isinstance(self.confirm_page_operations, bool)
                or not isinstance(self.require_identity, bool)
                or not isinstance(self.observe_before_reads, bool)
                or not isinstance(self.defer_page_context, bool)
                or not isinstance(self.require_page_route, bool)
                or not isinstance(self.require_task_contract, bool)):
            raise ValueError("turn policy flags must be booleans")
        for name in ("step_budget", "wall_budget_s", "approval_ttl_s",
                     "operation_approval_ttl_s", "keep_observations",
                     "max_user_answer_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("approval_ttl_s", "operation_approval_ttl_s"):
            if getattr(self, name) > 600:
                raise ValueError(f"{name} cannot exceed 600 seconds")
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
        unknown_disabled = self.disabled_actions - BROWSER_ACTIONS
        if unknown_disabled:
            raise ValueError(
                f"disabled actions are not browser tools: {sorted(unknown_disabled)}")
        if "observe" in self.disabled_actions:
            raise ValueError("observe cannot be disabled: policy refusals need a fresh page")
        if not isinstance(self.source, SourceCapability):
            raise TypeError("source must be a SourceCapability")
        if self.data_reads is not None and not isinstance(self.data_reads, DataReadTools):
            raise TypeError("data_reads must be DataReadTools or None")
        if self.data_reads is not None and not self.require_identity:
            raise ValueError("deterministic data reads require a verified session identity")
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


#: What a turn that ended without an answer says, per status. In the reader's language:
#: these were English only, so a Chinese conversation stopped with the Stop button, or a
#: declined write, ended on "The requested action was cancelled."
_TERMINAL_FALLBACK = {
    AgentTurnStatus.FINISHED: ("这一轮已结束，但没有给出回答。",
                               "The agent finished the turn without a user-facing answer."),
    AgentTurnStatus.NEEDS_INPUT: ("需要更多信息才能继续。",
                                  "The agent needs more information to continue."),
    AgentTurnStatus.REFUSED: ("这个请求没能完成。", "The agent could not complete the request."),
    AgentTurnStatus.CANCELLED: ("已取消。", "The requested action was cancelled."),
    AgentTurnStatus.INDETERMINATE: (
        "这次操作的结果不确定，没有自动重试。",
        "The action outcome is uncertain. It was not retried automatically."),
    AgentTurnStatus.FAILED: ("任务在完成前就结束了。",
                             "The task ended before the agent could finish it."),
}


def _fallback(status: AgentTurnStatus, text: str) -> str:
    chinese, english = _TERMINAL_FALLBACK[status]
    return chinese if re.search(r"[\u3400-\u9fff]", text) else english


def render_neutral_user_answer(context: AgentAnswerContext) -> str:
    """Prefer model prose and otherwise describe the terminal state without a success claim."""
    status = _status_for(context.agent.terminal_reason)
    if status is AgentTurnStatus.INDETERMINATE:
        return _fallback(status, context.message.text)
    if context.agent.terminal_reason == "provider_error":
        chinese = bool(re.search(r"[\u3400-\u9fff]", context.message.text))
        code = context.agent.provider_error_code or "provider_error"
        if code == "provider_rate_limited":
            return ("模型服务当前请求过多，请稍后重试。" if chinese else
                    "The model service is rate limited. Try again shortly.")
        if code == "provider_allowance_spent":
            return ("你今天的助手使用额度已用完，明天会自动恢复。" if chinese else
                    "Your assistant allowance for today is used up; it starts again tomorrow.")
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
        refused = _plain(context.agent.refused_operation or "")
        if re.search(r"[\u3400-\u9fff]", context.message.text):
            if prior_write_completed:
                return ("本轮先前的操作已经完成，但后续操作因权限不足没有执行。"
                        "请先核对已完成的结果；如需继续，请联系管理员授予权限。")
            if refused:
                return (f"当前账号没有{refused}的权限，这项操作没有执行。"
                        "同一个问题里其他可以读取的部分，可以单独再问一次；"
                        "如确实需要这项权限，请联系管理员授予。")
            return ("当前账号没有执行这项操作的权限，这项操作没有执行。"
                    "如确实需要，请联系管理员授予权限。")
        if prior_write_completed:
            return ("An earlier action in this task completed, but the later operation was "
                    "not permitted and was not carried out. Review the completed result; "
                    "ask an administrator for access if you need to continue.")
        if refused:
            return (f"Your account is not permitted to {refused}, so that was not carried "
                    "out. Ask separately for the part of the question it may read; ask an "
                    "administrator for access if you need this one.")
        return ("Your account is not permitted to perform this operation, so that operation "
                "was not carried out. Ask an administrator for access if you need it.")
    if context.agent.terminal_reason == "approval_failed":
        if re.search(r"[\u3400-\u9fff]", context.message.text):
            return "本次确认已失效或与当前操作不再匹配，因此没有执行写入。请核对后重新发送。"
        return ("This confirmation expired or no longer matched the current action, so no "
                "write was performed. Review the target and submit it again.")
    if context.agent.terminal_reason == "approval_expired":
        # A card nobody answered in time ends the turn as a stop does, with nothing more to
        # say: the owner's ruling is that a timeout interrupts the message, not explains it.
        return _fallback(AgentTurnStatus.CANCELLED, context.message.text)
    if context.agent.terminal_reason == "operation_not_confirmed":
        if re.search(r"[\u3400-\u9fff]", context.message.text):
            return "已取消，助手没有动这个页面。"
        return "Cancelled. The assistant left the page as it was."
    if context.agent.terminal_reason == "no_progress":
        if re.search(r"[\u3400-\u9fff]", context.message.text):
            return "最近几步没有找到新信息或可确认的页面变化，我已停止重复操作。可以说得更具体些，或缩小查询范围。"
        return ("The recent steps produced no new information or confirmed page change, "
                "so I stopped repeating them. You can clarify the goal or narrow the search.")
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
    return _fallback(status, context.message.text)


@dataclass(frozen=True, slots=True)
class AgentTurnExecution:
    schema_version: int
    run_id: str
    session_id: str
    message: AgentTurnMessage
    entry_observation_id: str | None
    entry_observation_digest: str | None
    status: AgentTurnStatus
    terminal_reason: str
    user_answer: str
    agent: RunResult
    business_result: BusinessToolResult | None
    answer_fallback_used: bool
    started_at: float
    finished_at: float
    answer_targets: tuple = ()

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
            "answer_targets": [target.to_json() for target in self.answer_targets],
            "clarification": public_clarification,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _status_for(terminal_reason: str) -> AgentTurnStatus:
    if terminal_reason == "agent_finished":
        return AgentTurnStatus.FINISHED
    if terminal_reason in {"needs_input", "needs_clarification"}:
        return AgentTurnStatus.NEEDS_INPUT
    if terminal_reason == "agent_refused":
        return AgentTurnStatus.REFUSED
    if terminal_reason in {"business_mutation_blocked", "business_mutation_refused",
                           "permission_denied"}:
        return AgentTurnStatus.REFUSED
    if terminal_reason == "no_progress":
        return AgentTurnStatus.REFUSED
    if terminal_reason in {"approval_denied", "approval_expired", "user_cancelled",
                           "operation_not_confirmed"}:
        return AgentTurnStatus.CANCELLED
    if terminal_reason == "indeterminate_write":
        return AgentTurnStatus.INDETERMINATE
    return AgentTurnStatus.FAILED


# The product module remains the public wire-contract surface.  Turn composition lives in
# ``execution.turn`` so binding, session construction, and terminal settlement can evolve
# without growing the contract and presentation module again.
from .turn import AgentTurnExecutor  # noqa: E402
