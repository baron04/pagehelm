"""Translate authenticated, redacted native API rows into the common summary contract.

Only the adapter knows the published field types, clock and reviewed display units.
The model supplies a closed computation spec; it never supplies these rows or proofs.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re

from web_operation_agent.answer.evidence import Origin
from web_operation_agent.execution.read_summary import (
    Missing, RelationValue, SummaryColumn, SummaryRead, SummaryRefused, SummaryRow,
    SummarySpec, compute_summary,
)

from .api_content import REDACTED


_MONEY = re.compile(r"price|amount|money|balance|deposit|cost|income|expenditure|金额|单价|价格|余额|货币|人民币|(?:单位.{0,3})[元分]", re.I)
_TIME = re.compile(r"(?:Time|Date)$", re.I)


def _instant(value, zone):
    # The native adapter already declares epoch milliseconds for these timestamp keys.
    if type(value) is not int or not 10 ** 11 < value < 10 ** 13:
        raise SummaryRefused("invalid_date", "native timestamps require epoch milliseconds")
    return (datetime.fromtimestamp(value // 1000, timezone.utc)
            + timedelta(milliseconds=value % 1000)).astimezone(zone)


def bind_native_conditions(spec, filters, props, equalities):
    """Reapply proven native identity equalities before summarizing.

    A published parameter name alone proves no comparison semantics. Only a
    same-release compiled equality on a published, same-name identity column is
    eligible. Do not compare native raw money/date parameters to converted display
    values. Other native predicates retain their native handling; they are never
    guessed, dropped from the request, or treated as local proof.
    """
    if not equalities:
        return spec
    where = spec.to_json()["where"]
    for key, value in filters.items():
        if (equalities.get(key) != key or key not in props
                or not (key == "id" or key.endswith("Id"))
                or props[key].get("type") not in {"integer", "string"}
                or value is None or value == "" or isinstance(value, (list, dict))):
            continue
        if (props[key].get("type") == "integer" and isinstance(value, str)
                and re.fullmatch(r"[+-]?[0-9]+", value)):
            value = int(value)
        condition = {"column": key, "op": "=", "value": value}
        if condition not in where:
            where.append(condition)
    return SummarySpec.from_dict({**spec.to_json(), "where": where})


def summarize(*, path, filters, fields, props, deref, rows, total, whole,
              requests, actor_digest, zone, money_review, spec, columns=None, names=None,
              relation_names=None):
    """Use only the data the caller fetched; no lookup or guessed type conversion here.

    `relation_names` names a bare identity column through another read the caller
    made under the same actor ({column: {identity: name}}); it is display metadata and
    never a grouping key."""
    declared = spec if isinstance(spec, SummarySpec) else SummarySpec.from_dict(spec)
    if rows is None or any(not isinstance(row, dict) for row in rows):
        raise SummaryRefused("not_a_collection", "summary requires a native list read")
    if not fields or len(fields) > 12 or len(set(fields)) != len(fields):
        raise SummaryRefused("invalid_columns", "summary requires 1–12 distinct fields")
    policy = money_review.fields_for(path) if money_review is not None else {}
    display_names = columns if columns is not None else {}
    dictionaries = names if names is not None else {}
    named_relations = relation_names if relation_names is not None else {}
    if (not isinstance(display_names, dict) or not isinstance(dictionaries, dict)
            or not isinstance(named_relations, dict)):
        raise SummaryRefused("invalid_input", "native display metadata must be field-bound mappings")
    columns = []
    converters = []
    for key in fields:
        if key not in props:
            raise SummaryRefused("unpublished_column", "summary columns must be published by this endpoint")
        schema = deref(props[key])
        description = str(schema.get("description") or "")
        kind, unit, unverified = schema.get("type"), "", False
        money = policy.get(key)
        if money is not None:
            kind, unit = "number", money.display_unit
            def convert(value, money=money):
                if not money.convertible(value):
                    raise SummaryRefused("invalid_money", "reviewed money field has an unverified value type")
                return Decimal(str(value)) * money.factor
        elif _MONEY.search(key + " " + description) and kind in {"integer", "number"}:
            # No review has proven this amount's display unit: its raw value is shown
            # as the application returned it, marked, never converted or given a unit.
            kind, unverified = "number", True
            def convert(value):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise SummaryRefused("invalid_number", "numeric schema does not permit a text or boolean value")
                return Decimal(str(value))
        elif (key == "id" or key.endswith("Id")) and kind in {"integer", "string"}:
            kind = "relation"
            label_key = key[:-2] + "Name" if key != "id" else ""
            labels = {}
            # These native foreign-key/name pairs are returned together in the same
            # actor-authorized row. Labels are display metadata, never grouping keys.
            if isinstance(named_relations.get(key), dict):
                labels = {str(identity): str(label) for identity, label in named_relations[key].items()
                          if isinstance(label, str) and label and REDACTED not in label}
            elif (key in (*declared.group_by, *declared.columns) and label_key in props
                    and deref(props[label_key]).get("type") == "string"):
                for row in rows:
                    identity, label = row.get(key), row.get(label_key)
                    if identity is not None and isinstance(label, str) and label:
                        if REDACTED in label:
                            raise SummaryRefused("redacted_value", "a redacted relation label cannot be delivered exactly")
                        previous = labels.setdefault(str(identity), label)
                        if previous != label:
                            raise SummaryRefused("ambiguous_relation_label", "one native relation has inconsistent names")
            def convert(value, labels=labels):
                if isinstance(value, bool) or not isinstance(value, (str, int)) or value == "":
                    raise SummaryRefused("invalid_identity", "record references require literal identifiers")
                return RelationValue(str(value), labels.get(str(value), str(value)))
        elif _TIME.search(key) and kind in {"integer", "number"}:
            kind = "datetime"
            def convert(value):
                return _instant(value, zone)
        elif kind in {"integer", "number"}:
            kind = "number"
            def convert(value):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise SummaryRefused("invalid_number", "numeric schema does not permit a text or boolean value")
                try:
                    result = Decimal(str(value))
                except InvalidOperation as exc:
                    raise SummaryRefused("invalid_number", "invalid decimal value") from exc
                if not result.is_finite():
                    raise SummaryRefused("invalid_number", "non-finite decimal value")
                return result
        elif kind == "string" and schema.get("format") in {"date", "date-time"}:
            is_date = schema["format"] == "date"
            kind = "date" if is_date else "datetime"
            def convert(value, is_date=is_date, time_key=bool(_TIME.search(key))):
                if not is_date and time_key and type(value) is int:
                    return _instant(value, zone)
                if not isinstance(value, str):
                    raise SummaryRefused("invalid_date", "published dates require an ISO string")
                shape = r"\d{4}-\d{2}-\d{2}" if is_date else r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?"
                if not re.fullmatch(shape, value):
                    raise SummaryRefused("invalid_date", "published dates require the declared ISO precision")
                try:
                    result = date.fromisoformat(value) if is_date else datetime.fromisoformat(value)
                    # The application owns the interpretation of its local ISO timestamps.
                    if not is_date:
                        result = result.replace(tzinfo=zone) if result.tzinfo is None else result.astimezone(zone)
                    return result
                except ValueError as exc:
                    raise SummaryRefused("invalid_date", "published date value is not ISO formatted") from exc
        elif kind == "string":
            kind = "text"
            def convert(value):
                if not isinstance(value, str):
                    raise SummaryRefused("invalid_text", "text schema requires an actual string")
                return value
        elif kind == "boolean":
            def convert(value):
                if type(value) is not bool:
                    raise SummaryRefused("invalid_boolean", "boolean schema requires a boolean")
                return value
        else:
            raise SummaryRefused("unproven_type", "summary requires an explicit supported scalar field type")
        labels = dictionaries.get(key) if kind in {"number", "text", "boolean"} else None
        if labels is not None and not isinstance(labels, dict):
            raise SummaryRefused("invalid_input", "native dictionary display metadata requires code-label pairs")
        if labels is not None and any(isinstance(label, str) and REDACTED in label for label in labels.values()):
            raise SummaryRefused("redacted_value", "a redacted dictionary label cannot be delivered exactly")
        columns.append(SummaryColumn(key, kind, unit, display_names.get(key, ""),
            tuple((str(code), label) for code, label in labels.items()) if labels is not None else None,
            unverified_unit=unverified))
        converters.append(convert)
    actual = []
    for row in rows:
        identity = row.get("id")
        if isinstance(identity, bool) or not isinstance(identity, (str, int)) or identity == "":
            raise SummaryRefused("missing_identity", "summary rows require native record identifiers")
        cells = []
        for key, convert in zip(fields, converters):
            value = row.get(key, Missing.MISSING)
            if isinstance(value, str) and REDACTED in value:
                raise SummaryRefused("redacted_value", "redacted fields cannot be summarized")
            cells.append(value if value is Missing.MISSING or value is None or value == "" else convert(value))
        actual.append(SummaryRow(str(identity), tuple(cells)))
    read = SummaryRead(
        origin=Origin("typed", "/admin-api/" + path, datetime.now(timezone.utc).isoformat(),
            json.dumps(filters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        actor_digest=actor_digest, columns=tuple(columns), rows=tuple(actual),
        matched_count=total if type(total) is int and total >= 0 else len(actual),
        records_scanned=len(actual), complete=bool(whole and type(total) is int and total >= 0),
        consistency="多次分页读取；应用未提供同一时点快照。" if requests > 1 else "单次应用读取。",
    )
    return compute_summary(read, declared)
