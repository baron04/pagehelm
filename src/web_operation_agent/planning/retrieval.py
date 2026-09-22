"""Deterministic retrieval of bounded, cited evidence from a compiled source map."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schemas import EvidenceRef, SourceMap, SourceRelation

STOPWORDS = frozenset("""
a an the and or but of to for from with without on in into at by as is are was were be been
this that these those it its their they them we our you your he she his her him
must should can could may might will would shall do does did done have has had
please make take put set get go now then than so such not no yes all any some each every
came come comes went gone want wants wanted need needs needed everything else stay stays
about against off out up down over under after before same other another new old
""".split())

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "") if w.lower() not in STOPWORDS]


def _stem(word: str) -> str:
    for suffix in ("ies", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _akin(a: str, b: str) -> bool:
    """Same word to a reader: equal, or one a prefix of the other from four characters on."""
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and long.startswith(short)


def _camel_tokens(name: str) -> list[str]:
    """`createPaymentApplication` -> ['create', 'payment', 'application']"""
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", name or "")
    return [p.lower() for p in parts if p]


@dataclass
class EvidenceBundle:
    """The bounded, cited slice of the map that a planner is allowed to see.

    An empty one is the control arm's input: no facts, nothing to cite. It carries the same
    fields so both arms produce the same shape of record.
    """
    task_ref: str
    lines: list[str] = field(default_factory=list)
    refs: dict[str, EvidenceRef] = field(default_factory=dict)
    stats: dict = field(default_factory=lambda: {"lines": 0, "citations": 0})

    def render(self) -> str:
        return "\n".join(self.lines)

    @property
    def citation_keys(self) -> set[str]:
        return set(self.refs)


class SourceRetriever:
    def __init__(self, smap: SourceMap):
        self.map = smap
        self.by_kind: dict[str, list[SourceRelation]] = {}
        for r in smap.relations:
            self.by_kind.setdefault(r.kind, []).append(r)
        self.handlers = {r.subject: r for r in self.by_kind.get("request_handler", [])}
        self._forms_by_target: dict[str, list[SourceRelation]] = {}
        for r in self.by_kind.get("form_target", []):
            self._forms_by_target.setdefault(r.object, []).append(r)
        self._fields_by_form: dict[str, list[SourceRelation]] = {}
        for r in self.by_kind.get("form_field", []):
            self._fields_by_form.setdefault(r.subject, []).append(r)
        self._params_by_service: dict[str, list[SourceRelation]] = {}
        for r in self.by_kind.get("service_parameter", []):
            self._params_by_service.setdefault(r.subject, []).append(r)
        self._responses_by_request: dict[str, list[SourceRelation]] = {}
        for r in self.by_kind.get("request_response", []):
            self._responses_by_request.setdefault(r.subject, []).append(r)
        self._views = {r.subject: r for r in self.by_kind.get("view_screen", [])}
        self._forms_by_screen: dict[str, list[SourceRelation]] = {}
        for r in self.by_kind.get("screen_includes_form", []):
            self._forms_by_screen.setdefault(r.subject, []).append(r)
        self._targets_by_form: dict[str, list[SourceRelation]] = {}
        for r in self.by_kind.get("form_target", []):
            self._targets_by_form.setdefault(r.subject, []).append(r)

    # -- scoring -----------------------------------------------------------------------
    @staticmethod
    def _overlap(words: set[str], candidate: list[str]) -> float:
        """Fraction of the candidate's words the task also uses, damped by its length.

        Matching is prefix-tolerant: a user writes "sprouted", "applied", "received", while
        a request is named `sproutSeedling`, `createPaymentApplication`,
        `receiveStockItem`. Requiring equality after a crude stemmer threw those
        matches away and left the retriever returning only navigation.
        """
        if not candidate:
            return 0.0
        cand = {_stem(c) for c in candidate}
        hit = sum(1 for c in cand if any(_akin(c, w) for w in words))
        return hit / (len(cand) ** 0.5)

    def _entity_of(self, service: str) -> str:
        for r in self.by_kind.get("service_entity", []):
            if r.subject == service:
                return r.object
        return ""

    def _vocabulary(self, webapp: str, uri: str, service: str) -> list[str]:
        """Every word the deployment itself associates with this request.

        The request name alone is not enough. A user asks to "put those dollars against
        their open invoice"; the request is called `createPaymentApplication` and the word
        "application" appears nowhere in the task. What does connect them is what the
        request writes (the PaymentApplication entity) and what its form asks for
        (invoiceId, amountApplied) -- both declared, both checkable, neither specific to any
        one task.
        """
        words = _camel_tokens(uri) + _camel_tokens(service) + _camel_tokens(self._entity_of(service))
        for f in self._forms_by_target.get(uri, [])[:3]:
            words += _camel_tokens(f.attrs.get("form_name", ""))
            for fld in self._fields_by_form.get(f.subject, [])[:20]:
                words += _camel_tokens(fld.object)
        return words

    def _score(self, words: set[str], vocab: list[str]) -> tuple[int, float]:
        """(distinct task words covered, density).

        Ranking by density alone rewarded short names: `updatePayment` covers one of two
        words and scored above `createPaymentApplication`, which covers the same word out of
        three, even though the longer name is the more specific answer. Coverage of the
        *task* is what matters; density only breaks ties.
        """
        cand = {_stem(c) for c in vocab}
        covered = {w for w in words if any(_akin(c, w) for c in cand)}
        density = len(covered) / (len(cand) ** 0.5) if cand else 0.0
        return len(covered), round(density, 4)

    #: How many scored requests reach the planner. Raised from 30 after measuring what the
    #: cut actually decided. For one task the request its write path needs ranked 46th of
    #: 2,794 under one expansion of the query and inside the top 30 under another, so a model
    #: call in the expander -- not the source and not the ranking -- was deciding whether the
    #: evidence contained the request at all. A wider cut makes coverage insensitive to that
    #: variation; the bundle grows from roughly 62 to 105 lines, which the planner's capped
    #: reasoning budget absorbs.
    MAX_REQUESTS = 60

    def retrieve(self, task_ref: str, prompt: str, *, max_requests: int = MAX_REQUESTS,
                 max_menu: int = 8, max_forms: int = 8, max_lines: int = 160,
                 webapps: set[str] | None = None, extra_terms: tuple[str, ...] = ()) -> EvidenceBundle:
        """`webapps` limits candidates to what this deployment actually serves.

        A source tree can contain modules the deployment does not serve. Ranking over all
        of them would offer requests that no current page can reach. A contract must only
        offer moves the running application actually has. The set comes
        from the entry page's own navigation, so it is a runtime fact, not a guess.

        `extra_terms` is the vocabulary bridge between a user's wording and a framework's
        method or route names. String matching alone cannot cross that gap, so terms are
        expanded first (see
        `expand_terms`) and the ranking stays deterministic given the expanded query.
        """
        words = {_stem(w) for w in _tokens(prompt)} | {_stem(w) for w in extra_terms if w}
        bundle = EvidenceBundle(task_ref=task_ref)

        # 1. navigation: which visible words lead where
        menus = []
        for r in self.by_kind.get("menu_link", []):
            if webapps and not (set(r.attrs.get("webapps") or []) & webapps):
                continue
            title = r.attrs.get("title") or ""
            hits, density = self._score(words, _tokens(title) + _camel_tokens(r.object))
            if hits:
                menus.append(((hits, density), r))
        menus.sort(key=lambda t: (-t[0][0], -t[0][1], t[1].subject))
        seen_titles: set[tuple] = set()
        chosen_menus = []
        for _, r in menus:
            key = (r.attrs.get("title", ""), r.object)
            if key in seen_titles:
                continue
            seen_titles.add(key)
            chosen_menus.append(r)
            if len(chosen_menus) >= max_menu:
                break

        # 2. candidate requests, ranked on everything the deployment associates with them
        reqs = []
        for subject, r in self.handlers.items():
            webapp, _, uri = subject.partition("/")
            if webapps and webapp not in webapps:
                continue
            service = r.object if r.attrs.get("event_type") == "service" else ""
            hits, density = self._score(words, self._vocabulary(webapp, uri, service))
            if hits:
                reqs.append(((hits, density), subject, r))
        reqs.sort(key=lambda t: (-t[0][0], -t[0][1], t[1]))
        # A matched navigation relation is an explicit edge, not merely another bag of
        # keywords. Follow it before filling the remaining budget with lexical candidates.
        # Otherwise a generic label such as "Add" can cite the useful route while the
        # handler that proves it read-only is displaced by dozens of same-word requests.
        chosen_reqs = []
        seen_requests: set[str] = set()
        for menu in chosen_menus:
            for webapp in menu.attrs.get("webapps") or []:
                subject = f"{webapp}/{menu.object}"
                handler = self.handlers.get(subject)
                if handler is not None and subject not in seen_requests:
                    chosen_reqs.append((subject, handler))
                    seen_requests.add(subject)
                    if len(chosen_reqs) >= max_requests:
                        break
            if len(chosen_reqs) >= max_requests:
                break
        for _, subject, relation in reqs:
            if len(chosen_reqs) >= max_requests:
                break
            if subject not in seen_requests:
                chosen_reqs.append((subject, relation))
                seen_requests.add(subject)

        # 3. render, following the declared chains outward from each choice
        for r in chosen_menus:
            parameter_notes = []
            for parameter in r.attrs.get("parameters") or []:
                name = parameter.get("name", "")
                from_field = parameter.get("from_field", "")
                value = parameter.get("value", "")
                if from_field:
                    source = f"field `{from_field}`"
                elif value:
                    source = f"literal `{value}`"
                elif name:
                    source = "the same-named field (implicit)"
                else:
                    source = "an unspecified source"
                parameter_notes.append(f"`{name or '(unnamed)'}` from {source}")
            parameter_clause = (f"; declared parameters: {'; '.join(parameter_notes)}"
                                if parameter_notes else "")
            self._emit(bundle, r,
                       f"navigation: the menu item shown as \"{r.attrs.get('title')}\" links to "
                       f"request `{r.object}` "
                       f"(webapps: {', '.join(r.attrs.get('webapps') or [])}"
                       f"{'; shown only when a condition holds' if r.attrs.get('conditional') else ''}"
                       f"{parameter_clause})")

        for subject, r in chosen_reqs:
            _, _, uri = subject.partition("/")
            svc = r.object if r.attrs.get("event_type") == "service" else ""
            entity = self._entity_of(svc) if svc else ""
            self._emit(bundle, r,
                       f"request `{subject}` is handled by "
                       f"{'service `' + svc + '`' if svc else (r.attrs.get('event_type') or 'a view')}"
                       + (f", which writes {entity}" if entity else "")
                       + f" (auth={r.attrs.get('auth') or 'n/a'})")
            # Already compiled controller facts must survive retrieval. A type=request
            # response continues processing within the controller; treating its target as
            # another browser submit invents an extra write. Keep the response condition
            # and application identity, and do not conflate views or redirects with it.
            for response in self._responses_by_request.get(subject, []):
                if response.attrs.get("response_type") == "request":
                    self._emit(bundle, response,
                               f"response `{response.attrs.get('response_name')}` of request "
                               f"`{subject}` declares type=request, target `{response.object}` "
                               f"in controller webapp `{response.attrs.get('webapp')}`; "
                               "this is a server-side continuation, not a second browser submission")
                elif response.attrs.get("response_type") == "view":
                    self._emit(bundle, response,
                               f"response `{response.attrs.get('response_name')}` of request "
                               f"`{subject}` renders view `{response.object}`")
                    webapp = response.attrs.get("webapp") or subject.partition("/")[0]
                    view = self._views.get(f"{webapp}/{response.object}")
                    if view is None:
                        continue
                    self._emit(bundle, view,
                               f"view `{response.object}` renders screen `{view.object}`")
                    for include in self._forms_by_screen.get(view.object, []):
                        self._emit(bundle, include,
                                   f"screen `{view.object}` includes form `{include.object}`")
                        for form_target in self._targets_by_form.get(include.object, []):
                            names, seen = [], set()
                            for field in self._fields_by_form.get(form_target.subject, []):
                                if field.object not in seen:
                                    seen.add(field.object)
                                    names.append(field.object)
                            target = form_target.object
                            target_note = (
                                f"uses alternate request `{target}` when "
                                f"`{form_target.attrs.get('use_when')}`"
                                if form_target.attrs.get("target_kind") == "alternate"
                                else f"submits to request `{target}`")
                            self._emit(bundle, form_target,
                                       f"included form "
                                       f"`{form_target.attrs.get('form_name')}` {target_note}; "
                                       f"it posts: {', '.join(names[:14]) or '(no named fields)'}")
            for f in self._forms_by_target.get(uri, [])[:2]:
                names, seen = [], set()
                for x in self._fields_by_form.get(f.subject, []):
                    if x.object not in seen:
                        seen.add(x.object)
                        names.append(x.object)
                target_note = (f"uses alternate submit `{uri}` when "
                               f"`{f.attrs.get('use_when')}`"
                               if f.attrs.get("target_kind") == "alternate"
                               else f"submits to `{uri}`")
                self._emit(bundle, f,
                           f"form `{f.attrs.get('form_name')}` {target_note}; it posts: "
                           f"{', '.join(names[:14]) or '(no named fields)'}")
            if svc:
                params = self._params_by_service.get(svc, [])
                required = [p for p in params if p.attrs.get("mode", "").startswith("IN")
                            and p.attrs.get("optional") == "false" and p.object != "*auto*"]
                optional = [p for p in params if p.attrs.get("mode", "").startswith("IN")
                            and p.attrs.get("optional") != "false" and p.object != "*auto*"]
                if required or optional:
                    self._emit(bundle, (required or optional)[0],
                               f"service `{svc}` requires "
                               f"{', '.join(p.object for p in required) or '(nothing mandatory)'}"
                               f"; optional inputs include "
                               f"{', '.join(p.object for p in optional[:8]) or '(none)'}")

        # 4. forms whose target no controller declares.
        # Some applications build request names at runtime from the entity, so a form can
        # target an operation that no static controller declaration mentions. Walking forms
        # only outward from chosen requests made those operations unreachable no matter how
        # well they matched, so forms are ranked as candidates in their own right.
        emitted_targets = {t for t, _ in ((s.partition("/")[2], r) for s, r in chosen_reqs)}
        forms = []
        for target, group in self._forms_by_target.items():
            if target in emitted_targets:
                continue
            for f in group:
                if webapps and not (set(f.attrs.get("webapps") or []) & webapps):
                    continue
                vocab = (_camel_tokens(f.attrs.get("form_name", "")) + _camel_tokens(target)
                         + [t for fld in self._fields_by_form.get(f.subject, [])[:20]
                            for t in _camel_tokens(fld.object)])
                hits, density = self._score(words, vocab)
                if hits:
                    forms.append(((hits, density), target, f))
        forms.sort(key=lambda t: (-t[0][0], -t[0][1], t[1]))
        seen_form_targets: set[str] = set()
        shown_forms = 0
        for _, target, f in forms:
            if target in seen_form_targets:
                continue
            seen_form_targets.add(target)
            names, seen = [], set()
            for x in self._fields_by_form.get(f.subject, []):
                if x.object not in seen:
                    seen.add(x.object)
                    names.append(x.object)
            target_note = (f"uses alternate request `{target}` when "
                           f"`{f.attrs.get('use_when')}`"
                           if f.attrs.get("target_kind") == "alternate"
                           else f"submits to request `{target}`")
            self._emit(bundle, f,
                       f"form `{f.attrs.get('form_name')}` (webapps: "
                       f"{', '.join(f.attrs.get('webapps') or [])}) {target_note}; it posts: "
                       f"{', '.join(names[:14]) or '(no named fields)'}")
            shown_forms += 1
            if shown_forms >= max_forms:
                break

        if len(bundle.lines) > max_lines:
            bundle.lines = bundle.lines[:max_lines]
            bundle.refs = {k: v for k, v in bundle.refs.items()
                           if any(k in line for line in bundle.lines)}
        bundle.stats = {"menu_candidates": len(menus), "request_candidates": len(reqs),
                        "requests_shown": len(chosen_reqs), "form_candidates": len(forms),
                        "forms_shown": shown_forms, "lines": len(bundle.lines),
                        "citations": len(bundle.refs), "webapps": sorted(webapps or []),
                        "extra_terms": list(extra_terms)}
        return bundle

    @staticmethod
    def _emit(bundle: EvidenceBundle, relation, text: str) -> None:
        ref = relation.evidence[0]
        line = f"[{ref.key}] {text}"
        if line in bundle.lines:
            return
        bundle.lines.append(line)
        bundle.refs[ref.key] = ref
