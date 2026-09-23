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
from decimal import Decimal, InvalidOperation, localcontext

from .evidence import (
    ComputationEvidence, Origin, QueryEvidence, RecordEvidence, SelectionEvidence,
    TurnEvidence,
)

#: What may be asked of the values. `read` returns one field of one record; `filter` narrows
#: a set; the rest reduce one. Difference, ratio, ordering and joins wait for a case that
#: needs them -- each adds a way for two inputs to be incomparable, and this is the layer
#: where incomparable inputs turn into confident wrong answers.
OPERATIONS = ("read", "filter", "count", "sum", "min", "max", "argmin", "argmax")

#: Comparisons a filter may apply. Enough for "over 2000" and for the equality checks a
#: preflight makes; deliberately not an expression language.
COMPARISONS = ("gt", "ge", "lt", "le", "eq", "ne", "in")

_NUMERIC = ("money", "quantity", "number")
#: Values that order but do not add. An ISO-8601 date or timestamp orders by its own text, so
#: a comparison needs no calendar: the shape is checked, and a value is compared on the
#: precision the operand states, so "on or before 2026-08-18" covers that whole day.
_ORDERED_TEXT = ("date", "datetime")
_ISO_MOMENT = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")


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
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.operation not in OPERATIONS:
            raise ComputationRefused(
                f"{self.operation!r} is not an operation this runs; it knows {OPERATIONS}")
        if not isinstance(self.over, tuple) or not self.over:
            raise ComputationRefused("a computation must name the evidence it runs on")
        if (self.operation in ("read", "filter", "sum", "min", "max", "argmin", "argmax")
                and not self.field_name):
            raise ComputationRefused(f"{self.operation} needs the field it applies to")
        if self.operation == "filter":
            if self.comparison not in COMPARISONS:
                raise ComputationRefused(
                    f"a filter compares with one of {COMPARISONS}")
            if self.comparison == "in":
                if (not isinstance(self.choices, tuple) or not self.choices
                        or len(self.choices) > 50
                        or any(not isinstance(x, str) for x in self.choices)):
                    raise ComputationRefused("in needs at most 50 text choices")
            elif not isinstance(self.threshold, str) or not self.threshold.strip():
                raise ComputationRefused("a filter needs something to compare against")

    def describe(self) -> str:
        if self.operation == "filter":
            operand = repr(self.choices) if self.comparison == "in" else self.threshold
            return f"{self.field_name} {self.comparison} {operand}"
        if self.operation == "count":
            return "count()"
        return f"{self.operation}({self.field_name})"


def _decimal(value: str, *, where: str) -> Decimal:
    """Parse exactly, or refuse. A number this cannot read is not a number it may total."""
    original = value.strip()
    text = original.replace(",", "")
    if (len(text) > 128 or not re.fullmatch(
            r"-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", original)):
        raise ComputationRefused(
            f"{where} is {value!r}, which this cannot read as a number without guessing")
    try:
        return Decimal(text)
    except InvalidOperation:  # pragma: no cover - the pattern above already excludes these
        raise ComputationRefused(f"{where} is not a number: {value!r}") from None


