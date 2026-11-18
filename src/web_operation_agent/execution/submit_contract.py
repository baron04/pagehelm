"""Approval contract for a native form submit or row action.

The assistant fills (or finds) a native form or row control; it never builds the request.
This module owns what is shown to the person before the application sends it, and the
rules a request must satisfy to be the one that was shown:

* ``FormData``      -- the whole data bound to the live form, as the Runtime read it.
* ``diff_models``   -- what differs between the form as loaded and as about to be submitted.
* ``build_card``    -- target, every change (hidden fields, related ids, lines), defaults,
                       and the button, as an ordinary bounded mutation proposal.
* ``SubmitPermit``  -- admits the requests the click causes, each effect at most once.

It names no application, page or component. An adapter supplies identity, the write
interface catalogue and the same-actor record reads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import secrets
import threading
import time
from typing import Callable
from urllib.parse import parse_qsl

from ..business.tools.contracts import BusinessMutationProposal
from ..business.write_pipeline import (
    ProposalDiff,
    create_detail,
    derived_detail,
    normalize_approval_details,
    target_rule_detail,
)
from ..schemas.approval_details import ApprovalDetail

MAX_MODEL_BYTES = 200_000
MAX_CHANGES = 150
MODES = frozenset({"create", "update", "action"})
_TOKEN = re.compile(r"[0-9a-f]{32}")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?$")


@dataclass(frozen=True)
class SubmitConventions:
    """The application's record conventions, declared through its ``SubmitPort``.

    The core holds none of these itself: a port that cannot say what its records look
    like cannot submit. ``epoch_zone`` is the zone epoch-millisecond values are read
    in; ``None`` keeps epoch numbers opaque. ``decode_write`` maps an HTTP status and
    response body to the status a release counts as accepted and the scalar the
    application returned (the new record's identity on a create, else ``None``).
    """
    volatile_keys: frozenset       # record keys the application maintains itself
    record_key: str                # request body/query field naming the target record
    line_key: str                  # identity field of a line inside a collection
    name_key: str                  # record field that names a target on the card
    epoch_zone: tzinfo | None      # zone epoch-millisecond values are read in; None keeps them opaque
    decode_write: Callable         # (status: int | None, body: bytes) -> (status, answer)


class ContractRefused(RuntimeError):
    """A typed refusal; the code is stable, the detail never carries record values."""

    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(code)


# -- values ------------------------------------------------------------------------------
def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _decimal(value) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Decimal(f"{value:.15g}")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _datetime(value, *, zone=None) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and abs(value) >= 10 ** 11:
        if zone is None:
            return None
        try:
            return datetime.fromtimestamp(value / 1000, zone).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        found = _DATE.match(value.strip())
        if found:
            year, month, day, hour, minute, second = (int(part or 0) for part in found.groups())
            try:
                return datetime(year, month, day, hour, minute, second)
            except ValueError:
                return None
    return None


def scalar_text(value) -> str:
    """One normal form per scalar: null and "" agree, 5 and 5.0 agree, spacing is ignored. A
    string stays as written (so "007" is not "7"); only a number and a string are folded with
    each other, in `same_scalar`."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        text = value.strip()
        stamp = _datetime(text)
        if stamp is not None and len(text) > 10:
            return stamp.strftime("%Y-%m-%d %H:%M:%S")
        return text
    parsed = _decimal(value)
    return format(parsed.normalize(), "f") if parsed is not None else str(value)


def _plain_number(text: str) -> Decimal | None:
    return _decimal(text) if re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", text.strip()) else None


def same_scalar(left, right, *, zone=None) -> bool:
    if scalar_text(left) == scalar_text(right):
        return True
    # A number the page holds as text, or the other way round: "5" and 5, but not "007" and 7.
    if isinstance(left, str) != isinstance(right, str):
        text, number = (left, right) if isinstance(left, str) else (right, left)
        parsed = _plain_number(text)
        known = _decimal(number)
        if parsed is not None and known is not None and parsed == known:
            return True
    # A timestamp the page sends for a date the person sees, in the declared zone.
    first, second = _datetime(left, zone=zone), _datetime(right, zone=zone)
    if first is None or second is None:
        return False
    if isinstance(left, (int, float)) != isinstance(right, (int, float)):
        date_only = isinstance(left, str) and len(left.strip()) <= 10 or (
            isinstance(right, str) and len(right.strip()) <= 10)
        return first.date() == second.date() if date_only else first == second
    return False


def is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def same_value(left, right, *, zone=None) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        return all(same_value(left.get(key), right.get(key), zone=zone)
                   for key in set(left) | set(right))
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            same_value(a, b, zone=zone) for a, b in zip(left, right))
    if is_empty(left) and is_empty(right):
        return True
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return False
    return same_scalar(left, right, zone=zone)


