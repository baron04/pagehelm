"""Narrow checks of explicit request shapes, without inventing query semantics."""
from __future__ import annotations

import re

from .ports import Resolved


def _norm(value):
    return "".join(value.casefold().split()).strip('“”「」\"\'')


def missing_group(deliverable, request, index):
    """Reject a total substituted for an explicitly named, indexed count grouping.

    Only literal field labels/aliases in an explicit grouping phrase qualify. No
    inferred category, field translation, top-N or automatic declaration repair.
    Page grouping and numeric extremes keep their own contracts.
    """
    if (index is None or deliverable.kind != "aggregate" or deliverable.metric is None
            or deliverable.metric.op != "count"):
        return None
    entity = index.entity(deliverable.entity)
    if entity is None:
        return None
    phrases = list(re.finditer(r"按\s*([^，。；！？\n]{1,80}?)\s*分组", request))
    phrases += list(re.finditer(r"\bgroup(?:ed|ing)?\s+by\s+([^,.;!?\n]{1,80})", request, re.I))
    for match in phrases:
        before = request[:match.start()]
        if re.search(r"(?:不要|不|无需|别|not|don't|do not)\s*$", before, re.I):
            continue
        context = re.split(r"[，。；！？,.;!?\n]", before)[-1]
        named = [e.key for e in index.entities if any(
            _norm(label) and _norm(label) in _norm(context) for label in (e.label, *e.aliases))]
        if named and entity.key not in named:
            continue
        written = _norm(match.group(1))
        fields = [f for f in entity.fields if written in {_norm(f.label), *map(_norm, f.aliases)}]
        if len(fields) != 1:
            continue
        field = fields[0]
        if _norm(deliverable.metric.group_by) in {_norm(field.label), *map(_norm, field.aliases)}:
            continue
        return Resolved(kind=deliverable.kind, problem="explicit count grouping was not declared",
                        missing_group=field.label,
                        repair=f"The request explicitly groups {entity.key} by {field.label!r}; "
                               "declare metric.group_by with that indexed field, preserving the scope "
                               "and other goals. A total alone does not answer the per-group counts.")
    return None


def missing_form_values(deliverable, request, index):
    """Empty model values require explicit literal clearing, using indexed labels.

    Unknown phrasing asks for a value instead of dispatching uncertain input.
    This narrow guard recognizes field lists, not business semantics or authority.
    """
    if index is None or deliverable.kind != "handoff":
        return None
    entity = index.entity(deliverable.entity)
    if entity is None:
        return None
    def indexed(label):
        return next((f for f in entity.fields if _norm(label) in {_norm(n) for n in (f.key, f.label, *f.aliases)}), None)

    requested = deliverable.field_values or ((deliverable.action, deliverable.value),)
    labels = sorted({n for f in entity.fields for n in (f.label, *f.aliases) if n}, key=len, reverse=True)
    if not labels:
        return None
    field = "(?:" + "|".join(re.escape(n) for n in labels) + ")"
    names = field + r"(?:\s*(?:、|,|，|和|及|与|\band\b|&)\s*" + field + r")*"
    patterns = [r"(?:\bclear\b|\bempty\b|\bblank\b|清空|置空)\s*(?:the\s+)?" + names,
                names + r"\s*(?:字段)?\s*(?:清空|置空|留空|设为空|改为空|改成空|设为\s*[\"“']\s*[\"”'])"]
    request = re.sub(r'[“「\"]([^”」\"]+)[”」\"]', " ", request)
    cleared = set()
    for pattern in patterns:
        for match in re.finditer(pattern, request, re.I):
            before = re.split(r"[。；;，,\n]", request[:match.start()])[-1]
            if re.search(r"(?:不要|别|不必|无需|不|don't|do not|never|not)", before, re.I):
                continue
            # Whole matches prevent Email from qualifying Secondary Email by substring.
            cleared.update(_norm(m.group()) for m in re.finditer(field, match.group(), re.I))
    missing = []
    supplied = set()
    for label, value in requested:
        f = indexed(label)
        if f is not None:
            supplied.add(f.label)
            if not value and not any(_norm(n) in cleared for n in (f.label, *f.aliases)):
                missing.append(f.label)
    missing.extend(f.label for label in deliverable.fields
                   if (f := indexed(label)) and f.label not in supplied)
    if not missing:
        return None
    return Resolved(kind="handoff", problem="form values were not supplied", input_fields=tuple(dict.fromkeys(missing)))
