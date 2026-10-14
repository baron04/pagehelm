"""A goal-directed turn: declare, resolve, act, check, reply.

Same wire contract as `AgentTurnExecutor` -- a channel, a config, a chat, a bound
application -- and the same fail-closed edges: every page switch is confirmed by the
person, every page command runs under the write control the application declares, and no
control that could write is ever pressed. The model declares the request; independent
readings and bounded repairs may add declarations. Resolution is server code the audit
can replay.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field, replace
import functools
import json
import re
import threading
import time
from typing import Callable, NamedTuple
from urllib.parse import urlsplit

from ..business.approvals import PAGE_OPERATIONS, ApprovalWatch, OperationPlan
from ..business.control import ApprovalGate, ApprovalGrant
from ..business.errors import BusinessToolRequestError
from ..business.writes import WriteWatch
from ..business.tools.contracts import form_control_unavailable, BusinessToolResult
from ..answer.conclusion import ConclusionRefused
from ..composition.binding import BoundApplication
from ..execution.product import (AgentAnswerContext, AgentTurnError, AgentTurnErrorCode,
                                 AgentTurnExecution, AgentTurnMessage, AgentTurnStatus,
                                 render_neutral_user_answer, _status_for)
from ..execution.runstate import RunResult
from ..execution.receipt import MeasuredChat, TurnReceipt
from ..execution.events import observed_turn
from ..execution.budget import TurnBudget
from ..execution.click_policy import ClickPolicy, click_policy_refusal
from ..modeling.chat import require_chat_port
from ..modeling.egress import AuditedChat
from ..modeling.provider import ProviderError
from ..modeling.spend import BudgetExhausted
from ..privacy import redact_sensitive_assignments, redact_session_handoffs
from ..schemas.browser import FORM_INPUT_CONTROLS, PageObservation
from ..transport.protocol import RemoteProtocolError
from ..state.conversation import ConversationState
from ..state.identity import PrincipalError
from ..state.ledger import SessionLedger
from ..transport.browser import RemoteBrowserClient, TransportError, TurnCancelled
from .ports import Fact, GoalPort, PagePlan, Resolution, Resolved, capabilities_of
from .spec import Filter, GoalSpec, Metric
from .ground import bind, days, ground, numbers_in, pick, render as render_values, without_days
from .index import conventions_named
from .narrow import (correct_records, locate, locate_names, neighbourhood, references,
                     summaries)
from .declaration_cache import DeclarationCache
from .forms import combine_forms, handoff_form, form_failure_message
from .understand import understand, understand_scope

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CJK = re.compile(r"[㐀-鿿]")
#: How long the page may take to settle after a switch, in half-second waits.
_READY_WAITS = 12
_RECORD_VIEW_ELEMENTS = 6
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
#: Bounded repairs, accepted on progress or a useful clarification at equal unresolved work.
_REPAIRS = 2
#: Independent readings added when two disagree, so that most of them can settle it.
_MORE_READINGS = 2
#: An ordinal picks one record out of several as an amount or a day does ("第二期", "第3张",
#: "the 2nd").
_ORDINAL = re.compile(r"第\s*(?:\d+|[一二两三四五六七八九十]+)|\b\d+(?:st|nd|rd|th)\b", re.I)
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
_INPUT_ROLES = FORM_INPUT_CONTROLS | {"searchbox"}
#: Roles a field's own suggestion list uses for the records it proposes.
_SUGGESTION_ROLES = frozenset({"option", "menuitem", "listitem", "link"})


#: Turns the person (or a card they left unanswered) stopped.
_STOPPED = ("user_cancelled", "operation_not_confirmed", "approval_expired", "approval_denied")


def _provider_failed(exc: ProviderError, chinese: bool) -> str:
    if getattr(exc, "code", "") == "provider_allowance_spent":
        return ("你今天的助手使用额度已用完，明天会自动恢复。" if chinese
                else "Your assistant allowance for today is used up; it starts again tomorrow.")
    return ("模型服务暂时没有响应，请稍后再试。" if chinese
            else "The model service did not respond; please try again.")


def _norm(text: str) -> str:
    return "".join(ch for ch in (text or "").casefold() if not ch.isspace()
                   ).replace("&amp;", "&")


def _said_apart(candidates: tuple[str, ...], text: str, details: dict,
                given: tuple[str, ...] = ()) -> str:
    """The one candidate that something only it shows is written in the request, else "".

    A candidate's name and detail are cut at brackets and separators; a part every
    candidate has tells nothing apart. A lone short number is not taken as such a part.
    `given` are the values the request asks to be entered: a word written as a new value
    ("change his company to X") says nothing about which record is meant, so it is left
    out of what the request is taken to say.
    """
    said = _norm(text)
    for value in sorted({_norm(v) for v in given if _norm(v)}, key=len, reverse=True):
        said = said.replace(value, " ")

    def parts(name: str) -> set[str]:
        cut = re.split(r"[（）()\[\]【】《》<>·/|,，;；:：\s]+", f"{name} {details.get(name, '')}")
        return {_norm(p) for p in cut if len(_norm(p)) >= 2 and not (p.isdigit() and len(p) < 4)}

    shown = {name: parts(name) for name in candidates}
    shared = set.intersection(*shown.values()) if shown else set()
    hits = [name for name in candidates if any(p in said for p in shown[name] - shared)]
    return hits[0] if len(hits) == 1 else ""


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-z\u3400-\u9fff]+", (text or "").casefold()) if t}


def _language(s) -> str:
    """The person's language as the resolver describes scopes in it."""
    return "zh" if s.chinese else ""

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
        if (deleting and (d.kind == "handoff" or (d.kind == "page" and d.action))
                and (d.record or (d.metric and d.metric.filters)) and not _DELETE.search(d.action or "")):
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


_NEW = frozenset({"create", "new", "add", "新增", "新建", "创建", "添加"})
_HIGHEST = re.compile(r"最高|最大|最多|最贵|\b(?:highest|largest|biggest|greatest)\b", re.I)
_LOWEST = re.compile(r"最低|最小|最少|最便宜|\b(?:lowest|smallest|least|cheapest)\b", re.I)
_FIRST_N = re.compile(r"前\s*[0-9一二两三四五六七八九十]+|\btop\s*\d+", re.I)


def _as_said(spec: GoalSpec, text: str, index) -> tuple[GoalSpec, tuple[str, ...]]:
    """Three spellings a declaration is often given that mean something the declaration
    has a proper place for. Returns (spec, what was read otherwise).

    A list ordered by a number, for a request that asks which are the highest or lowest
    and does not ask for a first so-many, is that number's extreme with every tie: an
    ordered list shows the answer somewhere in it, the extreme says it. A new record
    declared with conditions and no values is being given those as the values it starts
    with ("add a contact for this customer"): a record that does not exist has nothing
    to be found by. A field declared without a value, for a request that says to clear
    it, is that field with an empty value."""
    from .coverage import cleared_fields
    out, changed = [], []
    highest, lowest = bool(_HIGHEST.search(text or "")), bool(_LOWEST.search(text or ""))
    for d in spec.deliverables:
        if (d.kind == "list" and d.sort and not d.limit and highest != lowest
                and d.descending == highest and not _FIRST_N.search(text or "")
                and (d.metric is None or (d.metric.op == "count" and not d.metric.group_by))):
            filters = d.metric.filters if d.metric is not None else ()
            pages = d.metric.page_filters if d.metric is not None else ()
            d = replace(d, sort="", descending=False, metric=Metric(
                op="max" if highest else "min", field=d.sort, filters=filters, page_filters=pages))
            changed.append("extreme")
        if (d.kind == "handoff" and d.action.casefold() in _NEW and not d.record
                and not d.field_values and not d.value and d.metric is not None and d.metric.filters
                and all(f.op in ("", "=") and len(f.values) == 1 for f in d.metric.filters)
                and len({f.field.casefold() for f in d.metric.filters}) == len(d.metric.filters)):
            d = replace(d, metric=None, field_values=tuple(
                (f.field, f.values[0]) for f in d.metric.filters))
            changed.append("starting values")
        entity = index.entity(d.entity) if index is not None and d.entity else None
        if (d.kind == "handoff" and d.fields and not d.value and entity is not None
                and (d.field_values or not d.action)):
            cleared = cleared_fields(text or "", entity) or set()
            given = {k.casefold() for k, _ in d.field_values}

            def names(label: str) -> set[str]:
                """Every name the index gives the field this label is one name of."""
                return {_norm(n) for f in entity.fields for n in (f.key, f.label, *f.aliases)
                        if n and _norm(label) in {_norm(x) for x in (f.key, f.label, *f.aliases)}}
            empty = tuple(label for label in dict.fromkeys(d.fields)
                          if names(label) & cleared and label.casefold() not in given)
            if empty:
                d = replace(d, fields=tuple(f for f in d.fields if f not in empty),
                            field_values=d.field_values + tuple((label, "") for label in empty))
                changed.append("cleared")
        out.append(d)
    if not changed:
        return spec, ()
    return replace(spec, deliverables=tuple(out)), tuple(changed)


def _link_to_named(s: _TurnState, d) -> tuple[str, str] | None:
    """The declared entity's link to a record the request names (an order's Customer for
    a named customer): (the link field's label, that record's name), or None."""
    for index in (s.index, s.full_index):
        entity = index.entity(d.entity) if index is not None and d.entity else None
        if entity is None:
            continue
        for known in s.references:
            if known.entity == entity.key and _norm(known.name) == _norm(d.record):
                continue
            # The named record may be of the same entity (a company, for a contact picked
            # among its people): its link to itself is the condition then.
            field_ = next((f for f in entity.fields
                           if f.kind == "link" and f.relation == known.entity), None)
            if field_ is not None:
                return field_.label, known.name
    return None


def _answer_key(resolution: Resolution) -> tuple:
    """What a resolution answers, for counting readings that agree: a page by the address
    it opens (two readings opening one record's page agree, however they title it), the
    choices asked about as a set, and anything else as resolved, in full."""
    out = []
    for r in resolution.deliverables:
        if r.page is not None and not r.problem and not (r.facts or r.aggregate):
            out.append(("record_unavailable", r.page.url, r.page.missing_record)
                       if r.page.missing_record else ("page", r.page.url))
        elif len(r.candidates) >= 2 and not (r.facts or r.aggregate):
            out.append(("ask", tuple(sorted(r.candidates))))
        else:
            out.append(("resolved", r))
    return tuple(out)


def _asks(key: tuple) -> bool:
    return any(part[0] == "ask" for part in key)


def _answer_among(answer: Resolution, question: Resolution) -> bool:
    """Whether an answer is, deliverable by deliverable, the same as a question's or one
    of the choices it asks about (the record a page opens, or a fact is of, is among the
    names asked about)."""
    if len(answer.deliverables) != len(question.deliverables):
        return False
    for a, q in zip(answer.deliverables, question.deliverables):
        if len(q.candidates) < 2 or q.facts or q.aggregate:
            if _answer_key(Resolution(deliverables=(a,))) != _answer_key(Resolution(deliverables=(q,))):
                return False
            continue
        named = (a.page.label if a.page is not None else "") or (a.facts[0].subject if a.facts else "")
        if not named or named not in q.candidates:
            return False
    return True


def _finds(resolution: Resolution) -> bool:
    """Every deliverable resolved to something there: a value, a page, records, or the
    several records a description fits (which the person can be asked to choose from)."""
    return bool(resolution.deliverables) and all(
        (not r.problem and (bool(r.facts) or r.page is not None
                            or r.aggregate is not None))
        or len(r.candidates) >= 2 or bool(r.denied)
        for r in resolution.deliverables)


def _entity_over_page(spec: GoalSpec, text: str, index) -> tuple[GoalSpec, tuple[str, ...]]:
    """A count or list declared through a page that narrows its entity on its own
    (customers among contacts), when the request names the entity and not the page
    ("联系人里标记为公司的有几个"): every record of the entity is meant, so the page goes."""
    lowered = text.casefold()

    def says(names) -> bool:
        return any(n and n.casefold() in lowered for n in names)
    dropped = []
    out = []
    for d in spec.deliverables:
        page = index.page(d.page.key) if d.page.key and index is not None else None
        entity = index.entity(d.entity) if d.entity and index is not None else None
        if (d.kind in ("aggregate", "list") and page is not None and entity is not None
                and page.entity == entity.key and page.scope
                and says((entity.label,) + tuple(entity.aliases))
                and not says((page.label,) + tuple(page.aliases) + tuple(page.scope))):
            dropped.append(page.key)
            d = replace(d, page=replace(d.page, key=""))
        out.append(d)
    return (replace(spec, deliverables=tuple(out)), tuple(dropped)) if dropped else (spec, ())


def _said_of(record: str, text: str) -> str:
    """The longest run of a record's words the request writes, when it writes some of
    them and not the whole name; empty otherwise."""
    words = record.split()
    lowered = text.casefold()
    for size in range(len(words) - 1, 0, -1):
        for start in range(len(words) - size + 1):
            span = " ".join(words[start:start + size])
            if len(_norm(span)) >= 3 and span.casefold() in lowered:
                return span
    return ""


def _names_exactly(name: str, words: str) -> bool:
    """Whether a displayed name is the words and nothing more than a code in brackets
    ("[FURN_7777] Office Chair" for "Office Chair")."""
    rest = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", name.casefold().replace(words.casefold(), "", 1))
    return words.casefold() in name.casefold() and not re.search(r"\w", rest)


def _askable(resolution: Resolution | None) -> int:
    """Deliverables left unresolved with several records to ask the person about."""
    if resolution is None:
        return 0
    return sum(1 for r in resolution.deliverables
               if len(r.candidates) >= 2 and not (r.facts or r.aggregate or r.page))


def _within(s: _TurnState, spec: GoalSpec) -> bool:
    """Every entity the declaration names is one the readings were shown."""
    index = s.index
    return index is None or all(index.entity(d.entity) is not None
                                for d in spec.deliverables if d.entity)


def _totals_agreed(first: GoalSpec, second: GoalSpec) -> tuple[GoalSpec, GoalSpec]:
    """A sum is the total over every record that fits only when both readings say so:
    one reading alone takes "Acme's invoice amount this month" for a total about as
    often as the other takes it for one invoice's amount, and the owner's rule for that
    is to ask which. The flag then never makes the readings differ."""
    if len(first.deliverables) != len(second.deliverables):
        return first, second

    def settled(spec: GoalSpec, agreed: list[bool]) -> GoalSpec:
        return replace(spec, deliverables=tuple(
            replace(d, metric=replace(d.metric, total=total)) if d.metric is not None else d
            for d, total in zip(spec.deliverables, agreed)))

    agreed = [bool(a.metric and a.metric.total and b.metric and b.metric.total)
              for a, b in zip(first.deliverables, second.deliverables)]
    return settled(first, agreed), settled(second, agreed)


def _named_controls(spec: GoalSpec, parts: list, text: str, index, declared: frozenset = frozenset()
                    ) -> tuple[GoalSpec, tuple[str, ...]]:
    """A control the request names in the application's own words keeps its place in the
    declaration. The first step's terms say what each part is about as the application
    writes it; when such a term is a control of the declared entity (a header button) or
    the create control of the declared list page, and the request itself writes that
    word, a page declared without an action is the handoff to that control. A second or
    deciding reading now and then drops the control the first reading carried ("point
    out S00020's Preview" came back as "open S00020"); the request's own words settle
    it, without another reading. Returns (spec, the controls named)."""
    if not parts or not text or index is None:
        return spec, ()
    out, named = [], []
    for position, d in enumerate(spec.deliverables):
        control = ""
        if d.kind == "page" and not d.action:
            entity = index.entity(d.entity) if d.entity else None
            page = index.page(d.page.key) if d.page.key else None
            for term in _terms_for(parts, position, d, len(spec.deliverables)):
                if not (_names_verbatim(text, term) or _norm(term) in declared) \
                        or _names_a_page(index, term):
                    # A control the request writes in the application's words, or one a
                    # reading declared on this record ("确认按钮" read as Confirm): with the
                    # first step naming it too, that is two readings of the same thing.
                    continue
                if d.record and entity is not None:
                    control = next((a for a in entity.actions if _norm(a) == _norm(term)), "")
                elif (not d.record and page is not None and "create" in page.operations
                      and _norm(term) in ("new", "create")):
                    control = "New"
                if control:
                    break
        if control:
            d = replace(d, kind="handoff", action=control)
            named.append(control)
        out.append(d)
    if not named:
        return spec, ()
    return replace(spec, deliverables=tuple(out)), tuple(named)


def _shown_when(index, entity_key: str, control: str) -> str:
    """The source rule of when a control of an entity shows, with its citation; empty
    when the index holds none."""
    entity = index.entity(entity_key) if index is not None and entity_key else None
    if entity is None or not control:
        return ""
    rule = next((r for r in entity.rules if r.kind == "visibility"
                 and _norm(r.about) == _norm(control)), None)
    if rule is None:
        return ""
    return rule.text + (f" [{rule.citation}]" if rule.citation else "")


