"""Translate Odoo runtime metadata and JSON-RPC reads to the generic entity contract.

This module is deliberately application-owned. Odoo model names, ``fields_get`` payloads,
domains, and relation values end here; the core and Agent-facing tool schemas know only the
application-neutral descriptors from :mod:`web_operation_agent.business.entities`.
"""
from __future__ import annotations

import json
import math
import re

from web_operation_agent.business.entities import (
    EntityCatalog,
    EntityCell,
    EntityChoice,
    EntityDescriptor,
    EntityField,
    EntityPageHint,
    EntityQuery,
    EntityQueryPage,
    EntityRecord,
)
from web_operation_agent.business.runtime import BusinessToolContractError

from .descriptors import OdooRuntimeDescriptorReader


_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENSITIVE_FIELD = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|"
    r"access[_-]?key|credential|signature|cookie|session)", re.I)
_DENIED_MODEL_PARTS = frozenset({
    "api", "apikey", "attachment", "auth", "automation", "bus", "config",
    "credential", "group", "iap", "mail", "message", "oauth", "password",
    "rule", "secret", "session", "setting", "token", "user", "users",
})
_DENIED_MODEL_PREFIXES = ("ir.", "auth.", "mail.", "bus.", "web.")
_TYPE_MAP = {
    "boolean": ("boolean", "boolean"),
    "integer": ("integer", "number"),
    "float": ("number", "number"),
    # Without a currency binding a generic calculator must not add unlike denominations.
    "monetary": ("monetary", "text"),
    "char": ("text", "text"),
    "text": ("text", "text"),
    "selection": ("choice", "text"),
    "date": ("date", "date"),
    "datetime": ("datetime", "moment"),
    "many2one": ("reference", "text"),
}
_MAX_FIELDS_GET = 2_000
_MAX_SELECTION = 50
_MAX_VALUE_CHARS = 300


def _model_allowed(name: str) -> bool:
    folded = name.casefold()
    parts = frozenset(folded.split("."))
    return not folded.startswith(_DENIED_MODEL_PREFIXES) and not parts & _DENIED_MODEL_PARTS


def _choices(value: object) -> tuple[EntityChoice, ...]:
    if value in (None, False):
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_SELECTION:
        return ()
    result = []
    try:
        for item in value:
            if (not isinstance(item, (list, tuple)) or len(item) != 2
                    or isinstance(item[0], (dict, list, tuple)) or item[0] is None
                    or not isinstance(item[1], str)):
                return ()
            result.append(EntityChoice(value=item[0], label=item[1]))
    except ValueError:
        return ()
    return tuple(result)


def _render_value(value: object, field: EntityField) -> str:
    if value is None or (value is False and field.kind != "boolean"):
        return ""
    if field.kind == "boolean":
        if not isinstance(value, bool):
            raise BusinessToolContractError(
                f"Odoo returned an invalid {field.label} value")
        return "true" if value else "false"
    if field.kind == "reference":
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or isinstance(value[0], bool) or not isinstance(value[0], int)
                or not isinstance(value[1], str)):
            raise BusinessToolContractError(
                f"Odoo returned an invalid {field.label} value")
        value = value[1]
    elif field.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise BusinessToolContractError(
                f"Odoo returned an invalid {field.label} value")
    elif field.kind in {"number", "monetary"}:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            raise BusinessToolContractError(
                f"Odoo returned an invalid {field.label} value")
    elif field.kind in {"text", "date", "datetime"} and not isinstance(value, str):
        raise BusinessToolContractError(
            f"Odoo returned an invalid {field.label} value")
    elif field.kind == "choice" and isinstance(value, (dict, list, tuple)):
        raise BusinessToolContractError(
            f"Odoo returned an invalid {field.label} value")
    if field.kind == "choice":
        labels = {item.value: item.label for item in field.choices}
        value = labels.get(value, value)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    return " ".join(text.split())[:_MAX_VALUE_CHARS]


