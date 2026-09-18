"""Application-neutral Business Skills and typed server-tool dispatch.

The server Agent owns this catalog.  A Skill is a bounded playbook, while the executor
supplies current application facts.  Tool schemas become visible only after the Agent loads
the matching Skill, which makes ``load_skill -> tool -> ToolResult`` an enforced transition
instead of a sentence in a prompt.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable
from urllib.parse import urlsplit

from .answering import StructuredQueryAnswer
from .evidence import RecordEvidence
from .identity import OperationContext, PrincipalError
from .mutation import (MemoryMutationJournal, MutationClaimError, MutationJournal,
                       MutationJournalDurability, MutationJournalError)
from .schemas import BROWSER_ACTIONS


_SKILL_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
#: `<kind>:<application>@<release>:<locator>[#<anchor>]`. Bounded on every part, because a
#: Skill artifact is untrusted input and a citation is shown to a reviewer.
_CITATION = re.compile(
    r"\A(?P<kind>src|api)"
    r":(?P<application>[a-z][a-z0-9_-]{1,39})"
    r"@(?P<release>[A-Za-z0-9][A-Za-z0-9_.+:-]{0,99})"
    r":(?P<locator>[^\x00-\x1f\x7f#]{1,400})"
    r"(?:#(?P<anchor>[^\x00-\x1f\x7f]{1,200}))?\Z")
_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|authorization)", re.I)
_MAX_RESULT_BYTES = 64 * 1024
BUSINESS_SKILL_MATCHER_VERSION = "business-skill-trigger-v2"
_EXPLICIT_TOOL_REFERENCE = re.compile(
    r"\bcall\s+`?([a-z][a-z0-9_]{1,63})`?", re.I)
_DATA_CLASS = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_FORM_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")


def _trigger_tokens(value: str) -> tuple[str, ...]:
    """Tokenize Latin words and individual Han characters for bounded phrase matching."""
    return tuple(re.findall(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]", value.casefold()))


def _trigger_matches(trigger: str, task: str, *, max_gap_tokens: int = 3) -> bool:
    """Match a Skill phrase with word boundaries and a small amount of natural-language fill."""
    wanted = _trigger_tokens(trigger)
    available = _trigger_tokens(task)
    if not wanted or not available:
        return False
    cursor = -1
    for position, token in enumerate(wanted):
        # A phrase can occur anywhere in the task. Only the distance between its own words
        # is bounded; otherwise a one-word domain trigger would inexplicably stop matching
        # near the end of an ordinary sentence.
        upper = (len(available) if position == 0
                 else min(len(available), cursor + max_gap_tokens + 2))
        try:
            cursor = available.index(token, cursor + 1, upper)
        except ValueError:
            return False
    return True


#: Han aspect and passive markers that turn the verb after them into a description of state.
#: English carries the same distinction in the word itself -- "confirmed orders" never matches
#: a "confirm order" trigger -- but "已确认销售订单" (orders that are already confirmed) matches
#: "确认订单" once the matcher allows a gap, and a question about confirmed orders became a
#: request to confirm one.
_STATIVE_MARKERS = frozenset({"已", "被"})


def trigger_requests_action(trigger: str, task: str, *, max_gap_tokens: int = 3) -> bool:
    """Whether `trigger` occurs in `task` as something asked for, not as a description.

    The same match as `_trigger_matches`, tried at every start, and a start is discarded when
    the token before it -- or the one before that, for 已经 -- marks the verb as already done
    or done to something. Selection keeps the plain matcher: a question about confirmed orders
    still loads the order Skills. Only the claim that the user asked for a change is narrowed.
    """
    wanted = _trigger_tokens(trigger)
    available = _trigger_tokens(task)
    if not wanted or not available:
        return False
    for start, token in enumerate(available):
        if token != wanted[0]:
            continue
        before = available[max(0, start - 2):start]
        if before and (before[-1] in _STATIVE_MARKERS or tuple(before) == ("已", "经")):
            continue
        cursor, matched = start, True
        for next_token in wanted[1:]:
            upper = min(len(available), cursor + max_gap_tokens + 2)
            try:
                cursor = available.index(next_token, cursor + 1, upper)
            except ValueError:
                matched = False
                break
        if matched:
            return True
    return False


#: Scripts whose letters are visually interchangeable with Latin ones. A name mixing Latin
#: with either is a homograph, not a language: "Аcme" with a Cyrillic А renders exactly like
#: "Acme". Han, Kana and Hangul mix with Latin in ordinary company names and are not listed.
_CONFUSABLE_WITH_LATIN = frozenset({"CYRILLIC", "GREEK"})


def _script_of(character: str) -> str:
    try:
        return unicodedata.name(character).split(" ", 1)[0]
    except ValueError:
        return ""


def business_text_key(value: str) -> str:
    """Fold one business identifier into the key two records are compared on.

    Applied to both sides of a duplicate check, so a record this product did not create
    still compares equal to a visually identical name. Lenient by design: it never raises.
    Use :func:`normalize_business_text` for text this product is about to write.
    """
    if not isinstance(value, str):
        return ""
    folded = unicodedata.normalize("NFKC", value)
    folded = "".join(character for character in folded
                     if unicodedata.category(character) not in {"Cc", "Cf"})
    return " ".join(folded.split()).casefold()


def normalize_business_text(value: object, *, field: str, maximum: int) -> str:
    """Return `value` as text safe to write and to compare, or refuse it.

    Three defects make two records look identical to a person and different to a byte
    comparison, so each is removed or refused here rather than at the duplicate check:
    compatibility forms (fullwidth, ligatures) are folded by NFKC; every run of whitespace,
    including NBSP and ideographic space, becomes one ordinary space; control and format
    characters -- zero-width spaces, bidirectional overrides, default-ignorables, all
    category ``Cc``/``Cf`` -- are refused outright. Category ``Zs`` is deliberately not
    refused: U+0020 belongs to it.

    A name that mixes Latin letters with Cyrillic or Greek ones is refused as a homograph.
    """
    if not isinstance(value, str):
        raise BusinessToolRequestError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    for character in normalized:
        if unicodedata.category(character) in {"Cc", "Cf"}:
            raise BusinessToolRequestError(
                f"{field} contains an invisible or directional control character")
    normalized = " ".join(normalized.split())
    if not normalized:
        raise BusinessToolRequestError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise BusinessToolRequestError(f"{field} must be 1 to {maximum} visible characters")
    scripts = {_script_of(character) for character in normalized if character.isalpha()}
    if "LATIN" in scripts and scripts & _CONFUSABLE_WITH_LATIN:
        raise BusinessToolRequestError(
            f"{field} mixes Latin letters with a visually identical alphabet")
    return normalized


class BusinessToolError(RuntimeError):
    """A safe, expected refusal or read failure that the Agent may reason about."""

    code = "business_tool"


class BusinessToolRequestError(BusinessToolError):
    """The requested tool call is invalid or is not permitted for this turn."""

    code = "business_request"


class BusinessToolIdentityError(BusinessToolError):
    """The application credential or bound principal cannot authorize the call."""

    code = "business_identity"


class BusinessToolAuthenticationError(BusinessToolIdentityError):
    """The delegated application credential is missing, expired, or rejected."""

    code = "business_authentication"


class BusinessToolPermissionError(BusinessToolError):
    """An authenticated actor is not permitted to perform this operation.

    Distinct from an identity or authentication failure: the credential works and names a
    real actor, and that actor's own authority refuses the operation. `layer` says who
    refused -- the framework's per-tool scope check before anything reached the application,
    or the application's own permission system after it did. A mutation adapter may raise
    this only when the application guarantees that the refused call made no business change;
    an uncertain or partially applied refusal is ``BusinessMutationIndeterminate`` instead.
    """

    code = "business_permission_denied"
    LAYERS = frozenset({"tool_authorization", "application"})

    def __init__(self, message: str, *, layer: str = "application",
                 required_scope: str = ""):
        super().__init__(message)
        if layer not in self.LAYERS:
            raise ValueError("permission refusal layer is invalid")
        self.layer = layer
        self.required_scope = (required_scope if isinstance(required_scope, str)
                               and len(required_scope) <= 100 else "")


class BusinessToolUnavailableError(BusinessToolError):
    """A valid read could not reach the application service."""

    code = "business_unavailable"


class BusinessToolDispatchError(BusinessToolUnavailableError):
    """A mutating request reached transport but returned no safe outcome."""

    code = "business_dispatch_unknown"


class BusinessToolContractError(BusinessToolError):
    """The application integration returned data outside its declared contract."""

    code = "business_contract"


class BusinessMutationBlockedError(BusinessToolError):
    """An unresolved or in-flight business key blocks another write."""

    code = "business_mutation_blocked"

    def __init__(self, message: str, *, reason: str, state: str = ""):
        super().__init__(message)
        self.reason = reason
        self.state = state


class BusinessMutationRefusedError(BusinessToolError):
    """A definite application refusal made before any write was dispatched.

    This is distinct from ``BusinessMutationBlockedError``: a refusal such as a
    duplicate record is a settled business answer, while a blocked mutation points
    at an earlier operation that still needs reconciliation.
    """

    code = "business_mutation_refused"

    def __init__(self, message: str, *, reason: str, subject: str = ""):
        super().__init__(message)
        if (not isinstance(reason, str) or not reason.strip()
                or len(reason) > 100):
            raise ValueError("business mutation refusal reason is invalid")
        if not isinstance(subject, str) or len(subject) > 200:
            raise ValueError("business mutation refusal subject is invalid")
        self.reason = reason.strip()
        #: The record the rule refused, when there is one. "This order has nothing to invoice"
        #: does not tell a reader which order, and a reader who asked about one order needs to
        #: see that order named in the answer to know the refusal is about what they asked.
        self.subject = subject.strip()


class BusinessMutationIndeterminate(RuntimeError):
    """A dispatched mutation may have landed and therefore must never be retried.

    An application raising this usually knows more than the message says. When an upstream
    acknowledgement returned a record identifier and only the readback then failed, that
    identifier settles the row with one authorized read; without it an operator has to scan
    the whole collection. `phase` and `record_id` carry that forward to the durable journal
    and the redacted audit ledger, and nowhere else.
    """

    def __init__(self, message: str, *, phase: str = "", record_id: str = ""):
        super().__init__(message)
        self.phase = phase if isinstance(phase, str) and len(phase) <= 64 else ""
        self.record_id = (record_id if isinstance(record_id, str) and len(record_id) <= 200
                          else "")

    @property
    def reconciliation(self) -> dict:
        """The bounded post-dispatch facts, or an empty mapping when there are none."""
        return {key: value for key, value in
                (("phase", self.phase), ("record_id", self.record_id)) if value}


@dataclass(frozen=True, slots=True)
class MutationPostcondition:
    """Application-owned verdict produced after one acknowledged mutation dispatch."""

    verified: bool
    code: str
    evidence: dict

    def __post_init__(self) -> None:
        if not isinstance(self.verified, bool):
            raise ValueError("postcondition verified must be a boolean")
        if (not isinstance(self.code, str) or not _DATA_CLASS.fullmatch(self.code)
                or not isinstance(self.evidence, dict)):
            raise ValueError("postcondition verdict is invalid")
        try:
            json.dumps(self.evidence, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("postcondition evidence must be finite JSON") from exc
        if _has_sensitive_key(self.evidence):
            raise ValueError("postcondition evidence may not contain credentials")


@runtime_checkable
class BusinessPostconditionVerifier(Protocol):
    """Verify an acknowledged mutation without granting or dispatching another write."""

    @property
    def supported_tools(self) -> frozenset[str]: ...

    def verify(self, name: str, args: dict, result: "BusinessToolResult", *,
               context: OperationContext | None = None) -> MutationPostcondition: ...


@runtime_checkable
class BusinessDataClassifier(Protocol):
    """Classify typed application data before it enters the Agent context."""

    @property
    def tool_names(self) -> frozenset[str]: ...

    def classify(self, name: str, result: "BusinessToolResult") -> tuple[str, ...]: ...


@runtime_checkable
class BusinessRouteValidator(Protocol):
    """Validate a route returned by an application tool before Agent use."""

    def validate_tool_route(self, route: str) -> str: ...


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value \
            or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{name} must be a non-empty list of strings")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class Citation:
    """One checkable place a Skill's claim came from.

    A Skill is a playbook a person wrote, and the only thing that keeps it honest is being
    able to go and look at what it was written from. Until this existed `provenance` was
    validated as "a non-empty list of strings", and four spellings had accumulated across
    nine artifacts -- ``odoo:18.0:path#anchor``, ``twenty/v2.39.0:path``,
    ``minimal-demo://contacts/v1`` and the sentence ``Odoo external API: /jsonrpc
    object.execute_kw``. Nothing could resolve any of them, so nothing could tell a citation
    that had rotted from one that never pointed anywhere.

    The grammar is::

        <kind>:<application>@<release>:<locator>[#<anchor>]

    ``src`` is a path inside the application's own source at that release -- the compiled
    ``SourceMap`` can resolve it, which is what makes citation rot detectable. ``api`` is an
    interface the adapter calls, checkable against the application's published contract.
    Those are the only two kinds because they are the only two things a Skill is ever
    written from.
    """

    kind: str
    application: str
    release: str
    locator: str
    anchor: str = ""

    @property
    def text(self) -> str:
        anchor = f"#{self.anchor}" if self.anchor else ""
        return f"{self.kind}:{self.application}@{self.release}:{self.locator}{anchor}"

    @classmethod
    def parse(cls, value: object) -> "Citation":
        if not isinstance(value, str) or not _CITATION.fullmatch(value):
            raise ValueError(
                "a provenance entry must read <kind>:<application>@<release>:<locator>"
                "[#<anchor>], where kind is src or api")
        kind, rest = value.split(":", 1)
        application, rest = rest.split("@", 1)
        release, locator = rest.split(":", 1)
        locator, _, anchor = locator.partition("#")
        if not locator:
            raise ValueError("a provenance entry needs a locator")
        if kind == "src" and (locator.startswith("/") or ".." in locator.split("/")):
            raise ValueError("a src citation must be a relative path inside the repository")
        if kind == "api" and not locator.startswith("/"):
            raise ValueError("an api citation must be a path on the application's origin")
        return cls(kind=kind, application=application, release=release,
                   locator=locator, anchor=anchor)


def _provenance(value: object) -> tuple[str, ...]:
    """Every entry must parse, and no entry may appear twice."""
    entries = _string_list(value, "provenance")
    for entry in entries:
        Citation.parse(entry)
    if len(set(entries)) != len(entries):
        raise ValueError("provenance entries must be unique")
    return entries


def _has_sensitive_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(_SENSITIVE_KEY.search(str(key)) or _has_sensitive_key(item)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_sensitive_key(item) for item in value)
    return False


@dataclass(frozen=True)
class BusinessSelectionOption:
    """One application-labelled record that may be offered for user disambiguation.

    The adapter owns the label because only it knows which fields are meaningful to the
    application's users.  ``record_ref`` is retained on the server as historical context;
    it grants no route or tool authority and must be refreshed before use.
    """

    record_ref: str
    label: str

    def __post_init__(self) -> None:
        for name, value, limit in (("record_ref", self.record_ref, 500),
                                   ("label", self.label, 200)):
            if (not isinstance(value, str) or not value.strip() or len(value.strip()) > limit
                    or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value)):
                raise ValueError(f"business selection {name} is invalid")
            object.__setattr__(self, name, value.strip())


@dataclass(frozen=True, slots=True)
class FormDraftField:
    """One application-owned form binding; the model cannot replace its value or target."""

    field: str
    label: str
    value: str
    control: str = "textbox"

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or not _FORM_FIELD.fullmatch(self.field):
            raise ValueError("form draft field identifier is invalid")
        if self.control not in {"textbox", "combobox", "select"}:
            raise ValueError("form draft control must be textbox, combobox or select")
        for name, value, maximum, allow_empty in (
                ("label", self.label, 120, False),
                ("value", self.value, 4_000, True)):
            if (not isinstance(value, str) or (not allow_empty and not value.strip())
                    or len(value) > maximum
                    or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value)):
                raise ValueError(f"form draft {name} is invalid")
        if _SENSITIVE_KEY.search(self.field):
            raise ValueError("form draft cannot bind a credential-shaped field")

    def to_json(self) -> dict:
        return {"field": self.field, "label": self.label,
                "value": self.value, "control": self.control}


@dataclass(frozen=True, slots=True)
class FormDraft:
    """A one-turn, non-persisting form preparation issued by an application adapter.

    ``transient_until_submit`` is deliberately the only persistence contract. An adapter
    must not issue this value for an autosaving form; such a draft is a write and belongs
    behind the ordinary mutation proposal and approval path.
    """

    route: str
    fields: tuple[FormDraftField, ...]
    persistence: str = "transient_until_submit"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.route) if isinstance(self.route, str) else None
        if not (parsed and self.route.startswith("/") and not self.route.startswith("//")
                and not parsed.scheme and not parsed.netloc and not parsed.fragment
                and len(self.route) <= 1_000 and "%" not in parsed.path
                and not re.search(r"[\\\x00-\x20\x7f]", self.route)
                and ".." not in parsed.path.split("/")):
            raise ValueError("form draft route must be a safe relative path")
        if self.persistence != "transient_until_submit":
            raise ValueError("form draft must remain transient until user submission")
        if (not isinstance(self.fields, tuple) or not 1 <= len(self.fields) <= 20
                or any(not isinstance(item, FormDraftField) for item in self.fields)
                or len({item.field for item in self.fields}) != len(self.fields)):
            raise ValueError("form draft must contain 1-20 unique fields")

    def to_json(self) -> dict:
        return {"route": self.route,
                "fields": [item.to_json() for item in self.fields],
                "persistence": self.persistence,
                "submission": "user_only"}

    @property
    def content_digest(self) -> str:
        raw = json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class BusinessToolResult:
    """Bounded runtime facts for the model plus a smaller audit projection."""

    content: dict
    audit: dict
    allowed_routes: tuple[str, ...] = ()
    query_answer: StructuredQueryAnswer | None = None
    data_classes: tuple[str, ...] = ()
    #: What this read established, in a form an answer can cite. Optional, and empty for an
    #: adapter that has not been taught to produce it: the projection above is a display
    #: view -- positional strings with no record identity, type or currency -- and an answer
    #: built on it can say what a table said, never what a reader asked.
    evidence: tuple = ()
    #: Current, application-labelled records that a server clarification may offer. These
    #: are data, not capabilities: selecting one never authorises a route or a write.
    selection_options: tuple[BusinessSelectionOption, ...] = ()
    #: Exact route, fields and values that the trusted application adapter permits the
    #: server to place in a non-persisting form. It is one-turn authority and never grants
    #: submission. The model can request its application but cannot alter its bindings.
    form_draft: FormDraft | None = None
    #: A read may identify exactly one record route for a navigation-only task. The route
    #: remains ordinary, adapter-issued, one-turn authority; this field merely lets the
    #: server take the already-determined reversible step without another model round trip.
    navigation_target: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.content, dict) or not isinstance(self.audit, dict):
            raise ValueError("business tool content and audit must be JSON objects")
        if _has_sensitive_key(self.content) or _has_sensitive_key(self.audit):
            raise ValueError("business tool results may not contain credential-shaped keys")
        try:
            encoded = json.dumps(
                self.content, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")
            json.dumps(self.audit, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("business tool results must be finite JSON") from exc
        if len(encoded) > _MAX_RESULT_BYTES:
            raise ValueError("business tool result exceeds 64 KiB")
        if not isinstance(self.allowed_routes, tuple) \
                or len(self.allowed_routes) != len(set(self.allowed_routes)):
            raise ValueError("business tool routes must be a unique tuple")
        if self.query_answer is not None and not isinstance(
                self.query_answer, StructuredQueryAnswer):
            raise ValueError("business query answer projection is invalid")
        if (not isinstance(self.data_classes, tuple)
                or len(self.data_classes) != len(set(self.data_classes))
                or any(not isinstance(item, str) or not _DATA_CLASS.fullmatch(item)
                       for item in self.data_classes)):
            raise ValueError("business tool data_classes must be unique identifiers")
        if (not isinstance(self.selection_options, tuple)
                or len(self.selection_options) > 50
                or any(not isinstance(item, BusinessSelectionOption)
                       for item in self.selection_options)
                or len({item.record_ref for item in self.selection_options})
                != len(self.selection_options)):
            raise ValueError(
                "business tool selection options must be at most 50 unique record references")
        if self.selection_options:
            evidence_record_refs = {
                item.record_id for item in self.evidence if isinstance(item, RecordEvidence)}
            if any(item.record_ref not in evidence_record_refs
                   for item in self.selection_options):
                raise ValueError(
                    "business selection options must reference records established by evidence")
        if self.form_draft is not None:
            if not isinstance(self.form_draft, FormDraft):
                raise ValueError("business form draft is invalid")
            if self.form_draft.route not in self.allowed_routes:
                raise ValueError("business form draft route must be an allowed route")
            if not _contains_exact_string(self.content, self.form_draft.route):
                raise ValueError("business form draft route must appear in its content")
            if any(not _contains_exact_value(self.content, item.to_json())
                   for item in self.form_draft.fields):
                raise ValueError(
                    "business form draft fields must appear exactly in its content")
        if not isinstance(self.navigation_target, str):
            raise ValueError("business navigation target must be a string")
        if self.navigation_target:
            if self.navigation_target not in self.allowed_routes:
                raise ValueError(
                    "business navigation target must be one of the allowed routes")
        option_bytes = json.dumps(
            [{"record_ref": item.record_ref, "label": item.label}
             for item in self.selection_options],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
        if len(encoded) + len(option_bytes) > _MAX_RESULT_BYTES:
            raise ValueError("business tool result exceeds 64 KiB")
        for route in self.allowed_routes:
            parsed = urlsplit(route) if isinstance(route, str) else None
            safe = bool(
                parsed and route.startswith("/") and not route.startswith("//")
                and not parsed.scheme and not parsed.netloc and not parsed.fragment
                and len(route) <= 1000 and "%" not in parsed.path
                and not re.search(r"[\\\x00-\x20\x7f]", route)
                and ".." not in parsed.path.split("/"))
            if not safe:
                raise ValueError("business tool routes must be safe relative paths")
            if not _contains_exact_string(self.content, route):
                raise ValueError("each business tool route must appear in its content")

    @property
    def content_digest(self) -> str:
        raw = json.dumps(self.content, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def render(self) -> str:
        return json.dumps(self.content, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)


def _contains_exact_string(value: object, target: str) -> bool:
    if isinstance(value, str):
        return value == target
    if isinstance(value, dict):
        return any(_contains_exact_string(item, target) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_exact_string(item, target) for item in value)
    return False


def _contains_exact_value(value: object, target: object) -> bool:
    if value == target:
        return True
    if isinstance(value, dict):
        return any(_contains_exact_value(item, target) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_exact_value(item, target) for item in value)
    return False


class BusinessToolExecutor(Protocol):
    def tool_schemas(self) -> tuple[dict, ...]: ...

    def tool_effects(self) -> dict[str, str]: ...

    def execute(self, name: str, args: dict, *,
                context: OperationContext | None = None) -> BusinessToolResult: ...

    def prepare_mutation(self, name: str, args: dict, *,
                         context: OperationContext | None = None
                         ) -> "BusinessMutationSpec": ...

    def execute_mutation(self, name: str, args: dict, *,
                         context: OperationContext | None = None
                         ) -> BusinessToolResult: ...


@dataclass(frozen=True)
class BusinessMutationSpec:
    """Executor-normalized effect shown to the user before any write is dispatched."""

    args: dict
    effect: str
    targets: tuple[str, ...]
    reconcile_key: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reconcile_key, str) or len(self.reconcile_key) > 1000:
            raise ValueError("mutation reconcile key must be a bounded string")
        if (not isinstance(self.args, dict)
                or any(not isinstance(key, str) for key in self.args)
                or _has_sensitive_key(self.args)):
            raise ValueError("mutation arguments must be a credential-free JSON object")
        if (not isinstance(self.effect, str) or not self.effect.strip()
                or len(self.effect.strip()) > 300):
            raise ValueError("mutation effect must be a non-empty string")
        if (not isinstance(self.targets, tuple) or not self.targets or len(self.targets) > 20
                or any(not isinstance(item, str) or not item.strip()
                       or len(item.strip()) > 300
                       for item in self.targets)):
            raise ValueError("mutation targets must be a non-empty tuple of strings")
        try:
            encoded = json.dumps(self.args, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("mutation arguments must be finite JSON") from exc
        if len(encoded.encode("utf-8")) > _MAX_RESULT_BYTES:
            raise ValueError("mutation arguments exceed 64 KiB")
        object.__setattr__(self, "args", json.loads(encoded))
        object.__setattr__(self, "effect", self.effect.strip())
        object.__setattr__(self, "targets", tuple(item.strip() for item in self.targets))


@dataclass(frozen=True)
class BusinessMutationProposal:
    """A single-use, digest-bound server mutation proposal."""

    tool: str
    args: dict
    effect: str
    targets: tuple[str, ...]
    nonce: str
    operation_context_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tool, str) or not _TOOL_NAME.fullmatch(self.tool):
            raise ValueError("mutation tool is invalid")
        validated = BusinessMutationSpec(self.args, self.effect, self.targets)
        object.__setattr__(self, "args", validated.args)
        object.__setattr__(self, "effect", validated.effect)
        object.__setattr__(self, "targets", validated.targets)
        if not isinstance(self.nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", self.nonce):
            raise ValueError("mutation nonce is invalid")
        if self.operation_context_digest and not re.fullmatch(
                r"[0-9a-f]{64}", self.operation_context_digest):
            raise ValueError("mutation operation context digest is invalid")

    @property
    def content_digest(self) -> str:
        raw = json.dumps({
            "schema_version": 1,
            "tool": self.tool,
            "args": self.args,
            "effect": self.effect,
            "targets": list(self.targets),
            "nonce": self.nonce,
            "operation_context_digest": self.operation_context_digest,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def redacted(self) -> dict:
        return {
            "schema_version": 1,
            "tool": self.tool,
            "argument_names": sorted(self.args),
            "target_count": len(self.targets),
            "digest": self.content_digest,
            "operation_context_digest": self.operation_context_digest,
        }


def _without_redundant_reads(skills: list) -> list:
    """Drop a read-only Skill whose tools a mutation Skill in the same set already carries.

    A mutation Skill usually carries the read tools its own preflight needs, so offering the
    narrower read-only Skill beside it adds a second, ambiguous route to a strict subset of
    the same tools. Unrelated read Skills stay: a mixed-intent task needs them.
    """
    mutation_toolsets = [set(skill.tool_names) for skill in skills if not skill.read_only]
    if not mutation_toolsets:
        return list(skills)
    return [skill for skill in skills
            if (not skill.read_only
                or not any(set(skill.tool_names) < tools for tools in mutation_toolsets))]


@dataclass(frozen=True)
class BusinessSkill:
    skill_id: str
    title: str
    intent: str
    triggers: tuple[str, ...]
    instructions: tuple[str, ...]
    tool_names: tuple[str, ...]
    provenance: tuple[str, ...]
    read_only: bool
    artifact_path: str
    artifact_sha256: str

    @classmethod
    def load(cls, path: str | Path) -> "BusinessSkill":
        artifact = Path(path)
        return cls.from_bytes(artifact.read_bytes(), artifact_uri=str(artifact))

    @classmethod
    def from_bytes(cls, raw: bytes, *, artifact_uri: str) -> "BusinessSkill":
        """Load an immutable Skill artifact without requiring a filesystem path.

        ``artifact_uri`` is a logical identity retained for diagnostics and provenance;
        callers may use schemes such as ``package://`` or ``memory://``.  The bytes are
        parsed immediately, so no temporary resource path needs to outlive this call.
        """
        if not isinstance(raw, bytes):
            raise TypeError("Business Skill content must be bytes")
        if (not isinstance(artifact_uri, str) or not artifact_uri.strip()
                or len(artifact_uri) > 2_000
                or re.search(r"[\x00-\x1f\x7f]", artifact_uri)):
            raise ValueError("Business Skill artifact_uri is invalid")
        artifact_uri = artifact_uri.strip()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid Business Skill JSON: {artifact_uri}") from exc
        required = {
            "schema_version", "skill_id", "title", "intent", "triggers",
            "instructions", "tool_names", "provenance", "read_only",
        }
        if not isinstance(data, dict) or set(data) != required or data.get("schema_version") != 1:
            raise ValueError("Business Skill fields or schema_version do not match schema 1")
        skill_id = data.get("skill_id")
        if not isinstance(skill_id, str) or not _SKILL_ID.fullmatch(skill_id):
            raise ValueError("skill_id is invalid")
        for name in ("title", "intent"):
            if not isinstance(data.get(name), str) or not data[name].strip():
                raise ValueError(f"{name} must be a non-empty string")
        tool_names = _string_list(data.get("tool_names"), "tool_names")
        if len(set(tool_names)) != len(tool_names) \
                or any(not _TOOL_NAME.fullmatch(name) for name in tool_names):
            raise ValueError("tool_names are invalid")
        if not isinstance(data.get("read_only"), bool):
            raise ValueError("read_only must be a boolean")
        return cls(
            skill_id=skill_id,
            title=data["title"].strip(),
            intent=data["intent"].strip(),
            triggers=_string_list(data.get("triggers"), "triggers"),
            instructions=_string_list(data.get("instructions"), "instructions"),
            tool_names=tool_names,
            provenance=_provenance(data.get("provenance")),
            read_only=data["read_only"],
            artifact_path=artifact_uri,
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
        )

    @property
    def artifact_uri(self) -> str:
        """Logical artifact identity; ``artifact_path`` remains as a compatibility name."""
        return self.artifact_path

    def candidate_card(self, *, loaded: bool = False) -> dict:
        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "intent": self.intent,
            "read_only": self.read_only,
            "tools": list(self.tool_names),
            "loaded": loaded,
        }

    def loaded_card(self) -> dict:
        return {
            **self.candidate_card(loaded=True),
            "instructions": list(self.instructions),
            "tools": list(self.tool_names),
            "provenance": list(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class BusinessSkillSelection:
    """Bounded result of matching user intent against one installed catalog."""

    matcher_version: str
    skills: tuple[BusinessSkill, ...]
    trigger_matches: tuple[str, ...]
    fallback_used: bool
    #: Trigger phrases matched, but none of what they matched could perform a change, so the
    #: catalog's mutation Skills were added to the candidates anyway. Recorded because a
    #: widened surface is a decision, and a decision nobody can see is how the previous
    #: behaviour went unnoticed.
    mutation_surface_widened: bool = False
    #: The task had no domain trigger, so an application-declared route-to-Skill hint was
    #: used. The page path is trusted only as routing context and never grants a tool scope.
    route_context_used: bool = False

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(skill.skill_id for skill in self.skills)


class BusinessToolRuntime:
    """Validate a Skill catalog and dispatch only tools from loaded relevant Skills."""

    def __init__(self, skills: tuple[BusinessSkill, ...], executor: BusinessToolExecutor,
                 *, application_id: str = "",
                 required_scopes: dict[str, str] | None = None,
                 mutation_journal: MutationJournal | None = None,
                 data_classifier: BusinessDataClassifier | None = None,
                 postcondition_verifier: BusinessPostconditionVerifier | None = None,
                 route_validator: BusinessRouteValidator | None = None,
                 route_skill_hints: tuple[tuple[str, str], ...] = (),
                 max_pending_mutations: int = 1_000):
        skill_ids = [skill.skill_id for skill in skills]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("Business Skill IDs must be unique")
        schemas: dict[str, dict] = {}
        for schema in executor.tool_schemas():
            if not isinstance(schema, dict) or schema.get("type") != "function":
                raise ValueError("business tool schema must have type=function")
            function = schema.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
                raise ValueError("business tool name is invalid")
            if name in schemas:
                raise ValueError("business tool names must be unique")
            if not isinstance(function.get("parameters"), dict):
                raise ValueError("business tool schema requires parameters")
            schemas[name] = schema
        declared = {name for skill in skills for name in skill.tool_names}
        if declared != set(schemas):
            raise ValueError("Business Skills and executor schemas must declare the same tools")
        effects = executor.tool_effects()
        if (not isinstance(effects, dict) or set(effects) != set(schemas)
                or any(value not in {"read", "write"} for value in effects.values())):
            raise ValueError("executor must declare one read/write effect for every tool")
        for skill in skills:
            if skill.read_only and any(effects[name] == "write" for name in skill.tool_names):
                raise ValueError("read-only Business Skills may not declare write tools")
        callable_tools = (set(schemas) | set(BROWSER_ACTIONS)
                          | {"finish", "load_skill", "prefill_form"})
        for skill in skills:
            references = {
                match.group(1) for instruction in skill.instructions
                for match in _EXPLICIT_TOOL_REFERENCE.finditer(instruction)
            }
            unavailable = references - callable_tools
            if unavailable:
                raise ValueError(
                    f"Business Skill {skill.skill_id} instructions reference unavailable "
                    f"tools: {sorted(unavailable)}")
            foreign_business = (references & set(schemas)) - set(skill.tool_names)
            if foreign_business:
                raise ValueError(
                    f"Business Skill {skill.skill_id} instructions reference business tools "
                    f"it does not declare: {sorted(foreign_business)}")
        if application_id and not re.fullmatch(r"[a-z][a-z0-9.-]{2,79}", application_id):
            raise ValueError("business tool application_id is invalid")
        scopes = dict(required_scopes or {})
        if scopes and set(scopes) != set(schemas):
            raise ValueError("required_scopes must declare every business tool")
        if any(not isinstance(scope, str) or not re.fullmatch(
                r"[a-z][a-z0-9:._-]{1,99}", scope) for scope in scopes.values()):
            raise ValueError("business tool required scope is invalid")
        if (isinstance(max_pending_mutations, bool)
                or not isinstance(max_pending_mutations, int)
                or not 1 <= max_pending_mutations <= 100_000):
            raise ValueError("max_pending_mutations must be an integer from 1 to 100000")
        self._skills = {skill.skill_id: skill for skill in skills}
        self._schemas = schemas
        self._effects = dict(effects)
        self._executor = executor
        if data_classifier is not None:
            if (not callable(getattr(data_classifier, "classify", None))
                    or getattr(data_classifier, "tool_names", None) != frozenset(schemas)):
                raise TypeError(
                    "data_classifier must classify every business tool")
        write_tools = frozenset(
            name for name, effect in effects.items() if effect == "write")
        if postcondition_verifier is not None:
            if (not callable(getattr(postcondition_verifier, "verify", None))
                    or getattr(postcondition_verifier, "supported_tools", None) != write_tools):
                raise TypeError(
                    "postcondition_verifier must verify every mutation tool")
        self._data_classifier = data_classifier
        self._postcondition_verifier = postcondition_verifier
        if route_validator is not None and not isinstance(
                route_validator, BusinessRouteValidator):
            raise TypeError("route_validator must provide validate_tool_route")
        self._route_validator = route_validator
        compiled_hints = []
        for pattern, skill_id in route_skill_hints:
            if (not isinstance(pattern, str) or not pattern.startswith("^")
                    or not pattern.endswith("$") or skill_id not in self._skills):
                raise ValueError("route Skill hints require anchored patterns and known Skills")
            if not self._skills[skill_id].read_only:
                raise ValueError("route Skill hints may name only read-only Skills")
            try:
                compiled_hints.append((re.compile(pattern), pattern, skill_id))
            except re.error as exc:
                raise ValueError("route Skill hint pattern is invalid") from exc
        self._route_skill_hints = tuple(compiled_hints)
        self.application_id = application_id
        self._required_scopes = scopes
        self._mutation_journal = (mutation_journal
                                  if mutation_journal is not None
                                  else MemoryMutationJournal())
        if any(not callable(getattr(self._mutation_journal, method, None))
               for method in ("register", "claim", "finish", "get")):
            raise TypeError("mutation_journal does not implement the journal contract")
        durability = getattr(self._mutation_journal, "durability", None)
        if not isinstance(durability, MutationJournalDurability):
            raise TypeError(
                "mutation_journal must declare a MutationJournalDurability")
        self._mutation_journal_durability = durability
        self._max_pending_mutations = max_pending_mutations
        self._mutation_lock = threading.RLock()
        self._prepared_mutations: dict[
            str, tuple[BusinessMutationProposal, int]] = {}

    @classmethod
    def from_paths(cls, paths: list[str | Path] | tuple[str | Path, ...],
                   executor: BusinessToolExecutor) -> "BusinessToolRuntime":
        return cls(tuple(BusinessSkill.load(path) for path in paths), executor)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._schemas)

    @property
    def has_mutations(self) -> bool:
        return "write" in self._effects.values()

    @property
    def mutation_tool_names(self) -> frozenset[str]:
        return frozenset(
            name for name, effect in self._effects.items() if effect == "write")

    @property
    def required_scopes(self) -> dict[str, str]:
        return dict(self._required_scopes)

    @property
    def mutation_journal_durability(self) -> MutationJournalDurability:
        return self._mutation_journal_durability

    @property
    def data_classifier(self) -> BusinessDataClassifier | None:
        return self._data_classifier

    @property
    def postcondition_verifier(self) -> BusinessPostconditionVerifier | None:
        return self._postcondition_verifier

    @property
    def route_validator(self) -> BusinessRouteValidator | None:
        return self._route_validator

    def _classify_result(self, name: str, result: BusinessToolResult) -> BusinessToolResult:
        if self._route_validator is not None:
            try:
                for route in result.allowed_routes:
                    self._route_validator.validate_tool_route(route)
            except Exception as exc:
                raise BusinessToolContractError(
                    "application returned a route outside its adapter contract") from exc
        if self._data_classifier is None:
            return result
        try:
            classes = self._data_classifier.classify(name, result)
        except BusinessToolError:
            raise
        except Exception as exc:
            raise BusinessToolContractError(
                "application data classification failed") from exc
        if (not isinstance(classes, tuple)
                or len(classes) != len(set(classes))
                or any(not isinstance(item, str) or not _DATA_CLASS.fullmatch(item)
                       for item in classes)):
            raise BusinessToolContractError(
                "application data classifier returned invalid classes")
        return replace(result, data_classes=classes)

    @property
    def catalog_manifest(self) -> dict:
        """Return a versioned, non-secret description of installed Skills and tools."""
        skills = [{
            "skill_id": skill.skill_id,
            "artifact_sha256": skill.artifact_sha256,
            "read_only": skill.read_only,
            "tools": list(skill.tool_names),
        } for skill in sorted(self._skills.values(), key=lambda item: item.skill_id)]
        tools = [{
            "name": name,
            "effect": self._effects[name],
            "required_scope": self._required_scopes.get(name, ""),
            "schema_sha256": hashlib.sha256(json.dumps(
                self._schemas[name], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest(),
        } for name in sorted(self._schemas)]
        route_hints = [{"pattern": pattern, "skill_id": skill_id}
                       for _compiled, pattern, skill_id in self._route_skill_hints]
        return {"schema_version": 1, "skills": skills, "tools": tools,
                "route_skill_hints": route_hints}

    @property
    def catalog_sha256(self) -> str:
        encoded = json.dumps(
            self.catalog_manifest, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def resolve_capability(self, goal: str, *, allow_mutations: bool,
                           effect: str = "write",
                           extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        """Every installed capability that could serve this goal, from the whole catalog.

        Deliberately not `relevant`/`select`. Those rank candidates to decide what to load,
        and a miss there is a ranking miss; read as an answer it would say the application
        cannot do something it can, which is the one error a capability gap must never make.
        So this searches every installed Skill, and falls back to the tool names themselves
        when no trigger matches -- a Skill whose triggers are all English still answers a
        Chinese request to use the tool it owns.

        `extra` carries the non-typed surfaces the session knows about (declarative page
        actions, form handoff) so a gap means the whole executable surface was searched.
        """
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        if effect not in ("write", "any"):
            raise ValueError("a capability search looks for write capabilities or any")
        folded = str(goal or "").casefold()
        if not folded.strip():
            raise ValueError("a capability search needs the goal it searched for")
        # Searching for a change means searching the capabilities that can make one. A read
        # Skill whose trigger is a bare noun matches any sentence containing that noun --
        # `customers-query` triggers on "客户" and so matches "删除客户" -- and counting it
        # would answer "you have a capability" when what this session has is the capability
        # to look at the thing it cannot change.
        available = [skill for skill in sorted(self._skills.values(),
                                               key=lambda item: item.skill_id)
                     if (skill.read_only or allow_mutations)
                     and (effect == "any" or not skill.read_only)]
        found: list[str] = []
        for skill in available:
            if any(_trigger_matches(trigger, folded) for trigger in skill.triggers):
                found.append(skill.skill_id)
                continue
            # A trigger list that missed does not retire the tools it owns.
            if any(name.replace("_", " ") in folded or name in folded
                   for name in skill.tool_names):
                found.append(skill.skill_id)
        found += [name for name in extra
                  if isinstance(name, str) and name.strip() and name not in found]
        return tuple(dict.fromkeys(found))

    def relevant(self, task: str, limit: int = 3) -> tuple[BusinessSkill, ...]:
        folded = task.casefold()
        ranked: list[tuple[int, str, BusinessSkill]] = []
        for skill in self._skills.values():
            score = sum(1 for trigger in skill.triggers
                        if _trigger_matches(trigger, folded))
            if score:
                ranked.append((-score, skill.skill_id, skill))
        selected = [row[2] for row in sorted(ranked)]
        # A mutation Skill often contains the read tools needed for duplicate checks and
        # verification.  In that case a broader read-only Skill adds a second, ambiguous
        # route to a strict subset of the same tools.  Remove only that redundant candidate;
        # unrelated read Skills remain available for mixed-intent tasks.
        return tuple(_without_redundant_reads(selected)[:limit])

    def capability_skills(self, *, allow_mutations: bool) -> tuple[BusinessSkill, ...]:
        """Return the installed user-facing capability catalog in stable order.

        This is descriptive only: exposing a mutation Skill here neither loads its tools nor
        grants approval.  Keeping the catalog on the runtime avoids teaching the generic
        session about an application's entities.
        """
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        return tuple(
            skill for skill in sorted(self._skills.values(), key=lambda item: item.skill_id)
            if skill.read_only or allow_mutations)

    def select(self, task: str, *, allow_mutations: bool,
               trusted_entry_path: str = "",
               limit: int = 5) -> BusinessSkillSelection:
        """Select task candidates and use a bounded capability-aware empty fallback.

        Trigger matching is the primary high-precision route. If it finds nothing eligible,
        expose read-only Skills plus mutation Skills only when this turn has a real approval
        path. This preserves the user-text-only injection boundary while preventing one missed
        paraphrase from making the application's entire typed tool surface disappear.
        """
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("Skill candidate limit must be an integer from 1 to 20")
        if trusted_entry_path and (
                not isinstance(trusted_entry_path, str)
                or not trusted_entry_path.startswith("/")
                or len(trusted_entry_path) > 1_000):
            raise ValueError("trusted_entry_path must be a bounded absolute path")
        matched = self.relevant(task, limit=max(limit, len(self._skills)))
        eligible = [skill for skill in matched if skill.read_only or allow_mutations]
        route_context_used = False
        if not eligible and trusted_entry_path:
            context_ids = []
            for pattern, _source, skill_id in self._route_skill_hints:
                if pattern.fullmatch(trusted_entry_path) and skill_id not in context_ids:
                    context_ids.append(skill_id)
            eligible = [self._skills[skill_id] for skill_id in context_ids]
            route_context_used = bool(eligible)
        # Trigger phrases rank the catalog; they do not decide what this turn is able to do.
        # A write trigger is a verb and its object ("confirm quotation", "确认报价单"); the
        # read trigger beside it is usually the object alone ("quotation", "报价单"). Every
        # sentence matching the write therefore matches the read, and none of the reverse.
        # When word order breaks the write phrase -- "把报价单 S00002 确认掉" puts the verb
        # last -- the read Skill still matches, `matched` is not empty, and the fallback below
        # never runs, because it only ever fired on *no* match. The turn is then left with no
        # write tool and refuses correctly for the wrong reason, which reads exactly like the
        # product holding its line. Measured on the Odoo catalog: 10 of 21 ordinary phrasings
        # reached the write Skill through triggers alone.
        #
        # The catalog itself says which write belongs to which read: a mutation Skill carries
        # the read tools its own preflight needs, so its tool set is a strict superset of the
        # narrower read-only Skill about the same subject. When triggers land on that read
        # Skill and this turn can write, offer both and let the model say which it meant.
        # Widening no further is the point -- "list every product" gains nothing, because
        # `products-query` has no write Skill extending it.
        widened = False
        if (allow_mutations and eligible and not route_context_used
                and all(skill.read_only for skill in eligible)):
            selected_tools = [set(skill.tool_names) for skill in eligible]
            present = {skill.skill_id for skill in eligible}
            # Subsuming *every* matched read Skill is what makes this safe. "Create a person
            # named Bob at Acme company" matches both people and companies, and company
            # creation extends only one of them -- so the sentence is about more than that
            # write, and nothing is added. A subject named alone, whose whole read surface one
            # mutation Skill already carries, is the only case triggers cannot resolve.
            addition = [
                skill for skill in sorted(self._skills.values(),
                                          key=lambda item: item.skill_id)
                if (not skill.read_only and skill.skill_id not in present
                    and all(tools < set(skill.tool_names) for tools in selected_tools))]
            widened = bool(addition)
            eligible += addition
        # Pruning the read-only Skill here would undo the widening: it was added on that
        # Skill's account, and the reader may well have meant the read.
        if not widened:
            eligible = _without_redundant_reads(eligible)
        if eligible:
            return BusinessSkillSelection(
                BUSINESS_SKILL_MATCHER_VERSION, tuple(eligible[:limit]),
                tuple(skill.skill_id for skill in matched), False,
                mutation_surface_widened=widened,
                route_context_used=route_context_used)

        fallback = [
            skill for skill in sorted(self._skills.values(), key=lambda item: item.skill_id)
            if skill.read_only or allow_mutations
        ]
        fallback = _without_redundant_reads(fallback)
        return BusinessSkillSelection(
            BUSINESS_SKILL_MATCHER_VERSION, tuple(fallback[:limit]),
            tuple(skill.skill_id for skill in matched), bool(fallback))

    def load(self, skill_id: object, allowed_ids: set[str]) -> tuple[BusinessSkill | None, str]:
        if not isinstance(skill_id, str) or skill_id not in allowed_ids:
            return None, "skill_id was not selected for this task"
        skill = self._skills.get(skill_id)
        if skill is None:
            return None, "skill_id is no longer in the server catalog"
        return skill, ""

    def schemas_for(self, loaded_ids: set[str]) -> list[dict]:
        names = {name for skill_id in loaded_ids
                 for name in self._skills[skill_id].tool_names}
        return [self._schemas[name] for name in sorted(names)]

    def is_mutation(self, name: str) -> bool:
        return self._effects.get(name) == "write"

    def _forget_mutation(self, digest: str) -> None:
        with self._mutation_lock:
            self._prepared_mutations.pop(digest, None)

    def _validate_call(self, name: str, args: object,
                       loaded_ids: set[str],
                       context: OperationContext | None = None) -> dict:
        if name not in self._schemas:
            raise BusinessToolRequestError("unknown business tool")
        allowed = {tool for skill_id in loaded_ids
                   for tool in self._skills[skill_id].tool_names}
        if name not in allowed:
            raise BusinessToolRequestError(
                "load the matching Business Skill before calling this tool")
        if not isinstance(args, dict):
            raise BusinessToolRequestError("business tool arguments must be an object")
        if context is not None:
            if not isinstance(context, OperationContext):
                raise BusinessToolIdentityError(
                    "business tool operation context is invalid")
            try:
                context.require_live(application_id=self.application_id or None)
            except PrincipalError as exc:
                raise BusinessToolIdentityError(str(exc)) from exc
            # A live principal that lacks the scope is an authorization answer, not a broken
            # identity. Checked separately so the two never have to be told apart by text.
            required_scope = self._required_scopes.get(name)
            if required_scope and required_scope not in context.principal.scopes:
                raise BusinessToolPermissionError(
                    "the current actor is not permitted to use this business tool",
                    layer="tool_authorization", required_scope=required_scope)
        return args

    def execute(self, name: str, args: object,
                loaded_ids: set[str], *,
                context: OperationContext | None = None) -> BusinessToolResult:
        args = self._validate_call(name, args, loaded_ids, context)
        if self.is_mutation(name):
            raise BusinessToolRequestError(
                "business mutation requires explicit approval")
        try:
            result = (self._executor.execute(name, args)
                      if context is None
                      else self._executor.execute(name, args, context=context))
        except BusinessToolError:
            raise
        except Exception as exc:
            raise BusinessToolUnavailableError(
                f"business tool failed: {type(exc).__name__}") from exc
        if not isinstance(result, BusinessToolResult):
            raise BusinessToolContractError(
                "business tool returned an invalid result")
        return self._classify_result(name, result)

    def prepare_mutation(self, name: str, args: object,
                         loaded_ids: set[str], *,
                         context: OperationContext | None = None
                         ) -> BusinessMutationProposal:
        args = self._validate_call(name, args, loaded_ids, context)
        if not self.is_mutation(name):
            raise BusinessToolRequestError("business tool is read-only")
        try:
            spec = (self._executor.prepare_mutation(name, args)
                    if context is None
                    else self._executor.prepare_mutation(
                        name, args, context=context))
        except BusinessToolError:
            raise
        except Exception as exc:
            raise BusinessToolUnavailableError(
                f"business mutation preparation failed: {type(exc).__name__}") from exc
        if not isinstance(spec, BusinessMutationSpec):
            raise BusinessToolContractError(
                "business mutation preparation returned an invalid result")
        now_ms = int(time.time() * 1000)
        proposal = BusinessMutationProposal(
            tool=name, args=spec.args, effect=spec.effect, targets=spec.targets,
            nonce=uuid.uuid4().hex,
            operation_context_digest=(context.binding_digest if context else ""))
        principal = context.principal if context is not None else None
        key_payload = [self.application_id, name,
                       principal.tenant_id if principal else "",
                       principal.workspace_id if principal else "",
                       principal.actor_id if principal else "",
                       spec.reconcile_key or spec.args]
        business_key = hashlib.sha256(json.dumps(
            key_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        expires_at = context.expires_at if context is not None else now_ms + 10 * 60 * 1000
        with self._mutation_lock:
            expired = [digest for digest, (_proposal, item_expires_at)
                       in self._prepared_mutations.items()
                       if item_expires_at <= now_ms]
            for digest in expired:
                self._prepared_mutations.pop(digest, None)
            if len(self._prepared_mutations) >= self._max_pending_mutations:
                raise BusinessToolError(
                    "too many business mutations are awaiting a decision")
            try:
                self._mutation_journal.register(
                    proposal.content_digest, proposal.operation_context_digest,
                    expires_at=expires_at, now_ms=now_ms, business_key=business_key,
                    reconciliation={"application_id": self.application_id, "tool": name,
                                    "args": spec.args,
                                    "identity": ({key: value for key, value in
                                        principal.audit_projection().items() if key in {
                                            "tenant_digest", "workspace_digest", "actor_digest"}}
                                        if principal else {})})
            except MutationJournalError as exc:
                raise BusinessToolError(str(exc)) from exc
            self._prepared_mutations[proposal.content_digest] = (proposal, expires_at)
        return proposal

    def execute_mutation(self, proposal: BusinessMutationProposal,
                         approved_digest: str,
                         loaded_ids: set[str], *,
                         context: OperationContext | None = None,
                         on_claimed: Callable[[], None] | None = None) -> BusinessToolResult:
        if not isinstance(proposal, BusinessMutationProposal):
            raise BusinessToolError("business mutation proposal is invalid")
        digest = proposal.content_digest
        with self._mutation_lock:
            prepared_entry = self._prepared_mutations.get(digest)
            prepared = prepared_entry[0] if prepared_entry is not None else None
            if prepared != proposal or approved_digest != digest:
                prior = self._mutation_journal.get(digest)
                if prepared is None and prior is not None and prior.state != "prepared":
                    raise BusinessMutationBlockedError(
                        f"business mutation proposal is already {prior.state}",
                        reason="already_consumed", state=prior.state)
                raise BusinessToolError(
                    "business mutation approval does not match the proposal")
            self._validate_call(proposal.tool, proposal.args, loaded_ids, context)
            context_digest = context.binding_digest if context is not None else ""
            if proposal.operation_context_digest != context_digest:
                raise BusinessToolError(
                    "business mutation belongs to another operation context")
            if not self.is_mutation(proposal.tool):
                raise BusinessToolError(
                    "business mutation proposal names a read-only tool")
            # Claim atomically before dispatch. A competing caller, process restart, timeout,
            # or malformed acknowledgement cannot make the same proposal eligible again.
            if on_claimed is not None and not callable(on_claimed):
                raise TypeError("on_claimed must be callable or None")
            try:
                self._mutation_journal.claim(
                    digest, proposal.operation_context_digest)
            except MutationClaimError as exc:
                raise BusinessMutationBlockedError(
                    str(exc), reason=exc.reason, state=exc.state) from exc
            except MutationJournalError as exc:
                raise BusinessToolError(str(exc)) from exc
            if on_claimed is not None:
                try:
                    on_claimed()
                except Exception as exc:
                    # Nothing reached the application yet.  Release the key with a definite
                    # failed outcome rather than leaving a false in-flight write behind.
                    try:
                        self._mutation_journal.finish(digest, "failed")
                    except MutationJournalError:
                        pass
                    self._forget_mutation(digest)
                    raise BusinessToolUnavailableError(
                        "mutation dispatch audit could not be recorded") from exc
        try:
            result = (self._executor.execute_mutation(proposal.tool, proposal.args)
                      if context is None
                      else self._executor.execute_mutation(
                          proposal.tool, proposal.args, context=context))
        except BusinessToolDispatchError as exc:
            try:
                self._mutation_journal.finish(
                    digest, "indeterminate", reconciliation={"phase": "dispatch"})
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                "business mutation transport returned no safe outcome",
                phase="dispatch") from exc
        except BusinessToolError:
            try:
                self._mutation_journal.finish(digest, "failed")
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise
        except BusinessMutationIndeterminate as exc:
            try:
                self._mutation_journal.finish(
                    digest, "indeterminate", reconciliation=exc.reconciliation)
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise
        except Exception as exc:
            try:
                self._mutation_journal.finish(
                    digest, "indeterminate", reconciliation={"phase": "dispatch"})
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                f"business mutation outcome is unknown: {type(exc).__name__}",
                phase="dispatch") from exc
        if not isinstance(result, BusinessToolResult):
            try:
                self._mutation_journal.finish(digest, "indeterminate")
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                "business mutation returned an invalid acknowledgement")
        try:
            result = self._classify_result(proposal.tool, result)
            if self._postcondition_verifier is not None:
                postcondition = self._postcondition_verifier.verify(
                    proposal.tool, proposal.args, result, context=context)
                if not isinstance(postcondition, MutationPostcondition):
                    raise BusinessToolContractError(
                        "application postcondition verifier returned an invalid verdict")
                if not postcondition.verified:
                    raise BusinessMutationIndeterminate(
                        "business mutation postcondition was not verified")
                result = replace(result, audit={
                    **result.audit,
                    "postcondition": {
                        "code": postcondition.code,
                        "verified": True,
                        "evidence": postcondition.evidence,
                    },
                })
        except BusinessMutationIndeterminate as exc:
            try:
                self._mutation_journal.finish(
                    digest, "indeterminate", reconciliation=exc.reconciliation)
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise
        except Exception as exc:
            try:
                self._mutation_journal.finish(
                    digest, "indeterminate", reconciliation={"phase": "postcondition"})
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                "business mutation postcondition verification failed",
                phase="postcondition") from exc
        try:
            self._mutation_journal.finish(
                digest, "completed", result_digest=result.content_digest)
        except MutationJournalError as exc:
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                "business mutation completed but its durable outcome was not recorded") from exc
        self._forget_mutation(digest)
        return result