def _control_on_a_record(spec: GoalSpec, text: str, index, references
                         ) -> tuple[GoalSpec, tuple[str, ...]]:
    """A control's rules declared for a record the request names by its reference
    ("其实 S00005 还能做，Confirm 按钮在哪？"): the person means that record's control, and
    it is pointed at. Rules about a control in general ("Confirm 什么时候出现？") name no
    record, and stay a question about the rules. Returns (spec, the controls pointed at)."""
    if index is None or not references:
        return spec, ()
    located = {(k.entity, _norm(n)) for k in references for n in (k.text, k.name)}
    out, pointed = [], []
    for d in spec.deliverables:
        entity = index.entity(d.entity) if d.entity else None
        if (d.kind in ("fact", "list", "aggregate") and d.rules and d.record
                and entity is not None and (entity.key, _norm(d.record)) in located):
            control = next((a for a in entity.actions if _names_verbatim(text, a)), "")
            if control:
                d = replace(d, kind="handoff", action=control, fields=(), rules=(), metric=None)
                pointed.append(control)
        out.append(d)
    if not pointed:
        return spec, ()
    return replace(spec, deliverables=tuple(out)), tuple(pointed)


def _terms_for(parts: list, position: int, d, count: int) -> tuple[str, ...]:
    """The first step's terms for one deliverable: the part at the same position when
    parts and deliverables pair up, else the parts about the same entity."""
    if len(parts) == count:
        return tuple(parts[position].get("terms") or ())
    return tuple(term for part in parts
                 if _norm(part.get("entity") or "") == _norm(d.entity)
                 for term in (part.get("terms") or ()))


def _names_a_page(index, term: str) -> bool:
    """A word that is also the name of a page ("credit note" beside Credit Notes) names
    the kind of record the request is about, not the control of the same name."""
    wanted = _norm(term).rstrip("s")
    return any(_norm(page.label).rstrip("s") == wanted for page in index.pages)


def _names_verbatim(text: str, name: str) -> bool:
    """Whether the request writes this name as the application does: the same letters,
    whole (a "Send" inside "Sender" is not it), in either case."""
    name = (name or "").strip()
    if len(name) < 2:
        return False
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])",
                     text, re.I) is not None


def _first_line(name: str) -> str:
    """A control's name as the reply quotes it: a label's first line; a help marker on a
    later line is not part of it."""
    return ((name or "").strip().splitlines() or [""])[0].strip()


def _areas(index) -> list[str]:
    """The application's top-level areas as its menus name them, in their order."""
    if index is None:
        return []
    return list(dict.fromkeys(
        p.breadcrumb.split(" / ")[0].strip() for p in index.pages
        if p.breadcrumb and " / " in p.breadcrumb))


def assistant_answer(capabilities: frozenset[str], *, chinese: bool, areas=(),
                     repository_answers: bool = False) -> str:
    """What this assistant can do, asked of the assistant itself ("你能帮我做什么？").

    Written here rather than by the model, and from what the adapter declares: the
    required reads are always there, and a line for guiding a change, entering a value
    or explaining a rule appears only when the port offers it. The question used to be
    declared unsupported, and the reply was a refusal card saying the assistant could not
    do the request, with the model's own note about the question in brackets.
    """
    if chinese:
        opening = ([f"这个应用里你能用到的模块有：{'、'.join(areas)}（共 {len(areas)} 个）。", ""]
                   if areas else [])
        lines = opening + ["我可以在这个应用里帮你：",
                 "- 查数据：某条记录的字段（比如一张单据的客户、日期、状态），"
                 "符合条件的记录有多少条、金额合计多少，列出符合描述的记录；",
                 "- 找页面：打开你要看的列表或记录，告诉你某个功能在哪里；"]
        if "source" in capabilities:
            lines.append("- 解释规则：某个按钮为什么不显示、某个字段什么时候能改；")
        if repository_answers:
            lines.append("- 查源码知识：检索部署的代码与文档，解释规则或已记录的工程职责，并给出出处；")
        if "handoffs" in capabilities:
            lines.append("- 引导操作：要新建、修改、确认、发送或付款时，打开对应页面并标出要点的按钮"
                         + ("，你给了值的话先帮你填好" if "fill" in capabilities else "")
                         + "，由你核对后自己完成；")
        lines[-1] = lines[-1].removesuffix("；") + "。"
        return "\n".join(lines + ["", "我不会替你修改数据。直接用一句话说你要什么就行，"
                                        "可以一次问几件事。"])
    opening = ([f"The areas of this application open to you: {', '.join(areas)} "
                f"({len(areas)} in all).", ""] if areas else [])
    lines = opening + ["In this application I can:",
             "- Look up data: a field of a record (the customer, date or status of a "
             "document), how many records meet a condition or what they add up to, and "
             "which records fit a description;",
             "- Find pages: open the list or record you want to see, or say where a "
             "feature is;"]
    if "source" in capabilities:
        lines.append("- Explain rules: why a button is hidden, or when a field can be changed;")
    if repository_answers:
        lines.append("- Search deployed code and documentation for rules or documented engineering ownership, with citations;")
    if "handoffs" in capabilities:
        lines.append("- Guide a change: to create, change, confirm, send or pay something, "
                     "open the page and point at the control"
                     + (", filling in a value you gave" if "fill" in capabilities else "")
                     + ", for you to check and complete;")
    lines[-1] = lines[-1].removesuffix(";") + "."
    return "\n".join(lines + ["", "I do not change data for you. Say what you want in one "
                                    "sentence; you can ask several things at once."])


