"""Compile one flat read and resolve its literal, actor-bound scope without guessing."""
from __future__ import annotations

from dataclasses import replace

from ..business.errors import BusinessToolContractError, BusinessToolRequestError
from .data import ID, NAME, Compare, LinkValue, Schema, any_of, as_bool
from .ports import Resolved
from .resolve.resolver import Ambiguous, Resolver
from .resolve.words import EMPTY, NOT_EMPTY, norm
from .spec import GoalSpec


_OPERATIONS = {"record", "list", "count", "sum", "min", "max"}
_KEYS = {"operation", "entity", "record", "via", "fields", "field", "filters", "page",
         "page_filters", "search", "group_by", "sort", "descending", "limit"}


def _text(value, name):
    if not isinstance(value, str) or len(value) > 120:
        raise BusinessToolRequestError(f"{name} must be a string of at most 120 characters")
    return " ".join(value.split())


def _strings(value, name, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise BusinessToolRequestError(f"{name} must be an array of at most {limit} strings")
    result = [_text(item, name) for item in value]
    if any(not item for item in result):
        raise BusinessToolRequestError(f"{name} cannot contain empty strings")
    return result


def compile_read(args) -> GoalSpec:
    """Validate before actor binding; no property can disappear during compilation."""
    if not isinstance(args, dict) or set(args) - _KEYS:
        raise BusinessToolRequestError("read_data contains unsupported properties")
    operation = args.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise BusinessToolRequestError("read_data operation must be record, list, count, sum, min or max")
    text = {key: _text(args.get(key, ""), key) for key in (
        "entity", "record", "via", "field", "page", "search", "group_by", "sort")}
    if not text["entity"]:
        raise BusinessToolRequestError("read_data needs an indexed entity")
    fields = _strings(args.get("fields", []), "fields", 12)
    facets = _strings(args.get("page_filters", []), "page_filters", 8)
    filters = args.get("filters", [])
    if not isinstance(filters, list) or len(filters) > 6:
        raise BusinessToolRequestError("filters must be an array of at most six conditions")
    normalized = []
    for item in filters:
        if not isinstance(item, dict) or set(item) - {"field", "values", "op"}:
            raise BusinessToolRequestError("condition contains unsupported properties")
        label = _text(item.get("field", ""), "filter field")
        values = _strings(item.get("values"), "filter values", 6)
        op = _text(item.get("op", ""), "filter op")
        if not label or not values and op not in {"=", "!="}:
            raise BusinessToolRequestError("a condition needs a field and values, or =/!= for emptiness")
        normalized.append({"field": label, "values": values, "op": op})
    if "descending" in args and type(args["descending"]) is not bool:
        raise BusinessToolRequestError("descending must be boolean")
    if "limit" in args and (type(args["limit"]) is not int or not 1 <= args["limit"] <= 50):
        raise BusinessToolRequestError("limit must be an integer from 1 to 50")
    if operation != "list" and (text["sort"] or text["search"] or "limit" in args
                                or "descending" in args):
        raise BusinessToolRequestError("search, sort, descending and limit belong to list only")
    if "descending" in args and not text["sort"]:
        raise BusinessToolRequestError("descending requires an explicit sort field")
    if operation != "record" and (text["record"] or text["via"]):
        raise BusinessToolRequestError("record and via belong to record only; scope collections with filters")
    if text["via"] and not text["record"]:
        raise BusinessToolRequestError("via requires a named source record; an owner restriction belongs in filters")
    if text["group_by"] and operation != "count":
        raise BusinessToolRequestError("group_by belongs to count only")
    if operation in {"sum", "min", "max"}:
        if not text["field"]:
            raise BusinessToolRequestError("sum, min and max require a numeric field")
    elif text["field"]:
        raise BusinessToolRequestError("field belongs to sum, min and max; use fields for record/list")
    if operation in {"count", "sum"} and fields:
        raise BusinessToolRequestError("count and sum do not read display columns")
    if operation == "record" and (not fields or not (text["record"] or normalized)):
        raise BusinessToolRequestError("record needs fields and a name or explicit identifying conditions")

    declaration = {"kind": "fact" if operation == "record" else
                   "list" if operation == "list" else "aggregate",
                   "entity": text["entity"], "fields": fields,
                   "record": text["record"], "via": text["via"],
                   "page": {"key": text["page"], "search": text["search"]},
                   "metric": {"op": "count" if operation in {"record", "list"} else operation,
                              "field": text["field"], "filters": normalized,
                              "page_filters": facets, "group_by": text["group_by"],
                              "total": operation == "sum"},
                   "sort": text["sort"], "descending": args.get("descending", False),
                   "limit": args.get("limit", 0)}
    try:
        return GoalSpec.from_json({"deliverables": [declaration]})
    except (ValueError, TypeError) as exc:
        raise BusinessToolRequestError(str(exc)) from exc


def bind_read(spec, index, schema):
    """Use only unique exact indexed labels/keys/aliases, confirmed by the live schema."""
    if not isinstance(schema, Schema):
        raise BusinessToolContractError("the application returned an invalid read schema")
    d = spec.deliverables[0]
    entity = index.entity(d.entity)
    if entity is None:
        raise BusinessToolRequestError("the entity is absent from this actor's index")
    native = schema.entity(entity.key)
    if native is None:
        raise BusinessToolContractError("the indexed entity has no bound read schema")

    def field(label, current=entity):
        found = {f.key: f for f in (*current.fields, *current.held)
                 if norm(label) in {norm(f.key), norm(f.label), *(norm(a) for a in f.aliases)}}
        if len(found) != 1:
            raise BusinessToolRequestError(f"{label!r} is not one unique exact indexed field of {current.label}")
        indexed = next(iter(found.values()))
        owner = schema.entity(current.key)
        actual = owner.field(indexed.key) if owner is not None else None
        if actual is None or actual.label != indexed.label:
            raise BusinessToolContractError("the indexed field differs from the bound read schema")
        if sum(norm(f.label) == norm(actual.label) for f in owner.fields) != 1:
            raise BusinessToolRequestError("the requested field label is ambiguous in the read schema")
        return actual

    page = index.page(d.page.key) if d.page.key else None
    if d.page.key and page is None:
        raise BusinessToolRequestError("the query page is absent from this actor's index")
    if page is not None and (not page.entity or
                            page.entity != entity.key and page.entity != native.lines_of):
        raise BusinessToolRequestError("the query page does not scope the requested entity")
    page_schema = schema.page(page.key) if page is not None else None
    if page is not None and page_schema is None:
        raise BusinessToolContractError("the query page has no bound read schema")

    target = entity
    via = ""
    if d.via:
        link = field(d.via)
        if link.kind != "link" or not (target := index.entity(link.relation)):
            raise BusinessToolRequestError("via must name an indexed single-record link")
        via = link.label
    columns = tuple(field(label, target).label for label in d.fields)
    numeric = field(d.metric.field) if d.metric.field else None
    if numeric is not None and numeric.kind not in {"number", "money"}:
        raise BusinessToolRequestError("sum, min and max require an indexed numeric field")
    conditions = []
    for condition in d.metric.filters:
        info = field(condition.field)
        if not info.searchable:
            raise BusinessToolRequestError(f"{info.label} cannot be used as a condition")
        allowed = {"", "=", "!="}
        if info.kind in {"number", "money", "date", "datetime"}:
            allowed |= {"<", "<=", ">", ">=", "between"}
            maximum = 2 if condition.op == "between" else 1
            if len(condition.values) > maximum:
                raise BusinessToolRequestError("numeric/date comparisons cannot discard extra values")
        elif info.kind in {"text", "html", "link"}:
            allowed |= {"contains", "starts with"}
        if condition.op not in allowed:
            raise BusinessToolRequestError(f"{condition.op!r} is not supported for {info.label}")
        if condition.op == "between" and len(condition.values) != 2:
            raise BusinessToolRequestError("between needs exactly two boundary values")
        if info.kind == "bool" and len(condition.values) > 1:
            raise BusinessToolRequestError("a boolean comparison needs one value")
        conditions.append(replace(condition, field=info.label))
    filters = []
    for name in d.metric.page_filters:
        available = page_schema.filters if page_schema is not None else native.filters
        matches = [f for f in available if norm(name) in {norm(f.key), norm(f.label)}]
        if len(matches) != 1 or not matches[0].usable or matches[0].cond is None:
            raise BusinessToolRequestError("the named page filter is not uniquely usable in this scope")
        if matches[0].relative and matches[0].date_field:
            raise BusinessToolRequestError("a period needs an explicit condition on its indexed date field")
        filters.append(matches[0].label)
    bound = replace(d, entity=entity.key, via=via, fields=columns,
        sort=field(d.sort).label if d.sort else "",
        page=replace(d.page, key=page.key if page is not None else ""),
        metric=replace(d.metric, field=numeric.label if numeric else "",
            filters=tuple(conditions), page_filters=tuple(filters),
            group_by=field(d.metric.group_by).label if d.metric.group_by else ""))
    return replace(spec, deliverables=(bound,))


class SimpleReadResolver(Resolver):
    def _list_page(self, d):
        # The reused list resolver prepares a navigation plan for goal turns.
        # This surface delivers facts only, and need not access a page-address
        # port merely because the data schema includes an optional list page.
        return Resolved(kind="page")

    """Reuse typed reads/evidence, retaining every declared condition and page scope.

    The declaration resolver's recovery heuristics are useful for goal repair, but a
    flat tool call cannot silently reinterpret a failed query as a different query.
    """

    def _scope(self, d, *, search):
        scope = super()._scope(d, search=search)
        if not isinstance(scope, Resolved):
            scope.default_clauses = []  # The clauses stay in domain and cannot be lifted.
            scope.fixed = True
        return scope

    def _search_clause(self, view, model, domain, words):
        return [Compare(NAME, "contains", words)], self.say.words(words)

    def _find_record(self, model, reference):
        found = super()._find_record(model, reference)
        if found is not None and not isinstance(found, Ambiguous):
            names = {norm(found.ref.own_name), norm(found.ref.display), norm(found.personal)}
            if norm(reference) not in names and norm(self._own_name(reference)) not in names:
                return None
        return found

    def _condition(self, view, label, values, *, op="", today=None):
        key = view.field_by_label(label)
        info = view.info(key)
        if info is not None and values and not (
                len(values) == 1 and norm(values[0]) in EMPTY | NOT_EMPTY):
            if info.kind == "bool":
                value = as_bool(values[0])
                if value is None:
                    return None, f"{values[0]!r} is not a boolean value"
                return [Compare(key, "ne" if op == "!=" else "eq", value)], self.say.compare(label, op, values)
            if info.kind == "choice" and any(not any(norm(value) in {
                    norm(c.key), norm(c.label), *(norm(a) for a in c.aliases)} for c in info.choices)
                    for value in values):
                return None, f"the requested value is not an exact {info.label} choice"
            if info.kind == "link":
                if op in {"contains", "starts with"}:
                    comparison = "contains" if op == "contains" else "starts"
                    return [any_of(*(Compare(key, comparison, v) for v in values))], self.say.compare(label, op, values)
                ids = []
                for value in values:
                    found = self._find_record(info.relation, value)
                    if isinstance(found, Ambiguous):
                        self._held_by = tuple(found)
                        return None, f"several {info.label} records are named {value!r}"
                    if found is None:
                        return None, f"no {info.label} matches {value!r}"
                    ids.append(found[1])
                return [Compare(key, "not_in" if op == "!=" else
                    "under" if info.inclusive else "in", tuple(ids))], self.say.compare(label, op, values)
        return super()._condition(view, label, values, op=op, today=today)

    def _resolve_one(self, d):
        if d.kind != "fact":
            return self._aggregate(d)
        if d.record:
            found = self._find_record(d.entity, d.record)
            if found is None:
                return Resolved(kind="fact", problem=f"no record matches {d.record!r}", missing=d.record)
            if isinstance(found, Ambiguous):
                return Resolved(kind="fact", problem="record reference is ambiguous", candidates=tuple(found))
        else:
            found = None
        scope = self._scope(d, search=False)
        if isinstance(scope, Resolved):
            return scope
        domain = scope.domain + ([Compare(ID, "eq", found[1])] if found else [])
        before = self._count(d.entity, domain)
        rows = self._rows(d.entity, domain, limit=2) if before else ()
        after = self._count(d.entity, domain)
        if before != after or len(rows) != min(after, 2):
            return Resolved(kind="fact", problem="the record scope is incomplete or changed during reading")
        subject = " · ".join(scope.described)
        if not rows:
            return Resolved(kind="fact", problem="no record matches the declared scope", no_matches=subject)
        if after != 1:
            return Resolved(kind="fact", problem="the declared scope matches several records",
                            candidates=tuple(row.display for row in rows))
        row = rows[0]
        if found is not None and row.id != found[1]:
            raise BusinessToolContractError("the application returned another record for the named scope")
        model, rid, display = d.entity, row.id, row.display
        if d.via:
            view = self.view(model)
            key = view.field_by_label(d.via)
            target = self._read(model, rid, (key,)).values.get(key)
            if not isinstance(target, LinkValue) or not target.id:
                return Resolved(kind="fact", problem="the requested link is empty or was not read")
            model, rid, display = view.info(key).relation, target.id, target.display
        return self._facts(model, rid, display, d.fields)


__all__ = ["compile_read", "bind_read", "SimpleReadResolver"]
