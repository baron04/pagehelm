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
    kind: str = "text"              # text | select (a reference/selection control)
    placed: bool = True             # resolved to the business field it was typed into
    seq: int = 0                    # when it was entered, for source ordering


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


@dataclass(frozen=True)
class Candidate:
    """A cell the actor read this turn, bound to the one field it may source.

    binding="" marks a record's own name/id: it may source a reference/selection
    field only. A non-empty binding (the column's label) sources a field of the
    same name only; seq orders the read against the entry's fill so a read that
    came after can never explain it.
    """

    binding: str
    value: str
    seq: int = 0


@dataclass
class SourceLedger:
    entries: list = field(default_factory=list)
    receipts: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    _seq: int = 0

    def add(self, entry: Entry) -> None:
        if any(e.field == entry.field and e.value == entry.value and e.candidate for e in self.entries):
            entry = replace(entry, candidate=True)
        self._seq += 1
        self.entries = [e for e in self.entries if e.field != entry.field] + [
            replace(entry, seq=self._seq)]

    def receipt(self, receipt: Receipt) -> None:
        self.receipts.append(receipt)

    def add_candidates(self, items) -> None:
        for binding, value in items:
            plain = _plain(value)
            if plain and len(plain) <= 200 and len(self.candidates) < 4000:
                self._seq += 1
                self.candidates.append(
                    Candidate(str(binding or "")[:80], str(value)[:200], self._seq))


def _plain(text: str) -> str:
    return _PUNCT.sub("", str(text)).casefold()


def _same_value(left, right) -> bool:
    """Equal by meaning: datetimes as points in time, numbers normalized, else plain text."""
    left_stamp, right_stamp = _datetime(str(left)), _datetime(str(right))
    if left_stamp is not None or right_stamp is not None:
        return left_stamp is not None and left_stamp == right_stamp
    try:
        left_num, right_num = Decimal(scalar_text(left)), Decimal(scalar_text(right))
    except (InvalidOperation, ValueError, TypeError):
        left_num = right_num = None
    if left_num is not None and right_num is not None \
            and left_num.is_finite() and right_num.is_finite():
        return left_num == right_num
    return _plain(left) == _plain(right)


def note_receipt(turn: dict, field: str, value) -> None:
    """A tool result this turn may serve as the source of one typed value."""
    ledger = turn.setdefault("source_ledger", SourceLedger())
    if len(ledger.receipts) < 200:
        ledger.receipt(Receipt(str(field or ""), str(value)))


def note_candidates(turn: dict, items) -> None:
    """Record (binding, value) candidates the actor read this turn; see Candidate."""
    turn.setdefault("source_ledger", SourceLedger()).add_candidates(items or ())


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
        if not unit:
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


_TIME_PART = re.compile(r"[T\s]\d{1,2}[:：]\d{2}")


def _has_time(value) -> bool:
    """The string carries an explicit clock component -- even midnight counts."""
    return isinstance(value, str) and bool(_TIME_PART.search(value.strip()))


def _datetime_matches(value: str, words: list[str]) -> bool:
    stamp = _datetime(value)
    if stamp is None:
        return False
    timed = _has_time(value)
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


_BARE_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def user_supplied(entry: Entry, words: list[str]) -> bool:
    plain_words = [_plain(w) for w in words]
    for candidate in {entry.value, entry.label}:
        shown = _plain(candidate)
        # A bare number compares by meaning below: "3" inside "3千" is not the
        # person's 3, it is part of their 3000.
        if shown and not _BARE_NUMBER.fullmatch(shown) and any(shown in w for w in plain_words):
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


_RANGE = re.compile(r"\s*(?:~|～|—|至|到)\s*")


def _range_parts(value: str) -> tuple[str, str] | None:
    """A "start ~ end" entry value split into its two ends, if it is one."""
    if not isinstance(value, str):
        return None
    parts = [part.strip() for part in _RANGE.split(value) if part.strip()]
    return (parts[0], parts[1]) if len(parts) == 2 and all(
        _datetime(part) is not None for part in parts) else None


def _candidate_match(entry: Entry, ledger: SourceLedger) -> bool:
    """A bound candidate read this turn before the value was entered."""
    if entry.candidate:
        return True
    for cand in ledger.candidates:
        if cand.seq >= entry.seq:
            continue                      # read after the fill cannot source it
        if cand.binding:
            if _plain(cand.binding) != _plain(entry.field):
                continue                  # a column's cell sources its own field only
        elif entry.kind != "select":
            continue                      # a record's name/id sources selections only
        if _same_value(cand.value, entry.value) \
                or (entry.label and _same_value(cand.value, entry.label)):
            return True
    return False


def _receipt_match(entry: Entry, ledger: SourceLedger, fieldless: dict) -> bool:
    """A typed receipt sources the entry; a fieldless one binds on first use only."""
    for receipt in ledger.receipts:
        if _has_time(entry.value) and not _has_time(receipt.value):
            continue                      # a date alone does not give a time of day
        if not _same_value(receipt.value, entry.value):
            continue
        if receipt.field:
            if receipt.field == entry.field:
                return True
            continue
        bound = fieldless.get(id(receipt))
        if bound is None:
            fieldless[id(receipt)] = entry
            return True
        if bound is entry:
            return True
    return False


