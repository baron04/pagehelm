"""Keep declared targets and page requirements during a bounded repair.

Names and labels may be corrected against the actor's index. Removing a target or
interaction, however, is not progress towards the original requested deliverable.
This checks typed declarations, not natural-language intent or runtime authority.
"""
from __future__ import annotations

from .spec import GoalSpec


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
