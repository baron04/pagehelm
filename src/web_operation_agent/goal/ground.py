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
_MONTH_DAY = re.compile(r"(?:(\d{4})\s*年\s*)?(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_SLASH = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
_NAMED = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.I)


def days(text: str, today: str) -> tuple[str, ...]:
    """The calendar days a request names by shape (2026-08-29, 8 月 29 日, 8/29, Aug 29),
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
    out = []
    for y, m, d in found:
        if y and 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            out.append(f"{int(y):04d}-{int(m):02d}-{int(d):02d}")
    return tuple(dict.fromkeys(out))[:4]


def without_days(text: str) -> str:
    """The text with the days `days` reads taken out, so their numbers are not taken
    for amounts or codes."""
    for pattern in (_ISO, _MONTH_DAY, _SLASH, _NAMED):
        text = pattern.sub(" ", text or "")
    return text


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


def _norm_label(label: str) -> str:
    return "".join(str(label).casefold().split())


__all__ = ["bind", "days", "ground", "render", "without_days"]
