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
