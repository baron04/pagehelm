"""Application-neutral list default-order enforcement and ordinal reference binding."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


#: Query parameter names that override an endpoint's default sort order.
SORT_QUERY_PARAM_KEYS: frozenset[str] = frozenset({
    "order",
    "ordertype",
    "orderby",
    "sort",
    "sortasc",
    "sortby",
    "sortfield",
    "sorttype",
    "sortingfield",
    "sortingfields",
    "asc",
    "desc",
    "direction",
})

_SORT_PARAM_PREFIXES: tuple[str, ...] = (
    "sortingfield.",
    "sortingfields.",
    "orderby",
    "sortfield",
    "sortby",
)

#: Unit nouns commonly used for items in a delivered list.
_ITEM_UNIT = r"(?:个|条|位|台|辆|单|笔|份|家|张|件|行|项|名|人|款|批|套|组)"

#: Explicit user requests to sort, rank by metric, or pick newest/extremum rows.
_EXPLICIT_SORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"排序|排一下序|重新排|升序|降序|正序|倒序|逆序|顺排|倒排"
        r"|从小到大|从大到小|由小到大|由大到小|由低到高|由高到低|从低到高|从高到低"
        r"|从早到晚|从晚到早|从新到旧|从旧到新|从近到远|从远到近|由近及远|由近到远"
    ),
    re.compile(r"按[^，。；！？?\n]{1,16}(?:从|由|顺|逆|升|降)?排(?:序|列)?"),
    re.compile(r"以[^，。；！？?\n]{1,12}(?:升序|降序|顺序|倒序)"),
    re.compile(r"最新|最早|最晚|最旧|最高|最低|最大|最小|最多|最少|最贵|最便宜|最长|最短|最先"),
    re.compile(
        rf"(?:最近|近来)[的]?\s*(?:[一二三四五六七八九十两百千万\d]+|几)\s*{_ITEM_UNIT}"
    ),
    re.compile(r"排行|排名|榜单"),
    re.compile(r"\btop\s*\d+\b", re.IGNORECASE),
    re.compile(r"\b(?:order\s+by|sort\s+by|asc|desc)\b", re.IGNORECASE),
)

_PREFIX_LIMIT_RE = re.compile(
    rf"前\s*([一二三四五六七八九十两\d]+)\s*{_ITEM_UNIT}"
)

#: Ordinal reference expressions: reverse ordinal, last item, positive/non-positive ordinal, head item.
_REVERSE_ORDINAL_RE = re.compile(
    rf"倒数\s*第\s*([零〇一二三四五六七八九十两百\d]+)\s*{_ITEM_UNIT}?"
)
_LAST_ORDINAL_RE = re.compile(
    rf"最后\s*(?:一|1)?\s*({_ITEM_UNIT})"
)
_FORWARD_ORDINAL_RE = re.compile(
    rf"第\s*(-?\d+|[零〇一二三四五六七八九十两百]+)\s*({_ITEM_UNIT})?"
)
_FIRST_ORDINAL_RE = re.compile(
    rf"(?:首|头一)\s*({_ITEM_UNIT})"
)

#: Nouns immediately following "第 N" that are NOT list-item ordinal references.
_NON_LIST_ORDINAL_FOLLOW = re.compile(
    r"^\s*(?:季度?|季|周|星期|天|日|月|年|小时|分钟|秒|页|步|阶段|期|轮|级|层|次|遍|回|天内|版|节|章|题"
    r"|车间|部门|分部|事业部|工厂|仓库|分厂|分店|门店|医院|中学|小学|大学|楼|栋|号楼)"
)

#: Preceding cues indicating "第 N" expresses a metric ranking rather than an ordinal in the prior list.
_RANKING_PRECEDING_RE = re.compile(
    r"(?:排在?|排名在?|排行|位列|名列|列居|居于?|排到|占|排第)\s*$"
)
_RANKING_METRIC_CONTEXT_RE = re.compile(
    r"(?:金额|数量|销量|营收|收入|利润|成本|费用|总额|合计|得分|分数|成绩|权重|比例|占比|排序)"
    r"[^，。；！？?\n]{0,8}$"
)

#: SQL constructs where ORDER BY serves aggregation/grouping rather than row presentation order.
_SQL_AGGREGATE_CALL_RE = re.compile(
    r"\b(?:COUNT|SUM|AVG|MIN|MAX|TOTAL|GROUP_CONCAT)\s*\(", re.IGNORECASE
)
_SQL_CLAUSE_SKIP_RE = re.compile(
    r"\b(?:GROUP\s+BY|HAVING|UNION(?:\s+ALL)?|INTERSECT|EXCEPT|JOIN)\b", re.IGNORECASE
)
_SQL_TOP_CLAUSE_RE = re.compile(
    r"\bORDER\s+BY\b|\bLIMIT\b|\bOFFSET\b|\bGROUP\s+BY\b|\bHAVING\b|\bUNION\b|\bINTERSECT\b|\bEXCEPT\b",
    re.IGNORECASE,
)

#: Structured list-line parsers for verifying and repairing delivery order.
_NUMBERED_LINE_RE = re.compile(r"^(\s*)(\d+)\s*([.、)）])(\s*)(.+)$")
_BULLET_LINE_RE = re.compile(r"^(\s*)([-*•])(\s+)(.+)$")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?(?:\s*\|\s*:?-{2,}:?)*\s*\|?\s*$")

_CN_DIGITS: dict[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "壹": 1,
    "二": 2,
    "两": 2,
    "贰": 2,
    "三": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陆": 6,
    "七": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "玖": 9,
}

#: Preferred generic display column keys ordered from most human-meaningful to fallback identifiers.
_DISPLAY_KEYS_PRIORITY: tuple[str, ...] = (
    "name",
    "title",
    "subject",
    "nickname",
    "realName",
    "fullName",
    "userName",
    "username",
    "label",
    "no",
    "code",
    "number",
    "sn",
)
_DISPLAY_KEY_SUFFIX_RE = re.compile(r"(?:Name|Title|Subject|No|Code)$")

_ID_KEY_EXACT = frozenset({"id"})
_SKIP_IDENTITY_KEY_RE = re.compile(
    r"(?:time|date|remark|memo|desc|description|status|state|type|sort|deleted|tenant|"
    r"creator|updater|createby|updateby|avatar|url|pic|image|logo|token|secret|password)",
    re.IGNORECASE,
)
_ALIAS_KEY_RE = re.compile(
    r"^(?:id|no|code|sn|number|name|title|subject|nickname|username|mobile|phone|email)$"
    r"|(?:Name|Title|Subject|No|Code|Number|Sn|Plate)$",
    re.IGNORECASE,
)


def parse_cn_or_int(token: object) -> int | None:
    """Parse an Arabic integer (including negative/zero) or simple Chinese numeral."""
    if isinstance(token, bool):
        return None
    if isinstance(token, int):
        return token
    if not isinstance(token, str):
        return None
    raw = token.strip()
    if not raw:
        return None
    if re.fullmatch(r"-?\d{1,6}", raw):
        return int(raw)
    if all(ch in _CN_DIGITS for ch in raw):
        if len(raw) == 1:
            return _CN_DIGITS[raw]
        return None
    total = 0
    current = 0
    for ch in raw:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch == "十":
            total += (current if current else 1) * 10
            current = 0
        elif ch == "百":
            total += (current if current else 1) * 100
            current = 0
        else:
            return None
    return total + current


def has_explicit_sort_request(prompt: object) -> bool:
    """Return True only when the user explicitly requests sorting, ranking, or extremum/recency order."""
    if not isinstance(prompt, str):
        return False
    text = prompt.strip()
    if not text:
        return False
    if any(pat.search(text) for pat in _EXPLICIT_SORT_PATTERNS):
        return True
    # "按<列/条件>排/列" style phrasing.
    if re.search(r"按[^\s，。；！？?\n]{1,12}(?:排序|顺排|倒排|从小|从大|从高|从低|从早|从晚)", text):
        return True
    return False


def extract_prefix_limit(prompt: object) -> int | None:
    """Return N for default-order prefix asks (e.g. '前 3 个'), else None."""
    if not isinstance(prompt, str) or has_explicit_sort_request(prompt):
        return None
    text = prompt.strip()
    if not text:
        return None
    for match in _PREFIX_LIMIT_RE.finditer(text):
        # Ignore ranked/extremum asks such as "排名前 3 个".
        pre = text[max(0, match.start() - 6) : match.start()]
        if re.search(r"排名|排行|按[^，。；\n]{0,8}$", pre):
            continue
        num = parse_cn_or_int(match.group(1))
        if num is not None and 1 <= num <= 200:
            return num
    return None


def is_sort_query_param(key: object) -> bool:
    """Return True if an API query parameter name specifies server-side sorting."""
    if not isinstance(key, str):
        return False
    low = key.strip().lower()
    if not low:
        return False
    if low in SORT_QUERY_PARAM_KEYS:
        return True
    return low.startswith(_SORT_PARAM_PREFIXES)


def strip_unrequested_sort_params(params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Remove sort parameters when the user did not ask to sort, keeping all other filters."""
    if not isinstance(params, dict) or not params:
        return (dict(params) if isinstance(params, dict) else {}), []
    cleaned: dict[str, Any] = {}
    removed: list[str] = []
    for key, value in params.items():
        if is_sort_query_param(key) and value not in (None, "", []):
            removed.append(str(key))
        else:
            cleaned[key] = value
    return cleaned, removed