def same_payload(left, right, *, zone=None):
    """Field presence is part of an authorized payload, including empty/nullable fields."""
    if isinstance(left, dict) or isinstance(right, dict):
        return isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys() and all(
            same_payload(left[k], right[k], zone=zone) for k in left)
    if isinstance(left, list) or isinstance(right, list):
        return isinstance(left, list) and isinstance(right, list) and len(left) == len(right) and all(
            same_payload(a, b, zone=zone) for a, b in zip(left, right))
    return same_value(left, right, zone=zone)


def strip_volatile(record, keys):
    if isinstance(record, dict):
        return {key: strip_volatile(value, keys) for key, value in record.items() if key not in keys}
    if isinstance(record, list):
        return [strip_volatile(item, keys) for item in record]
    return record


def jsonable(value, *, depth: int = 0):
    """A JSON-safe copy that refuses what cannot be one, rather than guessing."""
    if depth > 8:
        raise ContractRefused("form_data_unreadable", "the form data is nested too deeply")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and value != value:
            raise ContractRefused("form_data_unreadable", "the form data holds a number that is not")
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item, depth=depth + 1) for key, item in value.items()}
    raise ContractRefused("form_data_unreadable", "the form data holds a value that is not data")


# -- the form as read from the page --------------------------------------------------------
@dataclass(frozen=True)
class FormItem:
    prop: str
    label: str = ""
    display: str = ""


@dataclass(frozen=True)
class FormData:
    """The data bound to the live form: the whole model, not only the controls on screen."""

    model: dict
    items: tuple[FormItem, ...] = ()
    mode: str = "update"
    record_id: str = ""
    title: str = ""

    @classmethod
    def from_json(cls, raw) -> "FormData":
        if not isinstance(raw, dict) or not isinstance(raw.get("model"), dict):
            raise ContractRefused("form_data_unreadable", "the Runtime returned no form data")
        model = jsonable(raw["model"])
        if len(canonical(model).encode("utf-8")) > MAX_MODEL_BYTES:
            raise ContractRefused("form_data_unreadable", "the form data is too large to show")
        mode = raw.get("mode")
        if mode not in {"create", "update"}:
            raise ContractRefused("form_data_unreadable", "the form is neither new nor an edit")
        items = tuple(FormItem(str(item.get("prop", ""))[:120], str(item.get("label", ""))[:120],
                               str(item.get("display", ""))[:200])
                      for item in raw.get("items") or () if isinstance(item, dict) and item.get("prop"))
        return cls(model, items[:400], mode, str(raw.get("record_id") or "")[:40],
                   str(raw.get("title") or "")[:120])

    @property
    def digest(self) -> str:
        return digest(self.model)

    def label(self, prop: str) -> str:
        for item in self.items:
            if item.prop == prop and item.label:
                return item.label
        return prop

    def display(self, prop: str) -> str:
        return next((item.display for item in self.items if item.prop == prop and item.display), "")


# -- what changed ------------------------------------------------------------------------
@dataclass(frozen=True)
class Change:
    path: tuple
    before: object
    after: object

    @property
    def field(self) -> str:
        return str(self.path[0])


