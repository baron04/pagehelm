"""Actor-scoped discovery and bounded generic reads for Odoo business models.

The model never supplies an Odoo model or field name.  It searches the current actor's
action-backed catalog, receives opaque identifiers bound to that catalog digest, and uses
those identifiers for description and querying.  Every application call still executes as
the delegated Odoo user; metadata is discovery, not authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.answer.evidence import Origin, QueryEvidence, RecordEvidence
from web_operation_agent.business.runtime import (BusinessToolContractError,
                                                  BusinessToolRequestError,
                                                  BusinessToolResult)
from web_operation_agent.state.identity import OperationContext

from .descriptors import (OdooActionDescriptor, OdooModelDescriptor,
                          OdooRuntimeCatalog, OdooRuntimeDescriptorReader)
from .specs import APPLICATION_ID


DISCOVER_MODELS_TOOL = "discover_odoo_models"
DESCRIBE_MODEL_TOOL = "describe_odoo_model"
QUERY_RECORDS_TOOL = "query_odoo_records"
GENERIC_READ_TOOLS = frozenset({
    DISCOVER_MODELS_TOOL, DESCRIBE_MODEL_TOOL, QUERY_RECORDS_TOOL,
})

_CANDIDATE_ID = re.compile(r"m_[0-9a-f]{32}\Z")
_FIELD_ID = re.compile(r"f_[0-9a-f]{32}\Z")
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TOKENS = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]")
_SENSITIVE_FIELD = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|"
    r"access[_-]?key|credential|signature|cookie|session)", re.I)
_DENIED_MODEL_PARTS = frozenset({
    "api", "apikey", "attachment", "auth", "automation", "bus", "config",
    "credential", "group", "iap", "mail", "message", "oauth", "password",
    "rule", "secret", "session", "setting", "token", "user", "users",
})
_DENIED_MODEL_PREFIXES = ("ir.", "auth.", "mail.", "bus.", "web.")
_SAFE_FIELD_TYPES = frozenset({
    "boolean", "integer", "float", "monetary", "char", "text", "selection",
    "date", "datetime", "many2one",
})
_TEXT_TYPES = frozenset({"char", "text", "selection", "many2one"})
_ORDERED_TYPES = frozenset({"integer", "float", "monetary", "date", "datetime"})
_OPERATORS = frozenset({"=", "!=", "in", "not in", "ilike", ">", ">=", "<", "<="})
_MAX_FIELDS_GET = 2_000
_MAX_DESCRIBED_FIELDS = 40
_MAX_QUERY_FIELDS = 8
_MAX_FILTERS = 8
_MAX_QUERY_ROWS = 20
_MAX_OFFSET = 10_000
_MAX_SELECTION = 50
_MAX_VALUE_CHARS = 300


DISCOVER_MODELS_SCHEMA = {
    "type": "function",
    "function": {
        "name": DISCOVER_MODELS_TOOL,
        "description": (
            "Search the signed-in Odoo user's current readable, menu-backed business model "
            "catalog. Returns opaque candidate IDs, not model authority. Use this before "
            "describe_odoo_model; do not guess a model name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200,
                          "description": "User-language entity or page description."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10,
                          "description": "Maximum candidates; defaults to 5."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

DESCRIBE_MODEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": DESCRIBE_MODEL_TOOL,
        "description": (
            "Describe safe readable fields for one opaque candidate returned by "
            "discover_odoo_models. Returns opaque field IDs for query_odoo_records."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string", "pattern": r"^m_[0-9a-f]{32}$"},
                "field_query": {"type": "string", "maxLength": 200,
                                "description": "Optional user-language field description."},
                "limit": {"type": "integer", "minimum": 1,
                          "maximum": _MAX_DESCRIBED_FIELDS,
                          "description": "Maximum fields; defaults to 20."},
            },
            "required": ["candidate_id"],
            "additionalProperties": False,
        },
    },
}

QUERY_RECORDS_SCHEMA = {
    "type": "function",
    "function": {
        "name": QUERY_RECORDS_TOOL,
        "description": (
            "Read current records for one described Odoo candidate. Use only opaque field "
            "IDs returned by describe_odoo_model. Filters are an AND list; sorting is fixed "
            "to id ascending. Reports exact matching and actor-visible totals."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string", "pattern": r"^m_[0-9a-f]{32}$"},
                "field_ids": {
                    "type": "array", "minItems": 1, "maxItems": _MAX_QUERY_FIELDS,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": r"^f_[0-9a-f]{32}$"},
                    "description": "Fields to return, from describe_odoo_model.",
                },
                "filters": {
                    "type": "array", "maxItems": _MAX_FILTERS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "field_id": {"type": "string",
                                         "pattern": r"^f_[0-9a-f]{32}$"},
                            "operator": {"type": "string", "enum": sorted(_OPERATORS)},
                            "value": {},
                        },
                        "required": ["field_id", "operator", "value"],
                        "additionalProperties": False,
                    },
                    "description": "Zero to eight server-validated conditions, combined by AND.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_QUERY_ROWS,
                          "description": "Rows in this page; defaults to 10."},
                "offset": {"type": "integer", "minimum": 0, "maximum": _MAX_OFFSET,
                           "description": "Bounded page offset; defaults to 0."},
            },
            "required": ["candidate_id", "field_ids"],
            "additionalProperties": False,
        },
    },
}

GENERIC_READ_SCHEMAS = (
    DISCOVER_MODELS_SCHEMA, DESCRIBE_MODEL_SCHEMA, QUERY_RECORDS_SCHEMA,
)


def _bounded_text(value: object, *, name: str, maximum: int,
                  allow_empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum or _CONTROL.search(value)
            or (not allow_empty and not value.strip())):
        raise BusinessToolRequestError(f"{name} must be bounded visible text")
    return " ".join(value.split())


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


def _opaque(prefix: str, binding: str, name: str) -> str:
    raw = f"{binding}\0{name}".encode("utf-8")
    return prefix + hashlib.sha256(raw).hexdigest()[:32]


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKENS.findall(value.casefold()))


def _model_allowed(name: str) -> bool:
    folded = name.casefold()
    parts = frozenset(folded.split("."))
    return not folded.startswith(_DENIED_MODEL_PREFIXES) and not parts & _DENIED_MODEL_PARTS


@dataclass(frozen=True, slots=True)
class _Candidate:
    candidate_id: str
    model: OdooModelDescriptor
    actions: tuple[OdooActionDescriptor, ...]

    @property
    def searchable_text(self) -> str:
        action_text = " ".join(
            [item.name for item in self.actions]
            + [crumb for item in self.actions for crumb in item.breadcrumbs])
        return f"{self.model.label} {self.model.model.replace('.', ' ')} {action_text}".casefold()


@dataclass(frozen=True, slots=True)
class _Field:
    field_id: str
    name: str
    label: str
    field_type: str
    required: bool
    readonly: bool
    searchable: bool
    selection: tuple[tuple[object, str], ...]


def _candidates(catalog: OdooRuntimeCatalog) -> tuple[_Candidate, ...]:
    actions_by_model: dict[str, list[OdooActionDescriptor]] = {}
    for action in catalog.actions:
        if action.readable and action.model and _model_allowed(action.model):
            actions_by_model.setdefault(action.model, []).append(action)
    candidates = []
    for model in catalog.models:
        actions = actions_by_model.get(model.model)
        if (not actions or model.transient or "read" not in model.operations
                or not _model_allowed(model.model)):
            continue
        candidates.append(_Candidate(
            candidate_id=_opaque("m_", catalog.descriptor_digest, model.model),
            model=model,
            actions=tuple(sorted(actions, key=lambda item: item.action_id)),
        ))
    identifiers = [item.candidate_id for item in candidates]
    if len(identifiers) != len(set(identifiers)):
        raise BusinessToolContractError("Odoo model candidate identifiers collided")
    return tuple(candidates)


def _score(query: str, text: str) -> int:
    wanted = _tokens(query)
    available = _tokens(text)
    if not wanted or not available:
        return 0
    counts = {token: available.count(token) for token in set(available)}
    overlap = sum(min(3, counts.get(token, 0)) for token in wanted)
    if not overlap:
        return 0
    phrase = 20 if query.casefold() in text else 0
    coverage = (100 * len({token for token in wanted if token in counts})) // len(set(wanted))
    return phrase + coverage + overlap


def _selection(value: object) -> tuple[tuple[object, str], ...]:
    if value in (None, False):
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_SELECTION:
        return ()
    result = []
    for item in value:
        if (not isinstance(item, (list, tuple)) or len(item) != 2
                or isinstance(item[0], (dict, list, tuple))
                or not isinstance(item[1], str)):
            return ()
        label = " ".join(item[1].split())[:120]
        if not label or _CONTROL.search(label):
            return ()
        result.append((item[0], label))
    return tuple(result)


def _safe_fields(candidate: _Candidate, payload: object) -> tuple[_Field, ...]:
    if not isinstance(payload, dict) or len(payload) > _MAX_FIELDS_GET:
        raise BusinessToolContractError("Odoo returned an invalid fields_get payload")
    # Odoo's virtual primary key is readable/searchable but is not consistently included in
    # fields_get. Make it an ordinary opaque field so exact point reads need no raw name.
    result = [_Field(
        field_id=_opaque("f_", candidate.candidate_id, "id"),
        name="id", label="ID", field_type="integer", required=True,
        readonly=True, searchable=True, selection=(),
    )]
    for name, value in sorted(payload.items()):
        if (name == "id"
                or not isinstance(name, str) or not _FIELD_NAME.fullmatch(name)
                or _SENSITIVE_FIELD.search(name) or not isinstance(value, dict)):
            continue
        field_type = value.get("type")
        if field_type not in _SAFE_FIELD_TYPES:
            continue
        label = value.get("string")
        if (not isinstance(label, str) or not label.strip() or len(label) > 200
                or _CONTROL.search(label) or _SENSITIVE_FIELD.search(label)):
            continue
        searchable = value.get("searchable")
        if not isinstance(searchable, bool):
            searchable = bool(value.get("store", True))
        result.append(_Field(
            field_id=_opaque("f_", candidate.candidate_id, name),
            name=name,
            label=" ".join(label.split()),
            field_type=field_type,
            required=value.get("required") is True,
            readonly=value.get("readonly") is True,
            searchable=searchable,
            selection=_selection(value.get("selection")) if field_type == "selection" else (),
        ))
    identifiers = [item.field_id for item in result]
    if len(identifiers) != len(set(identifiers)):
        raise BusinessToolContractError("Odoo field identifiers collided")
    return tuple(result)


def _field_score(query: str, field: _Field) -> tuple[int, int, str]:
    priority = 0 if field.name in {"id", "display_name", "name"} else 1
    return (-_score(query, f"{field.label} {field.name.replace('_', ' ')}"),
            priority, field.name)


def _scalar(value: object, field: _Field, operator: str) -> object:
    if field.field_type == "boolean":
        if not isinstance(value, bool):
            raise BusinessToolRequestError(f"{field.label} requires a boolean")
        return value
    if field.field_type == "integer" or (field.field_type == "many2one" and operator != "ilike"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise BusinessToolRequestError(f"{field.label} requires an integer")
        return value
    if field.field_type in {"float", "monetary"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)):
            raise BusinessToolRequestError(f"{field.label} requires a finite number")
        return value
    if not isinstance(value, str) or len(value) > 300 or _CONTROL.search(value):
        raise BusinessToolRequestError(f"{field.label} requires bounded text")
    if field.field_type == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            raise BusinessToolRequestError(f"{field.label} requires YYYY-MM-DD") from None
    if field.field_type == "datetime":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise BusinessToolRequestError(f"{field.label} requires an ISO datetime") from None
    if field.field_type == "selection" and field.selection \
            and value not in {item[0] for item in field.selection}:
        raise BusinessToolRequestError(f"{field.label} requires a declared selection key")
    return value


def _filter_value(value: object, field: _Field, operator: str) -> object:
    if operator in {"in", "not in"}:
        if not isinstance(value, list) or not 1 <= len(value) <= 50:
            raise BusinessToolRequestError(f"{operator} requires a list of 1 to 50 values")
        return [_scalar(item, field, "=") for item in value]
    return _scalar(value, field, operator)


def _render_value(value: object, field: _Field) -> str:
    if value is None or (value is False and field.field_type != "boolean"):
        return ""
    if field.field_type == "boolean":
        if not isinstance(value, bool):
            raise BusinessToolContractError(f"Odoo returned an invalid {field.label} value")
        return "true" if value else "false"
    if field.field_type == "many2one":
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or isinstance(value[0], bool) or not isinstance(value[0], int)
                or not isinstance(value[1], str)):
            raise BusinessToolContractError(f"Odoo returned an invalid {field.label} value")
        value = value[1]
    elif field.field_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise BusinessToolContractError(f"Odoo returned an invalid {field.label} value")
    elif field.field_type in {"float", "monetary"}:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            raise BusinessToolContractError(f"Odoo returned an invalid {field.label} value")
    elif field.field_type in {"char", "text", "date", "datetime"} \
            and not isinstance(value, str):
        raise BusinessToolContractError(f"Odoo returned an invalid {field.label} value")
    elif field.field_type == "selection" \
            and isinstance(value, (dict, list, tuple)):
        raise BusinessToolContractError(f"Odoo returned an invalid {field.label} value")
    if field.field_type == "selection":
        labels = dict(field.selection)
        value = labels.get(value, value)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    text = " ".join(text.split())
    return text[:_MAX_VALUE_CHARS]


def _value_type(field: _Field) -> str:
    if field.field_type in {"integer", "float"}:
        return "number"
    if field.field_type == "boolean":
        return "boolean"
    if field.field_type == "date":
        return "date"
    if field.field_type == "datetime":
        return "moment"
    # Monetary fields have no safe generic way to identify their currency field. Keeping
    # them textual prevents the core from adding unlike denominations.
    return "text"


class OdooGenericReadTools:
    """Mixin implementing the three generic-read tools over an Odoo RPC ``call`` method."""

    @staticmethod
    def _identity_audit(context: OperationContext | None) -> dict:
        if context is None:
            return {"identity_binding": "unverified"}
        return {"identity_binding": "principal_bound",
                "identity": context.audit_projection()}

    def _catalog(self) -> OdooRuntimeCatalog:
        return OdooRuntimeDescriptorReader(self).catalog()

    def _candidate(self, candidate_id: object) -> tuple[OdooRuntimeCatalog, _Candidate]:
        if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(candidate_id):
            raise BusinessToolRequestError("candidate_id must come from discover_odoo_models")
        catalog = self._catalog()
        candidate = next((item for item in _candidates(catalog)
                          if item.candidate_id == candidate_id), None)
        if candidate is None:
            raise BusinessToolRequestError(
                "candidate_id is absent or stale; discover the model again")
        return catalog, candidate

    def _fields(self, candidate: _Candidate) -> tuple[_Field, ...]:
        payload = self.call(candidate.model.model, "fields_get", [], {"attributes": [
            "string", "type", "required", "readonly", "searchable", "store", "selection",
        ]})
        return _safe_fields(candidate, payload)

    def execute_generic(self, name: str, args: dict, *,
                        context: OperationContext | None = None) -> BusinessToolResult:
        if name == DISCOVER_MODELS_TOOL:
            return self._discover(args, context)
        if name == DESCRIBE_MODEL_TOOL:
            return self._describe(args, context)
        if name == QUERY_RECORDS_TOOL:
            return self._query(args, context)
        raise BusinessToolRequestError("unknown Odoo generic read tool")

    def _discover(self, args: object,
                  context: OperationContext | None) -> BusinessToolResult:
        args = _exact_args(args, {"query", "limit"}, DISCOVER_MODELS_TOOL)
        query = _bounded_text(args.get("query"), name="query", maximum=200)
        limit = _bounded_int(args.get("limit"), name="limit", default=5,
                             minimum=1, maximum=10)
        catalog = self._catalog()
        ranked = [(score, item) for item in _candidates(catalog)
                  if (score := _score(query, item.searchable_text)) > 0]
        ranked.sort(key=lambda pair: (-pair[0], pair[1].model.label.casefold(),
                                      pair[1].candidate_id))
        candidates = []
        for score, item in ranked[:limit]:
            page_hints = []
            for action in item.actions[:5]:
                page_hints.append({
                    "name": action.name,
                    "breadcrumbs": list(action.breadcrumbs),
                    "view_modes": list(action.view_modes),
                })
            candidates.append({
                "candidate_id": item.candidate_id,
                "label": item.model.label,
                "assistant_operations": ["read"],
                "page_hints": page_hints,
                "match_score": score,
            })
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        content = {
            "query": query,
            "candidates": candidates,
            "matched_count": len(ranked),
            "returned_count": len(candidates),
            "catalog_complete": catalog.complete,
            "language": catalog.language,
            "company_id": catalog.company_id,
            "scope": "current actor's readable menu-backed non-sensitive Odoo models",
            "observed_at": observed_at,
        }
        return BusinessToolResult(
            content=content,
            audit={
                "application": APPLICATION_ID,
                "operation": DISCOVER_MODELS_TOOL,
                "catalog_digest": catalog.descriptor_digest,
                "matched_count": len(ranked),
                "returned_count": len(candidates),
                "catalog_complete": catalog.complete,
                "observed_at": observed_at,
                **self._identity_audit(context),
            },
        )

    def _describe(self, args: object,
                  context: OperationContext | None) -> BusinessToolResult:
        args = _exact_args(
            args, {"candidate_id", "field_query", "limit"}, DESCRIBE_MODEL_TOOL)
        catalog, candidate = self._candidate(args.get("candidate_id"))
        field_query = _bounded_text(
            args.get("field_query", ""), name="field_query", maximum=200, allow_empty=True)
        limit = _bounded_int(args.get("limit"), name="limit", default=20,
                             minimum=1, maximum=_MAX_DESCRIBED_FIELDS)
        fields = self._fields(candidate)
        ranked = sorted(fields, key=lambda item: _field_score(field_query, item))
        if field_query:
            ranked = [item for item in ranked
                      if _score(field_query, f"{item.label} {item.name.replace('_', ' ')}") > 0]
        visible = ranked[:limit]
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "candidate_id": candidate.candidate_id,
                "label": candidate.model.label,
                "assistant_operations": ["read"],
                "fields": [{
                    "field_id": field.field_id,
                    "label": field.label,
                    "type": field.field_type,
                    "required": field.required,
                    "readonly": field.readonly,
                    "searchable": field.searchable,
                    **({"selection": [{"value": value, "label": label}
                                       for value, label in field.selection]}
                       if field.selection else {}),
                } for field in visible],
                "safe_field_count": len(fields),
                "matched_field_count": len(ranked),
                "returned_field_count": len(visible),
                "fields_truncated": len(visible) < len(ranked),
                "scope": "fields_get under the current delegated actor; sensitive and "
                         "unbounded field types excluded",
                "observed_at": observed_at,
            },
            audit={
                "application": APPLICATION_ID,
                "operation": DESCRIBE_MODEL_TOOL,
                "model": candidate.model.model,
                "catalog_digest": catalog.descriptor_digest,
                "safe_field_count": len(fields),
                "returned_field_count": len(visible),
                "observed_at": observed_at,
                **self._identity_audit(context),
            },
        )

    def _query(self, args: object,
               context: OperationContext | None) -> BusinessToolResult:
        args = _exact_args(
            args, {"candidate_id", "field_ids", "filters", "limit", "offset"},
            QUERY_RECORDS_TOOL)
        catalog, candidate = self._candidate(args.get("candidate_id"))
        fields = self._fields(candidate)
        by_id = {field.field_id: field for field in fields}
        raw_field_ids = args.get("field_ids")
        if (not isinstance(raw_field_ids, list)
                or not 1 <= len(raw_field_ids) <= _MAX_QUERY_FIELDS
                or len(raw_field_ids) != len(set(raw_field_ids))
                or any(not isinstance(item, str) or not _FIELD_ID.fullmatch(item)
                       or item not in by_id for item in raw_field_ids)):
            raise BusinessToolRequestError(
                "field_ids must be unique values returned by describe_odoo_model")
        selected = tuple(by_id[item] for item in raw_field_ids)
        raw_filters = args.get("filters", [])
        if not isinstance(raw_filters, list) or len(raw_filters) > _MAX_FILTERS:
            raise BusinessToolRequestError("filters must be a list of at most eight conditions")
        domain = []
        conditions = []
        for item in raw_filters:
            if not isinstance(item, dict) or set(item) != {"field_id", "operator", "value"}:
                raise BusinessToolRequestError("each filter needs field_id, operator and value")
            field_id = item["field_id"]
            operator = item["operator"]
            field = by_id.get(field_id) if isinstance(field_id, str) else None
            if field is None or not _FIELD_ID.fullmatch(field_id):
                raise BusinessToolRequestError(
                    "filter field_id must come from describe_odoo_model")
            if not field.searchable:
                raise BusinessToolRequestError(f"{field.label} is not searchable")
            if operator not in _OPERATORS:
                raise BusinessToolRequestError("filter operator is not allowed")
            if operator == "ilike" and field.field_type not in _TEXT_TYPES:
                raise BusinessToolRequestError("ilike requires a text-like field")
            if operator in {">", ">=", "<", "<="} and field.field_type not in _ORDERED_TYPES:
                raise BusinessToolRequestError("ordered comparison requires a number or date field")
            value = _filter_value(item["value"], field, operator)
            domain.append([field.name, operator, value])
            conditions.append(f"{field.label} {operator} {value!r}"[:500])
        limit = _bounded_int(args.get("limit"), name="limit", default=10,
                             minimum=1, maximum=_MAX_QUERY_ROWS)
        offset = _bounded_int(args.get("offset"), name="offset", default=0,
                              minimum=0, maximum=_MAX_OFFSET)

        matched = self.call(candidate.model.model, "search_count", [domain])
        source_total = self.call(candidate.model.model, "search_count", [[]])
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in (matched, source_total)):
            raise BusinessToolContractError("Odoo returned an invalid generic record count")
        if matched > source_total:
            raise BusinessToolContractError(
                "Odoo returned a generic match count above its source total")
        rows = self.call(candidate.model.model, "search_read", [domain], {
            "fields": [field.name for field in selected],
            "limit": limit, "offset": offset, "order": "id asc",
        })
        if not isinstance(rows, list) or len(rows) > limit:
            raise BusinessToolContractError("Odoo returned an invalid generic record page")
        seen = set()
        records = []
        evidence = []
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        scope = (f"{candidate.model.label} records visible to the delegated Odoo user; "
                 f"{len(domain)} AND filters; id ascending")[:200]
        origin = Origin(kind="typed", reference=QUERY_RECORDS_TOOL,
                        observed_at=observed_at, scope=scope)
        evidence.append(QueryEvidence(
            evidence_id="query#1", origin=origin,
            collection=candidate.model.label[:100],
            conditions=tuple(conditions) or ("no filter",),
            matched_count=matched, records_scanned=len(rows),
            covers_whole_scope=offset == 0 and len(rows) == matched,
            count_exact=True,
        ))
        answer_rows = []
        for row in rows:
            record_id = row.get("id") if isinstance(row, dict) else None
            if (isinstance(record_id, bool) or not isinstance(record_id, int)
                    or record_id <= 0 or record_id in seen):
                raise BusinessToolContractError(
                    "Odoo returned a missing or repeated generic record identity")
            seen.add(record_id)
            values = []
            cells = []
            for field in selected:
                if field.name not in row:
                    raise BusinessToolContractError(
                        "Odoo omitted a requested generic record field")
                raw = row.get(field.name)
                rendered = _render_value(raw, field)
                values.append({"field_id": field.field_id,
                               "label": field.label, "value": rendered})
                cells.append(rendered)
                evidence.append(RecordEvidence(
                    evidence_id=f"record#{len(evidence)}", origin=origin,
                    record_id=f"{candidate.model.model}/{record_id}",
                    field_name=field.name, field_meaning=field.label,
                    field_was_read=True,
                    value=rendered or None,
                    value_type=_value_type(field),
                ))
            records.append({"record_id": record_id, "values": values})
            answer_rows.append((str(record_id), *cells))
        if len(rows) > matched:
            raise BusinessToolContractError("Odoo returned more rows than its generic count")
        result_truncated = offset > 0 or len(rows) < matched
        completeness = {
            "source_total_known": True,
            "matching_count_exact": True,
            "result_truncated": result_truncated,
            "returned_count": len(rows),
            "matched_count": matched,
        }
        return BusinessToolResult(
            content={
                "candidate_id": candidate.candidate_id,
                "collection": candidate.model.label,
                "fields": [{"field_id": field.field_id, "label": field.label,
                            "type": field.field_type} for field in selected],
                "records": records,
                "conditions": conditions or ["no filter"],
                "ordering": "id asc",
                "offset": offset,
                "source_total": source_total,
                "scope": scope,
                "consistency": "two counts and one page read; not a database snapshot",
                "observed_at": observed_at,
                "completeness": completeness,
            },
            audit={
                "application": APPLICATION_ID,
                "operation": QUERY_RECORDS_TOOL,
                "model": candidate.model.model,
                "catalog_digest": catalog.descriptor_digest,
                "filter_count": len(domain),
                "field_count": len(selected),
                "offset": offset,
                "matched_count": matched,
                "returned_count": len(rows),
                "source_total": source_total,
                "observed_at": observed_at,
                **completeness,
                **self._identity_audit(context),
            },
            evidence=tuple(evidence),
            query_answer=StructuredQueryAnswer(
                collection=candidate.model.label,
                fields=("record_id", *(field.name for field in selected)),
                records=tuple(answer_rows),
                source=f"Odoo delegated JSON-RPC ({candidate.model.label})",
                scope=scope,
                observed_at=observed_at,
                source_complete=True,
                result_truncated=result_truncated,
                matched_count=matched,
                source_total=source_total,
                records_scanned=len(rows),
            ),
        )