def _strip_sql_comments(statement: str) -> str:
    """Remove SQL comments while preserving string/quoted literals."""
    out: list[str] = []
    i, end = 0, len(statement)
    while i < end:
        ch = statement[i]
        if ch in "'\"`":
            j = i + 1
            while j < end and (statement[j] != ch or (j + 1 < end and statement[j + 1] == ch)):
                j += 2 if j + 1 < end and statement[j] == ch and statement[j + 1] == ch else 1
            out.append(statement[i : j + 1])
            i = j + 1
            continue
        if statement.startswith("--", i):
            stop = statement.find("\n", i)
            i = end if stop < 0 else stop
            continue
        if statement.startswith("/*", i):
            stop = statement.find("*/", i + 2)
            if stop < 0:
                break
            out.append(" ")
            i = stop + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _top_level_sql_marks(statement: str) -> list[tuple[str, int, int]]:
    """Return top-level SQL keyword marks outside parentheses and literals."""
    marks: list[tuple[str, int, int]] = []
    depth, i, end = 0, 0, len(statement)
    while i < end:
        ch = statement[i]
        if ch in "'\"`":
            stop = i + 1
            while stop < end and (statement[stop] != ch or (stop + 1 < end and statement[stop + 1] == ch)):
                stop += 2 if statement[stop] == ch else 1
            i = stop + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            m = _SQL_TOP_CLAUSE_RE.match(statement, i)
            if m is not None:
                kw = m.group(0).split()[0].upper()
                marks.append((kw, i, m.end()))
                i = m.end()
                continue
        i += 1
    return marks


