"""Application-neutral discovery and bounded reads over actor-visible entities.

Adapters translate their framework metadata and read APIs into the immutable values in
this module.  The Agent sees only the three schemas below and opaque bindings minted from
the current catalog and field descriptors; it never supplies a table, model, endpoint,
selector, method, or native query expression.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

from ..answer.answering import StructuredQueryAnswer
from ..answer.evidence import ComputationEvidence, Origin, QueryEvidence, RecordEvidence
from ..state.identity import OperationContext
from .errors import (BusinessToolContractError, BusinessToolPermissionError,
                     BusinessToolRequestError)
from .tools.contracts import BusinessToolResult


DISCOVER_ENTITIES_TOOL = "discover_entities"
DESCRIBE_ENTITY_TOOL = "describe_entity"
QUERY_ENTITY_RECORDS_TOOL = "query_entity_records"
RESOLVE_RECORD_REFERENCE_TOOL = "resolve_record_reference"
ENTITY_READ_TOOLS = frozenset({
    DISCOVER_ENTITIES_TOOL, DESCRIBE_ENTITY_TOOL, QUERY_ENTITY_RECORDS_TOOL,
})

_ENTITY_ID = re.compile(r"entity_[0-9a-f]{32}\Z")
_FIELD_ID = re.compile(r"field_[0-9a-f]{32}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TOKENS = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]")
_SENSITIVE = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|"
    r"access[_-]?key|credential|signature|cookie|session)", re.I)
_KINDS = frozenset({
    "boolean", "integer", "number", "monetary", "text", "choice",
    "date", "datetime", "reference",
})
_TEXT_KINDS = frozenset({"text", "choice", "reference"})
_ORDERED_KINDS = frozenset({"integer", "number", "monetary", "date", "datetime"})
_OPERATORS = frozenset({"=", "!=", "in", "not_in", "contains", ">", ">=", "<", "<="})
_EVIDENCE_TYPES = frozenset({"text", "number", "boolean", "date", "moment"})
_ANSWER_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_DESCRIBED_FIELDS = 40
_MAX_QUERY_FIELDS = 8
_MAX_AGGREGATE_FIELDS = 2
_MAX_AGGREGATE_GROUPS = 200
_MAX_FILTERS = 8
_MAX_QUERY_ROWS = 20
_MAX_OFFSET = 10_000
_MAX_VALUE_CHARS = 300
_MAX_DIRECTORY_LABELS = 200
_DIRECTORY_LABEL_CHARS = 4_000
_DIRECTORY_CURSOR = re.compile(r"directory_[0-9a-f]{32}\Z")


DISCOVER_ENTITIES_SCHEMA = {
    "type": "function",
    "function": {
        "name": DISCOVER_ENTITIES_TOOL,
        "description": (
            "Search the signed-in actor's current application-provided catalog of readable "
            "business entities. Returns opaque entity IDs, not native resource authority. "
            "If the entity kind is unknown, omit query to browse a compact directory of "
            "current type labels before searching by a displayed label. Words inside a "
            "record name are not its type. Directory pages contain metadata only, no "
            "records; follow next_cursor when needed. Never invent a resource name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200,
                          "description": "Optional entity/page words; omit to browse type labels."},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_DIRECTORY_LABELS,
                          "description": "Search: 1–10, default 5. Directory: 1–200, default 200, bounded by text size."},
                "cursor": {"type": "string", "pattern": r"^directory_[0-9a-f]{32}$",
                           "description": "Directory next_cursor from this actor's unchanged catalog; omit query."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

DESCRIBE_ENTITY_SCHEMA = {
    "type": "function",
    "function": {
        "name": DESCRIBE_ENTITY_TOOL,
        "description": (
            "Describe safe readable fields for one opaque entity returned by "
            "discover_entities. Returns opaque field IDs for query_entity_records."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "pattern": r"^entity_[0-9a-f]{32}$"},
                "field_query": {"type": "string", "maxLength": 200,
                                "description": "Optional user-language field description."},
                "limit": {"type": "integer", "minimum": 1,
                          "maximum": _MAX_DESCRIBED_FIELDS,
                          "description": "Maximum fields; defaults to 20."},
            },
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
}

QUERY_ENTITY_RECORDS_SCHEMA = {
    "type": "function",
    "function": {
        "name": QUERY_ENTITY_RECORDS_TOOL,
        "description": (
            "Read current records for one described entity. Use only opaque entity and "
            "field IDs returned by the current discovery calls. Filters are combined by "
            "AND and ordering is fixed by the server. Reports exact matching and "
            "actor-visible totals; a record's ui_path, when present, is where it opens. "
            "For a related record, query its returned related_entity_id and exact ID "
            "before opening its own ui_path. Full-scope sums use aggregate_field_ids and "
            "remain separate by unit even when returned rows are limited."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "pattern": r"^entity_[0-9a-f]{32}$"},
                "field_ids": {
                    "type": "array", "minItems": 0, "maxItems": _MAX_QUERY_FIELDS,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": r"^field_[0-9a-f]{32}$"},
                    "description": "Fields to return, from describe_entity. Use [] for a count-only query; its exact count is returned as ready aggregate evidence.",
                },
                "filters": {
                    "type": "array", "maxItems": _MAX_FILTERS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "field_id": {"type": "string",
                                         "pattern": r"^field_[0-9a-f]{32}$"},
                            "operator": {"type": "string", "enum": sorted(_OPERATORS)},
                            "value": {},
                        },
                        "required": ["field_id", "operator", "value"],
                        "additionalProperties": False,
                    },
                    "description": "Zero to eight server-validated conditions, combined by AND.",
                },
                "aggregate_field_ids": {
                    "type": "array", "maxItems": _MAX_AGGREGATE_FIELDS,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": r"^field_[0-9a-f]{32}$"},
                    "description": (
                        "Optional application sums over all matching records, grouped by unit. "
                        "Only described summable number or monetary fields are accepted; "
                        "the returned row limit does not limit these sums."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_QUERY_ROWS,
                          "description": "Rows in this page; defaults to 10."},
                "offset": {"type": "integer", "minimum": 0, "maximum": _MAX_OFFSET,
                           "description": "Bounded page offset; defaults to 0."},
            },
            "required": ["entity_id", "field_ids"],
            "additionalProperties": False,
        },
    },
}

ENTITY_READ_SCHEMAS = (
    DISCOVER_ENTITIES_SCHEMA, DESCRIBE_ENTITY_SCHEMA, QUERY_ENTITY_RECORDS_SCHEMA,
)

RESOLVE_RECORD_REFERENCE_SCHEMA = {
    "type": "function",
    "function": {
        "name": RESOLVE_RECORD_REFERENCE_TOOL,
        "description": (
            "Find a named or numbered record in the signed-in actor's readable application "
            "entities. Give exactly one of entity_query (the entity kind) or entity_id "
            "(an opaque ID already returned by discovery or reference lookup), separately "
            "from the reference. Prefer entity_id once the right type is known; words "
            "inside a record's name are not its entity type. Copy the record name/number "
            "from the user or a current candidate without appending the entity kind. Returns "
            "bounded candidates, not proof that an absent record does not exist. Use a "
            "candidate's opaque entity ID for follow-up typed reads."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_query": {"type": "string", "minLength": 1, "maxLength": 120,
                                 "description": "Entity kind, such as customer or invoice."},
                "entity_id": {"type": "string", "pattern": r"^entity_[0-9a-f]{32}$",
                              "description": "Current actor-bound type from discovery; no keyword reranking."},
                "reference": {"type": "string", "minLength": 1, "maxLength": 120,
                              "description": "A name or document number from the request."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["reference"],
            "oneOf": [{"required": ["entity_query"]}, {"required": ["entity_id"]}],
            "additionalProperties": False,
        },
    },
}


def _text(value: object, name: str, maximum: int, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum or _CONTROL.search(value)
            or (not empty and not value.strip())):
        raise ValueError(f"{name} must be bounded visible text")
    return " ".join(value.split())


def _request_text(value: object, name: str, maximum: int, *, empty: bool = False) -> str:
    try:
        return _text(value, name, maximum, empty=empty)
    except ValueError as exc:
        raise BusinessToolRequestError(str(exc)) from None


def _exact_args(args: object, allowed: set[str], name: str) -> dict:
    if not isinstance(args, dict) or set(args) - allowed:
        raise BusinessToolRequestError(f"{name} received unknown arguments")
    return args


def _bounded_int(value: object, *, name: str, default: int,
                 minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BusinessToolRequestError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _json_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _opaque(prefix: str, binding: str, reference: str) -> str:
    return prefix + hashlib.sha256(
        f"{binding}\0{reference}".encode("utf-8")).hexdigest()[:32]


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKENS.findall(value.casefold()))


def _score(query: str, text: str) -> int:
    wanted = _tokens(query)
    available = _tokens(text)
    if not wanted or not available:
        return 0
    counts = {token: available.count(token) for token in set(available)}
    overlap = sum(min(3, counts.get(token, 0)) for token in wanted)
    if not overlap:
        return 0
    phrase = 20 if query.casefold() in text.casefold() else 0
    coverage = (100 * len({token for token in wanted if token in counts})) // len(set(wanted))
    return phrase + coverage + overlap


@dataclass(frozen=True, slots=True)
class EntityPageHint:
    """One user-visible location that helped an adapter expose an entity."""

    name: str
    breadcrumbs: tuple[str, ...] = ()
    views: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "page hint name", 160))
        if (not isinstance(self.breadcrumbs, tuple) or len(self.breadcrumbs) > 10
                or not isinstance(self.views, tuple) or len(self.views) > 10):
            raise ValueError("entity page hints are invalid")
        object.__setattr__(self, "breadcrumbs", tuple(
            _text(item, "page hint breadcrumb", 240) for item in self.breadcrumbs))
        object.__setattr__(self, "views", tuple(
            _text(item, "page hint view", 40) for item in self.views))


@dataclass(frozen=True, slots=True)
class EntityDescriptor:
    """Adapter-private resource reference plus application-labelled discovery text."""

    reference: str
    label: str
    search_text: str
    page_hints: tuple[EntityPageHint, ...] = ()
    operations: tuple[str, ...] = ("read",)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference", _text(
            self.reference, "entity reference", 240))
        object.__setattr__(self, "label", _text(self.label, "entity label", 160))
        object.__setattr__(self, "search_text", _text(
            self.search_text, "entity search text", 2_000))
        if (not isinstance(self.page_hints, tuple) or len(self.page_hints) > 10
                or any(not isinstance(item, EntityPageHint) for item in self.page_hints)):
            raise ValueError("entity page hints must be a bounded tuple")
        if self.operations != ("read",):
            raise ValueError("the bounded entity contract currently permits reads only")


@dataclass(frozen=True, slots=True)
class EntityCatalog:
    """Current actor's adapter-filtered catalog and its invalidation binding."""

    binding_digest: str
    entities: tuple[EntityDescriptor, ...]
    complete: bool
    scope: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", self.binding_digest):
            raise ValueError("entity catalog binding_digest must be a SHA-256 digest")
        if (not isinstance(self.entities, tuple) or len(self.entities) > 10_000
                or any(not isinstance(item, EntityDescriptor) for item in self.entities)
                or len({item.reference for item in self.entities}) != len(self.entities)):
            raise ValueError("entity catalog contains invalid or repeated descriptors")
        if not isinstance(self.complete, bool):
            raise ValueError("entity catalog completeness must be boolean")
        object.__setattr__(self, "scope", _text(self.scope, "entity catalog scope", 300))


