"""Compose and settle one bounded application turn.

This module owns the operational path: bind current browser state and identity, construct the
server Agent session, settle its answer, then publish the terminal projection.  The public
wire types and neutral-answer rules remain in :mod:`execution.product`.
"""
from __future__ import annotations

import re
import time
from typing import Callable
from urllib.parse import urlsplit

from ..composition.binding import BoundApplication
from ..answer.answering import render_structured_query_answer
from ..answer.answer_view import render_sources
from ..transport.browser import RemoteBrowserClient, TransportError
from ..business.mutation import MutationJournalDurability
from ..business.runtime import BusinessToolRuntime
from ..business.forms import NoApplicationForms
from ..state.conversation import ConversationState
from ..business.control import ApprovalGate, ApprovalGrant
from ..state.identity import OperationContext, PrincipalError
from ..state.ledger import SessionLedger
from ..modeling.chat import require_chat_port
from ..transport.panel_view import describe_step, step_facts
from ..privacy import redact_sensitive_assignments, redact_session_handoffs
from ..transport.channel import RemoteSessionChannel
from ..transport.protocol import RemoteProtocolError
from .product import (
    AgentAnswerContext, AgentTurnConfig, AgentTurnError, AgentTurnErrorCode,
    AgentTurnExecution, AgentTurnMessage, AgentTurnStatus, _status_for,
    render_neutral_user_answer,
)
from .session import ServerAgentSession, TaskSpec


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


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

    def _publish_step(self, facts: dict, turn_id: str) -> None:
        """Put one step of the running turn on the timeline, or nothing if it has no line.

        Page commands already publish an `action` event of their own, so `describe_step`
        returns an empty string for those and this publishes nothing rather than saying
        the same dispatch twice.
        """
        text = describe_step(facts)
        if not text:
            return
        self.channel.publish(
            "progress",
            {"state": "step", "message": text, "step": step_facts(facts)},
            turn_id=turn_id)

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
            click_policy=self.config.click_policy,
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
            allow_page_facets=self.config.allow_page_facets,
            page_facet_policy=self.config.page_facet_policy,
            deterministic_read_navigation=(
                self.config.deterministic_read_navigation),
            skill_first=self.config.skill_first,
            # Only an application that declares a page write guard can have held one.
            page_write_guard=bool(
                self.application.descriptor.page_write_guard
                if self.application is not None else ""),
            plain_business_answer_min_page_actions=(
                self.config.plain_business_answer_min_page_actions),
            max_agent_said_chars=self.config.max_user_answer_chars,
            allow_application_entry=self.config.allow_application_entry,
            application_routes=(
                self.application.ports.routes if self.application is not None else None),
            application_forms=(
                self.application.ports.forms
                if (self.application is not None
                    and not isinstance(self.application.ports.forms, NoApplicationForms)
                    and (not self.application.ports.forms.required_scopes
                         or (principal is not None
                             and self.application.ports.forms.required_scopes
                             <= frozenset(principal.scopes))))
                else None),
            cancelled=lambda: self.channel.message_cancelled(turn_message.message_id),
            principal=principal,
            session_id=(self.channel.session_id if principal is not None else ""),
            turn_id=(turn_message.message_id if principal is not None else ""),
            time_zone=self.config.time_zone,
            conversation_context=self.conversation_state.model_context(),
            # What the turn is doing, as it does it. Between two page actions the product
            # can spend half a minute inside model calls and typed reads, and the panel
            # used to stand on `正在理解目标` for all of it.
            progress=lambda facts: self._publish_step(facts, turn_message.message_id),
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
