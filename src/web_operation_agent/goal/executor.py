"""A goal-directed turn: understand once, resolve, act, check, reply.

Same wire contract as `AgentTurnExecutor` -- a channel, a config, a chat, a bound
application -- and the same fail-closed edges: every page switch is confirmed by the
person, every page command runs under the write control the application declares, and no
control that could write is ever pressed. What differs is that the model is asked one
question and the rest of the turn is server code the audit can replay.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
import functools
import json
import re
import time
from typing import Callable, NamedTuple
from urllib.parse import urlsplit

from ..business.approvals import PAGE_OPERATIONS, ApprovalWatch, OperationPlan
from ..business.control import ApprovalGate, ApprovalGrant
from ..business.writes import WriteWatch
from ..composition.binding import BoundApplication
from ..execution.product import (AgentAnswerContext, AgentTurnError, AgentTurnErrorCode,
                                 AgentTurnExecution, AgentTurnMessage, AgentTurnStatus,
                                 render_neutral_user_answer)
from ..execution.runstate import RunResult
from ..modeling.chat import require_chat_port
from ..modeling.egress import AuditedChat
from ..modeling.provider import ProviderError
from ..privacy import redact_sensitive_assignments, redact_session_handoffs
from ..schemas.browser import PageObservation
from ..transport.protocol import RemoteProtocolError
from ..state.conversation import ConversationState
from ..state.identity import PrincipalError
from ..state.ledger import SessionLedger
from ..transport.browser import RemoteBrowserClient, TransportError, TurnCancelled
from .ports import Fact, GoalPort, PagePlan, Resolution, Resolved, capabilities_of
from .spec import GoalSpec
from .ground import bind, days, ground, pick, render as render_values, without_days
from .narrow import correct_records, locate, neighbourhood, references, summaries
from .understand import understand, understand_scope

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CJK = re.compile(r"[㐀-鿿]")
#: How long the page may take to settle after a switch, in half-second waits.
_READY_WAITS = 12
#: Attribute values a runtime uses to mark the current tab of a set.
_SELECTED_ATTRS = ("aria-selected", "selected", "aria-pressed", "aria-checked")
#: Element roles a handoff never highlights: navigation, not the control asked for. A link
#: leads somewhere else; a tag's "Delete" link is not the record's Delete. An item of a menu
#: the turn itself opened is the one exception (see `_reveal_control`).
_NO_HIGHLIGHT_ROLES = frozenset({"tab", "menuitem", "menu", "menubar", "tablist", "navigation",
                                 "link"})
#: Words that ask for a total over several records rather than one record's amount.
_TOTAL_WORDS = re.compile(r"合计|总计|总额|总金额|总共|一共|共计|加起来|累计|全部|所有|分别|各|total|sum\b|"
                          r"altogether|all\b|each\b", re.I)
#: A request to create a record, as a verb ("开一张新的发票", "新建客户"), not as the
#: adjective of records already made ("新建的订单").
_CREATE = re.compile(r"(?:新建|新增|创建|新开|开一张|录一张|录入|建一个|加一个)(?!的)|"
                     r"\b(?:create|add) (?:a|an|one)\b|\bnew (?:invoice|order|customer|quotation|bill)\b",
                     re.I)
#: A request to delete a record, and a control that deletes.
_DELETE = re.compile(r"删除|删掉|删了|\bdelete\b|\bremove\b", re.I)
#: Words that point at one record ("that invoice", "the one of August").
_ONE_RECORD = re.compile(r"那张|这张|那笔|这笔|那条|这条|那单|这单|那个|这个|哪张|哪笔|"
                         r"\bthat (?:invoice|order|refund|bill|quotation|one)\b", re.I)
#: Earlier turns a follow-up is read with, and how much of each reply.
_EARLIER_TURNS = 2
_EARLIER_REPLY_CHARS = 1_200
#: A sum over more records than this is a total, whatever the words.
_MAX_AMOUNT_CHOICES = 6
#: At most this many collapsed menus are opened looking for a control to hand over.
_MAX_REVEALS = 3
#: Rules about the same control or field, and rules of the same operation, answered
#: along with the one named.
_SAME_ABOUT_RULES = 10
_SAME_OPERATION_RULES = 6
#: The same refusals written on other entities, answered along with a rule's own.
_SAME_REFUSAL_ELSEWHERE = 4
#: The message a refusal rule quotes, as the index writes it, and the placeholders a
#: message fills in (which two places raising it may name differently).
_REFUSAL_MESSAGE = re.compile(r'refuses with "(.{20,}?)" when')
_PLACEHOLDER = re.compile(r"%\([^)]*\)[sd]|%[sd]|\{[^}]*\}")
#: Roles of elements a value can be entered into.
_INPUT_ROLES = frozenset({"textbox", "combobox", "select", "searchbox", "spinbutton"})
#: Roles a field's own suggestion list uses for the records it proposes.
_SUGGESTION_ROLES = frozenset({"option", "menuitem", "listitem", "link"})


def _norm(text: str) -> str:
    return "".join(ch for ch in (text or "").casefold() if not ch.isspace()
                   ).replace("&amp;", "&")


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-z\u3400-\u9fff]+", (text or "").casefold()) if t}


def _path_of(url: str) -> str:
    return urlsplit(url).path or "/"


def _appeared(before: Counter, elements) -> list:
    """The elements a menu added: for each role and name, the occurrences beyond what the
    page showed before, taken last in page order, since a menu's items render after the
    control that opened it (a navigation item of the same name stays out)."""
    now = Counter((e.role, _norm(e.name)) for e in elements)
    extra = {key: count - before.get(key, 0) for key, count in now.items()
             if count > before.get(key, 0)}
    shown = []
    for e in reversed(list(elements)):
        key = (e.role, _norm(e.name))
        if extra.get(key, 0) > 0:
            extra[key] -= 1
            shown.append(e)
    return list(reversed(shown))


_FLIPPED = {"<=": ">=", "<": ">", ">=": "<=", ">": "<"}
_NEGATIVE = re.compile(r"^-\d[\d,]*(?:\.\d+)?$")


def _magnitudes(spec: GoalSpec, text: str) -> tuple[GoalSpec, tuple[str, ...]]:
    """An amount the person states is a magnitude ("credit notes over 30,000"). A
    condition the model wrote with negative numbers the request never wrote -- a refund
    thought of as negative -- is read as the magnitude, its comparison turned round.
    Returns (spec, the fields changed)."""
    if "-" in (text or "") or "负" in (text or "") or "−" in (text or ""):
        return spec, ()
    out, changed = [], []
    for d in spec.deliverables:
        if d.metric is not None and d.metric.filters:
            filters = []
            for f in d.metric.filters:
                if f.values and all(_NEGATIVE.match(v.strip()) for v in f.values) \
                        and (f.op in _FLIPPED or f.op in ("", "=", "between")):
                    values = tuple(v.strip()[1:] for v in f.values)
                    if f.op == "between":
                        values = tuple(reversed(values))
                    f = replace(f, values=values, op=_FLIPPED.get(f.op, f.op))
                    changed.append(f.field)
                filters.append(f)
            d = replace(d, metric=replace(d.metric, filters=tuple(filters)))
        out.append(d)
    if not changed:
        return spec, ()
    return replace(spec, deliverables=tuple(out)), tuple(changed)


def _as_handoffs(spec: GoalSpec, text: str = "") -> tuple[GoalSpec, tuple[str, ...]]:
    """A page declared with a control to act on is a handoff: the person asked to be shown
    that control ("mark Pay for me", "where is Print Labels"), and opening the page alone
    would leave the control unmarked. So is a list page declared for a request to create
    one ("I want to make a new customer invoice, show me where"): its control is the
    page's own New. Returns (spec, the controls)."""
    creating = bool(_CREATE.search(text or ""))
    deleting = bool(_DELETE.search(text or ""))
    out, controls = [], []
    for d in spec.deliverables:
        if (deleting and d.kind in ("handoff", "page") and d.record and d.action
                and not _DELETE.search(d.action)):
            # The person asked to delete; another operation (archive) is not what they
            # asked for, however close. The page shows Delete or it does not.
            d = replace(d, action="Delete")
            controls.append("Delete")
        if d.kind == "page" and d.action and d.record:
            d = replace(d, kind="handoff")
            controls.append(d.action)
        elif d.kind == "page" and creating and not d.record and not d.action and d.page.key:
            d = replace(d, kind="handoff", action="create")
            controls.append("create")
        out.append(d)
    if not controls:
        return spec, ()
    return replace(spec, deliverables=tuple(out)), tuple(controls)


def _first_line(name: str) -> str:
    """A control's name as the reply quotes it: a label's first line; a help marker on a
    later line is not part of it."""
    return ((name or "").strip().splitlines() or [""])[0].strip()