@dataclass(frozen=True, slots=True)
class EntityChoice:
    value: object
    label: str

    def __post_init__(self) -> None:
        if isinstance(self.value, (dict, list, tuple)) or self.value is None:
            raise ValueError("entity choice values must be non-null scalars")
        if isinstance(self.value, str):
            object.__setattr__(self, "value", _text(
                self.value, "entity choice value", _MAX_VALUE_CHARS, empty=True))
        try:
            json.dumps(self.value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("entity choice value must be finite JSON") from exc
        object.__setattr__(self, "label", _text(self.label, "entity choice label", 120))


@dataclass(frozen=True, slots=True)
class EntityField:
    """Safe adapter field. ``reference`` never crosses the Agent-facing boundary."""

    reference: str
    label: str
    kind: str
    required: bool = False
    readonly: bool = True
    searchable: bool = True
    summable: bool = False
    choices: tuple[EntityChoice, ...] = ()
    evidence_type: str = "text"
    # Adapter-private binding for a monetary field's denomination. It is never
    # accepted from the Agent or included in the Agent-facing field descriptor.
    unit_field_reference: str = ""
    related_entity_reference: str = ""
    # A fixed denomination declared by the application's schema (rather than a
    # per-record currency field). Never supplied by the model or inferred from a label.
    fixed_unit: str = ""

    def __post_init__(self) -> None:
        reference = _text(self.reference, "entity field reference", 240)
        label = _text(self.label, "entity field label", 160)
        if _SENSITIVE.search(reference) or _SENSITIVE.search(label):
            raise ValueError("credential-shaped entity fields are not allowed")
        object.__setattr__(self, "reference", reference)
        object.__setattr__(self, "label", label)
        if self.kind not in _KINDS:
            raise ValueError("entity field kind is not supported")
        if any(not isinstance(item, bool)
               for item in (self.required, self.readonly, self.searchable, self.summable)):
            raise ValueError("entity field flags must be boolean")
        if self.summable and self.kind not in {"integer", "number", "monetary"}:
            raise ValueError("only numeric fields may be summable")
        if (not isinstance(self.choices, tuple) or len(self.choices) > 50
                or any(not isinstance(item, EntityChoice) for item in self.choices)
                or len({_json_digest(item.value) for item in self.choices}) != len(self.choices)):
            raise ValueError("entity field choices are invalid or repeated")
        if self.choices and self.kind != "choice":
            raise ValueError("only choice fields may declare choices")
        if self.evidence_type not in _EVIDENCE_TYPES:
            raise ValueError("entity field evidence type is invalid")
        if self.unit_field_reference:
            if self.kind != "monetary":
                raise ValueError("only monetary fields may bind a unit field")
            object.__setattr__(self, "unit_field_reference", _text(
                self.unit_field_reference, "entity unit field reference", 240))
        if not isinstance(self.fixed_unit, str):
            raise ValueError("a fixed monetary unit must be text")
        if self.fixed_unit:
            if self.kind != "monetary" or self.unit_field_reference:
                raise ValueError("a fixed unit requires monetary values and no unit field")
            object.__setattr__(self, "fixed_unit", _text(self.fixed_unit, "fixed monetary unit", 80))
        if self.related_entity_reference:
            if self.kind != "reference":
                raise ValueError("only reference fields may bind a related entity")
            object.__setattr__(self, "related_entity_reference", _text(
                self.related_entity_reference, "related entity reference", 240))


@dataclass(frozen=True, slots=True)
class EntityFilter:
    field: EntityField
    operator: str
    value: object


@dataclass(frozen=True, slots=True)
class EntityQuery:
    entity: EntityDescriptor
    fields: tuple[EntityField, ...]
    filters: tuple[EntityFilter, ...]
    limit: int
    offset: int
    aggregate_fields: tuple[EntityField, ...] = ()


@dataclass(frozen=True, slots=True)
class EntityAggregate:
    """An application-computed full-scope sum for one field and denomination."""

    field_reference: str
    value: str
    count: int
    unit: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_reference", _text(
            self.field_reference, "aggregate field reference", 240))
        object.__setattr__(self, "value", _text(
            self.value, "aggregate value", 120))
        object.__setattr__(self, "unit", _text(
            self.unit, "aggregate unit", 80, empty=True))
        try:
            number = Decimal(self.value)
        except InvalidOperation as exc:
            raise ValueError("aggregate value must be a decimal") from exc
        if not number.is_finite():
            raise ValueError("aggregate value must be finite")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 0:
            raise ValueError("aggregate count must be nonnegative")


