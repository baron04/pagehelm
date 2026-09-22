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

import re
from dataclasses import dataclass
from typing import Union

from .evidence import (
    CapabilityEvidence, ComputationEvidence, EvidenceUnknown, PageEvidence, QueryEvidence,
    RecordEvidence, SelectionEvidence, SourceEvidence, SourceHistoryEvidence, TurnEvidence,
)

#: How much of the question this answer covers. The model proposes one; the server may only
#: lower it. Every reference resolving says nothing about whether the question was answered,
#: and an answer that quietly promoted itself is the failure this ordering exists to stop.
COVERAGE = ("complete", "partial", "undetermined")
_ORDER = {name: index for index, name in enumerate(COVERAGE)}


class ConclusionRefused(ValueError):
    """The evidence cited cannot support the claim that was made, and why."""


def _answer_scope(turn: TurnEvidence, origin) -> str:
    """Keep executed query syntax in provenance instead of the normal answer surface."""
    for ref in turn.ids():
        item = turn.get(ref)
        if not isinstance(item, QueryEvidence) or item.origin != origin or not item.conditions:
            continue
        suffix = "; " + "; ".join(item.conditions)
        if origin.scope.endswith(suffix):
            return origin.scope[:-len(suffix)]
    return origin.scope


def _record_label(turn: TurnEvidence, item: RecordEvidence) -> str:
    """The record's own name from the same read, when that read fetched one.

    A field value was headed by its record id ("res.partner/13"), an identity the reader
    never sees in the application.
    """
    for ref in turn.ids():
        other = turn.get(ref)
        if (isinstance(other, RecordEvidence) and other.origin == item.origin
                and other.record_id == item.record_id and other.field_name == "name"
                and other.field_was_read and other.value):
            return other.value
    return ""


def _computed_scope(turn: TurnEvidence, scope: str) -> tuple[str, str]:
    """Split a computation's scope into the read it stands on and the filter applied to it.

    The scope is the read's own scope, its query conditions, then any filter a compute chain
    added. The first belongs in the answer; the query conditions are adapter syntax and stay
    in the answer basis; the filter is part of what a total is a total of.
    """
    reads = [item for ref in turn.ids()
             if isinstance(item := turn.get(ref), QueryEvidence)
             and (scope == item.origin.scope or scope.startswith(item.origin.scope + "; "))]
    if not reads:
        return scope, ""
    read = max(reads, key=lambda item: len(item.origin.scope))
    return _answer_scope(turn, read.origin), scope[len(read.origin.scope):].lstrip("; ")


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
                "record_label": _record_label(turn, item),
                "field_name": item.field_name, "field_meaning": item.field_meaning,
                "value": item.value, "empty": item.is_known_empty,
                "unit": item.unit, "scope": _answer_scope(turn, item.origin),
                "source_kind": item.origin.kind}


@dataclass(frozen=True, slots=True)
class Selection:
    """These records met this condition, and no others in what was looked at did."""

    evidence_id: str
    fields: tuple[str, ...] = ()

    kind = "selection"

    def verify(self, turn: TurnEvidence) -> dict:
        item = _resolve(turn, self.evidence_id)
        if not isinstance(item, (QueryEvidence, SelectionEvidence)):
            raise ConclusionRefused("a selection requires a query or computed selection")
        try:
            query = turn.query_for(self.evidence_id)
        except ValueError as exc:
            raise ConclusionRefused(str(exc)) from None
        records = [record for name in turn.ids()
                   if isinstance(record := turn.get(name), RecordEvidence)
                   and record.origin == query.origin]
        members = (item.members if isinstance(item, SelectionEvidence)
                   else tuple(dict.fromkeys(record.record_id for record in records)))
        condition = (item.condition if isinstance(item, SelectionEvidence)
                     else "; ".join(item.conditions) or item.collection)
        cells = {}
        for record in records:
            key = (record.record_id, record.field_name)
            if key in cells:
                raise ConclusionRefused("conflicting field evidence within one read")
            cells[key] = record
        rows = []
        for member in members:
            values = []
            for field_name in self.fields:
                record = cells.get((member, field_name))
                if record is None or not record.field_was_read:
                    raise ConclusionRefused(f"{member}.{field_name} was not read")
                values.append({"value": record.value, "unit": record.unit})
            rows.append(values)
        covers = item.covers_whole_scope and (
            isinstance(item, SelectionEvidence) or len(members) == query.matched_count)
        # How many records meet the condition, which is not how many are listed: a read
        # limited to 20 rows of 72 matches lists 20, and "20 match" was the answer's headline.
        # A computed selection counts what was read, so its total is its own size and is
        # exact only when what was read was everything.
        total, count_exact = ((len(members), covers) if isinstance(item, SelectionEvidence)
                              else (query.matched_count, query.count_exact or covers))
        return {"kind": self.kind, "condition": condition,
                "looked_for": query.looked_for, "exact_matches": query.exact_matches,
                "members": members, "matched": len(members), "fields": self.fields,
                "rows": rows, "total": total, "count_exact": count_exact,
                "covers_whole_scope": covers,
                "scope": _answer_scope(turn, query.origin),
                "source_kind": item.origin.kind}