class _Unmet(NamedTuple):
    """One part of a page request the page did not show, by kind and the name asked."""
    kind: str
    name: str = ""

    def __str__(self) -> str:  # the audit's wording
        return {"not_attempted": "not attempted",
                "not_confirmed": "working the page not confirmed",
                "not_opened": "page did not open",
                "refused": "the browser did not run the command",
                "page_unverified": "rendered destination not verified",
                "error_dialog": "the application reported an error dialog",
                "filter_on": f"filter {self.name}", "filter_off": f"filter {self.name} off",
                "paging": f"paging {self.name}",
                "section": f"section {self.name}", "control": f"control {self.name}",
                "action_unspecified": "no handoff control was specified",
                "record": f"no detail page for record {self.name}",
                "view": f"{self.name} view"}[self.kind]

    def said(self, zh: bool) -> str:
        """As the person is told it."""
        if zh:
            return {"not_attempted": "没有执行", "not_confirmed": "没有得到操作页面的同意",
                    "not_opened": "页面没有打开", "error_dialog": "应用弹出了错误提示",
                    "refused": "助手暂时无法操作页面",
                    "page_unverified": "无法确认目标页面已经加载",
                    "filter_on": f"筛选「{self.name}」没有生效",
                    "filter_off": f"筛选「{self.name}」没有取消",
                    "paging": f"没有{self.name}",
                    "section": f"没有找到页签「{self.name}」",
                    "control": f"没有找到控件「{self.name}」",
                    "action_unspecified": "没有确定要准备的操作或字段",
                    "record": f"没有取得「{self.name}」的详情地址",
                    "view": f"没有切换到 {self.name} 视图"}[self.kind]
        return {"not_attempted": "not attempted", "not_confirmed": "working the page was not agreed",
                "not_opened": "the page did not open",
                "refused": "the assistant cannot work the page right now",
                "page_unverified": "the rendered destination could not be verified",
                "error_dialog": "the application showed an error",
                "filter_on": f"filter “{self.name}” not applied",
                "filter_off": f"filter “{self.name}” not removed",
                "paging": f"did not {self.name}",
                "section": f"no tab “{self.name}”", "control": f"no control “{self.name}”",
                "action_unspecified": "no operation or field to prepare was determined",
                "record": f"no detail address for “{self.name}”",
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
    #: When the control to point at is not on the page: the deployment's source rule of
    #: when it shows, with its citation, read from the index (knowledge, not an action).
    rule: str = ""
    form_requested: bool = False
    form_complete: bool = False
    form_fields: tuple[str, ...] = ()
    form_submit: str = ""
    form_input_dispatched: bool = False
    form_reason: str = ""
    form_error_code: str = ""
    #: Requested fields the open form showed no field for, by the names asked.
    form_unfilled: tuple[str, ...] = ()
    #: (field label, option asked) for choices whose own list did not offer the option.
    form_unoffered: tuple[tuple[str, str], ...] = ()
    #: The record whose details the page loop brought on screen, when it has no address.
    record_shown: str = ""
    #: "page/size" as the page's own list read reported it once paging was settled.
    paging: str = ""


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
    receipt: TurnReceipt = field(default_factory=TurnReceipt)
    budget: TurnBudget | None = None
    model_stop: threading.Event = field(default_factory=threading.Event)
    model_futures: list = field(default_factory=list)
    step: int = 0
    page_actions: int = 0
    #: The browser did not run the last command it was sent: what it needs to work the
    #: page at all was not there. Nothing was done, and waiting on the page shows nothing.
    refused: bool = False
    terminal_reason: str = ""
    detail: str = ""
    chinese: bool = False
    write_intent: bool = False
    mutation_outcomes: list = field(default_factory=list)
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
    #: What the first step said each part is about, in the application's words; a
    #: control it names that the request also writes stays in the declaration.
    scope_parts: list = field(default_factory=list)
    #: The deployment's conventions the request's own words name (never an earlier
    #: turn's), passed with every resolution.
    conventions: tuple = ()
    #: The name the person chose in answer to the pending question, if this turn is
    #: that answer.
    chosen: tuple = ()
    #: The application's current date, for the days a request writes.
    today: str = ""
    #: The entities and words the neighbourhood was built from (staged path only).
    focus: tuple = ()
    #: Exact actor-index page selections survive entity narrowing and repair. A
    #: configuration page may have no record entity at all.
    page_keys: tuple[str, ...] = ()
    source_requests: tuple = ()
    source_answer: str = ""
    source_complete: bool = True
    unsupported: str = ""
    #: Quantities the request wrote that the declaration acted on carries nowhere, as
    #: written. What was done may be right, but it is not all that was asked.
    dropped: tuple[str, ...] = ()
    #: The turn's measured chat, for the one question a page step may ask of the model.
    chat: object = None
    #: Admission is reversible only before approval and is consulted once a turn.
    skill_consulted: bool = False
    skill_limit: str = ""



class GoalTurnExecutor:
    """Execute one turn as a declared goal, without owning the channel lifecycle."""

    def __init__(self, *, channel, config, chat, application: BoundApplication,
                 goal_port: GoalPort, control, run_id_factory,
                 ledger_factory=SessionLedger,
                 conversation_state: ConversationState | None = None,
                 readings: int = 2, staged: bool = True, values: bool = True,
                 declaration_cache: bool = True,
                 disabled_page_actions: frozenset[str] = frozenset(),
                 repository_answers: bool = False,
                 page_reach_steps: int = 0, page_reach_policy=None, turn_events=None,
                 complete_request_check: str = "single"):
        #: The staged path (the default): the request's hard references are located by
        #: the adapter, the model first picks entities and kinds from one-line summaries,
        #: then declares the details against those entities' neighbourhood of the index,
        #: where the rules of the deployment's source are. Off, the model declares once
        #: against the whole index, as a comparison or for a scripted apparatus.
        self.declaration_cache = DeclarationCache() if declaration_cache else None
        self.staged = bool(staged)
        # Goal actions are fixed read-only procedures under application write control.
        # A host may further restrict those procedures; the model-tool disabled set
        # historically belongs to the generic loop, not these procedures.
        self.disabled_page_actions = frozenset(disabled_page_actions)
        #: How many steps the bounded page loop (`goal.reach`) may take to bring the page
        #: to a state that has no address and no control the adapter can name; 0, the
        #: default, never runs it. Its clicks are admitted by a policy of their own, since
        #: what a model may press is a different question from what a fixed step may.
        if isinstance(page_reach_steps, bool) or not isinstance(page_reach_steps, int) \
                or not 0 <= page_reach_steps <= 20:
            raise ValueError("page_reach_steps must be an integer from 0 to 20")
        if page_reach_policy is not None and not isinstance(page_reach_policy, ClickPolicy):
            raise TypeError("page_reach_policy must implement ClickPolicy")
        self.page_reach_steps, self.page_reach_policy = page_reach_steps, page_reach_policy
        if not isinstance(repository_answers, bool):
            raise TypeError("repository_answers must be boolean")
        self.repository_answers = repository_answers and config.source.runtime_source
        if self.repository_answers and not self.staged:
            raise ValueError("repository answers require the staged goal declaration")
        #: Candidate values offered before the details are declared, when the port has
        #: them; off only to measure what they change.
        self.values = bool(values)
        if config.allow_business_mutations:
            from ..business.mutation import MutationJournalDurability
            if (application.ports.operations is None or not config.require_identity
                    or config.deployment_profile == "production" and
                    application.business_tools.mutation_journal_durability is not MutationJournalDurability.DURABLE):
                raise ValueError("goal writes require registered operations, identity and a durable journal")
        #: How many independent readings of the request are declared before acting.
        #: With two, differing resolved answers get two additional independent readings
        #: and a vote; an unresolved vote falls back to a reconciliation declaration.
        #: One reading trusts the first declaration as it is.
        self.readings = max(1, int(readings))
        if complete_request_check not in ("single", "anchored"):
            raise ValueError("complete_request_check must be 'single' or 'anchored'")
        self.complete_request_check = complete_request_check
        self.channel = channel
        self.turn_events = turn_events
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
        #: The person's leave to work the page, answered once for the conversation: a
        #: later turn goes ahead on it, a new conversation asks again. Writes are not
        #: covered by it; each still asks on its own.
        self.page_leave = False

    # -- binding ----------------------------------------------------------------------
    @observed_turn
    def execute(self, message: dict) -> AgentTurnExecution:
        turn_message = AgentTurnMessage.from_wire(message)
        dropped = (self.conversation_state.clear()
                   if self.channel.take_new_conversation() else -1)
        if dropped >= 0:
            self.page_leave = False
        if dropped >= 0 and self.declaration_cache is not None:
            self.declaration_cache.clear()
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
            """One card for the conversation: may it work the page."""
            chinese = bool(_CJK.search(turn_message.text))
            effect = ("在页面上帮你完成这件事" if chinese
                      else "Work the page to do this for you")
            targets = (("会打开页面、点击、填写和选择；这次对话里不再询问",) if chinese
                       else ("Opens pages, clicks, fills and chooses; not asked again in this conversation",))
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

        def approve_business(proposal, *, context=None):
            try:
                return gate.decide_plan(proposal, effect=proposal.effect, targets=proposal.targets,
                                        ttl_s=self.config.approval_ttl_s, context=context)
            except RemoteProtocolError as exc:
                if exc.code != "approval_expired":
                    raise
                return ApprovalGrant(approval_id="", decision="expired",
                    plan_digest=proposal.content_digest, decided_at=time.time(),
                    operation_context_digest=context.binding_digest if context else "")

        approvals = ApprovalWatch(
            ledger=ledger, run_id=run_id, principal=principal,
            session_id=(self.channel.session_id if principal is not None else ""),
            turn_id=(turn_message.message_id if principal is not None else ""),
            business_approval=(approve_business if self.config.allow_business_mutations else None),
            operation_confirmation=(confirm_operation if self.config.confirm_page_operations
                                    else None),
            cancelled=cancelled,
            approval_hold_s=self.config.approval_ttl_s,
            operation_hold_s=self.config.operation_approval_ttl_s)
        approvals.operation_granted = self.page_leave
        if self.page_leave and approvals.operation_confirmation is not None:
            ledger.append("operation_leave_carried", message_id=turn_message.message_id)
        writes = WriteWatch(control=self.control, ledger=ledger, run_id=run_id,
                            write_guard_declared=bool(self.application.descriptor.page_write_guard))
        continued = self._continued(turn_message)
        chosen: tuple = ()
        if continued is not None:
            ledger.append("goal_clarification_answered", message_id=turn_message.message_id)
            text, name = continued
            turn_message, chosen = replace(turn_message, text=text), (name,)
        state = _TurnState(message=turn_message, run_id=run_id, ledger=ledger,
                           browser=browser, writes=writes, approvals=approvals,
                           obs=current.observation, started_at=started_at,
                           chinese=bool(_CJK.search(turn_message.text)),
                           cancelled=cancelled, chosen=chosen)
        state.receipt.started -= max(0.0, time.time() - started_at)
        state.budget = TurnBudget(steps=self.config.step_budget, seconds=self.config.wall_budget_s,
                                  cancelled=cancelled,
                                  clock=lambda: time.monotonic() - approvals.waited_ms / 1000)
        state.budget.started -= max(0.0, time.time() - started_at)
        self.channel.publish("progress", {"state": "agent_started"},
                             turn_id=turn_message.message_id)
        try:
            text = self._run(state)
            if state.skill_limit:
                text = self._say(state, state.skill_limit) + ("\n\n" + text if text else "")
        except TurnCancelled:
            state.terminal_reason = "user_cancelled"
            text = "操作已停止。" if state.chinese else "The task was stopped."
        except BudgetExhausted as exc:
            state.terminal_reason = exc.kind if exc.kind in {
                "token_budget", "wall_budget", "step_budget"} else "batch_cost_stop"
            state.detail = str(exc)[:200]
            text = "本次任务已达到模型使用限额，未继续执行。" if state.chinese else "The model budget was reached; the task did not continue."
        except AgentTurnError:
            raise
        except Exception as exc:
            ledger.append("turn_execution_failed", error_type=type(exc).__name__)
            writes.end_control()
            raise AgentTurnError(AgentTurnErrorCode.EXECUTION_FAILED,
                                 "the goal turn failed before it produced a terminal result",
                                 retryable=False, run_id=run_id) from exc
        finally:
            # Leave answered in this turn carries to the next; leave withdrawn here (the
            # task read anew) is asked again.
            self.page_leave = approvals.operation_granted
            # Unused parallel readings are cancelled and joined before the terminal
            # receipt, so a failed first reading cannot leave a paid task running.
            state.model_stop.set()
            for future in state.model_futures:
                try:
                    future.result(timeout=max(1.0, state.budget.remaining()))
                except Exception:
                    # The observed failure is already accounted by MeasuredChat.
                    pass
        return self._settle(state, current, text)

    def _complete_sources(self, s: _TurnState, chat, *, current_facts=()) -> None:
        from ..privacy import SessionHandoffPolicy
        from .source_answers import answer, page_context, retrieve
        try:
            with s.receipt.stage("source"):
                retrieved, evidence = retrieve(self.config.source, s.source_requests, s.ledger)
            self._dispatch(s, "observe", {})
            if s.terminal_reason:
                return
            # Bounds match the goal's four slots and twelve fields; redact again at
            # this new model boundary rather than forwarding unrestricted row data.
            handoffs = SessionHandoffPolicy.extending(
                self.application.ports.routes.session_handoff_query_keys)
            safe_facts = tuple({k: redact_sensitive_assignments(handoffs.redact(str(v)))[:400]
                                for k, v in f.items()} for f in current_facts[:48])
            with s.receipt.stage("source"):
                result = answer(chat, source=self.config.source, requests=s.source_requests,
                    retrieved=retrieved, evidence=evidence, run_id=s.run_id,
                    cancelled=s.cancelled, chinese=s.chinese, ledger=s.ledger,
                    prompt=s.message.text, current_facts=safe_facts,
                    conversation=self.conversation_state.model_context(), handoffs=handoffs,
                    page=lambda: page_context(s.obs, handoffs=handoffs),
                    refresh=lambda: bool(self._dispatch(s, "observe", {}) is not None
                                         and not s.terminal_reason),
                    timeout_s=max(1.0, self.config.wall_budget_s - (time.time() - s.started_at)))
            s.source_answer, s.source_complete = result.text, result.complete
        except ProviderError as exc:
            s.terminal_reason = "provider_error"
            s.source_complete = False
            s.source_answer = _provider_failed(exc, s.chinese)
        except (ValueError, ConclusionRefused) as exc:
            s.source_complete = False
            s.source_answer = ("源码回答未通过证据核验。" if s.chinese
                               else "The source answer did not pass evidence verification.")
            s.ledger.append("goal_source_answer_refused", error_type=type(exc).__name__,
                            detail=str(exc)[:200])

    # -- the turn ---------------------------------------------------------------------
    def _run(self, s: _TurnState) -> str:
        from ..privacy import SessionHandoffPolicy
        from .context import page_context

        handoffs = SessionHandoffPolicy.extending(
            self.application.ports.routes.session_handoff_query_keys)

        def current_page():
            # Model reference hints come only from the current server observation.
            # The resolver and dispatch still establish actor scope and authority.
            return page_context(s.obs, handoffs=handoffs)

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
        chat = MeasuredChat(AuditedChat(self.chat, s.ledger), s.receipt,
                            remaining_s=s.budget.remaining, budget=s.budget,
                            stopped=s.model_stop.is_set)
        s.chat = chat
        s.budget.check()
        with s.receipt.stage("business"):
            index = self.goal_port.index(context=s.approvals.context(0, "index"))
        s.index = s.full_index = index
        s.conventions = conventions_named(index.conventions, s.message.text)
        s.ledger.append("goal_index_ready", entities=len(index.entities),
                        pages=len(index.pages), source_digest=index.source_digest,
                        complete=index.complete)
        s.step += 1
        elapsed = time.time() - s.started_at
        today = getattr(self.goal_port, "today", None)
        today_text = str(today() or "") if callable(today) else ""
        s.today = today_text
        principal = s.approvals.principal
        cache_owner = (principal.binding_digest if principal is not None else "anonymous",
                       id(self.chat), getattr(self.chat, "temperature", None),
                       index.source_digest, self.config.source.content_digest)
        scope_text = ""
        scope_parts: list[dict] = []
        scope_unsupported = ""
        known = ()
        skill_knowledge = ()
        # A follow-up is read with what was said before it, and the records it may mean
        # are looked for in the earlier request as well as in its own words.
        earlier, before = self._earlier()
        lookup = (before + " " + s.message.text).strip()
        parts = None
        operations = self.application.ports.operations
        # Knowledge routing shares the original scope call. Ordinary declarations
        # keep their existing wire even when this deployment enables writes.
        skill_candidates = tuple(skill.candidate_card() for skill in operations.skills.artifacts
            if any(d.effect_contract is not None and d.name in skill.write_operations
                   for d in operations.tools.definitions)
            ) if self.config.allow_business_mutations and operations else ()
        if self.staged:
            with s.receipt.stage("business"):
                known = locate(self.goal_port, lookup,
                               context=s.approvals.context(s.step, "locate"))
            s.references = tuple(known)
            s.ledger.append("goal_references", step=s.step,
                            references=[{"text": k.text, "entity": k.entity, "name": k.name}
                                        for k in known][:8])
            try:
                with s.receipt.stage("scope"):
                    (parts, write_intent, unsupported, about_assistant,
                     projection) = understand_scope(
                        chat, prompt=s.message.text,
                        summaries=summaries(index, lookup, known), run_id=s.run_id,
                        timeout_s=max(1.0, self.config.wall_budget_s - elapsed),
                        cancelled=s.cancelled, today=today_text, earlier=earlier,
                        cache=self.declaration_cache, cache_namespace=(cache_owner, "scope"),
                        repository_source=self.repository_answers,
                        skill_candidates=skill_candidates,
                        current_page=current_page())
            except ValueError as exc:
                # A legacy full-index declaration can recover business scope, but
                # cannot carry repository questions or their live-read dependencies.
                s.ledger.append("goal_declaration_invalid", step=s.step, detail=str(exc)[:200])
                s.ledger.append("goal_scope_unread", step=s.step)
                if skill_candidates:
                    from .skill_admission import consult_execution
                    answer = consult_execution(self, s)
                    if answer is not None:
                        return answer
                if self.repository_answers:
                    s.terminal_reason = "agent_refused"
                    return self._say(s, _cannot(s.message.text,
                        "未能完整识别本次查询与源码问答的范围，请补充要查询的记录或字段。" if s.chinese else
                        "The data and repository scope could not be declared completely; clarify the requested record or fields.",
                        s.chinese))
                parts = None
            except ProviderError as exc:
                s.ledger.append("provider_failed", step=s.step,
                                provider_error_code=getattr(exc, "code", "provider_error"))
                s.terminal_reason, s.detail = "provider_error", str(exc)[:200]
                return self._say(s, _provider_failed(exc, s.chinese))
        if self.staged and parts is not None and projection.get("selected_skill_ids"):
            from .skill_execution import execute_skill_plan, load_skill_context
            selected = projection["selected_skill_ids"]
            s.scope_parts = list(parts)
            s.source_requests = tuple(projection.get("source_requests", ()))
            s.unsupported = unsupported
            s.ledger.append("goal_skill_selected", step=s.step, skill_ids=list(selected),
                            source_parts=len(s.source_requests), side_effect_possible=False)
            context = load_skill_context(self, s, selected)
            return execute_skill_plan(self, s, selected, context=context, native_proposals=True)
        if self.staged and parts is not None:
            from .source_scope import scope_page_keys
            scope_parts = parts
            scope_unsupported = unsupported
            s.scope_parts = list(parts)
            s.page_keys = scope_page_keys(parts)
            s.ledger.append("goal_scope_declared", step=s.step, parts=parts,
                            write_intent=write_intent, unsupported=unsupported,
                            about_assistant=about_assistant, **projection)
            sources = projection.get("source_requests", ())
            s.source_requests = tuple(sources)
            if (write_intent or any(p.get("kind") == "handoff" and not p.get("rules") for p in parts)
                    or (not parts and (sources or not about_assistant))):
                from .skill_admission import consult_execution
                answer = consult_execution(self, s)
                if answer is not None:
                    return answer
            operations = self.application.ports.operations
            if operations is not None:
                from .skill_execution import skills_for_scope
                projection["skill_ids"] = skills_for_scope(operations, parts)
            if projection.get("skill_ids"):
                from .skill_execution import execute_skill_plan, load_skill_context
                context = load_skill_context(self, s, projection["skill_ids"])
                # Loading knowledge must not replace the existing query/page/source
                # interpreter, its independent readings or bounded repairs.
                # Preserve the enabled deployment's existing static excerpt path;
                # disabled writes always retain main's ordinary interpreter.
                static_skill_only = not sources and parts and all(
                    p.get("rules") and not p.get("names") for p in parts)
                legacy_change = not skill_candidates and (write_intent or (
                    any(skill.write_operations for skill in context.loaded)
                    and any(p.get("kind") == "handoff" and not p.get("rules") for p in parts)))
                if self.config.allow_business_mutations and (static_skill_only or legacy_change):
                    s.unsupported, s.write_intent = unsupported, write_intent
                    s.index = neighbourhood(s.full_index,
                        tuple(dict.fromkeys(p["entity"] for p in parts if p.get("entity"))),
                        request=s.message.text, page_keys=s.page_keys)
                    return execute_skill_plan(self, s, projection["skill_ids"], context=context)
                skill_knowledge = tuple(skill.loaded_card() for skill in context.loaded)
                s.ledger.append("goal_skill_knowledge_applied", step=s.step,
                    skills=[skill.skill_id for skill in context.loaded],
                    business_parts=len(parts), source_parts=len(sources), side_effect_possible=False)
            if sources and not parts:
                s.write_intent = write_intent
                self._complete_sources(s, chat)
                if not s.terminal_reason:
                    s.terminal_reason = "agent_finished" if s.source_complete and not write_intent else "agent_refused"
                return ""
            if not parts and about_assistant:
                return self._say(s, assistant_answer(capabilities_of(self.goal_port),
                    chinese=s.chinese, areas=_areas(s.full_index),
                    repository_answers=self.repository_answers))
            if not parts:
                s.terminal_reason = "agent_refused"
                return self._say(s, _cannot(s.message.text, unsupported, s.chinese))
            # Names the first step copied out of the request (a customer written in the
            # person's language, which no reference shape reveals) are looked for on the
            # entities it picked and those they link to; a record found joins the known ones.
            seen = {k.text for k in known} | {k.name for k in known}
            named = tuple(n for n in dict.fromkeys(n for p in parts for n in p.get("names", ()))
                          if n not in seen)
            if named:
                with s.receipt.stage("business"):
                    found = locate_names(self.goal_port, named,
                                         entities=tuple(dict.fromkeys(p["entity"] for p in parts if p.get("entity"))),
                                         context=s.approvals.context(s.step, "locate"))
                s.ledger.append("goal_names", step=s.step, names=list(named)[:8],
                                found=[{"text": k.text, "entity": k.entity, "name": k.name}
                                       for k in found][:8])
                known = tuple(known) + found
                s.references = tuple(known)
            # The rules of the source are shown only when the first step says a part
            # asks how the application behaves: a long catalogue of rule ids in front of
            # a question about data disturbs how a small model reads the question.
            # The words the first step wrote in the application's language rank the
            # rules alongside the request's own, which may be in another language.
            terms = " ".join(t for p in parts for t in p.get("terms", ()))
            # An entity a convention the request's words name is about ("成交" -> orders)
            # is in the neighbourhood whatever the first step picked.
            # So is the entity of a record the request names by its number (S00020), which
            # the first step may have passed over for an entity that record relates to.
            focus = (tuple(dict.fromkeys(
                [p["entity"] for p in parts if p.get("entity")] + [c.entity for c in s.conventions]
                + [k.entity for k in known if re.search(r"\d", k.text)])),
                (s.message.text + " " + terms).strip())
            index = neighbourhood(index, focus[0], request=focus[1],
                                  page_keys=s.page_keys,
                                  rules=any(p.get("rules") for p in parts))
            s.index, s.focus = index, focus
            s.ledger.append("goal_neighbourhood", step=s.step,
                            entities=[e.key for e in index.entities], pages=len(index.pages),
                            chars=len(index.render()))
            scope_text = json.dumps({"parts": parts, "write_intent": write_intent,
                                     "known_references": [
                                         {"text": k.text, "entity": k.entity, "name": k.name}
                                         for k in known]}, ensure_ascii=False, separators=(",", ":"))
            s.step += 1
            elapsed = time.time() - s.started_at
        candidates = ()
        if self.staged and self.values and "values" in capabilities_of(self.goal_port):
            try:
                with s.receipt.stage("business"):
                    candidates = ground(self.goal_port,
                                        entities=tuple(dict.fromkeys(p["entity"] for p in scope_parts if p.get("entity"))),
                                        known=known, dates=days(lookup, today_text),
                                        context=s.approvals.context(s.step, "candidates"))
            except Exception as exc:  # noqa: BLE001 -- grounding helps; its absence is not a failure
                s.ledger.append("goal_values_failed", step=s.step,
                                detail=f"{type(exc).__name__}: {exc}"[:200])
            s.ledger.append("goal_values", step=s.step, count=len(candidates),
                            kinds=sorted({c.kind for c in candidates}))
            s.candidates = candidates
        unbound = functools.partial(
            understand, chat, prompt=(s.message.text + (
                "\n\nStatic questions are answered separately; declare EVERY business data/page part only."
                if s.source_requests else "")), index=index, run_id=s.run_id,
            timeout_s=max(1.0, self.config.wall_budget_s - elapsed), cancelled=s.cancelled,
            today=today_text, scope=scope_text, values=render_values(candidates),
            earlier=earlier, form_values=(not scope_parts or write_intent
                or any(p.get("kind") == "handoff" for p in scope_parts)),
            skill_knowledge=skill_knowledge)

        def read(*, cache_slot=None, stage=None, **kwargs):
            # Every reading -- the first, the second, the deciding one, the repair --
            # sees the same scope and values, and has its value ids bound to the exact
            # names before it is compared or resolved.
            name = stage or ("repair" if kwargs.get("previous") is not None else "interpret")
            with s.receipt.stage(name):
                spec, projection = unbound(
                    cache=self.declaration_cache if cache_slot is not None else None,
                    cache_namespace=(cache_owner, cache_slot),
                    current_page=current_page(), **kwargs)
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
            second_reading = pool.submit(copy_context().run, read, cache_slot="second")
            s.model_futures.append(second_reading)
            pool.shutdown(wait=False)
        try:
            spec, projection = read(cache_slot="first")
        except ValueError as exc:
            s.ledger.append("goal_declaration_invalid", step=s.step, detail=str(exc)[:200])
            from .skill_admission import consult_execution
            answer = consult_execution(self, s)
            if answer is not None:
                return answer
            s.terminal_reason = "agent_refused"
            return self._say(s, "没能理解这个请求要做什么，请换个说法。" if s.chinese
                             else "I could not work out what this request asks for.")
        except ProviderError as exc:
            s.ledger.append("provider_failed", step=s.step,
                            provider_error_code=getattr(exc, "code", "provider_error"))
            s.terminal_reason, s.detail = "provider_error", str(exc)[:200]
            return self._say(s, _provider_failed(exc, s.chinese))
        s.ledger.append("goal_declared", step=s.step, spec=spec.to_json(), **projection)
        asks_rules = any(p.get("rules") for p in scope_parts)
        if (not spec.deliverables and spec.unsupported and not spec.about_assistant
                and second_reading is not None and not asks_rules):
            # The first reading says the application does not hold what is asked. The
            # second, read independently, settles it: saying so too, or answering from
            # the same entities the first looked at, the refusal stands (an answer there
            # is a neighbouring field). Only a second reading that reaches entities the
            # first did not see replaces it.
            spec = self._second_on_existence(s, spec, second_reading)
            second_reading = None
        elif (self.staged and not spec.deliverables and scope_parts
                and not (spec.unsupported and scope_unsupported)
                and not spec.about_assistant and not any(p.get("rules") for p in scope_parts)):
            # The first step found things to deliver in this request and the second
            # declared none without saying why: a small model now and then answers the
            # index instead of the request. It is asked once more with what the first
            # step found; its answer stands either way. When both steps said what the
            # application does not hold, the second is not asked again: asked again, it
            # maps the request to a neighbouring field (a payment reference for a bank's
            # batch number), a wrong answer where the refusal was the right one.
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
        spec, named = _named_controls(spec, s.scope_parts, s.message.text, index)
        if named:
            s.ledger.append("goal_control_named", step=s.step, controls=list(named))
        spec, pointed = _control_on_a_record(spec, s.message.text, s.full_index, s.references)
        if pointed:
            s.ledger.append("goal_rule_became_handoff", step=s.step, controls=list(pointed))
        spec, flipped = _magnitudes(spec, s.message.text)
        if flipped:
            s.ledger.append("goal_amounts_as_magnitudes", step=s.step, fields=list(flipped))
        spec, handed = _as_handoffs(spec, s.message.text)
        spec, reread = _as_said(spec, s.message.text, s.full_index or s.index)
        if reread:
            s.ledger.append("goal_declaration_as_said", step=s.step, changes=list(reread))
        if handed:
            s.ledger.append("goal_page_became_handoff", step=s.step, controls=list(handed))
        spec, unscoped = _entity_over_page(spec, s.message.text, s.full_index)
        if unscoped:
            s.ledger.append("goal_page_scope_dropped", step=s.step, pages=list(unscoped))
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
            gaps = _coverage_gaps(spec, scope_parts, s.message.text, today_text, s.candidates,
                                  named=_hands_over_a_named_control(spec, scope_parts, s.message.text),
                                  index=s.index)
            if gaps:
                s.ledger.append("goal_coverage_gaps", step=s.step, gaps=list(gaps))
                s.step += 1
                try:
                    again, projection = s.read(previous=spec, problems=gaps)
                    s.ledger.append("goal_declared", step=s.step, spec=again.to_json(),
                                    attempt="coverage", **projection)
                    remaining = _coverage_gaps(again, scope_parts, s.message.text, today_text,
                                               s.candidates, index=s.index) if again.deliverables else gaps
                    if again.deliverables and len(remaining) < len(gaps):
                        spec = again
                except (ValueError, ProviderError) as exc:
                    s.ledger.append("goal_coverage_repair_failed", step=s.step,
                                    detail=str(exc)[:200])
            s.dropped = _dropped_quantities(spec, s.message.text, s.candidates)
            if s.dropped:
                s.ledger.append("goal_quantities_dropped", step=s.step, count=len(s.dropped))
        s.write_intent = spec.write_intent or any(
            d.kind == "handoff" for d in spec.deliverables)
        if (not spec.deliverables and not spec.about_assistant and s.focus
                and not any(p.get("rules") for p in scope_parts)):
            spec = self._held_reading(s, spec)
        if s.write_intent or (not spec.deliverables and not spec.about_assistant):
            from .skill_admission import consult_execution
            answer = consult_execution(self, s,
                entities=tuple(d.entity for d in spec.deliverables if d.entity))
            if answer is not None:
                return answer
        if not spec.deliverables and spec.about_assistant:
            return self._say(s, assistant_answer(capabilities_of(self.goal_port),
                chinese=s.chinese, areas=_areas(s.full_index),
                repository_answers=self.repository_answers))
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
            return self._say(s, _cannot(s.message.text, why, s.chinese))
        return self._execute_declared_goal(s, spec)

    def _execute_declared_goal(self, s: _TurnState, spec: GoalSpec) -> str:
        """Resolve a declaration in this turn, retaining the existing page/form contract."""
        s.step += 1
        try:
            resolution = self._resolve(s, spec)
        except _ContractBroken:
            s.terminal_reason = "agent_refused"
            return self._say(s, "应用未能完整解析这次请求。" if s.chinese
                             else "The application could not resolve this request completely.")
        resolution = self._open_form_meant(s, spec, resolution)
        for _round in range(_REPAIRS):
            if s.read is None:
                break
            problems = _repairable(spec, resolution, observed_empty_entities=frozenset(
                q["entity"] for q in s.source_requests if q.get("current") and q.get("entity")))
            if not problems:
                break
            # A repair: the resolver found something a different declaration would fix
            # and says what; the model declares once more with that in front of it. The
            # new declaration replaces the old only when it leaves less unresolved, so a
            # repair never makes the turn worse; one that helps may be followed by one
            # more (a name found on another entity, whose field is then empty).
            s.ledger.append("goal_repair_requested", step=s.step, problems=list(problems))
            s.step += 1
            elapsed = time.time() - s.started_at
            # A name found on another entity than the one declared, or a declaration a
            # repair already moved there: the repair is read against a neighbourhood
            # that has those entities too, so that the next round can keep them.
            beyond = tuple(dict.fromkeys(
                [k for d in resolution.deliverables for k in d.elsewhere]
                + [d.entity for d in spec.deliverables if d.entity]))
            seen = {e.key for e in s.index.entities} if s.index is not None else set()
            wider = ({"index": neighbourhood(s.full_index, tuple(dict.fromkeys(s.focus[0] + beyond)),
                                             request=s.focus[1], rules=False,
                                             page_keys=s.page_keys)}
                     if any(k not in seen for k in beyond) and s.focus and s.full_index is not None
                     else {})
            try:
                repaired, projection = s.read(previous=spec, problems=problems, **wider)
            except (ValueError, ProviderError) as exc:
                s.ledger.append("goal_repair_failed", step=s.step, detail=str(exc)[:200])
                repaired = None
            if repaired is not None and not repaired.deliverables:
                s.ledger.append("goal_repair_empty", step=s.step,
                                unsupported=(repaired.unsupported or "")[:200])
            if repaired is None or not repaired.deliverables:
                break
            s.ledger.append("goal_declared", step=s.step, spec=repaired.to_json(),
                            attempt=2 + _round, **projection)
            s.step += 1
            try:
                second = self._resolve(s, repaired)
            except _ContractBroken:
                second = None
            # A repair corrects names, fields and conditions; what the person asked
            # for -- a record to open, a list, a value -- stays what the readings said,
            # except a change asked for, which the repair may make a handoff.
            from .repair import field_values_preserved, lost_requirements
            kept = len(repaired.deliverables) == len(spec.deliverables) and all(
                after.kind in (before.kind, "handoff")
                and field_values_preserved(before, after, s.full_index or s.index, s.message.text)
                and (before.metric is None or before.metric.op not in ("min", "max")
                     or (after.kind in ("aggregate", "list") and after.metric is not None
                         and after.metric.op == before.metric.op))
                for before, after in zip(spec.deliverables, repaired.deliverables))
            losses = lost_requirements(spec, repaired)
            kept = kept and not losses
            # A kind of record the person named ("这个客户") is the one they mean: a repair
            # may not answer from another entity's record of the same name instead.
            kept = kept and not _moved_from_named_kind(s, spec, repaired)
            # A name nobody's record has is answered as not found. A repair that turns it
            # into conditions which find nothing either is not nearer an answer, and would
            # lose the name the person gave.
            kept = kept and not (second is not None and _still_nothing(resolution, second))
            accepted = second is not None and kept and (
                _unresolved(repaired, second) < _unresolved(spec, resolution)
                or (_unresolved(repaired, second) == _unresolved(spec, resolution)
                    and _askable(second) > _askable(resolution)))
            s.ledger.append("goal_repaired", step=s.step, accepted=accepted,
                            lost_requirements=list(losses),
                            unresolved_before=_unresolved(spec, resolution),
                            unresolved_after=(_unresolved(repaired, second)
                                              if second is not None else None))
            if not accepted:
                break
            if repaired.unsupported and any(
                    set(before.fields) - set(after.fields)
                    for before, after in zip(spec.deliverables, repaired.deliverables)):
                # An accepted field repair may remove unavailable requested fields.
                # Preserve that loss. Arbitrary prose in an otherwise fulfilled
                # first declaration is not a typed unmet outcome (a null value is
                # still a valid answer; source parts have separate settlement).
                s.unsupported = repaired.unsupported
            spec, resolution = repaired, second
        resolution = self._which_amount(s, spec, resolution)
        resolution = self._not_chosen_for(s, spec, resolution)
        resolution = self._told_apart(s, spec, resolution)
        if s.source_requests:
            # Mixed source questions may ask whether a rule permits this record now.
            # Read first, then answer with only these actor-bound facts. Complete all
            # reads before a form handoff releases the browser's observation window.
            facts = tuple({"subject": f.subject, "label": f.label, "value": f.value,
                           **({"field_key": f.field_key, "native_value": f.native_value}
                              if f.native_value else {})}
                          for r in resolution.deliverables for f in r.facts)
            self._complete_sources(s, s.chat, current_facts=facts)
        input_fields = tuple(dict.fromkeys(f for r in resolution.deliverables for f in r.input_fields))
        if input_fields:
            s.terminal_reason = "needs_clarification"
            question = _not_done(Resolved(kind="handoff", input_fields=input_fields), s.chinese)
            s.clarification = {"schema_version": 1, "clarification_id": f"{s.run_id}-values"[:100],
                "task_id": s.run_id[:100], "question": question[:500], "response_kind": "free_text", "choices": []}
            s.ledger.append("goal_form_values_asked", step=s.step, fields=len(input_fields))
        outcomes: list[_PageOutcome | None] = [None] * len(resolution.deliverables)
        if not s.terminal_reason and any(d.kind in ("page", "handoff") and r.page is not None
               for d, r in zip(spec.deliverables, resolution.deliverables, strict=True)):
            # The declarations took their time; the page has gone on rendering meanwhile
            # and the observation the turn holds may be stale. Look once before acting.
            self._dispatch(s, "observe", {})
        form_indices = [i for i, r in enumerate(resolution.deliverables)
                        if r.page is not None and r.page.form is not None]
        form_plan = None
        if form_indices:
            try:
                if any(spec.deliverables[i].kind != "handoff" for i in form_indices):
                    raise BusinessToolRequestError("only a requested handoff can fill a form")
                if any(resolution.deliverables[i].problem or resolution.deliverables[i].denied
                       or resolution.deliverables[i].unoffered for i in form_indices):
                    raise BusinessToolRequestError("an unresolved handoff cannot issue a form")
                form_plan = combine_forms([resolution.deliverables[i].page for i in form_indices])
            except BusinessToolRequestError as exc:
                s.ledger.append("goal_form_plan_refused", error_code=exc.code, dispatched=False)
                refused = _PageOutcome(reached=False, form_requested=True,
                                       form_error_code="form_plan_invalid")
                for i in form_indices:
                    outcomes[i] = refused
        for i, (declared, resolved) in enumerate(zip(spec.deliverables, resolution.deliverables, strict=True)):
            if resolved.page is None or declared.kind not in ("page", "handoff"):
                continue
            if i in form_indices:
                continue
            if s.terminal_reason:
                outcomes[i] = _PageOutcome(reached=False, unmet=[_Unmet("not_attempted")])
                continue
            if (resolved.kind == "handoff" and resolved.page.form is None
                    and not resolved.page.highlight):
                missing = (_Unmet("record", resolved.page.missing_record)
                           if resolved.page.missing_record else _Unmet("action_unspecified"))
                outcomes[i] = _PageOutcome(reached=False, unmet=[missing])
                s.ledger.append("goal_handoff_target_missing", step=s.step, dispatched=False)
                continue
            outcome = self._reach(s, resolved.page)
            if any(u.kind == "control" for u in outcome.unmet):
                outcome.rule = _shown_when(s.full_index, declared.entity, resolved.page.highlight)
                if outcome.rule:
                    s.ledger.append("goal_control_rule_cited", step=s.step,
                                    control=resolved.page.highlight)
            outcomes[i] = outcome
        # A handoff releases the browser's read window; no subsequent page work may
        # leave or replace that draft. Reads have already resolved, so mixed answers stay.
        if form_plan is not None:
            outcome = (self._reach(s, form_plan) if not s.terminal_reason else
                       _PageOutcome(reached=False, form_requested=True))
            for i in form_indices:
                outcomes[i] = outcome
        if not s.terminal_reason:
            achieved = not s.unsupported and not s.dropped and all(_achieved(r, o) for r, o in
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
                others = [(r, o, d) for r, o, d in zip(
                    resolution.deliverables, outcomes, spec.deliverables, strict=True)
                    if r is not unsure]
                delivered = self._reply(s, [r for r, _, _ in others],
                                        [o for _, o, _ in others], [d for _, _, d in others])
                return "\n\n".join(part for part in (delivered, s.clarification["question"]) if part)
        # Reaching a read-only page cannot settle a request to mutate the application.
        # A declared handoff has its own explicit "you must act" reply; a page/fact-only
        # declaration for a write must also state that the requested change did not happen.
        unfulfilled_write = spec.write_intent and not any(
            d.kind == "handoff" for d in spec.deliverables)
        if unfulfilled_write:
            s.ledger.append("goal_write_intent_unfulfilled", step=s.step)
            if s.terminal_reason == "agent_finished":
                s.terminal_reason = "agent_refused"
        reply = self._reply(s, resolution.deliverables, outcomes, spec.deliverables)
        if unfulfilled_write:
            reply += ("\n\n这里只是找到了相关信息或页面，尚未执行你请求的更改；请核对后自行操作。"
                      if s.chinese else
                      "\n\nI only found the relevant information or page. I did not make the requested change; please review it and act yourself.")
        return reply

    def _open_form_meant(self, s: _TurnState, spec: GoalSpec, resolution: Resolution) -> Resolution:
        """A change to something not saved yet: the name the request gives is no record of
        the application, and a form open on the page shows that very name. The request is
        then about that form, which is prepared where it stands. Nothing is looked up
        again and no entry is pressed; a name the open form does not show changes nothing."""
        from ..business.errors import BusinessToolError
        from ..business.forms import ApplicationOpenFormPort
        from .ports import FormPlan
        port = self.application.ports.forms
        if not self.config.allow_form_prefill or not isinstance(port, ApplicationOpenFormPort):
            return resolution
        out, adopted = list(resolution.deliverables), False
        for i, (d, resolved) in enumerate(zip(spec.deliverables, out, strict=True)):
            requested = d.field_values
            if (d.kind != "handoff" or not resolved.missing or not requested or adopted
                    or not _open_form_shows(s.obs, resolved.missing)):
                continue
            try:
                with s.receipt.stage("business"):
                    reference, values = port.adopt_open(
                        s.obs, tuple(requested), context=s.approvals.context(s.step, "adopt_form"))
                plan = PagePlan(url="", label=s.obs.title, expected_path=_path_of(s.obs.url),
                                form=FormPlan(reference, tuple(values)))
            except (BusinessToolError, PrincipalError, ValueError) as exc:
                s.ledger.append("goal_open_form_not_adopted", step=s.step,
                                error_type=type(exc).__name__)
                continue
            out[i], adopted = Resolved(kind="handoff", page=plan), True
            s.ledger.append("goal_open_form_adopted", step=s.step, fields=len(values))
        return replace(resolution, deliverables=tuple(out)) if adopted else resolution

    def _held_reading(self, s: _TurnState, spec: GoalSpec) -> GoalSpec:
        """The readings say the application does not hold what is asked, while the
        request names fields it holds that no page shows this account (a product's
        weight), or what it asks is held by records that point at the chosen ones (a
        contract's payment plans): the request is read once more with those in front of
        the model. Its declaration stands only when it declares something."""
        held = neighbourhood(s.full_index, s.focus[0], request=s.focus[1], rules=False,
                             held=True, inbound=True, page_keys=s.page_keys)
        fields = [f"{e.label}.{f.label}" for e in held.entities for f in e.held]
        seen = {e.key for e in s.index.entities} if s.index is not None else set()
        joined = [e.key for e in held.entities if e.key not in seen]
        if not fields and not joined:
            return spec
        s.step += 1
        s.ledger.append("goal_held_fields_offered", step=s.step, fields=fields[:8],
                        entities=joined[:8])
        try:
            again, projection = s.read(index=held)
        except (ValueError, ProviderError) as exc:
            s.ledger.append("goal_declaration_invalid", step=s.step, detail=str(exc)[:200])
            return spec
        s.ledger.append("goal_declared", step=s.step, spec=again.to_json(), attempt="held",
                        **projection)
        if not again.deliverables:
            return spec
        try:
            tried = self._resolve(s, again, quiet=True)
        except _ContractBroken:
            return spec
        if all(r.problem and not (r.facts or r.aggregate or r.page or r.candidates)
               for r in tried.deliverables):
            # What the wider reading declared resolves to nothing: the readings' own
            # account of why the request cannot be answered stands.
            s.ledger.append("goal_held_reading_unresolved", step=s.step)
            return spec
        s.index = held
        return again

    def _second_on_existence(self, s: _TurnState, first: GoalSpec, second_reading) -> GoalSpec:
        s.step += 1
        try:
            second, projection = second_reading.result()
        except Exception as exc:  # noqa: BLE001 -- as in _agree: the first stands
            s.ledger.append("goal_second_reading_failed", step=s.step,
                            detail=f"{type(exc).__name__}: {exc}"[:200])
            return first
        s.ledger.append("goal_second_reading", step=s.step, spec=second.to_json(),
                        agrees=not second.deliverables, differences=[], **projection)
        if second.deliverables and not _within(s, second):
            s.ledger.append("goal_readings_disagree_on_existence", step=s.step,
                            taken="second", reason="entities the first did not see")
            return second
        if second.deliverables:
            s.ledger.append("goal_readings_disagree_on_existence", step=s.step, taken="first")
        return first

    def _agree(self, s: _TurnState, chat, index, first: GoalSpec, today, second_reading) -> GoalSpec:
        """A second, independent reading of the request; when it disagrees with the
        first, resolved answers vote with two more readings before reconciliation.

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
        # A control the request names in the application's words belongs to both
        # readings before they are compared: one reading dropping it is not a
        # disagreement the model needs to settle.
        if (not second.deliverables and second.unsupported and not second.about_assistant
                and not first.about_assistant and _within(s, first)
                and not any(p.get("rules") for p in s.scope_parts)):
            # (A question about how the application behaves is answered from its rules,
            # and "no such rule" is said there; a reading's view that some data is not
            # held says nothing about it.)
            # One reading says the application does not hold what is asked, and names
            # it; the other found something to answer with. They disagree on whether
            # the data exists at all, which a third reading settles no better than the
            # first two: the one that answers maps the request to a neighbouring field
            # far more often than the one that refuses misses a real one. The turn says
            # what is missing rather than answer with something close to it.
            s.ledger.append("goal_second_reading", step=s.step, spec=second.to_json(),
                            agrees=False, differences=["the data asked for is not held"],
                            **projection)
            s.ledger.append("goal_readings_disagree_on_existence", step=s.step)
            return GoalSpec(deliverables=(), write_intent=first.write_intent,
                            unsupported=second.unsupported)
        first, second = _totals_agreed(first, second)
        declared = frozenset(_norm(d.action) for d in first.deliverables + second.deliverables
                             if d.kind == "handoff" and d.action)
        first, named_first = _named_controls(first, s.scope_parts, s.message.text, index, declared)
        second, named_second = _named_controls(second, s.scope_parts, s.message.text, index, declared)
        # A handoff is a request to change something whichever reading said so.
        first, second = (replace(r, write_intent=r.write_intent or any(
            d.kind == "handoff" for d in r.deliverables)) for r in (first, second))
        named = tuple(dict.fromkeys(named_first + named_second))
        if named:
            s.ledger.append("goal_control_named", step=s.step, controls=list(named))
        differences = first.differences(second)
        s.ledger.append("goal_second_reading", step=s.step, spec=second.to_json(),
                        agrees=not differences, differences=differences[:8], **projection)
        if not differences:
            return first
        pair = self._quiet_pair(s, first, second, today)
        if pair is not None and pair[0].deliverables == pair[1].deliverables:
            # Worded apart (a label in another language, conditions in another order)
            # but pointing at the same answer: the third reading could only choose
            # between two readings that lead to the same place.
            s.ledger.append("goal_readings_resolve_alike", step=s.step)
            return first
        if pair is not None and _finds(pair[0]) != _finds(pair[1]):
            # Prefer a resolved deliverable over a missing/invalid one. Zero counts
            # and empty lists are resolved answers, so this does not prefer positives.
            chosen = first if _finds(pair[0]) else second
            s.ledger.append("goal_readings_one_finds", step=s.step,
                            taken="first" if chosen is first else "second")
            return chosen
        if pair is not None:
            # Two independent readings that lead to different answers: two more, read
            # just as independently, and the answer most of them lead to is taken. A
            # reading shown the others is swayed by what it is shown; independent ones
            # are not, and a small model's own majority is steadier than its choice.
            taken = self._majority(s, index, first, second, pair, today)
            if taken is not None:
                return taken
        problems = ("a second reading of the same request declared "
                    + json.dumps(second.to_json(), ensure_ascii=False)
                    + "; the readings differ in: " + "; ".join(differences[:6])
                    + ". Decide which reading the request means and declare that",)
        s.step += 1
        elapsed = time.time() - s.started_at
        try:
            third, projection = s.read(previous=first, problems=problems, stage="reconcile")
        except (ValueError, ProviderError) as exc:
            s.ledger.append("goal_readings_unreconciled", step=s.step, detail=str(exc)[:200])
            return first
        if not third.deliverables:
            return first
        # What the two readings agreed a total on stays so; the deciding reading does
        # not make a sum a total on its own.
        third, _first = _totals_agreed(third, first)
        s.ledger.append("goal_declared", step=s.step, spec=third.to_json(), attempt=3,
                        reconciled=True, **projection)
        return third

    def _quiet_pair(self, s: _TurnState, first: GoalSpec, second: GoalSpec, today):
        """Both readings, prepared the way a declaration is before it is resolved,
        resolved quietly (read-only, no audit); None when that cannot run."""
        try:
            return (self._quietly(s, first, today), self._quietly(s, second, today))
        except Exception:  # noqa: BLE001 -- a comparison that cannot run is a difference
            return None

    def _quietly(self, s: _TurnState, spec: GoalSpec, today) -> Resolution:
        """A reading resolved as its declaration would be, read-only and unaudited."""
        today_text = str(today() or "") if callable(today) else ""
        spec, _flipped = _magnitudes(spec, s.message.text)
        spec, _handed = _as_handoffs(spec, s.message.text)
        spec, _reread = _as_said(spec, s.message.text, s.full_index or s.index)
        spec, _corrections = correct_records(spec, s.message.text, s.references,
                                             tuple(c.display for c in s.candidates))
        if s.candidates and spec.deliverables:
            spec, _picked = pick(spec, s.message.text, today_text, s.candidates)
        return self._resolve(s, spec, quiet=True)

    def _majority(self, s: _TurnState, index, first: GoalSpec, second: GoalSpec, pair,
                  today) -> GoalSpec | None:
        """Two more independent readings; the reading whose answer most of the four lead
        to, when that answer finds what it describes and at least two agree on it alone;
        None otherwise, and the deciding reading settles it."""
        with ThreadPoolExecutor(max_workers=_MORE_READINGS) as pool:
            futures = [pool.submit(copy_context().run, s.read, stage="vote") for _ in range(_MORE_READINGS)]
        readings = [(first, pair[0]), (second, pair[1])]
        for future in futures:
            try:
                vote, projection = future.result()
            except Exception as exc:  # noqa: BLE001 -- a missing vote is no vote
                s.ledger.append("goal_vote_failed", step=s.step,
                                detail=f"{type(exc).__name__}: {exc}"[:200])
                continue
            if not vote.deliverables:
                continue
            vote, _first = _totals_agreed(vote, first)
            vote, _named = _named_controls(vote, s.scope_parts, s.message.text, index)
            try:
                resolved = self._quietly(s, vote, today)
            except Exception:  # noqa: BLE001 -- a vote that cannot be resolved is no vote
                continue
            s.ledger.append("goal_declared", step=s.step, spec=vote.to_json(), attempt="vote",
                            **projection)
            readings.append((vote, resolved))
        wanted = {p.get("kind") for p in s.scope_parts or ()}
        if wanted:
            # What is asked for -- a record to open, a list, a value -- is the first
            # step's; a reading that asks for another kind has no vote on the answer.
            readings = [(spec, resolved) for spec, resolved in readings
                        if {d.kind for d in spec.deliverables} == wanted]
        groups: list[list[int]] = []
        keys: list[tuple] = []
        for i, (_spec, resolved) in enumerate(readings):
            key = _answer_key(resolved)
            if key in keys:
                groups[keys.index(key)].append(i)
            else:
                keys.append(key)
                groups.append([i])
        if not groups:
            s.ledger.append("goal_readings_voted", step=s.step, votes=0, groups=[])
            return None
        top = max(len(g) for g in groups)
        leaders = [g for g in groups if len(g) == top]
        s.ledger.append("goal_readings_voted", step=s.step, votes=len(readings),
                        groups=[len(g) for g in groups])
        if len(leaders) > 1:
            # A tie between an answer and a question whose choices include it: the
            # readings that answered used what the request says to pick one, the others
            # did not; the answer stands. Any other tie is no majority.
            definite = [g for g in leaders if not _asks(keys[groups.index(g)])]
            asking = [g for g in leaders if _asks(keys[groups.index(g)])]
            if (len(definite) == 1 and asking
                    and all(_answer_among(readings[definite[0][0]][1], readings[g[0]][1])
                            for g in asking)):
                leaders = definite
        best = leaders[0]
        if len(best) < 2 or len(leaders) > 1 or not _finds(readings[best[0]][1]):
            return None
        s.ledger.append("goal_readings_majority", step=s.step, taken=best[0], agreeing=len(best))
        return readings[best[0]][0]

    def _resolve(self, s: _TurnState, spec: GoalSpec, *, quiet: bool = False) -> Resolution:
        s.budget.check()
        reading_policy = getattr(self.goal_port, "requires_page_reading", None)
        scoped_read = (reading_policy(spec, current_path=_path_of(s.obs.url))
                       if self.config.observe_before_reads and callable(reading_policy) else True)
        if self.config.observe_before_reads and scoped_read and any(d.kind in {"fact", "list", "aggregate"} for d in spec.deliverables):
            receipt = self._dispatch(s, "observe", {})
            if receipt is None or not receipt.ok:
                return Resolution(tuple(Resolved(kind=d.kind, problem="fresh page reading is unavailable",
                    read_failure="unavailable", read_operation=d.kind, read_fields=d.fields)
                    for d in spec.deliverables), ())
        with s.receipt.stage("business"):
            return self._resolve_declared(s, spec, quiet=quiet)

    def _resolve_declared(self, s: _TurnState, spec: GoalSpec, *, quiet: bool = False) -> Resolution:
        # A deliverable that needs something this application's port does not offer is
        # answered here, plainly; the port sees only what it can resolve.
        offered = capabilities_of(self.goal_port)
        spec = _search_as_record(spec, offered, s.full_index or s.index)
        # A page the port can open is opened even when the port does not offer the filters
        # and groups asked to be applied to it: what the person asked to see is reached,
        # and the reply says what was left out. A read is not narrowed this way -- a
        # filter it cannot apply would change the numbers.
        spec, left_out = _ask_what_it_can(spec, offered)
        missing = [_needs_beyond(d, offered) for d in spec.deliverables]
        # A question answered by rules of the source is answered here, from the index the
        # model read, with the rules' citations; the port is not asked.
        from .coverage import missing_group, missing_form_values
        by_rule = [(missing_form_values(d, s.message.text, s.full_index or s.index)
                    if self.config.allow_form_prefill and {"fill", "handoffs"} <= offered else None) or
                   missing_group(d, s.message.text, s.index) or
                   (self._rules_answer(s, d) if d.rules and d.kind not in ("handoff", "page") else None)
                   for d in spec.deliverables]
        missing = [frozenset() if r is not None else m for r, m in zip(by_rule, missing)]
        asked = GoalSpec(deliverables=tuple(d for d, m, r in zip(spec.deliverables, missing, by_rule)
                                            if not m and r is None),
                         write_intent=spec.write_intent, unsupported=spec.unsupported,
                         conventions=s.conventions, language=_language(s), chosen=s.chosen)
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
        # A custom port returning a positive total without any requested groups has
        # not supplied that data answer. Empty scopes legitimately have no groups.
        partial = replace(partial, deliverables=tuple(
            replace(r, aggregate=None, problem="the application did not return the requested groups",
                    missing_group=d.metric.group_by)
            if (d.kind == "aggregate" and d.metric is not None and d.metric.op == "count"
                and d.metric.group_by and r.aggregate is not None
                and r.aggregate.count > 0 and not r.aggregate.groups) else r
            for d, r in zip(asked.deliverables, partial.deliverables, strict=True)))
        resolved = iter(partial.deliverables)
        resolution = Resolution(deliverables=tuple(
            r if r is not None else
            Resolved(kind=d.kind, problem="this application does not offer "
                     + ", ".join(sorted(m)) + " here", unoffered=tuple(sorted(m)))
            if m else replace(next(resolved), unoffered=tuple(sorted(left)))
            for d, m, r, left in zip(spec.deliverables, missing, by_rule, left_out)),
            reads=partial.reads)
        if quiet:
            return resolution
        s.ledger.append("goal_resolved", step=s.step,
                        conventions=[c.terms[0] for c in s.conventions],
                        kinds=[d.kind for d in resolution.deliverables],
                        problems=[d.problem for d in resolution.deliverables],
                        read_failures=[d.read_failure for d in resolution.deliverables],
                        left_out=[list(x) for x in left_out if x],
                        reads=[list(r) for r in resolution.reads])
        return resolution

    def _told_apart(self, s: _TurnState, spec: GoalSpec, resolution: Resolution) -> Resolution:
        """Several records share the name, and the request already says which one.

        "杭州云帆科技有限公司吴敏的手机" with two contacts named 吴敏, one at each of two
        companies, is not a question: the company is written in the request. Each
        candidate is shown with what tells it apart from the others; when the request
        writes that of exactly one of them, that record is the one meant. It is resolved
        again by its own full name, so nothing is taken on the strength of the words
        alone, and anything short of one record leaves the question as it was.
        """
        details = {c.display: c.detail for c in s.candidates}
        out, changed = list(resolution.deliverables), False
        for i, (d, r) in enumerate(zip(spec.deliverables, resolution.deliverables)):
            if len(r.candidates) < 2 or r.facts or r.aggregate or r.page:
                continue
            given = tuple(str(value) for _, value in d.field_values) + ((d.value,) if d.value else ())
            one = _said_apart(tuple(r.candidates), s.message.text, details, given)
            if not one:
                continue
            try:
                meant = self.goal_port.resolve(
                    GoalSpec(deliverables=(replace(d, record=one),), conventions=s.conventions,
                             language=_language(s), chosen=(*s.chosen, one)),
                    context=s.approvals.context(s.step, "resolve"),
                    current_path=_path_of(s.obs.url)).deliverables[0]
            except Exception:  # noqa: BLE001 -- the question stands when the check cannot run
                continue
            if meant.problem or len(meant.candidates) >= 2:
                continue
            s.ledger.append("goal_record_told_apart", step=s.step, count=len(r.candidates))
            out[i], changed = meant, True
        return replace(resolution, deliverables=tuple(out)) if changed else resolution

    def _which_amount(self, s: _TurnState, spec: GoalSpec, resolution: Resolution) -> Resolution:
        """"What is the amount of Acme's September invoice" when two invoices fit asks which
        one (owner ruling, 2026-09-27): a sum over a few records is an answer only when the
        request asks for a total. The records are listed as the choices."""
        if _TOTAL_WORDS.search(s.message.text) or (
                s.scope_parts and all(p.get("kind") == "aggregate" for p in s.scope_parts)):
            # Asked for a total, in so many words or as the first step read the request.
            return resolution
        out = list(resolution.deliverables)
        # The request points at one record: by its words ("那张"), or as the first step
        # read it, a value of one record, which a later declaration made a list.
        one = _ONE_RECORD.search(s.message.text) or (
            s.scope_parts and all(p.get("kind") == "fact" for p in s.scope_parts))
        for i, (d, r) in enumerate(zip(spec.deliverables, resolution.deliverables)):
            agg = r.aggregate
            if d.metric is not None and d.metric.op in ("min", "max"):
                # An extreme explicitly requests its complete tied set.
                continue
            if d.metric is not None and d.metric.total:
                # The declaration says the person asked for the total over all of them.
                continue
            scope_kinds = {part.get("kind") for part in s.scope_parts
                           if part.get("entity") == d.entity}
            one_list = one and not ("list" in scope_kinds
                                    and scope_kinds <= {"list", "aggregate"})
            if (one_list and d.kind == "list" and agg is not None and not r.problem
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
                    GoalSpec(deliverables=(replace(d, kind="list"),), conventions=s.conventions, language=_language(s),
                             chosen=s.chosen),
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
                    or chosen in text or chosen in named):
                continue
            said = _said_of(d.record, s.message.text)
            if said:
                # The request writes only part of the name ("Office Chair" of "[FURN_7777]
                # Office Chair"): what those words name is the request's, not the pick.
                try:
                    worded = self.goal_port.resolve(
                        GoalSpec(deliverables=(replace(d, record=said),),
                                 conventions=s.conventions, language=_language(s), chosen=s.chosen),
                        context=s.approvals.context(s.step, "resolve"),
                        current_path=_path_of(s.obs.url)).deliverables[0]
                except Exception:  # noqa: BLE001 -- the declared record stands when the check cannot run
                    worded = None
                fits = worded.candidates if worded is not None else ()
                # A record the words name exactly, passed over for a longer name ("黑色
                # Office Chair" read as Office Chair Black): the request said what tells
                # them apart. Otherwise the pick is the model's.
                exact = [c for c in fits if _names_exactly(c, said)]
                if (len(fits) >= 2 and any(chosen == _norm(c) for c in fits)
                        and not (exact and all(chosen != _norm(c) for c in exact))):
                    s.ledger.append("goal_record_not_chosen", step=s.step, count=len(fits))
                    out[i] = Resolved(kind=d.kind, candidates=tuple(fits),
                                      problem=f"{len(fits)} records hold the name the request gives")
                    continue
            unnamed = replace(d, record="")
            if not ((d.metric and d.metric.filters) or d.page.search):
                # No condition declared, but the request names who the record belongs to
                # ("Joel's order"): that link is what the request says of it. An amount or
                # a day the request writes may pick the one out, as the model read it.
                if (numbers_in(s.message.text) or days(s.message.text, s.today)
                        or _ORDINAL.search(s.message.text)):
                    continue
                link = _link_to_named(s, d)
                if link is None:
                    continue
                unnamed = replace(unnamed, metric=Metric(op="count", filters=(
                    Filter(field=link[0], values=(link[1],)),)))
            try:
                described = self.goal_port.resolve(
                    GoalSpec(deliverables=(unnamed,), conventions=s.conventions, language=_language(s),
                             chosen=s.chosen),
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
    def _dispatch(self, s: _TurnState, action: str, args: dict, *, navigating: bool = False,
                  admitted: bool = False):
        """One page command under write control, with its receipt on the record.

        `admitted` is a click the page loop's own policy already allowed."""
        if s.terminal_reason:
            return None
        s.budget.check()
        click_refusal = ""
        if admitted and action == "click":
            pass
        elif action == "click" and action in self.disabled_page_actions and self.config.click_policy is not None:
            click_refusal = click_policy_refusal(self.config.click_policy, s.obs, args.get("ref"))
            s.ledger.append("click_policy_decision", step=s.step, allowed=not click_refusal,
                            reason=click_refusal[:200])
        if action in self.disabled_page_actions and not (action == "click" and (admitted or (
                self.config.click_policy is not None and not click_refusal))):
            s.terminal_reason, s.detail = "agent_refused", "the deployment disabled this page action"
            s.ledger.append("goal_action_disabled", action=action)
            return None
        if action in PAGE_OPERATIONS:
            # Every page operation is under the turn's one leave; asked for at the first.
            reason, detail = s.approvals.confirm_operation(
                s.step + 1, s.obs, action=action, url=str(args.get("url") or ""))
            if reason:
                s.terminal_reason, s.detail = reason, detail
                return None
        # Approval may have waited long enough to exhaust the shared deadline.
        s.budget.check()
        s.step += 1
        s.ledger.append("tool_call", step=s.step, tool=action, args=dict(args))
        s.writes.expect_decision(False)
        try:
            with s.receipt.stage("wait" if action == "wait_for" else "ui"):
                receipt = s.browser.execute_with_budget(
                    action, args, timeout_s=s.budget.check(),
                    expect_navigation=navigating)
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
        s.refused = receipt.error_code == "capability_refused"
        if receipt.observation is not None:
            s.obs = receipt.observation
        s.budget.check()
        return receipt

    def _wait_ready(self, s: _TurnState, expected_path: str, *, record: bool,
                    requires_page_route: bool = False) -> bool:
        """Wait until the page shows the destination, not merely its address.

        A single-page application changes the URL first and renders later; an observation
        taken between the two still lists the previous page's controls. The page counts
        as ready when its path is the expected one, the runtime does not report it
        pending or blocked, the application reports the rendered record or view, and
        two consecutive observations agree. A plan requiring main-page identity must
        instead have that exact application fact in two fresh ready observations.
        """
        confirmed = 0
        last_confirmed_epoch = -1
        last_names: frozenset[str] | None = None
        for _ in range(_READY_WAITS):
            obs = s.obs
            path_ok = not expected_path or _path_of(obs.url) == expected_path
            # An application that reports what it rendered (a record route, the active
            # view, its facets) is believed when it does; one that reports nothing of the
            # kind must show controls. A changed URL with an empty loading screen is
            # not proof that the destination rendered, even if a generic runtime marked
            # the new projection ready.
            markers = bool(obs.record_route) or bool(obs.active_view) or obs.page_facets is not None
            rendered = ((bool(obs.record_route) if record else bool(obs.active_view))
                        or obs.page_facets is not None) if markers else bool(obs.elements)
            ready = obs.render_state != "pending"
            if requires_page_route:
                # Destination identity comes from the application's rendered main page.
                # Neither its current URL nor unrelated controls establish that identity.
                markers = True
                rendered = bool(expected_path) and obs.page_route == expected_path
                ready = obs.render_state == "ready"
            names = frozenset(e.name for e in obs.elements)
            if obs.render_state == "blocked":
                return False
            if path_ok and ready and rendered:
                # Seen rendered twice, half a second apart: the header controls and tabs
                # of a form arrive a beat after its route does.
                fresh = (obs.epoch > last_confirmed_epoch if requires_page_route
                         else obs.epoch != last_confirmed_epoch)
                if fresh and (markers or names == last_names or last_names is None):
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
            if receipt is None or s.refused:
                return False
        return False

    def _reach(self, s: _TurnState, plan: PagePlan) -> _PageOutcome:
        out = _PageOutcome()
        target = plan.url
        # Only what this page is asked to do counts towards why it was not reached.
        s.refused = False
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
        # A page the browser did not open is not waited for.
        if s.refused or not self._wait_ready(
                s, plan.expected_path,
                record=bool(plan.section or plan.highlight
                            or re.search(r"/\d+$", plan.expected_path)),
                requires_page_route=plan.requires_page_route):
            if s.terminal_reason:
                out.reached = False
                return out
            out.reached = False
            out.unmet.append(_Unmet("refused" if s.refused
                                    else "error_dialog" if s.obs.render_state == "blocked"
                                    else "page_unverified" if plan.requires_page_route
                                    else "not_opened"))
            out.url = s.obs.url
            return out
        for name in plan.facets_on:
            if not self._facet(s, name, active=True):
                out.unmet.append(_Unmet("filter_on", name))
        for name in plan.facets_off:
            if not self._facet(s, name, active=False):
                out.unmet.append(_Unmet("filter_off", name))
        if plan.conditions:
            from .page_filters import apply_page_filters
            if not apply_page_filters(self, s, plan):
                out.unmet.append(_Unmet("filter_on", "requested page conditions"))
        if (plan.page_number or plan.page_step or plan.page_size) and not out.unmet:
            from .paging import apply_paging
            out.paging = apply_paging(self, s, plan)
            if not out.paging:
                out.unmet.append(_Unmet("paging", _paging_words(plan, s.chinese)))
        if plan.section and not self._select_tab(s, plan.section):
            out.unmet.append(_Unmet("section", plan.section))
        if plan.missing_record:
            brought = self._show_record(s, plan.missing_record) if not out.unmet else ""
            if brought:
                out.record_shown = plan.missing_record
            if brought != "shown":
                # Its page in front with the application's own error on it is said as
                # that, not as a record that could not be opened.
                out.unmet.append(_Unmet("error_dialog") if brought == "blocked"
                                 else _Unmet("record", plan.missing_record))
        if plan.form is not None:
            out.form_requested = True
            if not out.unmet:
                prepared = handoff_form(self, s, plan)
                out.form_complete, out.form_fields, out.form_submit = (
                    prepared.complete, prepared.fields, prepared.submit_label)
                out.form_input_dispatched = prepared.input_dispatched
                out.form_reason, out.form_error_code = prepared.reason, prepared.error_code
                out.form_unfilled, out.form_unoffered = prepared.unfilled, prepared.unoffered
                if prepared.reason:
                    s.terminal_reason, s.detail = prepared.reason, prepared.detail
        elif plan.highlight and plan.fill:
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
        # A record brought on screen from its list may have taken the page elsewhere, and
        # so may a form whose opener leads to a page of its own. Either is judged by what
        # was brought on screen, not by the address the plan started from.
        elsewhere = bool(out.record_shown) or (
            plan.form is not None and (out.form_complete or out.form_input_dispatched)
            and _path_of(s.obs.url) != plan.expected_path)
        out.reached = (bool(out.record_shown) or out.form_complete) if elsewhere else (
            _path_of(s.obs.url) == (plan.expected_path or _path_of(s.obs.url)))
        if plan.requires_page_route and not elsewhere and (
                s.obs.page_route != plan.expected_path or s.obs.render_state != "ready"):
            out.reached = False
            out.unmet.append(_Unmet("page_unverified"))
        s.ledger.append("goal_page_settled", step=s.step, reached=out.reached,
                        unmet=[str(u) for u in out.unmet], url=s.obs.url,
                        render_state=s.obs.render_state, active_view=s.obs.active_view,
                        record_route=s.obs.record_route,
                        page_route=s.obs.page_route,
                        selected_controls=list(s.obs.selected_controls)[:8],
                        facets=[str(f.get("name", "")) for f in (s.obs.page_facets or [])][:8],
                        elements=len(s.obs.elements),
                        roles=sorted({e.role for e in s.obs.elements})[:12],
                        controls=[(e.role, e.name[:40], {k: v for k, v in e.attrs.items()
                                                          if k in ("role", "aria-selected", "selected", "field")})
                                  for e in s.obs.elements[:16]])
        return out

    def _show_record(self, s: _TurnState, name: str) -> str:
        """Bring a record that has no address on screen from the list it is in: the page
        loop finds its row and opens what the row offers. It is "shown" when the list is
        no longer what is in front -- another page, or a dialog over it -- and what is in
        front carries the record's name; "blocked" when its own page came up and the
        application reports an error on it; "" when it was not brought on screen."""
        if self.page_reach_steps <= 0 or s.terminal_reason:
            return ""
        from .reach import reach
        start, wanted = s.obs, _norm(name)
        listed = any(e.attrs.get("page_control") for e in start.elements)
        #: Whether the list, when last seen, had been narrowed to the one row carrying
        #: the name: what that row opens is then the record's, even where the view that
        #: opens shows its values in fields the page does not read out.
        alone = [False]

        def named(obs: PageObservation) -> bool:
            return wanted in _norm(obs.content) or any(
                wanted in _norm(e.name) or wanted in _norm(str(e.attrs.get("value") or ""))
                for e in obs.elements)

        def shown(obs: PageObservation) -> bool:
            if obs.render_state in {"pending", "blocked"}:
                return False
            moved = _path_of(obs.url) != _path_of(start.url)
            if not moved and any(e.attrs.get("page_control") for e in obs.elements):
                alone[0] = bool(obs.page_read is not None and obs.page_read.get("rows") == 1
                                and named(obs))
                return False
            # A confirmation box is a few buttons; a record's view is not.
            return (moved or listed) and (named(obs) or (
                alone[0] and len(obs.elements) >= _RECORD_VIEW_ELEMENTS))

        result = reach(self, s, steps=self.page_reach_steps, arrived=shown, names=(name,),
                       handoffs=self._handoffs(),
                       goal=f'The details of the record named "{name}" are open: its own '
                            "detail page, or the dialog that shows that record.")
        blocked = (not result.arrived and s.obs.render_state == "blocked"
                   and _path_of(s.obs.url) != _path_of(start.url) and named(s.obs))
        s.ledger.append("goal_record_reached", step=s.step, arrived=result.arrived,
                        blocked=blocked, steps=result.steps, why=result.why[:200])
        return "shown" if result.arrived else "blocked" if blocked else ""

    def _handoffs(self):
        from ..privacy import SessionHandoffPolicy
        return SessionHandoffPolicy.extending(
            self.application.ports.routes.session_handoff_query_keys)

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
        exact = [e for e in obs.elements if _is_tab(e) and _norm(e.name) == wanted]
        if exact:
            return exact[0] if len(exact) == 1 else None
        partial = [e for e in obs.elements if _is_tab(e) and wanted and wanted in _norm(e.name)]
        return partial[0] if len(partial) == 1 else None

    @staticmethod
    def _tab_selected(obs: PageObservation, wanted: str) -> bool:
        if obs.render_state in {"pending", "blocked"}:
            return False
        matching = [e for e in obs.elements if _is_tab(e) and _norm(e.name) == wanted]
        if len(matching) > 1:
            return False
        selected = any(_norm(c) == wanted for c in obs.selected_controls)
        if not matching:
            return selected
        element = matching[0]
        # ARIA-linked panels must be visible, correctly associated and no longer busy.
        if str(element.attrs.get("panel_ready", "true")).lower() != "true":
            return False
        return selected or any(str(element.attrs.get(attr, "")).lower() == "true"
                               for attr in _SELECTED_ATTRS)

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
        path, record = _path_of(s.obs.url), s.obs.record_route

        def enter(action, value):
            current = element
            for attempt in range(3):
                before = s.obs.epoch
                receipt = self._dispatch(s, action, {"ref": current.ref, "epoch": before,
                                                      "value": value})
                if receipt is None:
                    return False
                if receipt.ok:
                    return (receipt.observation is not None and s.obs.epoch > before
                            and _path_of(s.obs.url) == path
                            and s.obs.record_route == record)
                # Only this typed pre-dispatch refusal proves that nothing was entered.
                # Acknowledged errors and missing/unknown outcomes never change value
                # and retry: onchange may already have run.
                if receipt.error_code != "stale_observation" or attempt == 2:
                    return False
                if (_path_of(s.obs.url) != path or s.obs.record_route != record
                        or s.obs.render_state in {"pending", "blocked"}):
                    return False
                current = self._find_input(s, plan)
                if current is None:
                    return False
            return False

        if plan.fill_kind == "choice":
            if not enter("select", plan.fill):
                return "", False
        else:
            if not enter("fill", plan.fill):
                return "", False
            if plan.fill_kind == "link":
                self._dispatch(s, "wait_for", {"ms": 600})
                if s.terminal_reason:
                    return "", False
                wanted = _norm(plan.fill)
                options = [e for e in s.obs.elements
                           if e.role in _SUGGESTION_ROLES and e.name and _norm(e.name) == wanted]
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
        verified = (current is not None and value in {plan.fill, shown}
                    and s.obs.render_state == "ready" and _path_of(s.obs.url) == path
                    and s.obs.record_route == record)
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

    def _continued(self, message: AgentTurnMessage) -> tuple[str, str] | None:
        """The request a reply to this conversation's pending question continues: the
        earlier request with the record the person chose, and that record's name. None
        when the message does not answer a pending choice, which then stands on its own."""
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
        return ((f"{asked}（指的是 {chosen['record_ref']}）" if _CJK.search(asked)
                 else f"{asked} (meaning {chosen['record_ref']})"), chosen["record_ref"])

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
                fact = Fact(subject=f"{owner.label} · {item.about}",
                            label=_RULE_KIND_ZH.get(item.kind, item.kind) if s.chinese else item.kind,
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
            if inputs:
                return (inputs[0] if len(inputs) == 1
                        and not form_control_unavailable(inputs[0].attrs) else None)
        # A missing semantic field is not permission to type into a differently named
        # one. A label-only page may still bind one exact unnamed, available input.
        inputs = [e for e in s.obs.elements if e.role in _INPUT_ROLES
                  and not form_control_unavailable(e.attrs)
                  and (not plan.fill_field or not e.attrs.get("field"))
                  and _norm(str(e.attrs.get("field_label") or e.name)) == _norm(plan.highlight)]
        return inputs[0] if len(inputs) == 1 else None

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
    def _reply(self, s: _TurnState, deliverables, outcomes, declared=()) -> str:
        lines = []
        rendered_forms = set()
        zh = s.chinese
        # A request asking one thing is quoted when that thing was not done, so the
        # person sees in their own words what was not done.
        request = " ".join(s.message.text.split()) if len(deliverables) == 1 else ""
        names = [_kind_name(s.full_index, d.entity, zh) for d in declared] \
            if len(declared) == len(deliverables) else [""] * len(deliverables)
        for resolved, outcome, name in zip(deliverables, outcomes, names):
            if outcome is not None and outcome.form_requested:
                if id(outcome) in rendered_forms:
                    continue
                rendered_forms.add(id(outcome))
            lines.extend(_render(resolved, outcome, zh, request, name))
        if s.unsupported:
            lines.append(("未完成的部分：" if zh else "Parts not completed: ") + s.unsupported)
        if s.dropped:
            said = "、".join(f"「{q}」" for q in s.dropped) if zh else ", ".join(f"“{q}”" for q in s.dropped)
            lines.append((f"未完成的部分：你提到的{said}没有体现在这次操作里，助手目前做不到这一部分。") if zh
                         else f"Parts not completed: {said} from your request is not reflected in what "
                              "was done; the assistant cannot do that part.")
        # A turn stopped by the person, or by an unanswered card, says so in the line of
        # the deliverable it stopped; the internal detail behind it is not the person's.
        form_failure = any(r.page is not None and r.page.form is not None
                           and o is not None and not o.form_complete
                           for r, o in zip(deliverables, outcomes))
        if (not form_failure
                and s.terminal_reason not in ("agent_finished", "agent_refused", "needs_clarification") + _STOPPED):
            detail = s.detail or s.terminal_reason
            if zh and not re.search(r"[\u3400-\u9fff]", detail):
                lines.append("这次操作没有完成。")
            else:
                lines.append(("这次操作没有完成：" if zh else "The turn did not complete: ")
                             + detail)
        return self._say(s, "\n\n".join(line for line in lines if line))

    def _say(self, s: _TurnState, text: str) -> str:
        return text.strip()

    # -- settlement -------------------------------------------------------------------
    def _settle(self, s: _TurnState, current, text: str) -> AgentTurnExecution:
        exhausted = s.budget.projection()["terminal_reason"]
        if exhausted:
            s.terminal_reason = exhausted
            text = "本次任务已达到执行限额，未继续执行。" if s.chinese else "The turn budget was reached; execution stopped."
        if s.cancelled():
            s.terminal_reason = "user_cancelled"
            text = "操作已停止。" if s.chinese else "The task was stopped."
        if s.mutation_outcomes and s.terminal_reason != "agent_finished":
            confirmed = "、".join(item["effect"] for item in s.mutation_outcomes)
            text = ("已完成并核对的操作仍然有效：" if s.chinese else "These verified effects remain completed: ") + confirmed + "\n\n" + text
        if s.source_answer:
            text = "\n\n".join(part for part in (text, s.source_answer) if part)
            if not s.source_complete and s.terminal_reason in ("", "agent_finished"):
                s.terminal_reason = "agent_refused"
            if len(text) > self.config.max_user_answer_chars:
                s.terminal_reason = "agent_refused"
                text = (("回答超过展示长度，以下仅为部分结果，请缩小问题范围。\n\n" if s.chinese
                         else "The answer exceeds the display limit; only part is shown. Please narrow the request.\n\n")
                        + text)
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
                          detail=s.detail,
                          clarification=s.clarification if terminal == "needs_clarification" else None)
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
            business_result=(BusinessToolResult(
                {"outcomes": s.mutation_outcomes}, {"verified_outcomes": len(s.mutation_outcomes)})
                if s.mutation_outcomes else None), answer_fallback_used=fallback,
            started_at=s.started_at, finished_at=time.time())
        self.conversation_state.record(
            message_id=s.message.message_id, user_text=s.message.text,
            assistant_text=user_answer, status=status.value, business_result=execution.business_result,
            clarification=s.clarification if status == AgentTurnStatus.NEEDS_INPUT else None)
        s.ledger.append("conversation_state_updated",
                        retained_turns=self.conversation_state.turn_count,
                        context_digest=self.conversation_state.content_digest)
        s.ledger.append("product_turn_finished", status=status.value,
                        terminal_reason=terminal, answer_fallback_used=fallback,
                        business_result_digest=execution.business_result.content_digest if execution.business_result else "")
        s.ledger.append("goal_run_receipt", budget=s.budget.projection(),
                        **s.receipt.report(self.chat, s.run_id))
        try:
            self.channel.publish("progress", {
                "state": status.value, "terminal_reason": terminal, "message": user_answer,
            }, turn_id=s.message.message_id)
        except Exception as exc:
            s.ledger.append("terminal_delivery_lost", status=status.value,
                            error_type=type(exc).__name__)
        return execution


#: A quantity as a person writes it in running Chinese text: digits directly before a
#: measure word or noun (50条, 100顶, 10卷). Digits inside codes and names are not these.
_QUANTITY = re.compile(r"(?<![A-Za-z0-9_.:：/\-])(\d+(?:\.\d+)?)(?=([\u4e00-\u9fff]))")


def _open_form_shows(observation: PageObservation, name: str) -> bool:
    """Whether a form in a dialog on the page shows `name` in one of its own controls."""
    wanted = _norm(name)
    return bool(wanted) and any(
        str(e.attrs.get("form_scope") or "").startswith("dialog") and e.attrs.get("field_label")
        and wanted in {_norm(str(e.attrs.get("value") or "")), _norm(str(e.attrs.get("selected_option") or ""))}
        for e in observation.elements)


def _dropped_quantities(spec: GoalSpec, text: str, candidates: tuple = ()) -> tuple[str, ...]:
    """Quantities the request wrote that appear nowhere in the declaration, by value.

    Days and clock times are read elsewhere and are not quantities. A number is compared
    by its value, so 9 is found in 09:00 and 2000 in 2,000.00.
    """
    declared = json.dumps(spec.to_json(), ensure_ascii=False)
    chosen = " ".join(f"{c.display} {c.detail}" for c in candidates if c.display in declared)
    def value(raw):
        try:
            return float(raw.replace(",", ""))
        except ValueError:
            return None
    held = {value(n) for n in _numbers(declared + " " + chosen)}
    out = []
    for match in _QUANTITY.finditer(without_days(text)):
        if value(match.group(1)) not in held:
            out.append(match.group(1) + match.group(2))
    return tuple(dict.fromkeys(out))[:4]


def _paging_words(plan, zh: bool) -> str:
    parts = []
    if plan.page_size:
        parts.append(f"改成每页 {plan.page_size} 条" if zh else f"show {plan.page_size} per page")
    if plan.page_number:
        parts.append(f"翻到第 {plan.page_number} 页" if zh else f"go to page {plan.page_number}")
    elif plan.page_step:
        parts.append(("翻到下一页" if plan.page_step > 0 else "翻到上一页") if zh
                     else ("go to the next page" if plan.page_step > 0 else "go to the previous page"))
    return ("、" if zh else " or ").join(parts)


def _numbers(text: str) -> set[str]:
    """Numbers as written, without separators or trailing zero decimals."""
    out = set()
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text or ""):
        value = raw.replace(",", "")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        out.add(value)
    return out