def _partition(turn: TurnEvidence, over: tuple[str, ...], field_name: str):
    """Resolve one original query or its derived selection, never join separate reads."""
    items = turn.require_all(over)
    datasets = [item for item in items if isinstance(item, (QueryEvidence, SelectionEvidence))]
    records = [item for item in items if isinstance(item, RecordEvidence)]
    if len(datasets) != 1 or len(datasets) + len(records) != len(items):
        raise ComputationRefused("a computation runs within exactly one query or selection")
    dataset = datasets[0]
    try:
        query = turn.query_for(dataset.evidence_id)
    except ValueError as exc:
        raise ComputationRefused(str(exc)) from None
    if not records and field_name:
        records = [item for name in turn.ids()
                   if isinstance(item := turn.get(name), RecordEvidence)
                   and item.origin == query.origin and item.field_name == field_name
                   and (not isinstance(dataset, SelectionEvidence)
                        or item.record_id in dataset.members)]
    if isinstance(dataset, SelectionEvidence):
        if any(item.record_id not in dataset.members for item in records):
            raise ComputationRefused("a record is outside the selected population")
    return query, dataset, records


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
    query, dataset, records = _partition(turn, request.over, request.field_name)
    unit = _combinable(query, records, request.field_name) if request.field_name else ""
    if any(item.origin != query.origin for item in records):
        raise ComputationRefused("records came from a different read than this query")
    if len({(item.record_id, item.field_name) for item in records}) != len(records):
        raise ComputationRefused("the same record field was given twice")
    expected = (len(dataset.members) if isinstance(dataset, SelectionEvidence)
                else query.matched_count)
    whole = dataset.covers_whole_scope
    if records or request.field_name:
        whole = whole and len({item.record_id for item in records}) == expected
    scope = query.origin.scope
    if isinstance(dataset, SelectionEvidence):
        scope += "; " + dataset.condition
    origin = Origin(kind="computed", reference=request.describe(),
                    observed_at=observed_at, scope=scope)

    if request.operation == "read":
        if len(records) != 1:
            raise ComputationRefused(
                f"reading one field wants one record; it was given {len(records)}")
        only = records[0]
        if only.value is None:
            raise ComputationRefused("the field is known empty; cite its field evidence directly")
        return turn_record(turn, ComputationEvidence(
            evidence_id=evidence_id, origin=origin, operation=request.describe(),
            inputs=request.over, value=only.value if only.value is not None else "",
            value_type=only.value_type, unit=unit, covers_whole_scope=whole))

    if request.operation == "count":
        # A count is a claim about how many there are, and a count of a read that stopped
        # early is not that claim about anything. A sum may honestly be a subtotal -- the
        # renderer has a word for it -- but "239, of an unknown total" answers no question
        # the user asked, and labelling it "within what was read" made it look answered.
        #
        # Exactness is about the count, not the rows. An adapter that counted every matching
        # record and returned twenty of them knows the count exactly; one whose scan stopped
        # early does not, and asking the model to "read the whole scope" is no remedy when
        # the tool itself caps its rows -- that loop took a 12-second answer to 86 seconds.
        exact = whole or (isinstance(dataset, QueryEvidence) and not records
                          and dataset.count_exact)
        if not exact:
            raise ComputationRefused(
                f"the read of {scope} did not finish, so how many there are is not known; "
                "say what the read covered instead of giving a number")
        return turn_record(turn, ComputationEvidence(
            evidence_id=evidence_id, origin=origin, operation="count()",
            inputs=request.over,
            value=str(len({item.record_id for item in records}) if records else expected),
            value_type="count",
            covers_whole_scope=True))

    if request.operation == "filter":
        if not records and expected:
            raise ComputationRefused("the requested filter field was not returned")
        kept = tuple(item.record_id for item in records if _matches(item, request))
        return turn_record(turn, SelectionEvidence(
            evidence_id=evidence_id, origin=origin, operation="filter",
            inputs=request.over,
            condition=(dataset.condition + "; " + request.describe()
                       if isinstance(dataset, SelectionEvidence) else request.describe()),
            members=kept,
            covers_whole_scope=whole))

    kinds = {item.value_type for item in records}
    if records and kinds <= set(_ORDERED_TEXT):
        # Dates order and do not add: earliest, latest and the record holding either are
        # answerable; a total of them is not a quantity of anything.
        return _extreme_moment(turn, request, records, origin, dataset,
                               evidence_id=evidence_id, whole=whole)
    values = [_decimal(item.value or "", where=f"{item.record_id}.{item.field_name}")
              for item in records]
    if not values:
        if (request.operation == "sum" and expected == 0 and whole
                and request.field_name in query.summable_fields):
            # An application-certified additive field has an empty-set sum of zero.
            # No result row exists from which to infer a currency, so report an
            # unqualified numeric zero rather than inventing one from the account.
            return turn_record(turn, ComputationEvidence(
                evidence_id=evidence_id, origin=origin, operation=request.describe(),
                inputs=request.over, value="0", value_type="number", unit="",
                covers_whole_scope=True))
        raise ComputationRefused(
            f"{request.operation} over no records has no result; a count would")
    numeric = {item.value_type for item in records}
    if not numeric <= set(_NUMERIC):
        raise ComputationRefused(
            "these values are " + ", ".join(sorted(numeric)) + ", which do not add or order")
    if len(numeric) != 1:
        raise ComputationRefused("mixed numeric field types are not comparable")
    if request.operation in ("argmin", "argmax"):
        extreme = min(values) if request.operation == "argmin" else max(values)
        return turn_record(turn, SelectionEvidence(
            evidence_id=evidence_id, origin=origin, operation=request.operation,
            inputs=request.over,
            condition=(dataset.condition + "; " + request.describe()
                       if isinstance(dataset, SelectionEvidence) else request.describe()),
            members=tuple(item.record_id for item, value in zip(records, values)
                          if value == extreme), covers_whole_scope=whole))
    with localcontext() as context:
        context.prec = 512
        result = {"sum": lambda: sum(values, Decimal(0)), "min": lambda: min(values),
                  "max": lambda: max(values)}[request.operation]()
    # Keep the scale the inputs had: a money total that gained or lost a decimal place is a
    # different number, and rounding it here would hide which.
    scale = max((-item.as_tuple().exponent for item in values), default=0)
    return turn_record(turn, ComputationEvidence(
        evidence_id=evidence_id, origin=origin, operation=request.describe(),
        inputs=request.over, value=f"{result:.{scale}f}",
        value_type=records[0].value_type, unit=unit, covers_whole_scope=whole))


