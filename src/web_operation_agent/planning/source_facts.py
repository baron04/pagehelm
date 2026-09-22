"""Ranked, cited answers to questions about an application's own rules.

The older :mod:`retrieval` path is controller-shaped: it answers which route, form and
screen handle something.  Field meanings, refusal conditions and action scope are relations
too.  This module builds a deterministic local BM25 index over those rule relations.  It has
no model call, embeddings, aliases or application-specific query vocabulary.
"""
from __future__ import annotations

import math
import re
import weakref
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..schemas import EvidenceRef, SourceMap, SourceRelation


FACT_KINDS = ("service_guard", "service_entity", "service_parameter", "request_handler",
              "form_target")
_TEXT_ATTRS = {
    "service_guard": ("condition", "loop", "exception", "decorators"),
    "service_entity": ("string", "help", "selection", "compute", "related", "domain",
                       "field_type"),
    "service_parameter": ("doc", "decorators"),
    "request_handler": ("name", "domain", "help", "view_mode"),
    "form_target": ("label", "invisible", "confirm", "root_view"),
}
_STOPWORDS = frozenset("""
a an the and or but of to for from with without on in into at by as is are was were be been
this that these those it its their they them we our you your he she his her him i me my
must should can could may might will would shall do does did done have has had not no
what which who whom when where why how there here then than so such all any some each
""".split())
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*|[\u3400-\u9fff]+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_MAX_LINE_CHARS = 400
_K1, _B = 1.2, 0.75
SOURCE_FACT_TYPES = frozenset({"static_declaration", "possible_cause", "implementation"})


def _stem(word: str) -> str:
    if word.endswith(("ss", "us", "is")):
        return word
    for suffix, replacement in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""),
                                ("s", "")):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + replacement
    return word


def terms(text: str) -> list[str]:
    """Split code identifiers and overlapping Han pairs without a language model.

    The pairs make labels and comments searchable in their own language. They do not
    translate a Chinese question into English code identifiers.
    """
    out = []
    for word in _WORD.findall(text or ""):
        if "\u3400" <= word[0] <= "\u9fff":
            out.extend(word[i:i + 2] for i in range(len(word) - 1))
            if len(word) == 1:
                out.append(word)
            continue
        for part in _CAMEL.findall(word):
            lowered = part.lower()
            if len(lowered) >= 2 and lowered not in _STOPWORDS:
                out.append(_stem(lowered))
    return out


@dataclass(frozen=True)
class SourceFact:
    relation: SourceRelation
    score: float

    @property
    def evidence(self) -> EvidenceRef:
        return self.relation.evidence[0]


def _document(relation: SourceRelation) -> list[str]:
    names = " ".join((relation.subject, relation.object))
    said = " ".join(str(relation.attrs.get(name) or "")
                    for name in _TEXT_ATTRS.get(relation.kind, ()))
    excerpt = " ".join(reference.excerpt for reference in relation.evidence)
    return terms(names) * 2 + terms(said) + terms(excerpt)


class SourceFactIndex:
    """A BM25 index over one map's rule-stating relations, built once per map."""

    def __init__(self, source_map: SourceMap):
        if not isinstance(source_map, SourceMap):
            raise TypeError("source_map must be a SourceMap")
        self._relations = [relation for relation in source_map.relations
                           if relation.kind in FACT_KINDS]
        self._documents = [Counter(_document(relation)) for relation in self._relations]
        self._lengths = [sum(document.values()) for document in self._documents]
        self._average = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        frequency: Counter = Counter()
        for document in self._documents:
            frequency.update(document.keys())
        count = len(self._documents)
        self._idf = {term: math.log(1 + (count - seen + 0.5) / (seen + 0.5))
                     for term, seen in frequency.items()}

    def __len__(self) -> int:
        return len(self._relations)

    def weights(self, query: str) -> dict[str, float]:
        return {term: self._idf.get(term, 0.0) for term in set(terms(query))}

    def search(self, query: str, *, limit: int = 8) -> tuple[SourceFact, ...]:
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer from 1 through 20")
        wanted = set(terms(query))
        if not wanted or not self._relations:
            return ()
        scored = []
        for index, document in enumerate(self._documents):
            score = 0.0
            for term in wanted & document.keys():
                frequency = document[term]
                norm = _K1 * (1 - _B + _B * self._lengths[index] / (self._average or 1))
                score += self._idf[term] * frequency * (_K1 + 1) / (frequency + norm)
            if score > 0:
                relation = self._relations[index]
                scored.append((-score, relation.kind, relation.subject, relation.object,
                               index))
        scored.sort()
        return tuple(SourceFact(self._relations[index], -negative)
                     for negative, _, _, _, index in scored[:limit])