class _Unmet(NamedTuple):
    """One part of a page request the page did not show, by kind and the name asked."""
    kind: str
    name: str = ""

    def __str__(self) -> str:  # the audit's wording
        return {"not_attempted": "not attempted",
                "not_confirmed": "working the page not confirmed",
                "not_opened": "page did not open",
                "error_dialog": "the application reported an error dialog",
                "filter_on": f"filter {self.name}", "filter_off": f"filter {self.name} off",
                "section": f"section {self.name}", "control": f"control {self.name}",
                "view": f"{self.name} view"}[self.kind]

    def said(self, zh: bool) -> str:
        """As the person is told it."""
        if zh:
            return {"not_attempted": "没有执行", "not_confirmed": "没有得到操作页面的同意",
                    "not_opened": "页面没有打开", "error_dialog": "应用弹出了错误提示",
                    "filter_on": f"筛选「{self.name}」没有生效",
                    "filter_off": f"筛选「{self.name}」没有取消",
                    "section": f"没有找到页签「{self.name}」",
                    "control": f"没有找到控件「{self.name}」",
                    "view": f"没有切换到 {self.name} 视图"}[self.kind]
        return {"not_attempted": "not attempted", "not_confirmed": "working the page was not agreed",
                "not_opened": "the page did not open",
                "error_dialog": "the application showed an error",
                "filter_on": f"filter “{self.name}” not applied",
                "filter_off": f"filter “{self.name}” not removed",
                "section": f"no tab “{self.name}”", "control": f"no control “{self.name}”",
                "view": f"not switched to the {self.name} view"}[self.kind]


@dataclass
class _PageOutcome:
    reached: bool = True
    unmet: list[_Unmet] = field(default_factory=list)
    url: str = ""
    highlighted: str = ""
    #: The menu the highlighted control is in, when the turn opened it to find it.
    menu: str = ""
    #: The value put into the highlighted field, as the page labels it; whether the
    #: page showed it afterwards; and a value that was meant but could not be entered.
    filled: str = ""
    fill_verified: bool = False
    fill_failed: str = ""


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
    write_intent: bool = False
    cancelled: Callable[[], bool] = lambda: False
    #: The index the declaration was read against (the neighbourhood, when staged).
    index: object = None
    #: How this turn reads the request: understand() with the turn's scope and values,
    #: value ids bound; the deciding reading and the repair go through it too.
    read: object = None
    #: The candidate values this turn offered, for the question a clarification asks.
    candidates: tuple = ()
    #: The clarification this turn ends with, when it asks the person to choose.
    clarification: dict | None = None
    #: The whole index, which the neighbourhood was cut from; rules are answered from
    #: it, so that a rule the catalogue left out still comes with the ones it names.
    full_index: object = None
    #: The records the request's own words name, as the adapter located them, and the
    #: records its own amounts, days or references picked out before resolving.
    references: tuple = ()
    warranted: tuple = ()