@dataclass(frozen=True, slots=True)
class EntityCell:
    field_reference: str
    value: str
    unit: str = ""
    related_record_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_reference", _text(
            self.field_reference, "entity cell field reference", 240))
        object.__setattr__(self, "value", _text(
            self.value, "entity cell value", _MAX_VALUE_CHARS, empty=True))
        object.__setattr__(self, "unit", _text(
            self.unit, "entity cell unit", 80, empty=True))
        object.__setattr__(self, "related_record_id", _text(
            self.related_record_id, "related record ID", 200, empty=True))


@dataclass(frozen=True, slots=True)
class EntityRecord:
    reference: str
    display_id: str
    cells: tuple[EntityCell, ...]
    #: Where this record opens in the application, when the application can say. A record
    #: found through the generic read otherwise had no way to be opened: the model made up
    #: an address, the server refused it, and "open this record" spent the whole turn on
    #: guesses. The route is checked against the application's route contract like any
    #: typed result's, and only then may `goto` use it.
    route: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference", _text(
            self.reference, "entity record reference", 400))
        object.__setattr__(self, "display_id", _text(
            self.display_id, "entity record display_id", 200))
        if not isinstance(self.route, str) or len(self.route) > 1000 or (
                self.route and (not self.route.startswith("/")
                                or self.route.startswith("//"))):
            raise ValueError("entity record route must be an application path")
        if (not isinstance(self.cells, tuple) or len(self.cells) > _MAX_QUERY_FIELDS
                or any(not isinstance(item, EntityCell) for item in self.cells)
                or len({item.field_reference for item in self.cells}) != len(self.cells)):
            raise ValueError("entity record cells are invalid or repeated")


@dataclass(frozen=True, slots=True)
class EntityQueryPage:
    """One exact, stably ordered application read returned under the current actor."""

    records: tuple[EntityRecord, ...]
    matched_count: int
    source_total: int
    source: str
    scope: str
    consistency: str = "count and page reads are not a transactional snapshot"
    aggregates: tuple[EntityAggregate, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.records, tuple) or len(self.records) > _MAX_QUERY_ROWS
                or any(not isinstance(item, EntityRecord) for item in self.records)
                or len({item.reference for item in self.records}) != len(self.records)
                or len({item.display_id for item in self.records}) != len(self.records)):
            raise ValueError("entity query records are invalid or repeated")
        for name, value in (("matched_count", self.matched_count),
                            ("source_total", self.source_total)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"entity query {name} must be a nonnegative integer")
        if self.matched_count > self.source_total or len(self.records) > self.matched_count:
            raise ValueError("entity query counts are inconsistent")
        if (not isinstance(self.aggregates, tuple)
                or len(self.aggregates) > _MAX_AGGREGATE_GROUPS
                or any(not isinstance(item, EntityAggregate) for item in self.aggregates)
                or len({(item.field_reference, item.unit) for item in self.aggregates})
                != len(self.aggregates)):
            raise ValueError("entity query aggregates are invalid or repeated")
        object.__setattr__(self, "source", _text(self.source, "entity query source", 200))
        object.__setattr__(self, "scope", _text(self.scope, "entity query scope", 200))
        object.__setattr__(self, "consistency", _text(
            self.consistency, "entity query consistency", 300))


@runtime_checkable
class EntityReadPort(Protocol):
    """Application translation boundary consumed by :class:`EntityReadTools`."""

    def catalog(self, *, context: OperationContext | None = None) -> EntityCatalog: ...

    def fields(self, entity: EntityDescriptor, *,
               context: OperationContext | None = None) -> tuple[EntityField, ...]: ...

    def query(self, request: EntityQuery, *,
              context: OperationContext | None = None) -> EntityQueryPage: ...