@dataclass(frozen=True, slots=True)
class Aggregate:
    """A total, a count or an extreme, and what it is a total of."""

    evidence_id: str

    kind = "aggregate"

    def verify(self, turn: TurnEvidence) -> dict:
        item = _one(turn, self.evidence_id, ComputationEvidence, "an aggregate")
        scope, applied = _computed_scope(turn, item.origin.scope)
        return {"kind": self.kind, "operation": item.operation, "value": item.value,
                "value_type": item.value_type, "unit": item.unit,
                "covers_whole_scope": item.covers_whole_scope,
                "scope": scope, "filter": applied, "source_kind": "computed"}


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
            gap, gap_kind = f"{item.field_name} of {item.record_id} was not read", "field_not_read"
        elif isinstance(item, PageEvidence):
            if item.saw != "control_only":
                raise ConclusionRefused(
                    "that observation carried a value, so it does not show a gap")
            gap, gap_kind = f"the page showed {item.detail} and no value", "control_only"
        elif isinstance(item, QueryEvidence):
            if item.covers_whole_scope:
                raise ConclusionRefused(
                    "that query covered its whole scope, so it is not a gap")
            gap, gap_kind = f"the query covered only part of {item.origin.scope}", "partial_read"
        else:
            raise ConclusionRefused(
                "an undetermined must stand on a gap: a field not read, a page that showed "
                "no value, or a query that covered only part of its scope")
        return {"kind": self.kind, "about": self.about, "gap": gap, "gap_kind": gap_kind,
                "field_name": getattr(item, "field_name", ""),
                "detail": getattr(item, "detail", ""),
                "scope": item.origin.scope, "source_kind": item.origin.kind}


@dataclass(frozen=True, slots=True)
class PageFact:
    """Quote what this page observation contained, without assigning business meaning."""

    evidence_id: str
    kind = "page_observation"

    def verify(self, turn: TurnEvidence) -> dict:
        item = _one(turn, self.evidence_id, PageEvidence, "a page observation")
        if item.saw == "control_only" or item.value is None:
            raise ConclusionRefused("the page showed only a control; its value is undetermined")
        return {"kind": self.kind, "detail": item.detail, "value": item.value,
                "page_route": item.page_route, "scope": item.origin.scope,
                "source_kind": "page"}


@dataclass(frozen=True, slots=True)
class CapabilityGap:
    """Nothing in this session performs what was asked, bound to the search that establishes it.

    This is the one conclusion that is not about the application's data, and it is a
    conclusion rather than a bare flag for the same reason as the rest: the claim has to
    stand on something a reader can check. What it stands on is the capability search --
    the whole catalog for this actor, not the Skills a matcher pre-selected.

    It refuses a search that matched something. A turn holding a capability has to use it,
    and letting this conclusion through anyway would turn every hard task into "I can't",
    which is the projection problem wearing its last remaining hat.
    """

    evidence_id: str

    kind = "capability_gap"

    def verify(self, turn: TurnEvidence) -> dict:
        item = _one(turn, self.evidence_id, CapabilityEvidence, "a capability gap")
        if not item.found_nothing:
            raise ConclusionRefused(
                "that capability search matched " + ", ".join(item.matches)
                + ", so this session can perform it; use the capability instead of declining")
        return {"kind": self.kind, "goal": item.goal, "application": item.application,
                "application_version": item.application_version, "actor": item.actor,
                "catalog_digest": item.catalog_digest, "matches": list(item.matches),
                "scope": item.origin.scope, "source_kind": item.origin.kind}


