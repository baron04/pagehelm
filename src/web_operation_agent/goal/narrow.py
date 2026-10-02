"""Narrowing a request to the few entities it is about, before any declaration.

Two things happen here, in this order. First, the hard references in the request --
document numbers, codes, names written in Latin script, amounts and dates -- are cut out
by their shape, which no language changes, and the adapter is asked which records they
name; a reference that names a record settles its entity without any model. Second, the
index is rendered as one line per entity. For a large application, actor-visible labels,
pages and fields retrieve a bounded candidate set before the model picks entities and
deliverable kinds. Located references and reviewed conventions always join that set.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re

from .index import ApplicationIndex, IndexEntity, render_conventions

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
#: Keep the existing full catalogue for small applications. Large catalogues are
#: retrieved before they are shown to a small model; this is a prompt bound, not an
#: index or permission bound.
_MAX_SCOPE_CHARS = 12_000
_MAX_SCOPE_ENTITIES = 60


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
        explicitly_written = bool(record and re.search(
            r"(?<![A-Za-z0-9_/])" + re.escape(record) + r"(?![A-Za-z0-9_/.-])",
            request, re.IGNORECASE))
        if record and _CODE.fullmatch(record) and _norm_ref(record) not in asked and not explicitly_written:
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


def locate_names(port, names: tuple[str, ...], *, entities: tuple[str, ...] = (),
                 context=None) -> tuple[KnownReference, ...]:
    """The records the names a model copied out of the request name, looked for on the
    entities those parts are about and the ones they link to. A name the shape of a
    reference does not reveal (a customer written in Chinese) is found here; one no record
    holds, or several do, adds nothing."""
    method = getattr(port, "locate_names", None)
    if not callable(method):
        return tuple(k for name in names for k in locate(port, name, context=context))
    known: list[KnownReference] = []
    for text, entity, name in method(tuple(names), entities=tuple(entities), context=context) or ():
        if entity and name:
            known.append(KnownReference(text=str(text), entity=str(entity), name=str(name)))
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


def _overlap(label: str, request: str, words: set[str]) -> int:
    """Match whole Latin words and CJK pairs, without application-specific synonyms."""
    label_words = _tokens(label)
    score = len(label_words & words) * 4
    normalized = "".join(request.casefold().split())
    name = "".join(label.casefold().split())
    if len(name) >= 2 and name in normalized:
        score += 24
    pairs = {name[i:i + 2] for i in range(len(name) - 1)
             if _cjk(name[i]) and _cjk(name[i + 1])}
    score += min(4, sum(pair in normalized for pair in pairs)) * 5
    return score


def candidate_entities(index: ApplicationIndex, request: str,
                       known: tuple[KnownReference, ...]) -> tuple[IndexEntity, ...]:
    """Retrieve a bounded, actor-visible set for the first model step.

    Page names and fields help when the request says what it wants to see rather than
    the entity's name. One representative per module helps a request whose words do not
    match the application's labels. This retrieval is advisory: the later declaration
    still resolves only against the full, actor-scoped index.
    """
    named = {k.entity for k in known}
    named.update(c.entity for c in index.conventions if c.entity and c.named_in(request))
    pages: dict[str, list[str]] = {}
    for page in index.pages:
        pages.setdefault(page.entity, []).extend((page.label, page.breadcrumb))
    words = _tokens(request)
    scored = []
    for position, entity in enumerate(index.entities):
        labels = (entity.label, *entity.aliases)
        label_score = max((_overlap(label, request, words) for label in labels), default=0)
        page_score = max((_overlap(label, request, words) for label in pages.get(entity.key, ())),
                         default=0)
        field_score = max((_overlap(label, request, words)
                           for field in (*entity.fields, *entity.held)
                           for label in (field.label, *field.aliases)), default=0)
        owner_score = _overlap(entity.lines_of, request, words) if entity.lines_of else 0
        score = (1000 if entity.key in named else 0) + 4 * label_score + 2 * page_score \
            + field_score + owner_score
        scored.append((-score, position, entity))
    scored.sort(key=lambda row: row[:2])
    # Retain at least one entity from each module among the candidates. The full
    # catalogue's order decides ties, so retrieval and its audit are reproducible.
    representatives = {}
    for row in scored:
        representatives.setdefault(row[2].key.split("/", 1)[0], row[2].key)
    pinned = named | set(representatives.values())
    limit = max(_MAX_SCOPE_ENTITIES, len(pinned))
    chosen = scored[:limit]
    present = {row[2].key for row in chosen}
    missing = [row for row in scored[limit:] if row[2].key in pinned and row[2].key not in present]
    for row in missing:
        replace_at = next((i for i in range(len(chosen) - 1, -1, -1)
                           if chosen[i][2].key not in pinned), None)
        if replace_at is not None:
            chosen[replace_at] = row
    chosen.sort(key=lambda row: row[:2])
    return tuple(row[2] for row in chosen)


def summaries(index: ApplicationIndex, request: str,
              known: tuple[KnownReference, ...]) -> str:
    """Detailed retrieved candidates and a compact directory for a large index.

    The compact directory lets the model select a semantically related entity whose
    label did not share the request's words. It is still bounded; unlike the detailed
    candidates, it carries no fields, controls, aliases or page breadcrumbs. Page
    breadcrumbs identify entities here; page keys belong to the detailed index,
    unless the person explicitly writes a key in the request.
    """
    pages_of: dict[str, list[str]] = {}
    for page in index.pages:
        pages = pages_of.setdefault(page.entity, [])
        explicit = bool(page.key and page.key.casefold() in request.casefold())
        text = f"{page.key} {page.breadcrumb}" if explicit else page.breadcrumb
        if text not in pages:
            pages.append(text)
    lines = ["ENTITIES (key: label [aliases] | pages)"]
    named = {k.entity: k for k in known}
    ranked = ranked_entities(index, request, known)
    large = len(index.entities) > _MAX_SCOPE_ENTITIES
    if large:
        ranked = candidate_entities(index, request, known)
        lines[0] = "LIKELY ENTITIES (key: label [aliases] | pages)"
    shown = set()
    for entity in ranked:
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
        line = " | ".join(parts)
        if large and len("\n".join(lines)) + len(line) > _MAX_SCOPE_CHARS \
                and entity.key not in named:
            continue
        lines.append(line)
        shown.add(entity.key)
    if large:
        lines.append("OTHER ENTITIES (key: label; also selectable)")
        for entity in index.entities:
            if entity.key in shown:
                continue
            line = f"{entity.key}: {entity.label}"
            if len("\n".join(lines)) + len(line) > _MAX_SCOPE_CHARS:
                break
            lines.append(line)
    if known:
        lines.append("")
        lines.append("KNOWN REFERENCES (text -> entity: record)")
        for k in known:
            lines.append(f"{k.text} -> {k.entity}: {k.name}")
    lines += render_conventions([c for c in index.conventions if c.named_in(request)])
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
        if (rule.kind, rule.about, rule.place) in listed:
            continue
        listed.add((rule.kind, rule.about, rule.place))
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


def _names_field(item, request: str, shown=()) -> bool:
    """Whether the request writes a field's label, one of its aliases, or its technical
    name as a word ("可用库存（qty_available）"), outside the longer names in `shown`."""
    if re.search(rf"(?<![\w]){re.escape(item.key.casefold())}(?![\w])", request.casefold()):
        return True
    names = ["".join(label.casefold().split()) for label in (item.label, *item.aliases) if label]
    written = "".join(request.casefold().split())
    for longer in sorted({"".join(n.casefold().split()) for n in shown if n}, key=len, reverse=True):
        if any(len(longer) > len(n) and n in longer for n in names):
            written = written.replace(longer, " ")
    return any(len(n) >= 2 and n in written for n in names)


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


def _partly_named(entity, request: str) -> bool:
    """Whether the request holds a word of an entity's or a field's label: two
    characters of a label in a language written without spaces, or a whole word of four
    letters or more of one written with them."""
    request_norm = "".join(request.casefold().split())
    words = _tokens(request)
    for label in (entity.label, *entity.aliases):
        text = "".join(label.casefold().split())
        if any(_cjk(text[i]) and _cjk(text[i + 1]) and text[i:i + 2] in request_norm
               for i in range(len(text) - 1)):
            return True
        if any(len(w) >= 4 and w in words for w in _tokens(label)):
            return True
    return False


def _cjk(ch: str) -> bool:
    return "\u3400" <= ch <= "\u9fff"


def neighbourhood(index: ApplicationIndex, keys: tuple[str, ...],
                  request: str = "", *, rules: bool = True, held: bool = False,
                  inbound: bool = False) -> ApplicationIndex:
    """The chosen entities with what a declaration about them may need to name: the
    entities their link fields point to, their lines, the records that own them, and
    every page that shows any of these. Rendered like the full index, only smaller.
    An entity the request names by label or alias is chosen too, since the first step
    may have picked a related one instead. Rules are catalogued on the chosen entities,
    ranked by the request's words and capped; a neighbour carries its refusals, and
    the owner of chosen lines also what its form says about editing them. With
    `rules` false (a request about data, not about how the application behaves) no
    rules are carried at all, and an entity is joined only by its label or alias. With
    `held`, a chosen entity also carries the fields its pages do not show that the
    request names. With `inbound`, an entity whose records point at a chosen one also
    joins when the request names part of one of its fields."""
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
            elif (other.key not in wanted
                  and (_partly_named(other, request)
                       or (inbound and any(_partly_named(f, request) for f in other.fields)))
                  and any(f.kind == "link" and f.relation == key for f in other.fields)):
                # Records that point at the chosen one and that the request names in part
                # ("第二期回款" beside a contract: its 回款计划).
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
    # With `held`, a chosen entity's fields its pages do not show come along when the
    # request names them; a neighbour's never do. A name inside a longer name the index
    # already shows ("日记账" in 日记账分录, "Payment" in Payment Terms) names that one.
    shown = [n for e in index.entities for n in (e.label, *e.aliases)]
    shown += [n for e in index.entities for f in e.fields for n in (f.label, *f.aliases)]
    shown += [n for p in index.pages for n in (p.label, *p.aliases)]
    entities = tuple(replace(e, rules=kept(e),
                             fields=e.fields if e.key in chosen else e.fields[:_NEIGHBOUR_FIELDS],
                             held=tuple(f for f in e.held if _names_field(f, request, shown))
                             if held and e.key in chosen else ())
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
                            actor=index.actor,
                            complete=index.complete, focused=True,
                            conventions=tuple(c for c in index.conventions if c.named_in(request)))


__all__ = ["KnownReference", "correct_records", "locate", "locate_names", "neighbourhood", "ranked_entities", "references",
           "summaries"]
