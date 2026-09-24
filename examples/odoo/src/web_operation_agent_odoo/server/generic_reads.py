"""Translate Odoo runtime metadata and JSON-RPC reads to the generic entity contract.

This module is deliberately application-owned. Odoo model names, ``fields_get`` payloads,
domains, and relation values end here; the core and Agent-facing tool schemas know only the
application-neutral descriptors from :mod:`web_operation_agent.business.entities`.
"""
from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from itertools import chain

from web_operation_agent.business.entities import (
    EntityAggregate,
    EntityCatalog,
    EntityCell,
    EntityChoice,
    EntityDescriptor,
    EntityField,
    EntityPageHint,
    EntityQuery,
    EntityQueryPage,
    EntityRecord,
    ReferenceMatch,
)
from web_operation_agent.business.runtime import BusinessToolContractError

from ..route import ODOO_ROUTE_PORT
from .descriptors import OdooRuntimeDescriptorReader


_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_MODEL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _bounded_entity_search_text(parts) -> tuple[str, bool]:
    """Keep deterministic discovery text within the core descriptor bound.

    A truncated catalog must not claim complete search coverage. The separate page-entry
    catalog still retains every actor-visible action for direct page lookup.
    """
    words = []
    length = 0
    seen = set()
    for raw in parts:
        value = " ".join(raw.split())
        if not value or value.casefold() in seen:
            continue
        size = len(value) + (1 if words else 0)
        if length + size > 2_000:
            return " ".join(words), False
        words.append(value)
        seen.add(value.casefold())
        length += size
    return " ".join(words), True


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
_MAX_CURRENCY_CHARS = 80


