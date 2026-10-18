"""Bounded calculations over one application-certified, actor-bound read.

Adapters supply values and their meaning; the model supplies only a closed spec.  This
module neither reads an application nor decides whether the spec matches a request.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum
from typing import Any

from ..answer.evidence import Origin

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}\Z")
_KINDS = frozenset({"text", "number", "boolean", "date", "datetime", "relation"})
_OPS = ("=", "!=", "contains", "prefix", "in", ">", ">=", "<", "<=", "between", "empty", "not_empty")
_MEASURES = ("count", "count_distinct", "sum", "min", "max")
MAX_ROWS = 5000
MAX_RESULT_CHARS = 100_000


class SummaryRefused(ValueError):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(detail)


class Missing(Enum):
    MISSING = "missing"


def _fail(code: str, detail: str):
    raise SummaryRefused(code, detail)


def _text(value: Any, name: str, limit=1000, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or not empty and not value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail("invalid_input", f"{name} is invalid")
    return value


def _name(value: Any):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        _fail("invalid_spec", "column names and measure aliases must be bounded identifiers")
    return value


@dataclass(frozen=True, slots=True)
class RelationValue:
    key: str
    label: str

    def __post_init__(self):
        _text(self.key, "relation key", 200)
        _text(self.label, "relation label", empty=True)


@dataclass(frozen=True, slots=True)
class SummaryColumn:
    name: str
    kind: str
    unit: str = ""

    def __post_init__(self):
        _name(self.name)
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            _fail("invalid_input", "unsupported column type")
        _text(self.unit, "column unit", 80, empty=True)
        if self.unit and self.kind != "number":
            _fail("invalid_input", "only numeric columns carry display units")


@dataclass(frozen=True, slots=True)
class SummaryRow:
    record_id: str
    values: tuple

    def __post_init__(self):
        _text(self.record_id, "record identity", 400)
        if not isinstance(self.values, tuple):
            _fail("invalid_input", "row values must be immutable")


def _empty(value):
    return value is None or value == ""


def _bounded_number(value):
    if type(value) is int:
        return value.bit_length() <= 426
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    return len(value.as_tuple().digits) <= 128 and -128 <= value.as_tuple().exponent <= 128 and value.adjusted() <= 128


def _valid_cell(column, value):
    if value is Missing.MISSING or _empty(value):
        return
    valid = {"text": lambda: isinstance(value, str),
             "number": lambda: _bounded_number(value),
             "boolean": lambda: type(value) is bool,
             "relation": lambda: isinstance(value, RelationValue),
             "date": lambda: type(value) is date or isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)),
             "datetime": lambda: isinstance(value, datetime) or isinstance(value, str) and bool(re.fullmatch(
                 r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})", value))}
    if not valid[column.kind]():
        _fail("type_mismatch", f"{column.name} has a value outside its declared type")
    if isinstance(value, str):
        _text(value, "cell", empty=True)
        try:
            if column.kind == "date":
                date.fromisoformat(value)
            if column.kind == "datetime":
                datetime.fromisoformat(value)
        except ValueError:
            _fail("type_mismatch", f"{column.name} has an invalid calendar value")
    if column.kind == "datetime":
        _moment(value)


@dataclass(frozen=True, slots=True)
class SummaryRead:
    origin: Origin
    actor_digest: str
    columns: tuple[SummaryColumn, ...]
    rows: tuple[SummaryRow, ...]
    matched_count: int
    records_scanned: int
    complete: bool
    consistency: str = ""

    def __post_init__(self):
        if not isinstance(self.origin, Origin) or self.origin.kind != "typed":
            _fail("invalid_input", "summary input must originate in an application read")
        _text(self.actor_digest, "actor binding", 200)
        _text(self.consistency, "read consistency", 1000, empty=True)
        if (not isinstance(self.columns, tuple) or not 1 <= len(self.columns) <= 20
                or any(not isinstance(c, SummaryColumn) for c in self.columns)
                or len({c.name for c in self.columns}) != len(self.columns)):
            _fail("invalid_input", "read columns must be unique and bounded")
        if (not isinstance(self.rows, tuple) or len(self.rows) > MAX_ROWS
                or any(not isinstance(row, SummaryRow) for row in self.rows)
                or len({row.record_id for row in self.rows}) != len(self.rows)):
            _fail("invalid_input", "read rows must have unique identities within the row budget")
        if (type(self.matched_count) is not int or self.matched_count < len(self.rows)
                or type(self.records_scanned) is not int or self.records_scanned < len(self.rows)
                or type(self.complete) is not bool
                or self.complete and len(self.rows) != self.matched_count):
            _fail("invalid_input", "read counts and completeness disagree")
        size = 0
        for row in self.rows:
            if len(row.values) != len(self.columns):
                _fail("missing_column", "every row must account for every published column")
            for column, value in zip(self.columns, row.values, strict=True):
                _valid_cell(column, value)
                size += len(str(value))
        if size > 2_000_000:
            _fail("input_budget", "summary input exceeds the bounded value budget")


@dataclass(frozen=True, slots=True)
class _Condition:
    column: str
    op: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class _Measure:
    op: str
    column: str
    alias: str


@dataclass(frozen=True, slots=True)
class _Top:
    by: str
    n: int
    direction: str


def _operand(value):
    if value is None or isinstance(value, (str, bool)) or type(value) is int and _bounded_number(value):
        if isinstance(value, str):
            _text(value, "condition operand", empty=True)
        return value
    if isinstance(value, (float, Decimal)) and not isinstance(value, bool):
        number = Decimal(str(value))
        if _bounded_number(number):
            return number
    _fail("invalid_spec", "condition operands must be finite scalar values")


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, RelationValue):
        return {"key": value.key, "label": value.label}
    if value is Missing.MISSING:
        return {"missing": True}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SummarySpec:
    where: tuple[_Condition, ...] = ()
    group_by: tuple[str, ...] = ()
    measures: tuple[_Measure, ...] = ()
    columns: tuple[str, ...] = ()
    top: _Top | None = None

    def __post_init__(self):
        if (not all(isinstance(value, tuple) for value in (self.where, self.group_by, self.measures, self.columns))
                or any(not isinstance(value, _Condition) for value in self.where)
                or any(not isinstance(value, _Measure) for value in self.measures)
                or self.top is not None and not isinstance(self.top, _Top)):
            _fail("invalid_spec", "spec components must be validated immutable values")
        if (len(self.where) > 32 or len(self.group_by) > 2 or len(self.columns) > 20 or len(self.measures) > 12
                or any(not isinstance(value, str) for value in (*self.group_by, *self.columns))
                or not self.measures and (not self.columns or self.group_by or self.top)
                or len(set(self.group_by)) != len(self.group_by) or len(set(self.columns)) != len(self.columns)):
            _fail("invalid_spec", "spec outputs must be bounded and unambiguous")
        for name in (*self.group_by, *self.columns):
            _name(name)
        aliases = []
        for measure in self.measures:
            _name(measure.alias)
            if measure.op not in _MEASURES or (measure.op == "count") != (measure.column == ""):
                _fail("invalid_spec", "invalid measure")
            if measure.column:
                _name(measure.column)
            aliases.append(measure.alias)
        if len(set(aliases)) != len(aliases) or set(aliases).intersection((*self.group_by, *self.columns)):
            _fail("invalid_spec", "measure names conflict")
        for condition in self.where:
            _name(condition.column)
            if condition.op not in _OPS:
                _fail("invalid_spec", "unsupported condition")
            if condition.op in {"in", "between"}:
                if (not isinstance(condition.value, tuple) or not 1 <= len(condition.value) <= 50
                        or condition.op == "between" and len(condition.value) != 2):
                    _fail("invalid_spec", "invalid condition operands")
                for value in condition.value:
                    _operand(value)
            elif condition.op not in {"empty", "not_empty"}:
                _operand(condition.value)
        if self.top and (type(self.top.n) is not int or not 1 <= self.top.n <= 50
                or self.top.by not in aliases or self.top.direction not in {"asc", "desc"}):
            _fail("invalid_spec", "invalid top definition")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {"where", "group_by", "measures", "columns", "top"}:
            _fail("invalid_spec", "summary requires a closed object")
        def entries(key, maximum):
            result = value.get(key, [])
            if not isinstance(result, list) or len(result) > maximum:
                _fail("invalid_spec", f"{key} must be a bounded array")
            return result
        conditions = []
        for item in entries("where", 32):
            if (not isinstance(item, dict) or set(item) - {"column", "op", "value"}
                    or not {"column", "op"} <= set(item) or item["op"] not in _OPS):
                _fail("invalid_spec", "invalid summary condition")
            op = item["op"]
            operand = item.get("value")
            if op in {"empty", "not_empty"}:
                if "value" in item:
                    _fail("invalid_spec", "empty conditions do not accept a value")
            elif "value" not in item:
                _fail("invalid_spec", "condition requires a value")
            elif op in {"in", "between"}:
                if (not isinstance(operand, list) or not 1 <= len(operand) <= 50
                        or op == "between" and len(operand) != 2):
                    _fail("invalid_spec", "in and between require bounded scalar arrays")
                operand = tuple(_operand(v) for v in operand)
            else:
                operand = _operand(operand)
            conditions.append(_Condition(_name(item["column"]), op, operand))
        grouping = tuple(_name(item) for item in entries("group_by", 2))
        columns = tuple(_name(item) for item in entries("columns", 20))
        measures = []
        for item in entries("measures", 12):
            if (not isinstance(item, dict) or set(item) - {"op", "column", "as"}
                    or not {"op", "as"} <= set(item) or item["op"] not in _MEASURES):
                _fail("invalid_spec", "invalid summary measure")
            if item["op"] == "count" and "column" in item or item["op"] != "count" and "column" not in item:
                _fail("invalid_spec", "count has no column; other measures require one")
            measures.append(_Measure(item["op"], _name(item["column"]) if "column" in item else "", _name(item["as"])))
        if (len(set(grouping)) != len(grouping) or len(set(columns)) != len(columns)
                or not measures and (not columns or grouping)
                or len({m.alias for m in measures}) != len(measures)
                or {m.alias for m in measures}.intersection((*grouping, *columns))):
            _fail("invalid_spec", "summary output names are ambiguous or no output was requested")
        top = None
        if "top" in value:
            item = value["top"]
            if (not isinstance(item, dict) or set(item) != {"by", "n", "direction"}
                    or type(item["n"]) is not int or not 1 <= item["n"] <= 50
                    or not isinstance(item["direction"], str) or item["direction"] not in {"asc", "desc"}
                    or not isinstance(item["by"], str)
                    or item["by"] not in {m.alias for m in measures}):
                _fail("invalid_spec", "top must name a measure, direction, and bounded position")
            top = _Top(item["by"], item["n"], item["direction"])
        return cls(tuple(conditions), grouping, tuple(measures), columns, top)

    def to_json(self):
        result = {"where": [{"column": c.column, "op": c.op,
            **({"value": _json_value(c.value)} if c.op not in {"empty", "not_empty"} else {})} for c in self.where],
            "group_by": list(self.group_by), "measures": [{"op": m.op,
                **({"column": m.column} if m.column else {}), "as": m.alias} for m in self.measures],
            "columns": list(self.columns)}
        if self.top:
            result["top"] = {"by": self.top.by, "n": self.top.n, "direction": self.top.direction}
        return result


def summary_schema():
    scalar = {"anyOf": [{"type": "string", "maxLength": 1000}, {"type": "number"},
                        {"type": "boolean"}, {"type": "null"}]}
    name = {"type": "string", "minLength": 1, "maxLength": 80}
    return {"type": "object", "additionalProperties": False, "properties": {
        "where": {"type": "array", "maxItems": 32,
            "description": "Conditions are ANDed. between includes both endpoints. For calendar periods use >= start and < next period start, preserving fractional seconds. Datetime thresholds require explicit timezone offsets.",
            "items": {"type": "object", "additionalProperties": False,
            "properties": {"column": name, "op": {"type": "string", "enum": list(_OPS)},
                "value": {"anyOf": [scalar, {"type": "array", "minItems": 1, "maxItems": 50, "items": scalar}]}},
            "required": ["column", "op"]}},
        "group_by": {"type": "array", "maxItems": 2, "items": name},
        "columns": {"type": "array", "maxItems": 20, "items": name},
        "measures": {"type": "array", "maxItems": 12, "items": {"type": "object", "additionalProperties": False,
            "properties": {"op": {"type": "string", "enum": list(_MEASURES)}, "column": name, "as": name},
            "required": ["op", "as"]}},
        "top": {"type": "object", "additionalProperties": False, "properties": {
            "by": name, "n": {"type": "integer", "minimum": 1, "maximum": 50},
            "direction": {"type": "string", "enum": ["asc", "desc"]}}, "required": ["by", "n", "direction"]}}}


def _identity(value):
    if isinstance(value, RelationValue):
        return ("relation", value.key)
    if type(value) is int or isinstance(value, Decimal):
        return ("number", Decimal(value))
    return (type(value).__name__, value)


def _display(value):
    if value is Missing.MISSING:
        _fail("missing_column", "an unread field cannot become a displayed value")
    if value is None:
        return None
    if isinstance(value, RelationValue):
        return f"{value.label} ({value.key})" if value.label and value.label != value.key else value.key
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _comparable(column, value):
    if column.kind == "number":
        if type(value) is not int and not isinstance(value, Decimal):
            _fail("type_mismatch", f"{column.name} requires a numeric operand")
        return Decimal(value)
    if column.kind == "relation":
        if isinstance(value, RelationValue):
            return value.key
        if isinstance(value, str):
            return value
        _fail("type_mismatch", "relation operands must use the exact key")
    if column.kind == "date":
        try:
            if type(value) is date:
                return value
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                _fail("type_mismatch", "date operand requires YYYY-MM-DD")
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            _fail("type_mismatch", "date operand is not an ISO date")
    if column.kind == "datetime":
        return _moment(value)
    if column.kind == "boolean" and type(value) is not bool or column.kind == "text" and not isinstance(value, str):
        _fail("type_mismatch", f"{column.name} operand does not match its declared type")
    return value


def _moment(value):
    try:
        if isinstance(value, str) and not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})", value):
            _fail("type_mismatch", "datetime requires an ISO timestamp with an explicit offset")
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if moment.tzinfo is None or moment.utcoffset() is None:
            _fail("type_mismatch", "datetime requires an explicit timezone offset")
        return moment
    except (TypeError, ValueError) as exc:
        if isinstance(exc, SummaryRefused):
            raise
        _fail("type_mismatch", "datetime operand is not an offset-qualified ISO timestamp")


def _validate_condition(column, condition):
    if condition.op in {"empty", "not_empty"}:
        return
    if condition.op in {"contains", "prefix"}:
        if column.kind != "text" or not isinstance(condition.value, str):
            _fail("type_mismatch", "text containment requires a text column and operand")
        return
    if condition.op in {">", ">=", "<", "<=", "between"} and column.kind not in {"number", "date", "datetime"}:
        _fail("type_mismatch", "ordered comparison requires a number or calendar column")
    if condition.value is None:
        if condition.op not in {"=", "!="}:
            _fail("type_mismatch", "null requires equality or an empty check")
        return
    operands = condition.value if condition.op in {"in", "between"} else (condition.value,)
    comparable = tuple(_comparable(column, value) for value in operands)
    if condition.op == "between" and comparable[0] > comparable[1]:
        _fail("invalid_spec", "between lower bound exceeds upper bound")


def _matches(column, value, condition):
    if value is Missing.MISSING:
        _fail("missing_column", "a condition cannot decide from a field that was not read")
    if condition.op in {"empty", "not_empty"}:
        return _empty(value) == (condition.op == "empty")
    if value is None:
        if condition.op in {"=", "!="} and condition.value is None:
            return condition.op == "="
        return False
    if condition.value is None:
        if condition.op in {"=", "!="}:
            return condition.op == "!="
        _fail("type_mismatch", "null only supports equality or empty checks")
    if value == "" and column.kind != "text":
        # A known blank numeric/calendar field has no ordered value.  Its blankness
        # remains separately observable through empty/not_empty, never coerced to zero.
        return False
    if condition.op in {"contains", "prefix"}:
        if column.kind != "text" or not isinstance(condition.value, str):
            _fail("type_mismatch", "text containment requires text values and operands")
        return condition.value in value if condition.op == "contains" else value.startswith(condition.value)
    left = _comparable(column, value)
    if condition.op == "in":
        return left in tuple(_comparable(column, item) for item in condition.value)
    if condition.op == "between":
        lower, upper = (_comparable(column, item) for item in condition.value)
        try:
            if lower > upper:
                _fail("invalid_spec", "between lower bound exceeds upper bound")
            return lower <= left <= upper
        except TypeError:
            _fail("type_mismatch", "timestamp operands use incompatible timezone precision")
    right = _comparable(column, condition.value)
    if condition.op in {"=", "!="}:
        return (left == right) == (condition.op == "=")
    if column.kind not in {"number", "date", "datetime"}:
        _fail("type_mismatch", "ordered comparison requires a number or declared calendar type")
    try:
        return {">": lambda: left > right, ">=": lambda: left >= right,
                "<": lambda: left < right, "<=": lambda: left <= right}[condition.op]()
    except TypeError:
        _fail("type_mismatch", "timestamp operands use incompatible timezone precision")


def _counts(rows, columns):
    return tuple((column.name,
        sum(row.values[index] is Missing.MISSING for row in rows),
        sum(row.values[index] is None for row in rows),
        sum(row.values[index] == "" for row in rows)) for index, column in enumerate(columns))


def _group_key(column, value):
    """Machine identity is independent of a relation's display label and numeric scale."""
    if value is Missing.MISSING:
        _fail("missing_column", "an unread field has no group identity")
    if value is None or value == "":
        return value
    if column.kind == "relation":
        return value.key if isinstance(value, RelationValue) else value
    if column.kind == "number":
        number = Decimal(value)
        if not number:
            return "0"
        with localcontext() as context:
            context.prec = 512
            return format(number.normalize(), "f")
    if column.kind == "date":
        return _comparable(column, value).isoformat()
    if column.kind == "datetime":
        return _comparable(column, value).astimezone(timezone.utc).isoformat()
    return _display(value)