def _hands_over_a_named_control(spec: GoalSpec, parts: list, text: str) -> bool:
    """The declaration hands over a control the request writes in the application's
    words and the first step named: what is asked is settled by the request itself."""
    terms = {_norm(t) for p in parts for t in (p.get("terms") or ())}
    return any(d.kind == "handoff" and d.action and _norm(d.action) in terms
               and _names_verbatim(text, d.action) for d in spec.deliverables)


def _coverage_gaps(spec: GoalSpec, parts: list, text: str, today: str,
                   candidates: tuple = (), *, named: bool = False, index=None) -> tuple[str, ...]:
    """What the request asked that the declaration leaves out, judged by shape only:
    values asked for (the first step said so) with only pages or handoffs declared; a
    code, amount or day the request wrote that appears nowhere in the declaration nor in
    the candidate values it chose."""
    gaps = []
    from .coverage import missing_group
    gaps.extend(r.repair for d in spec.deliverables if (r := missing_group(d, text, index)))
    reads = ("fact", "list", "aggregate")
    asked = [p for p in parts if p.get("kind") in reads]
    # A list asked for is also delivered by its own page narrowed to the same records:
    # the person sees the list there, and the applied conditions are verified on the page.
    # So is what a named record's tab lists: the request asked to see it, and the tab is
    # where the application shows it.
    shown = {d.entity for d in spec.deliverables if d.kind == "page"
             and (d.page.conditions or (d.record and d.section))}
    asked = [p for p in asked if not (p.get("kind") == "list" and p.get("entity") in shown)]
    if asked and not named and not any(d.kind in reads for d in spec.deliverables):
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
    gaps.extend(f"the request says {q!r} but the declaration carries that quantity nowhere; put it "
                "where it belongs (a value, a condition, a limit, page_number or page_size), or "
                "say in unsupported that it cannot be done" for q in _dropped_quantities(spec, text, candidates))
    for day in days(text, today):
        if day not in declared and day[5:] not in declared:
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
        return (outcome.reached and not outcome.unmet
                and (resolved.kind != "handoff" or bool(resolved.page
                     and (resolved.page.highlight or resolved.page.form)))
                and (not resolved.page or not resolved.page.missing_record
                     or bool(outcome.record_shown))
                and (not resolved.page or not resolved.page.form
                     or (outcome.form_complete and not outcome.form_unfilled
                         and not outcome.form_unoffered))
                and (not resolved.page or not resolved.page.fill
                     or bool(outcome.filled and outcome.fill_verified)))
    return False


