"""What the person asked for, as the model declares it once against the index.

The declaration is bounded and typed. Everything the model can name here is a label from
the index (an entity, a field, a page, a filter, a section, an action) or a piece of the
person's own words (a record reference, a value, a search term). It never names a route,
a tool, a permission or a database field.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json

KINDS = ("fact", "aggregate", "list", "page", "handoff")
METRIC_OPS = ("count", "sum")
_MAX_DELIVERABLES = 4
_MAX_TEXT = 120


def _text(value: object, name: str, *, allow_empty: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = " ".join(value.split())[:_MAX_TEXT]
    if not value and not allow_empty:
        raise ValueError(f"{name} must not be empty")
    return value


def _texts(value: object, name: str, *, limit: int = 8) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array of strings")
    out = []
    for item in value[:limit]:
        text = _text(item, name)
        if text:
            out.append(text)
    return tuple(out)


FILTER_OPS = ("", "=", "!=", "<", "<=", ">", ">=", "between", "contains")


@dataclass(frozen=True)
class Filter:
    field: str
    #: One or several values, in the words of the index (choice labels) or the person. A
    #: date field also takes a day, month or year (2026-09-12, 2026-09, 2026), a range
    #: (2026-09-01..2026-09-30) or a relative period (today, this month, last quarter,
    #: last 30 days, overdue).
    values: tuple[str, ...]
    #: Comparison for numbers, dates and text; empty means equality or membership.
    op: str = ""

    @classmethod
    def from_json(cls, value: object) -> "Filter":
        if not isinstance(value, dict):
            raise ValueError("filter must be an object")
        values = value.get("values", value.get("value"))
        op = _text(value.get("op"), "filter op").lower()
        if op not in FILTER_OPS:
            raise ValueError(f"filter op must be one of {', '.join(o for o in FILTER_OPS if o)}")
        return cls(field=_text(value.get("field"), "filter field", allow_empty=False),
                   values=_texts(values, "filter values", limit=6), op=op)


@dataclass(frozen=True)
class Metric:
    op: str
    #: The field summed, for `sum`; empty for a count.
    field: str = ""
    filters: tuple[Filter, ...] = ()
    #: Page filters, by name, whose scope applies (e.g. "To pay").
    page_filters: tuple[str, ...] = ()
    group_by: str = ""
    #: Report only the largest N groups; 0 means every group.
    top: int = 0

    @classmethod
    def from_json(cls, value: object) -> "Metric":
        if not isinstance(value, dict):
            raise ValueError("metric must be an object")
        op = _text(value.get("op"), "metric op").lower() or "count"
        if op not in METRIC_OPS:
            raise ValueError("metric op must be count or sum")
        top = value.get("top", 0) or 0
        if isinstance(top, bool) or not isinstance(top, int) or top < 0 or top > 20:
            raise ValueError("metric top must be an integer from 0 to 20")
        raw_filters = value.get("filters") or []
        if not isinstance(raw_filters, list):
            raise ValueError("metric filters must be an array")
        return cls(op=op, field=_text(value.get("field"), "metric field"),
                   filters=tuple(Filter.from_json(f) for f in raw_filters[:6]),
                   page_filters=_texts(value.get("page_filters"), "page filters"),
                   group_by=_text(value.get("group_by"), "group_by"), top=top)


@dataclass(frozen=True)
class PageSpec:
    #: A page key from the index, or empty when a record's own page is meant.
    key: str = ""
    view: str = ""
    filters_on: tuple[str, ...] = ()
    filters_off: tuple[str, ...] = ()
    group_by: str = ""
    search: str = ""

    @classmethod
    def from_json(cls, value: object) -> "PageSpec":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("page must be an object")
        return cls(key=_text(value.get("key"), "page key"),
                   view=_text(value.get("view"), "view").lower(),
                   filters_on=_texts(value.get("filters_on"), "filters_on"),
                   filters_off=_texts(value.get("filters_off"), "filters_off"),
                   group_by=_text(value.get("group_by"), "group_by"),
                   search=_text(value.get("search"), "search"))

    @property
    def empty(self) -> bool:
        return not any((self.key, self.view, self.filters_on, self.filters_off,
                        self.group_by, self.search))


@dataclass(frozen=True)
class Deliverable:
    kind: str
    entity: str = ""
    #: The record the person named, in their words ("S00006", "Office Chair").
    record: str = ""
    #: A link field to follow from that record to the record actually meant
    #: ("Customer" on an order, when the person asks about the order's customer).
    via: str = ""
    fields: tuple[str, ...] = ()
    metric: Metric | None = None
    page: PageSpec = field(default_factory=PageSpec)
    section: str = ""
    action: str = ""
    #: For a list: the field to order by, its direction, and how many records to show.
    sort: str = ""
    descending: bool = False
    limit: int = 0

    @classmethod
    def from_json(cls, value: object) -> "Deliverable":
        if not isinstance(value, dict):
            raise ValueError("deliverable must be an object")
        kind = _text(value.get("kind"), "kind").lower()
        if kind not in KINDS:
            raise ValueError(f"deliverable kind must be one of {', '.join(KINDS)}")
        metric = value.get("metric")
        limit = value.get("limit", 0) or 0
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0 or limit > 50:
            raise ValueError("limit must be an integer from 0 to 50")
        return cls(kind=kind,
                   entity=_text(value.get("entity"), "entity"),
                   record=_text(value.get("record"), "record"),
                   via=_text(value.get("via"), "via"),
                   fields=_texts(value.get("fields"), "fields", limit=12),
                   metric=Metric.from_json(metric) if metric else None,
                   page=PageSpec.from_json(value.get("page")),
                   section=_text(value.get("section"), "section"),
                   action=_text(value.get("action"), "action"),
                   sort=_text(value.get("sort"), "sort"),
                   descending=bool(value.get("descending", False)), limit=limit)

    def to_json(self) -> dict:
        out: dict = {"kind": self.kind}
        for name in ("entity", "record", "via", "section", "action"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        if self.fields:
            out["fields"] = list(self.fields)
        if self.sort:
            out["sort"] = self.sort
            out["descending"] = self.descending
        if self.limit:
            out["limit"] = self.limit
        if self.metric is not None:
            out["metric"] = {
                "op": self.metric.op, "field": self.metric.field,
                "filters": [{"field": f.field, "values": list(f.values),
                             **({"op": f.op} if f.op else {})}
                            for f in self.metric.filters],
                "page_filters": list(self.metric.page_filters),
                "group_by": self.metric.group_by, "top": self.metric.top}
        if not self.page.empty:
            out["page"] = {"key": self.page.key, "view": self.page.view,
                           "filters_on": list(self.page.filters_on),
                           "filters_off": list(self.page.filters_off),
                           "group_by": self.page.group_by, "search": self.page.search}
        return out


@dataclass(frozen=True)
class GoalSpec:
    deliverables: tuple[Deliverable, ...]
    #: The person asked for something to be changed, sent, created or paid.
    write_intent: bool = False
    #: Why nothing could be declared, in the model's words, when `deliverables` is empty.
    unsupported: str = ""

    @classmethod
    def from_json(cls, value: object) -> "GoalSpec":
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError("goal must be an object")
        raw = value.get("deliverables") or []
        if isinstance(raw, dict):
            # A single deliverable written as an object rather than a one-item array.
            raw = [raw]
        if not isinstance(raw, list):
            raise ValueError("deliverables must be an array")
        deliverables = tuple(Deliverable.from_json(d) for d in raw[:_MAX_DELIVERABLES])
        return cls(deliverables=deliverables,
                   write_intent=bool(value.get("write_intent", False)),
                   unsupported=_text(value.get("unsupported"), "unsupported"))

    def to_json(self) -> dict:
        return {"deliverables": [d.to_json() for d in self.deliverables],
                "write_intent": self.write_intent, "unsupported": self.unsupported}


#: The one function the model may call to declare the goal.
DECLARE_GOAL_TOOL = {
    "type": "function",
    "function": {
        "name": "declare_goal",
        "description": ("Declare what the person wants, in terms of the application index. "
                        "One deliverable per thing asked; up to four."),
        "parameters": {
            "type": "object",
            "properties": {
                "deliverables": {
                    "type": "array",
                    "maxItems": _MAX_DELIVERABLES,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": list(KINDS)},
                            "entity": {"type": "string",
                                       "description": "entity key from the index (a record's lines are their own entity)"},
                            "record": {"type": "string",
                                       "description": "the existing record named by the person, in their words; empty when a new record is to be created"},
                            "via": {"type": "string",
                                    "description": "link field label to follow from that record, when a related record is meant"},
                            "fields": {"type": "array", "items": {"type": "string"},
                                       "description": "field labels asked about (fact)"},
                            "metric": {
                                "type": "object",
                                "properties": {
                                    "op": {"type": "string", "enum": list(METRIC_OPS)},
                                    "field": {"type": "string"},
                                    "filters": {"type": "array", "items": {
                                        "type": "object",
                                        "properties": {
                                            "field": {"type": "string"},
                                            "values": {"type": "array",
                                                       "items": {"type": "string"},
                                                       "description": "choice labels, names, numbers; for a date field: 2026-09-12, 2026-09, 2026, 2026-09-01..2026-09-30, today, this month, last month, this quarter, last quarter, this year, last year, last 30 days, overdue"},
                                            "op": {"type": "string",
                                                   "enum": [o for o in FILTER_OPS if o],
                                                   "description": "comparison for numbers, dates or text"}},
                                        "required": ["field", "values"]}},
                                    "page_filters": {"type": "array",
                                                     "items": {"type": "string"}},
                                    "group_by": {"type": "string"},
                                    "top": {"type": "integer"}},
                                "required": ["op"]},
                            "page": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string",
                                            "description": "page key from the index; empty when the record's own page is meant"},
                                    "view": {"type": "string"},
                                    "filters_on": {"type": "array", "items": {"type": "string"}},
                                    "filters_off": {"type": "array", "items": {"type": "string"}},
                                    "group_by": {"type": "string"},
                                    "search": {"type": "string",
                                               "description": "free text to search on the page, in the application's language"}}},
                            "section": {"type": "string",
                                        "description": "section (tab) name on the record's page"},
                            "action": {"type": "string",
                                       "description": "handoff: the button name from the index for a business action, the label of the field the person wants to change, or New for creating a record"},
                            "sort": {"type": "string",
                                     "description": "list: field label to order by (the largest, the latest)"},
                            "descending": {"type": "boolean"},
                            "limit": {"type": "integer", "description": "list: how many records to show"}},
                        "required": ["kind"]}},
                "write_intent": {"type": "boolean"},
                "unsupported": {"type": "string",
                                "description": "why nothing can be declared, when deliverables is empty"}},
            "required": ["deliverables"]}}}


__all__ = ["DECLARE_GOAL_TOOL", "Deliverable", "Filter", "GoalSpec", "KINDS", "Metric",
           "PageSpec"]