class GoalTurnExecutor:
    """Execute one turn as a declared goal, without owning the channel lifecycle."""

    def __init__(self, *, channel, config, chat, application: BoundApplication,
                 goal_port: GoalPort, control, run_id_factory,
                 ledger_factory=SessionLedger,
                 conversation_state: ConversationState | None = None,
                 readings: int = 2, staged: bool = True, values: bool = True):
        #: The staged path (the default): the request's hard references are located by
        #: the adapter, the model first picks entities and kinds from one-line summaries,
        #: then declares the details against those entities' neighbourhood of the index,
        #: where the rules of the deployment's source are. Off, the model declares once
        #: against the whole index, as a comparison or for a scripted apparatus.
        self.staged = bool(staged)
        #: Candidate values offered before the details are declared, when the port has
        #: them; off only to measure what they change.
        self.values = bool(values)
        #: How many independent readings of the request are declared before acting.
        #: With two, a request the model reads differently each time is caught: the two
        #: readings are shown to it side by side and it declares once more, deciding.
        #: One reading trusts the first declaration as it is.
        self.readings = max(1, int(readings))
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

        def confirm_operation(plan: OperationPlan, *, context=None) -> ApprovalGrant:
            """One card for the turn, as the ReAct turn asks: may it work the page."""
            chinese = bool(_CJK.search(turn_message.text))
            effect = ("在页面上帮你完成这件事" if chinese
                      else "Work the page to do this for you")
            targets = (("会打开页面、点击、填写和选择",) if chinese
                       else ("Opens pages, clicks, fills and chooses",))
            try:
                return gate.decide_plan(plan, effect=effect, targets=targets,
                                        ttl_s=self.config.operation_approval_ttl_s,
                                        context=context, kind="operate")
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
            operation_confirmation=(confirm_operation if self.config.confirm_page_operations
                                    else None),
            cancelled=cancelled,
            operation_hold_s=self.config.operation_approval_ttl_s)
        writes = WriteWatch(control=self.control, ledger=ledger, run_id=run_id,
                            write_guard_declared=bool(self.application.descriptor.page_write_guard))
        continued = self._continued(turn_message)
        if continued is not None:
            ledger.append("goal_clarification_answered", message_id=turn_message.message_id)
            turn_message = replace(turn_message, text=continued)
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
        s.index = s.full_index = index
        s.ledger.append("goal_index_ready", entities=len(index.entities),
                        pages=len(index.pages), source_digest=index.source_digest,
                        complete=index.complete)
        s.step += 1
        elapsed = time.time() - s.started_at
        today = getattr(self.goal_port, "today", None)
        today_text = str(today() or "") if callable(today) else ""
        scope_text = ""
        scope_parts: list[dict] = []
        known = ()
        # A follow-up is read with what was said before it, and the records it may mean
        # are looked for in the earlier request as well as in its own words.
        earlier, before = self._earlier()
        lookup = (before + " " + s.message.text).strip()
        if self.staged:
            known = locate(self.goal_port, lookup,
                           context=s.approvals.context(s.step, "locate"))
            s.references = tuple(known)
            s.ledger.append("goal_references", step=s.step,
                            references=[{"text": k.text, "entity": k.entity, "name": k.name}
                                        for k in known][:8])
            try:
                parts, write_intent, unsupported, projection = understand_scope(
                    chat, prompt=s.message.text,
                    summaries=summaries(index, lookup, known), run_id=s.run_id,
                    timeout_s=max(1.0, self.config.wall_budget_s - elapsed),
                    cancelled=s.cancelled, today=today_text, earlier=earlier)
            except ValueError as exc:
                s.ledger.append("goal_declaration_invalid", step=s.step, detail=str(exc)[:200])
                s.terminal_reason = "agent_refused"
                return self._say(s, "没能理解这个请求要做什么，请换个说法。" if s.chinese
                                 else "I could not work out what this request asks for.")
            except ProviderError as exc:
                s.ledger.append("provider_failed", step=s.step,
                                provider_error_code=getattr(exc, "code", "provider_error"))
                s.terminal_reason, s.detail = "provider_error", str(exc)[:200]
                return self._say(s, "模型服务暂时没有响应，请稍后再试。" if s.chinese
                                 else "The model service did not respond; please try again.")
            scope_parts = parts
            s.ledger.append("goal_scope_declared", step=s.step, parts=parts,
                            write_intent=write_intent, unsupported=unsupported, **projection)
            if not parts:
                s.terminal_reason = "agent_refused"
                return self._say(s, ("这个请求助手做不到。" + (f"（{unsupported}）" if unsupported else ""))
                                 if s.chinese else
                                 ("The assistant cannot do this request."
                                  + (f" ({unsupported})" if unsupported else "")))
            # The rules of the source are shown only when the first step says a part
            # asks how the application behaves: a long catalogue of rule ids in front of
            # a question about data disturbs how a small model reads the question.
            # The words the first step wrote in the application's language rank the
            # rules alongside the request's own, which may be in another language.
            terms = " ".join(t for p in parts for t in p.get("terms", ()))
            index = neighbourhood(index, tuple(p["entity"] for p in parts),
                                  request=(s.message.text + " " + terms).strip(),
                                  rules=any(p.get("rules") for p in parts))
            s.index = index
            s.ledger.append("goal_neighbourhood", step=s.step,
                            entities=[e.key for e in index.entities], pages=len(index.pages),
                            chars=len(index.render()))
            scope_text = json.dumps({"parts": parts, "write_intent": write_intent,
                                     "known_references": [
                                         {"text": k.text, "entity": k.entity, "name": k.name}
                                         for k in known]}, ensure_ascii=False)
            s.step += 1
            elapsed = time.time() - s.started_at
        candidates = ()
        if self.staged and self.values and "values" in capabilities_of(self.goal_port):
            try:
                candidates = ground(self.goal_port,
                                    entities=tuple(dict.fromkeys(p["entity"] for p in scope_parts)),
                                    known=known, dates=days(lookup, today_text),
                                    context=s.approvals.context(s.step, "candidates"))
            except Exception as exc:  # noqa: BLE001 -- grounding helps; its absence is not a failure
                s.ledger.append("goal_values_failed", step=s.step,
                                detail=f"{type(exc).__name__}: {exc}"[:200])
            s.ledger.append("goal_values", step=s.step, count=len(candidates),
                            kinds=sorted({c.kind for c in candidates}))
            s.candidates = candidates
        unbound = functools.partial(
            understand, chat, prompt=s.message.text, index=index, run_id=s.run_id,
            timeout_s=max(1.0, self.config.wall_budget_s - elapsed), cancelled=s.cancelled,
            today=today_text, scope=scope_text, values=render_values(candidates),
            earlier=earlier)

        def read(**kwargs):
            # Every reading -- the first, the second, the deciding one, the repair --
            # sees the same scope and values, and has its value ids bound to the exact
            # names before it is compared or resolved.
            spec, projection = unbound(**kwargs)
            if candidates:
                spec, bound, unknown = bind(spec, candidates)
                projection = {**projection, "values_bound": bound,
                              **({"values_unknown": list(unknown)} if unknown else {})}
            return spec, projection
        s.read = read
        # The readings are independent, so they are asked for at the same time: the
        # second costs a call, not a wait. The staged path's second step is read twice
        # too: a settled scope still leaves the values and record numbers to misread.
        second_reading = None
        if self.readings > 1:
            pool = ThreadPoolExecutor(max_workers=1)
            second_reading = pool.submit(read)
            pool.shutdown(wait=False)
        try:
            spec, projection = read()
        except ValueError as exc:
            s.ledger.append("goal_declaration_invalid", step=s.step, detail=str(exc)[:200])
            s.terminal_reason = "agent_refused"
            return self._say(s, "没能理解这个请求要做什么，请换个说法。" if s.chinese
                             else "I could not work out what this request asks for.")
        except ProviderError as exc:
            s.ledger.append("provider_failed", step=s.step,
                            provider_error_code=getattr(exc, "code", "provider_error"))
            s.terminal_reason, s.detail = "provider_error", str(exc)[:200]
            return self._say(s, "模型服务暂时没有响应，请稍后再试。" if s.chinese
                             else "The model service did not respond; please try again.")
        s.ledger.append("goal_declared", step=s.step, spec=spec.to_json(), **projection)
        if (self.staged and not spec.deliverables and scope_parts
                and not any(p.get("rules") for p in scope_parts)):
            # The first step found things to deliver in this request and the second
            # declared none: the two readings contradict each other, and a small model
            # now and then answers the index instead of the request. It is asked once
            # more with what the first step found; its answer stands either way.
            try:
                again, projection = read(
                    previous=spec,
                    problems=("the request asks for: " + "; ".join(
                        f"{p['kind']} on {p['entity']}" + (f" ({p['about']})" if p.get("about") else "")
                        for p in scope_parts) + ". Declare these from the REQUEST below.",))
                s.ledger.append("goal_declared", step=s.step, spec=again.to_json(),
                                redeclared=True, **projection)
                if again.deliverables:
                    spec = again
            except (ValueError, ProviderError) as exc:
                s.ledger.append("goal_declaration_invalid", step=s.step, detail=str(exc)[:200])
        if second_reading is not None and spec.deliverables:
            spec = self._agree(s, chat, index, spec, today, second_reading)
        spec, flipped = _magnitudes(spec, s.message.text)
        if flipped:
            s.ledger.append("goal_amounts_as_magnitudes", step=s.step, fields=list(flipped))
        spec, handed = _as_handoffs(spec, s.message.text)
        if handed:
            s.ledger.append("goal_page_became_handoff", step=s.step, controls=list(handed))
        spec, corrections = correct_records(spec, s.message.text, known,
                                            tuple(c.display for c in s.candidates))
        if corrections:
            s.ledger.append("goal_records_corrected", step=s.step,
                            corrections=[{"declared": a, "located": b} for a, b in corrections])
            s.warranted += tuple(b for _a, b in corrections)
        if s.candidates and spec.deliverables:
            spec, picked = pick(spec, s.message.text, today_text, s.candidates)
            if picked:
                s.ledger.append("goal_record_picked", step=s.step, count=len(picked))
                s.warranted += tuple(picked)
        if self.staged and spec.deliverables:
            # A declaration can be well formed and still leave out what the request
            # asked: values asked for but only a page declared, a number, code or day the
            # request wrote that the declaration never uses. The resolver would answer it
            # anyway, so the gaps go back to the model once, before resolving.
            gaps = _coverage_gaps(spec, scope_parts, s.message.text, today_text, s.candidates)
            if gaps:
                s.ledger.append("goal_coverage_gaps", step=s.step, gaps=list(gaps))
                s.step += 1
                try:
                    again, projection = s.read(previous=spec, problems=gaps)
                    s.ledger.append("goal_declared", step=s.step, spec=again.to_json(),
                                    attempt="coverage", **projection)
                    remaining = _coverage_gaps(again, scope_parts, s.message.text, today_text,
                                               s.candidates) if again.deliverables else gaps
                    if again.deliverables and len(remaining) < len(gaps):
                        spec = again
                except (ValueError, ProviderError) as exc:
                    s.ledger.append("goal_coverage_repair_failed", step=s.step,
                                    detail=str(exc)[:200])
        s.write_intent = spec.write_intent or any(
            d.kind == "handoff" for d in spec.deliverables)
        if not spec.deliverables:
            s.terminal_reason = "agent_refused"
            why = spec.unsupported or ""
            if getattr(s.index, "focused", False) and any(e.rules for e in s.index.entities):
                # The rules of the source were in front of the model and it named none:
                # what the person asks about, in their words, is not a rule the deployed
                # source holds.
                asked = " ".join(s.message.text.split())[:120]
                why = ((f"部署源码里没有找到关于「{asked}」的规则。" if s.chinese
                        else f"The deployed source holds no rule about \"{asked}\".")
                       + (f" {why}" if why else ""))
            return self._say(s, ("这个请求助手做不到。" + (f"（{why}）" if why else ""))
                             if s.chinese else
                             ("The assistant cannot do this request."
                              + (f" ({why})" if why else "")))
        s.step += 1
        try:
            resolution = self._resolve(s, spec)
        except _ContractBroken:
            s.terminal_reason = "agent_refused"
            return self._say(s, "应用未能完整解析这次请求。" if s.chinese
                             else "The application could not resolve this request completely.")
        problems = _repairable(spec, resolution)
        if problems:
            # The one repair: the resolver found something a different declaration
            # would fix and says what; the model declares once more with that in
            # front of it. The second declaration replaces the first only when fewer
            # deliverables stay unresolved, so a repair never makes the turn worse.
            s.ledger.append("goal_repair_requested", step=s.step, problems=list(problems))
            s.step += 1
            elapsed = time.time() - s.started_at
            try:
                repaired, projection = s.read(previous=spec, problems=problems)
            except (ValueError, ProviderError) as exc:
                s.ledger.append("goal_repair_failed", step=s.step, detail=str(exc)[:200])
                repaired = None
            if repaired is not None and repaired.deliverables:
                s.ledger.append("goal_declared", step=s.step, spec=repaired.to_json(),
                                attempt=2, **projection)
                s.step += 1
                try:
                    second = self._resolve(s, repaired)
                except _ContractBroken:
                    second = None
                accepted = second is not None and \
                    _unresolved(repaired, second) < _unresolved(spec, resolution)
                s.ledger.append("goal_repaired", step=s.step, accepted=accepted,
                                unresolved_before=_unresolved(spec, resolution),
                                unresolved_after=_unresolved(repaired, second))
                if accepted:
                    spec, resolution = repaired, second
        resolution = self._which_amount(s, spec, resolution)
        resolution = self._not_chosen_for(s, spec, resolution)
        outcomes: list[_PageOutcome | None] = []
        if any(r.page is not None for r in resolution.deliverables):
            # The declarations took their time; the page has gone on rendering meanwhile
            # and the observation the turn holds may be stale. Look once before acting.
            self._dispatch(s, "observe", {})
        for resolved in resolution.deliverables:
            if resolved.page is None:
                outcomes.append(None)
                continue
            if s.terminal_reason:
                outcomes.append(_PageOutcome(reached=False, unmet=[_Unmet("not_attempted")]))
                continue
            outcomes.append(self._reach(s, resolved.page))
        if not s.terminal_reason:
            achieved = all(_achieved(r, o) for r, o in
                           zip(resolution.deliverables, outcomes, strict=True))
            s.terminal_reason = "agent_finished" if achieved else "agent_refused"
            unsure = next((r for r in resolution.deliverables
                           if len(r.candidates) >= 2 and not (r.facts or r.aggregate or r.page)),
                          None)
            if not achieved and unsure is not None:
                # Several records fit and nothing here tells them apart: the person
                # is asked which one, with what distinguishes them, instead of being
                # told the request cannot be done.
                s.terminal_reason = "needs_clarification"
                s.clarification = _clarification(s, unsure.candidates)
                s.ledger.append("goal_clarification_asked", step=s.step,
                                choices=len(s.clarification["choices"]))
                return s.clarification["question"]
        # Reaching a read-only page cannot settle a request to mutate the application.
        # A declared handoff has its own explicit "you must act" reply; a page/fact-only
        # declaration for a write must also state that the requested change did not happen.
        unfulfilled_write = spec.write_intent and not any(
            d.kind == "handoff" for d in spec.deliverables)
        if unfulfilled_write:
            s.ledger.append("goal_write_intent_unfulfilled", step=s.step)
            if s.terminal_reason == "agent_finished":
                s.terminal_reason = "agent_refused"
        reply = self._reply(s, resolution.deliverables, outcomes)
        if unfulfilled_write:
            reply += ("\n\n这里只是找到了相关信息或页面，尚未执行你请求的更改；请核对后自行操作。"
                      if s.chinese else
                      "\n\nI only found the relevant information or page. I did not make the requested change; please review it and act yourself.")
        return reply

    def _agree(self, s: _TurnState, chat, index, first: GoalSpec, today, second_reading) -> GoalSpec:
        """A second, independent reading of the request; when it disagrees with the
        first, both are put in front of the model and a third reading decides.

        A small model reads the same request differently from one call to the next --
        a list of lines as a count, a field as a page -- and a resolver cannot tell a
        plausible wrong reading from a right one. Two readings that agree are kept;
        two that differ are a question the model itself answers best when it sees both.
        A reading that fails to arrive leaves the first in place.
        """
        s.step += 1
        try:
            second, projection = second_reading.result()
        except Exception as exc:  # noqa: BLE001 -- the second reading is optional; whatever
            # stopped it (a malformed reply, a dropped call, a transport fault) leaves
            # the first reading in place and is recorded.
            s.ledger.append("goal_second_reading_failed", step=s.step,
                            detail=f"{type(exc).__name__}: {exc}"[:200])
            return first
        differences = first.differences(second)
        s.ledger.append("goal_second_reading", step=s.step, spec=second.to_json(),
                        agrees=not differences, differences=differences[:8], **projection)
        if not differences:
            return first
        if self._resolve_alike(s, first, second, today):
            # Worded apart (a label in another language, conditions in another order)
            # but pointing at the same answer: the third reading could only choose
            # between two readings that lead to the same place.
            s.ledger.append("goal_readings_resolve_alike", step=s.step)
            return first
        problems = ("a second reading of the same request declared "
                    + json.dumps(second.to_json(), ensure_ascii=False)
                    + "; the readings differ in: " + "; ".join(differences[:6])
                    + ". Decide which reading the request means and declare that",)
        s.step += 1
        elapsed = time.time() - s.started_at
        try:
            third, projection = s.read(previous=first, problems=problems)
        except (ValueError, ProviderError) as exc:
            s.ledger.append("goal_readings_unreconciled", step=s.step, detail=str(exc)[:200])
            return first
        if not third.deliverables:
            return first
        s.ledger.append("goal_declared", step=s.step, spec=third.to_json(), attempt=3,
                        reconciled=True, **projection)
        return third

    def _resolve_alike(self, s: _TurnState, first: GoalSpec, second: GoalSpec, today) -> bool:
        """Whether two readings, prepared the way a declaration is before it is
        resolved, resolve to the same deliverables. Read-only and quiet; anything that
        stops the comparison counts as a difference."""
        today_text = str(today() or "") if callable(today) else ""

        def prepared(spec: GoalSpec) -> GoalSpec:
            spec, _flipped = _magnitudes(spec, s.message.text)
            spec, _handed = _as_handoffs(spec, s.message.text)
            spec, _corrections = correct_records(spec, s.message.text, s.references,
                                                 tuple(c.display for c in s.candidates))
            if s.candidates and spec.deliverables:
                spec, _picked = pick(spec, s.message.text, today_text, s.candidates)
            return spec

        try:
            one = self._resolve(s, prepared(first), quiet=True)
            two = self._resolve(s, prepared(second), quiet=True)
        except Exception:  # noqa: BLE001 -- a comparison that cannot run is a difference
            return False
        return one.deliverables == two.deliverables

    def _resolve(self, s: _TurnState, spec: GoalSpec, *, quiet: bool = False) -> Resolution:
        # A deliverable that needs something this application's port does not offer is
        # answered here, plainly; the port sees only what it can resolve.
        offered = capabilities_of(self.goal_port)
        missing = [_needs_beyond(d, offered) for d in spec.deliverables]
        # A question answered by rules of the source is answered here, from the index the
        # model read, with the rules' citations; the port is not asked.
        by_rule = [self._rules_answer(s, d) if d.rules else None for d in spec.deliverables]
        missing = [frozenset() if r is not None else m for r, m in zip(by_rule, missing)]
        asked = GoalSpec(deliverables=tuple(d for d, m, r in zip(spec.deliverables, missing, by_rule)
                                            if not m and r is None),
                         write_intent=spec.write_intent, unsupported=spec.unsupported)
        if any(missing) and not quiet:
            s.ledger.append("goal_capability_missing", step=s.step,
                            missing=[sorted(m) for m in missing if m], offered=sorted(offered))
        partial = self.goal_port.resolve(
            asked, context=s.approvals.context(s.step, "resolve"),
            current_path=_path_of(s.obs.url)) if asked.deliverables else Resolution(
            deliverables=(), reads=())
        if (len(partial.deliverables) != len(asked.deliverables)
                or any(actual.kind != declared.kind for declared, actual in
                       zip(asked.deliverables, partial.deliverables, strict=True))):
            # The adapter must answer each deliverable it was asked with one of the
            # same kind; anything else is a broken port, reported as such, not acted on.
            if not quiet:
                s.ledger.append("goal_resolution_contract_refused", step=s.step,
                                declared=len(asked.deliverables),
                                resolved=len(partial.deliverables))
            raise _ContractBroken()
        resolved = iter(partial.deliverables)
        resolution = Resolution(deliverables=tuple(
            r if r is not None else
            Resolved(kind=d.kind, problem="this application does not offer "
                     + ", ".join(sorted(m)) + " here", unoffered=tuple(sorted(m)))
            if m else next(resolved)
            for d, m, r in zip(spec.deliverables, missing, by_rule)), reads=partial.reads)
        if quiet:
            return resolution
        s.ledger.append("goal_resolved", step=s.step,
                        kinds=[d.kind for d in resolution.deliverables],
                        problems=[d.problem for d in resolution.deliverables],
                        reads=[list(r) for r in resolution.reads])
        return resolution

    def _which_amount(self, s: _TurnState, spec: GoalSpec, resolution: Resolution) -> Resolution:
        """"What is the amount of Acme's September invoice" when two invoices fit asks which
        one (owner ruling, 2026-09-27): a sum over a few records is an answer only when the
        request asks for a total. The records are listed as the choices."""
        if _TOTAL_WORDS.search(s.message.text):
            return resolution
        out = list(resolution.deliverables)
        one = _ONE_RECORD.search(s.message.text)
        for i, (d, r) in enumerate(zip(spec.deliverables, resolution.deliverables)):
            agg = r.aggregate
            if (one and d.kind == "list" and agg is not None and not r.problem
                    and 2 <= len(agg.records) <= _MAX_AMOUNT_CHOICES):
                # "Open that refund of Azure's in August" and two fit: which one.
                s.ledger.append("goal_one_record_ambiguous", step=s.step, count=agg.count)
                out[i] = replace(r, aggregate=None, page=None, candidates=tuple(agg.records),
                                 problem=f"{agg.count} records fit and the request names one")
                continue
            if not (d.kind == "aggregate" and d.metric is not None and d.metric.op == "sum"
                    and not d.metric.group_by and agg is not None
                    and 2 <= agg.count <= _MAX_AMOUNT_CHOICES and not r.problem):
                continue
            try:
                listed = self.goal_port.resolve(
                    GoalSpec(deliverables=(replace(d, kind="list"),)),
                    context=s.approvals.context(s.step, "resolve"),
                    current_path=_path_of(s.obs.url)).deliverables[0]
            except Exception:  # noqa: BLE001 -- the sum stands when the records cannot be listed
                continue
            names = tuple(listed.aggregate.records) if listed.aggregate else ()
            if len(names) < 2:
                continue
            s.ledger.append("goal_amount_ambiguous", step=s.step, count=agg.count)
            out[i] = replace(r, aggregate=None, candidates=names,
                             problem=f"{agg.count} records fit and the request asks for one amount")
        return replace(resolution, deliverables=tuple(out))

    def _not_chosen_for(self, s: _TurnState, spec: GoalSpec, resolution: Resolution) -> Resolution:
        """The one record is the request's to pick, not the model's: when the declaration
        names a record the request never names, and what the request does say fits
        several records, that one among them included, the person is asked which.

        "Open Azure's refund of August" with two such refunds is a question, whichever of
        the two a reading happened to name. A record the words name ("S00018"), that the
        request's own amounts and days picked out before resolving, or that a description
        the request gives picks out alone, stands.
        """
        text = _norm(s.message.text)
        named = ({_norm(getattr(k, "name", "")) for k in s.references}
                 | {_norm(name) for name in s.warranted})
        out = list(resolution.deliverables)
        for i, (d, r) in enumerate(zip(spec.deliverables, resolution.deliverables)):
            chosen = _norm(d.record)
            if (d.kind not in ("page", "fact", "handoff") or d.rules or r.problem or not chosen
                    or chosen in text or chosen in named
                    or not ((d.metric and d.metric.filters) or d.page.search)):
                continue
            try:
                described = self.goal_port.resolve(
                    GoalSpec(deliverables=(replace(d, record=""),)),
                    context=s.approvals.context(s.step, "resolve"),
                    current_path=_path_of(s.obs.url)).deliverables[0]
            except Exception:  # noqa: BLE001 -- the declared record stands when the check cannot run
                continue
            fits = described.candidates
            if len(fits) < 2 or not any(chosen == _norm(c) for c in fits):
                continue
            s.ledger.append("goal_record_not_chosen", step=s.step, count=len(fits))
            out[i] = Resolved(kind=d.kind, candidates=tuple(fits),
                              problem=f"{len(fits)} records fit and the request names none of them")
        return replace(resolution, deliverables=tuple(out))

    # -- pages ------------------------------------------------------------------------
    def _dispatch(self, s: _TurnState, action: str, args: dict, *, navigating: bool = False):
        """One page command under write control, with its receipt on the record."""
        if action in PAGE_OPERATIONS:
            # Every page operation is under the turn's one leave; asked for at the first.
            reason, detail = s.approvals.confirm_operation(
                s.step + 1, s.obs, action=action, url=str(args.get("url") or ""))
            if reason:
                s.terminal_reason, s.detail = reason, detail
                return None
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
        s.approvals.settle_operation()
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
        last_confirmed_epoch = -1
        last_names: frozenset[str] | None = None
        for _ in range(_READY_WAITS):
            obs = s.obs
            path_ok = not expected_path or _path_of(obs.url) == expected_path
            # An application that reports what it rendered (a record route, the active
            # view, its facets) is believed when it does; one that reports nothing of the
            # kind counts as rendered when it is not pending and shows some controls,
            # and the two consecutive observations below must then list the same ones.
            markers = bool(obs.record_route) or bool(obs.active_view) or obs.page_facets is not None
            reports = markers or obs.render_state in ("ready", "pending")
            rendered = ((bool(obs.record_route) if record else bool(obs.active_view))
                        or obs.page_facets is not None) if markers else (
                not reports and bool(obs.elements)) or obs.render_state == "ready"
            names = frozenset(e.name for e in obs.elements)
            if obs.render_state == "blocked":
                return False
            if path_ok and obs.render_state != "pending" and rendered:
                # Seen rendered twice, half a second apart: the header controls and tabs
                # of a form arrive a beat after its route does.
                if obs.epoch != last_confirmed_epoch and (markers or names == last_names
                                                          or last_names is None):
                    confirmed += 1
                    last_confirmed_epoch = obs.epoch
                last_names = names
                if confirmed >= 2:
                    return True
            else:
                confirmed = 0
                last_confirmed_epoch = -1
                last_names = None
            receipt = self._dispatch(s, "wait_for", {"ms": 500})
            if receipt is None:
                return False
        return False

    def _reach(self, s: _TurnState, plan: PagePlan) -> _PageOutcome:
        out = _PageOutcome()
        target = plan.url
        if target and (_path_of(s.obs.url) != _path_of(target)
                       or urlsplit(target).query != urlsplit(s.obs.url).query):
            reason, detail = s.approvals.confirm_operation(
                s.step + 1, s.obs, action="goto", url=target)
            if reason:
                s.terminal_reason, s.detail = reason, detail
                out.reached = False
                out.unmet.append(_Unmet("not_confirmed"))
                return out
            receipt = self._dispatch(s, "goto", {"url": target}, navigating=True)
            if receipt is not None and not receipt.ok and receipt.error_code == "stale_observation" \
                    and not s.terminal_reason:
                # The page changed under the turn between looking and acting: look again
                # and act once more on what is there now.
                self._dispatch(s, "observe", {})
                receipt = self._dispatch(s, "goto", {"url": target}, navigating=True)
            if receipt is None:
                out.reached = False
                return out
        if not self._wait_ready(s, plan.expected_path,
                                record=bool(plan.section or plan.highlight
                                            or re.search(r"/\d+$", plan.expected_path))):
            if s.terminal_reason:
                out.reached = False
                return out
            out.reached = False
            out.unmet.append(_Unmet("not_opened" if s.obs.render_state != "blocked"
                                    else "error_dialog"))
            out.url = s.obs.url
            return out
        for name in plan.facets_on:
            if not self._facet(s, name, active=True):
                out.unmet.append(_Unmet("filter_on", name))
        for name in plan.facets_off:
            if not self._facet(s, name, active=False):
                out.unmet.append(_Unmet("filter_off", name))
        if plan.section and not self._select_tab(s, plan.section):
            out.unmet.append(_Unmet("section", plan.section))
        if plan.highlight and plan.fill:
            # The value goes in first, so the highlight marks a field already showing it.
            element = self._find_input(s, plan)
            if element is not None:
                out.filled, out.fill_verified = self._fill(s, element, plan)
            if not out.filled:
                out.fill_failed = plan.fill_label or plan.fill
        if plan.highlight:
            control, menu = self._highlight(s, plan.highlight)
            if control:
                out.highlighted, out.menu = control, menu
            else:
                out.unmet.append(_Unmet("control", plan.highlight))
        if plan.view and not self._view_shown(s.obs, plan.view):
            out.unmet.append(_Unmet("view", plan.view))
        out.url = s.obs.url
        out.reached = _path_of(s.obs.url) == (plan.expected_path or _path_of(s.obs.url))
        s.ledger.append("goal_page_settled", step=s.step, reached=out.reached,
                        unmet=[str(u) for u in out.unmet], url=s.obs.url,
                        render_state=s.obs.render_state, active_view=s.obs.active_view,
                        record_route=s.obs.record_route,
                        selected_controls=list(s.obs.selected_controls)[:8],
                        facets=[str(f.get("name", "")) for f in (s.obs.page_facets or [])][:8],
                        elements=len(s.obs.elements),
                        roles=sorted({e.role for e in s.obs.elements})[:12],
                        controls=[(e.role, e.name[:40], {k: v for k, v in e.attrs.items()
                                                          if k in ("role", "aria-selected", "selected", "field")})
                                  for e in s.obs.elements[:16]])
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
        tab = next((e for e in obs.elements if _is_tab(e) and _norm(e.name) == wanted),
                   None)
        if tab is None:
            tab = next((e for e in obs.elements
                        if _is_tab(e) and wanted and wanted in _norm(e.name)), None)
        return tab

    @staticmethod
    def _tab_selected(obs: PageObservation, wanted: str) -> bool:
        if any(_norm(c) == wanted for c in obs.selected_controls):
            return True
        for element in obs.elements:
            if _is_tab(element) and _norm(element.name) == wanted:
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

    def _fill(self, s: _TurnState, element, plan: PagePlan) -> tuple[str, bool]:
        """Put the plan's value into the field and say what the page then shows.

        Typed text and numbers go in as they are; a choice is picked from a native
        select by its option value, else by its visible label; a linked record is typed
        by name and then chosen among the suggestions the field itself offers, so only a
        record the application proposes is ever chosen. Nothing here saves: the page
        write guard holds throughout, and saving is the person's.
        """
        shown = plan.fill_label or plan.fill
        if plan.fill_kind == "choice":
            receipt = self._dispatch(s, "select", {"ref": element.ref, "epoch": s.obs.epoch,
                                                   "value": plan.fill})
            if receipt is None or not receipt.ok:
                if s.terminal_reason:
                    return "", False
                again = self._find_input(s, plan)
                if again is None:
                    return "", False
                receipt = self._dispatch(s, "select", {"ref": again.ref, "epoch": s.obs.epoch,
                                                       "value": plan.fill_label})
                if receipt is None or not receipt.ok:
                    return "", False
        else:
            receipt = self._dispatch(s, "fill", {"ref": element.ref, "epoch": s.obs.epoch,
                                                 "value": plan.fill})
            if receipt is None or not receipt.ok:
                return "", False
            if plan.fill_kind == "link":
                self._dispatch(s, "wait_for", {"ms": 600})
                if s.terminal_reason:
                    return "", False
                wanted = _norm(plan.fill)
                options = [e for e in s.obs.elements
                           if e.role in _SUGGESTION_ROLES and e.name and _norm(e.name) == wanted]
                if not options:
                    options = [e for e in s.obs.elements
                               if e.role in _SUGGESTION_ROLES and e.name
                               and _norm(e.name).startswith(wanted)]
                if len(options) != 1:
                    s.ledger.append("goal_fill_suggestion_missing", step=s.step,
                                    suggestions=len(options))
                    return "", False
                receipt = self._dispatch(s, "click", {"ref": options[0].ref, "epoch": s.obs.epoch})
                if receipt is None or not receipt.ok:
                    return "", False
        self._dispatch(s, "wait_for", {"ms": 400})
        if s.terminal_reason:
            return "", False
        current = self._find_input(s, plan)
        value = str((current.attrs.get("value") if current is not None else "") or "")
        verified = bool(value) and (_norm(value) == _norm(shown)
                                    or set(re.findall(r"\d+", shown)) <= set(re.findall(r"\d+", value))
                                    and bool(re.findall(r"\d+", shown)))
        s.ledger.append("goal_field_filled", step=s.step, control=plan.highlight,
                        kind=plan.fill_kind, verified=verified, shown=bool(value))
        return shown, verified

    def _earlier(self) -> tuple[str, str]:
        """This conversation's last turns as the model reads them (what the person asked,
        what the assistant replied, clipped), and the person's last request."""
        context = self.conversation_state.model_context() or {}
        turns = list(context.get("prior_turns") or [])[-_EARLIER_TURNS:]
        lines = []
        for turn in turns:
            lines.append("person: " + " ".join(str(turn.get("user", "")).split())[:300])
            lines.append("assistant: " + " ".join(str(turn.get("assistant", "")).split())
                         [:_EARLIER_REPLY_CHARS])
        before = " ".join(str(turns[-1].get("user", "")).split())[:300] if turns else ""
        return "\n".join(lines), before

    def _continued(self, message: AgentTurnMessage) -> str | None:
        """The request a reply to this conversation's pending question continues: the
        earlier request with the record the person chose. None when the message does not
        answer a pending choice, which then stands on its own."""
        pending = self.conversation_state.pending_clarification
        asked = self.conversation_state.last_user_text()
        if not pending or not asked or pending.get("response_kind") != "single_choice":
            return None
        reply = " ".join(message.text.split()).casefold()
        chosen = next((c for c in pending["choices"]
                       if reply.strip("。.!！ ") == c["choice_id"].casefold()), None)
        if chosen is None:
            chosen = next((c for c in pending["choices"]
                           if c["record_ref"].casefold() in reply), None)
        if chosen is None:
            return None
        return (f"{asked}（指的是 {chosen['record_ref']}）" if _CJK.search(asked)
                else f"{asked} (meaning {chosen['record_ref']})")

    def _rules_answer(self, s: _TurnState, d) -> Resolved:
        """The rules a deliverable names, as facts with their citations. A rule id the
        index does not have is a problem the model can repair, since the ids it may
        name are the ones it was shown."""
        index = s.full_index or s.index
        facts, citations, unknown, shown = [], [], [], set()
        for rule_id in d.rules:
            found = index.rule(rule_id) if index is not None else None
            if found is None:
                unknown.append(rule_id)
                continue
            entity, rule = found
            # A control's or field's rules come as a set: the one named, the others
            # about the same thing (the same field in several views, its meaning and
            # when it can be changed, the same button under several conditions), and
            # the rules of the same operation (what the button opens, what its method
            # refuses), each with its citation.
            group = [rule] + [r for r in entity.rules
                              if r is not rule and r.about == rule.about][:_SAME_ABOUT_RULES]
            if rule.subject:
                group += [r for r in entity.rules if r not in group
                          and r.subject == rule.subject][:_SAME_OPERATION_RULES]
            group = [(entity, r) for r in group]
            group += [pair for item in list(group) for pair in
                      _same_refusal_elsewhere(index, entity, item[1])][:_SAME_REFUSAL_ELSEWHERE]
            for owner, item in group:
                if item.id in shown:
                    continue
                shown.add(item.id)
                fact = Fact(subject=f"{owner.label} · {item.about}", label=item.kind,
                            value=item.text)
                # The same rule written in two views is said once and cited twice.
                if fact not in facts:
                    facts.append(fact)
                if item.citation:
                    citations.append(item.citation)
        if not facts:
            return Resolved(kind=d.kind, problem="no such rule: " + ", ".join(unknown),
                            repair="rules must be ids shown under RULES, such as r1")
        return Resolved(kind="fact", facts=tuple(facts), citations=tuple(dict.fromkeys(citations)),
                        problem=("unknown rule ids: " + ", ".join(unknown)) if unknown else "")

    def _find_input(self, s: _TurnState, plan: PagePlan):
        """The input the plan's value goes into: the one carrying the application's
        field identifier when the page exposes it, else the input the label names."""
        if plan.fill_field:
            inputs = [e for e in s.obs.elements if e.role in _INPUT_ROLES
                      and str(e.attrs.get("field") or "") == plan.fill_field]
            if len(inputs) == 1:
                return inputs[0]
        return self._find_control(s, plan.highlight, input=True)

    def _find_control(self, s: _TurnState, control: str, *, input: bool = False,
                      among=None, in_menu: bool = False, exact: bool = False):
        """The page element a control name means: for a highlight, any non-navigation
        element (a field's label marks the field well); for a value to enter, only the
        input itself, since a label is not something that can be typed into. `among`
        narrows the search to some elements; `in_menu` admits the items of a menu the
        turn opened; `exact` accepts only an exact name."""
        # The declaration may carry the index's type annotation ("Due Date(date)").
        wanted = _norm(re.sub(r"\((?:date|money|number|html|bool|text)\)$", "", control.strip()))
        excluded = _NO_HIGHLIGHT_ROLES - {"menuitem"} if in_menu else _NO_HIGHLIGHT_ROLES
        candidates = [e for e in (s.obs.elements if among is None else among)
                      if (e.role in _INPUT_ROLES if input else e.role not in excluded)]
        # A button by its exact name first, then any control (a field's input is named by
        # its label) by exact name, then by prefix.
        element = next((e for e in candidates if e.role == "button" and _norm(e.name) == wanted),
                       None)
        if element is None:
            element = next((e for e in candidates if _norm(e.name) == wanted), None)
        if element is None and exact:
            return None
        if element is None:
            element = next((e for e in candidates
                            if wanted and _norm(e.name).startswith(wanted)), None)
        if element is None:
            # The form labels a field by fewer words than the index does ("Bill Date"
            # for Invoice/Bill Date): a control whose words are all in the name asked.
            asked = _tokens(control)
            element = next((e for e in candidates
                            if e.name and _tokens(e.name) and _tokens(e.name) <= asked), None)
        return element

    def _highlight(self, s: _TurnState, control: str) -> tuple[str, str]:
        """Mark the control for the person; returns (its name, the menu it is in).

        A control of that exact name on the page comes first; then the collapsed menus;
        only then a looser match (a prefix, or fewer words)."""
        element, menu = self._find_control(s, control, exact=True), ""
        if element is None:
            element, menu = self._reveal_control(s, control)
        if element is None:
            element = self._find_control(s, control)
        if element is None:
            return "", ""
        receipt = self._dispatch(s, "highlight", {"ref": element.ref, "epoch": s.obs.epoch})
        if receipt is None or not receipt.ok:
            return "", ""
        s.ledger.append("page_handoff_ready", step=s.step, command_id=receipt.command_id,
                        observation_epoch=s.obs.epoch, submitted=False, persisted=False)
        # A label's first line is its name; a help marker on a later line is not.
        return _first_line(element.name), menu

    def _reveal_control(self, s: _TurnState, control: str):
        """A control that sits in a collapsed menu ("Delete" under a record's actions):
        open the menus the page marks as openable, in page order and at most
        `_MAX_REVEALS`, and look for it among what each one shows. Nothing in a menu is
        chosen; the Runtime opens only a control that declares itself collapsed. Returns
        (element, menu name) or (None, "")."""
        opened: set[str] = set()
        for _ in range(_MAX_REVEALS):
            toggle = next((e for e in s.obs.elements if e.attrs.get("revealable") == "true"
                           and _norm(e.name) not in opened), None)
            if toggle is None or s.terminal_reason:
                break
            opened.add(_norm(toggle.name))
            before = Counter((e.role, _norm(e.name)) for e in s.obs.elements)
            path = _path_of(s.obs.url)
            receipt = self._dispatch(s, "reveal", {"ref": toggle.ref, "epoch": s.obs.epoch})
            if receipt is None or not receipt.ok or _path_of(s.obs.url) != path:
                break
            shown = _appeared(before, s.obs.elements)
            if not shown:
                # The menu renders after the click: look once more before moving on.
                self._dispatch(s, "wait_for", {"ms": 300})
                shown = _appeared(before, s.obs.elements)
            s.ledger.append("goal_menu_revealed", step=s.step, menu=_first_line(toggle.name),
                            shown=len(shown))
            element = self._find_control(s, control, among=shown, in_menu=True)
            if element is not None:
                return element, _first_line(toggle.name)
        return None, ""

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
                          operation_control=s.approvals.operation_result(),
                          success_claim=("not_claimed" if terminal != "agent_finished"
                                         or s.write_intent
                                         else "verified"),
                          detail=s.detail)
        s.ledger.append("run_finished", task_ref=s.run_id, arm="goal",
                        terminal_reason=terminal, steps_used=s.step, wall_seconds=wall,
                        agent_said=text[:300], page_actions=s.page_actions,
                        operation_control=agent.operation_control or {})
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
            clarification=s.clarification if status == AgentTurnStatus.NEEDS_INPUT else None)
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