def _is_default_row_order(order_expr: str) -> bool:
    """Return True if an ORDER BY clause already orders exclusively by ascending _row."""
    norm = re.sub(r"\s+", " ", order_expr.strip().strip('"').strip("`").lower())
    return norm in {"_row", "_row asc"}


def enforce_sql_default_order(
    statement: str,
    *,
    allow_sort: bool,
    single_table_alias: str | None = None,
    has_row_column: bool = True,
) -> tuple[str, bool, str]:
    """Ensure non-aggregate SQL queries keep the application's default ``_row`` order unless user asked to sort.

    Returns ``(rewritten_sql, order_changed, original_order_clause)``.
    ``order_changed`` is True only when a non-default ``ORDER BY`` was actually overridden.
    """
    if allow_sort or not has_row_column or not isinstance(statement, str):
        return statement, False, ""
    stripped = _strip_sql_comments(statement).rstrip()
    trailing_semi = ";" if stripped.endswith(";") else ""
    stmt = stripped.rstrip(";").rstrip()
    if not re.match(r"^\s*SELECT\b", stmt, re.IGNORECASE):
        return statement, False, ""
    marks = _top_level_sql_marks(stmt)
    if any(kw in {"GROUP", "HAVING", "UNION", "INTERSECT", "EXCEPT"} for kw, _, _ in marks):
        return statement, False, ""
    if _SQL_CLAUSE_SKIP_RE.search(stmt):
        return statement, False, ""
    from_pos = re.search(r"\bFROM\b", stmt, re.IGNORECASE)
    head_proj = stmt[: from_pos.start()] if from_pos else stmt
    if _SQL_AGGREGATE_CALL_RE.search(head_proj):
        return statement, False, ""

    row_expr = f"{single_table_alias}._row" if single_table_alias else "_row"
    order_mark = next((m for m in reversed(marks) if m[0] == "ORDER"), None)
    limit_mark = next((m for m in marks if m[0] in {"LIMIT", "OFFSET"} and (order_mark is None or m[1] > order_mark[1])), None)
    if order_mark is not None:
        order_end = limit_mark[1] if limit_mark is not None else len(stmt)
        original_expr = stmt[order_mark[2] : order_end].strip()
        if _is_default_row_order(original_expr.split(".")[-1]):
            return statement, False, ""
        rewritten = (
            stmt[: order_mark[1]].rstrip()
            + f" ORDER BY {row_expr} ASC"
            + (" " + stmt[order_end:].lstrip() if order_end < len(stmt) else "")
            + trailing_semi
        )
        return rewritten, True, original_expr

    if limit_mark is not None:
        rewritten = (
            stmt[: limit_mark[1]].rstrip()
            + f" ORDER BY {row_expr} ASC "
            + stmt[limit_mark[1] :].lstrip()
            + trailing_semi
        )
        return rewritten, False, ""
    return f"{stmt} ORDER BY {row_expr} ASC{trailing_semi}", False, ""


@dataclass(frozen=True, slots=True)
class DeliveredListItem:
    """One row inside a program-owned delivered list snapshot."""

    position: int
    record_id: str
    display: str
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "record_id": self.record_id,
            "display": self.display,
            "aliases": list(self.aliases),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "DeliveredListItem":
        if not isinstance(raw, dict):
            raise ValueError("delivered list item must be an object")
        pos = raw.get("position")
        if isinstance(pos, bool) or not isinstance(pos, int) or pos < 1:
            raise ValueError("delivered list item position must be a positive integer")
        record_id = str(raw.get("record_id") or "").strip()[:120]
        display = str(raw.get("display") or "").strip()[:240]
        if not record_id and not display:
            raise ValueError("delivered list item requires record_id or display")
        raw_aliases = raw.get("aliases") or ()
        if not isinstance(raw_aliases, (list, tuple)):
            raise ValueError("delivered list item aliases must be a list")
        aliases = tuple(
            dict.fromkeys(
                str(a).strip()[:200]
                for a in raw_aliases[:8]
                if isinstance(a, (str, int)) and not isinstance(a, bool) and str(a).strip()
            )
        )
        return cls(position=pos, record_id=record_id, display=display, aliases=aliases)


