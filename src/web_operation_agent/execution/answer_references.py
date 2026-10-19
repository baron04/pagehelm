"""Answers whose facts the program fills in from this turn's reads.

The model writes the sentence. A value it states is a reference to a read result this
turn holds -- ``{r1.total}``, ``{r1.scanned}``, ``{r1.name}``, ``{r1#3.name}``, ``{r1.表}`` -- and the program
renders it from the read itself, with the display metadata the adapter supplied
(dictionary names, reviewed units, relation names). The text outside references is read
by the program, not by another model: a number the person never said, or a read value
copied instead of referenced, is sent back once; after that the program answers with
the read results themselves. Nothing here ends a turn as a refusal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ..answer.answering import StructuredQueryAnswer, render_structured_query_answer
from .read_summary import (SummaryResult, _label_key, _measure_label, render_summary, summary_conditions,
                           summary_ordering)

REFERENCE = re.compile(r"\{(r\d{1,3})(?:#(\d{1,5}))?\.([^{}\s]{1,80})\}")
_LOOSE = re.compile(r"\{\s*r\d")
_TABLE = frozenset({"表", "table"})
_TOTAL = frozenset({"total", "命中数"})
_SCANNED = frozenset({"scanned", "读取行数"})
#: Calendar dates stay whole, so that today's date does not allow its month as a count;
#: thousands separators belong to their number.
_NUMBER = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日"
                     r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
#: A person's 万 and 亿 are the same numbers written out ("100万" is 1000000).
_SCALED = re.compile(r"(\d+(?:\.\d+)?)\s*(万|亿)")
_SCALE = {"万": Decimal(10_000), "亿": Decimal(100_000_000)}
_LIST_MARKER = re.compile(r"(?m)^[ \t]*(?:[-*][ \t]+)?\d{1,3}[.、)）][ \t]*")
_RELATION = re.compile(r"(.*) \(([^()]*)\)\Z", re.S)
_INSTANT = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\Z")
_MAX_REFERENCES = 12
_COPY_ROWS, _COPY_VALUES, _COPY_CHARS = 200, 5000, 4


def _fence(text: str) -> str:
    text = re.sub(r"(BEGIN|END)_UNTRUSTED_", lambda m: m.group(1) + "-UNTRUSTED-", text)
    return f"BEGIN_UNTRUSTED_APPLICATION_DATA\n{text}\nEND_UNTRUSTED_APPLICATION_DATA"


@dataclass(frozen=True, slots=True)
class ReferenceTable:
    """One read result as prose references see it: column keys and titles, and each
    cell's display value (None for an empty one).

    `total` is the number of records meeting the read's conditions, `scanned` the rows the
    read actually fetched, and `meaning` the read's conditions and order in the program's
    words, so a reference is chosen for what it means rather than for its shape."""
    columns: tuple[tuple[str, str], ...]
    rows: tuple[tuple[str | None, ...], ...]
    total: int | None
    complete: bool
    scanned: int | None = None
    meaning: str = ""

    def __post_init__(self):
        if (not isinstance(self.columns, tuple) or not 1 <= len(self.columns) <= 40
                or any(not isinstance(c, tuple) or len(c) != 2 or not all(isinstance(v, str) and v for v in c)
                       for c in self.columns)
                or len({key for key, _title in self.columns}) != len(self.columns)
                or not isinstance(self.rows, tuple) or len(self.rows) > 5000
                or any(not isinstance(row, tuple) or len(row) != len(self.columns)
                       or any(v is not None and not isinstance(v, str) for v in row) for row in self.rows)
                or self.total is not None and (type(self.total) is not int or self.total < 0)
                or type(self.complete) is not bool
                or self.scanned is not None and (type(self.scanned) is not int or self.scanned < 0)
                or not isinstance(self.meaning, str) or len(self.meaning) > 600):
            raise ValueError("invalid reference table")

    def column(self, name: str) -> int | None:
        for index, (key, title) in enumerate(self.columns):
            if name in (key, title):
                return index
        return None


def _prose(column, value, chinese: bool):
    """A certified cell as a sentence states it, never a different value."""
    if value is None or value == "":
        return None
    if column.labels is not None:
        labels = {_label_key(column.kind, code): label for code, label in column.labels}
        label = labels.get(_label_key(column.kind, value))
        if label is not None:
            return label
        return f"{value}（字典中无此代码）" if chinese else f"{value} (code absent from dictionary)"
    if column.kind == "relation":
        # A reference to an identity column states the identity; its name rides along.
        match = _RELATION.fullmatch(value)
        return f"{match.group(1)}（{match.group(2)}）" if match and match.group(1) else value
    if column.kind == "datetime":
        match = _INSTANT.fullmatch(value)
        return f"{match.group(1)} {match.group(2)}" if match else value
    if column.kind == "boolean":
        return {"true": "是" if chinese else "yes", "false": "否" if chinese else "no"}.get(value, value)
    if column.kind == "number":
        if column.unverified_unit:
            return f"{value}（单位未核验）" if chinese else f"{value} (unit unverified)"
        if column.unit:
            return value + column.unit if re.match(r"[㐀-鿿]", column.unit) else f"{value} {column.unit}"
    return value


def table_from(projection, *, chinese: bool = True) -> ReferenceTable:
    """The prose view of a summary or of an exact list delivery."""
    if isinstance(projection, SummaryResult):
        sources = {column.name: column for column in projection.source_columns}
        measures = {m.alias: m for m in projection.spec.measures}
        titles = tuple((column.name, _measure_label(measures[column.name], chinese, sources)
                        if column.name in measures else column.display_name or column.name)
                       for column in projection.columns)
        rows = tuple(tuple(_prose(column, value, chinese) for column, value in zip(projection.columns, row, strict=True))
                     for row in projection.rows)
        total = (projection.matched_count if projection.complete
                 else projection.source_matched_count if not projection.spec.where else None)
        return ReferenceTable(_unique(titles), rows, total, projection.complete,
                              projection.source_count, _summary_meaning(projection, chinese))
    if isinstance(projection, StructuredQueryAnswer):
        return ReferenceTable(_unique(tuple((name, name) for name in projection.fields)),
                              tuple(tuple(value if value != "" else None for value in row) for row in projection.records),
                              projection.matched_count if projection.complete else None, projection.complete,
                              projection.records_scanned)
    raise TypeError("references are made from summary results or exact list deliveries")


def _summary_meaning(projection, chinese):
    """What a summary's count and row numbers mean: its conditions and its order."""
    parts = []
    conditions = summary_conditions(projection, chinese)
    query = projection.origin.scope
    if query and query not in {"{}", "[]"}:
        parts.append(("应用查询参数 " if chinese else "application query ") + query[:240])
    if conditions:
        parts.append(("条件 " if chinese else "conditions ") + "；".join(conditions))
    ordering = summary_ordering(projection, chinese)
    if ordering:
        parts.append(("按 " + "；".join(ordering) + " 排列") if chinese else "ordered by " + "; ".join(ordering))
    elif projection.spec.top is not None:
        parts.append("按名次排列" if chinese else "ordered by rank")
    else:
        parts.append("未按任何列排序，行号只是读取顺序，不代表大小或先后" if chinese
                     else "not ordered by any column; row numbers are reading order, not rank")
    return ("；" if chinese else "; ").join(parts)[:600]