def _is_tab(element) -> bool:
    """A tab is an element the page marks as one, or one that carries selection state
    the way tabs do; a runtime may report a `<button role="tab">` as a button."""
    return element.role == "tab" or "aria-selected" in element.attrs


class _ContractBroken(Exception):
    """The port answered a declaration with deliverables that do not match it."""


def _search_as_record(spec: GoalSpec, offered: frozenset[str], index=None) -> GoalSpec:
    """Where the application offers no page search, the words a deliverable would have
    searched for are read for what they are: the name of what is listed ("收款单" on the
    receipts page) says nothing, and a page deliverable's other words name the record to
    open, looked up as its name."""
    if "search" in offered or not any(d.page.search for d in spec.deliverables):
        return spec

    def names_the_kind(d) -> bool:
        entity = index.entity(d.entity) if index is not None and d.entity else None
        page = index.page(d.page.key) if index is not None and d.page.key else None
        said = _norm(d.page.search)
        names = [*(entity.label, *entity.aliases)] if entity is not None else []
        names += [page.label, *page.aliases] if page is not None else []
        return any(said == _norm(n) or said in _norm(n) for n in names if n)

    def a_condition_already(d) -> bool:
        # The words are a value the deliverable's conditions already hold ("厦门瑞丰软件定制合同"
        # as the 合同 of a payment-plan list): searching for them again adds nothing.
        said = _norm(d.page.search)
        return d.metric is not None and any(
            said == _norm(v) for f in d.metric.filters for v in f.values)

    out = []
    for d in spec.deliverables:
        if d.page.search and (names_the_kind(d) or a_condition_already(d)):
            d = replace(d, page=replace(d.page, search=""))
        elif d.kind == "page" and d.page.search and not d.record:
            d = replace(d, record=d.page.search, page=replace(d.page, search=""))
        out.append(d)
    return replace(spec, deliverables=tuple(out))


