"""What the model asserts, each kind bound to the evidence that could establish it.

Checking that a claim's references exist is not the same as checking that the claim is true.
A tool returning "customer A: 100, customer B: 200" and a model writing "customer A: 200"
cites nothing that is missing; every name and every number appears. The relation is what is
wrong, and a relation is only checkable if the claim says which relation it is making.

So a conclusion has a kind, and a kind names what it must be bound to. A field value stands
on a read of that field of that record. A "not found" stands on the query that looked and the
scope it covered -- never on a returned value, because the identifier a reader asked about is
by definition absent from what a search for it returns. An undetermined stands on the gap
itself: a field never read, or a page that showed a control and no value.

The model submits these, rather than a system slicing them back out of prose afterwards.
Recovering structure from a sentence is the natural-language proof system this design was
adopted instead of, and it would arrive at the same place by a longer road.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .evidence import (
    ComputationEvidence, EvidenceUnknown, PageEvidence, QueryEvidence, RecordEvidence,
    SelectionEvidence, TurnEvidence,
)

#: How much of the question this answer covers. The model proposes one; the server may only
#: lower it. Every reference resolving says nothing about whether the question was answered,
#: and an answer that quietly promoted itself is the failure this ordering exists to stop.
COVERAGE = ("complete", "partial", "undetermined")
_ORDER = {name: index for index, name in enumerate(COVERAGE)}


class ConclusionRefused(ValueError):
    """The evidence cited cannot support the claim that was made, and why."""


@dataclass(frozen=True, slots=True)
class FieldValue:
    """This record's field holds this, or was read and holds nothing."""

    record_id: str
    field_name: str
    evidence_id: str

    kind = "field_value"

    def verify(self, turn: TurnEvidence) -> dict:
        item = _one(turn, self.evidence_id, RecordEvidence, "a field value")
        if not item.field_was_read:
            raise ConclusionRefused(
                f"{self.field_name} of {self.record_id} was never read, so its value is not "
                "a conclusion this turn can reach -- say it could not be determined")
        if item.record_id != self.record_id or item.field_name != self.field_name:
            raise ConclusionRefused(
                f"that read is of {item.field_name} of {item.record_id}, "
                f"not {self.field_name} of {self.record_id}")
        return {"kind": self.kind, "record_id": item.record_id,
                "field_name": item.field_name, "field_meaning": item.field_meaning,
                "value": item.value, "empty": item.is_known_empty,
                "unit": item.unit, "scope": item.origin.scope,
                "source_kind": item.origin.kind}


@dataclass(frozen=True, slots=True)
class Selection:
    """These records met this condition, and no others in what was looked at did."""

    evidence_id: str

    kind = "selection"

    def verify(self, turn: TurnEvidence) -> dict:
        item = _one(turn, self.evidence_id, SelectionEvidence, "a selection")
        return {"kind": self.kind, "condition": item.condition,
                "members": item.members, "matched": len(item.members),
                "covers_whole_scope": item.covers_whole_scope,
                "scope": item.origin.scope, "source_kind": "computed"}


@dataclass(frozen=True, slots=True)
class Aggregate:
    """A total, a count or an extreme, and what it is a total of."""

    evidence_id: str

    kind = "aggregate"

    def verify(self, turn: TurnEvidence) -> dict:
        item = _one(turn, self.evidence_id, ComputationEvidence, "an aggregate")
        return {"kind": self.kind, "operation": item.operation, "value": item.value,
                "value_type": item.value_type, "unit": item.unit,
                "covers_whole_scope": item.covers_whole_scope,
                "scope": item.origin.scope, "source_kind": "computed"}


@dataclass(frozen=True, slots=True)
class NotFound:
    """What was looked for, where, and that it was not there.

    Bound to the query rather than to any value. The previous design would have refused this
    conclusion for the only reason it exists: the thing a reader asked about is absent from
    the records a search for it returns.
    """

    looked_for: str
    evidence_id: str

    kind = "not_found"

    def verify(self, turn: TurnEvidence) -> dict:
        item = _one(turn, self.evidence_id, QueryEvidence, "a not-found")
        if not item.found_nothing:
            raise ConclusionRefused(
                f"that query matched {item.matched_count} records, so it does not establish "
                f"that {self.looked_for} is absent")
        return {"kind": self.kind, "looked_for": self.looked_for,
                "collection": item.collection, "conditions": item.conditions,
                "covers_whole_scope": item.covers_whole_scope,
                "scope": item.origin.scope, "source_kind": item.origin.kind}


