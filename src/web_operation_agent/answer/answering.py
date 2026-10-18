"""Validated, application-supplied projections for authoritative query answers."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass


_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
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
    actor_digest: str = ""
    record_ids: tuple[str, ...] | None = None

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
        if not isinstance(self.records, tuple) or len(self.records) > 5000:
            raise ValueError("query answer records are invalid")
        normalized_records: list[tuple[str, ...]] = []
        characters = 0
        for record in self.records:
            if not isinstance(record, tuple) or len(record) != len(self.fields):
                raise ValueError("query answer record does not match its fields")
            normalized_records.append(tuple(
                _bounded_text(value, "query answer value", 1_000, allow_empty=True)
                for value in record))
            characters += sum(len(value) for value in normalized_records[-1])
            if characters > 100_000:
                raise ValueError("query answer values exceed the bounded collection budget")
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
        if (self.record_ids is None) != (self.actor_digest == ""):
            raise ValueError("native identities require an actor binding, and vice versa")
        if self.record_ids is not None:
            if (not isinstance(self.actor_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", self.actor_digest)
                    or not isinstance(self.record_ids, tuple) or len(self.record_ids) != len(self.records)
                    or any(not isinstance(key, str) or not key or len(key) > 400
                           or _CONTROL.search(key) or key != key.strip() for key in self.record_ids)
                    or len(set(self.record_ids)) != len(self.record_ids)):
                raise ValueError("native identities must be literal, unique, immutable and aligned with rows")

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
            **({"identity": {"actor_digest": self.actor_digest, "record_ids": list(self.record_ids)}}
               if self.record_ids is not None else {}),
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


_NUMERIC = re.compile(r"^-?[\d,]*\d(?:\.\d+)?%?$")


def _literal(value: str) -> str:
    """Application values are visible text, not Markdown commands or links."""
    return re.sub(r"([\\`*\[\]<>])", r"\\\1", value)


def _alignment(values: list[str]) -> str:
    """Right-align a column only when every value in it reads as a number."""
    shown = [value for value in values if value]
    return "---:" if shown and all(_NUMERIC.fullmatch(value) for value in shown) else "---"


def _markdown_table(fields: tuple[str, ...],
                    records: tuple[tuple[str, ...], ...]) -> tuple[str, str, list[str]] | None:
    """Header, delimiter and one line per record -- or ``None`` if a table would lie.

    The panel's table grammar has no escape for a cell separator, so a value containing
    one cannot be put in a cell without changing it. Rewriting business data to fit a
    layout is not a trade this function is allowed to make: it declines, and the caller
    falls back to the labelled-list form, which has no such character.
    """
    if any("|" in field for field in fields):
        return None
    if any("|" in value for record in records for value in record):
        return None
    columns = [[record[index] for record in records] for index in range(len(fields))]
    header = "| " + " | ".join(_literal(field) for field in fields) + " |"
    delimiter = "| " + " | ".join(_alignment(column) for column in columns) + " |"
    rows = ["| " + " | ".join(_literal(value) if value else "—" for value in record) + " |"
            for record in records]
    return header, delimiter, rows


def _labelled_rows(fields: tuple[str, ...],
                   records: tuple[tuple[str, ...], ...]) -> list[str]:
    return [f"{index}. " + "; ".join(f"{_literal(field)}: {_literal(value) if value else '—'}"
                                     for field, value in zip(fields, record))
            for index, record in enumerate(records, 1)]


def render_structured_query_answer(
        projection: StructuredQueryAnswer, *, user_text: str,
        max_chars: int) -> StructuredAnswerRendering:
    """Render trusted facts and make result limits explicit when they exist.

    The facts are laid out, not narrated: a heading that says what was read and how much
    of it, a table of the records, and the provenance as a footer. Every one of those was
    a run-on line of prose before, which is a poor way to read sixteen sales orders and a
    worse way to compare two of them.
    """
    if not isinstance(projection, StructuredQueryAnswer):
        raise TypeError("projection must be a StructuredQueryAnswer")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 512:
        raise ValueError("structured answer max_chars must be at least 512")
    chinese = bool(_HAN.search(user_text if isinstance(user_text, str) else ""))
    total = len(projection.records)
    known_total = (str(projection.source_total)
                   if projection.source_total is not None else ("未知" if chinese else "unknown"))
    if chinese:
        head = [f"## 查询结果：{_literal(projection.collection)}（{total} 条）"]
        if not projection.complete:
            limit = ("源扫描已完成，但返回结果受限" if projection.source_complete
                     else "结果可能不完整")
            head.append(
                f"{limit}：返回 {total}/{projection.matched_count} 条匹配记录；"
                f"来源总数 {known_total}；扫描记录数 "
                f"{projection.records_scanned if projection.records_scanned is not None else '未知'}。")
            if projection.incomplete_reason:
                head.append(f"原因：{_literal(projection.incomplete_reason)}。")
        footer = (f"> 数据来源：{_literal(projection.source)} · 范围：{_literal(projection.scope)}"
                  f" · 采集时间：{_literal(projection.observed_at)}")
        suffix_template = "显示限制：本答案列出 {shown}/{total} 条工具返回记录。"
        empty_note = "（无记录）"
    else:
        record_word = "record" if total == 1 else "records"
        head = [f"## Query result: {_literal(projection.collection)} ({total} {record_word})"]
        if not projection.complete:
            limit = ("The source scan completed, but the returned result is limited"
                     if projection.source_complete else "The result may be incomplete")
            head.append(
                f"{limit}: returned {total}/{projection.matched_count} matching records; "
                f"source total {known_total}; scanned "
                f"{projection.records_scanned if projection.records_scanned is not None else 'unknown'} records.")
            if projection.incomplete_reason:
                head.append(f"Reason: {_literal(projection.incomplete_reason)}.")
        footer = (f"> Source: {_literal(projection.source)} · scope: {_literal(projection.scope)}"
                  f" · observed: {_literal(projection.observed_at)}")
        suffix_template = "Display limit: this answer lists {shown}/{total} tool-returned records."
        empty_note = "(no records)"

    prefix = "\n".join(head)
    table = _markdown_table(projection.fields, projection.records)
    # Two lines of grammar before the first row when a table is possible; the labelled
    # list has none, and the two cost the same to bound.
    opening = [prefix, "", *(table[:2] if table else [])]
    row_lines = list(table[2]) if table else _labelled_rows(
        projection.fields, projection.records)

    def assemble(rows: list[str], suffix: str = "") -> str:
        # A table header with nothing under it states a shape, not a fact; with no rows
        # the answer says so in words instead.
        body = [*opening, *rows] if rows else [prefix, "", empty_note]
        tail = [suffix, footer] if suffix else [footer]
        return "\n".join([*body, "", *tail])

    complete_text = assemble(row_lines)
    if len(complete_text) <= max_chars:
        return StructuredAnswerRendering(complete_text, total, False)

    selected: list[str] = []
    for row in row_lines:
        candidate = assemble(
            [*selected, row], suffix_template.format(shown=len(selected) + 1, total=total))
        if len(candidate) > max_chars:
            break
        selected.append(row)
    suffix = suffix_template.format(shown=len(selected), total=total)
    rendered = assemble(selected, suffix)
    if len(rendered) > max_chars:
        rendered = "\n".join([prefix, "", suffix, footer])
    if len(rendered) > max_chars:
        rendered = "\n".join([prefix, "", suffix])
    if len(rendered) > max_chars:
        rendered = ("结构化查询结果超过配置的显示上限。" if chinese
                    else "The structured query result exceeds the configured display limit.")
    return StructuredAnswerRendering(rendered, len(selected), True)
