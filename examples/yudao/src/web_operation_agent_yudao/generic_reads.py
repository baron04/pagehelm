"""Translate actor-bound Yudao metadata and reads into the shared tool contracts.

No business question is enumerated here. The existing reader owns endpoint selection,
record permissions, complete pagination and money normalization; the tools expose only
catalog-bound entity/field IDs and application-qualified page addresses.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from web_operation_agent.business.entities import (
    ENTITY_READ_TOOLS, RESOLVE_RECORD_REFERENCE_SCHEMA, RESOLVE_RECORD_REFERENCE_TOOL,
    EntityAggregate, EntityCatalog, EntityCell, EntityChoice, EntityDescriptor, EntityField,
    EntityPageHint, EntityQueryPage, EntityReadTools, EntityRecord, ReferenceLookupTools,
    ReferenceMatch,
)
from web_operation_agent.business.errors import (BusinessToolContractError, BusinessToolRequestError,
                                                 BusinessToolUnavailableError, BusinessToolPermissionError,
                                                 BusinessToolAuthenticationError)
from web_operation_agent.business.goal_candidates import (
    RESOLVE_GOAL_CANDIDATES_SCHEMA, RESOLVE_GOAL_CANDIDATES_TOOL, GoalCandidateTools,
)
from web_operation_agent.business.page_entries import (
    FIND_PAGE_ENTRIES_SCHEMA, FIND_PAGE_ENTRIES_TOOL, PageEntry, PageEntryCatalog,
    PageEntryTools, PagePart,
)
from web_operation_agent.goal.data import EVERYTHING, ID, NAME, Compare, DataRefused, LinkValue, all_of

from .goal import _SECRET
from .client import YudaoReadRefused


_KINDS = {"text": ("text", "text"), "html": ("text", "text"),
          "number": ("number", "number"), "money": ("monetary", "number"),
          "date": ("date", "date"), "datetime": ("datetime", "moment"),
          "bool": ("boolean", "boolean"), "choice": ("choice", "text"),
          "link": ("reference", "text")}
_OPERATORS = {"=": "eq", "!=": "ne", ">": "gt", ">=": "ge", "<": "lt",
              "<=": "le", "contains": "contains", "in": "in", "not_in": "not_in"}
_CREDENTIAL = re.compile(r"password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|"
                         r"access[_-]?key|credential|signature|cookie|session|密码|密钥", re.I)


class YudaoEntityReadPort:
    def __init__(self, data):
        self.data = data

    def _binding(self, context):
        principal = getattr(context, "principal", None)
        actor = principal.actor_authority_digest if principal is not None else "synthetic-actor"
        schema = self.data.schema()
        generation = getattr(self.data._client, "read_generation", 0)
        material = (actor, str(self.data._client.tenant), generation,
                    [(e.key, e.label, [(f.key, f.label, f.kind) for f in e.fields])
                     for e in schema.entities], [(p.key, p.entity) for p in schema.pages])
        return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()

    def catalog(self, *, context=None):
        schema = self.data.schema()
        index = self.data.index()
        entities = []
        complete = True
        for entity in schema.entities:
            pages = [p for p in schema.pages if p.entity == entity.key]
            indexed = index.entity(entity.key)
            aliases = indexed.aliases if indexed is not None else ()
            words = [entity.label, entity.key, *aliases]
            words.extend(p.label for p in pages)
            search_text = " ".join(dict.fromkeys(words))
            complete = complete and len(search_text) <= 2000 and len(pages) <= 10
            entities.append(EntityDescriptor(
                reference=entity.key, label=entity.label,
                search_text=search_text[:2000],
                page_hints=tuple(EntityPageHint(name=p.label, views=("list", "detail"))
                                 for p in pages[:10])))
        return EntityCatalog(binding_digest=self._binding(context), entities=tuple(entities),
                             complete=complete, scope="当前账号可读取的应用实体")

    def fields(self, entity, *, context=None):
        del context
        spec = self.data.schema().entity(entity.reference)
        if spec is None:
            raise BusinessToolRequestError("the entity is no longer available")
        fields = []
        for field in spec.fields:
            if (field.kind not in _KINDS or _SECRET.search(field.key)
                    or _CREDENTIAL.search(field.key + " " + field.label)):
                continue
            kind, evidence_type = _KINDS[field.kind]
            unit = self.data._money_for(entity.reference, field.key)
            fields.append(EntityField(
                reference=field.key, label=field.label, kind=kind, evidence_type=evidence_type,
                searchable=field.searchable, summable=(field.key != ID and
                    (kind == "number" or kind == "monetary" and bool(unit))),
                fixed_unit=("元" if unit in {"元", "分"} else unit) if kind == "monetary" else "",
                # Do not present an incomplete enum as every available choice. A large
                # dictionary remains queryable by values read from the application.
                choices=tuple(EntityChoice(value=c.key, label=c.label) for c in field.choices)
                        if len(field.choices) <= 50 else (),
                related_entity_reference=field.relation if kind == "reference" else ""))
        return tuple(fields)

    def lookup(self, entity, reference, *, limit, context=None):
        del context
        rows = self.data.find(entity.reference, reference, limit=limit + 1)
        return tuple(ReferenceMatch(
            record_id=row.id, label=row.display or row.name,
            route=self._record_route(entity.reference, row.id))
            for row in rows[:limit]), len(rows) > limit

    def _record_route(self, entity, record_id):
        # A collection page is not the address of a record. Only the application's
        # qualified detail template may be returned as record navigation authority.
        page = next((p.key for p in self.data.schema().pages if p.entity == entity), "")
        return (self.data.record_address(entity, record_id)
                if page in self.data._ui.records else "")

    def _condition(self, filters):
        conditions = []
        for item in filters:
            operator = _OPERATORS.get(item.operator)
            if operator is None:
                raise BusinessToolRequestError("unsupported condition")
            value = tuple(item.value) if operator in {"in", "not_in"} else item.value
            if item.field.kind == "datetime":
                def local_time(raw):
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    return (parsed.astimezone(ZoneInfo(self.data.time_zone())).replace(tzinfo=None)
                            if parsed.tzinfo is not None else parsed)
                value = tuple(local_time(v) for v in value) if isinstance(value, tuple) else local_time(value)
            conditions.append(Compare(item.field.reference, operator, value))
        return all_of(*conditions)

    def _cell(self, entity, row, field):
        value = (row.id if field.reference == ID else row.display if field.reference == NAME
                 else row.values.get(field.reference))
        unit, related = "", ""
        if field.kind in {"number", "monetary"} and value is not None:
            # The legacy reader normalizes numeric money to display units. A
            # string has not passed that normalization, so attaching the display
            # unit could silently multiply a cents value by one hundred.
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                raise BusinessToolContractError("the application returned an unsupported numeric value")
            if not Decimal(str(value)).is_finite():
                raise BusinessToolContractError("the application returned a nonfinite numeric value")
        if isinstance(value, LinkValue):
            related, value = value.id, value.display
        elif isinstance(value, datetime):
            value = value.isoformat() if field.kind == "datetime" else value.date().isoformat()
        elif isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, bool):
            value = "true" if value else "false"
        elif field.kind == "choice" and value is not None:
            value = row.shown.get(field.reference, value)
        elif field.kind == "monetary":
            declared = self.data._money_for(entity, field.reference)
            if value is not None:
                unit = "元" if declared in {"元", "分"} else declared
        return EntityCell(field_reference=field.reference,
                          value="" if value is None else str(value), unit=unit,
                          related_record_id=related)

    def query(self, request, *, context=None):
        del context
        key = request.entity.reference
        where = self._condition(request.filters)
        try:
            rows = (self.data.rows(key, where, fields=tuple(f.reference for f in request.fields),
                                   order=((ID, False),), limit=request.offset + request.limit)
                    if request.fields else ())
            records = tuple(EntityRecord(
                reference=row.id, display_id=row.id,
                cells=tuple(self._cell(key, row, f) for f in request.fields),
                route=self._record_route(key, row.id))
                for row in rows[request.offset:])
            matched = self.data.count(key, where)
            total = self.data.count(key, EVERYTHING)
            aggregates = []
            if request.aggregate_fields:
                whole = self.data.rows(key, where,
                    fields=tuple(f.reference for f in request.aggregate_fields), limit=self.data._max_rows)
                if len(whole) != matched:
                    raise BusinessToolContractError("the application did not provide every aggregate row")
            for field in request.aggregate_fields:
                amount = Decimal(0)
                for row in whole:
                    raw = row.values.get(field.reference)
                    # A missing or malformed value is not a zero. The full sum must
                    # retain the same exact numeric semantics as the returned cells.
                    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal)):
                        raise BusinessToolContractError("the application omitted an aggregate value")
                    try:
                        value = Decimal(str(raw))
                    except InvalidOperation as exc:
                        raise BusinessToolContractError("the application returned a nonnumeric aggregate value") from exc
                    if not value.is_finite():
                        raise BusinessToolContractError("the application returned a nonfinite aggregate value")
                    amount += value
                unit = field.fixed_unit
                aggregates.append(EntityAggregate(field_reference=field.reference,
                                                  value=str(amount), count=matched, unit=unit))
            return EntityQueryPage(records=records, matched_count=matched, source_total=total,
                                   source="当前账号的应用接口", scope=request.entity.label,
                                   aggregates=tuple(aggregates))
        except (TypeError, ValueError) as exc:
            raise BusinessToolContractError("the application returned an unsupported field value") from exc


class YudaoPageEntryPort:
    def __init__(self, entity_port):
        self.entities = entity_port

    def catalog(self, *, context=None):
        data = self.entities.data
        schema = data.schema()
        entries = tuple(PageEntry(reference=p.key, label=p.label,
                                  search_text=f"{p.label} {p.key} {schema.entity(p.entity).label}",
                                  subject_reference=p.entity, route=data.page_address(p.key))
                        for p in schema.pages)
        return PageEntryCatalog(binding_digest=self.entities._binding(context), entries=entries,
                                complete=True, scope="当前账号可访问的应用页面")

    def parts(self, entry, *, context=None):
        del context
        entity = self.entities.data.schema().entity(entry.subject_reference)
        return tuple(PagePart(reference=f.key, label=f.label, kind="field")
                     for f in entity.fields if f.on_form and not _SECRET.search(f.key)
                     and not _CREDENTIAL.search(f.key + " " + f.label)) if entity is not None else ()


class YudaoReadTools:
    """The same discover/read/page tools used by other application distributions."""
    def __init__(self, data, *, application_id):
        port = YudaoEntityReadPort(data)
        self.entity_reads = EntityReadTools(port, application_id=application_id)
        self.references = ReferenceLookupTools(port, port, application_id=application_id)
        self.pages = PageEntryTools(YudaoPageEntryPort(port), application_id=application_id)
        self.candidates = GoalCandidateTools(self.entity_reads, self.references, self.pages,
                                            application_id=application_id)

    def tool_schemas(self):
        return (*self.entity_reads.tool_schemas(), RESOLVE_RECORD_REFERENCE_SCHEMA,
                FIND_PAGE_ENTRIES_SCHEMA, RESOLVE_GOAL_CANDIDATES_SCHEMA)

    def tool_effects(self):
        return {tool["function"]["name"]: "read" for tool in self.tool_schemas()}

    def execute(self, name, args, *, context=None):
        try:
            return self._execute(name, args, context=context)
        except YudaoReadRefused as exc:
            if exc.code == 403:
                raise BusinessToolPermissionError("the application denied this read", layer="application") from exc
            if exc.code == 401:
                raise BusinessToolAuthenticationError("the application sign-in expired") from exc
            raise BusinessToolUnavailableError(exc.reason or "the application refused this read") from exc
        except DataRefused as exc:
            raise BusinessToolUnavailableError("the application did not provide a complete read") from exc

    def _execute(self, name, args, *, context=None):
        if name in ENTITY_READ_TOOLS:
            return self.entity_reads.execute(name, args, context=context)
        if name == RESOLVE_RECORD_REFERENCE_TOOL:
            return self.references.execute(args, context=context)
        if name == FIND_PAGE_ENTRIES_TOOL:
            return self.pages.execute(args, context=context)
        if name == RESOLVE_GOAL_CANDIDATES_TOOL:
            return self.candidates.execute(args, context=context)
        raise BusinessToolRequestError("unknown application read tool")