@dataclass(frozen=True, slots=True)
class Undetermined:
    """Something the turn could not establish, bound to the gap that stopped it.

    A page that rendered a field's label and not its value is such a gap; so is a read that
    never asked for the field. Both were reported as the field being empty.
    """

    about: str
    evidence_id: str

    kind = "undetermined"

    def verify(self, turn: TurnEvidence) -> dict:
        item = _resolve(turn, self.evidence_id)
        if isinstance(item, RecordEvidence):
            if item.field_was_read:
                raise ConclusionRefused(
                    f"{item.field_name} of {item.record_id} was read; what it holds is "
                    "determined, so say what it holds")
            gap = f"{item.field_name} of {item.record_id} was not read"
        elif isinstance(item, PageEvidence):
            if item.saw != "control_only":
                raise ConclusionRefused(
                    "that observation carried a value, so it does not show a gap")
            gap = f"the page showed {item.detail} and no value"
        elif isinstance(item, QueryEvidence):
            if item.covers_whole_scope:
                raise ConclusionRefused(
                    "that query covered its whole scope, so it is not a gap")
            gap = f"the query covered only part of {item.origin.scope}"
        else:
            raise ConclusionRefused(
                "an undetermined must stand on a gap: a field not read, a page that showed "
                "no value, or a query that covered only part of its scope")
        return {"kind": self.kind, "about": self.about, "gap": gap,
                "scope": item.origin.scope, "source_kind": item.origin.kind}


Conclusion = Union[FieldValue, Selection, Aggregate, NotFound, Undetermined]

#: `action_succeeded` is deliberately absent. Whether a write happened already comes from the
#: mutation journal's terminal state and a same-actor readback, and routing it through a
#: conclusion the model composes would let the model re-adjudicate an outcome it does not
#: own. It stays where it is until there is a reason to move it that is not tidiness.
DEFERRED_KINDS = ("action_succeeded",)


@dataclass(frozen=True, slots=True)
class AnswerDraft:
    """What the model submits: conclusions, how much of the question they cover, and prose.

    The prose is free. It may explain, suggest a next step, or say what is missing, and it
    carries no verified standing: connectives go wrong even when every reference is right,
    and an `answer_grounded` stamp over a whole paragraph would claim otherwise. The
    conclusions are what the reader is being asked to rely on.
    """

    conclusions: tuple[Conclusion, ...]
    coverage: str
    prose: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.conclusions, tuple):
            raise TypeError("conclusions must be a tuple")
        if self.coverage not in COVERAGE:
            raise ConclusionRefused(f"coverage must be one of {COVERAGE}")
        if not isinstance(self.prose, str):
            raise TypeError("prose must be a string")


@dataclass(frozen=True, slots=True)
class VerifiedAnswer:
    """What survived checking, and how much of the question the server will let it claim."""

    conclusions: tuple[dict, ...]
    coverage: str
    prose: str
    lowered_because: tuple[str, ...] = ()

    @property
    def was_lowered(self) -> bool:
        return bool(self.lowered_because)


def verify(draft: AnswerDraft, turn: TurnEvidence) -> VerifiedAnswer:
    """Check every conclusion against this turn's evidence and settle the coverage.

    Coverage is proposed, not declared. The model may say an answer is complete; the server
    lowers it when the evidence says otherwise -- a subtotal, a partial query, an
    undetermined among the conclusions. It never raises it: whether an open question was
    really answered is not something this can decide, and pretending otherwise is how a
    table of five unrelated records came to be presented as an answer.
    """
    if not isinstance(draft, AnswerDraft):
        raise TypeError("verify takes an AnswerDraft")
    if not draft.conclusions:
        raise ConclusionRefused(
            "an answer with no conclusions asserts nothing the reader can rely on")
    verified = tuple(item.verify(turn) for item in draft.conclusions)

    lowered: list[str] = []
    coverage = draft.coverage
    for item in verified:
        if item["kind"] == "undetermined":
            coverage = _lower(coverage, "undetermined")
            lowered.append(f"{item['about']}: {item['gap']}")
        elif item["kind"] in ("aggregate", "selection") and not item["covers_whole_scope"]:
            coverage = _lower(coverage, "partial")
            lowered.append(
                f"{item.get('operation', item.get('condition'))} covered only part of "
                f"{item['scope']}")
        elif item["kind"] == "not_found" and not item["covers_whole_scope"]:
            coverage = _lower(coverage, "partial")
            lowered.append(
                f"the search for {item['looked_for']} covered only part of {item['scope']}")
    return VerifiedAnswer(conclusions=verified, coverage=coverage, prose=draft.prose,
                          lowered_because=tuple(lowered))


def _lower(current: str, floor: str) -> str:
    return current if _ORDER[current] >= _ORDER[floor] else floor


def _resolve(turn: TurnEvidence, evidence_id: str):
    try:
        return turn.get(evidence_id)
    except EvidenceUnknown as exc:
        raise ConclusionRefused(str(exc)) from None


def _one(turn: TurnEvidence, evidence_id: str, expected, what: str):
    item = _resolve(turn, evidence_id)
    if not isinstance(item, expected):
        raise ConclusionRefused(
            f"{what} stands on {expected.__name__}, but {evidence_id} is a "
            f"{type(item).__name__}")
    return item