def _field_binding(entity_id: str, fields: tuple[EntityField, ...]) -> str:
    return _json_digest({
        "entity_id": entity_id,
        "fields": [{
            "reference": field.reference,
            "label": field.label,
            "kind": field.kind,
            "required": field.required,
            "readonly": field.readonly,
            "searchable": field.searchable,
            "summable": field.summable,
            "choices": [{"value": choice.value, "label": choice.label}
                        for choice in field.choices],
            "evidence_type": field.evidence_type,
            "unit_field_reference": field.unit_field_reference,
            **({"fixed_unit": field.fixed_unit} if field.fixed_unit else {}),
            "related_entity_reference": field.related_entity_reference,
        } for field in fields],
    })


def _field_score(query: str, field: EntityField) -> tuple[int, int, str]:
    priority = 0 if field.label.casefold() in {"id", "name", "display name"} else 1
    return (-_score(query, f"{field.label} {field.reference.replace('_', ' ')}"),
            priority, field.label.casefold())


def _answer_fields(fields: tuple[EntityField, ...]) -> tuple[str, ...]:
    """Derive display keys from labels without exposing native field references."""
    result = []
    used = {"record_id"}
    for index, field in enumerate(fields, start=1):
        candidate = re.sub(r"[^a-z0-9]+", "_", field.label.casefold()).strip("_")[:64]
        candidate = candidate if _ANSWER_FIELD.fullmatch(candidate) else ""
        if not candidate or candidate in used:
            candidate = f"field_{index}"
        while candidate in used:
            candidate += "_value"
        result.append(candidate)
        used.add(candidate)
    return tuple(result)