def _line_key(items, key: str) -> list | None:
    keys = []
    for item in items:
        if not isinstance(item, dict) or is_empty(item.get(key)):
            return None
        keys.append(scalar_text(item[key]))
    return keys if len(set(keys)) == len(keys) else None


def diff_models(before, after, path: tuple = (), *, conventions: SubmitConventions) -> list[Change]:
    """Every field that differs, hidden ones included; lines pair by their declared key,
    else by position."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[Change] = []
        for key in sorted(set(before) | set(after), key=str):
            out += diff_models(before.get(key), after.get(key), path + (key,),
                             conventions=conventions)
        return out
    if isinstance(before, list) and isinstance(after, list):
        old, new = (_line_key(before, conventions.line_key),
                    _line_key(after, conventions.line_key))
        out = []
        if old is not None and new is not None:
            by_old, by_new = dict(zip(old, before)), dict(zip(new, after))
            for key in new:
                out += diff_models(by_old.get(key), by_new[key], path + (f"#{key}",),
                                 conventions=conventions)
            for key in old:
                if key not in by_new:
                    out.append(Change(path + (f"#{key}",), by_old[key], None))
            return out
        for index in range(max(len(before), len(after))):
            out += diff_models(before[index] if index < len(before) else None,
                               after[index] if index < len(after) else None, path + (index,),
                               conventions=conventions)
        return out
    if isinstance(before, dict) and before and after is None or isinstance(after, dict) and after and before is None:
        return [Change(path, before, after)]
    return [] if same_value(before, after, zone=conventions.epoch_zone) else [Change(path, before, after)]


def _show(value) -> str:
    if value is None or value == "":
        return "（空）"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        text = canonical(value)
        return text if len(text) <= 160 else text[:157] + "…"
    return str(value)[:160]


@dataclass(frozen=True)
class ActionRequest:
    method: str
    path: str
    body: dict


@dataclass(frozen=True)
class SubmitCard:
    mode: str                       # create | update | action
    page: str
    target: str                     # "新增" or the record's id and name
    record_id: str
    changes: tuple[Change, ...]     # requested by the person
    other: tuple[Change, ...]       # defaults and what the application linked on its own
    button: str
    form: FormData | None
    loaded: FormData | None
    unrequested: tuple[Change, ...] = ()
    record: dict | None = None
    restored: tuple[Change, ...] = ()
    action_request: ActionRequest | None = None
    value_sources: tuple[tuple[str, str, str], ...] = ()

    @property
    def changed_fields(self) -> frozenset:
        return frozenset(c.field for c in (*self.changes, *self.other, *self.unrequested))

    def _line(self, change: Change, labels: FormData | None) -> tuple[str, str]:
        name = labels.label(change.field) if labels else change.field
        rest = "".join(f"/{p}" for p in change.path[1:])
        shown = f"{name}{rest}"
        display = labels.display(change.field) if labels and len(change.path) == 1 else ""
        after = _show(change.after) + (f"（{display}）" if display and display != _show(change.after) else "")
        if self.mode == "create":
            return shown, after
        return shown, f"{_show(change.before)} → {after}"

    def details(self) -> tuple[ApprovalDetail, ...]:
        out = [target_rule_detail("目标", self.target[:300]),
               target_rule_detail("页面", self.page[:300] or "当前页面"),
               target_rule_detail("将要点的按钮", self.button[:200] or "（提交）")]
        if self.action_request is not None:
            out.append(target_rule_detail("动作接口", f"{self.action_request.method} {self.action_request.path}"))
        form = self.form
        for change in self.changes[:MAX_CHANGES]:
            label, text = self._line(change, form)
            out.append(create_detail(label[:200] or "字段", text[:2000]))
        for change in self.other[:MAX_CHANGES]:
            label, text = self._line(change, form)
            out.append(derived_detail(("默认或联动：" + label)[:200], text[:2000]))
        for change in self.unrequested[:MAX_CHANGES]:
            label, text = self._line(change, form)
            out.append(derived_detail(("用户没有要求：" + label)[:200], text[:2000]))
        for change in self.restored:
            label, _ = self._line(change, form)
            out.append(derived_detail(("页面打开时：" + label)[:200],
                f"改成了 {_show(change.before)}，已还原为 {_show(change.after)}"))
        for label, value, source in self.value_sources:
            if not source:
                out.append(derived_detail(("值来源：" + label)[:200],
                    f"{_show(value)}：没在你的话里找到这个值"))
        return normalize_approval_details(out)

    def effect(self) -> str:
        count = len(self.changes) + len(self.other) + len(self.unrequested)
        what = {"create": "新增", "update": "修改", "action": f"执行「{self.button}」"}[self.mode]
        text = f"{what}：{self.target}" if self.mode != "action" else f"{what}：{self.target}"
        return (text if self.mode == "action" else f"{text}，共 {count} 项变化")[:300]

    def proposal(self, *, context_digest: str = "") -> BusinessMutationProposal:
        tool = {"create": "native_form_create", "update": "native_form_update",
                "action": "native_row_action"}[self.mode]
        args = {"mode": self.mode, "record_id": self.record_id, "button": self.button,
                "page": self.page, "form_digest": self.form.digest if self.form else "",
                "loaded_digest": self.loaded.digest if self.loaded else "",
                "fields": sorted(self.changed_fields),
                "action_digest": digest([self.action_request.method, self.action_request.path, self.action_request.body]) if self.action_request else ""}
        diff = ProposalDiff.from_details((self.target[:300] or "当前表单",), self.details())
        return BusinessMutationProposal(
            tool=tool, args=args, effect=self.effect(), targets=diff.targets,
            nonce=secrets.token_hex(16), operation_context_digest=context_digest,
            details=diff.details())


def build_card(*, loaded: FormData | None, form: FormData | None, page: str, button: str,
               mode: str, conventions: SubmitConventions, target_name: str = "",
               record_id: str = "", requested: frozenset = frozenset(),
               record: dict | None = None, restored: tuple[Change, ...] = (),
               action_request: ActionRequest | None = None) -> SubmitCard:
    """The card for a form. `requested` names the top-level fields the person asked for;
    everything else that differs lands in its own list rather than being hidden."""
    if mode not in MODES:
        raise ContractRefused("form_data_unreadable", "unknown submit mode")
    if mode == "action":
        label = f"{target_name or record_id or '所选记录'}（编号 {record_id}）" if record_id else target_name
        key = conventions.record_key
        changes = tuple(diff_models({k: (record or {}).get(k) for k in action_request.body if k != key},
                                    {k: v for k, v in action_request.body.items() if k != key},
                                    conventions=conventions)) if action_request else ()
        return SubmitCard("action", page, label.strip(), record_id, changes, (), button, None, None,
                          action_request=action_request)
    if form is None or (mode == "update" and loaded is None):
        raise ContractRefused("form_data_unreadable", "the form was not read")
    if mode == "create":
        base = loaded.model if loaded is not None else {}
        changes = [c for c in diff_models({}, form.model, conventions=conventions)
                   if not is_empty(c.after)]
        initial = {c.path: c.after for c in diff_models({}, base, conventions=conventions)}
        requested_changes, other = [], []
        for change in changes:
            default = change.path in initial and same_value(
                initial[change.path], change.after, zone=conventions.epoch_zone)
            (other if default or (requested and change.field not in requested)
             else requested_changes).append(change)
        title = (loaded.title if loaded is not None and loaded.title else "").strip()
        target = title if title else "新增" + (f"（{page}）" if page else "")
        card = SubmitCard("create", page, target, "", tuple(requested_changes), tuple(other),
                          button, form, loaded)
    else:
        changes = diff_models(record if record is not None else loaded.model, form.model,
                              conventions=conventions)
        asked = [c for c in changes if not requested or c.field in requested]
        rest = [c for c in changes if requested and c.field not in requested]
        name = target_name or loaded.display(conventions.name_key) \
            or str(loaded.model.get(conventions.name_key) or "")
        target = f"编号 {record_id}" + (f"「{name}」" if name else "")
        card = SubmitCard("update", page, target, record_id, tuple(asked), (), button, form, loaded,
                          unrequested=tuple(rest), record=record, restored=restored)
    if len(card.changes) + len(card.other) + len(card.unrequested) + len(card.restored) > MAX_CHANGES:
        raise ContractRefused("card_too_large", "the change list is too long to show completely")
    if not (card.changes or card.other or card.unrequested):
        raise ContractRefused("no_change", "the form holds no change")
    return card


# -- the request that follows the click -----------------------------------------------------
@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str = ""
    sequence: int = 0


@dataclass
class LoggedRequest:
    sequence: int
    method: str
    path: str
    decision: str
    fields: tuple = ()
    body_digest: str = ""
    status: int | None = None
    #: What the application answered, when it is small: the id a create returns.
    answer: object = None
    #: The request as captured, kept only by a dry run (nothing is sent on).
    body: object = None
    #: Field names (never values) behind a refusal, so a mismatch can be classified.
    detail: tuple = ()
    #: The request as the page sent it, when it is small, and the digest of the record as the
    #: guard re-read it just before deciding: the material a judge needs to check the decision.
    sent: object = None
    reread_digest: str = ""


def _small_body(body: bytes):
    if not body or len(body) > MAX_MODEL_BYTES:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _json_body(body: bytes):
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ContractRefused("request_not_json") from None


def _id_of(path_query: dict, body, key: str):
    """The record id a request names. The body and the query string must not name two."""
    in_body = scalar_text(body[key]) if isinstance(body, dict) and not is_empty(body.get(key)) else ""
    in_query = scalar_text(path_query[key]) if not is_empty(path_query.get(key)) else ""
    if in_body and in_query and in_body != in_query:
        raise ContractRefused("target_mismatch")
    return in_body or in_query


@dataclass
class SubmitPermit:
    """What one approved click may cause. Everything not named here stays refused."""

    actor: str
    card: SubmitCard
    prefixes: tuple[str, ...]
    catalog: frozenset                   # {(METHOD, path)} of documented write interfaces
    conventions: SubmitConventions = field(kw_only=True)   # the adapter's declared record conventions
    baseline: object = None              # the record as read before the click (update/action)
    reread: Callable[[], object] | None = None
    reread_observer: Callable[[object], None] | None = None
    ttl_s: float = 30.0
    #: Judge every request exactly as if it were to be sent, record it, and send nothing.
    dry_run: bool = False
    clock: Callable[[], float] = time.monotonic
    permit_id: str = field(default_factory=lambda: secrets.token_hex(16))
    log: list = field(default_factory=list)
    duplicates: int = 0
    _seen: set = field(default_factory=set)
    _covered: set = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _decide: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self):
        self.expires = self.clock() + self.ttl_s

    def _refuse(self, method, path, code, body=b"", detail=()) -> Decision:
        with self._lock:
            sequence = len(self.log) + 1
            self.log.append(LoggedRequest(sequence, method, path[:200], code,
                                          body_digest=hashlib.sha256(body).hexdigest()[:16],
                                          detail=tuple(sorted(str(d) for d in detail))[:40],
                                          sent=_small_body(body)))
        return Decision(False, code, sequence)

    def admit(self, method: str, path: str, query: str, body: bytes) -> Decision:
        """Decide one write request caused by the click; record it either way.

        Decisions are serialised: two identical requests racing each other cannot both read
        the permit as unused.
        """
        with self._decide:
            return self._admit(method, path, query, body)

    def _admit(self, method: str, path: str, query: str, body: bytes) -> Decision:
        method = method.upper()
        if self.clock() > self.expires:
            return self._refuse(method, path, "permit_expired", body)
        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return self._refuse(method, path, "not_a_write", body)
        if not any(path.startswith(prefix) for prefix in self.prefixes) or (method, path) not in self.catalog:
            return self._refuse(method, path, "interface_not_for_target", body)
        pairs = dict(parse_qsl(query, keep_blank_values=True))
        conventions, zone = self.conventions, self.conventions.epoch_zone
        try:
            parsed = _json_body(body)
        except ContractRefused as exc:
            return self._refuse(method, path, exc.code, body)
        if parsed is not None and not isinstance(parsed, dict):
            return self._refuse(method, path, "request_not_object", body)
        card = self.card
        if card.mode in {"create", "update"} and method == "DELETE":
            return self._refuse(method, path, "interface_not_for_target", body)
        if set(pairs) - {conventions.record_key}:
            return self._refuse(method, path, "query_not_on_card", body,
                                set(pairs) - {conventions.record_key})
        try:
            target = _id_of(pairs, parsed, conventions.record_key)
        except ContractRefused as exc:
            return self._refuse(method, path, exc.code, body)
        fields = tuple(sorted(parsed)) if isinstance(parsed, dict) else ()
        if card.mode in {"update", "action"} and target != card.record_id:
            return self._refuse(method, path, "target_mismatch", body)
        if card.mode == "create" and target:
            return self._refuse(method, path, "target_mismatch", body)
        # A create is one effect: a second create to the same interface is a second record, so
        # it is a repeat whatever else differs in its body.
        key = hashlib.sha256(canonical([method, path, sorted(pairs.items())] + (
            [] if card.mode == "create" else [parsed])).encode()).hexdigest()
        with self._lock:
            if key in self._seen:
                self.duplicates += 1
                sequence = len(self.log) + 1
                self.log.append(LoggedRequest(sequence, method, path[:200], "duplicate", fields, key[:16]))
                return Decision(False, "duplicate", sequence)
        covered, reread_digest = set(), ""
        if card.mode == "action":
            expected = card.action_request
            if expected is None or method != expected.method or path != expected.path:
                return self._refuse(method, path, "action_not_on_card", body)
            if not same_payload(parsed, expected.body, zone=zone):
                return self._refuse(method, path, "action_body_not_on_card", body)
            covered = set(card.changed_fields)
        if card.mode in {"create", "update"}:
            if parsed is None or card.form is None:
                return self._refuse(method, path, "body_differs_from_form", body)
            form = card.form.model
            # The record key is addressing — it names the card's target like the query's id,
            # whatever the application calls it — so it is never itself a change.
            sent = {k: v for k, v in parsed.items()
                    if k not in conventions.volatile_keys and k != conventions.record_key}
            differs = [k for k, v in sent.items()
                       if k not in form or not same_payload(v, form[k], zone=zone)]
            if differs:
                return self._refuse(method, path, "body_differs_from_form", body, differs)
            if card.mode == "update":
                base = self.baseline if isinstance(self.baseline, dict) else {}
                loaded = card.loaded.model if card.loaded is not None else {}

                def before(key, value):
                    # What changed is judged against the form as it was loaded: that is what the
                    # card was drawn against. The record and the form shape the same data
                    # differently (nulls and zeros, computed and display keys, line layout), so
                    # comparing the body to the record would call representation a change. The
                    # record's own unchanged-ness is checked on its own terms, before the click and
                    # again before release.
                    return card.record.get(key) if card.record is not None else (loaded.get(key) if card.loaded is not None else base.get(key))
                moved = {k for k, v in sent.items() if not same_value(before(k, v), v, zone=zone)}
                # A plain value the record holds now, that the request would put something else
                # over without the card saying so, is someone else's change about to be undone.
                # An empty side (null for 0, "" for none) is the shapes' difference, not a change.
                overwritten = {k for k, v in sent.items()
                               if k not in card.changed_fields and not isinstance(v, (list, dict))
                               and not isinstance(base.get(k), (list, dict)) and not is_empty(v)
                               and not is_empty(base.get(k)) and not same_value(base.get(k), v, zone=zone)}
                if not (moved & card.changed_fields):
                    # A write that carries none of what the card shows is not the card's.
                    return self._refuse(method, path, "request_without_card_change", body)
                if not moved <= card.changed_fields or overwritten:
                    return self._refuse(method, path, "change_not_on_card", body,
                                        (moved - card.changed_fields) | overwritten)
                covered = moved
            else:
                covered = {k for k, v in sent.items() if not is_empty(v)} & card.changed_fields
                if not covered:
                    return self._refuse(method, path, "request_without_card_change", body)
        if card.mode in {"update", "action"} and self.reread is not None:
            try:
                current = self.reread()
            except Exception:  # noqa: BLE001 -- an unreadable record is not an unchanged one
                return self._refuse(method, path, "record_unreadable", body)
            if self.reread_observer is not None:
                self.reread_observer(current)
            released = self._covered | {conventions.record_key}
            held = {k: v for k, v in (strip_volatile(current, conventions.volatile_keys) or {}).items()
                    if k not in released} if isinstance(current, dict) else current
            kept = {k: v for k, v in (strip_volatile(self.baseline, conventions.volatile_keys) or {}).items()
                    if k not in released} if isinstance(self.baseline, dict) else self.baseline
            reread_digest = digest(strip_volatile(current, conventions.volatile_keys))[:16]
            if not same_value(held, kept, zone=zone):
                return self._refuse(method, path, "record_changed", body, ("reread:" + reread_digest,))
        with self._lock:
            self._seen.add(key)
            self._covered |= covered
            sequence = len(self.log) + 1
            if self.dry_run:
                self.log.append(LoggedRequest(sequence, method, path[:200], "would_allow", fields, key[:16],
                                              body=parsed, sent=_small_body(body), reread_digest=reread_digest))
                return Decision(False, "dry_run", sequence)
            self.log.append(LoggedRequest(sequence, method, path[:200], "allowed", fields, key[:16],
                                          sent=_small_body(body), reread_digest=reread_digest))
        return Decision(True, "", sequence)

    def complete(self) -> bool:
        """Whether the admitted requests together carry every change on the card."""
        if self.card.mode == "action":
            return any(item.decision == "allowed" for item in self.log)
        return self.card.changed_fields <= self._covered

    def allowed(self) -> list[LoggedRequest]:
        return [item for item in self.log if item.decision == "allowed"]

    def captured(self) -> list[LoggedRequest]:
        """What a dry run would have sent."""
        return [item for item in self.log if item.decision == "would_allow"]

    def blocked(self) -> list[LoggedRequest]:
        return [item for item in self.log if item.decision != "allowed"]

    def settle(self, sequence: int, status: int | None, response: bytes = b"") -> None:
        status, answer = self.conventions.decode_write(status, response)
        with self._lock:
            for item in self.log:
                if item.sequence == sequence:
                    item.status, item.answer = status, answer

    def created_id(self) -> str:
        """The record id an accepted create answered with, if it named one."""
        for item in self.allowed():
            if item.status is not None and 200 <= item.status < 300 and isinstance(item.answer, (int, str)) \
                    and not isinstance(item.answer, bool) and str(item.answer).strip():
                return str(item.answer).strip()
        return ""


# -- terminal states ------------------------------------------------------------------------
def terminal_state(permit: SubmitPermit, *, readback_ok: bool | None, clicked: bool = True) -> str:
    """completed | partial | not_executed | failed | indeterminate (never retried)."""
    allowed = permit.allowed()
    accepted = [i for i in allowed if i.status is not None and 200 <= i.status < 300]
    unknown = [i for i in allowed if i.status is None]
    refused = [i for i in allowed if i.status is not None and not 200 <= i.status < 300]
    if not allowed:
        return "not_executed"
    if unknown:
        return "indeterminate"
    if refused and not accepted:
        return "failed"
    stray = [i for i in permit.blocked() if i.decision != "duplicate"]
    if accepted and (refused or stray or not permit.complete()):
        return "partial"
    if accepted and readback_ok is True and permit.complete():
        return "completed"
    return "indeterminate"
