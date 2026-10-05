"""Grounding a request's words in the application's real values, before declaration.

The model is good at choosing, from a short list, the value a person's words mean -- a
Chinese description of an English product name, a partial or reordered name, an order
described by its customer and amount -- and bad at producing an exact string blind. So the
adapter is asked, with the records the request already names, for the values around them
(an order's lines, a customer's recent documents), each gets an id, the model declares ids,
and the core binds the ids back to the exact names before anything is resolved.
"""
from __future__ import annotations

from dataclasses import replace
import re

from .ports import Candidate
from .spec import Filter, GoalSpec, Metric

#: Values shown to the model in one turn; a short list is what a small model reads well.
_MAX_CANDIDATES = 40
_ID = re.compile(r"^\s*\[?(v\d{1,3})\]?\s*$")


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_CLOCK = re.compile(r"(?<!\d)(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?!\d)")
_MONTH_DAY = re.compile(r"(?:(\d{4})\s*年\s*)?(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_SLASH = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
_NAMED = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.I)
_CJK_MONTH_DAY = re.compile(r"(?:(\d{4})\s*年\s*)?(?<![一二三四五六七八九十])([一二三四五六七八九十]{1,2})月"
                            r"([一二三四五六七八九十]{1,3})[日号]")


def days(text: str, today: str) -> tuple[str, ...]:
    """The calendar days a request names by shape (2026-08-29, 8 月 29 日, 八月二日, 8/29, Aug 29),
    as ISO dates; a day without a year is in `today`'s year."""
    year = (today or "")[:4]
    found: list[tuple[str, str, str]] = []
    for match in _ISO.finditer(text or ""):
        found.append(match.groups())
    for match in _MONTH_DAY.finditer(text or ""):
        found.append((match.group(1) or year, match.group(2), match.group(3)))
    for match in _SLASH.finditer(text or ""):
        found.append((year, match.group(1), match.group(2)))
    for match in _NAMED.finditer(text or ""):
        found.append((year, str(_MONTHS[match.group(1).lower()[:3]]), match.group(2)))
    for match in _CJK_MONTH_DAY.finditer(text or ""):
        month, day = _cjk_small(match.group(2)), _cjk_small(match.group(3))
        if month and day:
            found.append((match.group(1) or year, str(month), str(day)))
    out = []
    for y, m, d in found:
        if y and 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            out.append(f"{int(y):04d}-{int(m):02d}-{int(d):02d}")
    return tuple(dict.fromkeys(out))[:4]


def without_days(text: str) -> str:
    """The text with the days `days` reads taken out, so their numbers are not taken
    for amounts or codes."""
    for pattern in (_ISO, _MONTH_DAY, _SLASH, _NAMED, _CJK_MONTH_DAY):
        text = pattern.sub(" ", text or "")
    return _CLOCK.sub(" ", text)


def ground(port, *, entities: tuple[str, ...], known, dates: tuple[str, ...] = (),
           context=None) -> tuple[Candidate, ...]:
    """The candidate values for this request from a port that offers them, numbered."""
    method = getattr(port, "candidates", None)
    if not callable(method):
        return ()
    found = tuple(method(entities=entities, known=tuple((k.entity, k.name) for k in known),
                         dates=dates, context=context) or ())[:_MAX_CANDIDATES]
    return tuple(replace(c, id=f"v{i}") for i, c in enumerate(found, start=1))


def render(candidates: tuple[Candidate, ...]) -> str:
    """One line per value: id, kind and entity, the exact name, and what distinguishes it."""
    return "\n".join(
        f"{c.id} {c.kind} {c.entity}: {c.display}" + (f" — {c.detail}" if c.detail else "")
        for c in candidates)


