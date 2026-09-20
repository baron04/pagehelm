"""Compile a small, source-bound set of read-only navigation affordances.

The resolver is deliberately narrower than :class:`SourceRetriever`.  It does not ask a
model to reconstruct a URL from prose.  It joins relations already present in a
``SourceMap`` and emits typed candidates only when the deployed controller declares the
request to be view-only.  Runtime parameter expressions are carried as inert strings;
this module never evaluates them or reads runtime state.

Ranking is deterministic.  Public task and query terms establish eligibility and carry
substantially more weight than page terms.  Page text can break a tie between already
eligible candidates, but can never cause a candidate to be returned by itself.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

from .composition.ports import ApplicationRoutePort
from .schemas import SourceMap, SourceRelation, canonical_json


_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_STOP_WORDS = frozenset({
    "a", "add", "an", "and", "at", "create", "edit", "find", "for", "from",
    "go", "in", "into", "list", "new", "of", "on", "open", "page", "please",
    "screen", "show", "stop", "the", "to", "update", "view", "when",
})
_ALIASES = {
    "clone": "copy", "copied": "copy", "copies": "copy", "duplicate": "copy",
    "duplicated": "copy", "duplicates": "copy",
    "identifier": "identity", "identifiers": "identity", "identification": "identity",
    "identifications": "identity", "ids": "identity",
    "locate": "find", "located": "find", "search": "find",
    "modify": "edit", "modified": "edit", "update": "edit", "updated": "edit",
}


def _require_string(value: object, name: str, *, empty: bool = False) -> None:
    if not isinstance(value, str) or (not empty and not value):
        qualifier = "a string" if empty else "a non-empty string"
        raise ValueError(f"{name} must be {qualifier}")


@dataclass(frozen=True, order=True)
class AffordanceParameter:
    """An unevaluated parameter declaration copied from a menu link."""

    name: str
    from_field: str = ""
    value: str = ""

    def __post_init__(self) -> None:
        _require_string(self.name, "parameter.name")
        _require_string(self.from_field, "parameter.from_field", empty=True)
        _require_string(self.value, "parameter.value", empty=True)

    def to_json(self) -> dict:
        return {"name": self.name, "from_field": self.from_field, "value": self.value}


@dataclass(frozen=True)
class SourceAffordance:
    """A cited, deployment-bound option that may be opened without a write event."""

    candidate_id: str
    source_map_digest: str
    title: str
    webapp: str
    request: str
    route_template: str
    parameters: tuple[AffordanceParameter, ...]
    view: str
    screen: str
    forms: tuple[str, ...]
    citations: tuple[str, ...]
    rank_score: int
    matched_terms: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("candidate_id", "source_map_digest", "title", "webapp", "request",
                     "route_template"):
            _require_string(getattr(self, name), name)
        _require_string(self.view, "view", empty=True)
        _require_string(self.screen, "screen", empty=True)
        if (not self.route_template.startswith(f"/{self.webapp}/")
                or "\\" in self.route_template):
            raise ValueError("route_template must be a same-webapp relative route")
        if not self.citations:
            raise ValueError("a source affordance must cite its SourceMap facts")
        if not isinstance(self.rank_score, int) or self.rank_score <= 0:
            raise ValueError("rank_score must be a positive integer")

    def to_json(self) -> dict:
        """Return the complete wire representation; no dataclass internals leak through."""
        return {
            "candidate_id": self.candidate_id,
            "source_map_digest": self.source_map_digest,
            "title": self.title,
            "webapp": self.webapp,
            "request": self.request,
            "route_template": self.route_template,
            "parameters": [p.to_json() for p in self.parameters],
            "view": self.view,
            "screen": self.screen,
            "forms": list(self.forms),
            "citations": list(self.citations),
            "rank_score": self.rank_score,
            "matched_terms": list(self.matched_terms),
        }


def _tokens(text: str) -> set[str]:
    pieces: list[str] = []
    for raw in _WORD_RE.findall(text):
        pieces.extend(_CAMEL_RE.split(raw))
    out: set[str] = set()
    for piece in pieces:
        token = piece.casefold()
        if len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        token = _ALIASES.get(token, token)
        if token and token not in _STOP_WORDS:
            out.add(token)
    return out


def _humanize(value: str) -> str:
    leaf = value.rsplit("/", 1)[-1]
    human = " ".join(_CAMEL_RE.split(leaf.replace("_", " ").replace("-", " "))).strip()
    # Some OFBiz controller names concatenate a generic verb and a business noun in all
    # lower case (for example ``viewidentifications``), so CamelCase splitting cannot see
    # the useful noun.  Split only the long, common controller prefixes; their generic verb
    # is a stop word and the remainder carries the ranking signal.
    if human == leaf and leaf == leaf.casefold():
        for prefix in ("create", "view", "find", "edit", "list", "show"):
            if leaf.startswith(prefix) and len(leaf) >= len(prefix) + 4:
                return prefix + " " + leaf[len(prefix):]
    return human


def _refs(relations: Iterable[SourceRelation]) -> set[str]:
    return {ref.key for relation in relations for ref in relation.evidence}


def _identity(subject: str) -> tuple[str, str] | None:
    clean = subject.strip("/")
    if "/" not in clean:
        return None
    webapp, request = clean.split("/", 1)
    return (webapp, request) if webapp and request else None


def _menu_destinations(relation: SourceRelation,
                       known: set[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Resolve only syntactic menu targets; never evaluate an OFBiz expression."""
    raw = relation.object.strip()
    if not raw:
        return ()
    split = urlsplit(raw)
    if split.scheme or split.netloc or "\\" in split.path:
        return ()
    path = split.path.strip("/")
    parts = path.split("/")
    if len(parts) >= 3 and parts[-2] == "control":
        explicit = (parts[-3], parts[-1])
        return (explicit,) if explicit in known else ()
    if len(parts) >= 2 and (parts[0], "/".join(parts[1:])) in known:
        return ((parts[0], "/".join(parts[1:])),)
    webapps = relation.attrs.get("webapps", ())
    if not isinstance(webapps, (list, tuple)):
        return ()
    return tuple(sorted({(webapp, path) for webapp in webapps
                         if isinstance(webapp, str) and (webapp, path) in known}))


