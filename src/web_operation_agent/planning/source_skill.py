"""Compile a reviewable App Skill draft from a version-bound SourceMap.

This is an onboarding helper, not a runtime planner.  It follows only explicit descriptor
relations and refuses ambiguous request/view/screen chains instead of guessing.
"""
from __future__ import annotations

import json
import re
import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..application import ApplicationRoutePort
from ..business.runtime import BusinessSkill, Citation
from ..schemas import SourceMap, SourceRelation


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_GENERIC = frozenset({"find", "open", "page", "report", "screen", "show", "view"})
_ALIASES = {"customer": "sales", "customers": "sales", "day": "date", "days": "date",
            "overdue": "due", "invoices": "invoice"}


def humanize(value: str) -> str:
    leaf = value.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    pieces = []
    for part in re.split(r"[_-]+", leaf):
        pieces.extend(_CAMEL.split(part))
    words = []
    for piece in pieces:
        if piece.casefold() == "id":
            words.append("ID")
        elif piece:
            words.append(piece[:1].upper() + piece[1:])
    return " ".join(words)


def _terms(value: str) -> set[str]:
    terms = set()
    for token in _TOKEN.findall(humanize(value).casefold()):
        token = _ALIASES.get(token, token)
        if token not in _GENERIC and len(token) >= 3:
            terms.add(token)
    return terms


def _refs(rows: list[SourceRelation]) -> tuple[str, ...]:
    return tuple(sorted({ref.key for row in rows for ref in row.evidence}))


def _one(rows: list[SourceRelation], what: str) -> SourceRelation:
    if len(rows) != 1:
        raise ValueError(f"expected one {what}, found {len(rows)}")
    return rows[0]


@dataclass(frozen=True)
class SourceSkillDraft:
    skill: dict
    source_digest: str
    relation_counts: dict[str, int]
    source_chain: tuple[str, ...]

    def to_json(self) -> dict:
        return {
            "skill": self.skill,
            "source_digest": self.source_digest,
            "relation_counts": self.relation_counts,
            "source_chain": list(self.source_chain),
        }