def bind(spec: GoalSpec, candidates: tuple[Candidate, ...]) -> tuple[GoalSpec, int, tuple[str, ...]]:
    """The declaration with value ids replaced by what they stand for.

    A value that is a name binds to that name. A value that stands for several
    conditions (one line: its record and its item) binds, wherever its id is written, to
    all of them as conditions, so the record it belongs to is never lost. Returns (spec,
    how many ids were bound, ids that name no candidate). An id is only ever a whole value
    (`v12`); words the model wrote itself are left as they are.
    """
    by_id = {c.id: c for c in candidates}
    bound = 0
    unknown: list[str] = []

    def lookup(value: str):
        nonlocal bound
        match = _ID.match(value or "")
        if not match:
            return None
        candidate = by_id.get(match.group(1))
        if candidate is None:
            unknown.append(match.group(1))
            return None
        bound += 1
        return candidate

    out = []
    for d in spec.deliverables:
        changes = {}
        conditions: list = []
        for name in ("record", "value", "action"):
            current = getattr(d, name)
            candidate = lookup(current) if current else None
            if candidate is None:
                continue
            if name == "record" and candidate.binds:
                changes["record"] = ""
                conditions.extend(candidate.binds)
            else:
                changes[name] = candidate.display
        if d.page.search:
            candidate = lookup(d.page.search)
            if candidate is not None:
                changes["page"] = replace(d.page, search=candidate.display)
        if d.page.conditions:
            page_conditions = []
            for condition in d.page.conditions:
                values = []
                for value in condition.values:
                    candidate = lookup(value)
                    values.append(candidate.display if candidate else value)
                    if candidate:
                        page_conditions.extend(Filter(label, (bound_value,))
                            for label, bound_value in candidate.binds if bound_value != candidate.display)
                page_conditions.append(replace(condition, values=tuple(values)))
            changes["page"] = replace(changes.get("page", d.page), conditions=tuple(page_conditions))
        filters = list(d.metric.filters) if d.metric is not None else []
        for i, f in enumerate(filters):
            values = []
            for v in f.values:
                candidate = lookup(v)
                if candidate is None:
                    values.append(v)
                    continue
                values.append(candidate.display)
                conditions.extend(pair for pair in candidate.binds if pair[1] != candidate.display)
            filters[i] = replace(f, values=tuple(values))
        present = {_norm_label(f.field) for f in filters}
        for label, value in conditions:
            if _norm_label(label) in present:
                filters = [replace(f, values=(value,)) if _norm_label(f.field) == _norm_label(label)
                           else f for f in filters]
            else:
                filters.append(Filter(field=label, values=(value,)))
                present.add(_norm_label(label))
        if d.metric is not None or filters:
            metric = d.metric or Metric(op="count")
            if tuple(filters) != metric.filters:
                changes["metric"] = replace(metric, filters=tuple(filters))
        out.append(replace(d, **changes) if changes else d)
    if not bound and not unknown:
        return spec, 0, ()
    return replace(spec, deliverables=tuple(out)), bound, tuple(dict.fromkeys(unknown))


_CJK_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
               "七": 7, "八": 8, "九": 9}
_CJK_UNITS = {"十": 10, "百": 100, "千": 1000}
_CJK_NUMBER = re.compile(r"[零〇一二两三四五六七八九十百千万]+")


def _cjk_small(word: str) -> int | None:
    """A month or a day written in CJK numerals (二, 十二, 二十二), or None."""
    if "十" not in word:
        return _CJK_DIGITS.get(word) if len(word) == 1 else None
    tens, _, ones = word.partition("十")
    if len(tens) > 1 or len(ones) > 1:
        return None
    return (_CJK_DIGITS.get(tens, 0) if tens else 1) * 10 + (_CJK_DIGITS.get(ones, 0) if ones else 0)


def _cjk_value(word: str) -> int | None:
    """A number written in CJK numerals (两万, 一万一千七百五十), or None."""
    total, section, digit = 0, 0, 0
    for char in word:
        if char in _CJK_DIGITS:
            digit = _CJK_DIGITS[char]
        elif char in _CJK_UNITS:
            section += (digit or 1) * _CJK_UNITS[char]
            digit = 0
        elif char == "万":
            total += (section + digit) * 10_000
            section, digit = 0, 0
    value = total + section + digit
    return value if value >= 10 else None