def _unique(columns):
    seen, out = set(), []
    for key, title in columns:
        title = title if title not in seen else key
        seen.update((key, title))
        out.append((key, title))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Composition:
    text: str
    problems: tuple[str, ...]
    used: tuple[str, ...]


class TurnReferences:
    """The read results one turn may cite, numbered r1, r2... in the order they were read."""

    def __init__(self):
        self._items: dict[str, tuple[ReferenceTable, object]] = {}
        self._sequence = 0

    def __bool__(self):
        return bool(self._items)

    def __contains__(self, reference):
        return reference in self._items

    def tables(self):
        """References whose whole result the program can deliver as a table."""
        return tuple(ref for ref, (_table, projection) in self._items.items()
                     if isinstance(projection, (SummaryResult, StructuredQueryAnswer)))

    def projection(self, reference):
        return self._items[reference][1]

    def add(self, table: ReferenceTable, projection=None) -> str:
        if not isinstance(table, ReferenceTable):
            raise TypeError("a reference holds a reference table")
        self._sequence += 1
        reference = f"r{self._sequence}"
        self._items[reference] = (table, projection)
        while len(self._items) > _MAX_REFERENCES:
            del self._items[next(iter(self._items))]
        return reference

    def describe(self, reference: str, *, preview: int = 10) -> str:
        """What the model is told of a new reference: its columns and first rows."""
        table, _projection = self._items[reference]
        columns = "、".join(key if key == title else f"{key}={title}" for key, title in table.columns[:20])
        counts = "；".join(
            ([f"{{{reference}.total}}={table.total}，满足这次读取条件的记录数"] if table.total is not None else [])
            + ([f"{{{reference}.scanned}}={table.scanned}，这次实际读到的行数"] if table.scanned is not None else []))
        head = (f"读取编号 {reference}：" + (f"{table.meaning}。" if table.meaning else "")
                + f"结果 {len(table.rows)} 行"
                + ("" if table.complete else "，未读全（不能引用单元格，只能引用读取的条数）")
                + f"；列：{columns}。答复里的数据写成引用，程序按读取结果填入："
                + (counts + "；" if counts else "")
                + f"{{{reference}.列}}（单行时）、{{{reference}#行号.列}}、{{{reference}.表}}。"
                  "只引用意思和你的说法相同的值；没有意思相同的引用，就不说这个数。")
        if not table.rows:
            return head
        lines = [f"#{index} " + " | ".join("—" if value is None else value for value in row)
                 for index, row in enumerate(table.rows[:preview], 1)]
        more = f"\n其余 {len(table.rows) - preview} 行未在此预览；行号顺序与读取结果一致。" if len(table.rows) > preview else ""
        return head + "\n前几行（行号从1起）：\n" + _fence("\n".join(lines)) + more

    def _render_table(self, reference, *, chinese, max_chars, user_text):
        table, projection = self._items[reference]
        if isinstance(projection, SummaryResult):
            rendering = render_summary(projection, max_chars=max_chars, user_text=user_text)
        elif isinstance(projection, StructuredQueryAnswer):
            rendering = render_structured_query_answer(projection, user_text=user_text, max_chars=max_chars)
        else:
            return _markdown(table, chinese)
        return None if rendering.presentation_truncated else rendering.text

    def compose(self, answer: str, *, said=(), chinese: bool = True, max_chars: int = 8000,
                user_text: str = "", check: bool = True) -> Composition:
        """Fill every reference from the reads and read what the model wrote around them."""
        problems, used, pieces, last = [], [], [], 0
        for match in REFERENCE.finditer(answer):
            reference, row, field = match.group(1), match.group(2), match.group(3)
            pieces.append(answer[last:match.start()])
            last = match.end()
            value = self._resolve(reference, row, field, problems, chinese=chinese,
                                  max_chars=max_chars, user_text=user_text)
            if value is not None:
                used.append(reference)
            pieces.append(value if value is not None else match.group(0))
        pieces.append(answer[last:])
        literal = REFERENCE.sub(" ", answer)
        if _LOOSE.search(literal):
            problems.append("有引用格式不对：写成 {r编号.列}、{r编号#行号.列}、{r编号.total}、{r编号.scanned} 或 {r编号.表}，花括号里不要空格。")
        if check:
            problems += self._unbound(literal, said)
        text = "".join(pieces)
        if len(text) > max_chars:
            problems.append(f"填入后的答复超过 {max_chars} 字；只引用用户要的内容。")
        return Composition(text, tuple(dict.fromkeys(problems)), tuple(dict.fromkeys(used)))

    def _resolve(self, reference, row, field, problems, *, chinese, max_chars, user_text):
        if reference not in self._items:
            known = "、".join(self._items) or "无"
            problems.append(f"引用 {{{reference}.{field}}} 不存在；本轮的读取编号：{known}。")
            return None
        table, _projection = self._items[reference]
        if field in _TABLE:
            if row is not None or not table.complete:
                problems.append(f"{reference} 没有读全，不能作为完整结果交付；缩小范围重读，或如实说明。")
                return None
            rendered = self._render_table(reference, chinese=chinese, max_chars=max_chars, user_text=user_text)
            if rendered is None:
                problems.append(f"{reference} 的完整表格超出答复长度，不能截断交付；缩小范围或如实说明。")
            return rendered
        if field in _SCANNED and row is None:
            if table.scanned is None:
                problems.append(f"{reference} 没有可核实的读取行数。")
                return None
            return str(table.scanned)
        if field in _TOTAL and row is None:
            if table.total is None:
                problems.append(f"{reference} 没有可核实的命中数。")
                return None
            return str(table.total)
        index = table.column(field)
        if index is None:
            names = "、".join(key for key, _title in table.columns[:20])
            problems.append(f"{reference} 没有列“{field}”；可用的列：{names}。")
            return None
        if not table.complete:
            problems.append(f"{reference} 没有读全，不能引用其中的单元格；只能引用 {{{reference}.total}}。")
            return None
        if row is None:
            if len(table.rows) != 1:
                problems.append(f"{reference} 有 {len(table.rows)} 行，写成 {{{reference}#行号.{field}}}。")
                return None
            position = 0
        else:
            position = int(row) - 1
            if not 0 <= position < len(table.rows):
                problems.append(f"{reference} 没有第 {row} 行（共 {len(table.rows)} 行）。")
                return None
        value = table.rows[position][index]
        return value if value is not None else ("空" if chinese else "empty")

    def _unbound(self, literal, said):
        """Numbers the person never said, and read values copied rather than referenced."""
        allowed = _said_numbers(said)
        words = " ".join(str(text) for text in said)
        bare = _LIST_MARKER.sub("", literal)
        stray = [token for token in _NUMBER.findall(bare) if _number_key(token) not in allowed]
        problems = []
        if stray:
            shown = "、".join(dict.fromkeys(stray[:6]))
            problems.append(f"答复中的数字“{shown}”没有引用来源：有意思相同的引用就写成引用；"
                            "没有的，连同这句说法一起删去，不要换成意思不同的引用。")
        copied, budget = [], _COPY_VALUES
        for table, _projection in reversed(list(self._items.values())):
            for row in table.rows[:_COPY_ROWS]:
                for value in row:
                    budget -= 1
                    if (value and len(value) >= _COPY_CHARS and value in bare and value not in words
                            and value not in copied):
                        copied.append(value)
                if budget <= 0:
                    break
            if budget <= 0:
                break
        if copied:
            shown = "”“".join(copied[:4])
            problems.append(f"“{shown}”是读取结果里的值，写成引用，不要照抄。")
        return problems

    @staticmethod
    def withhold(answer: str, *, said=(), chinese: bool = True) -> str:
        """With no read to answer from: the model's sentence, its unsourced numbers withheld."""
        allowed = _said_numbers(said)
        markers = {match.start() for match in _LIST_MARKER.finditer(answer)}
        def keep(match):
            line_start = answer.rfind("\n", 0, match.start()) + 1
            prefix = answer[line_start:match.start()]
            if _number_key(match.group(0)) in allowed or (line_start in markers and not prefix.strip(" \t-*")):
                return match.group(0)
            return "（未核实）" if chinese else "(unverified)"
        return _NUMBER.sub(keep, REFERENCE.sub("", answer))

    def fallback(self, references=(), *, chinese=True, max_chars=8000, user_text="") -> str:
        """The program's own answer: the read results themselves, without the model's prose."""
        chosen = [ref for ref in references if ref in self._items] or list(self._items)[-2:]
        parts = []
        for reference in chosen:
            table, _projection = self._items[reference]
            if table.complete:
                rendered = self._render_table(reference, chinese=chinese, max_chars=max_chars, user_text=user_text)
            else:
                rendered = None
            if rendered is None:
                rendered = (f"（{reference}：命中 {table.total}，未读全，未列出记录）" if table.total is not None
                            else f"（{reference}：未读全，未列出记录）")
            parts.append(rendered)
        if not parts:
            return ""
        head = "以下是本轮实际读取的结果：" if chinese else "These are the results this turn actually read:"
        text = head
        for part in parts:
            if len(text) + len(part) + 2 > max_chars:
                break
            text += "\n\n" + part
        return text