@dataclass(frozen=True, slots=True)
class DeliveredList:
    """Authoritative server snapshot of the rows delivered in a list answer."""

    entity: str
    source_path: str
    label: str
    total_rows: int
    ordered_by_user: bool
    items: tuple[DeliveredListItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity[:120],
            "source_path": self.source_path[:240],
            "label": self.label[:200],
            "total_rows": max(len(self.items), int(self.total_rows)),
            "ordered_by_user": bool(self.ordered_by_user),
            "items": [item.to_dict() for item in self.items[:50]],
        }

    @classmethod
    def from_dict(cls, raw: object) -> "DeliveredList":
        if not isinstance(raw, dict):
            raise ValueError("delivered list must be an object")
        raw_items = raw.get("items")
        if not isinstance(raw_items, (list, tuple)) or not raw_items or len(raw_items) > 50:
            raise ValueError("delivered list requires 1 to 50 items")
        items = tuple(DeliveredListItem.from_dict(item) for item in raw_items)
        total_rows = raw.get("total_rows", len(items))
        if isinstance(total_rows, bool) or not isinstance(total_rows, int) or total_rows < 0:
            raise ValueError("delivered list total_rows is invalid")
        ordered_by_user = raw.get("ordered_by_user", False)
        if not isinstance(ordered_by_user, bool):
            raise ValueError("delivered list ordered_by_user must be boolean")
        return cls(
            entity=str(raw.get("entity") or "").strip()[:120],
            source_path=str(raw.get("source_path") or "").strip()[:240],
            label=str(raw.get("label") or "").strip()[:200],
            total_rows=max(len(items), total_rows),
            ordered_by_user=ordered_by_user,
            items=items,
        )


@dataclass(frozen=True, slots=True)
class OrdinalReference:
    """One parsed ordinal reference in user prose (e.g. '第 2 个', '最后一个')."""

    raw_phrase: str
    position: int
    from_end: bool = False


@dataclass(frozen=True, slots=True)
class OrdinalResolution:
    """Program resolution result for an ``OrdinalReference`` against ``DeliveredList``."""

    status: str  # "bound" | "missing_list" | "out_of_range"
    reference: OrdinalReference
    item: DeliveredListItem | None = None
    delivered_list: DeliveredList | None = None
    resolved_position: int | None = None


def parse_ordinal_reference(prompt: object) -> OrdinalReference | None:
    """Parse an unambiguous list-item ordinal reference from user prose, else return None."""
    if not isinstance(prompt, str):
        return None
    text = prompt.strip()
    if not text:
        return None

    m_rev = _REVERSE_ORDINAL_RE.search(text)
    if m_rev is not None:
        after = text[m_rev.end() :]
        if not _NON_LIST_ORDINAL_FOLLOW.match(after):
            num = parse_cn_or_int(m_rev.group(1))
            if num is not None:
                return OrdinalReference(raw_phrase=m_rev.group(0).strip(), position=num, from_end=True)

    m_last = _LAST_ORDINAL_RE.search(text)
    if m_last is not None:
        after = text[m_last.end() :]
        if not _NON_LIST_ORDINAL_FOLLOW.match(after) and not re.search(
            r"一次|一天|一夜|一步|一页|一期|一轮|一场|一季度|一月|一周|一年", m_last.group(0)
        ):
            return OrdinalReference(raw_phrase=m_last.group(0).strip(), position=1, from_end=True)

    for m_fwd in _FORWARD_ORDINAL_RE.finditer(text):
        after = text[m_fwd.end() :]
        if _NON_LIST_ORDINAL_FOLLOW.match(after):
            continue
        unit = m_fwd.group(2) or ""
        before = text[max(0, m_fwd.start() - 16) : m_fwd.start()]
        # If no item unit ("第 2" vs "第 2 个"), require stronger disambiguation and avoid "排第 2".
        if not unit and _RANKING_PRECEDING_RE.search(before):
            continue
        if _RANKING_PRECEDING_RE.search(before) or _RANKING_METRIC_CONTEXT_RE.search(before):
            continue
        # Require an item unit or clear action/reference context when unit is omitted.
        if not unit and not re.search(r"^[的在把给看查改删停关开]?", after):
            continue
        if not unit:
            # Bare "第2" without unit can be ambiguous (e.g. "排名第2"); only accept when
            # accompanied by no time/ranking noun and explicitly looks like list selection.
            if not re.search(r"(?:那|这|上面|列表|刚才|上面第|^)\s*$|^(?:把|将|给|查看|打开|修改|改为|改成|删除|停用|启用)", before):
                continue
        num = parse_cn_or_int(m_fwd.group(1))
        if num is not None:
            return OrdinalReference(raw_phrase=m_fwd.group(0).strip(), position=num, from_end=False)

    m_first = _FIRST_ORDINAL_RE.search(text)
    if m_first is not None:
        after = text[m_first.end() :]
        before = text[max(0, m_first.start() - 12) : m_first.start()]
        if not _NON_LIST_ORDINAL_FOLLOW.match(after) and not _RANKING_PRECEDING_RE.search(before):
            return OrdinalReference(raw_phrase=m_first.group(0).strip(), position=1, from_end=False)

    return None