def _needs_beyond(d, offered: frozenset[str]) -> frozenset[str]:
    """The optional capabilities a deliverable needs that the port does not offer."""
    needs = set()
    if d.page.conditions or d.page.filters_on or d.page.filters_off or d.page.group_by or (
            d.metric is not None and d.metric.page_filters):
        needs.add("facets")
    if d.section:
        needs.add("sections")
    if d.page.search:
        needs.add("search")
    if d.page.paged:
        needs.add("paging")
    if d.kind == "handoff":
        needs.add("handoffs")
        if d.value:
            needs.add("fill")
    return frozenset(needs) - offered


def _ask_what_it_can(spec: GoalSpec, offered: frozenset[str]) -> tuple[GoalSpec, tuple[tuple[str, ...], ...]]:
    """The spec as the port can be asked it, and per deliverable the capabilities left
    out of it.

    A page is asked for without the filters and groups the port does not offer: the page
    itself is what the person asked to see, and the reply says plainly that this
    application does not offer them. A read keeps its filters -- dropping one would answer
    about more records than were asked for -- so it stays a deliverable this application
    cannot answer.
    """
    out, left = [], []
    for d in spec.deliverables:
        drop = frozenset({"facets", "paging"} & _needs_beyond(d, offered)) if d.kind == "page" else frozenset()
        if "facets" in drop:
            d = replace(d, page=replace(d.page, filters_on=(), filters_off=(), group_by="", conditions=()))
        if "paging" in drop:
            d = replace(d, page=replace(d.page, page_number=0, page_step=0, page_size=0))
        out.append(d)
        left.append(tuple(sorted(drop)))
    return replace(spec, deliverables=tuple(out)), tuple(left)