def odoo_model_allowed(name: str) -> bool:
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
            if action.readable and action.model and odoo_model_allowed(action.model):
                actions_by_model.setdefault(action.model, []).append(action)
        entities = []
        searchable_complete = True
        for model in catalog.models:
            actions = actions_by_model.get(model.model)
            if (not actions or model.transient or "read" not in model.operations
                    or not odoo_model_allowed(model.model)):
                continue
            ordered = tuple(sorted(actions, key=lambda item: item.action_id))
            search_text, search_complete = _bounded_entity_search_text(
                chain((model.model.replace(".", " "), model.label),
                      (item.name for item in ordered),
                      (crumb for item in ordered for crumb in item.breadcrumbs)))
            searchable_complete = searchable_complete and search_complete
            entities.append(EntityDescriptor(
                reference=model.model,
                label=model.label.strip()[:160],
                search_text=search_text,
                page_hints=tuple(EntityPageHint(
                    name=(item.name.strip() or model.label.strip())[:160],
                    breadcrumbs=tuple(crumb.strip()[:240]
                                      for crumb in item.breadcrumbs[:10]),
                    views=tuple(item.view_modes[:10]),
                ) for item in ordered[:10]),
            ))
        return EntityCatalog(
            binding_digest=catalog.descriptor_digest,
            entities=tuple(entities),
            complete=catalog.complete and searchable_complete,
            scope="current actor's readable, page-backed, non-sensitive business entities",
        )

    def fields(self, entity: EntityDescriptor, *, context=None) -> tuple[EntityField, ...]:
        payload = self._client.call(entity.reference, "fields_get", [], {"attributes": [
            "string", "type", "required", "readonly", "searchable", "store", "selection",
            "currency_field", "relation",
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
            currency_field = value.get("currency_field", "")
            if (mapped[0] != "monetary" or not isinstance(currency_field, str)
                    or not _FIELD_NAME.fullmatch(currency_field)
                    or currency_field not in payload
                    or not isinstance(payload[currency_field], dict)
                    or payload[currency_field].get("type") != "many2one"
                    or payload[currency_field].get("store") is not True):
                currency_field = ""
            result.append(EntityField(
                reference=name,
                label=" ".join(label.split()),
                kind=mapped[0],
                required=value.get("required") is True,
                readonly=value.get("readonly") is True,
                searchable=searchable,
                summable=(mapped[0] in {"integer", "number", "monetary"}
                          and value.get("store") is True
                          and (mapped[0] != "monetary" or bool(currency_field))),
                choices=_choices(value.get("selection"))
                if value.get("type") == "selection" else (),
                evidence_type=mapped[1],
                unit_field_reference=currency_field,
                related_entity_reference=(value["relation"]
                                          if mapped[0] == "reference"
                                          and isinstance(value.get("relation"), str)
                                          and _MODEL_NAME.fullmatch(value["relation"])
                                          and odoo_model_allowed(value["relation"])
                                          else ""),
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
        aggregates = []
        for field in request.aggregate_fields:
            grouping = [field.unit_field_reference] if field.unit_field_reference else []
            groups = self._client.call(model, "read_group", [
                domain, [f"{field.reference}:sum"], grouping,
            ], {"lazy": False})
            if not isinstance(groups, list) or len(groups) > 200:
                raise BusinessToolContractError("Odoo returned an invalid aggregate grouping")
            for group in groups:
                if not isinstance(group, dict):
                    raise BusinessToolContractError("Odoo returned an invalid aggregate group")
                count = group.get("__count")
                raw = group.get(field.reference)
                if (isinstance(count, bool) or not isinstance(count, int) or count <= 0
                        or isinstance(raw, bool) or not isinstance(raw, (int, float))
                        or not math.isfinite(float(raw))):
                    raise BusinessToolContractError("Odoo returned an invalid aggregate value")
                unit = ""
                if field.unit_field_reference:
                    currency = group.get(field.unit_field_reference)
                    if (not isinstance(currency, (list, tuple)) or len(currency) != 2
                            or isinstance(currency[0], bool)
                            or not isinstance(currency[0], int) or currency[0] <= 0
                            or not isinstance(currency[1], str) or not currency[1].strip()
                            or len(currency[1]) > _MAX_CURRENCY_CHARS
                            or _CONTROL.search(currency[1])):
                        raise BusinessToolContractError(
                            "Odoo returned an aggregate without its currency")
                    unit = currency[1].strip()
                aggregates.append(EntityAggregate(
                    field_reference=field.reference,
                    value=format(Decimal(str(raw)), "f"),
                    count=count, unit=unit,
                ))
        read_fields = list(dict.fromkeys(
            [field.reference for field in request.fields]
            + [field.unit_field_reference for field in request.fields
               if field.unit_field_reference]))
        rows = self._client.call(model, "search_read", [domain], {
            "fields": read_fields,
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
                unit = ""
                if field.unit_field_reference:
                    currency = row.get(field.unit_field_reference)
                    if field.unit_field_reference not in row:
                        raise BusinessToolContractError(
                            "Odoo omitted a requested monetary currency")
                    if currency not in (None, False):
                        if (not isinstance(currency, (list, tuple))
                                or len(currency) != 2
                                or isinstance(currency[0], bool)
                                or not isinstance(currency[0], int)
                                or not isinstance(currency[1], str)
                                or not currency[1].strip()
                                or len(currency[1]) > _MAX_CURRENCY_CHARS
                                or _CONTROL.search(currency[1])):
                            raise BusinessToolContractError(
                                "Odoo returned an invalid monetary currency")
                        unit = currency[1].strip()
                cells.append(EntityCell(
                    field_reference=field.reference,
                    value=_render_value(row[field.reference], field),
                    unit=unit,
                    related_record_id=(str(row[field.reference][0])
                                       if field.related_entity_reference
                                       and isinstance(row[field.reference], (list, tuple))
                                       else ""),
                ))
            records.append(EntityRecord(
                reference=f"{model}/{record_id}",
                display_id=str(record_id),
                cells=tuple(cells),
                route=_record_route(model, record_id),
            ))
        return EntityQueryPage(
            records=tuple(records),
            matched_count=matched,
            source_total=source_total,
            aggregates=tuple(aggregates),
            source=f"delegated application API ({request.entity.label})",
            scope=(f"{request.entity.label} records visible to the delegated actor; "
                   f"{len(domain)} AND filters; stable identity ascending")[:200],
        )


class OdooReferenceLookupPort:
    """Use the actor's native name search, without a customer-only domain or sudo."""

    def __init__(self, client):
        self._client = client

    def lookup(self, entity: EntityDescriptor, reference: str, *, limit: int,
               context=None) -> tuple[tuple[ReferenceMatch, ...], bool]:
        rows = self._client.call(entity.reference, "name_search", [reference], {
            "operator": "=", "limit": limit + 1,
        })
        if rows == []:
            rows = self._client.call(entity.reference, "name_search", [reference], {
                "operator": "ilike", "limit": limit + 1,
            })
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise BusinessToolContractError("Odoo returned invalid name-search matches")
        matches = []
        seen = set()
        for row in rows:
            if (not isinstance(row, (tuple, list)) or len(row) != 2
                    or isinstance(row[0], bool) or not isinstance(row[0], int)
                    or row[0] <= 0 or row[0] in seen
                    or not isinstance(row[1], str) or not row[1].strip()
                    or len(row[1]) > 200 or _CONTROL.search(row[1])):
                raise BusinessToolContractError("Odoo returned invalid name-search matches")
            seen.add(row[0])
            matches.append(ReferenceMatch(
                record_id=str(row[0]), label=" ".join(row[1].split()),
                route=_record_route(entity.reference, row[0]),
            ))
        return tuple(matches[:limit]), len(matches) > limit


def _record_route(model: str, record_id: int) -> str:
    """Odoo 18's own address for one record, `/odoo/<model>/<id>`, or none.

    The web client opens that address in the model's default form view for any actor who
    may read the record, whatever menu it sits under. A model name the route contract does
    not accept gets no route rather than failing the whole read.
    """
    route = f"/odoo/{model}/{record_id}"
    try:
        ODOO_ROUTE_PORT.validate_tool_route(route)
    except ValueError:
        return ""
    return route


__all__ = ["OdooEntityReadPort", "OdooReferenceLookupPort", "odoo_model_allowed"]