def _numbers(text: str) -> set[str]:
    """Numbers as written, without separators or trailing zero decimals."""
    out = set()
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text or ""):
        value = raw.replace(",", "")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        out.add(value)
    return out


def _coverage_gaps(spec: GoalSpec, parts: list, text: str, today: str,
                   candidates: tuple = ()) -> tuple[str, ...]:
    """What the request asked that the declaration leaves out, judged by shape only:
    values asked for (the first step said so) with only pages or handoffs declared; a
    code, amount or day the request wrote that appears nowhere in the declaration nor in
    the candidate values it chose."""
    gaps = []
    reads = ("fact", "list", "aggregate")
    asked = [p for p in parts if p.get("kind") in reads]
    if asked and not any(d.kind in reads for d in spec.deliverables):
        gaps.append("the request asks for values (" + "; ".join(
            f"{p['kind']} on {p['entity']}" for p in asked) + ") but the declaration only "
            "opens a page or hands over; declare the values asked for")
    opened = [p for p in parts if p.get("kind") == "page"]
    if opened and not asked and not any(d.kind in ("page", "handoff") for d in spec.deliverables):
        gaps.append("the request asks to open a page (" + "; ".join(
            f"page on {p['entity']}" for p in opened) + ") but the declaration only reads "
            "values; declare the page to open")
    declared = json.dumps(spec.to_json(), ensure_ascii=False).casefold()
    chosen = " ".join(f"{c.display} {c.detail}" for c in candidates
                      if c.display.casefold() in declared).casefold()
    covered = declared + " " + chosen
    numbers = _numbers(covered)
    for ref in references(without_days(text)):
        if not re.search(r"\d", ref) or ref.casefold() in covered:
            continue
        if _numbers(ref) and _numbers(ref) <= numbers:
            continue
        gaps.append(f"the request says {ref!r} but the declaration does not use it; put it "
                    "where it belongs (a record, a condition's value, a search)")
    for day in days(text, today):
        if day not in covered and day[5:] not in covered:
            gaps.append(f"the request names the day {day} but the declaration has no "
                        "condition on it")
    return tuple(gaps[:4])