def _unresolved(spec: GoalSpec, resolution: Resolution) -> tuple[int, int]:
    """How much is left unanswered, worst first: deliverables that came to nothing (and a
    change asked for with nothing to point at), then answers that are there but not whole
    (every field asked for empty, or some asked for missing). Compared as a pair, a found
    record with an empty field is nearer an answer than no record at all."""
    hard = sum(1 for d in resolution.deliverables
               if d.problem and not d.denied and not (d.facts or d.aggregate
                                     or (d.page and d.kind not in ("fact", "list", "aggregate"))))
    if spec.write_intent and not any(d.kind == "handoff" for d in spec.deliverables):
        hard += 1
    soft = sum(1 for d in resolution.deliverables
               if d.facts and not d.problem and len(d.empty) == len(d.facts))
    soft += sum(1 for d in resolution.deliverables if d.facts and d.problem and not d.denied)
    return hard, soft


def _moved_from_named_kind(s: _TurnState, before: GoalSpec, after: GoalSpec) -> bool:
    """A repaired deliverable now on another entity, where the request names the kind of
    record declared first (its label or one of its aliases) and not the new one's."""
    index = s.full_index or s.index
    if index is None:
        return False
    said = _norm(s.message.text)

    def named(key: str) -> bool:
        entity = index.entity(key) if key else None
        if entity is None:
            return False
        # Its label or an alias, or a word the deployment's conventions give it ("客户"
        # for the CRM's customers).
        words = [entity.label, *entity.aliases]
        words += [t for c in getattr(index, "conventions", ()) or () if c.entity == key
                  for t in c.terms]
        return any(n and _norm(n) in said for n in words)

    def same_word(old: str, new: str) -> bool:
        """The word the person used for the first kind is part of what the second is
        called too ("订单" for a sales order; the record is a 交易订单): they named a kind
        of thing both are, and the record's number says which."""
        first, second = index.entity(old), index.entity(new)
        if first is None or second is None:
            return False
        words = [first.label, *first.aliases] + [
            t for c in getattr(index, "conventions", ()) or () if c.entity == old for t in c.terms]
        called = [_norm(n) for n in (second.label, *second.aliases) if n]
        return any(n and _norm(n) in said and any(_norm(n) in c for c in called) for n in words)

    return any(b.entity and a.entity and b.entity != a.entity and named(b.entity)
               and not named(a.entity) and not same_word(b.entity, a.entity)
               for b, a in zip(before.deliverables, after.deliverables))


def _still_nothing(before: Resolution, after: Resolution) -> bool:
    """Every deliverable came to nothing before because a name was not found, and after
    the repair every one still comes to nothing."""
    def nothing(d) -> bool:
        return bool(d.problem) and not (d.facts or d.aggregate or d.page or d.candidates)
    return (bool(before.deliverables) and all(nothing(d) and d.missing for d in before.deliverables)
            and all(nothing(d) for d in after.deliverables))


