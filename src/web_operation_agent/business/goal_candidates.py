"""One bounded, read-only candidate lookup for a declared user goal.

This combines existing actor-scoped metadata tools; it does not select a candidate,
execute a business query, or verify that the declaration understood the user's words.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from ..state.identity import OperationContext
from .entities import (DESCRIBE_ENTITY_TOOL, DISCOVER_ENTITIES_TOOL,
                       EntityReadTools, ReferenceLookupTools, _request_text)
from .errors import BusinessToolRequestError
from .page_entries import PageEntryTools
from .tools.contracts import BusinessToolResult


RESOLVE_GOAL_CANDIDATES_TOOL = "resolve_goal_candidates"
_GOAL_ENTRY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "delivery": {"type": "string", "enum": ["answer", "page", "handoff"]},
        "subject_query": {"type": "string", "minLength": 1, "maxLength": 120},
        "reference": {"type": "string", "minLength": 1, "maxLength": 120},
        "detail_query": {"type": "string", "minLength": 1, "maxLength": 200},
    },
    "required": ["delivery", "subject_query"],
}
RESOLVE_GOAL_CANDIDATES_SCHEMA = {"type": "function", "function": {
    "name": RESOLVE_GOAL_CANDIDATES_TOOL,
    "description": (
        "Use this read-only tool to find candidates for answer, page or handoff "
        "deliverables in one bounded metadata lookup. Include each requested kind. "
        "Translate the subject and field/tab into the application's displayed terms, "
        "but copy an exact record name or number from the user. Results are candidates, "
        "not a complete goal declaration, chosen target, proof of absence, "
        "permission to navigate, or an answer. "
        "Check ambiguous entities and fresh page/data state before finishing."),
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {"goals": {"type": "array", "minItems": 1, "maxItems": 3,
                                 "items": _GOAL_ENTRY_SCHEMA}},
        "required": ["goals"],
    },
}}


@dataclass(frozen=True, slots=True)
class GoalCandidateRequest:
    delivery: str
    subject_query: str
    reference: str = ""
    detail_query: str = ""


def _requests(args: object) -> tuple[GoalCandidateRequest, ...]:
    if not isinstance(args, dict) or set(args) != {"goals"}:
        raise BusinessToolRequestError("resolve_goal_candidates requires only goals")
    raw = args["goals"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
        raise BusinessToolRequestError("goals must contain one to three deliverables")
    requests = []
    for entry in raw:
        if (not isinstance(entry, dict)
                or set(entry) - {"delivery", "subject_query", "reference", "detail_query"}
                or not {"delivery", "subject_query"} <= set(entry)
                or not isinstance(entry["delivery"], str)
                or entry["delivery"] not in {"answer", "page", "handoff"}):
            raise BusinessToolRequestError("a goal needs delivery and subject_query")
        if ("reference" in entry and not str(entry["reference"]).strip()
                or "detail_query" in entry and not str(entry["detail_query"]).strip()):
            raise BusinessToolRequestError("provided reference and detail_query must be nonempty")
        requests.append(GoalCandidateRequest(
            delivery=entry["delivery"],
            subject_query=_request_text(entry["subject_query"], "subject_query", 120),
            reference=_request_text(entry.get("reference", ""), "reference", 120,
                                    empty=True),
            detail_query=_request_text(entry.get("detail_query", ""), "detail_query", 200,
                                       empty=True)))
    return tuple(requests)


class GoalCandidateTools:
    """Compose existing metadata results without creating new application authority."""

    def __init__(self, entity_reads: EntityReadTools, references: ReferenceLookupTools,
                 pages: PageEntryTools, *, application_id: str):
        self._entities = entity_reads
        self._references = references
        self._pages = pages
        self._application_id = application_id

    def execute(self, args: dict, *, context: OperationContext | None = None
                ) -> BusinessToolResult:
        requests = _requests(args)
        results = []
        allowed_routes = []
        for request in requests:
            row = {"delivery": request.delivery,
                   "subject_query": request.subject_query,
                   **({"reference": request.reference} if request.reference else {}),
                   **({"detail_query": request.detail_query} if request.detail_query else {})}
            if request.delivery in {"answer", "page"}:
                discovery = self._entities.execute(
                    DISCOVER_ENTITIES_TOOL, {"query": request.subject_query, "limit": 3},
                    context=context)
                row["entity_candidates"] = [{
                    "entity_id": item["entity_id"], "label": item["label"],
                    "page_hints": [hint["name"] for hint in item.get("page_hints", ())[:2]],
                } for item in discovery.content["candidates"]]
                row["entity_catalog_complete"] = discovery.content["catalog_complete"]
                row["entity_candidates_truncated"] = (
                    discovery.content["matched_count"] > len(row["entity_candidates"]))
                if request.reference:
                    records = self._references.execute({
                        "entity_query": request.subject_query,
                        "reference": request.reference, "limit": 3}, context=context)
                    row["record_candidates"] = records.content["candidates"]
                    row["record_lookup_incomplete"] = records.content[
                        "truncated_or_incomplete"]
                    allowed_routes.extend(records.allowed_routes)
                if request.delivery == "answer" and request.detail_query:
                    row["field_candidates"] = []
                    for entity in discovery.content["candidates"][:2]:
                        detail = self._entities.execute(DESCRIBE_ENTITY_TOOL, {
                            "entity_id": entity["entity_id"],
                            "field_query": request.detail_query, "limit": 3},
                            context=context)
                        row["field_candidates"].append({
                            "entity_id": entity["entity_id"],
                            "fields": [{"field_id": field["field_id"],
                                        "label": field["label"],
                                        "type": field["type"]}
                                       for field in detail.content["fields"]],
                            "fields_truncated": detail.content["fields_truncated"]})
            if request.delivery in {"page", "handoff"}:
                page = self._pages.execute({
                    "query": request.subject_query,
                    **({"detail_query": request.detail_query}
                       if request.detail_query else {}),
                    "limit": 3}, context=context)
                row["page_candidates"] = [{
                    "entry_id": item["entry_id"], "label": item["label"],
                    "openable": item["openable"],
                    **({"ui_path": item["ui_path"]} if "ui_path" in item else {
                        "unavailable_reason": item["unavailable_reason"]}),
                    **({"part_matches": item.get("part_matches", [])}
                       if request.detail_query else {}),
                    **({"part_metadata_checked": item.get("part_metadata_checked", False)}
                       if request.detail_query else {}),
                } for item in page.content["candidates"]]
                row["page_catalog_complete"] = page.content["catalog_complete"]
                row["page_candidates_truncated"] = (
                    page.content["matched_count"] > len(row["page_candidates"]))
                allowed_routes.extend(page.allowed_routes)
            results.append(row)
        digest = hashlib.sha256(json.dumps(
            [asdict(request) for request in requests], sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest()
        return BusinessToolResult(
            content={"goals": results,
                     "candidate_warning": "Unmatched or ambiguous metadata does not prove "
                     "the requested object is absent; candidates grant no write authority."},
            audit={"application": self._application_id,
                   "operation": RESOLVE_GOAL_CANDIDATES_TOOL,
                   "goal_digest": digest, "goal_count": len(results),
                   "entity_candidate_count": sum(len(row.get("entity_candidates", ()))
                                                 for row in results),
                   "page_candidate_count": sum(len(row.get("page_candidates", ()))
                                               for row in results),
                   "record_candidate_count": sum(len(row.get("record_candidates", ()))
                                                 for row in results)},
            allowed_routes=tuple(dict.fromkeys(allowed_routes)),
            data_classes=("application.metadata", "application.route",
                          "business.identifier"))


__all__ = ["GoalCandidateRequest", "GoalCandidateTools",
           "RESOLVE_GOAL_CANDIDATES_SCHEMA", "RESOLVE_GOAL_CANDIDATES_TOOL"]