def fact_text(fact: SourceFact, *, route: Callable[[str], str] | None = None) -> str:
    """Render what one relation says without inventing semantics outside it."""
    relation = fact.relation
    attrs = relation.attrs
    if relation.kind == "service_guard":
        text = f"rule {relation.subject}: refuses"
        if attrs.get("exception"):
            text += f" ({attrs['exception']})"
        text += f" {relation.object!r}"
        if attrs.get("condition"):
            text += f" when {attrs['condition']}"
        if attrs.get("loop"):
            text += f" (for {attrs['loop']})"
    elif relation.kind == "service_entity":
        text = f"field {relation.subject}.{relation.object}"
        if attrs.get("field_type"):
            text += f" ({attrs['field_type']})"
        details = [f"{name}={attrs[name]}" for name in
                   ("string", "selection", "help", "compute", "related", "domain", "default")
                   if attrs.get(name)]
        if details:
            text += ": " + "; ".join(details)
    elif relation.kind == "service_parameter":
        text = f"method {relation.subject}({relation.object})"
        if attrs.get("decorators"):
            text += " " + " ".join(f"@{item}" for item in attrs["decorators"])
        doc = " ".join(str(attrs.get("doc") or "").split())
        if doc:
            text += f": {doc}"
        else:
            body = (fact.evidence.excerpt.split("\n", 1)[1]
                    if "\n" in fact.evidence.excerpt else "")
            body = " ".join(body.split())
            text += f": {body}" if body else ""
    elif relation.kind == "request_handler":
        path = route(relation.subject) if route is not None else relation.subject
        text = f"screen {str(attrs.get('name') or relation.subject)!r} at {path}"
        if relation.object:
            text += f" shows {relation.object}"
        if attrs.get("domain"):
            text += f" filtered by {attrs['domain']}"
        if attrs.get("event_type") and attrs["event_type"] != "view-only":
            text += f" ({attrs['event_type']}, not a read-only page)"
        if attrs.get("help"):
            text += f": {attrs['help']}"
    else:
        text = f"button {str(attrs.get('label') or relation.object)!r} calls {relation.object}"
        if attrs.get("invisible"):
            text += f", hidden when {attrs['invisible']}"
        if attrs.get("confirm"):
            text += f", asks {attrs['confirm']!r}"
    return text


def _fit(text: str, weights: dict[str, float], evidence: EvidenceRef) -> str:
    """Keep the head and the query-relevant window of an overlong fact."""
    where = len(f"[{evidence.path}:{evidence.start_line}-{evidence.end_line}] ")
    if where + len(text) <= _MAX_LINE_CHARS:
        return text
    head, separator, body = text.partition(": ")
    if not separator:
        head, body = "", text
    budget = _MAX_LINE_CHARS - where - len(head) - len(separator) - 2
    if budget < 80:
        return text
    words = list(re.finditer(r"\S+", body))
    found = {index: set(terms(word.group())) & weights.keys()
             for index, word in enumerate(words)}
    hits = [index for index, matched in found.items() if matched]
    start = 0
    if hits:
        rarest = max((term for index in hits for term in found[index]),
                     key=lambda term: (weights[term], term))
        anchor = next(index for index in hits if rarest in found[index])
        start = words[max(0, anchor - 3)].start()
    window = body[start:start + budget]
    if start + budget < len(body):
        window = window[:window.rfind(" ")] if " " in window else window
        window += "…"
    if start > 0:
        window = "…" + window
    return f"{head}{separator}{window}"


@dataclass(frozen=True)
class SourceHit:
    """One cited source result and the bounded explanation shown with it."""

    evidence: EvidenceRef
    text: str
    verbatim: bool = False
    max_chars: int = _MAX_LINE_CHARS
    fact_type: str = "static_declaration"

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, EvidenceRef):
            raise TypeError("evidence must be an EvidenceRef")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("a source hit says something")
        if not isinstance(self.verbatim, bool):
            raise TypeError("verbatim must be a boolean")
        if (isinstance(self.max_chars, bool) or not isinstance(self.max_chars, int)
                or not 100 <= self.max_chars <= 4000):
            raise ValueError("max_chars must be from 100 to 4000")
        if self.fact_type not in SOURCE_FACT_TYPES:
            raise ValueError(f"fact_type must be one of {sorted(SOURCE_FACT_TYPES)}")

    @property
    def line(self) -> str:
        reference = self.evidence
        where = f"[{reference.path}:{reference.start_line}-{reference.end_line}]"
        if self.verbatim:
            line = f"{where}\n{self.text.rstrip()}"
        else:
            line = f"{where} {' '.join(self.text.split())}"
        return line if len(line) <= self.max_chars else line[:self.max_chars - 1] + "…"


def render_fact(fact: SourceFact, *, route: Callable[[str], str] | None = None) -> str:
    return SourceHit(evidence=fact.evidence, text=fact_text(fact, route=route)).line


@runtime_checkable
class SourceLookup(Protocol):
    """A local lookup over one exact release, returning only cited source bytes."""

    release: str

    def search(self, query: str, *, limit: int = 8,
               route: Callable[[str], str] | None = None) -> tuple[SourceHit, ...]: ...


class CompiledSourceLookup:
    def __init__(self, source_map: SourceMap):
        if not isinstance(source_map, SourceMap):
            raise TypeError("source_map must be a SourceMap")
        self.release = source_map.release
        self._source_map = source_map

    def search(self, query: str, *, limit: int = 8,
               route: Callable[[str], str] | None = None) -> tuple[SourceHit, ...]:
        index = fact_index(self._source_map)
        facts = index.search(query, limit=limit)
        weights = index.weights(query)
        return tuple(SourceHit(evidence=fact.evidence,
                               text=_fit(fact_text(fact, route=route), weights, fact.evidence),
                               fact_type=("possible_cause"
                                          if fact.relation.kind == "service_guard"
                                          else "static_declaration"))
                     for fact in facts)


_INDEXES: dict[int, tuple[weakref.ref, SourceFactIndex]] = {}


def fact_index(source_map: SourceMap) -> SourceFactIndex:
    """Build this immutable map's fact index once and reuse it across sessions."""
    key = id(source_map)
    held = _INDEXES.get(key)
    if held is not None and held[0]() is source_map:
        return held[1]
    index = SourceFactIndex(source_map)
    _INDEXES[key] = (weakref.ref(source_map, lambda _ref, key=key: _INDEXES.pop(key, None)),
                     index)
    return index