def resolve_ordinal_binding(
    ref: OrdinalReference,
    delivered_list: DeliveredList | None,
) -> OrdinalResolution:
    """Resolve an ordinal reference against the most recently delivered list."""
    if delivered_list is None or not delivered_list.items:
        return OrdinalResolution(status="missing_list", reference=ref)
    count = len(delivered_list.items)
    if ref.position <= 0:
        return OrdinalResolution(status="out_of_range", reference=ref, delivered_list=delivered_list)
    pos = (count - ref.position + 1) if ref.from_end else ref.position
    if pos < 1 or pos > count:
        return OrdinalResolution(status="out_of_range", reference=ref, delivered_list=delivered_list)
    return OrdinalResolution(
        status="bound",
        reference=ref,
        item=delivered_list.items[pos - 1],
        delivered_list=delivered_list,
        resolved_position=pos,
    )


def target_matches_bound(value: object, item: DeliveredListItem) -> bool:
    """Return True when a proposed target string/ID refers to the program-bound item."""
    if item is None:
        return False
    raw = str(value or "").strip()
    if not raw:
        return False
    folded = re.sub(r"\s+", "", raw).casefold()
    # Extract "(ID=123)" suffix if supplied by caller.
    m_id = re.search(r"[（(]\s*(?:id|编号)\s*[=:：#]?\s*([0-9]+)\s*[）)]$", raw, re.I)
    if m_id and item.record_id and m_id.group(1) == item.record_id:
        return True
    if item.record_id and raw == item.record_id:
        return True
    for candidate in (item.display, *item.aliases):
        c_text = re.sub(r"\s+", "", str(candidate or "")).casefold()
        if not c_text:
            continue
        if folded == c_text:
            return True
        if len(c_text) >= 2 and c_text in folded and m_id is None:
            return True
    return False


def value_matches_other_row(
    value: object,
    delivered_list: DeliveredList | None,
    bound_position: int | None,
) -> DeliveredListItem | None:
    """Return a different item from ``delivered_list`` if ``value`` unambiguously identifies it."""
    if delivered_list is None or not delivered_list.items or bound_position is None:
        return None
    raw = str(value or "").strip()
    if not raw:
        return None
    bound_item = delivered_list.items[bound_position - 1]
    if target_matches_bound(raw, bound_item):
        return None
    for candidate_item in delivered_list.items:
        if candidate_item.position == bound_position:
            continue
        if target_matches_bound(raw, candidate_item):
            return candidate_item
    return None


def extract_row_identity(row: object) -> tuple[str, str, tuple[str, ...]]:
    """Extract ``(record_id, display, aliases)`` from a row dict without entity-specific rules."""
    if not isinstance(row, dict):
        text = str(row or "").strip()[:240]
        return "", text, ((text,) if text else ())
    record_id = ""
    raw_id = row.get("id")
    if raw_id is not None and not isinstance(raw_id, (bool, dict, list)) and str(raw_id).strip():
        record_id = str(raw_id).strip()[:120]
    else:
        for k, v in row.items():
            if (
                isinstance(k, str)
                and (k in _ID_KEY_EXACT or k.endswith("Id"))
                and v is not None
                and not isinstance(v, (bool, dict, list))
                and str(v).strip()
            ):
                record_id = str(v).strip()[:120]
                break

    display = ""
    folded_map = {str(k).casefold(): (str(k), v) for k, v in row.items() if isinstance(k, str)}
    for key_pref in _DISPLAY_KEYS_PRIORITY:
        pair = folded_map.get(key_pref.casefold())
        if pair is None:
            continue
        val = pair[1]
        if isinstance(val, str) and val.strip():
            display = val.strip()[:240]
            break
    if not display:
        for k, v in row.items():
            if (
                isinstance(k, str)
                and _DISPLAY_KEY_SUFFIX_RE.search(k)
                and not _SKIP_IDENTITY_KEY_RE.search(k)
                and isinstance(v, str)
                and v.strip()
            ):
                display = v.strip()[:240]
                break
    if not display:
        for k, v in row.items():
            if not isinstance(k, str) or _SKIP_IDENTITY_KEY_RE.search(k):
                continue
            if k.endswith(("Id", "id")) and not k.casefold().endswith("code"):
                continue
            if isinstance(v, str) and v.strip():
                display = v.strip()[:240]
                break
    if not display and record_id:
        display = record_id

    aliases_list: list[str] = []
    for k, v in row.items():
        if not isinstance(k, str) or _SKIP_IDENTITY_KEY_RE.search(k):
            continue
        if isinstance(v, (bool, dict, list)) or v is None:
            continue
        txt = str(v).strip()
        if not txt or txt == display or len(txt) > 160:
            continue
        if _ALIAS_KEY_RE.search(k):
            aliases_list.append(txt)
    if record_id and record_id != display and record_id not in aliases_list:
        aliases_list.append(record_id)
    # Also synthesize "<wordName>-<number>" if composite document keys exist (domain-agnostic).
    for k, word_val in row.items():
        if isinstance(k, str) and k.endswith("WordName") and word_val:
            num_val = row.get(k[: -len("WordName")] + "Number")
            if num_val is not None:
                aliases_list.append(f"{str(word_val).strip()}-{str(num_val).strip()}")
    return record_id, display, tuple(dict.fromkeys(aliases_list))[:8]


