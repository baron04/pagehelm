"""What one turn established, with enough identity for an answer to cite it.

A query projection says what the last call returned. That is not the same as what the turn
established, and answering from the former is how a question about a record that does not
exist came to be answered with a table of five other records: the projection renders rows,
and rows are only an answer when the question was "show me the rows".

Evidence is the thing a conclusion cites. It comes in four kinds because a single required
shape does not fit them: a query that matched nothing has no record, an aggregate has no
record, and a page observation may not identify a business record at all. Demanding a stable
record identifier from every piece of evidence would make the two cases this exists to fix --
"S00042 is not in the open quotations" and "this read did not fetch the phone" -- impossible
to express.

Nothing here renders. Rendering an answer from evidence is a separate concern, and keeping
it separate is what stops the display projection from quietly becoming the evidence carrier
again.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Union

#: An evidence id is opaque to the reader and never leaves the server in a public answer.
#: It exists so a conclusion can name what it stands on, and so the same read can be cited
#: twice without being recorded twice.
_EVIDENCE_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*#[0-9]{1,6}")

#: Where a piece of evidence came from. `typed` is an application tool call, `page` is a
#: Runtime observation, `computed` is this server executing a bounded operation over other
#: evidence. The distinction is load-bearing: a page observation must never be described as
#: an API read, which is exactly what one failing answer did.
SOURCE_KINDS = ("typed", "page", "computed")
MAX_TURN_EVIDENCE_BYTES = 256 * 1024


def _text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    stripped = value.strip()
    if not stripped and not allow_empty:
        raise ValueError(f"{name} must not be empty")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", stripped):
        raise ValueError(f"{name} must not carry control characters")
    return stripped


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class Origin:
    """The three things every piece of evidence must be able to say.

    `scope` is what this particular act of looking covered -- the filter, the page, the
    permission boundary. It is not a completeness verdict: four different questions
    (did we see every record, every field, everything this user may see, and does the
    field mean what we think) were compressed into one `source_complete` boolean before,
    and the answers that went wrong went wrong in the seams between them.
    """

    kind: str
    reference: str
    observed_at: str
    scope: str

    def __post_init__(self) -> None:
        if self.kind not in SOURCE_KINDS:
            raise ValueError(f"evidence source kind must be one of {SOURCE_KINDS}")
        object.__setattr__(self, "reference", _text(self.reference, "evidence reference"))
        object.__setattr__(self, "observed_at", _text(self.observed_at, "observed_at"))
        object.__setattr__(self, "scope", _text(self.scope, "scope"))


@dataclass(frozen=True, slots=True)
class RecordEvidence:
    """One field of one record, and whether this read actually fetched it.

    `field_was_read=False` is not a value; it is the absence of one, and it must stay
    distinguishable from a field that was read and found empty. An answer once said a
    customer's phone and mobile "are both empty" when the read had never asked for either
    and the page showed only their labels. The phone was `(603)-996-3829`.
    """

    evidence_id: str
    origin: Origin
    record_id: str
    field_name: str
    field_meaning: str
    field_was_read: bool
    value: str | None = None
    value_type: str = ""
    unit: str = ""

    def __post_init__(self) -> None:
        _require_id(self.evidence_id)
        _require_origin(self.origin)
        object.__setattr__(self, "record_id", _text(self.record_id, "record_id"))
        object.__setattr__(self, "field_name", _text(self.field_name, "field_name"))
        object.__setattr__(self, "field_meaning", _text(self.field_meaning, "field_meaning"))
        if not isinstance(self.field_was_read, bool):
            raise TypeError("field_was_read must be a boolean")
        if not self.field_was_read:
            if self.value is not None:
                raise ValueError("a field that was not read cannot carry a value")
            if self.value_type or self.unit:
                raise ValueError("a field that was not read has no type or unit")
            return
        if self.value is not None and not isinstance(self.value, str):
            raise TypeError("value must be a string or None")
        object.__setattr__(self, "value_type", _text(self.value_type, "value_type"))
        # A number without its unit is how a tax-excluded column came to be reported as the
        # amount owed. The unit travels with the value or the value is not business data.
        if self.value_type in ("money", "quantity") and not self.unit.strip():
            raise ValueError(f"a {self.value_type} value must carry its unit or currency")
        object.__setattr__(self, "unit", self.unit.strip())

    @property
    def is_known_empty(self) -> bool:
        """Read, and found to hold nothing. Distinct from never having been read."""
        return self.field_was_read and self.value is None


@dataclass(frozen=True, slots=True)
class QueryEvidence:
    """What was asked of the application, and how much of it the answer covers.

    A query that matched nothing is evidence, and it is the only evidence a "not found"
    conclusion can stand on: the identifier a reader asked about is, by definition, absent
    from the records a search returns. A design that demanded every cited value appear in
    the results would have rejected the one correct answer this exists to allow.
    """

    evidence_id: str
    origin: Origin
    collection: str
    conditions: tuple[str, ...]
    matched_count: int
    records_scanned: int | None = None
    covers_whole_scope: bool = False

    def __post_init__(self) -> None:
        _require_id(self.evidence_id)
        _require_origin(self.origin)
        object.__setattr__(self, "collection", _text(self.collection, "collection"))
        if not isinstance(self.conditions, tuple):
            raise TypeError("conditions must be a tuple")
        object.__setattr__(self, "conditions", tuple(
            _text(item, "condition") for item in self.conditions))
        object.__setattr__(self, "matched_count", _count(self.matched_count, "matched_count"))
        if self.records_scanned is not None:
            object.__setattr__(self, "records_scanned",
                               _count(self.records_scanned, "records_scanned"))
        if not isinstance(self.covers_whole_scope, bool):
            raise TypeError("covers_whole_scope must be a boolean")

    @property
    def found_nothing(self) -> bool:
        return self.matched_count == 0


@dataclass(frozen=True, slots=True)
class PageEvidence:
    """What a page showed, and whether it showed a value or only a place for one.

    A form renders the label "Phone" whether or not the field holds anything, and the text
    a Runtime observation carries does not include input values. Seeing the label is
    evidence that the control exists. It is not evidence that the field is empty, and the
    difference is the whole reason this kind is separate.
    """

    evidence_id: str
    origin: Origin
    page_route: str
    saw: str
    detail: str
    value: str | None = None

    #: What the observation actually contained.
    SAW_KINDS = ("value", "control_only", "text")

    def __post_init__(self) -> None:
        _require_id(self.evidence_id)
        _require_origin(self.origin)
        if self.origin.kind != "page":
            raise ValueError("page evidence must come from a page observation")
        object.__setattr__(self, "page_route", _text(self.page_route, "page_route"))
        if self.saw not in self.SAW_KINDS:
            raise ValueError(f"saw must be one of {self.SAW_KINDS}")
        object.__setattr__(self, "detail", _text(self.detail, "detail"))
        if self.saw == "control_only" and self.value is not None:
            raise ValueError("a control with no readable value cannot carry one")
        if self.saw == "value" and self.value is None:
            raise ValueError("page evidence that saw a value must carry it")


@dataclass(frozen=True, slots=True)
class ComputationEvidence:
    """A bounded operation this server ran over other evidence, and what it produced.

    The model chooses the operation; the server runs it on the real values. That split is
    what a natural-language check could not give: "the total is 7,210.00" is a claim about
    arithmetic over five records, and no amount of inspecting the sentence establishes it.
    """

    evidence_id: str
    origin: Origin
    operation: str
    inputs: tuple[str, ...]
    value: str
    value_type: str
    unit: str = ""
    #: Whether the inputs covered the whole scope the question asked about. A sum over the
    #: fifty records one page returned is a subtotal; calling it the total is the single
    #: most expensive thing a correct-looking number can do, so the result carries the
    #: answer rather than leaving it to whoever renders it.
    covers_whole_scope: bool = False

    def __post_init__(self) -> None:
        _require_id(self.evidence_id)
        _require_origin(self.origin)
        if self.origin.kind != "computed":
            raise ValueError("computation evidence must be recorded as computed")
        if not isinstance(self.covers_whole_scope, bool):
            raise TypeError("covers_whole_scope must be a boolean")
        object.__setattr__(self, "operation", _text(self.operation, "operation"))
        if not isinstance(self.inputs, tuple) or not self.inputs:
            raise ValueError("a computation must name the evidence it ran on")
        for item in self.inputs:
            _require_id(item)
        object.__setattr__(self, "value", _text(self.value, "computed value"))
        object.__setattr__(self, "value_type", _text(self.value_type, "value_type"))
        if self.value_type in ("money", "quantity") and not self.unit.strip():
            raise ValueError(f"a {self.value_type} result must carry its unit or currency")
        object.__setattr__(self, "unit", self.unit.strip())



@dataclass(frozen=True, slots=True)
class SelectionEvidence:
    """The records a condition kept, and the condition it applied.

    A filter's result is a set, not a number, and the difference matters: "which quotations
    are over 2000" was answered with all five because the answer carried rows rather than a
    selection. What a reader needs is the condition that ran and the members it left.
    """

    evidence_id: str
    origin: Origin
    operation: str
    inputs: tuple[str, ...]
    condition: str
    members: tuple[str, ...]
    covers_whole_scope: bool = False

    def __post_init__(self) -> None:
        _require_id(self.evidence_id)
        _require_origin(self.origin)
        if self.origin.kind != "computed":
            raise ValueError("a selection is computed, not observed")
        object.__setattr__(self, "operation", _text(self.operation, "operation"))
        if not isinstance(self.inputs, tuple) or not self.inputs:
            raise ValueError("a selection must name the evidence it ran on")
        for item in self.inputs:
            _require_id(item)
        object.__setattr__(self, "condition", _text(self.condition, "condition"))
        if not isinstance(self.members, tuple):
            raise TypeError("members must be a tuple")
        # An empty selection is a result, not a failure: "none of them are over 2000" is an
        # answer, and it is a different answer from "I could not check".
        object.__setattr__(self, "members", tuple(
            _text(item, "member record id") for item in self.members))
        if len(set(self.members)) != len(self.members):
            raise ValueError("a selection cannot hold the same record twice")
        if not isinstance(self.covers_whole_scope, bool):
            raise TypeError("covers_whole_scope must be a boolean")


Evidence = Union[RecordEvidence, QueryEvidence, PageEvidence,
                 ComputationEvidence, SelectionEvidence]


def _require_id(value: object) -> None:
    if not isinstance(value, str) or not _EVIDENCE_ID.fullmatch(value):
        raise ValueError("an evidence id must read <kind>#<number>")


def _require_origin(value: object) -> None:
    if not isinstance(value, Origin):
        raise TypeError("evidence must carry an Origin")


class EvidenceUnknown(KeyError):
    """A conclusion named evidence this turn never recorded."""


@dataclass
class TurnEvidence:
    """Everything one turn established, in the order it established it.

    Held by the turn rather than by the business tool runtime or the Runtime channel: a
    page observation and a typed read are evidence of the same kind of thing for an answer,
    and they arrive through different doors with different trust roots. Hanging one on the
    other's result type would put a Runtime observation inside a business tool's record.
    """

    _items: dict[str, Evidence] = field(default_factory=dict)

    def capture(self, items: tuple[Evidence, ...], *, namespace: str) -> tuple[str, ...]:
        """Atomically import call-local evidence under host-owned identities.

        Adapters may start their IDs at one on every call. The call namespace belongs to
        the session, so a second read cannot collide with, or borrow authority from, the
        first. A malformed batch never leaves behind a query claiming unrecorded members.
        """
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", namespace):
            raise ValueError("invalid evidence namespace")
        if not isinstance(items, tuple):
            raise TypeError("an evidence batch must be a tuple")
        if any(not isinstance(item, (RecordEvidence, QueryEvidence, PageEvidence,
                                     ComputationEvidence, SelectionEvidence)) for item in items):
            raise TypeError("an evidence batch may only contain evidence")
        local_ids = [item.evidence_id for item in items]
        if len(set(local_ids)) != len(local_ids):
            raise ValueError("an evidence batch contains duplicate IDs")
        mapping = {name: f"{namespace}-{name}" for name in local_ids}
        staged = TurnEvidence(dict(self._items))
        for item in items:
            changes = {"evidence_id": mapping[item.evidence_id],
                       "origin": replace(item.origin,
                                         reference=f"{namespace}:{item.origin.reference}")}
            if isinstance(item, (ComputationEvidence, SelectionEvidence)):
                if any(name not in mapping for name in item.inputs):
                    raise EvidenceUnknown("imported derived evidence must use its own batch")
                changes["inputs"] = tuple(mapping[name] for name in item.inputs)
            staged.record(replace(item, **changes))
        encoded = json.dumps([asdict(item) for item in staged._items.values()],
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_TURN_EVIDENCE_BYTES:
            raise ValueError("turn evidence byte limit exceeded; narrow the query")
        self._items = staged._items
        return tuple(mapping.values())

    def record(self, item: Evidence) -> str:
        if not isinstance(item, (RecordEvidence, QueryEvidence, PageEvidence,
                                 ComputationEvidence, SelectionEvidence)):
            raise TypeError("only evidence can be recorded")
        if item.evidence_id in self._items:
            raise ValueError(f"evidence {item.evidence_id} was already recorded this turn")
        if isinstance(item, (ComputationEvidence, SelectionEvidence)):
            # Checked here rather than in the dataclass: an input is only meaningful against
            # a turn, and a computation whose inputs came from somewhere else is not
            # recomputable by whoever reads the answer.
            missing = [name for name in item.inputs if name not in self._items]
            if missing:
                raise EvidenceUnknown(
                    f"{item.evidence_id} names evidence this turn does not have: "
                    + ", ".join(sorted(missing)))
        self._items[item.evidence_id] = item
        return item.evidence_id

    def get(self, evidence_id: str) -> Evidence:
        try:
            return self._items[evidence_id]
        except KeyError:
            raise EvidenceUnknown(f"this turn recorded no evidence {evidence_id!r}") from None

    def require_all(self, ids: tuple[str, ...]) -> tuple[Evidence, ...]:
        """Resolve every id or name all the ones that are missing, not just the first."""
        missing = [name for name in ids if name not in self._items]
        if missing:
            raise EvidenceUnknown(
                "this turn recorded no evidence " + ", ".join(repr(x) for x in sorted(missing)))
        return tuple(self._items[name] for name in ids)

    def query_for(self, evidence_id: str) -> QueryEvidence:
        """Find the one read behind a query or a chain of selections."""
        item = self.get(evidence_id)
        seen = set()
        while isinstance(item, SelectionEvidence):
            if item.evidence_id in seen or len(seen) >= 32:
                raise ValueError("selection lineage is cyclic or too deep")
            seen.add(item.evidence_id)
            parents = [parent for parent in self.require_all(item.inputs)
                       if isinstance(parent, (QueryEvidence, SelectionEvidence))]
            if len(parents) != 1:
                raise ValueError("selection must descend from exactly one query")
            item = parents[0]
        if not isinstance(item, QueryEvidence):
            raise ValueError("evidence has no query root")
        return item

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._items

    def ids(self) -> tuple[str, ...]:
        return tuple(self._items)
