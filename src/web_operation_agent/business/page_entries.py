"""Application-neutral discovery of actor-visible page entries.

An entry is metadata, not a navigation permit. Only an adapter-qualified route may enter
``allowed_routes``; the ordinary page policy still checks it again before dispatch.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from ..state.identity import OperationContext
from .entities import _score, _text, _tokens
from .errors import BusinessToolContractError, BusinessToolRequestError
from .tools.contracts import BusinessToolResult


FIND_PAGE_ENTRIES_TOOL = "find_page_entries"
FIND_PAGE_ENTRIES_SCHEMA = {
    "type": "function",
    "function": {
        "name": FIND_PAGE_ENTRIES_TOOL,
        "description": (
            "Find bounded page/action candidates in the signed-in actor's current application "
            "catalog. A candidate with ui_path may be opened through the ordinary page "
            "policy; one without ui_path is information only. Never invent a route or "
            "treat a context-dependent action as a standalone page. An empty actor catalog "
            "match does not prove the page does not exist for other users."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200,
                          "description": "Page or action name in the application's terms."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True, slots=True)
class PageEntry:
    """One adapter-discovered action; only ``route`` is navigation authority."""

    reference: str
    label: str
    search_text: str
    breadcrumbs: tuple[str, ...] = ()
    views: tuple[str, ...] = ()
    route: str = ""
    unavailable_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference", _text(
            self.reference, "page entry reference", 240))
        object.__setattr__(self, "label", _text(self.label, "page entry label", 200))
        object.__setattr__(self, "search_text", _text(
            self.search_text, "page entry search text", 2_000, empty=True))
        if (not isinstance(self.breadcrumbs, tuple) or len(self.breadcrumbs) > 20
                or not isinstance(self.views, tuple) or len(self.views) > 20):
            raise ValueError("page entry metadata must be bounded tuples")
        object.__setattr__(self, "breadcrumbs", tuple(
            _text(value, "page breadcrumb", 1_000) for value in self.breadcrumbs))
        object.__setattr__(self, "views", tuple(
            _text(value, "page view", 40) for value in self.views))
        if self.route:
            route = _text(self.route, "page route", 300)
            parts = urlsplit(route)
            if (not route.startswith("/") or route.startswith("//") or parts.query
                    or parts.fragment or parts.scheme or parts.netloc or "%" in route
                    or ".." in parts.path.split("/") or "\\" in route):
                raise ValueError("page entry route must be a plain relative path")
            object.__setattr__(self, "route", route)
        if self.unavailable_reason:
            object.__setattr__(self, "unavailable_reason", _text(
                self.unavailable_reason, "page entry unavailable reason", 80))
        if bool(self.route) == bool(self.unavailable_reason):
            raise ValueError("page entry needs either a qualified route or a reason")


@dataclass(frozen=True, slots=True)
class PageEntryCatalog:
    binding_digest: str
    entries: tuple[PageEntry, ...]
    complete: bool
    scope: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding_digest, str) or not re.fullmatch(
                r"(?:sha256:)?[0-9a-f]{64}", self.binding_digest):
            raise ValueError("page entry catalog digest is invalid")
        if (not isinstance(self.entries, tuple) or len(self.entries) > 5_000
                or any(not isinstance(item, PageEntry) for item in self.entries)
                or len({item.reference for item in self.entries}) != len(self.entries)):
            raise ValueError("page entry catalog is invalid or duplicated")
        if not isinstance(self.complete, bool):
            raise ValueError("page entry catalog completeness must be boolean")
        object.__setattr__(self, "scope", _text(self.scope, "page entry scope", 300))


@runtime_checkable
class PageEntryPort(Protocol):
    def catalog(self, *, context: OperationContext | None = None) -> PageEntryCatalog: ...


class PageEntryTools:
    def __init__(self, port: PageEntryPort, *, application_id: str):
        if not isinstance(port, PageEntryPort):
            raise TypeError("page entry tools require a catalog port")
        self._port = port
        self._application_id = _text(application_id, "application ID", 100)

    def execute(self, args: dict, *, context: OperationContext | None = None
                ) -> BusinessToolResult:
        if not isinstance(args, dict) or set(args) - {"query", "limit"}:
            raise BusinessToolRequestError("find_page_entries received unknown arguments")
        try:
            query = _text(args.get("query"), "query", 200)
        except ValueError as exc:
            raise BusinessToolRequestError(str(exc)) from None
        limit = args.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
            raise BusinessToolRequestError("limit must be from 1 to 5")
        try:
            catalog = self._port.catalog(context=context)
        except BusinessToolContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise BusinessToolContractError(
                "the application returned an invalid page entry catalog") from exc
        if not isinstance(catalog, PageEntryCatalog):
            raise BusinessToolContractError(
                "the application returned an invalid page entry catalog")
        binding = catalog.binding_digest
        if context is not None:
            binding += "\0" + context.principal.actor_authority_digest
        ranked = []
        wanted = set(_tokens(query))
        for entry in catalog.entries:
            searchable = " ".join((entry.label, entry.search_text, *entry.breadcrumbs))
            overlap = len(wanted & set(_tokens(searchable)))
            # A lone stopword in a multi-term query must not become a page candidate.
            if len(wanted) > 1 and (overlap < 2 or overlap * 3 < len(wanted) * 2):
                continue
            score = _score(query, searchable)
            if entry.label.casefold() == query.casefold():
                score += 50
            if score > 0:
                entry_id = "page_" + hashlib.sha256(
                    f"{binding}\0{entry.reference}".encode("utf-8")).hexdigest()[:32]
                ranked.append((score, entry_id, entry))
        ranked.sort(key=lambda item: (-item[0], -int(bool(item[2].route)),
                                      item[2].label.casefold(), item[1]))
        visible = ranked[:limit]
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "query": query,
                "candidates": [{
                    "entry_id": entry_id,
                    "label": entry.label,
                    "breadcrumbs": list(entry.breadcrumbs),
                    "views": list(entry.views),
                    "openable": bool(entry.route),
                    **({"ui_path": entry.route} if entry.route else {
                        "unavailable_reason": entry.unavailable_reason}),
                } for _score_value, entry_id, entry in visible],
                "matched_count": len(ranked),
                "returned_count": len(visible),
                "catalog_complete": catalog.complete,
                "scope": catalog.scope,
                "observed_at": observed_at,
            },
            audit={
                "application": self._application_id,
                "operation": FIND_PAGE_ENTRIES_TOOL,
                "catalog_digest": catalog.binding_digest,
                "matched_count": len(ranked),
                "returned_count": len(visible),
                "catalog_complete": catalog.complete,
                "observed_at": observed_at,
                "identity_binding": "principal_bound" if context else "unverified",
                **({"identity": context.audit_projection()} if context else {}),
            },
            allowed_routes=tuple(dict.fromkeys(
                entry.route for _score_value, _entry_id, entry in visible
                if entry.route)),
        )


__all__ = ["FIND_PAGE_ENTRIES_SCHEMA", "FIND_PAGE_ENTRIES_TOOL", "PageEntry",
           "PageEntryCatalog", "PageEntryPort", "PageEntryTools"]
