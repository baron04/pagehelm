"""Where each value the assistant typed into a form came from.

A card shows what will be saved; this annotates, before it is shown, whether every value
the assistant entered this turn is one the person gave, chose or had computed for that
field. The assistant may not invent a value. Form defaults and what the application
links on its own are not entered by the assistant and are listed on the card instead.

A value passes when it is one of:

* in the person's words this turn or an earlier one -- dates, times and amounts are
  compared by meaning ("11月12号下午两点" is 2026-11-12 14:00), everything else only
  ignoring spacing and punctuation;
* a real candidate the person chose (a dropdown option or a record found for them);
* the result of a date conversion or calculation made for this very field this turn.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
import re

from .submit_contract import _datetime, scalar_text

_PUNCT = re.compile(r"[\s,，.。;；:：、!！?？\"'“”‘’()（）\[\]【】《》<>-]+")
_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}
_UNITS = {"十": 10, "百": 100, "千": 1000}


@dataclass(frozen=True)
class Entry:
    """One value the assistant entered or selected, as the person could check it."""

    field: str
    value: str
    label: str = ""                 # what the person sees for it (an option's label)
    candidate: bool = False         # chosen from options the page really offered
    kind: str = "text"              # text | datetime | number


@dataclass(frozen=True)
class Receipt:
    """A conversion or calculation made for one field this turn."""

    field: str
    value: str


@dataclass(frozen=True)
class Verdict:
    entry: Entry
    ok: bool
    source: str = ""                # user_text | candidate | receipt | ""
    reason: str = ""


@dataclass
class SourceLedger:
    entries: list = field(default_factory=list)
    receipts: list = field(default_factory=list)

    def add(self, entry: Entry) -> None:
        if any(e.field == entry.field and e.value == entry.value and e.candidate for e in self.entries):
            entry = replace(entry, candidate=True)
        self.entries = [e for e in self.entries if e.field != entry.field] + [entry]

    def receipt(self, receipt: Receipt) -> None:
        self.receipts.append(receipt)


def _plain(text: str) -> str:
    return _PUNCT.sub("", str(text)).casefold()


def cn_number(text: str) -> int | None:
    """二千四百、三万五、十二、两百 -- whole numbers up to 99,999,999; None if not one."""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, section, digit, seen = 0, 0, None, False
    for char in text:
        if char in _DIGITS:
            digit, seen = _DIGITS[char], True
        elif char in _UNITS:
            section += (1 if digit is None else digit) * _UNITS[char]
            digit, seen = None, True
        elif char in "万亿":
            section += digit or 0
            total += (section or 1) * (10_000 if char == "万" else 100_000_000)
            section, digit, seen = 0, None, True
        else:
            return None
    value = total + section + (digit or 0)
    # 三万五 reads as 35,000; 一百二 as 120.
    if seen and digit is not None and text[-2:-1] in {"万"} and len(text) >= 2:
        value = total + section + digit * 1000
    return value if seen else None


_NUM = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)"
_AMOUNT = re.compile(rf"({_NUM})\s*(万|千|百|k|K|元|块|块钱)?")


def amounts_in(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for number, unit in _AMOUNT.findall(text):
        try:
            base = Decimal(number) if re.fullmatch(r"\d+(?:\.\d+)?", number) else None
        except InvalidOperation:
            base = None
        if base is None:
            whole = cn_number(number)
            base = Decimal(whole) if whole is not None else None
        if base is None:
            continue
        scale = {"万": 10_000, "千": 1_000, "百": 100, "k": 1_000, "K": 1_000}.get(unit, 1)
        out.add((base * scale).normalize())
        out.add(base.normalize())
    return out


_DAY = re.compile(rf"(?:(\d{{4}})\s*[年/-]\s*)?(\d{{1,2}})\s*(?:月|/|-)\s*(\d{{1,2}})\s*(?:日|号)?")
_CLOCK = re.compile(rf"(上午|早上|早晨|中午|下午|晚上|傍晚|凌晨)?\s*({_NUM})\s*(?:点|时|:|：)\s*(半|{_NUM}\s*分?)?")


def _clock_number(text: str) -> int | None:
    text = text.strip().rstrip("分")
    return int(text) if text.isdigit() else cn_number(text)


def moments_in(text: str) -> list[tuple]:
    """(month, day, hour|None, minute|None) mentioned in the words; years are not asked for."""
    days = [(int(m), int(d)) for _y, m, d in _DAY.findall(text) if 1 <= int(m) <= 12 and 1 <= int(d) <= 31]
    clocks = []
    previous_end, inherited = 0, ""
    for match in _CLOCK.finditer(text):
        period, hour, minute = match.groups()
        if not period and re.fullmatch(r"\s*(?:到|至|[-—~～])\s*", text[previous_end:match.start()]):
            period = inherited
        inherited, previous_end = period, match.end()
        h = _clock_number(hour)
        if h is None or h > 24:
            continue
        if period in {"下午", "晚上", "傍晚"} and h < 12:
            h += 12
        if period == "凌晨" and h == 12:
            h = 0
        m = 30 if minute == "半" else (_clock_number(minute) if minute else 0)
        clocks.append((h, m if m is not None else 0))
    out = []
    for month, day in days or [(None, None)]:
        for hour, minute in clocks or [(None, None)]:
            if month is not None or hour is not None:
                out.append((month, day, hour, minute))
    return out


def _datetime_matches(value: str, words: list[str]) -> bool:
    stamp = _datetime(value)
    if stamp is None:
        return False
    timed = len(value.strip()) > 10 and (stamp.hour or stamp.minute or stamp.second)
    for text in words:
        for month, day, hour, minute in moments_in(text):
            if month is not None and (stamp.month, stamp.day) != (month, day):
                continue
            if hour is not None and (stamp.hour, stamp.minute) != (hour, minute):
                continue
            if month is None and hour is None:
                continue
            if timed and hour is None:
                continue          # a date alone does not give a time of day
            return True
    return False


def user_supplied(entry: Entry, words: list[str]) -> bool:
    plain_words = [_plain(w) for w in words]
    for candidate in {entry.value, entry.label}:
        shown = _plain(candidate)
        if shown and any(shown in w for w in plain_words):
            return True
    if _datetime(entry.value) is not None and _datetime_matches(entry.value, words):
        return True
    try:
        number = Decimal(scalar_text(entry.value))
    except InvalidOperation:
        return False
    if re.fullmatch(r"-?\d+(?:\.\d+)?", scalar_text(entry.value)):
        wanted = number.normalize()
        return any(wanted in amounts_in(w) for w in words)
    return False


def check_entries(ledger: SourceLedger, words: list[str]) -> list[Verdict]:
    """A source verdict per business form value; missing sources are shown on the card."""
    out = []
    for entry in ledger.entries:
        if not str(entry.value).strip() and not entry.label:
            out.append(Verdict(entry, True, "user_text"))
        elif user_supplied(entry, words):
            out.append(Verdict(entry, True, "user_text"))
        elif entry.candidate:
            out.append(Verdict(entry, True, "candidate"))
        elif any(r.field == entry.field and (_plain(r.value) == _plain(entry.value)
                                             or scalar_text(r.value) == scalar_text(entry.value))
                 for r in ledger.receipts):
            out.append(Verdict(entry, True, "receipt"))
        else:
            out.append(Verdict(entry, False, "", "not_from_the_person"))
    return out