@dataclass(frozen=True, slots=True)
class SourceExcerpt:
    """A model paraphrase linked to a checked span of exact source evidence."""

    evidence_id: str
    statement: str
    support: str
    kind = "source_excerpt"

    def verify(self, turn: TurnEvidence) -> dict:
        item = _one(turn, self.evidence_id, SourceEvidence, "a source excerpt")
        statement = _source_statement(
            self.statement, item.citation, item.sha256, source_excerpt=item.excerpt)
        support = _support_span(self.support, item.excerpt, item.summary)
        return {"kind": self.kind, "citation": item.citation, "release": item.release,
                "sha256": item.sha256, "excerpt": item.excerpt, "scope": item.origin.scope,
                "source_kind": "source", "fact_type": item.fact_type,
                "statement": statement, "support": support}


@dataclass(frozen=True, slots=True)
class SourceHistory:
    """A model paraphrase linked to checked deployment-bound Git metadata."""

    evidence_id: str
    statement: str
    support: str
    kind = "source_history"

    def verify(self, turn: TurnEvidence) -> dict:
        item = _one(turn, self.evidence_id, SourceHistoryEvidence, "source history")
        statement = _source_statement(self.statement, item.citation, item.sha256)
        support = _support_span(self.support, item.statement)
        return {"kind": self.kind, "citation": item.citation, "release": item.release,
                "deployed_commit": item.deployed_commit, "history_kind": item.history_kind,
                "history_statement": item.statement, "statement": statement,
                "support": support,
                "sha256": item.sha256,
                "scope": item.origin.scope, "source_kind": "source"}


def _support_span(value: str, *evidence_texts: str) -> str:
    """Require a literal span from this turn's cited evidence, kept off the user surface.

    This makes attribution inspectable. It does not establish that a model's paraphrase
    logically follows from the span; that remains an answer-quality question.
    """
    if not isinstance(value, str) or not 8 <= len(value) <= 400:
        raise ConclusionRefused("source support needs 8 to 400 characters")
    support = " ".join(value.split())
    if (len(support) < 8 or any(ord(char) < 32 or ord(char) == 127 for char in support)
            or not any(support in " ".join(text.split()) for text in evidence_texts)):
        raise ConclusionRefused("support is not present in the cited source evidence")
    return support


def _source_statement(value: str, citation: str, digest: str, *,
                      source_excerpt: str = "") -> str:
    """Keep code, source locations and digests out of user-facing conclusions."""
    if (not isinstance(value, str) or not value.strip() or len(value) > 800
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "`" in value):
        raise ConclusionRefused("source conclusion needs one bounded plain-text statement")
    statement = value.strip()
    copied_excerpt = " ".join(source_excerpt.split())
    if (citation in statement or digest in statement
            or re.search(r"\b[0-9a-fA-F]{64}\b", statement)
            or re.search(r"(?<!\w)[\w./-]+\.(?:py|pyi|js|jsx|ts|tsx|xml|json|ya?ml|sql|md)"
                         r"(?::[1-9]\d*(?:-[1-9]\d*)?)?\b", statement)
            or re.search(r"\b(?:if|elif|else|def|class|for|while)\s+.{0,80}:\s*"
                         r"(?:raise|return|pass)\b", statement)
            or (len(copied_excerpt) >= 16
                and copied_excerpt in " ".join(statement.split()))):
        raise ConclusionRefused("source code, locations and hashes are internal evidence")
    return statement


Conclusion = Union[FieldValue, Selection, Aggregate, NotFound, Undetermined, PageFact,
                   CapabilityGap, SourceExcerpt, SourceHistory]

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
    """What survived structural checks, not a proof of semantic entailment.

    A source paraphrase has a checked reference and literal support span. This cannot
    determine whether the model interpreted that span correctly.
    """

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
            floor = ("undetermined" if all(x["kind"] == "undetermined" for x in verified)
                     else "partial")
            coverage = _lower(coverage, floor)
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
