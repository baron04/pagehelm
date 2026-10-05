"""Keep declared targets and page requirements during a bounded repair.

Names and labels may be corrected against the actor's index. Removing a target or
interaction, however, is not progress towards the original requested deliverable.
This checks typed declarations, not natural-language intent or runtime authority.
"""
from __future__ import annotations
import re

from .spec import Deliverable, GoalSpec
from .index import ApplicationIndex


def field_values_preserved(before: Deliverable, after: Deliverable,
                           index: ApplicationIndex | None, request: str = "") -> bool:
    """Relabel a draft only through unique fields of the corrected actor index.

    A model declaration is not a field identity. An application can expose a UI
    label and an API alias for the same field. Resolve both in the corrected
    entity, without fuzzy matching; preserve exact values and the record/entry.
    This grants neither form entry nor approval, which are checked later.
    """
    if not before.field_values:
        return True
    if (before.record, before.via, before.action) != (after.record, after.via, after.action):
        return False
    if before.field_values == after.field_values:
        return True
    entity = index.entity(after.entity) if index is not None else None
    if entity is None or len(before.field_values) != len(after.field_values):
        return False

    def bindings(pairs):
        result = {}
        for label, value in pairs:
            wanted = " ".join(label.split()).casefold()
            matches = [f for f in entity.fields if wanted in {
                " ".join(name.split()).casefold() for name in (f.key, f.label, *f.aliases)}]
            if len(matches) != 1 or matches[0].key in result:
                return None
            result[matches[0].key] = value
        return result

    previous, corrected = bindings(before.field_values), bindings(after.field_values)
    if previous is not None:
        return previous == corrected
    if corrected is None or sorted(v for _, v in before.field_values) != sorted(corrected.values()):
        return False
    # A wrong entity can introduce a label the requested entity never used. It
    # cannot be canonically relabelled. Admit only nonempty, literal assignments
    # the person actually wrote, each bound to one field in the corrected index.
    # This deliberately leaves paraphrases and clearing to clarification.
    for key, value in corrected.items():
        field = next(f for f in entity.fields if f.key == key)
        if not value:
            return False
        labels = sorted({field.label, *field.aliases}, key=len, reverse=True)
        label = "(?:" + "|".join(re.escape(n) for n in labels) + ")"
        pattern = (r"(?:^|[，,。；;的把]|(?i:\b(?:set|fill)\b))\s*(?i:" + label
                   + r"\s*(?:字段)?\s*(?:改填|填写|填入|填|改成|改为|设置为|设为|[:：=]|to\b)\s*"
                   + ")" + re.escape(value) + r"(?=$|[\s，,。；;、])")
        matches = list(re.finditer(pattern, request))
        if len(matches) != 1:
            return False
        clause = re.split(r"[。；;，,\n]", request[:matches[0].start()])[-1]
        if re.search(r"不要|别|不必|无需|不能|不改|don't|do not|never|\bnot\b", clause, re.I):
            return False
    return True


def lost_requirements(before: GoalSpec, after: GoalSpec) -> tuple[str, ...]:
    """Describe requirements erased by a repair, without guessing replacement names.

An unresolved record name may become explicit selection conditions, but never an
unqualified list. Page facets may be relabelled; their direction and cardinality
must survive. Runtime resolution still verifies every corrected name and condition.
"""
    if len(before.deliverables) != len(after.deliverables):
        return ("deliverable count changed",)
    losses = []
    for position, (old, new) in enumerate(zip(before.deliverables, after.deliverables)):
        def lost(detail):
            losses.append(f"deliverable {position + 1}: {detail}")

        if old.record and not (new.record or new.page.search
                               or (new.metric and new.metric.filters)):
            lost("record target removed")
        if old.section and not new.section:
            lost("record section removed")
        # A corrected target record may select its own page instead of the former
        # collection. Otherwise an explicit indexed page must remain explicit.
        if old.page.key and not (new.page.key or new.record):
            lost("page target removed")
        for name in ("view", "group_by"):
            if getattr(old.page, name) and not getattr(new.page, name):
                lost(f"page {name} removed")
        if old.page.search and not (new.page.search or new.record):
            lost("page search removed")
        if old.page.conditions:
            if new.kind != "page":
                lost("applied page filtering replaced with another deliverable")
            if len(new.page.conditions) < len(old.page.conditions):
                lost("applied page conditions removed")
            if sum(c.op == "!=" for c in new.page.conditions) < sum(c.op == "!=" for c in old.page.conditions):
                lost("applied page exclusion removed")
        for name in ("filters_on", "filters_off"):
            if len(getattr(new.page, name)) < len(getattr(old.page, name)):
                lost(f"page {name} removed")
        if old.metric:
            if new.metric is None:
                lost("query requirements removed")
            else:
                # Positive conditions may be corrected; exclusion conditions may
                # not be converted into inclusions or silently discarded.
                if len(new.metric.filters) < len(old.metric.filters):
                    lost("query conditions removed")
                if (sum(c.op == "!=" for c in new.metric.filters)
                        < sum(c.op == "!=" for c in old.metric.filters)):
                    lost("query exclusion removed")
                if len(new.metric.page_filters) < len(old.metric.page_filters):
                    lost("query page filters removed")
                if old.metric.group_by and not new.metric.group_by:
                    lost("query grouping removed")
        if old.sort and not new.sort:
            lost("ordering removed")
    return tuple(losses)
