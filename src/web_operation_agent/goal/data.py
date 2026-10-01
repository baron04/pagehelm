"""The data an application offers a goal-directed turn, in the index's own terms.

The core resolves a declaration: what a condition means, which record a name picks
out, when a page's defaults are lifted, how lines relate to their document. It asks the
application only for facts, through a `DataPort` compiled under the acting user:

- the schema of its entities, fields and pages;
- records found by name;
- field values of one record;
- counts, rows, totals and groups of the records a condition describes;
- the address of a page or a record.

A condition is a small tree (`Compare`, `All`, `Any`, `Not`) over field keys. An
adapter translates it into its own query language; one without a query language for
some comparison reads the records it can and filters them with `matches`. A page's own
scope may be a `Native` clause the core never looks inside: only the adapter that
produced it executes it, and it cannot be filtered in memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, Union


# -- conditions -------------------------------------------------------------------------

#: Comparison operators. Text comparisons ignore case. `contains`/`starts` apply to text
#: and to a link's display name; `set`/`unset` take no value; `in`/`not_in`/`under` take
#: a tuple. For a link field the value of `eq`/`ne`/`in`/`not_in` is the target record's
#: id; `under` is the target records or any record below them (a company's contacts),
#: the same as `in` where the application has no such hierarchy.
OPERATORS = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "in", "not_in", "under",
                       "contains", "not_contains", "starts", "set", "unset"})

#: Pseudo-fields every entity has: the record's name as displayed, and its id.
NAME = "@name"
ID = "@id"


@dataclass(frozen=True)
class Compare:
    #: A field key of the entity queried, or a dotted path through link fields
    #: ("order.state") when a line entity is scoped by its document.
    field: str
    op: str
    value: object = None

    def __post_init__(self) -> None:
        if self.op not in OPERATORS:
            raise ValueError(f"unknown operator {self.op!r}")


@dataclass(frozen=True)
class All:
    items: tuple["Cond", ...] = ()


@dataclass(frozen=True)
class Any:
    items: tuple["Cond", ...] = ()


@dataclass(frozen=True)
class Not:
    item: "Cond"


@dataclass(frozen=True)
class Native:
    """A clause in the adapter's own query language (a page's saved domain). `label`
    says what it selects, for the reply; `fields` the fields it constrains, so that a
    condition on the same field can replace it. The core only combines it with others."""
    payload: object
    label: str = ""
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class Via:
    """A condition on the record a link field points to (a line's order)."""
    link: str
    item: "Cond"


Cond = Union[Compare, All, Any, Not, Native, Via]

#: The condition every record satisfies.
EVERYTHING: Cond = All(())


def all_of(*items: Cond | None) -> Cond:
    """The conjunction of the given conditions, flattened; None and EVERYTHING vanish."""
    out: list[Cond] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, All):
            out.extend(item.items)
        else:
            out.append(item)
    return out[0] if len(out) == 1 else All(tuple(out))


def any_of(*items: Cond) -> Cond:
    """The disjunction; an EVERYTHING among them makes the whole EVERYTHING."""
    out: list[Cond] = []
    for item in items:
        if item == EVERYTHING:
            return EVERYTHING
        if isinstance(item, Any):
            out.extend(item.items)
        else:
            out.append(item)
    return out[0] if len(out) == 1 else Any(tuple(out))


def parts(cond: Cond) -> tuple[Cond, ...]:
    """The conjuncts of a condition (itself when it is not a conjunction)."""
    if isinstance(cond, All):
        return cond.items
    return (cond,)


def is_native(cond: Cond) -> bool:
    if isinstance(cond, Native):
        return True
    if isinstance(cond, (All, Any)):
        return any(is_native(c) for c in cond.items)
    if isinstance(cond, (Not, Via)):
        return is_native(cond.item)
    return False


class NotEvaluable(ValueError):
    """A condition that cannot be decided in memory (a `Native` clause)."""


def matches(cond: Cond, row: "Row") -> bool:
    """Whether a row read from the application satisfies a condition.

    For adapters whose API cannot filter by every comparison: they read the records the
    acting user may see and filter here. Values compare by kind: numbers as numbers,
    dates and datetimes as such, text case-insensitively, links by target id (or, for
    `contains`/`starts`, by the target's display name).
    """
    if isinstance(cond, All):
        return all(matches(c, row) for c in cond.items)
    if isinstance(cond, Any):
        return any(matches(c, row) for c in cond.items)
    if isinstance(cond, Not):
        return not matches(cond.item, row)
    if isinstance(cond, (Native, Via)):
        raise NotEvaluable(getattr(cond, "label", "") or type(cond).__name__)
    value = row.display if cond.field == NAME else row.id if cond.field == ID \
        else row.values.get(cond.field)
    op = "in" if cond.op == "under" else cond.op
    if op == "set":
        return not _empty(value)
    if op == "unset":
        return _empty(value)
    if isinstance(value, LinkValue):
        if op in ("contains", "not_contains", "starts"):
            return _text_op(op, value.display, cond.value)
        value = value.id
    if op in ("in", "not_in"):
        hit = any(_equal(value, v) for v in (cond.value or ()))
        return hit if op == "in" else not hit
    if op in ("contains", "not_contains", "starts"):
        return _text_op(op, "" if value is None else str(value), cond.value)
    if _empty(value):
        # Nothing compares to an empty field except "not equal".
        return op == "ne" and not _empty(cond.value)
    if op == "eq":
        return _equal(value, cond.value)
    if op == "ne":
        return not _equal(value, cond.value)
    left, right = _comparable(value, cond.value)
    if left is None:
        return False
    return {"lt": left < right, "le": left <= right, "gt": left > right, "ge": left >= right}[op]


