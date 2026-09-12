"""The model chooses the operation; this runs it on the values the application returned.

"The open quotations total 7,210.00" is a claim about arithmetic over five records. No
amount of inspecting the sentence establishes it, which is why the design that tried to
check the sentence was rejected: a whitelist of values catches a number that was never
returned and misses every number that was returned and then related wrongly.

So the sum is not read out of prose, it is performed here, over evidence, and becomes
evidence of its own. Five operations are enough for the questions that went wrong -- read a
field, filter a set, count it, total it, take an extreme -- and the set stays small on
purpose: what is bounded is which operations may run on which inputs, never which questions
a reader may ask. An operation this cannot do is a reason to go and fetch more, or to say
what could not be determined; it is not a reason to refuse the question.

Combining is where a correct-looking number goes wrong. Two reads of the same collection at
different moments, under different filters, or overlapping in records, do not add up, and
neither do two currencies. Each is refused by name rather than silently reconciled, because
a total that quietly dropped a constraint is indistinguishable from one that never had it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .evidence import (
    ComputationEvidence, Origin, QueryEvidence, RecordEvidence, SelectionEvidence,
    TurnEvidence,
)

#: What may be asked of the values. `read` returns one field of one record; `filter` narrows
#: a set; the rest reduce one. Difference, ratio, ordering and joins wait for a case that
#: needs them -- each adds a way for two inputs to be incomparable, and this is the layer
#: where incomparable inputs turn into confident wrong answers.
OPERATIONS = ("read", "filter", "count", "sum", "min", "max")

#: Comparisons a filter may apply. Enough for "over 2000" and for the equality checks a
#: preflight makes; deliberately not an expression language.
COMPARISONS = ("gt", "ge", "lt", "le", "eq", "ne")

_NUMERIC = ("money", "quantity", "number")


class ComputationRefused(ValueError):
    """The operation could not be performed on these inputs, and why."""


@dataclass(frozen=True, slots=True)
class ComputationRequest:
    """What the model asked for. Validated here; executed only against real evidence."""

    operation: str
    over: tuple[str, ...]
    field_name: str = ""
    comparison: str = ""
    threshold: str = ""

    def __post_init__(self) -> None:
        if self.operation not in OPERATIONS:
            raise ComputationRefused(
                f"{self.operation!r} is not an operation this runs; it knows {OPERATIONS}")
        if not isinstance(self.over, tuple) or not self.over:
            raise ComputationRefused("a computation must name the evidence it runs on")
        if self.operation in ("read", "filter", "sum", "min", "max") and not self.field_name:
            raise ComputationRefused(f"{self.operation} needs the field it applies to")
        if self.operation == "filter":
            if self.comparison not in COMPARISONS:
                raise ComputationRefused(
                    f"a filter compares with one of {COMPARISONS}")
            if not str(self.threshold).strip():
                raise ComputationRefused("a filter needs something to compare against")

    def describe(self) -> str:
        if self.operation == "filter":
            return f"{self.field_name} {self.comparison} {self.threshold}"
        if self.operation == "count":
            return "count()"
        return f"{self.operation}({self.field_name})"


def _decimal(value: str, *, where: str) -> Decimal:
    """Parse exactly, or refuse. A number this cannot read is not a number it may total."""
    text = value.strip().replace(",", "")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        raise ComputationRefused(
            f"{where} is {value!r}, which this cannot read as a number without guessing")
    try:
        return Decimal(text)
    except InvalidOperation:  # pragma: no cover - the pattern above already excludes these
        raise ComputationRefused(f"{where} is not a number: {value!r}") from None


def _partition(turn: TurnEvidence, over: tuple[str, ...]):
    """Split the named evidence into the one query that governs it and its records."""
    items = turn.require_all(over)
    queries = [item for item in items if isinstance(item, QueryEvidence)]
    records = [item for item in items if isinstance(item, RecordEvidence)]
    others = [item for item in items
              if not isinstance(item, (QueryEvidence, RecordEvidence))]
    if others:
        raise ComputationRefused(
            "this runs on record reads within one query; it was given "
            + ", ".join(sorted(type(item).__name__ for item in others)))
    if len(queries) != 1:
        # Two queries are two scopes, and a result that spans them describes neither. One
        # query and no records is legitimate: that is how a count of a zero match is taken.
        raise ComputationRefused(
            f"a computation runs within exactly one query; it was given {len(queries)}")
    return queries[0], records


def _combinable(query: QueryEvidence, records: list[RecordEvidence],
                field_name: str) -> str:
    """Refuse inputs that do not add up, naming which check failed.

    Every one of these has produced a wrong answer somewhere: a field that was never read
    counted as a value, two currencies summed, the same record counted twice, a subtotal
    presented as a total. Returns the shared unit.
    """
    wrong_field = sorted({item.field_name for item in records} - {field_name})
    if wrong_field:
        raise ComputationRefused(
            f"these reads are of {', '.join(wrong_field)}, not {field_name}")
    unread = [item.record_id for item in records if not item.field_was_read]
    if unread:
        raise ComputationRefused(
            "a field that was never read has no value to compute with: "
            + ", ".join(sorted(unread)))
    seen = [item.record_id for item in records]
    duplicates = sorted({name for name in seen if seen.count(name) > 1})
    if duplicates:
        raise ComputationRefused(
            "the same record was given twice, so any total would double it: "
            + ", ".join(duplicates))
    outside = sorted({item.origin.scope for item in records} - {query.origin.scope})
    if outside:
        raise ComputationRefused(
            "these reads were taken under a different scope than the query that governs "
            "them, so they describe different populations")
    units = {item.unit for item in records if item.unit}
    if len(units) > 1:
        raise ComputationRefused(
            "these values are in " + ", ".join(sorted(units))
            + "; a total across them would mean nothing")
    return units.pop() if units else ""


def run(turn: TurnEvidence, request: ComputationRequest, *,
        evidence_id: str, observed_at: str):
    """Perform the operation and record its result as evidence of this turn.

    The result says whether it covered the whole scope rather than leaving that to whoever
    renders it: a sum over one page of records is a subtotal, and the distance between
    "subtotal" and "total" is the distance between a useful answer and a wrong one.
    """
    query, records = _partition(turn, request.over)
    unit = _combinable(query, records, request.field_name) if request.field_name else ""
    origin = Origin(kind="computed", reference=request.describe(),
                    observed_at=observed_at, scope=query.origin.scope)
    whole = query.covers_whole_scope

    if request.operation == "read":
        if len(records) != 1:
            raise ComputationRefused(
                f"reading one field wants one record; it was given {len(records)}")
        only = records[0]
        return turn_record(turn, ComputationEvidence(
            evidence_id=evidence_id, origin=origin, operation=request.describe(),
            inputs=request.over, value=only.value if only.value is not None else "",
            value_type=only.value_type, unit=unit, covers_whole_scope=whole))

    if request.operation == "count":
        return turn_record(turn, ComputationEvidence(
            evidence_id=evidence_id, origin=origin, operation="count()",
            inputs=request.over, value=str(len(records)), value_type="count",
            covers_whole_scope=whole))

    if request.operation == "filter":
        threshold = _decimal(request.threshold, where="the value compared against")
        kept = tuple(
            item.record_id for item in records
            if _compare(_decimal(item.value or "", where=f"{item.record_id}.{item.field_name}"),
                        request.comparison, threshold))
        return turn_record(turn, SelectionEvidence(
            evidence_id=evidence_id, origin=origin, operation="filter",
            inputs=request.over, condition=request.describe(), members=kept,
            covers_whole_scope=whole))

    values = [_decimal(item.value or "", where=f"{item.record_id}.{item.field_name}")
              for item in records]
    if not values:
        raise ComputationRefused(
            f"{request.operation} over no records has no result; a count would")
    numeric = {item.value_type for item in records}
    if not numeric <= set(_NUMERIC):
        raise ComputationRefused(
            "these values are " + ", ".join(sorted(numeric)) + ", which do not add or order")
    result = {"sum": sum(values, Decimal(0)), "min": min(values), "max": max(values)}[
        request.operation]
    # Keep the scale the inputs had: a money total that gained or lost a decimal place is a
    # different number, and rounding it here would hide which.
    scale = max((-item.as_tuple().exponent for item in values), default=0)
    return turn_record(turn, ComputationEvidence(
        evidence_id=evidence_id, origin=origin, operation=request.describe(),
        inputs=request.over, value=f"{result:.{scale}f}",
        value_type=records[0].value_type, unit=unit, covers_whole_scope=whole))


def _compare(left: Decimal, comparison: str, right: Decimal) -> bool:
    return {"gt": left > right, "ge": left >= right, "lt": left < right,
            "le": left <= right, "eq": left == right, "ne": left != right}[comparison]


def turn_record(turn: TurnEvidence, item):
    turn.record(item)
    return item
