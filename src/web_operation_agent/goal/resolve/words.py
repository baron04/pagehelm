"""Words, periods and emptiness as a declaration writes them; nothing here reads data."""
from __future__ import annotations

import html
import re
from datetime import date, timedelta

#: Values that ask whether a field is set at all, in the index's language and in Chinese.
EMPTY = frozenset({"empty", "notset", "none", "unset", "空", "为空", "没有"})
NOT_EMPTY = frozenset({"notempty", "set", "any", "nonempty", "非空", "不为空", "有"})

#: Relative periods a date condition may name, in the index's language and in Chinese.
PERIODS = {
    "today": "today", "今天": "today", "yesterday": "yesterday", "昨天": "yesterday",
    "this week": "this_week", "本周": "this_week", "last week": "last_week", "上周": "last_week",
    "this month": "this_month", "本月": "this_month", "这个月": "this_month",
    "last month": "last_month", "上月": "last_month", "上个月": "last_month",
    "this quarter": "this_quarter", "本季度": "this_quarter",
    "last quarter": "last_quarter", "上季度": "last_quarter", "上个季度": "last_quarter",
    "this year": "this_year", "今年": "this_year", "last year": "last_year", "去年": "last_year",
    "overdue": "overdue", "逾期": "overdue", "过期": "overdue",
}

LAST_DAYS = re.compile(r"^(?:last|past|最近|过去)\s*(\d{1,3})\s*(?:days|天)$", re.I)
DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
MONTH = re.compile(r"^(\d{4})-(\d{2})$")
YEAR = re.compile(r"^(\d{4})$")
RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*(?:\.\.|~|to|至|到)\s*(\d{4}-\d{2}-\d{2})$")


def norm(text: str) -> str:
    return "".join(ch for ch in html.unescape(text or "").casefold() if not ch.isspace())


def tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-z㐀-鿿]+", (text or "").casefold()) if t}


def foreign(text) -> bool:
    """Words with no Latin letter or digit: written in the person's language, where the
    application's names are not."""
    text = str(text or "")
    return bool(text.strip()) and re.search(r"[A-Za-z0-9]", text) is None


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    return date(day.year + month // 12, month % 12 + 1, 1)


def period(value: str, today: date) -> tuple[date | None, date | None] | None:
    """A named or written period as [start, end) dates; None when it is not one."""
    text = " ".join(value.strip().lower().split())
    name = PERIODS.get(text)
    if name == "today":
        return today, today + timedelta(days=1)
    if name == "yesterday":
        return today - timedelta(days=1), today
    if name == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    if name == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=7)
    if name == "this_month":
        return _month_start(today), _add_months(today, 1)
    if name == "last_month":
        return _add_months(today, -1), _month_start(today)
    if name in ("this_quarter", "last_quarter"):
        quarter_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        if name == "last_quarter":
            quarter_start = _add_months(quarter_start, -3)
        return quarter_start, _add_months(quarter_start, 3)
    if name == "this_year":
        return date(today.year, 1, 1), date(today.year + 1, 1, 1)
    if name == "last_year":
        return date(today.year - 1, 1, 1), date(today.year, 1, 1)
    if name == "overdue":
        return None, today
    match = LAST_DAYS.match(text)
    if match:
        days = int(match.group(1))
        return today - timedelta(days=days), today + timedelta(days=1)
    match = RANGE.match(text)
    if match:
        start, end = date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
        return start, end + timedelta(days=1)
    if DAY.match(text):
        start = date.fromisoformat(text)
        return start, start + timedelta(days=1)
    match = MONTH.match(text)
    if match:
        start = date(int(match.group(1)), int(match.group(2)), 1)
        return start, _add_months(start, 1)
    if YEAR.match(text):
        return date(int(text), 1, 1), date(int(text) + 1, 1, 1)
    return None


def worded(values) -> list[list[str]]:
    """For each value, the words a name must all hold, a plural matched as its singular;
    empty when a value has no word to match."""
    out = []
    for value in values:
        words = [w for w in re.split(r"[^0-9A-Za-z]+", str(value)) if len(w) > 1]
        if not words:
            return []
        out.append([w[:-1] if w.lower().endswith("s") and len(w) > 3 else w for w in words])
    return out
