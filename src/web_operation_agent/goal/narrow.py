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

from dataclasses import dataclass, replace
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


def correct_records(spec, request: str, known: tuple[KnownReference, ...],
                    offered: tuple[str, ...] = ()):
    """A declared record written like a code that is not in the request, where the
    request's located references leave exactly one record it can be, is that record:
    record numbers are copied, not composed, and a small model now and then composes
    one (S00096 for S00009). A code copied from the values offered this turn was not
    composed and stands. Returns (spec, corrections as (declared, located))."""
    from dataclasses import replace as _replace
    if not known:
        return spec, ()
    asked = {_norm_ref(r) for r in references(request)} | {_norm_ref(r) for r in offered}
    corrections = []
    out = []
    for d in spec.deliverables:
        record = (d.record or "").strip()
        if record and _CODE.fullmatch(record) and _norm_ref(record) not in asked:
            # A code stands for a code: a located customer's name never replaces one.
            same = ({k.name for k in known if k.entity == d.entity}
                    or {k.name for k in known if _CODE.fullmatch(k.name.strip())})
            if len(same) == 1:
                located = next(iter(same))
                corrections.append((record, located))
                d = _replace(d, record=located)
        out.append(d)
    if not corrections:
        return spec, ()
    return _replace(spec, deliverables=tuple(out)), tuple(corrections)


def _norm_ref(text: str) -> str:
    return "".join((text or "").casefold().split())


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
    for label in (entity.label, *entity.aliases, *entity.actions):
        norm = "".join(label.casefold().split())
        if norm and (norm in request_norm or (_tokens(label) and _tokens(label) <= words)):
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
        if entity.actions:
            parts.append("controls: " + ", ".join(entity.actions[:8]))
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


#: Rules catalogued per chosen entity: the ones the request's words touch first, then
#: every kind represented, then the rest until this many. A catalogue line is short
#: (id and what the rule is about), so most of an entity's rules fit.
_MAX_RULES_SHOWN = 120
_MIN_PER_KIND = 8
#: Per kind a neighbour keeps (refusals; for the owner of chosen lines, also what its
#: form says about editing them), and what an entity the request merely names keeps.
_MAX_NEIGHBOUR_RULES = 24
_MAX_NAMED_RULES = 24
_MAX_NEIGHBOUR_PAGES = 3
_NEIGHBOUR_FIELDS = 20


def _ranked_rules(entity: IndexEntity, request: str):
    """The entity's rules, those sharing words with the request first, each kind kept
    represented so that a question the words do not reach still meets its kind."""
    words = _tokens(request)
    scored = []
    listed: set[tuple[str, str]] = set()
    for position, rule in enumerate(entity.rules):
        # One catalogue entry per control or field and kind: the answer brings the
        # other rules of the same kind about the same thing along with the one named.
        if (rule.kind, rule.about) in listed:
            continue
        listed.add((rule.kind, rule.about))
        overlap = len(words & (_tokens(rule.about) | _tokens(rule.text)))
        scored.append((-overlap, position, rule))
    scored.sort(key=lambda item: item[:2])
    hits = [rule for overlap, _, rule in scored if overlap < 0]
    rest = [rule for overlap, _, rule in scored if overlap == 0]
    # Every kind keeps a floor, so a question the words do not reach (a refusal asked
    # about in other terms) still meets rules of its kind; the rest round-robin.
    by_kind: dict[str, list] = {}
    for rule in rest:
        by_kind.setdefault(rule.kind, []).append(rule)
    chosen = list(hits[:_MAX_RULES_SHOWN])
    for kind, pool in by_kind.items():
        have = sum(1 for r in chosen if r.kind == kind)
        while pool and have < _MIN_PER_KIND and len(chosen) < _MAX_RULES_SHOWN:
            chosen.append(pool.pop(0))
            have += 1
    while any(by_kind.values()) and len(chosen) < _MAX_RULES_SHOWN:
        for kind in list(by_kind):
            if by_kind[kind] and len(chosen) < _MAX_RULES_SHOWN:
                chosen.append(by_kind[kind].pop(0))
    return tuple(chosen)