def _existing_append_match(
    entry: Entry,
    ledger: SourceLedger,
    words: list[str],
    fieldless: dict,
    existing: dict[str, str] | None,
) -> Verdict | None:
    """When editing an existing record/form, preserving a field's loaded text and
    appending (or prepending) a sourced addition is valid."""
    if not existing or not isinstance(entry.value, str):
        return None
    old = str(existing.get(_plain(entry.field)) or "").strip()
    val = entry.value.strip()
    if not old or not val:
        return None
    if val == old:
        return Verdict(entry, True, "candidate")
    separators = " \t\r\n,，;；:：、/|-—"
    rest = ""
    if val.startswith(old):
        rest = val[len(old):].lstrip(separators)
    elif val.endswith(old):
        rest = val[:-len(old)].rstrip(separators)
    if not rest:
        return None
    sub = _verdict(replace(entry, value=rest), ledger, words, fieldless, existing=None)
    return Verdict(entry, True, sub.source) if sub.ok else None


def _verdict(
    entry: Entry,
    ledger: SourceLedger,
    words: list[str],
    fieldless: dict,
    *,
    existing: dict[str, str] | None = None,
) -> Verdict:
    parts = _range_parts(entry.value)
    if parts is not None:
        verdicts = [_verdict(replace(entry, value=part), ledger, words, fieldless, existing=existing)
                    for part in parts]
        if all(v.ok for v in verdicts):
            source = verdicts[0].source if all(v.source == verdicts[0].source
                                               for v in verdicts) else "mixed"
            return Verdict(entry, True, source)
        return Verdict(entry, False, "", "not_from_the_person")
    if not entry.placed:
        return Verdict(entry, False, "", "unplaced_fill")
    if not str(entry.value).strip() and not entry.label:
        return Verdict(entry, True, "user_text")
    if user_supplied(entry, words):
        return Verdict(entry, True, "user_text")
    if _candidate_match(entry, ledger):
        return Verdict(entry, True, "candidate")
    if _receipt_match(entry, ledger, fieldless):
        return Verdict(entry, True, "receipt")
    appended = _existing_append_match(entry, ledger, words, fieldless, existing)
    if appended is not None:
        return appended
    return Verdict(entry, False, "", "not_from_the_person")


_APPEND_CLAUSE_SPLIT = re.compile(r"[，,；;。！？!\n]+")
_APPEND_NEGATION = re.compile(r"(?:不|别|勿|无需|无须|不用|不必)(?:要|用)?(?:追加|补充|附上|附注|续写|补写)")
_APPEND_OVERWRITE = re.compile(r"覆盖|替换|清空|重写|代替")
_APPEND_VERB = re.compile(
    r"追加|补充|附上|附注|续写|补写"
    r"|后(?:面|边)?(?:加|添|写|补)"
    r"|末尾(?:加|添|写|补)"
    r"|(?:在|往)[^，,；;。！？!\n]{0,16}(?:备注|说明|描述|附言|内容)[^，,；;。！？!\n]{0,6}(?:加上?|添上?|写上?|补上?)"
)
_TEXT_NOTE_FIELD = re.compile(
    r"^(?:remark|description|memo|note|notes|comment|content|body|备注|说明|描述|附言|附注|正文|内容)$",
    re.IGNORECASE,
)


def append_overwrites_existing(prompt: str, field_name: str, before_value, after_value) -> bool:
    """True when the user asked to append/supplement a text field that already held
    non-empty text, and the proposed value drops that existing text instead."""
    if not isinstance(before_value, str) or not before_value.strip():
        return False
    if not isinstance(after_value, str):
        return False
    old_s = before_value.strip()
    new_s = after_value.strip()
    if old_s in new_s or (_plain(old_s) and _plain(old_s) in _plain(new_s)):
        return False
    clauses = [
        c for c in _APPEND_CLAUSE_SPLIT.split(prompt or "")
        if c and _APPEND_VERB.search(c)
        and not _APPEND_NEGATION.search(c)
        and not _APPEND_OVERWRITE.search(c)
    ]
    if not clauses:
        return False
    label = (field_name or "").strip()
    if _TEXT_NOTE_FIELD.match(label):
        return not any(
            re.search(rf"{re.escape(label)}[^，,；;。！？!\n]{{0,4}}(?:改成|改为|设为|置为|修改为|更新为)", c)
            for c in _APPEND_CLAUSE_SPLIT.split(prompt or "") if c
        )
    return bool(label and not label.isascii() and any(label in c for c in clauses))


def check_entries(
    ledger: SourceLedger,
    words: list[str],
    *,
    existing: dict[str, str] | None = None,
) -> list[Verdict]:
    """A source verdict per business form value; missing sources are shown on the card."""
    fieldless: dict = {}
    normalized_existing = (
        {_plain(k): str(v) for k, v in existing.items() if isinstance(v, str) and v.strip()}
        if existing else None
    )
    return [_verdict(entry, ledger, words, fieldless, existing=normalized_existing) for entry in ledger.entries]