def _scalar(value: object, field: EntityField, operator: str) -> object:
    if field.kind == "boolean":
        if not isinstance(value, bool):
            raise BusinessToolRequestError(f"{field.label} requires a boolean")
        return value
    if field.kind == "integer" or (field.kind == "reference" and operator != "contains"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise BusinessToolRequestError(f"{field.label} requires an integer")
        return value
    if field.kind in {"number", "monetary"}:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            raise BusinessToolRequestError(f"{field.label} requires a finite number")
        return value
    if field.kind == "choice" and field.choices:
        if isinstance(value, (dict, list, tuple)) or value is None:
            raise BusinessToolRequestError(
                f"{field.label} requires a declared choice value")
        if isinstance(value, str):
            value = _request_text(
                value, field.label, _MAX_VALUE_CHARS, empty=True)
        try:
            selected = _json_digest(value)
        except (TypeError, ValueError):
            raise BusinessToolRequestError(
                f"{field.label} requires a declared choice value") from None
        if selected not in {_json_digest(item.value) for item in field.choices}:
            raise BusinessToolRequestError(
                f"{field.label} requires a declared choice value")
        return value
    if not isinstance(value, str) or len(value) > _MAX_VALUE_CHARS or _CONTROL.search(value):
        raise BusinessToolRequestError(f"{field.label} requires bounded text")
    if field.kind == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            raise BusinessToolRequestError(f"{field.label} requires YYYY-MM-DD") from None
    if field.kind == "datetime":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise BusinessToolRequestError(f"{field.label} requires an ISO datetime") from None
    return value


def _filter_value(value: object, field: EntityField, operator: str) -> object:
    if operator in {"in", "not_in"}:
        if not isinstance(value, list) or not 1 <= len(value) <= 50:
            raise BusinessToolRequestError(f"{operator} requires a list of 1 to 50 values")
        return [_scalar(item, field, "=") for item in value]
    return _scalar(value, field, operator)


class EntityReadTools:
    """The stable Agent surface over one application's :class:`EntityReadPort`."""

    def __init__(self, port: EntityReadPort, *, application_id: str):
        if not isinstance(port, EntityReadPort):
            raise TypeError("entity read port must implement catalog, fields and query")
        if not isinstance(application_id, str) or not application_id.strip():
            raise ValueError("entity read tools need an application_id")
        self._port = port
        self._application_id = application_id

    @staticmethod
    def tool_schemas() -> tuple[dict, ...]:
        return ENTITY_READ_SCHEMAS

    @staticmethod
    def tool_effects() -> dict[str, str]:
        return {name: "read" for name in ENTITY_READ_TOOLS}

    @staticmethod
    def _identity_audit(context: OperationContext | None) -> dict:
        if context is None:
            return {"identity_binding": "unverified"}
        return {"identity_binding": "principal_bound",
                "identity": context.audit_projection()}

    def _catalog(self, context: OperationContext | None) -> EntityCatalog:
        try:
            catalog = self._port.catalog(context=context)
        except (BusinessToolContractError, BusinessToolRequestError):
            raise
        except (TypeError, ValueError) as exc:
            raise BusinessToolContractError("the application returned an invalid entity catalog") \
                from exc
        if not isinstance(catalog, EntityCatalog):
            raise BusinessToolContractError("the application returned an invalid entity catalog")
        return catalog

    @staticmethod
    def _entities(catalog: EntityCatalog, context: OperationContext | None
                  ) -> tuple[tuple[str, EntityDescriptor], ...]:
        binding = catalog.binding_digest
        if context is not None:
            binding = _json_digest({
                "catalog": binding,
                "actor_authority": context.principal.actor_authority_digest,
            })
        bound = tuple((_opaque("entity_", binding, entity.reference), entity)
                      for entity in catalog.entities)
        if len({item[0] for item in bound}) != len(bound):
            raise BusinessToolContractError("entity identifiers collided")
        return bound

    def _entity(self, entity_id: object, context: OperationContext | None
                ) -> tuple[EntityCatalog, str, EntityDescriptor]:
        if not isinstance(entity_id, str) or not _ENTITY_ID.fullmatch(entity_id):
            raise BusinessToolRequestError("entity_id must come from discover_entities")
        catalog = self._catalog(context)
        found = next(((opaque, entity) for opaque, entity in self._entities(catalog, context)
                      if opaque == entity_id), None)
        if found is None:
            raise BusinessToolRequestError(
                "entity_id is absent or stale; discover the entity again")
        return catalog, found[0], found[1]

    def _fields(self, entity_id: str, entity: EntityDescriptor,
                context: OperationContext | None
                ) -> tuple[tuple[str, EntityField], ...]:
        try:
            fields = self._port.fields(entity, context=context)
        except (BusinessToolContractError, BusinessToolRequestError):
            raise
        except (TypeError, ValueError) as exc:
            raise BusinessToolContractError("the application returned invalid entity fields") \
                from exc
        if (not isinstance(fields, tuple) or len(fields) > 2_000
                or any(not isinstance(field, EntityField) for field in fields)
                or len({field.reference for field in fields}) != len(fields)):
            raise BusinessToolContractError("the application returned invalid entity fields")
        binding = _field_binding(entity_id, fields)
        bound = tuple((_opaque("field_", binding, field.reference), field) for field in fields)
        if len({item[0] for item in bound}) != len(bound):
            raise BusinessToolContractError("entity field identifiers collided")
        return bound

    def execute(self, name: str, args: dict, *,
                context: OperationContext | None = None) -> BusinessToolResult:
        if name == DISCOVER_ENTITIES_TOOL:
            return self._discover(args, context)
        if name == DESCRIBE_ENTITY_TOOL:
            return self._describe(args, context)
        if name == QUERY_ENTITY_RECORDS_TOOL:
            return self._query(args, context)
        raise BusinessToolRequestError("unknown entity read tool")

    def _discover(self, args: object,
                  context: OperationContext | None) -> BusinessToolResult:
        args = _exact_args(args, {"query", "limit", "cursor"}, DISCOVER_ENTITIES_TOOL)
        if "query" not in args:
            return self._directory(args, context)
        if "cursor" in args:
            raise BusinessToolRequestError("cursor is only for directory browsing without query")
        query = _request_text(args.get("query"), "query", 200)
        limit = _bounded_int(args.get("limit"), name="limit", default=5,
                             minimum=1, maximum=10)
        catalog = self._catalog(context)
        ranked = []
        for entity_id, entity in self._entities(catalog, context):
            page_text = " ".join(
                [hint.name for hint in entity.page_hints]
                + [crumb for hint in entity.page_hints for crumb in hint.breadcrumbs])
            score = _score(query, f"{entity.label} {entity.search_text} {page_text}")
            if score > 0:
                ranked.append((score, entity_id, entity))
        ranked.sort(key=lambda item: (-item[0], item[2].label.casefold(), item[1]))
        candidates = [{
            "entity_id": entity_id,
            "label": entity.label,
            "assistant_operations": list(entity.operations),
            "page_hints": [{
                "name": hint.name,
                "breadcrumbs": list(hint.breadcrumbs),
                "views": list(hint.views),
            } for hint in entity.page_hints[:5]],
            "match_score": score,
        } for score, entity_id, entity in ranked[:limit]]
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "query": query,
                "candidates": candidates,
                "matched_count": len(ranked),
                "returned_count": len(candidates),
                "catalog_complete": catalog.complete,
                "scope": catalog.scope,
                "observed_at": observed_at,
            },
            audit={
                "application": self._application_id,
                "operation": DISCOVER_ENTITIES_TOOL,
                "catalog_digest": catalog.binding_digest,
                "matched_count": len(ranked),
                "returned_count": len(candidates),
                "catalog_complete": catalog.complete,
                "observed_at": observed_at,
                **self._identity_audit(context),
            },
        )

    def _directory(self, args: dict,
                   context: OperationContext | None) -> BusinessToolResult:
        """Browse display vocabulary, without reading or selecting any business record.

        Labels need no native references or schema dump. Duplicated display labels remain
        ambiguous: a later ordinary search returns their distinct actor-bound IDs. The
        continuation binds the exact visible labels as well as the adapter's generation.
        """
        limit = _bounded_int(args.get("limit"), name="limit", default=_MAX_DIRECTORY_LABELS,
                             minimum=1, maximum=_MAX_DIRECTORY_LABELS)
        catalog = self._catalog(context)
        labels = sorted({entity.label for entity in catalog.entities},
                        key=lambda label: (label.casefold(), label))
        binding = _json_digest({
            "catalog": catalog.binding_digest, "labels": labels,
            "actor": (context.principal.actor_authority_digest if context else None),
        })
        def cursor_for(position: int) -> str:
            return _opaque("directory_", binding, str(position))
        start = 0
        if "cursor" in args:
            cursor = args["cursor"]
            if not isinstance(cursor, str) or not _DIRECTORY_CURSOR.fullmatch(cursor):
                raise BusinessToolRequestError("cursor must come from a directory next_cursor")
            found = next((index for index in range(1, len(labels))
                          if cursor_for(index) == cursor), None)
            if found is None:
                raise BusinessToolRequestError(
                    "directory cursor is absent or stale; browse again without cursor")
            start = found
        visible = []
        size = 2  # JSON array brackets; count the escaped JSON, not just raw labels.
        for label in labels[start:start + limit]:
            increment = len(json.dumps(label, ensure_ascii=False)) + int(bool(visible))
            if size + increment > _DIRECTORY_LABEL_CHARS:
                break
            visible.append(label)
            size += increment
        end = start + len(visible)
        more = end < len(labels)
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "mode": "type_directory", "entity_labels": visible,
                "visible_entity_count": len(catalog.entities),
                "distinct_label_count": len(labels), "returned_count": len(visible),
                "next_cursor": cursor_for(end) if more else None,
                "catalog_complete": catalog.complete,
                "truncated_or_incomplete": more or not catalog.complete,
                "scope": catalog.scope,
                "observed_at": observed_at,
                "note": "Type vocabulary only. Search a displayed label for entity IDs, "
                        "then read the original record name under that type. Duplicate "
                        "labels may describe different types; this page cannot prove "
                        "record existence or absence or authorize navigation.",
            },
            audit={
                "application": self._application_id, "operation": DISCOVER_ENTITIES_TOOL,
                "mode": "type_directory", "catalog_digest": catalog.binding_digest,
                "visible_entity_count": len(catalog.entities), "returned_count": len(visible),
                "catalog_complete": catalog.complete, "more_labels": more,
                "observed_at": observed_at,
                **self._identity_audit(context),
            },
        )

    def _describe(self, args: object,
                  context: OperationContext | None) -> BusinessToolResult:
        args = _exact_args(args, {"entity_id", "field_query", "limit"}, DESCRIBE_ENTITY_TOOL)
        catalog, entity_id, entity = self._entity(args.get("entity_id"), context)
        query = _request_text(
            args.get("field_query", ""), "field_query", 200, empty=True)
        limit = _bounded_int(args.get("limit"), name="limit", default=20,
                             minimum=1, maximum=_MAX_DESCRIBED_FIELDS)
        fields = self._fields(entity_id, entity, context)
        ranked = sorted(fields, key=lambda item: _field_score(query, item[1]))
        if query:
            ranked = [item for item in ranked if _score(
                query, f"{item[1].label} {item[1].reference.replace('_', ' ')}") > 0]
        visible = ranked[:limit]
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "entity_id": entity_id,
                "label": entity.label,
                "assistant_operations": list(entity.operations),
                "fields": [{
                    "field_id": field_id,
                    "label": field.label,
                    "type": field.kind,
                    "required": field.required,
                    "readonly": field.readonly,
                    "searchable": field.searchable,
                    "summable": field.summable,
                    **({"choices": [{"value": choice.value, "label": choice.label}
                                      for choice in field.choices]}
                       if field.choices else {}),
                } for field_id, field in visible],
                "safe_field_count": len(fields),
                "matched_field_count": len(ranked),
                "returned_field_count": len(visible),
                "fields_truncated": len(visible) < len(ranked),
                "scope": "safe fields described under the current actor; credential-shaped "
                         "and unbounded fields excluded",
                "observed_at": observed_at,
            },
            audit={
                "application": self._application_id,
                "operation": DESCRIBE_ENTITY_TOOL,
                "entity_ref": entity.reference,
                "catalog_digest": catalog.binding_digest,
                "safe_field_count": len(fields),
                "returned_field_count": len(visible),
                "observed_at": observed_at,
                **self._identity_audit(context),
            },
        )

    def _query(self, args: object,
               context: OperationContext | None) -> BusinessToolResult:
        args = _exact_args(args, {"entity_id", "field_ids", "filters", "limit", "offset",
                                  "aggregate_field_ids"},
                           QUERY_ENTITY_RECORDS_TOOL)
        catalog, entity_id, entity = self._entity(args.get("entity_id"), context)
        fields = self._fields(entity_id, entity, context)
        by_id = dict(fields)
        raw_ids = args.get("field_ids")
        if (not isinstance(raw_ids, list) or not 0 <= len(raw_ids) <= _MAX_QUERY_FIELDS
                or len(raw_ids) != len(set(raw_ids))
                or any(not isinstance(item, str) or not _FIELD_ID.fullmatch(item)
                       or item not in by_id for item in raw_ids)):
            raise BusinessToolRequestError(
                "field_ids must be unique values returned by describe_entity")
        selected = tuple(by_id[item] for item in raw_ids)
        aggregate_ids = args.get("aggregate_field_ids", [])
        if (not isinstance(aggregate_ids, list)
                or len(aggregate_ids) > _MAX_AGGREGATE_FIELDS
                or any(not isinstance(item, str) for item in aggregate_ids)
                or len(aggregate_ids) != len(set(aggregate_ids))
                or any(not _FIELD_ID.fullmatch(item)
                       or item not in by_id or not by_id[item].summable
                       or (by_id[item].kind == "monetary"
                           and not (by_id[item].unit_field_reference or by_id[item].fixed_unit))
                       for item in aggregate_ids)):
            raise BusinessToolRequestError(
                "aggregate_field_ids must name described summable fields with units")
        aggregate_fields = tuple(by_id[item] for item in aggregate_ids)
        raw_filters = args.get("filters", [])
        if not isinstance(raw_filters, list) or len(raw_filters) > _MAX_FILTERS:
            raise BusinessToolRequestError("filters must be a list of at most eight conditions")
        filters = []
        conditions = []
        for item in raw_filters:
            if not isinstance(item, dict) or set(item) != {"field_id", "operator", "value"}:
                raise BusinessToolRequestError("each filter needs field_id, operator and value")
            field_id, operator = item["field_id"], item["operator"]
            field = by_id.get(field_id) if isinstance(field_id, str) else None
            if field is None or not _FIELD_ID.fullmatch(field_id):
                raise BusinessToolRequestError(
                    "filter field_id must come from describe_entity")
            if not field.searchable:
                raise BusinessToolRequestError(f"{field.label} is not searchable")
            if operator not in _OPERATORS:
                raise BusinessToolRequestError("filter operator is not allowed")
            if operator == "contains" and field.kind not in _TEXT_KINDS:
                raise BusinessToolRequestError("contains requires a text-like field")
            if operator in {">", ">=", "<", "<="} and field.kind not in _ORDERED_KINDS:
                raise BusinessToolRequestError(
                    "ordered comparison requires a number or date field")
            value = _filter_value(item["value"], field, operator)
            filters.append(EntityFilter(field=field, operator=operator, value=value))
            conditions.append(f"{field.label} {operator} {value!r}"[:500])
        limit = _bounded_int(args.get("limit"), name="limit", default=10,
                             minimum=1, maximum=_MAX_QUERY_ROWS)
        offset = _bounded_int(args.get("offset"), name="offset", default=0,
                              minimum=0, maximum=_MAX_OFFSET)
        request = EntityQuery(entity=entity, fields=selected, filters=tuple(filters),
                              limit=limit, offset=offset,
                              aggregate_fields=aggregate_fields)
        try:
            page = self._port.query(request, context=context)
        except (BusinessToolContractError, BusinessToolRequestError):
            raise
        except (TypeError, ValueError) as exc:
            raise BusinessToolContractError("the application returned an invalid entity page") \
                from exc
        if not isinstance(page, EntityQueryPage) or len(page.records) > limit:
            raise BusinessToolContractError("the application returned an invalid entity page")
        expected = tuple(field.reference for field in selected)
        aggregate_by_ref = {field.reference: field for field in aggregate_fields}
        grouped_counts = {reference: 0 for reference in aggregate_by_ref}
        for aggregate in page.aggregates:
            field = aggregate_by_ref.get(aggregate.field_reference)
            if field is None or (field.kind == "monetary") != bool(aggregate.unit):
                raise BusinessToolContractError(
                    "the application returned an unrequested or unbound aggregate")
            if field.fixed_unit and aggregate.unit != field.fixed_unit:
                raise BusinessToolContractError("the application changed the declared aggregate unit")
            grouped_counts[field.reference] += aggregate.count
        if any(count != page.matched_count for count in grouped_counts.values()):
            raise BusinessToolContractError(
                "the application returned an incomplete aggregate")
        if page.aggregates and not aggregate_fields:
            raise BusinessToolContractError("the application returned an unrequested aggregate")
        related_entities = {entity.reference: entity_id for entity_id, entity
                            in self._entities(catalog, context)}
        for record in page.records:
            if tuple(cell.field_reference for cell in record.cells) != expected:
                raise BusinessToolContractError(
                    "the application omitted, repeated or reordered a requested field")
            for field, cell in zip(selected, record.cells, strict=True):
                if (cell.related_record_id and
                        (field.kind != "reference" or not field.related_entity_reference)):
                    raise BusinessToolContractError(
                        "the application returned an unbound related record")
                if cell.unit:
                    if field.kind != "monetary" or not cell.value:
                        raise BusinessToolContractError(
                            "the application attached a unit to a non-monetary value")
                    try:
                        number = Decimal(cell.value)
                    except InvalidOperation as exc:
                        raise BusinessToolContractError(
                            "the application returned a nonnumeric monetary value") from exc
                    if not number.is_finite():
                        raise BusinessToolContractError(
                            "the application returned a nonfinite monetary value")
                if field.fixed_unit and cell.value and cell.unit != field.fixed_unit:
                    raise BusinessToolContractError("the application changed the declared monetary unit")
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        origin = Origin(kind="typed", reference=QUERY_ENTITY_RECORDS_TOOL,
                        observed_at=observed_at, scope=page.scope)
        answer_fields = _answer_fields(selected)
        evidence = [QueryEvidence(
            evidence_id="query#1", origin=origin, collection=entity.label[:100],
            conditions=tuple(conditions) or ("no filter",),
            matched_count=page.matched_count, records_scanned=len(page.records),
            covers_whole_scope=offset == 0 and len(page.records) == page.matched_count,
            count_exact=True,
            summable_fields=tuple(answer_name for field, answer_name in
                                  zip(selected, answer_fields, strict=True)
                                  if field.summable),
        )]
        public_aggregates = []
        for index, aggregate in enumerate(page.aggregates, start=1):
            field = aggregate_by_ref[aggregate.field_reference]
            field_id = next(item for item, bound in fields if bound.reference == field.reference)
            public_aggregates.append({
                "field_id": field_id, "label": field.label,
                "sum": aggregate.value, "count": aggregate.count,
                **({"unit": aggregate.unit} if aggregate.unit else {}),
            })
            evidence.append(ComputationEvidence(
                evidence_id=f"aggregate#{index}",
                origin=Origin(kind="typed", reference=QUERY_ENTITY_RECORDS_TOOL,
                              observed_at=observed_at,
                              scope=page.scope + "; " + "; ".join(conditions or ["no filter"])),
                operation=f"sum of {field.label}", inputs=("query#1",),
                value=aggregate.value,
                value_type="money" if field.kind == "monetary" else "number",
                unit=aggregate.unit, covers_whole_scope=True,
            ))
        records = []
        answer_rows = []
        for record in page.records:
            values = []
            cells = []
            for field_id, field, answer_field, cell in zip(
                    raw_ids, selected, answer_fields, record.cells, strict=True):
                values.append({"field_id": field_id, "label": field.label,
                               "value": cell.value,
                               **({"unit": cell.unit} if cell.unit else {}),
                               **({"related_entity_id": related_entities[
                                   field.related_entity_reference],
                                   "related_record_id": cell.related_record_id}
                                  if (cell.related_record_id and
                                      field.related_entity_reference in related_entities)
                                  else {})})
                cells.append(cell.value)
                evidence.append(RecordEvidence(
                    evidence_id=f"record#{len(evidence)}", origin=origin,
                    record_id=f"{entity.label}/{record.display_id}",
                    field_name=answer_field,
                    field_meaning=field.label, field_was_read=True,
                    value=cell.value or None,
                    value_type=("money" if field.kind == "monetary" and cell.unit
                                else field.evidence_type),
                    unit=cell.unit,
                ))
            records.append({"record_id": record.display_id, "values": values,
                            **({"ui_path": record.route} if record.route else {})})
            answer_rows.append((record.display_id, *cells))
        # The port already certified this count for the exact executed conditions.
        # Keep its evidence in the same batch as the query. No model arithmetic,
        # inferred user intent or additional application read is needed.
        evidence.append(ComputationEvidence(
            evidence_id="count#1", origin=origin, operation="count()",
            inputs=("query#1",), value=str(page.matched_count), value_type="count",
            covers_whole_scope=True))
        truncated = offset > 0 or len(page.records) < page.matched_count
        completeness = {
            "source_total_known": True,
            "matching_count_exact": True,
            "result_truncated": truncated,
            "returned_count": len(page.records),
            "matched_count": page.matched_count,
        }
        return BusinessToolResult(
            content={
                "entity_id": entity_id,
                "collection": entity.label,
                "fields": [{"field_id": field_id, "label": field.label,
                            "type": field.kind}
                           for field_id, field in zip(raw_ids, selected, strict=True)],
                "records": records,
                "aggregates": public_aggregates,
                "conditions": conditions or ["no filter"],
                "ordering": "stable record identity ascending",
                "offset": offset,
                "source_total": page.source_total,
                "scope": page.scope,
                "consistency": page.consistency,
                "observed_at": observed_at,
                "completeness": completeness,
            },
            audit={
                "application": self._application_id,
                "operation": QUERY_ENTITY_RECORDS_TOOL,
                "entity_ref": entity.reference,
                "catalog_digest": catalog.binding_digest,
                "filter_count": len(filters),
                "field_count": len(selected),
                "aggregate_field_count": len(aggregate_fields),
                "offset": offset,
                "matched_count": page.matched_count,
                "returned_count": len(page.records),
                "source_total": page.source_total,
                "observed_at": observed_at,
                **completeness,
                **self._identity_audit(context),
            },
            evidence=tuple(evidence),
            query_answer=StructuredQueryAnswer(
                collection=entity.label,
                fields=("record_id", *answer_fields),
                records=tuple(answer_rows), source=page.source, scope=page.scope,
                observed_at=observed_at, source_complete=True,
                result_truncated=truncated, matched_count=page.matched_count,
                source_total=page.source_total, records_scanned=len(page.records),
            ),
            allowed_routes=tuple(dict.fromkeys(
                record.route for record in page.records if record.route)),
        )