def _number_key(token: str) -> str:
    """One spelling per number: separators and trailing zeros do not make another value.

    A leading zero belongs to a code ("0017" is not "000017"), so such a token stays literal."""
    if not re.fullmatch(r"[\d,]+(?:\.\d+)?", token) or re.match(r"0\d", token):
        return token
    try:
        value = Decimal(token.replace(",", ""))
    except InvalidOperation:
        return token
    return format(value.normalize(), "f") if value else "0"


def _said_numbers(said) -> set[str]:
    allowed = set()
    for text in said:
        text = str(text)
        allowed.update(_number_key(token) for token in _NUMBER.findall(text))
        allowed.update(_number_key(str(Decimal(amount) * _SCALE[unit])) for amount, unit in _SCALED.findall(text))
    return allowed


def _markdown(table: ReferenceTable, chinese: bool) -> str:
    def cell(value):
        return "—" if value is None else re.sub(r"([\\`*\[\]<>_|])", r"\\\1", value)
    lines = ["| " + " | ".join(cell(title) for _key, title in table.columns) + " |",
             "| " + " | ".join("---" for _ in table.columns) + " |"]
    lines += ["| " + " | ".join(cell(value) for value in row) + " |" for row in table.rows[:50]]
    if len(table.rows) > 50:
        lines.append(f"（另有 {len(table.rows) - 50} 行未列出）" if chinese else f"({len(table.rows) - 50} more rows)")
    return "\n".join(lines)


__all__ = ["Composition", "REFERENCE", "ReferenceTable", "TurnReferences", "table_from"]
