"""Narrowing a request to the few entities it is about, before any declaration.

Two things happen here, in this order. First, the hard references in the request --
document numbers, codes, names written in Latin script, amounts and dates -- are cut out
by their shape, which no language changes, and the adapter is asked which records they
name; a reference that names a record settles its entity without any model. Second, the
index is rendered as one line per entity, ranked so that the entities of known references
come first, then entities whose label or aliases the request mentions, then the rest; the
model picks entities and deliverable kinds from that, and words the request shares with
a label only order the list, never cut it.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .index import ApplicationIndex, IndexEntity

#: A document number or code: letters and digits with separators, at least one digit,
#: as applications number their records (S00006, INV/2026/00002, E-COM06, PO-2025-0617).
_CODE = re.compile(r"(?<![\w/])[A-Za-z]{1,8}[-/_]?\d[\w/.-]*(?![\w/])")
#: A name in Latin script: capitalised words, possibly several ("Azure Interior") or
#: run together ("OpenWood").
_NAME = re.compile(r"(?<![\w])(?:[A-Z][a-z][a-zA-Z0-9]*(?:[ '.-][A-Z][a-zA-Z0-9]+)*)(?![\w])")
#: An amount or a plain number with at least two digits, optionally with decimals.
_AMOUNT = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])\d{2,}(?:\.\d+)?(?![\w])")
#: A calendar date written with digits.
_DATE = re.compile(r"\d{4}-\d{2}(?:-\d{2})?")
_MAX_REFERENCES = 8


@dataclass(frozen=True)
class KnownReference:
    """A hard reference in the request and the record the adapter found it to name."""
    text: str
    entity: str
    name: str


def references(text: str) -> tuple[str, ...]:
    """The hard references in a request, by shape, longest first and without repeats.

    Codes come before names so that "INV/2026/00002" is one reference and not a name
    plus a number; a name inside a code is not reported again.
    """
    found: list[str] = []
    taken: list[tuple[int, int]] = []

    def take(match):
        span = match.span()
        if any(a <= span[0] < b or a < span[1] <= b for a, b in taken):
            return
        value = match.group(0).strip(" .,-")
        if value and value not in found:
            found.append(value)
            taken.append(span)

    for pattern in (_CODE, _DATE, _AMOUNT, _NAME):
        for match in pattern.finditer(text or ""):
            take(match)
    return tuple(found[:_MAX_REFERENCES])


def locate(port, text: str, *, context=None) -> tuple[KnownReference, ...]:
    """Ask the adapter which records the request's hard references name.

    An adapter that cannot locate references (no `locate_reference` method) contributes
    nothing; narrowing then rests on the request's words and the model's choice.
    """
    method = getattr(port, "locate_reference", None)
    if not callable(method):
        return ()
    known: list[KnownReference] = []
    for ref in references(text):
        for hit in method(ref, context=context) or ():
            entity, name = str(hit[0]), str(hit[1])
            if entity and name:
                known.append(KnownReference(text=ref, entity=entity, name=name))
    return tuple(known)


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-z㐀-鿿]+", (text or "").casefold()) if t}


def _mentions(entity: IndexEntity, request: str) -> bool:
    request_norm = "".join(request.casefold().split())
    words = _tokens(request)
    for label in (entity.label, *entity.aliases):
        norm = "".join(label.casefold().split())
        if norm and (norm in request_norm or _tokens(label) <= words):
            return True
    return False


def ranked_entities(index: ApplicationIndex, request: str,
                    known: tuple[KnownReference, ...]) -> tuple[IndexEntity, ...]:
    """Every entity of the index, the likely ones first: those a known reference
    names, then those whose label or an alias the request mentions, then the rest in
    the index's order. Nothing is dropped."""
    named = {k.entity for k in known}
    first = [e for e in index.entities if e.key in named]
    second = [e for e in index.entities if e.key not in named and _mentions(e, request)]
    rest = [e for e in index.entities if e.key not in named and e not in second]
    return tuple(first + second + rest)


def summaries(index: ApplicationIndex, request: str,
              known: tuple[KnownReference, ...]) -> str:
    """One line per entity, ranked, with the pages that show it; what the first step
    of a staged declaration reads instead of the full index."""
    pages_of: dict[str, list[str]] = {}
    for page in index.pages:
        pages_of.setdefault(page.entity, []).append(f"{page.key} {page.breadcrumb}")
    lines = ["ENTITIES (key: label [aliases] | pages)"]
    named = {k.entity: k for k in known}
    for entity in ranked_entities(index, request, known):
        parts = [f"{entity.key}: {entity.label}"
                 + (f" [{', '.join(entity.aliases)}]" if entity.aliases else "")
                 + (f" (lines of {entity.lines_of})" if entity.lines_of else "")]
        if entity.key in named:
            parts.append(f"named in request: {named[entity.key].name}")
        pages = pages_of.get(entity.key, [])
        if pages:
            parts.append("pages: " + "; ".join(pages[:6]))
        lines.append(" | ".join(parts))
    if known:
        lines.append("")
        lines.append("KNOWN REFERENCES (text -> entity: record)")
        for k in known:
            lines.append(f"{k.text} -> {k.entity}: {k.name}")
    return "\n".join(lines)


def neighbourhood(index: ApplicationIndex, keys: tuple[str, ...]) -> ApplicationIndex:
    """The chosen entities with what a declaration about them may need to name: the
    entities their link fields point to, their lines, the records that own them, and
    every page that shows any of these. Rendered like the full index, only smaller."""
    chosen = {k for k in keys if index.entity(k) is not None}
    wanted = set(chosen)
    by_label = {e.label: e.key for e in index.entities}
    for key in chosen:
        entity = index.entity(key)
        for f in entity.fields:
            if f.kind == "link" and f.relation and index.entity(f.relation) is not None:
                wanted.add(f.relation)
        if entity.lines_of and entity.lines_of in by_label:
            wanted.add(by_label[entity.lines_of])
        for other in index.entities:
            if other.lines_of == entity.label:
                wanted.add(other.key)
    entities = tuple(e for e in index.entities if e.key in wanted)
    pages = tuple(p for p in index.pages if p.entity in wanted)
    return ApplicationIndex(entities=entities, pages=pages, source_digest=index.source_digest,
                            complete=index.complete)


__all__ = ["KnownReference", "locate", "neighbourhood", "ranked_entities", "references",
           "summaries"]
