"""Validated, application-supplied projections for authoritative query answers."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass


_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _bounded_text(value: object, name: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = " ".join(value.split())
    invisible = any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in text)
    if ((not text and not allow_empty) or len(text) > limit
            or _CONTROL.search(text) or invisible):
        raise ValueError(f"{name} is invalid")
    return text


@dataclass(frozen=True, slots=True)
class StructuredQueryAnswer:
    """Immutable public facts produced by a typed read tool, never by the model.

    Records use positional values so callers cannot mutate a nested dictionary after the
    result crossed the application boundary. ``to_json`` is the only public projection.
    """

    collection: str
    fields: tuple[str, ...]
    records: tuple[tuple[str, ...], ...]
    source: str
    scope: str
    observed_at: str
    source_complete: bool
    result_truncated: bool
    matched_count: int
    source_total: int | None
    records_scanned: int | None
    incomplete_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "collection", _bounded_text(
            self.collection, "query collection", 100))
        object.__setattr__(self, "source", _bounded_text(
            self.source, "query source", 200))
        object.__setattr__(self, "scope", _bounded_text(
            self.scope, "query scope", 200))
        object.__setattr__(self, "observed_at", _bounded_text(
            self.observed_at, "query observed_at", 100))
        object.__setattr__(self, "incomplete_reason", _bounded_text(
            self.incomplete_reason, "query incomplete_reason", 500, allow_empty=True))
        if (not isinstance(self.fields, tuple) or not self.fields
                or len(self.fields) > 20 or len(set(self.fields)) != len(self.fields)
                or any(not isinstance(field, str) or not _FIELD.fullmatch(field)
                       for field in self.fields)):
            raise ValueError("query answer fields are invalid")
        if not isinstance(self.records, tuple) or len(self.records) > 100:
            raise ValueError("query answer records are invalid")
        normalized_records: list[tuple[str, ...]] = []
        for record in self.records:
            if not isinstance(record, tuple) or len(record) != len(self.fields):
                raise ValueError("query answer record does not match its fields")
            normalized_records.append(tuple(
                _bounded_text(value, "query answer value", 1_000, allow_empty=True)
                for value in record))
        object.__setattr__(self, "records", tuple(normalized_records))
        if not isinstance(self.source_complete, bool) or not isinstance(
                self.result_truncated, bool):
            raise ValueError("query answer completeness flags must be booleans")
        for name, value in (("matched_count", self.matched_count),
                            ("source_total", self.source_total),
                            ("records_scanned", self.records_scanned)):
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, int) or value < 0):
                raise ValueError(f"query answer {name} is invalid")
        if len(self.records) > self.matched_count:
            raise ValueError("query answer returned more records than it matched")
        if len(self.records) < self.matched_count and not self.result_truncated:
            raise ValueError("an omitted matching record must mark the query result truncated")
        if self.source_total is not None and self.matched_count > self.source_total:
            raise ValueError("query answer matched_count exceeds the known source total")
        if not self.source_complete and not self.result_truncated:
            raise ValueError("an incomplete source must mark the query result truncated")
        if not self.source_complete and not self.incomplete_reason:
            raise ValueError("an incomplete source requires a bounded reason")

    @property
    def complete(self) -> bool:
        return self.source_complete and not self.result_truncated

    def to_json(self) -> dict:
        return {
            "schema_version": 1,
            "kind": "query_collection",
            "collection": self.collection,
            "fields": list(self.fields),
            "records": [dict(zip(self.fields, values)) for values in self.records],
            "source": {
                "label": self.source,
                "scope": self.scope,
                "observed_at": self.observed_at,
            },
            "completeness": {
                "complete": self.complete,
                "source_complete": self.source_complete,
                "result_truncated": self.result_truncated,
                "returned_count": len(self.records),
                "matched_count": self.matched_count,
                "source_total": self.source_total,
                "records_scanned": self.records_scanned,
                "incomplete_reason": self.incomplete_reason,
            },
        }

    @property
    def content_digest(self) -> str:
        raw = json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class StructuredAnswerRendering:
    text: str
    presented_records: int
    presentation_truncated: bool


def render_structured_query_answer(
        projection: StructuredQueryAnswer, *, user_text: str,
        max_chars: int) -> StructuredAnswerRendering:
    """Render trusted facts without allowing an answer limit to hide completeness."""
    if not isinstance(projection, StructuredQueryAnswer):
        raise TypeError("projection must be a StructuredQueryAnswer")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 512:
        raise ValueError("structured answer max_chars must be at least 512")
    chinese = bool(_HAN.search(user_text if isinstance(user_text, str) else ""))
    total = len(projection.records)
    known_total = (str(projection.source_total)
                   if projection.source_total is not None else ("未知" if chinese else "unknown"))
    if chinese:
        completeness = (
            "完整" if projection.complete else
            "源扫描完整，但返回结果受限" if projection.source_complete else "不完整")
        lines = [
            f"查询结果：{projection.collection}",
            (f"完整性：{completeness}；返回 {total}/{projection.matched_count} 条匹配记录；"
             f"来源总数 {known_total}；扫描记录数 "
             f"{projection.records_scanned if projection.records_scanned is not None else '未知'}。"),
            f"数据来源：{projection.source}；范围：{projection.scope}；采集时间：{projection.observed_at}。",
        ]
        if projection.incomplete_reason:
            lines.append(f"不完整原因：{projection.incomplete_reason}。")
        lines.append("记录：")
        suffix_template = "显示限制：本答案列出 {shown}/{total} 条工具返回记录。"
    else:
        completeness = (
            "complete" if projection.complete else
            "source scan complete, returned result limited" if projection.source_complete
            else "incomplete")
        lines = [
            f"Query result: {projection.collection}",
            (f"Completeness: {completeness}; returned {total}/{projection.matched_count} matching "
             f"records; source total {known_total}; scanned "
             f"{projection.records_scanned if projection.records_scanned is not None else 'unknown'} records."),
            f"Source: {projection.source}; scope: {projection.scope}; observed: {projection.observed_at}.",
        ]
        if projection.incomplete_reason:
            lines.append(f"Incomplete reason: {projection.incomplete_reason}.")
        lines.append("Records:")
        suffix_template = "Display limit: this answer lists {shown}/{total} tool-returned records."

    prefix = "\n".join(lines)
    row_lines = []
    for index, values in enumerate(projection.records, 1):
        parts = [f"{field}: {value or '—'}"
                 for field, value in zip(projection.fields, values)]
        row_lines.append(f"{index}. " + "; ".join(parts))
    complete_text = "\n".join([prefix, *row_lines])
    if not row_lines:
        complete_text += "\n" + ("（无记录）" if chinese else "(no records)")
    if len(complete_text) <= max_chars:
        return StructuredAnswerRendering(complete_text, total, False)

    selected: list[str] = []
    for row in row_lines:
        shown = len(selected) + 1
        suffix = suffix_template.format(shown=shown, total=total)
        candidate = "\n".join([prefix, *selected, row, suffix])
        if len(candidate) > max_chars:
            break
        selected.append(row)
    suffix = suffix_template.format(shown=len(selected), total=total)
    rendered = "\n".join([prefix, *selected, suffix])
    if len(rendered) > max_chars:
        rendered = "\n".join([lines[0], lines[1], suffix])
    if len(rendered) > max_chars:
        rendered = ("结构化查询结果超过配置的显示上限。" if chinese
                    else "The structured query result exceeds the configured display limit.")
    return StructuredAnswerRendering(rendered, len(selected), True)