def _empty(value) -> bool:
    # A yes/no field's `false` is a value the application holds, not an absence: treating
    # it as empty made every `eq` against it fail, silently ("还在付款的订单" counted 0).
    return value is None or value == "" or value == () or value == []


#: The words a yes/no field is written with, in a Chinese or an English deployment.
_YES_WORDS = frozenset({"true", "yes", "y", "1", "是"})
_NO_WORDS = frozenset({"false", "no", "n", "0", "否"})


def as_bool(value) -> bool | None:
    """A yes/no value as a boolean, or None when it is not one.

    A state word ("未支付", "已发货") is not a yes/no word; coercing it with `bool()`
    turned every non-empty word into true and flipped the comparison.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().casefold()
        if word in _YES_WORDS:
            return True
        if word in _NO_WORDS:
            return False
    return None


def _text_op(op: str, text: str, wanted) -> bool:
    haystack, needle = str(text).casefold(), str(wanted or "").casefold()
    if op == "starts":
        return haystack.startswith(needle)
    hit = needle in haystack
    return hit if op == "contains" else not hit


def _equal(left, right) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    a, b = _comparable(left, right)
    if a is not None:
        return a == b
    return str(left) == str(right)


def _comparable(left, right):
    """Both sides as numbers, datetimes or text, or (None, None) when they do not mix."""
    if isinstance(left, bool) or isinstance(right, bool):
        a, b = as_bool(left), as_bool(right)
        # A word that is not a yes/no word compares as nothing rather than as true.
        return (a, b) if a is not None and b is not None else (None, None)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left), float(right)
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        try:
            return float(str(left).replace(",", "")), float(str(right).replace(",", ""))
        except ValueError:
            return None, None
    a, b = _moment(left), _moment(right)
    if a is not None and b is not None:
        return a, b
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold(), right.casefold()
    return None, None


def _moment(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is None else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and len(value) >= 10 and value[4:5] == "-":
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")[:26])
        except ValueError:
            return None
    return None


# -- what comes back ------------------------------------------------------------------------

@dataclass(frozen=True)
class LinkValue:
    """A link field's value on a row: the target record's id and display name."""
    id: str
    display: str


@dataclass(frozen=True)
class Ref:
    """A record found by name."""
    entity: str
    id: str
    #: As the application displays it ("[FURN_0269] Office Chair Black").
    display: str
    #: Its own name, without what the display adds (a code in brackets): what a
    #: person means when they say the name.
    name: str = ""
    #: The record this one belongs to, when its display leads with it (a contact shown
    #: "Company, Person"): the company is the likelier referent of the shared name.
    owner: str = ""

    @property
    def own_name(self) -> str:
        return self.name or self.display


@dataclass(frozen=True)
class Row:
    id: str
    display: str
    #: field key -> value in comparable form: str, number, bool, date/datetime (ISO
    #: text is accepted), LinkValue, a choice's key, or a tuple of LinkValue for a
    #: many-valued link.
    values: dict = field(default_factory=dict)
    #: field key -> the value as the application shows it ("1,250.00 EUR", "Posted").
    shown: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    count: int


# -- the schema ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Choice:
    key: str
    label: str
    #: The label in the person's language, as the application translates it.
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldSchema:
    key: str
    label: str
    #: text | number | money | date | datetime | choice | link | links | lines | html | bool | other
    kind: str
    #: The entity a link, links or lines field points to.
    relation: str = ""
    choices: tuple[Choice, ...] = ()
    #: False for a field the application cannot filter by (a computed value).
    searchable: bool = True
    #: Where it shows: on the list page, on the record's page; the tab it sits in.
    listed: bool = False
    on_form: bool = False
    tab: str = ""
    aliases: tuple[str, ...] = ()
    #: For a `lines` field: the link field on the line entity that points back, and the
    #: condition that picks, among that entity's records, the ones the form embeds
    #: (product lines, not tax lines).
    inverse: str = ""
    embedded: Cond = EVERYTHING
    #: For a `lines` field: the line entity's number and money fields the form shows,
    #: which a question about one line's values reads.
    columns: tuple[str, ...] = ()
    #: For a link: whether a record's sub-records count as the record ("a customer's
    #: orders" includes its contacts' orders).
    inclusive: bool = False


@dataclass(frozen=True)
class FilterSchema:
    """A named filter a page offers. A filter relative to today is a `Native` clause
    the adapter evaluates at the time of the read."""
    key: str
    label: str
    cond: Cond | None
    #: The date field a date filter offers periods of, when it is one.
    date_field: str = ""
    group_by: str = ""
    #: False when the filter depends on who is asking ("My orders").
    usable: bool = True
    #: True when the filter's meaning depends on the current date ("This month").
    relative: bool = False


@dataclass(frozen=True)
class PageSchema:
    key: str
    entity: str
    label: str
    #: The records the page shows at all (its saved domain).
    scope: Cond = EVERYTHING
    #: Filters the page applies when opened; lifted when they hide what was asked.
    defaults: tuple[FilterSchema, ...] = ()
    filters: tuple[FilterSchema, ...] = ()
    #: False when the page cannot open a record (no form).
    opens_records: bool = True
    #: Operations the acting user may start from it ("create").
    operations: tuple[str, ...] = ()
    #: Whether the application's navigation leads to it (not only a direct address).
    menu: bool = True


@dataclass(frozen=True)
class EntitySchema:
    key: str
    label: str
    fields: tuple[FieldSchema, ...] = ()
    filters: tuple[FilterSchema, ...] = ()
    sections: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    #: create | read | write | delete, as the acting user may.
    operations: frozenset = frozenset()
    #: For a line entity: the entity whose records own these lines.
    lines_of: str = ""
    #: The text field holding a record's own name, when there is one.
    name_field: str = ""
    #: The money field that is a record's total (an order's amount due), when it has one:
    #: what a bare amount in a request most likely means.
    total: str = ""

    @property
    def shown(self) -> tuple["FieldSchema", ...]:
        """The fields a list or a record's page shows, list columns first."""
        return tuple(f for f in self.fields if f.listed or f.on_form)

    @property
    def main_date(self) -> str:
        """The date a period without a field means (an order's date)."""
        return next((f.key for f in self.shown if f.kind in ("date", "datetime")), "")

    def field(self, key: str) -> FieldSchema | None:
        return next((f for f in self.fields if f.key == key), None)