def numbers_in(text: str) -> set[str]:
    """The amounts a request writes -- in digits or in CJK numerals -- as plain numbers
    (no separators, no trailing zero decimals); days are left to `days`."""
    text = without_days(text)
    out = set()
    for raw in re.findall(r"(?<![\w.])\d[\d,]*(?:\.\d+)?", text):
        value = raw.replace(",", "")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        if len(value.replace(".", "")) >= 2:
            out.add(value)
    for word in _CJK_NUMBER.findall(text):
        value = _cjk_value(word)
        if value is not None:
            out.add(str(value))
    return out


def _detail_numbers(text: str) -> set[str]:
    out = set()
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text or ""):
        value = raw.replace(",", "")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        out.add(value)
    return out


def pick(spec: GoalSpec, text: str, today: str,
         candidates: tuple[Candidate, ...]) -> tuple[GoalSpec, tuple[str, ...]]:
    """A declaration that describes one record without naming it, where the request's
    amounts and days fit exactly one candidate record, is pointed at that record: the
    description is what the candidates were looked up by, and a small model often writes
    the amount under a wrong field instead of choosing. Only for one record's facts,
    page or change, or for the lines of one record; one line written as its item is
    that record's line of the item. Returns (spec, names picked)."""
    wanted_numbers = numbers_in(text)
    wanted_days = set(days(text, today))
    if not (wanted_numbers or wanted_days):
        return spec, ()
    line_entities = {c.entity for c in candidates if c.kind == "line"}
    picked: list[str] = []
    out = []
    for d in spec.deliverables:
        single = d.kind in ("fact", "page", "handoff") or d.entity in line_entities
        # One line written as its item (the product) is a line of a record the request
        # only describes: the record is picked first, then its line of that item.
        items = [c for c in candidates if c.kind == "line" and c.entity == d.entity
                 and d.record and _norm_label(d.record) in _norm_label(c.display)]
        if (d.record and not items) or not single:
            out.append(d)
            continue
        fits = []
        for c in candidates:
            if c.kind != "record" or not (c.entity == d.entity or c.binds):
                continue
            found = _detail_numbers(c.detail)
            if wanted_numbers <= found and all(day in c.detail for day in wanted_days):
                fits.append(c)
        if len(fits) != 1:
            out.append(d)
            continue
        chosen = fits[0]
        picked.append(chosen.display)
        # The conditions that only described the record give way to the record itself.
        kept = tuple(f for f in (d.metric.filters if d.metric else ())
                     if not (_detail_numbers(" ".join(f.values)) & wanted_numbers
                             or any(day[:7] in v for v in f.values for day in wanted_days)))
        if items:
            # The picked record's line of that item stands for both, alone.
            mine = [c for c in items if any(value == chosen.display for _label, value in c.binds)]
            if len(mine) != 1:
                picked.pop()
                out.append(d)
                continue
            d = replace(d, record="", metric=replace(
                d.metric or Metric(op="count"),
                filters=tuple(Filter(field=label, values=(value,)) for label, value in mine[0].binds)))
        elif chosen.entity == d.entity:
            d = replace(d, record=chosen.display,
                        metric=replace(d.metric, filters=kept) if d.metric else None)
        else:
            # The record's own condition on the lines replaces whatever described it.
            labels = {_norm_label(label) for label, _value in chosen.binds}
            kept = tuple(f for f in kept if _norm_label(f.field) not in labels)
            extra = tuple(Filter(field=label, values=(value,)) for label, value in chosen.binds)
            d = replace(d, metric=replace(d.metric or Metric(op="count"), filters=kept + extra))
        out.append(d)
    if not picked:
        return spec, ()
    return replace(spec, deliverables=tuple(out)), tuple(picked)


def _norm_label(label: str) -> str:
    return "".join(str(label).casefold().split())


__all__ = ["bind", "days", "ground", "numbers_in", "pick", "render", "without_days"]