def _named(entity: IndexEntity, request: str) -> bool:
    """Whether the request names the entity by its label or an alias, or one of its
    controls or fields by the exact label the page shows (two words at least, so that
    a one-word label such as Date names nothing)."""
    request_norm = "".join(request.casefold().split())
    if any("".join(label.casefold().split()) in request_norm
           for label in (entity.label, *entity.aliases) if label):
        return True
    words = _tokens(request)
    for about in (*entity.actions, *(r.about for r in entity.rules)):
        # A rule about a window's field is labelled "Control › Field": each is a label.
        for label in about.split("›"):
            parts = _tokens(label)
            if len(parts) >= 2 and parts <= words and any(re.search(r"[a-z]", p) for p in parts):
                return True
    return False


def neighbourhood(index: ApplicationIndex, keys: tuple[str, ...],
                  request: str = "", *, rules: bool = True) -> ApplicationIndex:
    """The chosen entities with what a declaration about them may need to name: the
    entities their link fields point to, their lines, the records that own them, and
    every page that shows any of these. Rendered like the full index, only smaller.
    An entity the request names by label or alias is chosen too, since the first step
    may have picked a related one instead. Rules are catalogued on the chosen entities,
    ranked by the request's words and capped; a neighbour carries its refusals, and
    the owner of chosen lines also what its form says about editing them. With
    `rules` false (a request about data, not about how the application behaves) no
    rules are carried at all, and an entity is joined only by its label or alias."""
    chosen = {k for k in keys if index.entity(k) is not None}
    # An entity the request names joins with a few of its rules, without bringing its
    # own neighbours: the first step's choice keeps the neighbourhood's shape.
    named = {e.key for e in index.entities if e.key not in chosen
             and _named(e if rules else replace(e, actions=(), rules=()), request)}
    wanted = set(chosen) | named
    owners: set[str] = set()
    by_label = {e.label: e.key for e in index.entities}
    for key in chosen:
        entity = index.entity(key)
        for f in entity.fields:
            if f.kind == "link" and f.relation and index.entity(f.relation) is not None:
                wanted.add(f.relation)
        if entity.lines_of and entity.lines_of in by_label:
            wanted.add(by_label[entity.lines_of])
            owners.add(by_label[entity.lines_of])
        for other in index.entities:
            if other.lines_of == entity.label:
                wanted.add(other.key)

    def kept(entity: IndexEntity):
        if not rules:
            return ()
        if entity.key in chosen:
            return _ranked_rules(entity, request)
        if entity.key in named:
            return _ranked_rules(entity, request)[:_MAX_NAMED_RULES]
        # A neighbour keeps what an action on the chosen record may run into: its
        # refusals (a payment registered on an invoice is refused by the invoice's
        # rules); the owner of chosen lines also when its form lets the lines be edited.
        kinds = ("refusal", "editability") if entity.key in owners else ("refusal",)
        listed: set[tuple[str, str]] = set()
        out = []
        for kind in kinds:
            count = 0
            for rule in entity.rules:
                if rule.kind == kind and (rule.kind, rule.about) not in listed \
                        and count < _MAX_NEIGHBOUR_RULES:
                    listed.add((rule.kind, rule.about))
                    out.append(rule)
                    count += 1
        return tuple(out)

    # The chosen entities with every field they have; a neighbour with the few a full
    # index shows, since a declaration names its fields only through a link.
    entities = tuple(replace(e, rules=kept(e),
                             fields=e.fields if e.key in chosen else e.fields[:_NEIGHBOUR_FIELDS])
                     for e in index.entities if e.key in wanted)
    # Every page of a chosen entity; of a neighbour, the first few (its main pages).
    per_neighbour: dict[str, int] = {}
    pages = []
    for page in index.pages:
        if page.entity in chosen:
            pages.append(page)
        elif page.entity in wanted and per_neighbour.get(page.entity, 0) < _MAX_NEIGHBOUR_PAGES:
            per_neighbour[page.entity] = per_neighbour.get(page.entity, 0) + 1
            pages.append(page)
    pages = tuple(pages)
    return ApplicationIndex(entities=entities, pages=pages, source_digest=index.source_digest,
                            complete=index.complete, focused=True)


__all__ = ["KnownReference", "correct_records", "locate", "neighbourhood", "ranked_entities", "references",
           "summaries"]