def _parameters(menus: Iterable[SourceRelation]) -> tuple[AffordanceParameter, ...]:
    params: set[AffordanceParameter] = set()
    for menu in menus:
        raw = menu.attrs.get("parameters", ())
        if not isinstance(raw, (list, tuple)):
            continue
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
                continue
            from_field = item.get("from_field", "")
            value = item.get("value", "")
            if isinstance(from_field, str) and isinstance(value, str):
                params.add(AffordanceParameter(item["name"], from_field, value))
    return tuple(sorted(params))


def _one_declared_object(relations: Iterable[SourceRelation], *, prefer_success: bool = False
                         ) -> tuple[str, tuple[SourceRelation, ...]]:
    """Return one unambiguous declared object and the relations that support it.

    A request may declare different response views and a view may map to different screens.
    Picking the lexicographically first one would turn an unresolved branch into a false
    source fact.  A unique success response is usable; otherwise the object stays unknown.
    """
    rows = tuple(relations)
    preferred = tuple(r for r in rows if r.attrs.get("response_name") == "success") \
        if prefer_success else rows
    if prefer_success and not preferred:
        preferred = rows
    objects = {r.object for r in preferred if r.object}
    if len(objects) != 1:
        return "", ()
    obj = next(iter(objects))
    return obj, tuple(sorted((r for r in preferred if r.object == obj),
                             key=lambda r: tuple(ref.key for ref in r.evidence)))


def _menu_variants(menus: Iterable[SourceRelation], request: str
                   ) -> tuple[tuple[str, tuple[AffordanceParameter, ...],
                                    tuple[SourceRelation, ...], str, bool], ...]:
    """Keep parameter schemas separate across menu contexts.

    The same controller route is often linked from several menus with different literals,
    runtime sources or conditions.  Unioning those declarations fabricates a parameter
    schema that does not exist on any source edge.  Equivalent duplicate relations inside
    one exact menu context may still share a candidate and its citations.
    """
    groups: dict[tuple, list[SourceRelation]] = {}
    for menu in menus:
        title = (str(menu.attrs.get("title", "")).strip()
                 if menu.attrs.get("title_resolved") is not False else "")
        title = title or _humanize(request)
        parameters = _parameters((menu,))
        conditional = bool(menu.attrs.get("conditional"))
        key = (menu.subject, title.casefold(), parameters, conditional)
        groups.setdefault(key, []).append(menu)
    if not groups:
        return ((_humanize(request), (), (), "", False),)
    return tuple(
        (rows[0].attrs.get("title", "")
         if rows[0].attrs.get("title_resolved") is not False
         and str(rows[0].attrs.get("title", "")).strip()
         else _humanize(request),
         key[2], tuple(sorted(rows, key=lambda r: tuple(ref.key for ref in r.evidence))),
         key[0], key[3])
        for key, rows in sorted(groups.items(), key=lambda item: (
            item[0][1], item[0][0], item[0][2], item[0][3]))
    )


