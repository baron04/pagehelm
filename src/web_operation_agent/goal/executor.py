"""A goal-directed turn: understand once, resolve, act, check, reply.

Same wire contract as `AgentTurnExecutor` -- a channel, a config, a chat, a bound
application -- and the same fail-closed edges: every page switch is confirmed by the
person, every page command runs under the write control the application declares, and no
control that could write is ever pressed. What differs is that the model is asked one
question and the rest of the turn is server code the audit can replay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
from typing import Callable
from urllib.parse import urlsplit

from ..business.approvals import ApprovalWatch, NavigationPlan
from ..business.control import ApprovalGate, ApprovalGrant
from ..business.writes import WriteWatch
from ..composition.binding import BoundApplication
from ..execution.product import (AgentAnswerContext, AgentTurnError, AgentTurnErrorCode,
                                 AgentTurnExecution, AgentTurnMessage, AgentTurnStatus,
                                 render_neutral_user_answer)
from ..execution.runstate import RunResult
from ..modeling.chat import require_chat_port
from ..modeling.egress import AuditedChat
from ..privacy import redact_sensitive_assignments, redact_session_handoffs
from ..schemas.browser import PageObservation
from ..transport.protocol import RemoteProtocolError
from ..state.conversation import ConversationState
from ..state.identity import PrincipalError
from ..state.ledger import SessionLedger
from ..transport.browser import RemoteBrowserClient, TransportError, TurnCancelled
from .ports import GoalPort, PagePlan, Resolved
from .spec import GoalSpec
from .understand import understand

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CJK = re.compile(r"[㐀-鿿]")
#: How long the page may take to settle after a switch, in half-second waits.
_READY_WAITS = 12
#: Attribute values a runtime uses to mark the current tab of a set.
_SELECTED_ATTRS = ("aria-selected", "selected", "aria-pressed", "aria-checked")
#: Element roles a handoff never highlights: navigation, not the control asked for.
_NO_HIGHLIGHT_ROLES = frozenset({"tab", "menuitem", "menu", "menubar", "tablist", "navigation"})


def _norm(text: str) -> str:
    return "".join(ch for ch in (text or "").casefold() if not ch.isspace()
                   ).replace("&amp;", "&")


def _path_of(url: str) -> str:
    return urlsplit(url).path or "/"


@dataclass
class _PageOutcome:
    reached: bool = True
    unmet: list[str] = field(default_factory=list)
    url: str = ""
    highlighted: str = ""


@dataclass
class _TurnState:
    """Everything one execute() call carries between its phases."""
    message: AgentTurnMessage
    run_id: str
    ledger: SessionLedger
    browser: RemoteBrowserClient
    writes: WriteWatch
    approvals: ApprovalWatch
    obs: PageObservation
    started_at: float
    step: int = 0
    page_actions: int = 0
    terminal_reason: str = ""
    detail: str = ""
    chinese: bool = False
    cancelled: Callable[[], bool] = lambda: False


class GoalTurnExecutor:
    """Execute one turn as a declared goal, without owning the channel lifecycle."""

    def __init__(self, *, channel, config, chat, application: BoundApplication,
                 goal_port: GoalPort, control, run_id_factory,
                 ledger_factory=SessionLedger,
                 conversation_state: ConversationState | None = None):
        self.channel = channel
        self.config = config
        self.chat = require_chat_port(chat, role="executor chat")
        if not isinstance(application, BoundApplication):
            raise TypeError("application must be a BoundApplication")
        self.application = application
        self.goal_port = goal_port
        self.control = control
        self.run_id_factory = run_id_factory
        self.ledger_factory = ledger_factory
        self.conversation_state = conversation_state or ConversationState()

    # -- binding ----------------------------------------------------------------------
    def execute(self, message: dict) -> AgentTurnExecution:
        turn_message = AgentTurnMessage.from_wire(message)
        dropped = (self.conversation_state.clear()
                   if self.channel.take_new_conversation() else -1)
        if not self.application.descriptor.owns_url(turn_message.page_url):
            raise AgentTurnError(AgentTurnErrorCode.APPLICATION_MISMATCH,
                                 "the task belongs to a different application origin",
                                 retryable=False)
        principal = getattr(self.channel, "principal", None)
        if principal is None and self.config.require_identity:
            raise AgentTurnError(AgentTurnErrorCode.IDENTITY_UNAVAILABLE,
                                 "this application requires a verified session identity",
                                 retryable=False)
        try:
            self.application.ports.identity.validate(
                principal, application_id=self.application.descriptor.application_id)
        except PrincipalError as exc:
            raise AgentTurnError(
                AgentTurnErrorCode.IDENTITY_UNAVAILABLE,
                "the session identity is invalid, expired, or belongs to another application",
                retryable=False) from exc
        entry = self.channel.latest_observation_envelope
        if entry is None:
            raise AgentTurnError(AgentTurnErrorCode.OBSERVATION_UNAVAILABLE,
                                 "the browser has not supplied a page observation",
                                 retryable=True)
        if turn_message.page_url != entry.observation.url:
            raise AgentTurnError(AgentTurnErrorCode.STALE_MESSAGE,
                                 "the task was submitted from a page other than the latest observation",
                                 retryable=True)
        run_id = self.run_id_factory(turn_message)
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise AgentTurnError(AgentTurnErrorCode.INVALID_RUN_ID,
                                 "run_id_factory returned an invalid run identifier",
                                 retryable=False)
        started_at = time.time()
        ledger = self.ledger_factory(
            self.config.ledger_path, run_id,
            base={"session_id": self.channel.session_id,
                  "turn_id": turn_message.message_id,
                  "tenant_digest": (principal.audit_projection().get("tenant_digest", "")
                                    if principal is not None else "")})
        if dropped >= 0:
            ledger.append("new_conversation", dropped_turns=dropped)
        if principal is not None:
            ledger.append("turn_identity_bound", message_id=turn_message.message_id,
                          identity=principal.audit_projection())
        browser = RemoteBrowserClient(self.channel, timeout_s=self.config.transport_timeout_s,
                                      turn_id=turn_message.message_id)
        try:
            browser.start()
        except TransportError as exc:
            ledger.append("turn_start_failed", error_type=type(exc).__name__)
            raise AgentTurnError(AgentTurnErrorCode.RUNTIME_UNAVAILABLE,
                                 "the browser runtime was unavailable for this turn",
                                 retryable=True, run_id=run_id) from exc
        current = self.channel.latest_observation_envelope
        if (current is None or current.observation_id != entry.observation_id
                or current.page_epoch != entry.page_epoch
                or current.observation.content_digest != entry.observation.content_digest):
            ledger.append("turn_binding_rejected", message_id=turn_message.message_id)
            raise AgentTurnError(AgentTurnErrorCode.STALE_MESSAGE,
                                 "the page changed while the turn was being bound",
                                 retryable=True, run_id=run_id)
        gate = ApprovalGate(self.channel, ledger)
        cancelled = lambda: self.channel.message_cancelled(turn_message.message_id)  # noqa: E731

        def confirm_navigation(plan: NavigationPlan, *, context=None) -> ApprovalGrant:
            chinese = bool(_CJK.search(turn_message.text))
            destination = _path_of(plan.url) if plan.url else (
                "另一个页面" if chinese else "another page")
            effect = ("离开当前页面，打开另一个页面" if chinese
                      else "Leave the current page and open another one")
            request = redact_sensitive_assignments(
                redact_session_handoffs(turn_message.text))[:120]
            targets = ((f"将打开：{destination}", f"你的请求：{request}") if chinese
                       else (f"Opens: {destination}", f"Your request: {request}"))
            try:
                return gate.decide_plan(plan, effect=effect, targets=targets,
                                        ttl_s=self.config.approval_ttl_s, context=context,
                                        kind="navigation")
            except RemoteProtocolError as exc:
                if exc.code != "approval_expired":
                    raise
                return ApprovalGrant(approval_id="", decision="expired",
                                     plan_digest=plan.content_digest, decided_at=time.time(),
                                     operation_context_digest=(
                                         context.binding_digest if context else ""))

        approvals = ApprovalWatch(
            ledger=ledger, run_id=run_id, principal=principal,
            session_id=(self.channel.session_id if principal is not None else ""),
            turn_id=(turn_message.message_id if principal is not None else ""),
            business_approval=None,
            navigation_confirmation=(confirm_navigation if self.config.confirm_navigation
                                     else None),
            cancelled=cancelled)
        writes = WriteWatch(control=self.control, ledger=ledger, run_id=run_id,
                            write_guard_declared=bool(self.application.descriptor.page_write_guard))
        state = _TurnState(message=turn_message, run_id=run_id, ledger=ledger,
                           browser=browser, writes=writes, approvals=approvals,
                           obs=current.observation, started_at=started_at,
                           chinese=bool(_CJK.search(turn_message.text)),
                           cancelled=cancelled)
        self.channel.publish("progress", {"state": "agent_started"},
                             turn_id=turn_message.message_id)
        try:
            text = self._run(state)
        except AgentTurnError:
            raise
        except Exception as exc:
            ledger.append("turn_execution_failed", error_type=type(exc).__name__)
            writes.end_control()
            raise AgentTurnError(AgentTurnErrorCode.EXECUTION_FAILED,
                                 "the goal turn failed before it produced a terminal result",
                                 retryable=False, run_id=run_id) from exc
        return self._settle(state, current, text)

    # -- the turn ---------------------------------------------------------------------
    def _run(self, s: _TurnState) -> str:
        s.approvals.start_turn(int((s.started_at + self.config.wall_budget_s) * 1000))
        try:
            s.writes.begin_control()
            s.writes.start_observing()
        except Exception as exc:  # ControlPlaneError; typed by the control plane module
            s.terminal_reason, s.detail = "control_plane_error", str(exc)[:200]
            return self._say(s, "无法开始这次操作。" if s.chinese
                             else "The turn could not start.")
        s.ledger.append("observation", epoch=s.obs.epoch, url=s.obs.url,
                        elements=len(s.obs.elements), digest=s.obs.content_digest)
        chat = AuditedChat(self.chat, s.ledger)
        index = self.goal_port.index(context=s.approvals.context(0, "index"))
        s.ledger.append("goal_index_ready", entities=len(index.entities),
                        pages=len(index.pages), source_digest=index.source_digest,
                        complete=index.complete)
        s.step += 1
        elapsed = time.time() - s.started_at
        try:
            spec, projection = understand(
                chat, prompt=s.message.text, index=index, run_id=s.run_id,
                timeout_s=max(1.0, self.config.wall_budget_s - elapsed),
                cancelled=s.cancelled)
        except ValueError as exc:
            s.ledger.append("goal_declaration_invalid", step=s.step, detail=str(exc)[:200])
            s.terminal_reason = "agent_refused"
            return self._say(s, "没能理解这个请求要做什么，请换个说法。" if s.chinese
                             else "I could not work out what this request asks for.")
        s.ledger.append("goal_declared", step=s.step, spec=spec.to_json(), **projection)
        if not spec.deliverables:
            s.terminal_reason = "agent_refused"
            why = spec.unsupported or ""
            return self._say(s, ("这个请求超出了助手能做的范围。" + (f"（{why}）" if why else ""))
                             if s.chinese else
                             ("This request is outside what the assistant can do."
                              + (f" ({why})" if why else "")))
        s.step += 1
        resolution = self.goal_port.resolve(
            spec, context=s.approvals.context(s.step, "resolve"),
            current_path=_path_of(s.obs.url))
        s.ledger.append("goal_resolved", step=s.step,
                        kinds=[d.kind for d in resolution.deliverables],
                        problems=[d.problem for d in resolution.deliverables],
                        reads=[list(r) for r in resolution.reads])
        outcomes: list[_PageOutcome | None] = []
        for resolved in resolution.deliverables:
            if resolved.page is None:
                outcomes.append(None)
                continue
            if s.terminal_reason:
                outcomes.append(_PageOutcome(reached=False, unmet=["not attempted"]))
                continue
            outcomes.append(self._reach(s, resolved.page))
        if not s.terminal_reason:
            achieved = any(_achieved(r, o) for r, o in zip(resolution.deliverables, outcomes))
            s.terminal_reason = "agent_finished" if achieved else "agent_refused"
        return self._reply(s, resolution.deliverables, outcomes)

    # -- pages ------------------------------------------------------------------------
    def _dispatch(self, s: _TurnState, action: str, args: dict, *, navigating: bool = False):
        """One page command under write control, with its receipt on the record."""
        s.step += 1
        s.ledger.append("tool_call", step=s.step, tool=action, args=dict(args))
        s.writes.expect_decision(False)
        try:
            receipt = s.browser.execute(action, args, expect_navigation=navigating)
        except TurnCancelled as exc:
            s.terminal_reason, s.detail = "user_cancelled", str(exc)[:200]
            return None
        except TransportError as exc:
            s.terminal_reason, s.detail = "transport_error", str(exc)[:200]
            return None
        if navigating:
            s.approvals.settle_navigation(receipt)
        outcome = s.writes.write_outcome(receipt, s.step, action_may_write=False)
        s.ledger.append("write_outcome", step=s.step, **{k: v for k, v in outcome.items()
                                                          if k != "detail"})
        s.ledger.append("action_receipt", step=s.step, tool=action, command_id=receipt.command_id,
                        ok=receipt.ok, error_code=receipt.error_code,
                        url_before=receipt.url_before, url_after=receipt.url_after,
                        navigated=receipt.navigated, write="none")
        if action not in ("observe", "wait_for"):
            s.page_actions += 1
        if receipt.observation is not None:
            s.obs = receipt.observation
        return receipt

    def _wait_ready(self, s: _TurnState, expected_path: str, *, record: bool) -> bool:
        """Wait until the page shows the destination, not merely its address.

        A single-page application changes the URL first and renders later; an observation
        taken between the two still lists the previous page's controls. The page counts
        as ready when its path is the expected one, the runtime does not report it
        pending or blocked, the application reports the rendered record or view, and
        two consecutive observations agree.
        """
        confirmed = 0
        for _ in range(_READY_WAITS):
            obs = s.obs
            path_ok = not expected_path or _path_of(obs.url) == expected_path
            rendered = (bool(obs.record_route) if record else bool(obs.active_view)) \
                or (obs.page_facets is not None)
            if obs.render_state == "blocked":
                return False
            if path_ok and obs.render_state != "pending" and rendered:
                # Seen rendered twice, half a second apart: the header controls and tabs
                # of a form arrive a beat after its route does.
                confirmed += 1
                if confirmed >= 2:
                    return True
            receipt = self._dispatch(s, "wait_for", {"ms": 500})
            if receipt is None:
                return False
        return bool(not expected_path or _path_of(s.obs.url) == expected_path)

    def _reach(self, s: _TurnState, plan: PagePlan) -> _PageOutcome:
        out = _PageOutcome()
        target = plan.url
        if target and (_path_of(s.obs.url) != _path_of(target)
                       or urlsplit(target).query != urlsplit(s.obs.url).query):
            reason, detail = s.approvals.confirm_navigation(
                s.step + 1, s.obs, action="goto", url=target)
            if reason:
                s.terminal_reason, s.detail = reason, detail
                out.reached = False
                out.unmet.append("page switch not confirmed")
                return out
            if self._dispatch(s, "goto", {"url": target}, navigating=True) is None:
                out.reached = False
                return out
        if not self._wait_ready(s, plan.expected_path,
                                record=bool(plan.section or plan.highlight
                                            or re.search(r"/\d+$", plan.expected_path))):
            if s.terminal_reason:
                out.reached = False
                return out
            out.reached = False
            out.unmet.append("page did not open" if s.obs.render_state != "blocked"
                             else "the application reported an error dialog")
            out.url = s.obs.url
            return out
        for name in plan.facets_on:
            if not self._facet(s, name, active=True):
                out.unmet.append(f"filter {name}")
        for name in plan.facets_off:
            if not self._facet(s, name, active=False):
                out.unmet.append(f"filter {name} off")
        if plan.section and not self._select_tab(s, plan.section):
            out.unmet.append(f"section {plan.section}")
        if plan.highlight:
            control = self._highlight(s, plan.highlight)
            if control:
                out.highlighted = control
            else:
                out.unmet.append(f"control {plan.highlight}")
        if plan.view and not self._view_shown(s.obs, plan.view):
            out.unmet.append(f"{plan.view} view")
        out.url = s.obs.url
        out.reached = _path_of(s.obs.url) == (plan.expected_path or _path_of(s.obs.url))
        s.ledger.append("goal_page_settled", step=s.step, reached=out.reached,
                        unmet=list(out.unmet), url=s.obs.url,
                        render_state=s.obs.render_state, active_view=s.obs.active_view,
                        record_route=s.obs.record_route,
                        selected_controls=list(s.obs.selected_controls)[:8],
                        facets=[str(f.get("name", "")) for f in (s.obs.page_facets or [])][:8],
                        elements=len(s.obs.elements),
                        roles=sorted({e.role for e in s.obs.elements})[:12])
        return out

    def _facet(self, s: _TurnState, name: str, *, active: bool) -> bool:
        wanted = _norm(name)
        if not active and wanted.startswith("my") and not any(
                _norm(str(f.get("name", ""))) == wanted for f in (s.obs.page_facets or [])):
            # "Not only mine": the page's own name for its personal filter may differ from
            # the index's ("My Quotations" where the index said "My Orders"). The active
            # facet that scopes to the person is the one meant.
            personal = next((f for f in (s.obs.page_facets or [])
                             if _norm(str(f.get("name", ""))).startswith("my")), None)
            if personal is not None:
                wanted = _norm(str(personal.get("name", "")))
        for _attempt in range(3):
            shown = [f for f in (s.obs.page_facets or []) if _norm(str(f.get("name", ""))) == wanted]
            if shown and active:
                return True
            if not shown and not active:
                return True
            candidate = next((c for c in (s.obs.facet_candidates or [])
                              if _norm(str(c.get("name", ""))) == wanted
                              and bool(c.get("active")) != active), None)
            if candidate is None:
                current = next((c for c in (s.obs.facet_candidates or [])
                                if _norm(str(c.get("name", ""))) == wanted), None)
                if current is not None and bool(current.get("active")) == active:
                    return True
                self._dispatch(s, "wait_for", {"ms": 400})
                continue
            receipt = self._dispatch(s, "apply_facet", {"ref": str(candidate["ref"]),
                                                         "active": active})
            if receipt is None:
                return False
            self._dispatch(s, "wait_for", {"ms": 600})
            shown = [f for f in (s.obs.page_facets or []) if _norm(str(f.get("name", ""))) == wanted]
            return bool(shown) == active
        return False

    @staticmethod
    def _tab(obs: PageObservation, wanted: str):
        tab = next((e for e in obs.elements if e.role == "tab" and _norm(e.name) == wanted),
                   None)
        if tab is None:
            tab = next((e for e in obs.elements
                        if e.role == "tab" and wanted and wanted in _norm(e.name)), None)
        return tab

    @staticmethod
    def _tab_selected(obs: PageObservation, wanted: str) -> bool:
        if any(_norm(c) == wanted for c in obs.selected_controls):
            return True
        for element in obs.elements:
            if element.role == "tab" and _norm(element.name) == wanted:
                return any(str(element.attrs.get(attr, "")).lower() == "true"
                           for attr in _SELECTED_ATTRS)
        return False

    def _select_tab(self, s: _TurnState, section: str) -> bool:
        wanted = _norm(section)
        if self._tab_selected(s.obs, wanted):
            return True
        tab = self._tab(s.obs, wanted)
        if tab is None:
            return False
        receipt = self._dispatch(s, "click", {"ref": tab.ref, "epoch": s.obs.epoch})
        if receipt is None or not receipt.ok:
            return False
        for _ in range(3):
            if self._tab_selected(s.obs, _norm(tab.name)):
                return True
            self._dispatch(s, "wait_for", {"ms": 400})
        return self._tab_selected(s.obs, _norm(tab.name))

    def _highlight(self, s: _TurnState, control: str) -> str:
        wanted = _norm(control)
        candidates = [e for e in s.obs.elements if e.role not in _NO_HIGHLIGHT_ROLES]
        # A button by its exact name first, then any control (a field's input is named by
        # its label) by exact name, then by prefix.
        element = next((e for e in candidates if e.role == "button" and _norm(e.name) == wanted),
                       None)
        if element is None:
            element = next((e for e in candidates if _norm(e.name) == wanted), None)
        if element is None:
            element = next((e for e in candidates
                            if wanted and _norm(e.name).startswith(wanted)), None)
        if element is None:
            return ""
        receipt = self._dispatch(s, "highlight", {"ref": element.ref, "epoch": s.obs.epoch})
        if receipt is None or not receipt.ok:
            return ""
        s.ledger.append("page_handoff_ready", step=s.step, command_id=receipt.command_id,
                        observation_epoch=s.obs.epoch, submitted=False, persisted=False)
        return element.name

    @staticmethod
    def _view_shown(obs: PageObservation, view: str) -> bool:
        wanted = _norm(view)
        if _norm(obs.active_view) == wanted:
            return True
        return any(_norm(c) == wanted for c in obs.selected_controls)

    # -- the reply --------------------------------------------------------------------
    def _reply(self, s: _TurnState, deliverables, outcomes) -> str:
        lines = []
        zh = s.chinese
        for resolved, outcome in zip(deliverables, outcomes):
            lines.extend(_render(resolved, outcome, zh))
        if s.terminal_reason not in ("agent_finished", "agent_refused"):
            lines.append(("这次操作没有完成：" if zh else "The turn did not complete: ")
                         + (s.detail or s.terminal_reason))
        return self._say(s, "\n\n".join(line for line in lines if line))

    def _say(self, s: _TurnState, text: str) -> str:
        return text.strip()

    # -- settlement -------------------------------------------------------------------
    def _settle(self, s: _TurnState, current, text: str) -> AgentTurnExecution:
        release = s.writes.end_control()
        if release is not None and not s.terminal_reason:
            s.terminal_reason, s.detail = "control_plane_error", str(release)[:200]
        terminal = s.terminal_reason or "agent_finished"
        wall = round(time.time() - s.started_at, 3)
        agent = RunResult(run_id=s.run_id, task_ref=s.run_id, arm="goal",
                          terminal_reason=terminal, steps_used=s.step, wall_seconds=wall,
                          agent_said=text[:self.config.max_user_answer_chars],
                          page_actions=s.page_actions,
                          navigation_control=s.approvals.navigation_result(),
                          success_claim=("not_claimed" if terminal != "agent_finished"
                                         else "verified"),
                          detail=s.detail)
        s.ledger.append("run_finished", task_ref=s.run_id, arm="goal",
                        terminal_reason=terminal, steps_used=s.step, wall_seconds=wall,
                        agent_said=text[:300], page_actions=s.page_actions,
                        navigation_control=agent.navigation_control or {})
        status = _status_for(terminal)
        user_answer = redact_sensitive_assignments(redact_session_handoffs(text)).strip()
        user_answer = user_answer[:self.config.max_user_answer_chars]
        fallback = False
        if not user_answer:
            fallback = True
            user_answer = render_neutral_user_answer(AgentAnswerContext(
                message=s.message, agent=agent, business_result=None,
                max_chars=self.config.max_user_answer_chars))
        execution = AgentTurnExecution(
            schema_version=1, run_id=s.run_id, session_id=self.channel.session_id,
            message=s.message, entry_observation_id=current.observation_id,
            entry_observation_digest=current.observation.content_digest,
            status=status, terminal_reason=terminal, user_answer=user_answer, agent=agent,
            business_result=None, answer_fallback_used=fallback,
            started_at=s.started_at, finished_at=time.time())
        self.conversation_state.record(
            message_id=s.message.message_id, user_text=s.message.text,
            assistant_text=user_answer, status=status.value, business_result=None,
            clarification=None)
        s.ledger.append("conversation_state_updated",
                        retained_turns=self.conversation_state.turn_count,
                        context_digest=self.conversation_state.content_digest)
        s.ledger.append("product_turn_finished", status=status.value,
                        terminal_reason=terminal, answer_fallback_used=fallback,
                        business_result_digest="")
        try:
            self.channel.publish("progress", {
                "state": status.value, "terminal_reason": terminal, "message": user_answer,
            }, turn_id=s.message.message_id)
        except Exception as exc:
            s.ledger.append("terminal_delivery_lost", status=status.value,
                            error_type=type(exc).__name__)
        return execution


def _status_for(terminal_reason: str) -> AgentTurnStatus:
    if terminal_reason == "agent_finished":
        return AgentTurnStatus.FINISHED
    if terminal_reason == "agent_refused":
        return AgentTurnStatus.REFUSED
    if terminal_reason in {"user_cancelled", "navigation_not_confirmed"}:
        return AgentTurnStatus.CANCELLED
    return AgentTurnStatus.FAILED


def _achieved(resolved: Resolved, outcome: _PageOutcome | None) -> bool:
    if resolved.problem and resolved.kind in ("fact", "aggregate", "list") and not (
            resolved.facts or resolved.aggregate):
        return False
    if resolved.kind in ("fact",) and resolved.facts:
        return True
    if resolved.kind in ("aggregate", "list") and resolved.aggregate is not None:
        return True
    if outcome is not None:
        return outcome.reached and not outcome.unmet
    return False


def _render(resolved: Resolved, outcome: _PageOutcome | None, zh: bool) -> list[str]:
    lines: list[str] = []
    if resolved.facts:
        for fact in resolved.facts:
            lines.append(f"**{fact.subject} · {fact.label}**：{fact.value}" if zh
                         else f"**{fact.subject} · {fact.label}**: {fact.value}")
    agg = resolved.aggregate
    if agg is not None:
        if resolved.kind == "list":
            names = "、".join(agg.records) if zh else ", ".join(agg.records)
            head = (f"**{agg.subject}**：共 {agg.count} 条" if zh
                    else f"**{agg.subject}**: {agg.count} records")
            if names:
                head += ("：" if zh else ": ") + names
            if not agg.complete:
                head += "（仅列出前 20 条）" if zh else " (first 20 shown)"
            lines.append(head)
        else:
            head = (f"**{agg.subject}**：{agg.count} 条" if zh
                    else f"**{agg.subject}**: {agg.count}")
            for label, value in agg.sums:
                head += (f"；{label} 合计 {value}" if zh else f"; {label} total {value}")
            if agg.groups:
                top = "，".join(f"{name}（{count} 条）" for name, count in agg.groups) if zh \
                    else ", ".join(f"{name} ({count})" for name, count in agg.groups)
                head += (f"；最多的是 {top}" if zh else f"; most: {top}")
            if agg.note:
                head += f"（{agg.note}）" if zh else f" ({agg.note})"
            lines.append(head)
    if resolved.page is not None and outcome is not None:
        plan = resolved.page
        if resolved.kind == "handoff":
            if outcome.highlighted:
                lines.append((f"已打开 {plan.label} 并在页面上标出「{outcome.highlighted}」。"
                              "助手不能替你点击或提交，请核对后自行操作。") if zh else
                             (f"Opened {plan.label} and highlighted “{outcome.highlighted}”. "
                              "The assistant cannot press or submit it for you; please check "
                              "and act yourself."))
            else:
                lines.append((f"已打开 {plan.label}，但没有找到「{plan.highlight}」这个控件，"
                              "助手无法替你完成这项操作。") if zh else
                             (f"Opened {plan.label}, but could not find the “{plan.highlight}” "
                              "control; the assistant cannot complete this for you."))
        elif outcome.reached and not outcome.unmet:
            what = plan.label
            extras = []
            if plan.view:
                extras.append(f"{plan.view} 视图" if zh else f"{plan.view} view")
            if plan.facets_on:
                extras.append(("筛选：" if zh else "filters: ") + ", ".join(plan.facets_on))
            if plan.facets_off:
                extras.append(("已移除筛选：" if zh else "filters removed: ")
                              + ", ".join(plan.facets_off))
            suffix = f"（{'；'.join(extras)}）" if extras and zh else (
                f" ({'; '.join(extras)})" if extras else "")
            lines.append((f"已打开：{what}{suffix}") if zh else f"Opened: {what}{suffix}")
        else:
            unmet = "、".join(outcome.unmet) if zh else ", ".join(outcome.unmet)
            lines.append((f"打开 {plan.label} 未完成：{unmet or '页面未到达'}。") if zh
                         else f"Opening {plan.label} was not completed: {unmet or 'page not reached'}.")
    if resolved.problem and not (resolved.facts or resolved.aggregate or resolved.page):
        text = resolved.problem
        if resolved.candidates:
            text += (" 候选：" if zh else " candidates: ") + ", ".join(resolved.candidates)
        lines.append((f"无法完成这一项：{text}") if zh else f"Could not do this part: {text}")
    elif resolved.problem and resolved.facts:
        lines.append((f"（{resolved.problem}）") if zh else f"({resolved.problem})")
    return lines


__all__ = ["GoalTurnExecutor"]