def _repairable(spec: GoalSpec, resolution: Resolution, *,
                observed_empty_entities: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """What to tell the model before its one second declaration; empty when nothing
    about the declaration, as opposed to the data, is what failed."""
    problems = [r.repair for d, r in zip(spec.deliverables, resolution.deliverables, strict=True)
                if r.repair and not (d.kind == "fact" and d.entity in observed_empty_entities
                    and r.facts and not r.problem and not r.missing_fields
                    and set(r.empty) == set(d.fields) == {f.label for f in r.facts})]
    # The application would not read an entity the way it was declared. Another entity of
    # the index may hold the same thing under a module the request did not name (stock in
    # the warehouse module or in the purchasing one); the declarer is told once.
    problems += [f"the application refused to read {d.entity} this way"
                 + (f" ({r.refused[:120]})" if r.refused else "")
                 + "; if another entity of the index holds what is asked, declare it there, "
                   "keeping every condition; otherwise declare the same again"
                 for d, r in zip(spec.deliverables, resolution.deliverables, strict=True)
                 if r.read_failure == "refused" and not r.repair and not r.denied and d.entity]
    if spec.write_intent and not any(d.kind == "handoff" for d in spec.deliverables):
        problems.append("the person asks for a change (write_intent is true) but no "
                        "deliverable is a handoff: declare a handoff on the record meant, "
                        "with action set to the button or the field label to change")
    return tuple(dict.fromkeys(problems))


#: A rule's kind, as a Chinese reply names it.
_RULE_KIND_ZH = {"visibility": "何时显示", "editability": "何时可改", "refusal": "何时拒绝",
                 "meaning": "含义", "target": "打开什么"}
#: A value the record does not hold, as a Chinese reply says it.
_EMPTY_ZH = {"(empty)": "（空）"}


def _render(resolved: Resolved, outcome: _PageOutcome | None, zh: bool,
            request: str = "", name: str = "") -> list[str]:
    lines: list[str] = []
    if resolved.facts:
        for fact in resolved.facts:
            lines.append(f"**{fact.subject} · {fact.label}**：{_EMPTY_ZH.get(fact.value, fact.value)}"
                         if zh else f"**{fact.subject} · {fact.label}**: {fact.value}")
        if resolved.citations:
            # The rule's source, verbatim: the lines it names are hashed, so a reader
            # or an evaluator can check the citation names exactly those bytes.
            lines.append(("依据（部署版本源码中的规则，不是当前数据）：" if zh
                          else "Source (a rule of the deployed version's source, not current data):")
                         + "\n" + "\n".join(resolved.citations))
    agg = resolved.aggregate
    if agg is not None:
        if agg.extreme is not None:
            operation, label, value = agg.extreme
            adjective = ("最高" if operation == "max" else "最低") if zh else (
                "maximum" if operation == "max" else "minimum")
            head = f"**{agg.subject} · {label} {adjective}**：" if zh else f"**{agg.subject} · {label} {adjective}**: "
            if value is None:
                head += "没有已知数值。" if zh else "No known numeric values."
            else:
                head += value + (f"；全部并列记录（{len(agg.records)} 条）：" if zh
                                 else f"; all tied records ({len(agg.records)}):")
                head += "".join(f"\n{n}. {' '.join(str(record).split())}"
                                for n, record in enumerate(agg.records, 1))
            lines.append(head)
        # Records listed are shown as a list, whichever kind asked for them (a fact
        # about some lines is answered with those lines).
        elif resolved.kind == "list" or agg.records:
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
        # For list queries, if the page was successfully opened, show that success first
        # rather than treating it as a failure. The page is where the user can see the list.
        if (resolved.kind == "list" and resolved.page is not None
                and outcome is not None and outcome.reached and not outcome.unmet):
            # Page opened successfully - continue to page rendering logic below
            pass
        else:
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
                lines.append(_not_done(resolved, zh, request, name) + where)
            return lines
    if resolved.page is not None and outcome is not None:
        plan = resolved.page
        if plan.missing_record and outcome.record_shown:
            if not outcome.unmet:
                lines.append((f"已打开「{outcome.record_shown}」的详情。" if zh
                              else f"Opened the details of “{outcome.record_shown}”."))
            else:
                why = ("、" if zh else ", ").join(u.said(zh) for u in outcome.unmet)
                lines.append((f"已打开「{outcome.record_shown}」的页面，但{why}，页面内容可能不完整。") if zh
                             else f"Opened the page of “{outcome.record_shown}”, but {why}; "
                                  "the page may be incomplete.")
            return lines
        if plan.missing_record and (outcome.reached or any(u.kind == "record" for u in outcome.unmet)):
            opened = ((f"已打开：{plan.label}。" if zh else f"Opened: {plan.label}. ")
                      if outcome.reached else "")
            why = (f"当前集成没有提供「{plan.missing_record}」的详情地址，"
                   "未打开该记录详情或执行记录内的操作。") if zh else (
                   f"The integration has no detail address for “{plan.missing_record}”; "
                   "record details were not opened and no record actions were performed.")
            lines.append(opened + why)
            return lines
        if plan.form is not None:
            if outcome.form_complete:
                fields = ("、" if zh else ", ").join(outcome.form_fields)
                submit = outcome.form_submit
                lines.append((f"已预填：{fields}，尚未提交。请核对并完善其余必填内容，"
                              f"再手动点击页面高亮的‘{submit}’。"
                              if submit else f"已预填：{fields}，尚未提交。请核对并完善其余必填内容后自行提交。") if zh else
                             (f"Prefilled: {fields}. The form has not been submitted. Review the values "
                              "and complete any remaining required fields, then "
                              + (f"manually click the highlighted '{submit}'." if submit else "submit it yourself.")))
            elif not outcome.form_input_dispatched:
                lines.append("表单操作未完成，没有填写或提交。" if zh else
                             "The form operation did not complete. No values were entered or submitted.")
            else:
                lines.append("表单填写或交接未完成；部分字段可能已输入。请核对当前页面，助手没有提交。" if zh else
                             "Form entry or handoff did not complete; some fields may have been entered. "
                             "Check the current page. The assistant did not submit.")
            if not outcome.form_complete:
                why = (("、" if zh else ", ").join(u.said(zh) for u in outcome.unmet)
                       if outcome.unmet else form_failure_message(
                           outcome.form_reason, outcome.form_error_code, zh))
                lines.append(("原因：" if zh else "Reason: ") + why + ("。" if zh else "."))
            for label, option in outcome.form_unoffered:
                lines.append((f"未填写：「{label}」的选项里没有「{option}」，这一项需要你自己选。") if zh
                             else f"Not filled: “{label}” does not offer “{option}”; that choice is yours to make.")
            if outcome.form_unfilled:
                asked = ("、".join(f"「{n}」" for n in outcome.form_unfilled) if zh
                         else ", ".join(f"“{n}”" for n in outcome.form_unfilled))
                lines.append((f"未填写：这张表单上没有找到{asked}对应的字段，这部分需要你自己填。") if zh
                             else f"Not filled: this form shows no field for {asked}; that part is yours to enter.")
            return lines
        if resolved.kind == "handoff":
            if not plan.highlight:
                lines.append("没有确定要准备的操作或字段，请说明要做什么；助手没有执行页面操作。" if zh else
                             "No operation or field to prepare was determined. Please specify the goal; "
                             "the assistant did not perform page actions.")
                return lines
            if outcome.highlighted and outcome.filled and not outcome.fill_verified:
                lines.append((f"已打开 {plan.label} 并标出「{outcome.highlighted}」。"
                              f"尝试输入「{outcome.filled}」，但未能核验页面中的完整值；"
                              "请检查当前字段后自行保存，助手没有提交。") if zh else
                             (f"Opened {plan.label} and highlighted “{outcome.highlighted}”. "
                              f"Attempted to enter “{outcome.filled}”, but could not verify "
                              "the complete value on the page. Check the field and save "
                              "it yourself; the assistant did not submit."))
            elif outcome.highlighted and outcome.filled:
                # The field holds the value; saving is the person's. A form that saves
                # itself on leaving the page needs a deliberate discard when unwanted.
                lines.append((f"已打开 {plan.label}，已在「{outcome.highlighted}」填入"
                              f"「{outcome.filled}」并标出，请你核对后自行保存；"
                              "助手只做准备，提交由你完成。不需要这次修改就点放弃。") if zh else
                             (f"Opened {plan.label} and entered “{outcome.filled}” in "
                              f"“{outcome.highlighted}”; please check it and save it "
                              "yourself. The assistant prepares, you submit. Discard it if "
                              "the change is not wanted."))
            elif outcome.highlighted and outcome.fill_failed:
                lines.append((f"已打开 {plan.label} 并在页面上标出「{outcome.highlighted}」，"
                              f"但未能核验「{outcome.fill_failed}」的填写结果，部分值可能已输入；请检查后自行保存；"
                              "助手只做准备，提交由你完成。") if zh else
                             (f"Opened {plan.label} and highlighted “{outcome.highlighted}”, "
                              f"but could not verify entry of “{outcome.fill_failed}”; some "
                              "values may have been entered. Check the page and save it "
                              "yourself. The assistant prepares, you submit."))
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
            elif outcome.rule:
                # The deployment's source says when the control shows: that is the reason
                # given, with its citation, rather than a guess about permissions.
                lines.append((f"{plan.label} 页面上没有显示「{plan.highlight}」。部署源码的规则："
                              f"{outcome.rule}。助手没有执行任何操作。") if zh else
                             (f"{plan.label} does not show “{plan.highlight}”. The deployed "
                              f"source's rule: {outcome.rule}. The assistant did nothing."))
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
            if outcome.paging:
                number, size = outcome.paging.split("/")
                extras.append(f"第 {number} 页，每页 {size} 条" if zh else f"page {number}, {size} per page")
            suffix = f"（{'；'.join(extras)}）" if extras and zh else (
                f" ({'; '.join(extras)})" if extras else "")
            if plan.missing_view:
                # The page is open, in the only kind of view it has; the one asked for
                # is not something this page offers, and the reply says so.
                suffix += (f"，但这个页面不支持 {plan.missing_view} 视图" if zh
                           else f", but this page does not support a {plan.missing_view} view")
            if resolved.unoffered:
                # The page is open; the filtering or grouping asked to be applied to it is
                # not something this application offers, and the reply says so.
                offers = ("、".join(_OFFER_ZH.get(n, n) for n in resolved.unoffered) if zh
                          else ", ".join(_OFFER_EN.get(n, n) for n in resolved.unoffered))
                suffix += (f"，但这个应用没有提供{offers}" if zh
                           else f", but this application does not offer {offers}")
            lines.append((f"已打开：{what}{suffix}") if zh else f"Opened: {what}{suffix}")
        elif outcome.reached and all(u.kind == "paging" for u in outcome.unmet):
            # The list is open as it was; the page or page size asked for was not reached.
            lines.append((f"已打开：{plan.label}，但{outcome.unmet[0].said(True)}") if zh
                         else f"Opened: {plan.label}, but {outcome.unmet[0].said(False)}")
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
    elif (resolved.problem or resolved.no_matches) and not (resolved.facts or resolved.aggregate or resolved.page):
        lines.append(_not_done(resolved, zh, request, name))
    return lines


#: The optional capabilities of a goal port (`OPTIONAL`), as a person would name them.
_OFFER_ZH = {"facets": "页面筛选与分组", "paging": "翻页和每页条数", "sections": "页签", "lines": "明细行",
             "handoffs": "指出控件", "fill": "预先填值", "search": "页面搜索",
             "source": "源码规则", "values": "候选值"}
_OFFER_EN = {"facets": "page filters and groups", "paging": "paging", "sections": "tabs", "lines": "line items",
             "handoffs": "pointing at controls", "fill": "entering values",
             "search": "page search", "source": "rules from source", "values": "candidate values"}


#: The operations an adapter may report as denied, as a person would name them.
_DENIED = {"delete": ("删除", "delete"), "create": ("新建", "create"),
           "write": ("修改", "change"), "read": ("读取", "read")}


def _cannot(request: str, why: str, zh: bool) -> str:
    """The reply to a request the assistant cannot do. The model's reason is shown when
    it is written in the person's language; otherwise the request is quoted, so the
    person is never answered in a language they did not use."""
    if zh:
        if why and _CJK.search(why):
            return f"这个请求助手做不到。（{why}）"
        return f"「{' '.join(request.split())}」这个请求助手做不到。"
    return "The assistant cannot do this request." + (f" ({why})" if why else "")


def _kind_name(index, entity_key: str, zh: bool) -> str:
    """What the person calls records of an entity: the application's Chinese name for
    it in a Chinese reply, its label otherwise; empty when the index does not hold it."""
    entity = index.entity(entity_key) if index is not None and entity_key else None
    if entity is None:
        return ""
    return (entity.aliases[0] if zh and entity.aliases else entity.label)


def _not_done(resolved: Resolved, zh: bool, request: str = "", name: str = "") -> str:
    """A part that did not get done, as the person is told it. The resolver's own
    reason ("filter 'p262' is not usable here") is written for the declarer, in the
    index's terms, and stays in the audit and the repair; the person hears that it was
    not done and, when several records fit, which ones."""
    if resolved.input_fields:
        labels = "、".join(resolved.input_fields) if zh else ", ".join(resolved.input_fields)
        return (f"请提供「{labels}」的新值；如果要清空，请明确说明。" if zh
                else f"Please provide new values for {labels}; say explicitly if you want to clear them.")
    if resolved.no_matches:
        return (f"在当前账号可见范围内，没有找到符合「{resolved.no_matches}」的记录。" if zh
                else f"No record matches {resolved.no_matches} within this account's visible scope.")
    if resolved.missing_fields:
        labels = "、".join(resolved.missing_fields) if zh else ", ".join(resolved.missing_fields)
        return (f"当前可查询的字段中没有「{labels}」，这一项没有完成。" if zh
                else f"The available fields do not include {labels}; this part was not done.")
    if resolved.missing_group:
        return (f"未能按「{resolved.missing_group}」分组统计，这一项没有完成。" if zh else
                f"Counts grouped by {resolved.missing_group} were not resolved; this part was not done.")
    if resolved.denied:
        # The account's own rights, not the assistant, stand in the way; nothing was done.
        # A new record is not "this record": it is records of this kind.
        zh_op, en_op = _DENIED.get(resolved.denied, (resolved.denied, resolved.denied))
        if resolved.denied == "read":
            zh_what, en_what = (name or "这类记录"), (name or "these records")
            return (f"你的账号没有读取{zh_what}的权限，这一项没有完成。" if zh
                    else f"Your account has no permission to read {en_what}; this part was not done.")
        zh_what, en_what = (("这类记录", "records of this kind") if resolved.denied == "create"
                            else ("这条记录", "this record"))
        if name:
            # Named as the person names them ("没有新建产品的权限").
            zh_what, en_what = name, (f"{name} records" if resolved.denied == "create"
                                      else f"this {name} record")
        if request:
            return (f"「{request}」没有执行：你的账号没有{zh_op}{zh_what}的权限，助手没有执行任何操作。"
                    if zh else f"\"{request}\" was not done: your account has no permission to "
                    f"{en_op} {en_what}; the assistant did nothing.")
        return (f"你的账号没有{zh_op}{zh_what}的权限，助手没有执行任何操作。" if zh
                else f"Your account has no permission to {en_op} {en_what}; the assistant did nothing.")
    if "handoffs" in resolved.unoffered:
        # A change asked of an assistant that can only read here: that is the reason,
        # not the capabilities a guided change would have used. The request is quoted, so
        # the person sees which change was not made.
        asked = (f"「{request}」没有执行：" if zh else f"\"{request}\" was not done: ") if request else ""
        return (asked + "助手在这个应用里是只读的：只能查数据、打开页面，不能替你修改、提交或审核，"
                "也不能带你完成这项修改" + ("。" if asked else "，这一项没有执行。") if zh
                else asked + "The assistant is read-only in this application: it can look up "
                     "data and open pages, but cannot change, submit or approve anything, or "
                     "guide you through the change" + ("." if asked else "; this part was not done."))
    if resolved.unoffered:
        # What the application does not offer is the person's to know, plainly.
        offers = [(_OFFER_ZH if zh else _OFFER_EN).get(name, name) for name in resolved.unoffered]
        return (f"这个应用没有提供{'、'.join(offers)}，这一项无法完成。" if zh
                else f"This application does not offer {', '.join(offers)}, so this part "
                     "could not be done.")
    if resolved.read_failure:
        subject = name or ("这类记录" if zh else "these records")
        operation = resolved.read_operation
        labels = ("、" if zh else ", ").join(resolved.read_fields)
        target = ({"count": "数量", "sum": (labels + "合计") if labels else "金额合计",
                   "min": (labels + "最低值"), "max": (labels + "最高值"),
                   "list": "记录列表", "fact": labels or "字段值"}.get(operation, "请求结果") if zh else
                  {"count": "the count", "sum": "the total of " + (labels or "the requested field"),
                   "min": "the minimum of " + labels, "max": "the maximum of " + labels,
                   "list": "the record list", "fact": labels or "the field values"}.get(operation, "the requested result"))
        text = (f"{subject}未完成读取，无法确定{target}。" if zh else
                f"The read of {subject} did not complete; cannot determine {target}.")
        if resolved.read_failure == "unavailable":
            text += "读取服务暂不可用。" if zh else " The read service is unavailable."
        # Only the adapter's explicit user-facing reason, never exception prose.
        why = " ".join(resolved.refused.split())
        if why and (not zh or _CJK.search(why)):
            text += (f"应用提示：{why}" if zh else f" Application reports: {why}")
        return text
    if resolved.refused:
        # The application's own sentence for a read it refused, when that sentence names
        # what it needs ("请选择时间范围"): the person can act on that, and a bare "not
        # done" leaves them with nothing to do. Shown when it is in their language, as a
        # model's reason is; the audit keeps the fuller one either way.
        why = " ".join(resolved.refused.split())
        if zh and _CJK.search(why):
            return f"这一项没有完成：{why}。"
        if not zh:
            return f"This part was not done: {why}."
    names = "、".join(resolved.candidates) if zh else ", ".join(resolved.candidates)
    if zh:
        return "无法完成这一项。" + (f"符合的记录有：{names}。" if names else "")
    return "Could not do this part." + (f" Records that fit: {names}." if names else "")


__all__ = ["GoalTurnExecutor", "assistant_answer"]