def resolve_source_affordances(source_map: SourceMap, *, task: str, query: str = "",
                               page_text: str = "", served_webapps: Iterable[str],
                               route_port: ApplicationRoutePort,
                               limit: int = 5) -> tuple[SourceAffordance, ...]:
    """Return at most ``limit`` deterministic, view-only candidates.

    ``task``, ``query`` and ``page_text`` must already be public/sanitized.  The function
    has no filesystem, browser, evaluator, database or model access.  A controller identity
    is eligible only when every declaration for that identity is explicitly ``view-only``;
    a conflicting write handler therefore removes the route rather than being guessed away.
    ``route_port`` composes each candidate's route the way its adapter's typed tools would
    accept it; this module carries no application-specific route grammar of its own.
    """
    if not isinstance(source_map, SourceMap):
        raise TypeError("source_map must be a SourceMap")
    if not isinstance(route_port, ApplicationRoutePort):
        raise TypeError("route_port must implement ApplicationRoutePort")
    for value, name in ((task, "task"), (query, "query"), (page_text, "page_text")):
        _require_string(value, name, empty=True)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 12:
        raise ValueError("limit must be an integer from 1 through 12")
    served = frozenset(served_webapps)
    if not served or any(not isinstance(item, str) or not item for item in served):
        raise ValueError("served_webapps must contain non-empty strings")

    task_terms, query_terms, page_terms = _tokens(task), _tokens(query), _tokens(page_text)
    if not task_terms and not query_terms:
        return ()

    grouped_handlers: dict[tuple[str, str], list[SourceRelation]] = {}
    for relation in source_map.by_kind("request_handler"):
        key = _identity(relation.subject)
        if key is not None:
            grouped_handlers.setdefault(key, []).append(relation)
    view_only = {
        key: relations for key, relations in grouped_handlers.items()
        if key[0] in served and relations
        and all(relation.attrs.get("event_type") == "view-only" for relation in relations)
    }

    menus_by_route: dict[tuple[str, str], list[SourceRelation]] = {}
    known = set(view_only)
    for menu in source_map.by_kind("menu_link"):
        for key in _menu_destinations(menu, known):
            menus_by_route.setdefault(key, []).append(menu)

    responses_by_route: dict[tuple[str, str], list[SourceRelation]] = {}
    for response in source_map.by_kind("request_response"):
        key = _identity(response.subject)
        if (key in view_only and response.attrs.get("response_type") == "view"
                and response.object):
            responses_by_route.setdefault(key, []).append(response)

    view_screens: dict[str, list[SourceRelation]] = {}
    for relation in source_map.by_kind("view_screen"):
        view_screens.setdefault(relation.subject, []).append(relation)
    screen_forms: dict[str, list[SourceRelation]] = {}
    for relation in source_map.by_kind("screen_includes_form"):
        screen_forms.setdefault(relation.subject, []).append(relation)

    ranked: list[tuple[int, str, str, str, str, SourceAffordance]] = []
    source_digest = source_map.content_digest
    for (webapp, request), handlers in view_only.items():
        menus = menus_by_route.get((webapp, request), [])
        view, response_relations = _one_declared_object(
            responses_by_route.get((webapp, request), ()), prefer_success=True)
        screen, screen_relations = _one_declared_object(
            view_screens.get(f"{webapp}/{view}", ())) if view else ("", ())
        includes = tuple(sorted(screen_forms.get(screen, ()),
                                key=lambda r: (r.object,
                                               tuple(ref.key for ref in r.evidence)))) \
            if screen else ()
        forms = tuple(sorted({relation.object for relation in includes if relation.object}))

        for title, parameters, menu_relations, menu_context, conditional in \
                _menu_variants(menus, request):
            searchable = " ".join((title, request, _humanize(request), view, screen,
                                   " ".join(forms),
                                   " ".join(parameter.name for parameter in parameters)))
            candidate_terms = _tokens(searchable)
            task_matches = task_terms & candidate_terms
            query_matches = query_terms & candidate_terms
            primary_matches = task_matches | query_matches
            if not primary_matches:
                continue
            page_matches = page_terms & candidate_terms
            score = 8 * len(task_matches) + 5 * len(query_matches) + min(3, len(page_matches))
            folded_task, folded_query = task.casefold(), query.casefold()
            if _tokens(title) and title.casefold() in folded_task:
                score += 20
            elif _tokens(title) and title.casefold() in folded_query:
                score += 12
            human_request = _humanize(request).casefold()
            if _tokens(human_request) and human_request in folded_task:
                score += 16
            elif _tokens(human_request) and human_request in folded_query:
                score += 10

            chain = (list(handlers) + list(menu_relations) + list(response_relations)
                     + list(screen_relations) + list(includes))
            citations = tuple(sorted(_refs(chain)))
            route_template = route_port.compose_contract_route(webapp, request)
            identity_payload = {
                "source_map_digest": source_digest,
                "route": route_template,
                "menu_context": menu_context,
                "conditional": conditional,
                "parameters": [parameter.to_json() for parameter in parameters],
                "citations": list(citations),
            }
            candidate_id = "sa_" + hashlib.sha256(
                canonical_json(identity_payload).encode("utf-8")).hexdigest()[:24]
            candidate = SourceAffordance(
                candidate_id=candidate_id,
                source_map_digest=source_digest,
                title=title,
                webapp=webapp,
                request=request,
                route_template=route_template,
                parameters=parameters,
                view=view,
                screen=screen,
                forms=forms,
                citations=citations,
                rank_score=score,
                matched_terms=tuple(sorted(primary_matches)),
            )
            ranked.append((-score, title.casefold(), webapp, request, candidate_id,
                           candidate))

    ranked.sort(key=lambda item: item[:5])
    return tuple(item[5] for item in ranked[:limit])