@dataclass(frozen=True)
class Schema:
    entities: tuple[EntitySchema, ...] = ()
    pages: tuple[PageSchema, ...] = ()

    def entity(self, key: str) -> EntitySchema | None:
        return next((e for e in self.entities if e.key == key), None)

    def page(self, key: str) -> PageSchema | None:
        return next((p for p in self.pages if p.key == key), None)


# -- the port --------------------------------------------------------------------------------

class DataRefused(RuntimeError):
    """The application refused a read (not a missing record: an error or a denial).

    `reason` is the application's own sentence for the refusal, when its answer carried
    one ("请选择时间范围"), in that application's language; `str(self)` keeps the endpoint
    and the code, for the audit and the repair.
    """

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class DataPort(Protocol):
    """Read-only access to the application under the acting user.

    Every method may raise `DataRefused`. `where` may contain `Native` clauses only
    from this port's own schema.
    """

    def schema(self) -> Schema: ...

    def time_zone(self) -> str: ...

    def find(self, entity: str, text: str, *, limit: int = 8) -> tuple[Ref, ...]:
        """Records whose name the application matches to `text`, most likely first."""

    def name_of(self, display: str) -> str:
        """A record's own name within how the application displays it ("Office Chair"
        for "[FURN_7777] Office Chair"); the display itself when it adds nothing."""

    def labels_for(self, text: str) -> tuple[str, ...]:
        """Optional. The labels the application's own translations render as `text`,
        most specific first; a port without translations need not define it."""

    def read(self, entity: str, id: str, fields: tuple[str, ...]) -> Row: ...

    def count(self, entity: str, where: Cond) -> int: ...

    def rows(self, entity: str, where: Cond, *, fields: tuple[str, ...] = (),
             order: tuple[tuple[str, bool], ...] = (), limit: int = 80) -> tuple[Row, ...]:
        """At most `limit` rows; `order` is (field key, descending) pairs."""

    def total(self, entity: str, where: Cond, field: str) -> tuple[float, str]:
        """The complete scoped sum and unit. Raise DataRefused for unknown values;
        missing values are not zero unless the application's field contract says so."""

    def groups(self, entity: str, where: Cond, field: str, *, limit: int = 12,
               by_month: bool = False) -> tuple[Group, ...]:
        """Counts per value of a field, largest first."""

    def page_address(self, page: str, *, view: str = "") -> str: ...

    def record_address(self, entity: str, id: str) -> str: ...


__all__ = ["All", "Any", "Choice", "Compare", "Cond", "DataPort", "DataRefused", "EVERYTHING",
           "EntitySchema", "FieldSchema", "FilterSchema", "Group", "LinkValue", "Native", "Not",
           "ID", "NAME", "NotEvaluable", "OPERATORS", "Via", "PageSchema", "Ref", "Row", "Schema", "all_of", "any_of",
           "as_bool",
           "is_native", "matches", "parts"]
