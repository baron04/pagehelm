"""Compile a reviewable App Skill draft from a version-bound SourceMap.

This is an onboarding helper, not a runtime planner.  It follows only explicit descriptor
relations and refuses ambiguous request/view/screen chains instead of guessing.
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .schemas import SourceMap, SourceRelation


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

    def __init__(self, source_map: SourceMap):
        self.source_map = source_map

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
            "route_template": f"/{webapp}/control/{request}",
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

    def __init__(self, source_map: SourceMap, source_root: str | Path | None = None):
        self.source_map = source_map
        self._evidence = source_map.evidence_index()
        self._search = SourceSkillBootstrapper(source_map)
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