class SourceSkillBootstrapper:
    """Build one route Skill from unambiguous, cited descriptor relations."""

    def __init__(self, source_map: SourceMap, *, route_port: ApplicationRoutePort):
        if not isinstance(route_port, ApplicationRoutePort):
            raise TypeError("route_port must implement ApplicationRoutePort")
        self.source_map = source_map
        self.route_port = route_port

    def search(self, query: str, webapp: str, limit: int = 5) -> tuple[dict, ...]:
        """Rank declared view-only requests from task words; page/menu links are not required."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be non-empty")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("limit must be from 1 to 20")
        query_terms = _terms(query)
        candidates: dict[str, dict] = {}
        for row in self.source_map.by_kind("request_handler"):
            prefix, separator, request = row.subject.partition("/")
            if not separator or prefix != webapp or row.attrs.get("event_type") != "view-only":
                continue
            matched = sorted(query_terms & _terms(request))
            if not matched:
                continue
            candidate = {
                "webapp": webapp, "request": request,
                "title": humanize(request), "matched_terms": matched,
                "score": len(matched), "provenance": list(_refs([row])),
            }
            previous = candidates.get(request)
            if previous is None or candidate["score"] > previous["score"]:
                candidates[request] = candidate
        ranked = sorted(candidates.values(),
                        key=lambda row: (-row["score"], len(row["title"]), row["request"]))
        return tuple(ranked[:limit])

    def bootstrap(self, query: str, webapp: str) -> tuple[tuple[dict, ...], SourceSkillDraft]:
        """Discover and compile the unique highest-ranked view-only request."""
        candidates = self.search(query, webapp, limit=10)
        if not candidates:
            raise ValueError("source search found no matching view-only request")
        if len(candidates) > 1 and candidates[0]["score"] == candidates[1]["score"]:
            raise ValueError("source search has tied top candidates; manual selection required")
        return candidates, self.compile(webapp, candidates[0]["request"])

    def compile(self, webapp: str, request: str) -> SourceSkillDraft:
        if not re.fullmatch(r"[A-Za-z0-9_]+", webapp):
            raise ValueError("webapp is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", request) or ".." in request.split("/"):
            raise ValueError("request is invalid")
        subject = f"{webapp}/{request}"
        handlers = [row for row in self.source_map.by_kind("request_handler")
                    if row.subject == subject]
        handler = _one(handlers, "request_handler")
        if handler.attrs.get("event_type") != "view-only":
            raise ValueError("request is not declared view-only")

        responses = [row for row in self.source_map.by_kind("request_response")
                     if row.subject == subject and row.attrs.get("response_name") == "success"
                     and row.attrs.get("response_type") == "view"]
        response = _one(responses, "unambiguous success view response")
        view_subject = f"{webapp}/{response.object}"
        view_rows = [row for row in self.source_map.by_kind("view_screen")
                     if row.subject == view_subject]
        view = _one(view_rows, "view_screen")
        screen = view.object
        form_rows = [row for row in self.source_map.by_kind("screen_includes_form")
                     if row.subject == screen]
        if not form_rows:
            raise ValueError("screen has no cited form")
        form_subjects = sorted({row.object for row in form_rows})
        if len(form_subjects) != 1:
            raise ValueError(f"screen includes {len(form_subjects)} forms; manual selection required")
        form_subject = form_subjects[0]
        fields = [row for row in self.source_map.by_kind("form_field")
                  if row.subject == form_subject]
        visible_fields = [row for row in fields
                          if row.attrs.get("widget") not in ("hidden", "submit")]
        expected_text = tuple(dict.fromkeys(humanize(row.object) for row in visible_fields))
        if not expected_text:
            raise ValueError("form has no visible field facts")

        title = humanize(request)
        triggers = []
        for token in _TOKEN.findall(title.casefold()):
            token = _ALIASES.get(token, token)
            if token not in _GENERIC and len(token) >= 3 and token not in triggers:
                triggers.append(token)
        if not triggers:
            raise ValueError("request name supplies no useful trigger")
        slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
        used = [handler, response, view, *form_rows, *fields]
        skill = {
            "schema_version": 1,
            "skill_id": f"{webapp}.generated.{slug}",
            "title": title,
            "intent": f"Open the read-only {title} screen and stop before submitting its form.",
            "triggers": triggers,
            "current_webapp": webapp,
            "route_template": self.route_port.compose_contract_route(webapp, request),
            "parameters": [],
            "expected_text": list(expected_text),
            "provenance": list(_refs(used)),
            "read_only": True,
        }
        counts = {
            "request_handler": len(handlers), "request_response": len(responses),
            "view_screen": len(view_rows), "screen_includes_form": len(form_rows),
            "form_field": len(fields), "visible_form_field": len(visible_fields),
        }
        return SourceSkillDraft(
            skill=skill, source_digest=self.source_map.content_digest,
            relation_counts=counts,
            source_chain=(subject, response.object, screen, form_subject),
        )


class SourceEvidenceProvider:
    """Bounded search and slice reads over evidence already admitted to the SourceMap."""

    def __init__(self, source_map: SourceMap, source_root: str | Path | None = None, *,
                 route_port: ApplicationRoutePort):
        self.source_map = source_map
        self._evidence = source_map.evidence_index()
        self._search = SourceSkillBootstrapper(source_map, route_port=route_port)
        self.source_root = Path(source_root).resolve() if source_root is not None else None

    def search_source(self, query: str, webapp: str, limit: int = 5) -> tuple[dict, ...]:
        return self._search.search(query, webapp, limit)

    def read_source_slice(self, citation: str, max_chars: int = 4000) -> dict:
        if (not isinstance(max_chars, int) or isinstance(max_chars, bool)
                or not 100 <= max_chars <= 8000):
            raise ValueError("max_chars must be from 100 to 8000")
        if not isinstance(citation, str) or citation not in self._evidence:
            raise ValueError("citation is not in this version-bound SourceMap")
        evidence = self._evidence[citation]
        excerpt = evidence.excerpt
        truncated = False
        if self.source_root is not None:
            path = (self.source_root / evidence.path).resolve()
            if self.source_root not in path.parents or not path.is_file():
                raise ValueError("compiled citation is outside the configured source root")
            if path.stat().st_size > 5_000_000:
                raise ValueError("source file exceeds the bounded slice-reader limit")
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if evidence.end_line > len(lines):
                raise ValueError("compiled citation is outside the current source file")
            body = "".join(lines[evidence.start_line - 1:evidence.end_line])
            if hashlib.sha256(body.encode()).hexdigest() != evidence.sha256:
                raise ValueError("source slice no longer matches its version-bound hash")
            excerpt = body
        if not excerpt:
            raise ValueError("the compiled citation has no bounded excerpt or source reader")
        truncated = len(excerpt) > max_chars
        return {
            "source_digest": self.source_map.content_digest,
            "citation": evidence.key,
            "sha256": evidence.sha256,
            "excerpt": excerpt[:max_chars],
            "truncated": truncated,
        }


# ---------------------------------------------------------------- Business Skill drafts


@dataclass(frozen=True)
class CitationCheck:
    """One `provenance` entry and what the compiled source had to say about it."""

    citation: str
    kind: str
    resolved: bool
    reason: str


@dataclass(frozen=True)
class SkillVerification:
    """Whether a Skill's claims still point at something in this release's source."""

    skill_id: str
    source_digest: str
    release: str
    checks: tuple[CitationCheck, ...]

    @property
    def source_citations(self) -> tuple[CitationCheck, ...]:
        return tuple(check for check in self.checks if check.kind == "src")

    @property
    def passed(self) -> bool:
        """True when every *source* citation resolves.

        An ``api`` citation names the application's published interface, which no source map
        can confirm; reporting it as a failure would make the gate meaningless, and reporting
        it as a pass would claim a check nobody ran. It is carried as unchecked.
        """
        return all(check.resolved for check in self.source_citations)

    def to_json(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "source_digest": self.source_digest,
            "release": self.release,
            "passed": self.passed,
            "checks": [
                {"citation": check.citation, "kind": check.kind,
                 "resolved": check.resolved, "reason": check.reason}
                for check in self.checks],
        }


@dataclass(frozen=True)
class BusinessSkillDraft:
    """A Skill artifact a person still has to read, plus what the source actually said.

    The division is deliberate. Titles, triggers, routes and citations are facts a compiler
    can take from the application's own descriptors, and taking them by hand is how a Skill
    comes to cite a file that moved two releases ago. Instructions are not facts: they are a
    playbook for a model, and a compiler that wrote them would be inventing the one part
    that needs judgement. So `facts` is what the source declared and `review` is what it
    could not supply -- a draft is finished by a person reading both.
    """

    skill: dict
    source_digest: str
    release: str
    facts: tuple[str, ...]
    review: tuple[str, ...]

    def to_json(self) -> dict:
        return {"skill": self.skill, "source_digest": self.source_digest,
                "release": self.release, "facts": list(self.facts),
                "review": list(self.review)}


@dataclass(frozen=True)
class FieldChange:
    """One field a reissue would change, with both sides kept for a person to read."""

    field: str
    before: tuple[str, ...] | str | bool
    after: tuple[str, ...] | str | bool

    def render(self) -> str:
        def show(value) -> str:
            if isinstance(value, tuple):
                return "[" + ", ".join(repr(item) for item in value) + "]"
            return repr(value)
        return f"  {self.field}:\n    - {show(self.before)}\n    + {show(self.after)}"


@dataclass(frozen=True)
class SkillReissue:
    """An existing Skill recompiled against a newer SourceMap, as a diff.

    The split is the same one `BusinessSkillDraft` makes, applied to an artifact that
    already exists. Titles, triggers and citations are facts the compiler takes from the
    application's own descriptors, so a reissue recomputes them. Instructions are a playbook
    a person wrote; recomputing them would throw away the only part that needed judgement,
    so they cross over byte for byte -- as do the intent, the tool names, and any citation
    the source cannot speak for (an `api:` entry names a published interface, and inventing
    one here would be a claim no map can support).

    It is a diff rather than a new file because the facts moving is exactly the moment a
    person should look: a title that changed because the deployment renamed an action is
    routine, and a scope filter that appeared in `facts` may mean the instructions are now
    wrong. Nothing here can tell those apart, so nothing here decides.
    """

    skill_id: str
    artifact_uri: str
    skill: dict
    source_digest: str
    release: str
    changed: tuple[FieldChange, ...]
    recomputed: tuple[str, ...]
    preserved: tuple[str, ...]
    facts: tuple[str, ...]
    review: tuple[str, ...]

    @property
    def unchanged(self) -> bool:
        return not self.changed

    def render(self) -> str:
        """The diff a reviewer reads before any file is written."""
        lines = [f"{self.skill_id} <- {self.release} ({self.source_digest[:12]})"]
        if self.unchanged:
            lines.append("  no recomputed field changed; the artifact would be unchanged")
        else:
            lines.extend(change.render() for change in self.changed)
        lines.append("  recomputed: " + ", ".join(self.recomputed))
        lines.append("  kept verbatim: " + ", ".join(self.preserved))
        if self.facts:
            lines.append("  the source now declares:")
            lines.extend(f"    - {fact}" for fact in self.facts)
        if self.review:
            lines.append("  a person still has to judge:")
            lines.extend(f"    - {item}" for item in self.review)
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "artifact_uri": self.artifact_uri,
            "skill": self.skill,
            "source_digest": self.source_digest,
            "release": self.release,
            "unchanged": self.unchanged,
            "recomputed": list(self.recomputed),
            "changed": [{"field": change.field,
                         "before": list(change.before)
                         if isinstance(change.before, tuple) else change.before,
                         "after": list(change.after)
                         if isinstance(change.after, tuple) else change.after}
                        for change in self.changed],
            "preserved": list(self.preserved),
            "facts": list(self.facts),
            "review": list(self.review),
        }


class BusinessSkillCompiler:
    """Draft and check typed-tool Skills against one release's compiled descriptors.

    Works over the relation vocabulary every `SourceCompiler` emits -- `request_handler`,
    `menu_link`, `view_screen`, `service_entity`, `service_parameter` -- so it is not tied
    to the application whose compiler produced them.
    """

    def __init__(self, source_map: SourceMap, *, application: str,
                 route_port: ApplicationRoutePort):
        if not isinstance(route_port, ApplicationRoutePort):
            raise TypeError("route_port must implement ApplicationRoutePort")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,39}", application or ""):
            raise ValueError("application must be a short lowercase identifier")
        self.source_map = source_map
        self.application = application
        self.route_port = route_port
        self._paths: dict[str, list[SourceRelation]] = {}
        for relation in source_map.relations:
            for reference in relation.evidence:
                self._paths.setdefault(reference.path, []).append(relation)

    # -- verification -------------------------------------------------------------------
    def verify(self, skill) -> SkillVerification:
        checks = []
        for entry in skill.provenance:
            citation = Citation.parse(entry)
            if citation.kind != "src":
                checks.append(CitationCheck(
                    citation=entry, kind=citation.kind, resolved=True,
                    reason="an interface citation; no source map can confirm it"))
                continue
            checks.append(CitationCheck(
                citation=entry, kind="src", **self._resolve(citation)))
        return SkillVerification(
            skill_id=skill.skill_id, source_digest=self.source_map.content_digest,
            release=self.source_map.release, checks=tuple(checks))

    def _resolve(self, citation: Citation) -> dict:
        if citation.application != self.application:
            return {"resolved": False,
                    "reason": f"cites {citation.application}, not {self.application}"}
        # A Skill is written against a release *line*; the map records the exact build it
        # was compiled from. Requiring equality would force every artifact to be reissued
        # for every deployment image, which is not what a citation is for.
        if not self.source_map.release.startswith(citation.release):
            return {"resolved": False,
                    "reason": (f"cites release {citation.release}, which this map's "
                               f"{self.source_map.release} is not a build of")}
        relations = self._paths.get(citation.locator)
        if not relations:
            return {"resolved": False,
                    "reason": "no relation in this map cites that path"}
        if not citation.anchor:
            return {"resolved": True, "reason": f"{len(relations)} relations cite that path"}
        hits = sum(1 for relation in relations if self._mentions(relation, citation.anchor))
        if not hits:
            return {"resolved": False,
                    "reason": f"the path is cited but nothing in it declares "
                              f"{citation.anchor!r}"}
        return {"resolved": True, "reason": f"{hits} relations declare that anchor"}

    @staticmethod
    def _mentions(relation: SourceRelation, anchor: str) -> bool:
        if anchor in (relation.subject, relation.object):
            return True
        if any(anchor == str(value) for value in relation.attrs.values()):
            return True
        # A model method is `<model>.<method>`; the compiler records it as the subject of a
        # `service_parameter`, and a field as `<model>` plus the field name.
        return relation.subject.startswith(anchor + ".") or anchor.startswith(
            relation.subject + ".")

    # -- drafting -----------------------------------------------------------------------
    def draft(self, *, skill_id: str, subject: str, tool_names: tuple[str, ...],
              read_only: bool, methods: tuple[str, ...] = ()) -> BusinessSkillDraft:
        """Compile one declared request, its menu, its views and its model into a draft.

        Keyed on the relation `subject`, which every compiler spells the same way, not on
        an attribute one of them happens to carry: `action_id` is Odoo's word for this and
        a core module that keyed on it would work for exactly one application.
        """
        handlers = [row for row in self.source_map.by_kind("request_handler")
                    if row.subject == subject]
        action = _one(handlers, f"declared request {subject}")
        # What a handler's `object` slot holds is where the two compilers stop agreeing.
        # Odoo puts the model an action opens there; OFBiz puts the service a request
        # invokes, and leaves it empty for a view-only one. So the model is taken only when
        # the map itself describes it as an entity, and a draft without one keeps every
        # fact that does not depend on it rather than refusing to compile.
        entities = {row.subject for row in self.source_map.by_kind("service_entity")}
        model = action.object if action.object in entities else ""
        menus = self._menus_for(subject)
        views = [row for row in self.source_map.by_kind("request_response")
                 if row.subject == subject]
        fields = [row for row in self.source_map.by_kind("service_entity")
                  if model and row.subject == model]
        method_rows = [row for row in self.source_map.by_kind("service_parameter")
                       if row.subject in methods]

        title = str(action.attrs.get("name") or "").strip() or humanize(subject)
        route = self._route(action, menus)
        facts, review = self._facts(action, menus, views, fields, method_rows,
                                    model=model, route=route, methods=methods)
        skill = {
            "schema_version": 1,
            "skill_id": skill_id,
            "title": title,
            "intent": f"Work with {title} through the typed tools, on current data.",
            "triggers": list(self._triggers(title, menus, model)),
            "instructions": [
                "DRAFT: a person must replace these with a playbook for this task.",
                *(f"Declared by the source: {fact}" for fact in facts),
            ],
            "tool_names": list(tool_names),
            "provenance": list(self._citations(action, menus, views, fields, method_rows)),
            "read_only": bool(read_only),
        }
        return BusinessSkillDraft(skill=skill, source_digest=self.source_map.content_digest,
                                  release=self.source_map.release, facts=facts,
                                  review=review)

    # -- reissue ------------------------------------------------------------------------
    #: Always taken from the existing artifact. Two are judgement (`intent`, `instructions`),
    #: two are the adapter's (`tool_names`, `read_only`), and `skill_id` is the artifact's
    #: identity -- a reissue that changed it would be a different Skill.
    PRESERVED_FIELDS = ("skill_id", "intent", "instructions", "tool_names", "read_only")

    #: The fields a compiler can derive from descriptors, and therefore the ones a reissue
    #: can recompute. `provenance` is pure compiler output. `title` and `triggers` are not:
    #: they *start* as compiler output and a person then edits them -- the eight Odoo Skills
    #: carry hand-written titles and bilingual trigger lists whose Chinese aliases exist
    #: nowhere in any descriptor. The artifact format records only the final value, not
    #: whether anyone edited it, so no rule here can tell the two apart; the caller chooses,
    #: and the diff shows exactly what each choice costs.
    RECOMPUTABLE_FIELDS = ("title", "triggers", "provenance")

    def reissue(self, skill, *, subject: str, methods: tuple[str, ...] = (),
                recompute: tuple[str, ...] = RECOMPUTABLE_FIELDS) -> SkillReissue:
        """Recompile one existing Skill's facts against this map, keeping what a person wrote.

        `subject` and `methods` are the drafting inputs, and they are supplied rather than
        recovered from the artifact on purpose: what a reissue exists to repair is a citation
        that no longer resolves, and deriving "which request was this Skill about" from those
        same citations would mean asking the broken thing where to look. The distribution
        that ships the Skill declares them; naming one this map does not contain fails here
        rather than producing a plausible artifact about the wrong request.
        """
        unknown = sorted(set(recompute) - set(self.RECOMPUTABLE_FIELDS))
        if unknown:
            raise ValueError(f"a compiler cannot recompute {unknown}")
        draft = self.draft(skill_id=skill.skill_id, subject=subject,
                           tool_names=tuple(skill.tool_names),
                           read_only=bool(skill.read_only), methods=methods)
        # Source citations are recomputed; everything else the artifact carried stays, in the
        # order it was in. An `api:` citation names a published interface no source map can
        # confirm -- `verify` already reports it as unchecked -- so a reissue neither drops
        # it nor pretends to have re-derived it.
        recomputed = [entry for entry in draft.skill["provenance"]
                      if Citation.parse(entry).kind == "src"]
        carried = [entry for entry in skill.provenance
                   if Citation.parse(entry).kind != "src"]
        compiled = {
            "title": draft.skill["title"],
            "triggers": list(draft.skill["triggers"]),
            "provenance": recomputed + carried,
        }
        existing = {"title": skill.title, "triggers": list(skill.triggers),
                    "provenance": list(skill.provenance)}
        taken = {name: (compiled[name] if name in recompute else existing[name])
                 for name in self.RECOMPUTABLE_FIELDS}
        reissued = {
            "schema_version": 1,
            "skill_id": skill.skill_id,
            "title": taken["title"],
            "intent": skill.intent,
            "triggers": taken["triggers"],
            "instructions": list(skill.instructions),
            "tool_names": list(skill.tool_names),
            "provenance": taken["provenance"],
            "read_only": bool(skill.read_only),
        }
        # A reissue that emits something `BusinessSkill.load` would reject is worse than one
        # that fails: it hands a person a diff to approve and a file that cannot be shipped.
        raw = json.dumps(reissued, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        loaded = BusinessSkill.from_bytes(raw.encode("utf-8"),
                                          artifact_uri="memory://reissue")
        if loaded.instructions != tuple(skill.instructions):
            raise ValueError("a reissue must not rewrite the instructions")

        changed = []
        for field, before, after in (
                ("title", skill.title, reissued["title"]),
                ("triggers", tuple(skill.triggers), tuple(reissued["triggers"])),
                ("provenance", tuple(skill.provenance), tuple(reissued["provenance"]))):
            if before != after:
                changed.append(FieldChange(field=field, before=before, after=after))
        return SkillReissue(
            skill_id=skill.skill_id, artifact_uri=skill.artifact_uri, skill=reissued,
            source_digest=self.source_map.content_digest, release=self.source_map.release,
            changed=tuple(changed), recomputed=tuple(sorted(recompute)),
            preserved=self.PRESERVED_FIELDS + tuple(
                name for name in self.RECOMPUTABLE_FIELDS if name not in recompute),
            facts=draft.facts, review=draft.review)

    def _menus_for(self, subject: str) -> list[SourceRelation]:
        """Menus point at a request by its subject, or by its route within the scope.

        Both spellings occur in practice -- Odoo's menu targets the whole identity
        (`odoo/orders`), OFBiz's targets the bare request (`findTrays`) -- and neither is
        wrong, so the link accepts either rather than picking one application's habit.
        """
        route = subject.split("/", 1)[-1]
        return [row for row in self.source_map.by_kind("menu_link")
                if row.object in (subject, route)]

    def _route(self, action: SourceRelation, menus: list[SourceRelation]) -> str:
        """Compose the subject's `(scope, route)` and let the port say whether it is one.

        A compiler records a subject for every declared request, including ones that are
        reachable only from inside the application and have no address of their own. The
        port's own tool-route contract is the only thing that can tell them apart, so the
        composed path is offered to it rather than pattern-matched here.
        """
        for candidate in [action.subject] + [str(menu.object) for menu in menus]:
            scope, _, route = candidate.partition("/")
            if not scope or not route:
                continue
            composed = self.route_port.compose_contract_route(scope, route)
            try:
                self.route_port.validate_tool_route(composed)
            except ValueError:
                continue
            return composed
        return ""

    def _triggers(self, title: str, menus: list[SourceRelation], model: str) -> list[str]:
        words: list[str] = []
        sources = [title, model.replace(".", " ") if model else ""]
        sources += [str(menu.attrs.get("title") or "").replace("/", " ") for menu in menus]
        for text in sources:
            for token in _TOKEN.findall(text.casefold()):
                token = _ALIASES.get(token, token)
                if len(token) >= 3 and token not in _GENERIC and token not in words:
                    words.append(token)
        return words[:12]

    def _facts(self, action, menus, views, fields, method_rows, *, model, route,
               methods) -> tuple[tuple[str, ...], tuple[str, ...]]:
        facts = [f"the request opens {model}"] if model else []
        domain = str(action.attrs.get("domain") or "").strip()
        if domain and domain != "[]":
            # This is the one the prose usually gets wrong: a list page that looks like
            # "all orders" is a filtered scope, and the filter is only in the descriptor.
            facts.append(f"the scope is filtered by domain {domain}")
        else:
            facts.append("the action declares no domain filter")
        if route:
            facts.append(f"its collection route is {route}")
        if menus:
            facts.append("it is reached from " + "; ".join(
                sorted(str(menu.attrs.get("title") or "") for menu in menus)))
        view_modes = str(action.attrs.get("view_mode") or "")
        if view_modes:
            facts.append(f"it offers the views {view_modes}")
        required = sorted(row.object for row in fields
                          if str(row.attrs.get("required") or "").strip() == "True")
        if required:
            facts.append("its model declares these fields required: " + ", ".join(required))
        for row in method_rows:
            # A method may be declared in several modules; which module carries the
            # docstring is the difference between a useful fact and a bare name.
            doc = " ".join(str(row.attrs.get("doc") or "").split())
            module = str(row.attrs.get("module") or "")
            facts.append(f"{row.subject} is declared in {module}"
                         + (f": {doc[:300]}" if doc else " with no docstring"))

        review = [
            "instructions: a compiler cannot write the playbook; this draft only lists "
            "what the source declared",
            "tool_names: the typed tool surface comes from the adapter, not the source",
        ]
        missing = [name for name in methods
                   if not any(row.subject == name for row in method_rows)]
        if missing:
            review.append("these methods are not in the compiled map: " + ", ".join(missing))
        if not model:
            review.append("this map does not describe the record behind the request; "
                          "required fields and model methods are unavailable")
        if not views:
            review.append("the request declares no view; check the route by hand")
        if not route:
            review.append("the request declares no route; a Skill that opens a page needs "
                          "one supplied by hand")
        return tuple(facts), tuple(review)

    @staticmethod
    def _anchor(relation: SourceRelation) -> str:
        """The identifier a reader would search the cited file for.

        Applications name their descriptors differently -- Odoo has globally qualified XML
        ids, OFBiz has request and menu-item names -- so this prefers whichever identity the
        compiler recorded and falls back to the relation's own subject.
        """
        for key in ("action_id", "menu_id", "view_id", "form_name", "screen_name"):
            value = str(relation.attrs.get(key) or "")
            if value:
                return value
        return relation.subject

    def _citations(self, action, menus, views, fields, method_rows) -> list[str]:
        # The release *line*, not the build: `18.0-20260908+image:sha256:...` is one build of
        # 18.0, and a Skill that cited the build would have to be reissued for every image.
        release = self.source_map.release.split("+", 1)[0].split("-", 1)[0]
        cited: list[tuple[SourceRelation, str]] = [(action, self._anchor(action))]
        cited += [(menu, self._anchor(menu)) for menu in menus]
        cited += [(view, str(view.object or "")) for view in views]
        if fields:
            # Several files may declare fields of one record -- Odoo's `account.move` is
            # extended by a dozen modules -- and citing whichever came first points a
            # reader at an incidental one. Cite the file that declares the most.
            by_file: dict[str, list[SourceRelation]] = {}
            for row in fields:
                for reference in row.evidence:
                    by_file.setdefault(reference.path, []).append(row)
            primary = max(sorted(by_file), key=lambda path: len(by_file[path]))
            cited.append((by_file[primary][0], fields[0].subject))
        cited += [(row, row.subject) for row in method_rows]

        entries: list[str] = []
        for relation, anchor in cited:
            for reference in relation.evidence:
                entry = Citation(kind="src", application=self.application, release=release,
                                 locator=reference.path, anchor=anchor).text
                if entry not in entries:
                    entries.append(entry)
        return entries