@dataclass(frozen=True, slots=True)
class ReferenceMatch:
    """One actor-visible match supplied by an application, not a chosen target."""

    record_id: str
    label: str
    route: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", _text(
            self.record_id, "reference record ID", 120))
        object.__setattr__(self, "label", _text(self.label, "reference label", 200))
        if self.route:
            object.__setattr__(self, "route", _text(self.route, "reference route", 300))


@runtime_checkable
class ReferenceLookupPort(Protocol):
    """Adapter-owned bounded lookup under the same actor as entity discovery."""

    def lookup(self, entity: EntityDescriptor, reference: str, *, limit: int,
               context: OperationContext | None = None
               ) -> tuple[tuple[ReferenceMatch, ...], bool]: ...


class ReferenceLookupTools:
    """Resolve names/numbers without giving the Agent native model or query authority."""

    def __init__(self, entity_port: EntityReadPort, lookup_port: ReferenceLookupPort, *,
                 application_id: str):
        if not isinstance(entity_port, EntityReadPort) or not isinstance(
                lookup_port, ReferenceLookupPort):
            raise TypeError("reference lookup requires entity and lookup ports")
        self._entity_reads = EntityReadTools(entity_port, application_id=application_id)
        self._lookup = lookup_port
        self._application_id = application_id

    def execute(self, args: dict, *, context: OperationContext | None = None
                ) -> BusinessToolResult:
        args = _exact_args(args, {"entity_query", "entity_id", "reference", "limit"},
                           RESOLVE_RECORD_REFERENCE_TOOL)
        if ("entity_id" in args) == ("entity_query" in args):
            raise BusinessToolRequestError("give exactly one of entity_query or entity_id")
        reference = _request_text(args.get("reference"), "reference", 120)
        limit = _bounded_int(args.get("limit"), name="limit", default=5,
                             minimum=1, maximum=5)
        bound = "entity_id" in args
        if bound:
            catalog, entity_id, entity = self._entity_reads._entity(args["entity_id"], context)
            ranked = [(0, entity_id, entity)]
            entity_query = ""
        else:
            entity_query = _request_text(args["entity_query"], "entity_query", 120)
            catalog = self._entity_reads._catalog(context)
            ranked = []
            for entity_id, entity in self._entity_reads._entities(catalog, context):
                score = _score(entity_query, f"{entity.label} {entity.search_text}")
                if score > 0:
                    ranked.append((score, entity_id, entity))
            ranked.sort(key=lambda item: (-item[0], item[2].label.casefold(), item[1]))
        matches = []
        searched = []
        lookup_truncated = False
        permission_omissions = 0
        for score, entity_id, entity in ranked[:3]:
            try:
                response = self._lookup.lookup(
                    entity, reference, limit=limit, context=context)
            except BusinessToolPermissionError:
                if bound:
                    # A known type's refusal cannot silently widen to other types.
                    raise
                permission_omissions += 1
                searched.append({"entity_id": entity_id, "label": entity.label,
                                 "status": "permission_denied"})
                continue
            except (BusinessToolContractError, BusinessToolRequestError):
                raise
            except (TypeError, ValueError) as exc:
                raise BusinessToolContractError(
                    "the application returned invalid reference matches") from exc
            if not isinstance(response, tuple) or len(response) != 2:
                raise BusinessToolContractError(
                    "the application returned invalid reference matches")
            found, truncated = response
            if (not isinstance(found, tuple) or len(found) > limit
                    or any(not isinstance(item, ReferenceMatch) for item in found)
                    or not isinstance(truncated, bool)
                    or len({item.record_id for item in found}) != len(found)):
                raise BusinessToolContractError(
                    "the application returned invalid reference matches")
            lookup_truncated |= truncated
            searched.append({"entity_id": entity_id, "label": entity.label,
                             "status": "read", "returned_count": len(found),
                             "lookup_incomplete": truncated})
            for item in found:
                exact = item.label.casefold() == reference.casefold()
                matches.append((exact, score, entity_id, entity, item))
        matches.sort(key=lambda item: (
            -int(item[0]), -item[1], item[3].label.casefold(),
            item[4].label.casefold(), item[4].record_id))
        visible = matches[:limit]
        truncated = (lookup_truncated or len(ranked) > 3 or len(matches) > limit
                     or permission_omissions > 0
                     or not catalog.complete)
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                **({"entity_id": args["entity_id"]} if bound else {"entity_query": entity_query}),
                "lookup_mode": "bound_entity" if bound else "entity_query",
                "reference": reference,
                "candidates": [{
                    "entity_id": entity_id,
                    "entity_label": entity.label,
                    "record_id": item.record_id,
                    "record_label": item.label,
                    "exact_label": exact,
                    **({"ui_path": item.route} if item.route else {}),
                } for exact, _score_value, entity_id, entity, item in visible],
                "searched_entity_count": min(3, len(ranked)) - permission_omissions,
                "searched_entities": searched,
                **({"recovery_hint": (
                    "These are only the types and reference actually searched. If no requested "
                    "record was found, verify the original name/number without added type words "
                    "and discover the requested entity kind independently. If uncertain, call "
                    "discover_entities without query to browse current type labels. Retry with its "
                    "current entity_id; do not substitute a collection for a requested record. "
                    "An empty or incomplete lookup is not proof of global absence.")}
                   if not visible or truncated else {}),
                "permission_omissions": permission_omissions,
                "matching_entity_count": len(ranked),
                "returned_count": len(visible),
                "truncated_or_incomplete": truncated,
                "scope": catalog.scope,
                "observed_at": observed_at,
            },
            audit={
                "application": self._application_id,
                "operation": RESOLVE_RECORD_REFERENCE_TOOL,
                "lookup_mode": "bound_entity" if bound else "entity_query",
                "catalog_digest": catalog.binding_digest,
                "searched_entity_count": min(3, len(ranked)) - permission_omissions,
                "permission_omissions": permission_omissions,
                "returned_count": len(visible),
                "truncated_or_incomplete": truncated,
                "observed_at": observed_at,
                **EntityReadTools._identity_audit(context),
            },
            allowed_routes=tuple(dict.fromkeys(
                item.route for *_rest, item in visible if item.route)),
        )


__all__ = [
    "DESCRIBE_ENTITY_SCHEMA", "DESCRIBE_ENTITY_TOOL",
    "DISCOVER_ENTITIES_SCHEMA", "DISCOVER_ENTITIES_TOOL",
    "ENTITY_READ_SCHEMAS", "ENTITY_READ_TOOLS",
    "QUERY_ENTITY_RECORDS_SCHEMA", "QUERY_ENTITY_RECORDS_TOOL",
    "RESOLVE_RECORD_REFERENCE_SCHEMA", "RESOLVE_RECORD_REFERENCE_TOOL",
    "EntityCatalog", "EntityCell", "EntityChoice", "EntityDescriptor",
    "EntityAggregate",
    "EntityField", "EntityFilter", "EntityPageHint", "EntityQuery",
    "EntityQueryPage", "EntityReadPort", "EntityReadTools", "EntityRecord",
    "ReferenceLookupPort", "ReferenceLookupTools", "ReferenceMatch",
]