def _identity_tokens_for_row(row: dict, idx: int) -> tuple[str, ...]:
    record_id, display, aliases = extract_row_identity(row)
    tokens: list[str] = []
    if display and len(display) >= 2:
        tokens.append(display)
    for alias in aliases:
        # Skip short pure integers (e.g. "1", "2") as standalone prose match tokens to avoid
        # colliding with row numbers "1.", "2."; only keep IDs of 3+ chars or labeled IDs.
        if alias.isdigit() and len(alias) < 3:
            continue
        if len(alias) >= 2:
            tokens.append(alias)
    if record_id and (not record_id.isdigit() or len(record_id) >= 3):
        tokens.append(record_id)
    # Include all non-trivial unique string cell values (>= 2 chars) as secondary signals
    if not tokens:
        for v in row.values():
            if isinstance(v, str) and 2 <= len(v.strip()) <= 80:
                tokens.append(v.strip())
    del idx
    return tuple(dict.fromkeys(tokens))


def _match_line_to_row_indices(
    line_text: str,
    row_tokens: list[tuple[str, ...]],
    source_rows: list[dict],
) -> int | None:
    """Match one rendered line/row to a unique index in ``source_rows``."""
    compact_line = re.sub(r"\s+", " ", line_text).strip()
    if not compact_line:
        return None
    # Remove leading numbering like "1. " or "1、" before matching short numeric IDs
    body = re.sub(r"^\s*\d+\s*[.、)）]\s*", "", compact_line)
    body = re.sub(r"^\s*[-*•]\s+", "", body)

    scores: list[tuple[int, int]] = []
    for idx, tokens in enumerate(row_tokens):
        row = source_rows[idx]
        rec_id, display, aliases = extract_row_identity(row)
        best = 0
        if display and display in body:
            best = max(best, len(display) * 10 + 50)
        for alias in aliases:
            if alias in body:
                if alias.isdigit():
                    if re.search(rf"(?:ID|id|编号|#|No\.?)\s*[=:：#]?\s*{re.escape(alias)}(?![0-9])", body):
                        best = max(best, len(alias) * 10 + 40)
                    elif f"| {alias} |" in compact_line or f"|{alias}|" in compact_line:
                        best = max(best, len(alias) * 8 + 20)
                else:
                    best = max(best, len(alias) * 8 + 30)
        if rec_id and re.search(rf"(?:ID|id|编号|#)\s*[=:：#]?\s*{re.escape(rec_id)}(?![0-9])", body):
            best = max(best, len(rec_id) * 10 + 35)
        elif not display and tokens:
            for tok in tokens:
                if tok in body:
                    best = max(best, len(tok) * 5)
        if best > 0:
            scores.append((best, idx))
    if not scores:
        return None
    scores.sort(key=lambda pair: (-pair[0], pair[1]))
    if len(scores) >= 2 and scores[0][0] == scores[1][0]:
        return None
    return scores[0][1]


def _extract_list_blocks(lines: list[str]) -> list[tuple[str, int, int, list[int]]]:
    """Identify contiguous markdown table / numbered / bulleted blocks in ``lines``."""
    blocks: list[tuple[str, int, int, list[int]]] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        # Markdown table block: header line with '|', separator line '---', then data lines with '|'
        if (
            "|" in line
            and i + 1 < n
            and _MD_TABLE_SEP_RE.match(lines[i + 1])
        ):
            j = i + 2
            data_lines: list[int] = []
            while j < n and "|" in lines[j] and lines[j].strip():
                data_lines.append(j)
                j += 1
            if data_lines:
                blocks.append(("table", data_lines[0], data_lines[-1] + 1, data_lines))
                i = j
                continue
        # Numbered list block: consecutive lines matching 1. / 1、 / 1)
        if _NUMBERED_LINE_RE.match(line):
            j = i
            item_lines: list[int] = []
            while j < n and _NUMBERED_LINE_RE.match(lines[j]):
                item_lines.append(j)
                j += 1
            if len(item_lines) >= 2:
                blocks.append(("numbered", item_lines[0], item_lines[-1] + 1, item_lines))
                i = j
                continue
        # Bulleted list block
        if _BULLET_LINE_RE.match(line):
            j = i
            item_lines = []
            while j < n and _BULLET_LINE_RE.match(lines[j]):
                item_lines.append(j)
                j += 1
            if len(item_lines) >= 2:
                blocks.append(("bullet", item_lines[0], item_lines[-1] + 1, item_lines))
                i = j
                continue
        i += 1
    return blocks


