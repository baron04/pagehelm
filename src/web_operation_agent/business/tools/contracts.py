"""Typed values and adapter protocols for application-owned business tools."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from ...answer.answering import StructuredQueryAnswer
from ...answer.evidence import RecordEvidence
from ...state.identity import OperationContext
from ...schemas.browser import MAX_FORM_LABEL_CHARS, FORM_INPUT_CONTROLS


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
_DATA_CLASS = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_ENTITY_CLASS = re.compile(r"^[a-z][a-z0-9_.]{1,63}$")

#: What a business tool may do to one entity class. A deployment's policy is written in
#: this vocabulary rather than against tool names, so it keeps meaning when an application
#: renames a tool, and an application that invents an operation is refused rather than
#: silently classified as something milder.
TOOL_OPERATIONS = frozenset({
    "read", "create", "update", "archive", "delete", "state_change", "issue_document",
})


@dataclass(frozen=True, slots=True)
class ToolOperation:
    """The entity a tool acts on and what it does to it."""

    entity: str
    operation: str

    def __post_init__(self) -> None:
        if not isinstance(self.entity, str) or not _ENTITY_CLASS.fullmatch(self.entity):
            raise ValueError("tool operation entity is invalid")
        if self.operation not in TOOL_OPERATIONS:
            raise ValueError(f"tool operation must be one of {sorted(TOOL_OPERATIONS)}")

    @property
    def reads_only(self) -> bool:
        return self.operation == "read"


@dataclass(frozen=True, slots=True)
class ForbiddenOperation:
    """An effect class this deployment refuses to install, whatever a Skill declares.

    `entity` is one entity class or `*` for every one. The rule is checked when the catalog
    is built, so a forbidden capability cannot reach a session at all -- it is not a
    run-time refusal the model could retry against.
    """

    operation: str
    entity: str = "*"
    reason: str = ""

    def __post_init__(self) -> None:
        if self.operation not in TOOL_OPERATIONS:
            raise ValueError(f"forbidden operation must be one of {sorted(TOOL_OPERATIONS)}")
        if self.entity != "*" and not (
                isinstance(self.entity, str) and _ENTITY_CLASS.fullmatch(self.entity)):
            raise ValueError("forbidden operation entity is invalid")
        if not isinstance(self.reason, str) or len(self.reason) > 200:
            raise ValueError("forbidden operation reason must be short text")

    def matches(self, operation: ToolOperation) -> bool:
        return (self.operation == operation.operation
                and self.entity in {"*", operation.entity})

    def __str__(self) -> str:
        where = "any entity" if self.entity == "*" else self.entity
        return f"{self.operation} on {where}" + (f" ({self.reason})" if self.reason else "")
_FORM_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")


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
    nine artifacts -- ``sample:18.0:path#anchor``, ``sample/v2:path``,
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
    binding: str = "field"
    option_label: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or not _FORM_FIELD.fullmatch(self.field):
            raise ValueError("form draft field identifier is invalid")
        if self.control not in FORM_INPUT_CONTROLS:
            raise ValueError("form draft control is not supported")
        if self.binding not in {"field", "label"}:
            raise ValueError("form draft binding must be field or label")
        if (not isinstance(self.option_label, str) or len(self.option_label) > MAX_FORM_LABEL_CHARS
                or self.option_label and (self.control != "combobox"
                    or not self.option_label.strip()
                    or re.search(r"[\x00-\x1f\x7f]", self.option_label))):
            raise ValueError("form draft option label is invalid")
        for name, value, maximum, allow_empty in (
                ("label", self.label, MAX_FORM_LABEL_CHARS, False),
                ("value", self.value, 4_000, True)):
            if (not isinstance(value, str) or (not allow_empty and not value.strip())
                    or len(value) > maximum
                    or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value)):
                raise ValueError(f"form draft {name} is invalid")
        if _SENSITIVE_KEY.search(self.field):
            raise ValueError("form draft cannot bind a credential-shaped field")

    def to_json(self) -> dict:
        return {"field": self.field, "label": self.label,
                "value": self.value, "control": self.control,
                **({"binding": self.binding} if self.binding != "field" else {}),
                **({"option_label": self.option_label} if self.option_label else {})}


#: A form the application leaves alone until its own submit control is used.
TRANSIENT_UNTIL_SUBMIT = "transient_until_submit"
#: A form the application itself commits when the page is left -- Odoo 18 does this to an
#: open record form. Filling one is a change to an existing record in all but the last
#: click, so it goes behind the same visible confirmation as a write (owner ruling: the
#: confirmation is what makes it safe, and refusing the whole form handoff over it would
#: take away the one shape of change the person completes themselves).
APPLICATION_SAVES_ON_LEAVE = "application_saves_on_leave"
FORM_PERSISTENCE = (TRANSIENT_UNTIL_SUBMIT, APPLICATION_SAVES_ON_LEAVE)


@dataclass(frozen=True, slots=True)
class FormDraft:
    """A one-turn form preparation issued by an application adapter.

    The server fills exactly the fields the adapter names and never submits. Two
    persistence contracts exist, and they differ in what the person is asked first: a form
    that stays transient until submission is filled straight away, while one the
    application saves on leaving carries the effect and targets of the change it stages and
    is shown as a confirmation before anything is typed.
    """

    route: str
    fields: tuple[FormDraftField, ...]
    persistence: str = TRANSIENT_UNTIL_SUBMIT
    #: For a draft the application may save: what the person is agreeing to, in their own
    #: words, and the records it names. Written by the adapter, never by the model.
    effect: str = ""
    targets: tuple[str, ...] = ()
    control_scope: str = ""
    submit_label: str = ""

    def __post_init__(self) -> None:
        parsed = urlsplit(self.route) if isinstance(self.route, str) else None
        if not (parsed and self.route.startswith("/") and not self.route.startswith("//")
                and not parsed.scheme and not parsed.netloc and not parsed.fragment
                and len(self.route) <= 1_000 and "%" not in parsed.path
                and not re.search(r"[\\\x00-\x20\x7f]", self.route)
                and ".." not in parsed.path.split("/")):
            raise ValueError("form draft route must be a safe relative path")
        if self.persistence not in FORM_PERSISTENCE:
            raise ValueError("form draft persistence is not one this server knows")
        if (not isinstance(self.fields, tuple) or not 1 <= len(self.fields) <= 20
                or any(not isinstance(item, FormDraftField) for item in self.fields)
                or len({item.field for item in self.fields}) != len(self.fields)):
            raise ValueError("form draft must contain 1-20 unique fields")
        if self.persistence == APPLICATION_SAVES_ON_LEAVE:
            # Without these the confirmation could not say what is being staged, and a
            # confirmation that cannot name its effect is not one.
            if (not isinstance(self.effect, str) or not self.effect.strip()
                    or len(self.effect) > 300):
                raise ValueError("a saving form draft must carry a 1-300 character effect")
            if (not isinstance(self.targets, tuple) or not 1 <= len(self.targets) <= 24
                    or any(not isinstance(item, str) or not item.strip()
                           or len(item) > 300 for item in self.targets)):
                raise ValueError("a saving form draft must name 1-24 targets")
        elif self.effect or self.targets:
            raise ValueError("a transient form draft states no effect or targets")
        validate_form_scope(self.control_scope, self.submit_label,
                            label_binding=any(item.binding == "label" for item in self.fields))
        if self.submit_label and self.persistence != TRANSIENT_UNTIL_SUBMIT:
            raise ValueError("submit highlighting requires a transient form")

    @property
    def needs_confirmation(self) -> bool:
        return self.persistence == APPLICATION_SAVES_ON_LEAVE

    def redacted(self) -> dict:
        """What the audit ledger keeps: the shape of the handoff, never the typed values."""
        return {"route": self.route, "persistence": self.persistence,
                "fields": [item.field for item in self.fields],
                "effect": self.effect, "targets": list(self.targets),
                **({"control_scope": self.control_scope} if self.control_scope else {}),
                **({"submit_label": self.submit_label} if self.submit_label else {})}

    def to_json(self) -> dict:
        return {"route": self.route,
                "fields": [item.to_json() for item in self.fields],
                "persistence": self.persistence,
                "submission": "user_only",
                **({"control_scope": self.control_scope} if self.control_scope else {}),
                **({"submit_label": self.submit_label} if self.submit_label else {}),
                **({"effect": self.effect, "targets": list(self.targets)}
                   if self.persistence == APPLICATION_SAVES_ON_LEAVE else {})}

    @property
    def content_digest(self) -> str:
        raw = json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def validate_form_scope(scope: str, submit_label: str, *, label_binding: bool) -> None:
    if not isinstance(scope, str) or (scope and not re.fullmatch(
            r"(?:form|dialog|dialog_labelledby|dialog_describedby):[A-Za-z][A-Za-z0-9_:.-]{0,199}", scope)):
        raise ValueError("form control scope is invalid")
    if (not isinstance(submit_label, str) or len(submit_label) > MAX_FORM_LABEL_CHARS
            or re.search(r"[\x00-\x1f\x7f]", submit_label)
            or (submit_label and not submit_label.strip())):
        raise ValueError("form submit label is invalid")
    if (label_binding or submit_label) and not scope:
        raise ValueError("label bindings and submit highlighting require a form control scope")


def matches_form_control(element, *, field: str, label: str, control: str,
                         binding: str, scope: str = "") -> bool:
    if element.role != control or (scope and element.attrs.get("form_scope") != scope):
        return False
    if binding == "label":
        return (not element.attrs.get("field")
                and element.attrs.get("field_label") == label)
    return element.attrs.get("field") == field


def form_control_unavailable(attrs: dict) -> bool:
    for name in ("disabled", "readonly", "read-only"):
        value = attrs.get(name)
        if value is True or (isinstance(value, str)
                and value.casefold() not in {"", "0", "false", "no", "off"}):
            return True
    return False


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
