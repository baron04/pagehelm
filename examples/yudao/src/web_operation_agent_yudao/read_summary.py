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


def summarize(*, path, filters, fields, props, deref, rows, total, whole,
              requests, actor_digest, zone, money_review, spec, columns=None, names=None):
    """Use only the data this call fetched; no extra lookup or guessed type conversion."""
    declared = spec if isinstance(spec, SummarySpec) else SummarySpec.from_dict(spec)
    if rows is None or any(not isinstance(row, dict) for row in rows):
        raise SummaryRefused("not_a_collection", "summary requires a native list read")
    if not fields or len(fields) > 12 or len(set(fields)) != len(fields):
        raise SummaryRefused("invalid_columns", "summary requires 1–12 distinct fields")
    policy = money_review.fields_for(path) if money_review is not None else {}
    display_names = columns if columns is not None else {}
    dictionaries = names if names is not None else {}
    if not isinstance(display_names, dict) or not isinstance(dictionaries, dict):
        raise SummaryRefused("invalid_input", "native display metadata must be field-bound mappings")
    columns = []
    converters = []
    for key in fields:
        if key not in props:
            raise SummaryRefused("unpublished_column", "summary columns must be published by this endpoint")
        schema = deref(props[key])
        description = str(schema.get("description") or "")
        kind, unit = schema.get("type"), ""
        money = policy.get(key)
        if money is not None:
            kind, unit = "number", money.display_unit
            def convert(value, money=money):
                if not money.convertible(value):
                    raise SummaryRefused("invalid_money", "reviewed money field has an unverified value type")
                return Decimal(str(value)) * money.factor
        elif _MONEY.search(key + " " + description):
            raise SummaryRefused("unverified_money", "money summary requires the existing per-field display review")
        elif (key == "id" or key.endswith("Id")) and kind in {"integer", "string"}:
            kind = "relation"
            label_key = key[:-2] + "Name" if key != "id" else ""
            labels = {}
            # These native foreign-key/name pairs are returned together in the same
            # actor-authorized row. Labels are display metadata, never grouping keys.
            if (key in (*declared.group_by, *declared.columns) and label_key in props
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
            tuple((str(code), label) for code, label in labels.items()) if labels is not None else None))
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