def _clarification(s: _TurnState, names: tuple[str, ...]) -> dict:
    """The question asking which of several records the person means, each with what
    tells it apart (from the values this turn looked up), and the choices a reply may
    name by number or by name."""
    details = {c.display: c.detail for c in s.candidates}
    choices = []
    for number, name in enumerate(dict.fromkeys(names), start=1):
        if number > 6:
            break
        detail = details.get(name, "")
        label = f"{name}（{detail}）" if detail and s.chinese else (
            f"{name} ({detail})" if detail else name)
        choices.append({"choice_id": str(number), "label": label[:200], "record_ref": name[:500]})
    lines = [f"{c['choice_id']}. {c['label']}" for c in choices]
    question = (("你指的是哪一个？\n" + "\n".join(lines) + "\n回复编号或名称即可。") if s.chinese
                else ("Which one do you mean?\n" + "\n".join(lines)
                      + "\nReply with the number or the name."))
    return {"schema_version": 1, "clarification_id": f"{s.run_id}-choice"[:100],
            "task_id": s.run_id[:100], "question": question[:500],
            "response_kind": "single_choice", "choices": choices}


def _status_for(terminal_reason: str) -> AgentTurnStatus:
    if terminal_reason == "agent_finished":
        return AgentTurnStatus.FINISHED
    if terminal_reason == "agent_refused":
        return AgentTurnStatus.REFUSED
    if terminal_reason == "needs_clarification":
        return AgentTurnStatus.NEEDS_INPUT
    if terminal_reason in {"user_cancelled", "operation_not_confirmed", "approval_expired"}:
        return AgentTurnStatus.CANCELLED
    return AgentTurnStatus.FAILED