def _moment(value: str, *, where: str) -> str:
    """An ISO-8601 date or timestamp, refused when it is anything else."""
    text = value.strip()
    if not _ISO_MOMENT.fullmatch(text):
        raise ComputationRefused(
            f"{where} is not an ISO-8601 date or timestamp (2026-08-18 or "
            "2026-08-18 12:00:00)")
    return text.replace("T", " ")


def _extreme_moment(turn: TurnEvidence, request: ComputationRequest, records, origin,
                    dataset, *, evidence_id: str, whole: bool):
    if request.operation not in ("min", "max", "argmin", "argmax"):
        raise ComputationRefused(
            f"{request.field_name} holds dates, which order but do not add; ask for the "
            "earliest or the latest")
    moments = [_moment(item.value or "", where=f"{item.record_id}.{item.field_name}")
               for item in records]
    extreme = min(moments) if request.operation in ("min", "argmin") else max(moments)
    condition = (dataset.condition + "; " + request.describe()
                 if isinstance(dataset, SelectionEvidence) else request.describe())
    if request.operation in ("argmin", "argmax"):
        return turn_record(turn, SelectionEvidence(
            evidence_id=evidence_id, origin=origin, operation=request.operation,
            inputs=request.over, condition=condition,
            members=tuple(item.record_id for item, moment in zip(records, moments)
                          if moment == extreme),
            covers_whole_scope=whole))
    return turn_record(turn, ComputationEvidence(
        evidence_id=evidence_id, origin=origin, operation=request.describe(),
        inputs=request.over, value=extreme, value_type=records[0].value_type,
        covers_whole_scope=whole))


def _matches(item: RecordEvidence, request: ComputationRequest) -> bool:
    if item.value is None:
        raise ComputationRefused("an empty value cannot be compared to a nonempty operand")
    if item.value_type in _ORDERED_TEXT:
        if request.comparison == "in":
            raise ComputationRefused("in currently supports text and enum fields only")
        operand = _moment(request.threshold, where="the value compared against")
        # Compared at the operand's own precision: a date operand covers the whole day of a
        # timestamp value, which is what "orders on or before that date" means.
        value = _moment(item.value, where=item.field_name)[:len(operand)]
        return _compare_text(value, request.comparison, operand)
    if item.value_type in _NUMERIC:
        if request.comparison == "in":
            raise ComputationRefused("in currently supports text and enum fields only")
        return _compare(_decimal(item.value, where=item.field_name), request.comparison,
                        _decimal(request.threshold, where="the value compared against"))
    if item.value_type not in ("text", "enum") or request.comparison not in ("eq", "ne", "in"):
        raise ComputationRefused("text and enum fields support exact eq, ne or in only")
    if request.comparison == "in":
        return item.value in request.choices
    return (item.value == request.threshold if request.comparison == "eq"
            else item.value != request.threshold)


def _compare(left: Decimal, comparison: str, right: Decimal) -> bool:
    return {"gt": left > right, "ge": left >= right, "lt": left < right,
            "le": left <= right, "eq": left == right, "ne": left != right}[comparison]


def _compare_text(left: str, comparison: str, right: str) -> bool:
    return {"gt": left > right, "ge": left >= right, "lt": left < right,
            "le": left <= right, "eq": left == right, "ne": left != right}[comparison]


def turn_record(turn: TurnEvidence, item):
    turn.record(item)
    return item