def check_answer_list_order(
    answer: str,
    source_rows: list[dict],
    *,
    prefix_limit: int | None = None,
) -> dict[str, Any]:
    """Verify whether list items rendered in ``answer`` match ``source_rows`` order (and prefix)."""
    if not isinstance(answer, str) or not answer.strip() or not source_rows:
        return {"matched_indices": [], "in_order": True, "prefix_ok": True, "format": "none"}
    lines = answer.splitlines()
    row_tokens = [_identity_tokens_for_row(row, idx) for idx, row in enumerate(source_rows)]
    blocks = _extract_list_blocks(lines)

    best_block: tuple[str, list[int], list[int]] | None = None
    for fmt, _start, _end, line_idxs in blocks:
        matched: list[int] = []
        matched_line_idxs: list[int] = []
        seen_rows: set[int] = set()
        for li in line_idxs:
            row_idx = _match_line_to_row_indices(lines[li], row_tokens, source_rows)
            if row_idx is not None and row_idx not in seen_rows:
                seen_rows.add(row_idx)
                matched.append(row_idx)
                matched_line_idxs.append(li)
        if matched and (best_block is None or len(matched) > len(best_block[1])):
            best_block = (fmt, matched, matched_line_idxs)

    if best_block is not None and (len(best_block[1]) >= 2 or (prefix_limit is not None and best_block[1])):
        fmt, matched, _ = best_block
    else:
        # Fallback: scan prose mentions by earliest character offset of each unique row's display name.
        fmt = "prose"
        hits: list[tuple[int, int]] = []
        for idx, row in enumerate(source_rows):
            _rid, display, aliases = extract_row_identity(row)
            pos = -1
            if display and len(display) >= 2:
                pos = answer.find(display)
            if pos < 0:
                for alias in aliases:
                    if not alias.isdigit() and len(alias) >= 3:
                        pos = answer.find(alias)
                        if pos >= 0:
                            break
            if pos >= 0:
                hits.append((pos, idx))
        hits.sort(key=lambda item: item[0])
        matched = [idx for _pos, idx in hits]

    in_order = len(matched) < 2 or all(matched[k] < matched[k + 1] for k in range(len(matched) - 1))
    prefix_ok = True
    if prefix_limit is not None and matched:
        expected_k = min(prefix_limit, len(source_rows))
        expected_prefix = list(range(expected_k))
        if matched != expected_prefix:
            prefix_ok = False
    return {
        "matched_indices": matched,
        "in_order": in_order,
        "prefix_ok": prefix_ok,
        "format": fmt,
    }


def _reformat_row_as_line(row: dict, position: int, *, fmt: str, col_count: int = 0) -> str:
    record_id, display, aliases = extract_row_identity(row)
    extra = next((a for a in aliases if a != record_id and a != display), "")
    id_part = f"（ID={record_id}）" if record_id and record_id != display else ""
    summary = f"{display}{id_part}" + (f" / {extra}" if extra else "")
    if fmt == "table":
        if col_count <= 2:
            return f"| {position} | {summary} |"
        cells = [str(position), display]
        if col_count >= 3:
            cells.append(record_id or extra or "-")
        while len(cells) < col_count:
            cells.append("-")
        return "| " + " | ".join(cells[:col_count]) + " |"
    if fmt == "bullet":
        return f"- {summary}"
    return f"{position}. {summary}"