@dataclass(frozen=True, slots=True)
class SummaryResult:
    origin: Origin
    actor_digest: str
    spec: SummarySpec
    source_count: int
    source_matched_count: int
    source_scanned: int
    matched_count: int
    columns: tuple[SummaryColumn, ...]
    rows: tuple[tuple[str | None, ...], ...]
    matched_record_ids: tuple[str, ...]
    missing_counts: tuple[tuple[str, int], ...]
    null_counts: tuple[tuple[str, int], ...]
    empty_counts: tuple[tuple[str, int], ...]
    group_stats: tuple
    source_complete: bool
    result_truncated: bool
    incomplete_reason: str = ""
    consistency: str = ""
    tie_cutoff: str | None = None
    ties_at_cutoff: int = 0
    source_columns: tuple[SummaryColumn, ...] = ()
    group_keys: tuple[tuple[str | None, ...], ...] = ()
    row_record_ids: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self):
        if not isinstance(self.origin, Origin) or self.origin.kind != "typed" or not isinstance(self.spec, SummarySpec):
            _fail("invalid_result", "summary result must retain a validated read origin and spec")
        if (not isinstance(self.source_columns, tuple) or not 1 <= len(self.source_columns) <= 20
                or any(not isinstance(column, SummaryColumn) for column in self.source_columns)
                or len({column.name for column in self.source_columns}) != len(self.source_columns)):
            _fail("invalid_result", "summary result must retain bounded immutable source column semantics")
        source_columns = {column.name: column for column in self.source_columns}
        referenced = {*self.spec.group_by, *self.spec.columns, *(condition.column for condition in self.spec.where),
            *(measure.column for measure in self.spec.measures if measure.column)}
        if referenced - source_columns.keys():
            _fail("invalid_result", "summary spec references an unpublished source column")
        _text(self.actor_digest, "result actor binding", 200)
        _text(self.consistency, "result consistency", 1000, empty=True)
        _text(self.incomplete_reason, "result reason", 1000, empty=True)
        counts = (self.source_count, self.source_matched_count, self.source_scanned, self.matched_count, self.ties_at_cutoff)
        if (any(type(count) is not int or count < 0 for count in counts)
                or self.source_count > MAX_ROWS or self.source_count > self.source_matched_count
                or self.source_scanned < self.source_count or self.matched_count > self.source_count
                or type(self.source_complete) is not bool or type(self.result_truncated) is not bool
                or self.source_complete and self.source_count != self.source_matched_count):
            _fail("invalid_result", "summary counts and source completeness disagree")
        if (not isinstance(self.columns, tuple) or not 1 <= len(self.columns) <= 34
                or any(not isinstance(column, SummaryColumn) for column in self.columns)
                or len({column.name for column in self.columns}) != len(self.columns)):
            _fail("invalid_result", "summary output columns must be unique immutable columns")
        expected_columns = tuple(dict.fromkeys((*self.spec.group_by, *self.spec.columns))) + tuple(
            measure.alias for measure in self.spec.measures)
        if tuple(column.name for column in self.columns) != expected_columns:
            _fail("invalid_result", "summary output columns must match the declared projection")
        count_names = {measure.alias for measure in self.spec.measures if measure.op in {"count", "count_distinct"}}
        if any(column.name in count_names and (column.kind != "number" or column.unit) for column in self.columns):
            _fail("invalid_result", "count measures must be numeric and unitless")
        measures = {measure.alias: measure for measure in self.spec.measures}
        for column in self.columns:
            measure = measures.get(column.name)
            if measure is None:
                if column != source_columns[column.name]:
                    _fail("invalid_result", "displayed source column semantics changed")
            elif measure.op not in {"count", "count_distinct"}:
                source = source_columns[measure.column]
                if column.kind != source.kind or column.unit != source.unit:
                    _fail("invalid_result", "computed column must retain its proven source type and unit")
        if (not isinstance(self.rows, tuple) or len(self.rows) > MAX_ROWS
                or any(not isinstance(row, tuple) or len(row) != len(self.columns) for row in self.rows)
                or any(value is not None and not isinstance(value, str) for row in self.rows for value in row)):
            _fail("invalid_result", "summary output values must be immutable strings or null")
        for row in self.rows:
            for column, value in zip(self.columns, row, strict=True):
                if value is not None:
                    _text(value, "result cell", 2000, empty=True)
                    if column.kind == "number" and value != "":
                        try:
                            number = Decimal(value)
                            if (not number.is_finite() or len(number.as_tuple().digits) > 512
                                    or not -512 <= number.as_tuple().exponent <= 512):
                                _fail("invalid_result", "numeric summary result must be finite and bounded")
                        except InvalidOperation:
                            _fail("invalid_result", "numeric summary result is not a decimal")
        if sum(len(value or "") for row in self.rows for value in row) > MAX_RESULT_CHARS:
            _fail("invalid_result", "summary output exceeds the result budget")
        if (not isinstance(self.matched_record_ids, tuple) or len(self.matched_record_ids) != self.matched_count
                or any(not isinstance(value, str) for value in self.matched_record_ids)
                or len(set(self.matched_record_ids)) != len(self.matched_record_ids)):
            _fail("invalid_result", "summary membership must have one immutable identity per selected row")
        for identity in self.matched_record_ids:
            _text(identity, "summary member identity", 400)
        if (not isinstance(self.group_keys, tuple) or len(self.group_keys) != len(self.rows)
                or any(not isinstance(key, tuple) or len(key) != len(self.spec.group_by) for key in self.group_keys)
                or any(value is not None and not isinstance(value, str) for key in self.group_keys for value in key)):
            _fail("invalid_result", "machine group keys must align with every output row")
        for key in self.group_keys:
            for name, value in zip(self.spec.group_by, key, strict=True):
                if value is None:
                    continue
                _text(value, "machine group key", 1000, empty=True)
                column = source_columns[name]
                try:
                    parsed = Decimal(value) if column.kind == "number" and value != "" else value
                    if column.kind == "number" and value != "" and (not parsed.is_finite()
                            or len(parsed.as_tuple().digits) > 256 or parsed.as_tuple().exponent < -128
                            or parsed.adjusted() > 128):
                        _fail("invalid_result", "numeric group identity must be finite and bounded")
                    if column.kind == "boolean" and value not in {"true", "false", ""}:
                        _fail("invalid_result", "boolean group identity must preserve its type")
                    if column.kind != "boolean" and _group_key(column, parsed) != value:
                        _fail("invalid_result", "machine group identity is not canonical")
                except (InvalidOperation, SummaryRefused) as exc:
                    if isinstance(exc, SummaryRefused) and exc.code == "invalid_result":
                        raise
                    _fail("invalid_result", "machine group identity does not match its source type")
        if sum(len(value or "") for key in self.group_keys for value in key) > MAX_RESULT_CHARS:
            _fail("invalid_result", "machine group keys exceed their bounded identity budget")
        if self.spec.group_by and len(set(self.group_keys)) != len(self.group_keys):
            _fail("invalid_result", "an aggregate group cannot occur twice")
        if (not isinstance(self.row_record_ids, tuple) or len(self.row_record_ids) != len(self.rows)
                or any(not isinstance(members, tuple) or len(members) > MAX_ROWS for members in self.row_record_ids)
                or sum(len(members) for members in self.row_record_ids) > MAX_ROWS
                or any(not isinstance(identity, str) for members in self.row_record_ids for identity in members)):
            _fail("invalid_result", "native row membership must align with every output row")
        flattened = tuple(identity for members in self.row_record_ids for identity in members)
        if (len(flattened) > MAX_ROWS or len(set(flattened)) != len(flattened)
                or not set(flattened) <= set(self.matched_record_ids)):
            _fail("invalid_result", "output rows cannot repeat or invent native record membership")
        for identity in flattened:
            _text(identity, "output native identity", 400)
        if (not self.spec.measures and any(len(members) != 1 for members in self.row_record_ids)
                or self.spec.group_by and any(not members for members in self.row_record_ids)):
            _fail("invalid_result", "list rows and nonempty groups must retain their native members")
        if self.source_complete and not self.result_truncated and not self.spec.top:
            if set(flattened) != set(self.matched_record_ids):
                _fail("invalid_result", "unranked output must cover the entire selected population")
        names = None
        for gaps in (self.missing_counts, self.null_counts, self.empty_counts):
            if (not isinstance(gaps, tuple) or len(gaps) > 20
                    or any(not isinstance(item, tuple) or len(item) != 2 for item in gaps)):
                _fail("invalid_result", "value gap counts must be immutable and bounded")
            current = tuple(item[0] for item in gaps)
            for name, count in gaps:
                _name(name)
                if type(count) is not int or not 0 <= count <= self.matched_count:
                    _fail("invalid_result", "value gap counts exceed their population")
            if len(set(current)) != len(current) or names is not None and current != names:
                _fail("invalid_result", "value gap columns must be unique and agree")
            names = current
        if names != tuple(source_columns):
            _fail("invalid_result", "value gap counts must account for every source column")
        if any(sum(values) > self.matched_count for values in zip(
                (count for _, count in self.missing_counts), (count for _, count in self.null_counts),
                (count for _, count in self.empty_counts), strict=True)):
            _fail("invalid_result", "missing, null and empty populations must be disjoint")
        if not isinstance(self.group_stats, tuple) or len(self.group_stats) > MAX_ROWS:
            _fail("invalid_result", "group statistics must be immutable and bounded")
        grouped_count = 0
        for group in self.group_stats:
            if (not isinstance(group, tuple) or len(group) != 3 or not isinstance(group[0], tuple)
                    or len(group[0]) != len(self.spec.group_by)
                    or any(value is not None and not isinstance(value, str) for value in group[0])
                    or type(group[1]) is not int or not 0 <= group[1] <= self.matched_count
                    or not isinstance(group[2], tuple) or len(group[2]) != len(names or ())):
                _fail("invalid_result", "invalid group statistics")
            grouped_count += group[1]
            for expected_name, item in zip(names or (), group[2], strict=True):
                if (not isinstance(item, tuple) or len(item) != 4 or item[0] != expected_name
                        or any(type(count) is not int or count < 0 for count in item[1:])
                        or sum(item[1:]) > group[1]):
                    _fail("invalid_result", "group gap counts exceed their population")
        if grouped_count > self.matched_count:
            _fail("invalid_result", "group statistics repeat selected records")
        if ((not self.source_complete or self.result_truncated) and (self.rows or not self.incomplete_reason)
                or not self.source_complete and (self.matched_count or self.matched_record_ids)):
            _fail("invalid_result", "incomplete results cannot publish partial calculations")
        if self.source_complete and not self.result_truncated:
            if not self.spec.measures and len(self.rows) != self.matched_count:
                _fail("invalid_result", "a complete list must contain every selected record")
            if self.spec.measures and len(self.rows) != len(self.group_stats):
                _fail("invalid_result", "aggregate rows and group statistics disagree")
            if self.spec.measures and any(len(members) != group[1] for members, group in zip(
                    self.row_record_ids, self.group_stats, strict=True)):
                _fail("invalid_result", "group population must match its native record members")
            if self.spec.measures and not self.spec.top and grouped_count != self.matched_count:
                _fail("invalid_result", "aggregate groups do not cover every selected record")
        if self.tie_cutoff is not None:
            _text(self.tie_cutoff, "ranking cutoff", 200)
            if self.spec.top is None or self.ties_at_cutoff < 1:
                _fail("invalid_result", "ranking cutoff requires a declared ranking and tied population")
        elif self.ties_at_cutoff:
            _fail("invalid_result", "tied population requires a cutoff")

    @property
    def complete(self):
        return self.source_complete and not self.result_truncated

    @property
    def records(self):
        return self.rows

    def to_json(self):
        return {"schema_version": 1, "kind": "read_summary", "origin": {
            "kind": self.origin.kind, "reference": self.origin.reference,
            "observed_at": self.origin.observed_at, "scope": self.origin.scope},
            "actor_digest": self.actor_digest, "spec": self.spec.to_json(),
            "source_count": self.source_count, "source_matched_count": self.source_matched_count,
            "source_scanned": self.source_scanned, "matched_count": self.matched_count,
            "columns": [{"name": c.name, "kind": c.kind, "unit": c.unit} for c in self.columns],
            "source_columns": [{"name": c.name, "kind": c.kind, "unit": c.unit} for c in self.source_columns],
            "rows": [list(row) for row in self.rows], "matched_record_ids": list(self.matched_record_ids),
            "group_keys": [list(key) for key in self.group_keys],
            "row_record_ids": [list(members) for members in self.row_record_ids],
            "missing_counts": dict(self.missing_counts), "null_counts": dict(self.null_counts),
            "empty_counts": dict(self.empty_counts), "group_stats": _json_value(self.group_stats),
            "source_complete": self.source_complete, "result_truncated": self.result_truncated,
            "complete": self.complete, "incomplete_reason": self.incomplete_reason,
            "consistency": self.consistency, "tie_cutoff": self.tie_cutoff,
            "ties_at_cutoff": self.ties_at_cutoff}

    @property
    def content_digest(self):
        return hashlib.sha256(json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _measure(column, rows, index, measure):
    if measure.op == "count":
        return len(rows)
    values = [row.values[index] for row in rows]
    if any(v is Missing.MISSING for v in values):
        _fail("missing_column", f"{measure.column} was not read for every matching record")
    if measure.op == "count_distinct":
        return len({_identity(v) for v in values if v is not None})
    if column.kind != "number" and measure.op == "sum":
        _fail("type_mismatch", "sum requires a declared numeric column")
    if column.kind not in {"number", "date", "datetime"}:
        _fail("type_mismatch", "extremes require numbers or declared calendar values")
    values = [value for value in values if not _empty(value)]
    if not values:
        return 0 if not rows and measure.op == "sum" else None
    comparable = [_comparable(column, value) for value in values]
    if measure.op == "sum":
        with localcontext() as context:
            context.prec = 512
            return sum(comparable, Decimal(0))
    try:
        chosen = (min if measure.op == "min" else max)(range(len(comparable)), key=comparable.__getitem__)
    except TypeError:
        _fail("type_mismatch", "calendar values use incompatible timezone precision")
    return comparable[chosen]


def compute_summary(read: SummaryRead, spec: SummarySpec):
    if not isinstance(read, SummaryRead) or not isinstance(spec, SummarySpec):
        _fail("invalid_input", "summary needs validated read and spec objects")
    lookup = {column.name: index for index, column in enumerate(read.columns)}
    referenced = {*spec.group_by, *spec.columns, *(c.column for c in spec.where),
                  *(m.column for m in spec.measures if m.column)}
    if referenced - lookup.keys():
        _fail("unknown_column", "summary references a column outside this published read")
    for condition in spec.where:
        _validate_condition(read.columns[lookup[condition.column]], condition)
    for measure in spec.measures:
        if measure.op in {"sum", "min", "max"}:
            kind = read.columns[lookup[measure.column]].kind
            if kind not in ({"number"} if measure.op == "sum" else {"number", "date", "datetime"}):
                _fail("type_mismatch", "measure does not apply to this published column type")
    names = tuple(dict.fromkeys((*spec.group_by, *spec.columns)))
    output_columns = tuple(read.columns[lookup[name]] for name in names) + tuple(
        SummaryColumn(m.alias, "number" if m.op in {"count", "count_distinct", "sum"} else read.columns[lookup[m.column]].kind,
            read.columns[lookup[m.column]].unit if m.op in {"sum", "min", "max"} else "") for m in spec.measures)
    selected = tuple(row for row in read.rows if all(_matches(read.columns[lookup[c.column]], row.values[lookup[c.column]], c)
        for c in spec.where)) if read.complete else ()
    stats = _counts(selected, read.columns)
    groups = {}
    if spec.measures:
        for row in selected:
            values = tuple(row.values[lookup[name]] for name in spec.group_by)
            if Missing.MISSING in values:
                _fail("missing_column", "an unread relation cannot be grouped")
            key = tuple(_group_key(read.columns[lookup[name]], value)
                        for name, value in zip(spec.group_by, values, strict=True))
            groups.setdefault(key, []).append(row)
        if not spec.group_by:
            groups[()] = list(selected)
    output, raw_measures, group_stats, group_keys, row_record_ids = [], [], [], [], []
    if read.complete and spec.measures:
        for members in groups.values():
            display = []
            for name in names:
                values = [row.values[lookup[name]] for row in members]
                unique = {_group_key(read.columns[lookup[name]], value) for value in values}
                if len(unique) > 1:
                    _fail("ambiguous_projection", "a grouped display column must have one exact value per group")
                if any(isinstance(value, RelationValue) for value in values) and len({value.label for value in values}) > 1:
                    _fail("ambiguous_projection", "one relation key has inconsistent labels in this read")
                display.append(_display(values[0]) if values else None)
            measures = tuple(_measure(read.columns[lookup[m.column]] if m.column else None,
                members, lookup.get(m.column, 0), m) for m in spec.measures)
            output.append(tuple(display) + tuple(_display(v) for v in measures))
            raw_measures.append(measures)
            group_stats.append((tuple(display[:len(spec.group_by)]), len(members), _counts(members, read.columns)))
            group_keys.append(tuple(_group_key(read.columns[lookup[name]], members[0].values[lookup[name]])
                                    for name in spec.group_by))
            row_record_ids.append(tuple(row.record_id for row in members))
    elif read.complete:
        output = [tuple(_display(row.values[lookup[name]]) for name in names) for row in selected]
        group_keys = [() for row in selected]
        row_record_ids = [(row.record_id,) for row in selected]
    cutoff, ties = None, 0
    if read.complete and spec.top and output:
        index = next(i for i, measure in enumerate(spec.measures) if measure.alias == spec.top.by)
        values = [measures[index] for measures in raw_measures]
        if any(value is None for value in values):
            _fail("unrankable_result", "a missing measure cannot be silently ranked or discarded")
        ordered = sorted(range(len(output)), key=values.__getitem__, reverse=spec.top.direction == "desc")
        boundary = values[ordered[min(spec.top.n, len(ordered)) - 1]]
        kept = [i for i in ordered if values[i] >= boundary] if spec.top.direction == "desc" else [i for i in ordered if values[i] <= boundary]
        output, group_stats = [output[i] for i in kept], [group_stats[i] for i in kept]
        group_keys, row_record_ids = [group_keys[i] for i in kept], [row_record_ids[i] for i in kept]
        cutoff, ties = _display(boundary), sum(value == boundary for value in values)
    reason = "" if read.complete else "The application did not certify a complete read; no summary was calculated."
    truncated = sum(len(value or "") for row in output for value in row) > MAX_RESULT_CHARS
    if truncated:
        output = []
        group_keys, row_record_ids = [], []
        reason = "The complete summary exceeds the bounded result budget; no partial result was delivered."
    if read.complete and selected and spec.measures and any(value is None for row in raw_measures for value in row):
        # Known empty values are not unread values.  Their absence is reported explicitly.
        reason = reason or "Some measures have no nonempty values; their result is null, not numeric zero."
    return SummaryResult(read.origin, read.actor_digest, spec, len(read.rows), read.matched_count,
        read.records_scanned, len(selected), output_columns, tuple(output), tuple(row.record_id for row in selected),
        tuple((name, missing) for name, missing, _, _ in stats),
        tuple((name, nulls) for name, _, nulls, _ in stats),
        tuple((name, empty) for name, _, _, empty in stats), tuple(group_stats),
        read.complete, truncated, reason, read.consistency, cutoff, ties, source_columns=read.columns,
        group_keys=tuple(group_keys), row_record_ids=tuple(row_record_ids))


def _escape(value):
    # Preserve literal business values; avoid Markdown table grammar entirely when unsafe.
    return re.sub(r"([\\`*\[\]<>_])", r"\\\1", value)


def _shown_operand(value):
    if isinstance(value, (tuple, list)):
        return ", ".join(_shown_operand(item) for item in value)
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "; ".join(f"{key}={_shown_operand(item)}" for key, item in value.items())
    return str(value)


def _measure_label(measure, chinese):
    labels = {"count": ("记录数", "Record count"), "count_distinct": ("去重数量", "distinct count"),
              "sum": ("合计", "sum"), "min": ("最小值", "minimum"), "max": ("最大值", "maximum")}
    label = labels[measure.op][0 if chinese else 1]
    return measure.column + (" " if measure.column else "") + label


def render_summary(result: SummaryResult, *, max_chars: int, user_text: str = ""):
    from ..answer.answering import StructuredAnswerRendering
    if not isinstance(result, SummaryResult) or type(max_chars) is not int or max_chars < 512:
        _fail("invalid_input", "summary rendering requires a result and at least 512 characters")
    chinese = bool(re.search(r"[\u3400-\u9fff]", user_text))
    headline = "## 汇总结果" if chinese else "## Summary result"
    count_line = (f"读取 {result.source_count} 行；条件命中 {result.matched_count} 行。" if chinese else
                  f"Read {result.source_count} rows; selected {result.matched_count} rows.")
    lines = [headline, count_line]
    if not result.complete:
        lines.append("未完整交付。" if chinese else "The result was not completely delivered.")
    if result.incomplete_reason:
        lines.append(_escape(result.incomplete_reason))
    conditions = []
    source_columns = {column.name: column for column in result.source_columns}
    operators = {"=": "为", "!=": "不为", "contains": "包含", "prefix": "前缀为", "in": "属于",
                 ">": "大于", ">=": "不少于", "<": "小于", "<=": "不超过", "between": "范围为",
                 "empty": "为空", "not_empty": "非空"}
    for condition in result.spec.where:
        source = source_columns[condition.column]
        shown = condition.column + (f" ({source.unit})" if source.unit else "") + " " + (operators[condition.op] if chinese else condition.op)
        if condition.op not in {"empty", "not_empty"}:
            shown += " " + _shown_operand(condition.value)
        conditions.append(shown)
    lines.append(("计算条件：" if chinese else "Calculation conditions: ") + _escape(
        ("；" if chinese else "; ").join(conditions) if conditions else ("不再附加筛选" if chinese else "no additional filter")))
    if result.spec.group_by:
        lines.append(("分组：" if chinese else "Grouped by: ") + _escape(", ".join(result.spec.group_by)))
    if result.spec.measures:
        lines.append(("统计：" if chinese else "Measures: ") + _escape(
            "; ".join(_measure_label(measure, chinese) for measure in result.spec.measures)))
    if result.spec.top:
        measure = next(measure for measure in result.spec.measures if measure.alias == result.spec.top.by)
        direction = ("降序" if result.spec.top.direction == "desc" else "升序") if chinese else result.spec.top.direction
        lines.append((f"名次：按 {_measure_label(measure, chinese)} {direction}取前 {result.spec.top.n} 个位置，保留临界值全部并列。"
            if chinese else f"Ranking: {_measure_label(measure, chinese)} {direction}, first {result.spec.top.n} positions including all cutoff ties."))
    lines.append("")
    measure_labels = {measure.alias: _measure_label(measure, chinese) for measure in result.spec.measures}
    headings = tuple(measure_labels.get(column.name, column.name) + (f" ({column.unit})" if column.unit else "") for column in result.columns)
    table = not any("|" in value for value in headings) and not any(
        value is not None and "|" in value for row in result.rows for value in row)
    if result.rows and table:
        lines += ["| " + " | ".join(_escape(name) for name in headings) + " |",
                  "| " + " | ".join("---:" if c.kind == "number" else "---" for c in result.columns) + " |"]
        lines += ["| " + " | ".join(_escape(value) if value is not None else "—" for value in row) + " |" for row in result.rows]
    elif result.rows:
        lines += [f"{i}. " + "; ".join(_escape(name) + ": " + (_escape(value) if value is not None else "—")
            for name, value in zip(headings, row, strict=True)) for i, row in enumerate(result.rows, 1)]
    else:
        lines.append("（无结果行）" if chinese else "(no result rows)")
    missing = [(name, missing, dict(result.null_counts)[name], dict(result.empty_counts)[name])
               for name, missing in result.missing_counts if missing or dict(result.null_counts)[name] or dict(result.empty_counts)[name]]
    if missing:
        lines.append(("缺值统计（未读/null/空串）：" if chinese else "Value gaps (unread/null/empty): ")
            + _escape("; ".join(f"{name}: {unread}/{nulls}/{empty}" for name, unread, nulls, empty in missing)))
    if result.tie_cutoff is not None:
        lines.append(("临界值/并列组数：" if chinese else "Cutoff / tied groups: ") + _escape(result.tie_cutoff) + f" / {result.ties_at_cutoff}")
    scope = result.origin.scope
    try:
        parsed = json.loads(scope)
        if isinstance(parsed, dict):
            scope = _shown_operand(parsed)
    except ValueError:
        pass
    lines += ["", ("> 来源：" if chinese else "> Source: ") + _escape(result.origin.reference)
        + " · " + _escape(scope) + " · " + _escape(result.origin.observed_at)]
    if result.consistency:
        lines.append(_escape(result.consistency))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = headline + "\n\n" + ("完整结果超出交付预算，未交付部分结果。" if chinese else
            "The complete result exceeds the delivery budget; no partial rows were delivered.")
        return StructuredAnswerRendering(text, 0, True)
    return StructuredAnswerRendering(text, len(result.rows), not result.complete)