def _same_refusal_elsewhere(index, entity, rule) -> list:
    """The same refusal on other entities: one message, several places that raise it.

    Which of them the person means is theirs to say, not the declaration's; a check
    written on both the order and its template, or on a payment and the wizard that
    makes it, is answered in both places."""
    message = _refusal_message(rule)
    if not message or index is None:
        return []
    return [(other, r) for other in index.entities if other is not entity
            for r in other.rules
            if _refusal_message(r) == message][:_SAME_REFUSAL_ELSEWHERE]


def _refusal_message(rule) -> str:
    """The first sentence of the message a refusal quotes, its placeholders left out:
    the words two places raising the same refusal share."""
    found = _REFUSAL_MESSAGE.search(rule.text) if rule.kind == "refusal" else None
    if found is None:
        return ""
    words = " ".join(_PLACEHOLDER.sub("", found.group(1)).split())
    first = re.split(r"(?<=[.!?])\s", words, maxsplit=1)[0]
    return first if len(first) >= 20 else ""


def _achieved(resolved: Resolved, outcome: _PageOutcome | None) -> bool:
    if resolved.problem:
        return False
    if resolved.kind in ("fact",) and resolved.facts:
        return True
    if resolved.kind == "fact" and resolved.aggregate is not None and resolved.aggregate.records:
        # A fact about some lines answered by listing the lines it is about.
        return True
    if resolved.kind in ("aggregate", "list") and resolved.aggregate is not None:
        return True
    if outcome is not None:
        return outcome.reached and not outcome.unmet
    return False