def reorder_answer_to_default(
    answer: str,
    source_rows: list[dict],
    *,
    prefix_limit: int | None = None,
) -> tuple[str, bool]:
    """Deterministically repair a re-ordered or wrong-prefix list in ``answer`` to default order."""
    if not isinstance(answer, str) or not answer.strip() or not source_rows:
        return answer, False
    lines = answer.splitlines()
    row_tokens = [_identity_tokens_for_row(row, idx) for idx, row in enumerate(source_rows)]
    blocks = _extract_list_blocks(lines)

    best: tuple[str, int, int, list[int], list[tuple[int, str]]] | None = None
    for fmt, start, end, line_idxs in blocks:
        pairs: list[tuple[int, str]] = []
        seen: set[int] = set()
        for li in line_idxs:
            row_idx = _match_line_to_row_indices(lines[li], row_tokens, source_rows)
            if row_idx is not None and row_idx not in seen:
                seen.add(row_idx)
                pairs.append((row_idx, lines[li]))
        if len(pairs) >= 2 and (best is None or len(pairs) > len(best[4])):
            best = (fmt, start, end, line_idxs, pairs)

    if best is None:
        return answer, False
    fmt, start, end, line_idxs, pairs = best
    if len(pairs) != len(line_idxs):
        # Some lines did not cleanly resolve; avoid corrupting unmapped lines.
        return answer, False

    matched_indices = [idx for idx, _ in pairs]
    if prefix_limit is not None:
        expected_k = min(prefix_limit, len(source_rows))
        expected = list(range(expected_k))
        if matched_indices == expected:
            return answer, False
        row_by_idx = {idx: raw_line for idx, raw_line in pairs}
        col_count = len([c for c in lines[start].strip().strip("|").split("|")]) if fmt == "table" else 0
        rebuilt: list[str] = []
        for pos_idx in expected:
            raw_line = row_by_idx.get(pos_idx)
            if raw_line is None:
                rebuilt.append(_reformat_row_as_line(source_rows[pos_idx], pos_idx + 1, fmt=fmt, col_count=col_count))
            else:
                rebuilt.append(raw_line)
        new_block = _renumber_block(rebuilt, fmt=fmt)
    else:
        if all(matched_indices[k] < matched_indices[k + 1] for k in range(len(matched_indices) - 1)):
            return answer, False
        sorted_pairs = sorted(pairs, key=lambda pair: pair[0])
        new_block = _renumber_block([raw_line for _idx, raw_line in sorted_pairs], fmt=fmt)

    updated_lines = [*lines[:start], *new_block, *lines[end:]]
    new_answer = "\n".join(updated_lines)
    return new_answer, new_answer != answer


def _renumber_block(block_lines: list[str], *, fmt: str) -> list[str]:
    if fmt != "numbered":
        return list(block_lines)
    out: list[str] = []
    for pos, raw_line in enumerate(block_lines, start=1):
        m = _NUMBERED_LINE_RE.match(raw_line)
        if m:
            indent, _num, sep, space, rest = m.groups()
            out.append(f"{indent}{pos}{sep}{space}{rest}")
        else:
            out.append(f"{pos}. {raw_line.lstrip('-*• ').strip()}")
    return out


def build_delivered_items_from_rows(
    rows: list[dict],
    *,
    max_items: int = 50,
) -> tuple[DeliveredListItem, ...]:
    """Convert ordered row dicts to 1-based ``DeliveredListItem`` entries."""
    items: list[DeliveredListItem] = []
    for pos, row in enumerate(rows[:max_items], start=1):
        record_id, display, aliases = extract_row_identity(row)
        if not record_id and not display:
            continue
        items.append(
            DeliveredListItem(
                position=pos,
                record_id=record_id,
                display=display or record_id,
                aliases=aliases,
            )
        )
    return tuple(items)


def extract_delivered_list_from_answer(
    answer: str,
    source_rows: list[dict],
    *,
    entity: str,
    source_path: str,
    label: str,
    total_rows: int,
    ordered_by_user: bool,
    prefix_limit: int | None = None,
) -> DeliveredList | None:
    """Build a ``DeliveredList`` reflecting the exact rows and order shown in ``answer``."""
    if not source_rows:
        return None
    check = check_answer_list_order(answer, source_rows, prefix_limit=prefix_limit)
    matched_indices: list[int] = check["matched_indices"]
    if matched_indices:
        chosen_rows = [source_rows[idx] for idx in matched_indices[:50] if 0 <= idx < len(source_rows)]
    elif len(source_rows) <= 50 and any(
        (extract_row_identity(row)[1] and extract_row_identity(row)[1] in answer)
        for row in source_rows
    ):
        chosen_rows = [
            row
            for row in source_rows[:50]
            if extract_row_identity(row)[1] and extract_row_identity(row)[1] in answer
        ]
    else:
        return None
    items = build_delivered_items_from_rows(chosen_rows)
    if not items:
        return None
    return DeliveredList(
        entity=entity,
        source_path=source_path,
        label=label,
        total_rows=max(len(items), total_rows),
        ordered_by_user=ordered_by_user,
        items=items,
    )


__all__ = [
    "DeliveredList",
    "DeliveredListItem",
    "OrdinalReference",
    "OrdinalResolution",
    "SORT_QUERY_PARAM_KEYS",
    "build_delivered_items_from_rows",
    "check_answer_list_order",
    "enforce_sql_default_order",
    "extract_delivered_list_from_answer",
    "extract_prefix_limit",
    "extract_row_identity",
    "has_explicit_sort_request",
    "is_sort_query_param",
    "parse_cn_or_int",
    "parse_ordinal_reference",
    "reorder_answer_to_default",
    "resolve_ordinal_binding",
    "strip_unrequested_sort_params",
    "target_matches_bound",
    "value_matches_other_row",
]