class OdooEntityReadPort:
    """Odoo-specific implementation of the core ``EntityReadPort`` protocol."""

    def __init__(self, client):
        self._client = client

    def catalog(self, *, context=None) -> EntityCatalog:
        catalog = OdooRuntimeDescriptorReader(self._client).catalog()
        actions_by_model = {}
        for action in catalog.actions:
            if action.readable and action.model and _model_allowed(action.model):
                actions_by_model.setdefault(action.model, []).append(action)
        entities = []
        for model in catalog.models:
            actions = actions_by_model.get(model.model)
            if (not actions or model.transient or "read" not in model.operations
                    or not _model_allowed(model.model)):
                continue
            ordered = tuple(sorted(actions, key=lambda item: item.action_id))
            entities.append(EntityDescriptor(
                reference=model.model,
                label=model.label,
                search_text=(model.model.replace(".", " ") + " " + " ".join(
                    [item.name for item in ordered]
                    + [crumb for item in ordered for crumb in item.breadcrumbs])),
                page_hints=tuple(EntityPageHint(
                    name=item.name,
                    breadcrumbs=tuple(item.breadcrumbs),
                    views=tuple(item.view_modes),
                ) for item in ordered),
            ))
        return EntityCatalog(
            binding_digest=catalog.descriptor_digest,
            entities=tuple(entities),
            complete=catalog.complete,
            scope="current actor's readable, page-backed, non-sensitive business entities",
        )

    def fields(self, entity: EntityDescriptor, *, context=None) -> tuple[EntityField, ...]:
        payload = self._client.call(entity.reference, "fields_get", [], {"attributes": [
            "string", "type", "required", "readonly", "searchable", "store", "selection",
        ]})
        if not isinstance(payload, dict) or len(payload) > _MAX_FIELDS_GET:
            raise BusinessToolContractError("Odoo returned an invalid fields_get payload")
        result = [EntityField(
            reference="id", label="ID", kind="integer", required=True,
            readonly=True, searchable=True, evidence_type="number",
        )]
        for name, value in sorted(payload.items()):
            if (name == "id" or not isinstance(name, str) or not _FIELD_NAME.fullmatch(name)
                    or _SENSITIVE_FIELD.search(name) or not isinstance(value, dict)):
                continue
            mapped = _TYPE_MAP.get(value.get("type"))
            label = value.get("string")
            if (mapped is None or not isinstance(label, str) or not label.strip()
                    or len(label) > 160 or _CONTROL.search(label)
                    or _SENSITIVE_FIELD.search(label)):
                continue
            searchable = value.get("searchable")
            if not isinstance(searchable, bool):
                searchable = bool(value.get("store", True))
            result.append(EntityField(
                reference=name,
                label=" ".join(label.split()),
                kind=mapped[0],
                required=value.get("required") is True,
                readonly=value.get("readonly") is True,
                searchable=searchable,
                choices=_choices(value.get("selection"))
                if value.get("type") == "selection" else (),
                evidence_type=mapped[1],
            ))
        return tuple(result)

    def query(self, request: EntityQuery, *, context=None) -> EntityQueryPage:
        operator_map = {"contains": "ilike", "not_in": "not in"}
        domain = [[condition.field.reference,
                   operator_map.get(condition.operator, condition.operator),
                   condition.value]
                  for condition in request.filters]
        model = request.entity.reference
        matched = self._client.call(model, "search_count", [domain])
        source_total = self._client.call(model, "search_count", [[]])
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in (matched, source_total)):
            raise BusinessToolContractError("Odoo returned an invalid entity record count")
        if matched > source_total:
            raise BusinessToolContractError(
                "Odoo returned an entity match count above its source total")
        rows = self._client.call(model, "search_read", [domain], {
            "fields": [field.reference for field in request.fields],
            "limit": request.limit,
            "offset": request.offset,
            "order": "id asc",
        })
        if not isinstance(rows, list) or len(rows) > request.limit:
            raise BusinessToolContractError("Odoo returned an invalid entity record page")
        records = []
        seen = set()
        for row in rows:
            record_id = row.get("id") if isinstance(row, dict) else None
            if (isinstance(record_id, bool) or not isinstance(record_id, int)
                    or record_id <= 0 or record_id in seen):
                raise BusinessToolContractError(
                    "Odoo returned a missing or repeated entity record identity")
            seen.add(record_id)
            cells = []
            for field in request.fields:
                if field.reference not in row:
                    raise BusinessToolContractError(
                        "Odoo omitted a requested entity record field")
                cells.append(EntityCell(
                    field_reference=field.reference,
                    value=_render_value(row[field.reference], field),
                ))
            records.append(EntityRecord(
                reference=f"{model}/{record_id}",
                display_id=str(record_id),
                cells=tuple(cells),
            ))
        return EntityQueryPage(
            records=tuple(records),
            matched_count=matched,
            source_total=source_total,
            source=f"delegated application API ({request.entity.label})",
            scope=(f"{request.entity.label} records visible to the delegated actor; "
                   f"{len(domain)} AND filters; stable identity ascending")[:200],
        )


__all__ = ["OdooEntityReadPort"]