def _is_tab(element) -> bool:
    """A tab is an element the page marks as one, or one that carries selection state
    the way tabs do; a runtime may report a `<button role="tab">` as a button."""
    return element.role == "tab" or "aria-selected" in element.attrs


class _ContractBroken(Exception):
    """The port answered a declaration with deliverables that do not match it."""


def _needs_beyond(d, offered: frozenset[str]) -> frozenset[str]:
    """The optional capabilities a deliverable needs that the port does not offer."""
    needs = set()
    if d.page.filters_on or d.page.filters_off or d.page.group_by or (
            d.metric is not None and d.metric.page_filters):
        needs.add("facets")
    if d.section:
        needs.add("sections")
    if d.page.search:
        needs.add("search")
    if d.kind == "handoff":
        needs.add("handoffs")
        if d.value:
            needs.add("fill")
    return frozenset(needs) - offered


def _unresolved(spec: GoalSpec, resolution: Resolution) -> int:
    """Deliverables that came to nothing, plus a change asked for with nothing to point at."""
    count = sum(1 for d in resolution.deliverables
                if d.problem and not (d.facts or d.aggregate or d.page))
    if spec.write_intent and not any(d.kind == "handoff" for d in spec.deliverables):
        count += 1
    return count


def _repairable(spec: GoalSpec, resolution: Resolution) -> tuple[str, ...]:
    """What to tell the model before its one second declaration; empty when nothing
    about the declaration, as opposed to the data, is what failed."""
    problems = [d.repair for d in resolution.deliverables if d.repair]
    if spec.write_intent and not any(d.kind == "handoff" for d in spec.deliverables):
        problems.append("the person asks for a change (write_intent is true) but no "
                        "deliverable is a handoff: declare a handoff on the record meant, "
                        "with action set to the button or the field label to change")
    return tuple(dict.fromkeys(problems))


