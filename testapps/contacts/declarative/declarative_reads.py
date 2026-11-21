"""Actor-bound entity, page-entry, and collection read executor for declarative apps."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable, Mapping

from web_operation_agent.answer.evidence import Origin, QueryEvidence, RecordEvidence
from web_operation_agent.business.entities import (
    ENTITY_READ_SCHEMAS,
    ENTITY_READ_TOOLS,
    EntityCatalog,
    EntityCell,
    EntityDescriptor,
    EntityField,
    EntityPageHint,
    EntityQuery,
    EntityQueryPage,
    EntityReadTools,
    EntityRecord,
    ReferenceLookupTools,
    ReferenceMatch,
    RESOLVE_RECORD_REFERENCE_SCHEMA,
    RESOLVE_RECORD_REFERENCE_TOOL,
)
from web_operation_agent.business.errors import (
    BusinessToolContractError,
    BusinessToolError,
    BusinessToolPermissionError,
)
from web_operation_agent.business.goal_candidates import (
    GoalCandidateTools,
    RESOLVE_GOAL_CANDIDATES_SCHEMA,
    RESOLVE_GOAL_CANDIDATES_TOOL,
)
from web_operation_agent.business.page_entries import (
    FIND_PAGE_ENTRIES_SCHEMA,
    FIND_PAGE_ENTRIES_TOOL,
    PageEntry,
    PageEntryCatalog,
    PageEntryTools,
    PagePart,
)
from web_operation_agent.business.runtime import BusinessToolResult
from web_operation_agent.execution.events import observed_interval
from web_operation_agent.state.identity import OperationContext
from .declarative_client import DeclarativeRestClient
from .declarative_writes import DeclarativeTypedWriteHandler, TypedWriteSpec
from .openapi import EntitySpec, PageSpec


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:300]
    return str(value)[:300]


def _evidence_value(value: Any) -> tuple[str | None, str]:
    if value is None or value == "":
        return None, "text"
    if isinstance(value, bool):
        return ("true" if value else "false"), "boolean"
    if isinstance(value, (int, float)):
        return str(value), "number"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:500], "text"
    return str(value)[:500], "text"


class DeclarativeEntityReadPort:
    """Declarative ``EntityReadPort`` + ``ReferenceLookupPort`` over ``EntitySpec``s."""

    def __init__(self, executor: Any, entities: tuple[EntitySpec, ...], *, application_id: str):
        self._executor, self._entities = executor, tuple(entities)
        self._by_name = {e.name: e for e in self._entities}
        self._application_id = application_id

    @staticmethod
    def _actor_binding(context) -> str:
        principal = getattr(context, "principal", None)
        return principal.actor_authority_digest if principal is not None else "unverified-synthetic-actor"

    def catalog(self, *, context=None) -> EntityCatalog:
        binding = hashlib.sha256((f"{self._application_id}-entity-v1\0" + self._actor_binding(context)).encode("utf-8")).hexdigest()
        descriptors = tuple(
            EntityDescriptor(
                reference=e.name, label=e.label, search_text=e.search_text,
                page_hints=(EntityPageHint(name=e.page_title or e.label, breadcrumbs=(e.label,), views=e.page_views),),
            )
            for e in self._entities
        )
        return EntityCatalog(binding_digest=binding, entities=descriptors, complete=True, scope="current actor's readable entities")

    def fields(self, entity: EntityDescriptor, *, context=None) -> tuple[EntityField, ...]:
        spec = self._by_name.get(entity.reference)
        if spec is None:
            raise BusinessToolError("the requested entity is no longer available")
        return tuple(
            EntityField(reference=f.name, label=f.label, kind=f.kind, searchable=f.searchable, summable=f.summable, fixed_unit=f.fixed_unit)
            for f in spec.fields if f.in_entity
        )

    def _rows(self, spec: EntitySpec) -> list[dict]:
        if hasattr(self._executor, "fetch_rows"):
            return self._executor.fetch_rows(spec.name)
        if spec.row_fetcher is not None:
            return spec.row_fetcher(self._executor)
        raise BusinessToolError("no row fetcher configured")

    def lookup(self, entity: EntityDescriptor, reference: str, *, limit: int, context=None) -> tuple[tuple[ReferenceMatch, ...], bool]:
        spec = self._by_name.get(entity.reference)
        if spec is None:
            raise BusinessToolError("the requested entity is no longer available")
        source, nk, ik = self._rows(spec), spec.name_field, spec.id_field
        rows = [row for row in source if str(row.get(nk, "")).casefold() == reference.casefold()]
        if not rows:
            rows = [row for row in source if reference.casefold() in str(row.get(nk, "")).casefold()]
        rows.sort(key=lambda row: (str(row.get(nk, "")).casefold() != reference.casefold(), str(row[ik])))
        return (
            tuple(
                ReferenceMatch(
                    record_id=str(row[ik]), label=str(row.get(nk, row[ik])),
                    route=spec.detail_route_template.format(id=row[ik]) if spec.detail_route_template else "",
                )
                for row in rows[:limit]
            ),
            len(rows) > limit,
        )

    @staticmethod
    def _matches(row: dict, condition) -> bool:
        actual, expected, op = row.get(condition.field.reference), condition.value, condition.operator
        if op in {"=", "!="}:
            eq = (type(actual) is type(expected) and actual == expected) if (isinstance(actual, bool) or isinstance(expected, bool)) else (actual == expected)
            return eq if op == "=" else not eq
        if op == "contains":
            return actual is not None and str(expected).casefold() in str(actual).casefold()
        if op in {"in", "not_in"}:
            hit = any((type(actual) is type(x) and actual == x) if (isinstance(actual, bool) or isinstance(x, bool)) else (actual == x) for x in expected)
            return hit if op == "in" else not hit
        if op in {">", ">=", "<", "<="} and actual is not None and not isinstance(actual, bool) and not isinstance(expected, bool):
            return {">": actual > expected, ">=": actual >= expected, "<": actual < expected, "<=": actual <= expected}[op]
        raise BusinessToolError("the declarative adapter cannot apply this filter")

    def query(self, request: EntityQuery, *, context=None) -> EntityQueryPage:
        spec = self._by_name.get(request.entity.reference)
        if spec is None:
            raise BusinessToolError("the requested entity is no longer available")
        id_field = spec.id_field
        source = sorted(self._rows(spec), key=lambda row: str(row[id_field]))
        matched = [row for row in source if all(self._matches(row, cond) for cond in request.filters)]
        page = matched[request.offset : request.offset + request.limit]
        ref_prefix = spec.subject_reference or spec.name
        records = tuple(
            EntityRecord(
                reference=f"{ref_prefix}/{row[id_field]}", display_id=str(row[id_field]),
                cells=tuple(EntityCell(field_reference=field.reference, value=_cell_text(row.get(field.reference)), unit=field.fixed_unit if field.kind == "monetary" else "") for field in request.fields),
            )
            for row in page
        )
        scope = self._executor._scope(context) if hasattr(self._executor, "_scope") else "current actor workspace"
        return EntityQueryPage(
            records=records, matched_count=len(matched), source_total=len(source),
            source=f"delegated REST API ({spec.label})"[:200],
            scope=f"{scope}; {len(request.filters)} AND filters; stable identity ascending"[:200],
            consistency="one complete actor-visible REST list filtered and paged in the adapter",
        )


class DeclarativePageEntryPort:
    """Declarative ``PageEntryPort`` combining reviewed ``PageSpec``s with live menus."""

    def __init__(self, executor: Any, pages: tuple[PageSpec, ...], entities: tuple[EntitySpec, ...], *, application_id: str, menu_fetcher: Callable[[OperationContext | None], Mapping[str, Any]] | None = None):
        self._executor, self._pages, self._page_list = executor, {p.key: p for p in pages}, tuple(pages)
        self._entities, self._application_id, self._menu_fetcher = {e.name: e for e in entities}, application_id, menu_fetcher

    def catalog(self, *, context=None) -> PageEntryCatalog:
        actor_binding = DeclarativeEntityReadPort._actor_binding(context)
        if self._menu_fetcher is not None:
            payload = self._menu_fetcher(context)
            if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list) or not isinstance(payload.get("complete"), bool):
                raise BusinessToolContractError("the application returned an invalid page catalog")
            principal = getattr(context, "principal", None)
            if principal is not None and (payload.get("actor") != principal.actor_id or payload.get("workspace") != principal.workspace_id):
                raise BusinessToolPermissionError("the page catalog actor differs from the bound actor")
            entries = []
            for item in payload["pages"]:
                if not isinstance(item, dict) or set(item) != {"key", "label", "path"} or any(not isinstance(v, str) or not v.strip() for v in item.values()):
                    raise BusinessToolContractError("the application returned an invalid page entry")
                reviewed = self._pages.get(item["key"])
                route = item["path"] if reviewed is not None and item["path"] == reviewed.route else ""
                search_text = f"{item['label']} {reviewed.search_text}".strip() if route and reviewed and reviewed.search_text else item["label"]
                entries.append(PageEntry(reference=item["key"], label=item["label"], search_text=search_text, subject_reference=reviewed.subject_reference if route and reviewed else "", breadcrumbs=(item["label"],), views=reviewed.views if route and reviewed else (), route=route, unavailable_reason="" if route else "unreviewed_route"))
            complete, digest_payload = payload["complete"], json.dumps(payload, sort_keys=True)
        else:
            entries = [PageEntry(reference=p.key, label=p.label, search_text=p.search_text, subject_reference=p.subject_reference, breadcrumbs=p.breadcrumbs, views=p.views, route=p.route) for p in self._page_list]
            complete, digest_payload = True, json.dumps([p.key for p in self._page_list])
        digest = hashlib.sha256((f"{self._application_id}-page-entries-v2\0" + actor_binding + "\0" + digest_payload).encode()).hexdigest()
        scope = self._executor._scope(context) if hasattr(self._executor, "_scope") else "current actor"
        return PageEntryCatalog(binding_digest=digest, entries=tuple(entries), complete=complete, scope=scope + "; current actor menu")

    def parts(self, entry: PageEntry, *, context=None) -> tuple[PagePart, ...]:
        pspec = self._pages.get(entry.reference)
        entity = self._entities.get(pspec.entity) if pspec and pspec.entity else None
        if entity is None:
            return ()
        return tuple(PagePart(reference=f.name, label=f.label, kind="field", search_text=(f.search_text or f"{f.label} {f.name}").strip()) for f in entity.fields if f.name != entity.id_field and f.in_entity)


class DeclarativeReadExecutor:
    """Generic read/lookup/candidate executor backing ``DeclarativeApplicationAdapter``."""

    def __init__(
        self, *, application_id: str, client: DeclarativeRestClient, entities: tuple[EntitySpec, ...],
        pages: tuple[PageSpec, ...], menu_fetcher: Callable[[OperationContext | None], Mapping[str, Any]] | None = None,
        write_specs: tuple[TypedWriteSpec, ...] = (), allow_mutations: bool = False,
        scope_formatter: Callable[[Any], str] | None = None, entity_port_cls: type[DeclarativeEntityReadPort] = DeclarativeEntityReadPort,
    ):
        self.application_id, self.client, self.origin, self._api_token = application_id, client, client.origin, client._api_token
        self._entities = tuple(entities)
        self._by_name = {e.name: e for e in self._entities}
        self._by_list_tool = {e.list_tool_name: e for e in self._entities if e.list_tool_name}
        self._write_specs, self.allow_mutations = {w.name: w for w in write_specs}, bool(allow_mutations)
        if self.allow_mutations and write_specs and not client._api_token and client._headers_factory is None:
            raise ValueError("a write needs a delegated credential to write with")
        self._scope_formatter, self.identity_port = scope_formatter, None
        entity_port = entity_port_cls(self, self._entities, application_id=application_id)
        self._entity_port = entity_port
        self._entity_reads = EntityReadTools(entity_port, application_id=application_id)
        self._reference_lookup = ReferenceLookupTools(entity_port, entity_port, application_id=application_id)
        page_port = DeclarativePageEntryPort(self, pages, self._entities, application_id=application_id, menu_fetcher=menu_fetcher)
        self._page_entries = PageEntryTools(page_port, application_id=application_id)
        self._goal_candidates = GoalCandidateTools(self._entity_reads, self._reference_lookup, self._page_entries, application_id=application_id)
        self._write_handlers = {w.name: DeclarativeTypedWriteHandler(w, self, application_id=application_id) for w in write_specs}

    def set_identity_port(self, identity_port: Any) -> None:
        self.identity_port = identity_port
        self._write_handlers = {
            w.name: DeclarativeTypedWriteHandler(w, self, application_id=self.application_id, identity_port=identity_port, require_context=False)
            for w in self._write_specs.values()
        }

    def _scope(self, context) -> str:
        if self._scope_formatter is not None:
            return self._scope_formatter(context)
        principal = getattr(context, "principal", None)
        return f"workspace {principal.workspace_id}: records the current actor may read" if principal is not None else "records the delegated actor may read"

    @observed_interval("application")
    def fetch_rows(self, entity_name: str) -> list[dict]:
        spec = self._by_name.get(entity_name)
        if spec is None:
            raise BusinessToolError("unknown entity")
        if spec.row_fetcher is not None:
            return spec.row_fetcher(self)
        req_keys = tuple(f.name for f in spec.fields if f.in_entity and f.required)
        return self.client.get_collection(spec.list_endpoint, collection_key=spec.collection_key, id_field=spec.id_field, required_keys=req_keys)

    def get(self, path: str, **params) -> dict:
        return self.client.get_json(path, **params)

    def tool_schemas(self) -> tuple[dict, ...]:
        list_schemas = tuple(
            {"type": "function", "function": {
                "name": s.list_tool_name,
                "description": s.list_tool_description or f"Read current {s.label} from the workspace.",
                "parameters": {"type": "object", "properties": ({s.list_filter_param: {"type": "string", "description": s.list_filter_description or "Optional filter."}} if s.list_filter_param else {}), "additionalProperties": False},
            }}
            for s in self._by_list_tool.values()
        )
        write_schemas = tuple({"type": "function", "function": {"name": w.name, "description": w.description, "parameters": w.parameters}} for w in self._write_specs.values()) if self.allow_mutations else ()
        return list_schemas + ENTITY_READ_SCHEMAS + (RESOLVE_RECORD_REFERENCE_SCHEMA, FIND_PAGE_ENTRIES_SCHEMA, RESOLVE_GOAL_CANDIDATES_SCHEMA) + write_schemas

    def tool_effects(self) -> dict[str, str]:
        effects = {e.list_tool_name: "read" for e in self._by_list_tool.values()}
        effects.update({name: "read" for name in ENTITY_READ_TOOLS})
        effects.update({RESOLVE_RECORD_REFERENCE_TOOL: "read", FIND_PAGE_ENTRIES_TOOL: "read", RESOLVE_GOAL_CANDIDATES_TOOL: "read"})
        if self.allow_mutations:
            effects.update({wname: "write" for wname in self._write_specs})
        return effects

    def execute(self, name: str, args: dict, *, context=None) -> BusinessToolResult:
        if name in ENTITY_READ_TOOLS:
            return self._entity_reads.execute(name, args, context=context)
        if name == RESOLVE_RECORD_REFERENCE_TOOL:
            return self._reference_lookup.execute(args, context=context)
        if name == FIND_PAGE_ENTRIES_TOOL:
            return self._page_entries.execute(args, context=context)
        if name == RESOLVE_GOAL_CANDIDATES_TOOL:
            return self._goal_candidates.execute(args, context=context)
        spec = self._by_list_tool.get(name)
        if spec is None:
            raise BusinessToolError("invalid business query")
        allowed_args = {spec.list_filter_param} if spec.list_filter_param else set()
        if not isinstance(args, dict) or set(args) - allowed_args:
            raise BusinessToolError("invalid query arguments")
        query = args.get(spec.list_filter_param, "") if spec.list_filter_param else ""
        if not isinstance(query, str) or len(query) > 120 or re.search(r"[\x00-\x1f\x7f]", query):
            raise BusinessToolError(f"{spec.list_filter_param or 'query'} must be a string of at most 120 characters")
        source, filter_field, id_field = self.fetch_rows(spec.name), (spec.list_filter_field or spec.name_field), spec.id_field
        matched = [
            {**raw, **({"ui_path": spec.detail_route_template.format(id=raw[id_field])} if spec.detail_route_template else {})}
            for raw in source if not query or query.casefold() in str(raw.get(filter_field, "")).casefold()
        ]
        routes = tuple(item["ui_path"] for item in matched if item.get("ui_path"))
        conditions = (f"{filter_field} case-insensitive substring {query!r}",) if query else ("no filter",)
        scope = self._scope(context)
        origin = Origin(kind="typed", reference=name, observed_at=datetime.now(timezone.utc).isoformat(), scope=scope + "; " + "; ".join(conditions))
        evidence: list[Any] = [QueryEvidence(evidence_id="query#1", origin=origin, collection=spec.name, looked_for=query, conditions=conditions, matched_count=len(matched), records_scanned=len(source), covers_whole_scope=True)]
        ev_fields, ref_prefix = tuple(f.name for f in spec.fields if f.name != id_field and f.in_entity and not f.is_secret), (spec.subject_reference or spec.name)
        for row in matched:
            for field in ev_fields:
                if field in row:
                    ev_val, ev_type = _evidence_value(row[field])
                    evidence.append(RecordEvidence(evidence_id=f"record#{len(evidence)}", origin=origin, record_id=f"{ref_prefix}/{row[id_field]}", field_name=field, field_meaning=field, field_was_read=True, value=ev_val, value_type=ev_type))
                else:
                    evidence.append(RecordEvidence(evidence_id=f"record#{len(evidence)}", origin=origin, record_id=f"{ref_prefix}/{row[id_field]}", field_name=field, field_meaning=field, field_was_read=False))
        return BusinessToolResult(
            content={spec.collection_key or spec.name: matched, "matched_count": len(matched), "source_total": len(source), "scope": scope, "completeness": {"source_complete": True, "result_truncated": False, "incomplete_reason": ""}},
            audit={"application": self.application_id, "operation": name, "returned_count": len(matched), "source_complete": True},
            allowed_routes=routes, evidence=tuple(evidence),
        )

    def prepare_mutation(self, name: str, args: dict, *, context=None):
        handler = self._write_handlers.get(name)
        if not self.allow_mutations or handler is None:
            raise BusinessToolError("this composition has no such write")
        return handler.prepare_spec(args, context=context)

    def execute_mutation(self, name: str, args: dict, *, context=None) -> BusinessToolResult:
        handler = self._write_handlers.get(name)
        if not self.allow_mutations or handler is None:
            raise BusinessToolError("this composition has no such write")
        return handler.execute_verified(args, context=context)


__all__ = ["DeclarativeEntityReadPort", "DeclarativePageEntryPort", "DeclarativeReadExecutor", "DeclarativeRestClient"]