def _render(resolved: Resolved, outcome: _PageOutcome | None, zh: bool) -> list[str]:
    lines: list[str] = []
    if resolved.facts:
        for fact in resolved.facts:
            lines.append(f"**{fact.subject} · {fact.label}**：{fact.value}" if zh
                         else f"**{fact.subject} · {fact.label}**: {fact.value}")
        if resolved.citations:
            # The rule's source, verbatim: the lines it names are hashed, so a reader
            # or an evaluator can check the citation names exactly those bytes.
            lines.append(("依据（部署版本源码中的规则，不是当前数据）：" if zh
                          else "Source (a rule of the deployed version's source, not current data):")
                         + "\n" + "\n".join(resolved.citations))
    agg = resolved.aggregate
    if agg is not None:
        # Records listed are shown as a list, whichever kind asked for them (a fact
        # about some lines is answered with those lines).
        if resolved.kind == "list" or agg.records:
            # A record's name may carry a line break (a product's description under its
            # name); one record reads as one line.
            records = [" ".join(str(r).split()) for r in agg.records]
            head = (f"**{agg.subject}**：共 {agg.count} 条" if zh
                    else f"**{agg.subject}**: {agg.count} records")
            if not agg.complete:
                head += "（仅列出前 20 条）" if zh else " (first 20 shown)"
            if len(records) == 1:
                head += ("：" if zh else ": ") + records[0]
            elif records:
                # Numbered, one to a line, so that "the second one" in a reply means one
                # record and the person can see which.
                head += ("：" if zh else ":") + "".join(
                    f"\n{number}. {record}" for number, record in enumerate(records, 1))
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
    if (resolved.problem and resolved.kind in ("list", "aggregate", "fact")
            and not (resolved.facts or resolved.aggregate)):
        # A question that went unanswered stays unanswered in the reply, whatever page
        # was opened beside it: the page is where to look, not an answer.
        where = ""
        if resolved.page is not None:
            where = (f"（可在 {resolved.page.label} 页面上自行核对）" if zh
                     else f" (you can check on {resolved.page.label})")
        if resolved.missing:
            # Only the name was looked up: nothing is called that, which is not to say
            # nothing the person described exists (a Chinese description of an English
            # product name finds no name and still means a real product).
            lines.append((f"没有找到名为「{resolved.missing}」的记录。{where}") if zh
                         else f"Nothing named \"{resolved.missing}\" was found.{where}")
        else:
            lines.append(_not_done(resolved, zh) + where)
        return lines
    if resolved.page is not None and outcome is not None:
        plan = resolved.page
        if resolved.kind == "handoff":
            if outcome.highlighted and outcome.filled:
                # The field holds the value; saving is the person's. A form that saves
                # itself on leaving the page needs a deliberate discard when unwanted.
                check = "" if outcome.fill_verified else ("（页面未回显，请核对）" if zh
                                                          else " (not shown back by the page; please check)")
                lines.append((f"已打开 {plan.label}，已在「{outcome.highlighted}」填入"
                              f"「{outcome.filled}」{check}并标出，请你核对后自行保存；"
                              "助手只做准备，提交由你完成。不需要这次修改就点放弃。") if zh else
                             (f"Opened {plan.label} and entered “{outcome.filled}” in "
                              f"“{outcome.highlighted}”{check}; please check it and save it "
                              "yourself. The assistant prepares, you submit. Discard it if "
                              "the change is not wanted."))
            elif outcome.highlighted and outcome.fill_failed:
                lines.append((f"已打开 {plan.label} 并在页面上标出「{outcome.highlighted}」，"
                              f"但没能填入「{outcome.fill_failed}」，请你自行填写并保存；"
                              "助手只做准备，提交由你完成。") if zh else
                             (f"Opened {plan.label} and highlighted “{outcome.highlighted}”, "
                              f"but could not enter “{outcome.fill_failed}”; please enter and "
                              "save it yourself. The assistant prepares, you submit."))
            elif outcome.highlighted and outcome.menu:
                # The control is an item of a menu the turn opened; the page may close the
                # menu when the person clicks elsewhere, so the reply says where it is.
                lines.append((f"已打开 {plan.label}，展开「{outcome.menu}」菜单并标出"
                              f"「{outcome.highlighted}」，请你核对后自行点击；菜单若已收起，"
                              f"点「{outcome.menu}」即可再找到它。助手只做准备，操作由你完成。")
                             if zh else
                             (f"Opened {plan.label}, opened the “{outcome.menu}” menu and "
                              f"highlighted “{outcome.highlighted}”; please check it and press "
                              f"it yourself. If the menu has closed, it is under "
                              f"“{outcome.menu}”. The assistant prepares, you act."))
            elif outcome.highlighted:
                # A handoff is a completed preparation, not a refusal: the reply says what
                # is ready and who acts next, without the words a refusal uses.
                lines.append((f"已打开 {plan.label} 并在页面上标出「{outcome.highlighted}」，"
                              "请你核对后自行点击；助手只做准备，提交由你完成。") if zh else
                             (f"Opened {plan.label} and highlighted “{outcome.highlighted}”; "
                              "please check it and press it yourself. The assistant prepares, "
                              "you submit."))
            else:
                # The limitation comes first: the page holds no such control for this
                # account, and nothing was done. Opening the page is not an achievement.
                lines.append((f"{plan.label} 页面上没有「{plan.highlight}」这个控件，可能是你的账号"
                              "没有这项操作的权限；助手没有执行任何操作。") if zh else
                             (f"{plan.label} shows no “{plan.highlight}” control, perhaps because "
                              "your account has no permission for it; the assistant did nothing."))
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
            if plan.missing_view:
                # The page is open, in the only kind of view it has; the one asked for
                # is not something this page offers, and the reply says so.
                suffix += (f"，但这个页面不支持 {plan.missing_view} 视图" if zh
                           else f", but this page does not support a {plan.missing_view} view")
            lines.append((f"已打开：{what}{suffix}") if zh else f"Opened: {what}{suffix}")
        elif outcome.reached and all(u.kind == "filter_on" for u in outcome.unmet):
            # The page is open; a filter or group it asked for has no such choice there.
            # Said as what the page lacks, not as the assistant failing, because the
            # page itself was delivered.
            names = "、".join(u.name for u in outcome.unmet) if zh else \
                ", ".join(u.name for u in outcome.unmet)
            lines.append((f"已打开：{plan.label}（页面上没有这个筛选项：{names}）") if zh
                         else f"Opened: {plan.label} (the page has no such filter: {names})")
        else:
            unmet = "、".join(u.said(zh) for u in outcome.unmet) if zh else \
                ", ".join(u.said(zh) for u in outcome.unmet)
            lines.append((f"打开 {plan.label} 未完成：{unmet or '页面未到达'}。") if zh
                         else f"Opening {plan.label} was not completed: {unmet or 'page not reached'}.")
    if resolved.missing and not (resolved.facts or resolved.aggregate or resolved.page):
        lines.append((f"没有找到名为「{resolved.missing}」的记录。") if zh
                     else f"Nothing named \"{resolved.missing}\" was found.")
    elif resolved.problem and not (resolved.facts or resolved.aggregate or resolved.page):
        lines.append(_not_done(resolved, zh))
    return lines


#: The optional capabilities of a goal port (`OPTIONAL`), as a person would name them.
_OFFER_ZH = {"facets": "页面筛选与分组", "sections": "页签", "lines": "明细行",
             "handoffs": "指出控件", "fill": "预先填值", "search": "页面搜索",
             "source": "源码规则", "values": "候选值"}
_OFFER_EN = {"facets": "page filters and groups", "sections": "tabs", "lines": "line items",
             "handoffs": "pointing at controls", "fill": "entering values",
             "search": "page search", "source": "rules from source", "values": "candidate values"}


def _not_done(resolved: Resolved, zh: bool) -> str:
    """A part that did not get done, as the person is told it. The resolver's own
    reason ("filter 'p262' is not usable here") is written for the declarer, in the
    index's terms, and stays in the audit and the repair; the person hears that it was
    not done and, when several records fit, which ones."""
    if resolved.unoffered:
        # What the application does not offer is the person's to know, plainly.
        offers = [(_OFFER_ZH if zh else _OFFER_EN).get(name, name) for name in resolved.unoffered]
        return (f"这个应用没有提供{'、'.join(offers)}，这一项无法完成。" if zh
                else f"This application does not offer {', '.join(offers)}, so this part "
                     "could not be done.")
    names = "、".join(resolved.candidates) if zh else ", ".join(resolved.candidates)
    if zh:
        return "无法完成这一项。" + (f"符合的记录有：{names}。" if names else "")
    return "Could not do this part." + (f" Records that fit: {names}." if names else "")


__all__ = ["GoalTurnExecutor"]
