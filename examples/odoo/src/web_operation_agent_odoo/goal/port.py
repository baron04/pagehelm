"""Odoo's side of a goal-directed turn: the resolver.

Everything here is read-only JSON-RPC under the delegated user: ``name_search``, ``read``,
``search_count``, ``read_group`` and ``search_read``. Nothing here dispatches a page
command or a write; the core does the former and nothing does the latter.
"""
from __future__ import annotations

from dataclasses import replace

import ast
from datetime import date, datetime, time as time_of_day, timedelta, timezone
import html
import re
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from web_operation_agent.business.runtime import BusinessToolError
from web_operation_agent.goal.index import (ApplicationIndex, IndexEntity, IndexField,
                                             IndexPage)
from web_operation_agent.goal.ports import Aggregate, Fact, PagePlan, Resolution, Resolved
from web_operation_agent.goal.spec import Deliverable, Filter, GoalSpec, Metric, PageSpec
from web_operation_agent.state.identity import OperationContext

from .index import _IndexCompiler
from .lines import _LinesResolver
from .values import _ValueCandidates

from ..translations import OdooTranslations
from .text import (_any_of, Ambiguous, _EMPTY, _GROUP_LIMIT, _ModelMeta, _NOT_EMPTY, _RECORD_LIMIT, _Scope, _SearchFilter, _describes_record, _fields_of, _literal_dict, _literal_domain, _money, _norm, _period, _plain_text, _strip_code, _tab_of, _tokens)


#: Deliverable kinds answered by reading records; one may be answered as another.
_READS = frozenset({"fact", "list", "aggregate"})


class OdooGoalPort(_IndexCompiler, _LinesResolver, _ValueCandidates):
    """Index compilation and goal resolution over the delegated JSON-RPC client."""

    def __init__(self, client, *, translations: OdooTranslations | None = None):
        self._client = client
        #: What the application calls its labels in Chinese, from its own translation
        #: files; none until a deployment attaches them.
        self._translations = translations or OdooTranslations.none()
        self._conventions: tuple = ()
        self._lock = threading.Lock()
        self._index: ApplicationIndex | None = None
        self._actions: list[dict] = []
        self._models: dict[str, _ModelMeta] = {}
        self._page_filters: dict[int, list[_SearchFilter]] = {}
        self._model_labels: dict[str, str] = {}
        #: What this actor may do on each model, from the catalog the index was
        #: compiled from (create, read, write, unlink).
        self._operations: dict[str, frozenset] = {}
        #: The deployment's conventions the request being resolved names, with the
        #: records each selects (set per resolve from the declaration).
        self._named_scopes: tuple = ()
        self._reads: list[tuple[str, str]] = []
        #: A one2many's static domain the next aggregate applies to its line scope.
        self._line_scope: list | None = None
        #: The one record a list in this turn found, for a later deliverable that names
        #: none ("the order still to invoice: I want to invoice it now").
        self._found_one: tuple | None = None

    # -- resolution -----------------------------------------------------------------
    def locate_reference(self, text: str, *, context: OperationContext | None = None):
        """The records a hard reference names, as (entity key, display name), looked
        for on every entity a menu page shows; a number or a date names nothing here."""
        if not text or not re.search(r"[A-Za-z]", text) or re.fullmatch(r"[\d.,/-]+", text):
            return ()
        index = self.index(context=context)
        entities = list(dict.fromkeys(p.entity for p in index.pages if p.entity in self._models))
        hits = []
        for model in entities:
            found = self._find_record(model, text)
            if isinstance(found, tuple) and not isinstance(found, Ambiguous):
                wanted = _norm(text)
                shown = _norm(_strip_code(found[2]))
                # Exact, or the record's name begins with the words; a mere substring
                # somewhere inside a name is not a reference to that record.
                # A contact of a company is displayed "Company, Person": the person's
                # own name is the last part.
                own = _norm(str(found[2]).rsplit(",", 1)[-1])
                if (shown == wanted or shown.startswith(wanted) or _norm(found[2]).startswith(wanted)
                        or own == wanted or own.startswith(wanted)):
                    hits.append((model, found[2]))
        return tuple(hits[:3])

    def attach_translations(self, translations: OdooTranslations) -> None:
        """Give the index the application's own Chinese names for its labels. The index
        is recompiled on its next use."""
        with self._lock:
            self._translations = translations
            self._index = None
            self._models = {}

    def attach_conventions(self, conventions) -> None:
        """Give the index what this organisation means by some of its words, as its
        deployment declares. The index is recompiled on its next use."""
        with self._lock:
            self._conventions = tuple(conventions)
            self._index = None

    def attach_source(self, source_map, *, release: str) -> None:
        """Give the index the rules compiled from the deployment's source, cited under
        `release` (the deployment's own release name, the one an evaluator can bind).
        The index is recompiled on its next use."""
        with self._lock:
            self._source_map, self._source_release = source_map, release
            self._index = None

    def capabilities(self) -> frozenset[str]:
        """Everything optional; source rules only once a compiled map is attached."""
        base = {"facets", "sections", "lines", "handoffs", "fill", "search", "values"}
        if getattr(self, "_source_map", None) is not None:
            base.add("source")
        return frozenset(base)

    def resolve(self, spec: GoalSpec, *, context: OperationContext | None = None,
                current_path: str = "") -> Resolution:
        index = self.index(context=context)
        self._reads = []
        self._found_one = None
        self._named_scopes = tuple(getattr(spec, "conventions", ()) or ())
        resolved = []
        for deliverable in spec.deliverables:
            try:
                result = self._resolve_one(deliverable, index)
                if (result.kind != deliverable.kind and result.kind in _READS
                        and deliverable.kind in _READS):
                    # A fact about some lines is answered by listing them; it is still the
                    # fact that was asked, and its problems go to the same repair.
                    result = replace(result, kind=deliverable.kind)
                resolved.append(result)
            except BusinessToolError as exc:
                resolved.append(self._refused_read(deliverable, index, exc))
        return Resolution(deliverables=tuple(resolved), reads=tuple(self._reads))

    def _refused_read(self, d: Deliverable, index: ApplicationIndex,
                      exc: BusinessToolError) -> Resolved:
        """A read the application refused. When a condition is the cause -- a field the
        application lists as searchable but cannot search -- the condition is named and
        sent back for repair instead of ending the deliverable."""
        problem = f"the application refused a read: {exc}"[:200]
        filters = d.metric.filters if d.metric else ()
        if len(filters) < 2:
            return Resolved(kind=d.kind, problem=problem)
        culprits = []
        for condition in filters:
            rest = tuple(f for f in filters if f is not condition)
            try:
                self._resolve_one(replace(d, metric=replace(d.metric, filters=rest)), index)
            except BusinessToolError:
                continue
            culprits.append(condition.field)
        if len(culprits) != 1:
            return Resolved(kind=d.kind, problem=problem)
        return Resolved(kind=d.kind, problem=f"{culprits[0]!r} cannot be searched",
                        repair=(f"the application cannot search on {culprits[0]!r}; drop that "
                                "condition if the request did not ask for it, or use another "
                                "field that says the same"))

    def today(self) -> str:
        """The current date where the application's users are, for the declaration."""
        return self._today().isoformat()

    def _today(self) -> date:
        zone = ""
        time_zone = getattr(self._client, "time_zone", None)
        if callable(time_zone):
            try:
                zone = str(time_zone() or "")
            except BusinessToolError:
                zone = ""
        try:
            return datetime.now(ZoneInfo(zone)).date() if zone else date.today()
        except ZoneInfoNotFoundError:
            return date.today()

    def _utc_boundary(self, day: date) -> str:
        zone = ""
        time_zone = getattr(self._client, "time_zone", None)
        if callable(time_zone):
            try:
                zone = str(time_zone() or "")
            except BusinessToolError:
                zone = ""
        try:
            local = datetime.combine(day, time_of_day(), ZoneInfo(zone)) if zone else \
                datetime.combine(day, time_of_day(), timezone.utc)
        except ZoneInfoNotFoundError:
            local = datetime.combine(day, time_of_day(), timezone.utc)
        return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        self._reads.append((method, model))
        return self._client.call(model, method, args, kwargs or {})

    def _resolve_one(self, d: Deliverable, index: ApplicationIndex) -> Resolved:
        d = self._line_record_from_conditions(d)
        if isinstance(d, Resolved):
            return d
        if d.kind == "page" and not d.record and d.page.key and not (d.metric and d.metric.filters):
            return self._list_page(d, index)
        if d.kind == "list" and d.record and d.via:
            # "The lines of S00004", declared on the line entity with the parent named as
            # the record and the link field as via: a list with that link as condition.
            # A via that is not a field of the line entity means its parent link.
            line_meta = self._models.get(d.entity) if d.entity else None
            via = d.via
            parent = self._parent_link(d.entity) if line_meta is not None else None
            if line_meta is not None and line_meta.field_by_label(via) is None and parent is not None:
                via = str(line_meta.fields[parent[1]].get("string") or parent[1])
            if parent is not None:
                embedded = self._embedded_lines(parent[0], d.entity)
                if embedded is not None and embedded[1]:
                    self._line_scope = list(embedded[1])
            return self._aggregate(Deliverable(
                kind="list", entity=d.entity, fields=d.fields, sort=d.sort,
                descending=d.descending, limit=d.limit, page=d.page,
                metric=Metric(op="count", filters=(Filter(field=via, values=(d.record,)),)
                              + (d.metric.filters if d.metric else ()))), index)
        entity = index.entity(d.entity) if d.entity else None
        described = _describes_record(d)
        if (d.kind in ("fact", "page", "handoff") and entity is not None and not d.record
                and not described and self._found_one is not None
                and self._found_one[0] == entity.key and _norm(d.action) not in ("new", "create")):
            d = replace(d, record=self._found_one[2])
        if d.kind == "list" and entity is not None and (d.record or described):
            # "The order of 22 August, its products and subtotals": a list of the lines
            # of one record, the record named or described, the line field among the
            # columns asked for.
            line_field = next((f for f, i in self._models[entity.key].fields.items()
                               if i.get("type") == "one2many" and i.get("relation") in self._models
                               and self._embedded_lines(entity.key, str(i["relation"])) is not None
                               and any(_norm(i.get("string", "")) == _norm(label)
                                       for label in d.fields)), None) \
                if entity.key in self._models else None
            if line_field is not None:
                owner = self._find_record(entity.key, d.record) if d.record else None
                if not (isinstance(owner, tuple) and not isinstance(owner, Ambiguous)):
                    owner = self._locate(d, index) if described else Resolved(
                        kind=d.kind, problem=f"no {entity.label} matches {d.record!r}")
                if isinstance(owner, Resolved):
                    return owner
                return self._lines_list(entity.key, line_field, owner, d, index)
        if d.kind in ("aggregate", "list"):
            return self._aggregate(d, index)
        if (d.kind in ("handoff", "page") and entity is not None and _norm(d.action) == "delete"
                and self._operations and "unlink" not in self._operations.get(entity.key, ())):
            # Odoo shows Delete only to an account that may delete these records; for one
            # that may not, no record of them can be deleted, whichever one was meant.
            return Resolved(kind="handoff", problem=f"this account may not delete {entity.label} records",
                            denied="delete")
        if d.kind == "page" and entity is None and d.page.key:
            return self._list_page(d, index)
        if entity is None:
            return Resolved(kind=d.kind, problem="the entity named is not in the index",
                            repair=("entity must be one of the index's entity keys: "
                                    + ", ".join(e.key for e in index.entities[:30])))
        if d.kind == "handoff" and not described and (
                not d.record or _norm(d.action) in ("new", "create")):
            # A creation names no existing record; the name given, if any, is the new
            # record's or its customer's, and is not looked up.
            return self._creation(d, entity, index)
        if not d.record and not described:
            if d.kind == "page":
                page = self._default_page(entity.key, index)
                return self._list_page(Deliverable(kind="page", entity=d.entity,
                                                   page=type(d.page)(key=page.key if page else "",
                                                                     view=d.page.view,
                                                                     filters_on=d.page.filters_on,
                                                                     filters_off=d.page.filters_off,
                                                                     group_by=d.page.group_by)),
                                       index) if page else Resolved(
                    kind=d.kind, problem="no page shows this entity")
            return Resolved(kind=d.kind, problem="no record was named")
        found = self._find_record(entity.key, d.record) if d.record else None
        via = d.via
        if described and not (isinstance(found, tuple) and not isinstance(found, Ambiguous)):
            # Not a name but a description ("the quotation sent to Joel Willis", "the
            # 31,750 credit note"): the conditions pick the record out, or say why not.
            found = self._locate(d, index)
            if isinstance(found, Resolved):
                return found
        if d.kind == "handoff" and _norm(d.action) in ("", "new", "create") and (
                found is None or isinstance(found, Ambiguous)):
            # A change asked for and the name is no existing record: it is the record to
            # be created (or the customer it is for), and the control is the list page's
            # own "New". A change to a record that was named but not found is refused,
            # never turned into a creation.
            return self._creation(d, entity, index)
        if found is None:
            # The reference may belong to a record that links to this entity ("S00006" is
            # an order; the person asked about its customer). Re-home the reference when
            # exactly one such entity holds it, and follow the link.
            rehomed = self._rehome(entity.key, d.record, index)
            if rehomed is None:
                names = (self._names_repair(entity.key, self._models.get(entity.key))
                         if _foreign(d.record) else "")
                return Resolved(kind=d.kind, problem=f"no {entity.label} matches {d.record!r}",
                                missing=d.record,
                                repair=(f"no {entity.label} is named {d.record!r}; "
                                        + (f"{names}" if names else
                                           "if the person described the record rather than "
                                           "naming it, leave record empty and put the "
                                           "description in metric.filters (customer, amount, "
                                           "status, date)")))
            found, via = rehomed
            if d.via:
                via = d.via
        if isinstance(found, Ambiguous):
            return Resolved(kind=d.kind, problem=f"{d.record!r} is ambiguous",
                            candidates=tuple(found))
        model, rid, name = found
        if model != entity.key and d.fields and not via and model in self._models:
            # Described on one entity, found on another (an order the model looked for
            # among invoices): the fields asked for, as the entity it is on has them.
            d = replace(d, fields=_mapped_fields(d.fields, self._models[entity.key],
                                                 self._models[model]) or d.fields)
        if via:
            meta = self._models.get(model) or self._model_meta(model)
            link = meta.link_by_label(via)
            info = meta.fields.get(link or "", {})
            if link is None:
                lines = meta.field_by_label(via)
                line_info = meta.fields.get(lines or "", {})
                if line_info.get("type") == "one2many" and line_info.get("relation") in self._models:
                    # "The lines of S00004": a list of the line entity linked to the record.
                    line_meta = self._models[line_info["relation"]]
                    parent = next((f for f, i in line_meta.fields.items()
                                   if i.get("type") == "many2one" and i.get("relation") == model),
                                  None)
                    if parent is not None:
                        parent_label = str(line_meta.fields[parent].get("string") or parent)
                        return self._aggregate(Deliverable(
                            kind="list", entity=line_info["relation"], fields=d.fields,
                            sort=d.sort, descending=d.descending, limit=d.limit,
                            metric=Metric(op="count", filters=(Filter(field=parent_label,
                                                                       values=(name,)),))), index)
                return Resolved(kind=d.kind, problem=f"{via!r} is not a link field")
            rows = self._call(model, "read", [[rid]], {"fields": [link]})
            target = rows[0].get(link) if rows else None
            if not target:
                return Resolved(kind=d.kind,
                                problem=f"{name} has no {info.get('string', via)}")
            model, rid, name = str(info["relation"]), int(target[0]), str(target[1])
            if model not in self._models:
                self._models[model] = self._model_meta(model)
        if d.kind == "fact":
            return self._facts(model, rid, name, d.fields)
        if d.kind == "handoff":
            meta = self._models.get(model) or self._model_meta(model)
            control = d.action
            if d.fields and not any(_norm(a) == _norm(d.action) for a in meta.actions):
                # The change is to a field, and the model named the field alongside a
                # button it guessed: the field is what the person will edit.
                control = d.fields[0]
            plan = self._record_page(model, rid, name, section="", highlight=control,
                                     value=d.value)
            repair = ""
            if d.value and not plan.fill:
                field = meta.field_by_label(control) if meta is not None else None
                info = meta.fields.get(field or "", {}) if meta is not None else {}
                if info.get("type") == "many2one":
                    repair = (f"no {info.get('string') or control} is named {d.value!r}; "
                              "value must be the exact name of an existing record")
            return Resolved(kind="handoff", page=plan, repair=repair)
        plan = self._record_page(model, rid, name, section=d.section, highlight="")
        if d.section and model in self._models:
            # "Its invoice lines": the section that embeds the record's lines is shown,
            # and the lines are read out too, since a request for a record's lines
            # is usually a question about them.
            line_field = next((f for f, i in self._models[model].fields.items()
                               if i.get("type") == "one2many" and i.get("relation") in self._models
                               and _norm(i.get("string", "")) == _norm(d.section)
                               and self._embedded_lines(model, str(i["relation"])) is not None),
                              None)
            if line_field is not None:
                lines = self._lines_list(model, line_field, (model, rid, name), d, index)
                if lines.aggregate is not None:
                    return Resolved(kind="page", page=plan, aggregate=lines.aggregate)
        return Resolved(kind="page", page=plan)

    def _creation(self, d: Deliverable, entity, index: ApplicationIndex) -> Resolved:
        """"Create a new X": the control is "New" on the entity's list page -- the one
        declared when it shows this entity (Bills rather than Invoices), else the widest
        one, whose action restricts nothing (Quotations rather than Sales Orders), since
        a new record starts in the state the narrower pages exclude."""
        candidates = [a for a in self._actions if a["model"] == entity.key]
        action = next((a for a in candidates if f"p{a['action_id']}" == d.page.key
                       and a.get("breadcrumbs")), None)
        if action is None:
            action = next((a for a in candidates if a.get("breadcrumbs")
                           and not _literal_domain(str(a.get("domain") or ""))),
                          candidates[0] if candidates else None)
        page = index.page(f"p{action['action_id']}") if action else None
        if action is None or page is None:
            return Resolved(kind="handoff", problem="no page shows this entity")
        if "create" not in page.operations:
            # The catalog says this actor may not create records of this entity: the
            # control is not pointed at, and the reply says so.
            return Resolved(kind="handoff", problem="creation is not available to this actor")
        control = "New" if _norm(d.action) in ("", "new", "create") else d.action
        return Resolved(kind="handoff", page=PagePlan(
            url=self._action_url(action), label=page.breadcrumb,
            expected_path=self._action_url(action), highlight=control,
            highlight_effect="create"))

    # -- records ----------------------------------------------------------------------
    def _rehome(self, model: str, reference: str, index: ApplicationIndex):
        hits = []
        for entity in index.entities:
            meta = self._models.get(entity.key)
            if entity.key == model or meta is None or not meta.list_fields:
                continue
            links = [name for name, info in meta.fields.items()
                     if info.get("type") == "many2one" and info.get("relation") == model
                     and name in meta.list_fields + meta.form_fields]
            if not links:
                continue
            found = self._find_record(entity.key, reference)
            if isinstance(found, tuple) and not isinstance(found, Ambiguous):
                hits.append((found, str(meta.fields[links[0]].get("string") or links[0])))
        return hits[0] if len(hits) == 1 else None

    def _names_of(self, model: str, field: str, domain: list) -> list[str]:
        """The distinct names a link field has among the records `domain` describes."""
        try:
            rows = self._call(model, "search_read", [domain], {"fields": [field], "limit": 200})
        except BusinessToolError:
            return []
        names = []
        for row in rows:
            value = row.get(field)
            if isinstance(value, (list, tuple)) and len(value) == 2 and value[1] not in names:
                names.append(str(value[1]))
        return names[:20]

    def _find_record(self, model: str, reference: str):
        rows = self._call(model, "name_search", [reference], {"limit": 8})
        if not rows and _strip_code(reference) and _strip_code(reference) != reference.strip():
            # A name as the application displays it, its code in brackets first
            # ("[FURN_6741] Large Meeting Table"): the name search knows the name alone.
            code = re.match(r"^\[([^\]]*)\]", reference.strip())
            rows = [(rid, name) for rid, name in self._call(
                model, "name_search", [_strip_code(reference)], {"limit": 8})
                    if not code or name.startswith(f"[{code.group(1)}]") or "[" not in name]
            if not rows:
                return None
            reference = _strip_code(reference)
        if not rows:
            return None
        wanted = _norm(reference)
        exact = [(rid, name) for rid, name in rows
                 if _norm(_strip_code(name)) == wanted or _norm(name) == wanted]
        if len(exact) == 1 or (exact and all(n == exact[0][1] for _r, n in exact)):
            rid, name = exact[0]
            return model, int(rid), str(name)
        if len(rows) == 1:
            rid, name = rows[0]
            return model, int(rid), str(name)
        prefix = [(rid, name) for rid, name in rows
                  if _norm(_strip_code(name)).startswith(wanted)]
        if len(prefix) == 1:
            rid, name = prefix[0]
            return model, int(rid), str(name)
        # Contacts of a company carry the company's name first: the company is meant.
        companies = [(rid, name) for rid, name in rows if "," not in str(name)]
        if len(companies) == 1:
            rid, name = companies[0]
            return model, int(rid), str(name)
        return Ambiguous(str(name) for _rid, name in rows[:6])

    def _facts(self, model: str, rid: int, name: str, labels: tuple[str, ...]) -> Resolved:
        meta = self._models.get(model) or self._model_meta(model)
        self._models.setdefault(model, meta)
        fields, missing = [], []
        for label in labels:
            field = meta.field_by_label(label)
            if field is None:
                missing.append(label)
            elif field not in fields:
                fields.append(field)
        if not fields:
            lines = self._line_facts(model, rid, name, missing)
            if lines is not None:
                return lines
            return Resolved(kind="fact",
                            problem="none of the fields asked for exist on this record",
                            repair=(f"{meta.label} has no field named "
                                    + ", ".join(repr(m) for m in missing)
                                    + "; its fields are: " + (_fields_of(self.index(), model)
                                                              or ", ".join(
                                        str(i.get("string") or f)
                                        for f, i in list(meta.fields.items())[:30]))))
        read_fields = list(fields)
        if any(meta.fields[f].get("type") == "monetary" for f in fields) \
                and "currency_id" in meta.fields:
            read_fields.append("currency_id")
        rows = self._call(model, "read", [[rid]], {"fields": read_fields})
        row = rows[0] if rows else {}
        facts = []
        for field in fields:
            info = meta.fields[field]
            facts.append(Fact(subject=name, label=str(info.get("string") or field),
                              value=self._display(row.get(field), info, row)))
        problem = ""
        if missing:
            problem = "not on this record: " + ", ".join(missing)
        return Resolved(kind="fact", facts=tuple(facts), problem=problem)

    def _display(self, value, info: dict, row: dict) -> str:
        kind = info.get("type")
        if kind == "boolean":
            # A flag that is off is a "no", not a missing value.
            return "Yes" if value else "No"
        if value is False or value is None or value == "":
            return "(empty)"
        if kind == "many2one":
            return str(value[1]) if isinstance(value, (list, tuple)) and len(value) > 1 else str(value)
        if kind == "selection":
            for key, label in info.get("selection") or ():
                if key == value:
                    return str(label)
            return str(value)
        if kind == "monetary":
            currency = row.get("currency_id")
            code = currency[1] if isinstance(currency, (list, tuple)) and len(currency) > 1 else ""
            return _money(float(value), code)
        if kind == "float":
            return f"{float(value):,.2f}"
        if kind == "html":
            return _plain_text(str(value)) or "(empty)"
        if kind in ("one2many", "many2many"):
            ids = [int(v) for v in value][:10] if isinstance(value, (list, tuple)) else []
            names = []
            if ids and info.get("relation"):
                rows = self._call(str(info["relation"]), "read", [ids], {"fields": ["display_name"]})
                names = [str(r.get("display_name") or "") for r in rows]
            total = len(value) if isinstance(value, (list, tuple)) else 0
            suffix = "…" if total > len(names) else ""
            return f"{total} records: " + ", ".join(names) + suffix if names else f"{total} records"
        return str(value)

    # -- pages ------------------------------------------------------------------------
    def _action_url(self, action: dict, rid: int | None = None) -> str:
        route = str(action.get("route") or "")
        base = f"/odoo/{route}" if route and not route.startswith("action-") else (
            f"/odoo/action-{action['action_id']}")
        return f"{base}/{rid}" if rid is not None else base

    def _default_page(self, model: str, index: ApplicationIndex) -> IndexPage | None:
        for action in self._actions:
            if action["model"] == model:
                return index.page(f"p{action['action_id']}")
        return None

    def _fill_for(self, meta: _ModelMeta, field: str, value: str) -> tuple[str, str, str]:
        """(what to put in, how, its visible label) for a value the person gave a field,
        or empty when the value cannot be typed with certainty: a date at day precision,
        a number, a choice the field offers, the exact name of one existing record, or
        plain text. A checkbox and a value that names no record are left to the person."""
        info = meta.fields.get(field, {})
        kind = info.get("type")
        value = value.strip()
        if not value:
            return "", "", ""
        if kind in ("date", "datetime"):
            period = _period(value, self._today())
            if period is None or period[0] is None or period[1] is None \
                    or (period[1] - period[0]).days != 1:
                return "", "", ""
            return period[0].isoformat(), "text", period[0].isoformat()
        if kind == "selection":
            wanted = _norm(value)
            for key, label in info.get("selection") or ():
                if _norm(str(label)) == wanted or key == value:
                    return str(key), "choice", str(label)
            return "", "", ""
        if kind == "many2one":
            found = self._find_record(str(info["relation"]), value)
            if not (isinstance(found, tuple) and not isinstance(found, Ambiguous)):
                return "", "", ""
            return found[2], "link", found[2]
        if kind in ("integer", "float", "monetary"):
            try:
                float(value.replace(",", ""))
            except ValueError:
                return "", "", ""
            return value.replace(",", ""), "text", value
        if kind in ("char", "text", "html"):
            return value, "text", value
        return "", "", ""

    def _record_page(self, model: str, rid: int, name: str, *, section: str,
                     highlight: str, value: str = "") -> PagePlan:
        meta = self._models.get(model)
        candidates = [a for a in self._actions
                      if a["model"] == model and "form" in (a.get("view_modes") or ())]
        candidates.sort(key=lambda a: (str(a.get("route") or "").startswith("action-"),
                                       not a.get("breadcrumbs")))
        chosen = None
        for action in candidates:
            if not action.get("breadcrumbs"):
                # Without a menu page the record's own route is the address people see.
                continue
            domain = _literal_domain(str(action.get("domain") or ""))
            if domain is None:
                continue
            if domain:
                if not self._call(model, "search_count", [[["id", "=", rid]] + domain], {}):
                    continue
            chosen = action
            break
        url = self._action_url(chosen, rid) if chosen else f"/odoo/{model}/{rid}"
        tab = ""
        if section and meta is not None:
            wanted = _norm(section)
            for item in meta.sections:
                if _norm(item) == wanted or wanted in _norm(item):
                    tab = item
                    break
            tab = tab or section
        control = highlight
        fill = ("", "", "")
        fill_field = ""
        if highlight and meta is not None:
            wanted = _norm(highlight)
            for item in meta.actions:
                if _norm(item) == wanted:
                    control = item
                    break
            else:
                # Not a button: the field the person wants to change, named loosely
                # ("Expiration Date" for Expiration, "Invoice Date" for Invoice/Bill Date).
                asked = _tokens(highlight)
                field = next((n for n, i in meta.fields.items()
                              if _norm(str(i.get("string") or "")) == wanted), None)
                if field is None:
                    field = next((n for n, i in meta.fields.items()
                                  if n in meta.form_fields and asked
                                  and asked <= _tokens(str(i.get("string") or ""))), None)
                if field is None:
                    field = meta.field_by_label(highlight)
                if field is not None and field in meta.form_fields:
                    control = str(meta.fields[field].get("string") or field)
                    if not tab:
                        # A field inside a tab is reached by selecting that tab first.
                        tab = _tab_of(meta.form_arch, field)
                    if value:
                        fill = self._fill_for(meta, field, value)
                        fill_field = field if fill[0] else ""
        label = name if not tab else f"{name} · {tab}"
        return PagePlan(url=url, label=label, expected_path=url, section=tab,
                        highlight=control, fill=fill[0], fill_kind=fill[1], fill_label=fill[2],
                        fill_field=fill_field)

    def _list_page(self, d: Deliverable, index: ApplicationIndex) -> Resolved:
        page = index.page(d.page.key) if d.page.key else None
        if page is None and d.entity:
            page = self._default_page(d.entity, index)
        if page is None:
            return Resolved(kind="page", problem="the page named is not in the index")
        if d.page.search and d.kind == "page":
            # "Show me Large Desk's card": the words name one record of the page's entity.
            # With a section asked for, a record is certainly meant, and the one record
            # the words pick out is it ("Azure" for Azure Interior).
            found = self._find_record(page.entity, d.page.search)
            if (isinstance(found, tuple) and not isinstance(found, Ambiguous)
                    and (d.section or _norm(_strip_code(found[2])) == _norm(d.page.search))):
                if page.entity not in self._models:
                    self._models[page.entity] = self._model_meta(page.entity)
                return Resolved(kind="page", page=self._record_page(
                    found[0], found[1], found[2], section=d.section, highlight=""))
            if found is None and not self._call(page.entity, "search_count",
                                                [[["display_name", "ilike", d.page.search]]], {}):
                # Nothing on this page is named so: opening the list would show the
                # person something else and say the words were found.
                return Resolved(kind="page", problem=f"no {page.label} record matches {d.page.search!r}",
                                repair=(f"no {page.label} record is named {d.page.search!r}; when the "
                                        "words name a related record (a customer, a product), put "
                                        "them in metric.filters on the field that links to it, and "
                                        "leave the search empty"))
        action = next((a for a in self._actions if f"p{a['action_id']}" == page.key), None)
        if action is None:
            return Resolved(kind="page", problem="the page named is not in the index")
        url = self._action_url(action)
        view = d.page.view
        if view and view not in page.views:
            view = ""
        # A facet the index does not list may still exist on the live page (the page's
        # own search view); it is passed through by name and settled by the runtime.
        on = [self._facet_name(page, name) or name for name in d.page.filters_on]
        if d.page.group_by:
            on.append(self._facet_name(page, d.page.group_by, group=True) or d.page.group_by)
        off = [self._facet_name(page, name) or name for name in d.page.filters_off]
        problem = ""
        if d.page.view and not view:
            problem = f"this page has no {d.page.view} view"
        expected = url
        if view:
            url = f"{url}?view_type={view}"
        return Resolved(kind="page", problem=problem, page=PagePlan(
            url=url, label=page.breadcrumb, expected_path=expected, view=view,
            facets_on=tuple(dict.fromkeys(on)), facets_off=tuple(dict.fromkeys(off)),
            missing_view=d.page.view if problem else ""))

    @staticmethod
    def _facet_name(page: IndexPage, label: str, *, group: bool = False) -> str | None:
        wanted = _norm(label)
        pool = page.groups if group else page.filters
        for item in pool:
            if _norm(item) == wanted:
                return item
        for item in pool:
            if wanted and (wanted in _norm(item) or _norm(item) in wanted):
                return item
        if not group:
            for item in page.groups:
                if _norm(item) == wanted:
                    return item
        return None

    # -- aggregates -------------------------------------------------------------------
    def _scope(self, d: Deliverable, index: ApplicationIndex, *, search: bool):
        """The records a deliverable's page, filters and conditions describe.

        Returns a `_Scope`, or a `Resolved` problem when a name in the declaration has no
        meaning here. The page scopes by its own domain and default filters (except ones
        about the current user); an entity alone means every record of it the person may
        read. Page filters switched on narrow, ones switched off exclude; conditions are
        field comparisons; search words, when asked for, are what the search box would do.
        """
        page = index.page(d.page.key) if d.page.key else None
        entity = index.entity(d.entity) if d.entity else None
        if page is None and entity is None:
            return Resolved(kind=d.kind, problem="no entity or page names this scope")
        prefix = ""
        if page is not None and entity is not None and page.entity != entity.key:
            parent = self._parent_link(entity.key)
            if parent is not None and parent[0] == page.entity:
                # The lines of the records a page shows (the product lines of vendor
                # refunds): the page's scope reaches the lines through their link.
                prefix = parent[1] + "."
            else:
                # The lines of an order declared with the orders page: the entity named
                # is the one asked about; the page was the way there, not the scope.
                page = None
        model = page.entity if page is not None and not prefix else entity.key
        meta = self._models.get(model) or self._model_meta(model)
        self._models.setdefault(model, meta)
        today = self._today()
        action = (next((a for a in self._actions if f"p{a['action_id']}" == page.key), None)
                  if page is not None else None)

        def through(clauses):
            return [[prefix + c[0], c[1], c[2]] if prefix and isinstance(c, (list, tuple))
                    and len(c) == 3 else c for c in clauses]

        domain = through(_literal_domain(str(action.get("domain") or "")) or []) if action else []
        if prefix and not self._line_scope:
            embedded = self._embedded_lines(page.entity, entity.key)
            if embedded is not None and embedded[1]:
                self._line_scope = list(embedded[1])
        if self._line_scope:
            # The lines as the record's form embeds them (product lines, not tax lines).
            domain += self._line_scope
            self._line_scope = None
        described = [page.label if page is not None else meta.label]
        context = _literal_dict(str(action.get("context") or "")) if action else {}
        default_clauses: list = []
        for key, value in context.items():
            if not (str(key).startswith("search_default_") and value):
                continue
            pool = self._page_filters.get(action["action_id"], meta.filters) if action \
                else meta.filters
            name = key[len("search_default_"):]
            item = next((f for f in pool if f.name == name), None)
            resolved = item.domain_at(today) if item is not None else None
            if resolved is None and item is None:
                # The action's own search view was not served to this user, but the
                # default names a choice of one of the model's selection fields, which
                # is how these filters are conventionally written ("in_invoice").
                page_meta = self._models.get(page.entity, meta) if page is not None else meta
                resolved = next(([[field, "=", name]] for field, info in page_meta.fields.items()
                                 if info.get("type") == "selection"
                                 and any(k == name for k, _l in info.get("selection") or ())),
                                None)
            if resolved and "uid" not in str(resolved):
                domain += through(resolved)
                default_clauses += through(resolved)
        fixed = False
        scopes = [c for c in self._named_scopes if c.entity == model] if not prefix else []
        if scopes:
            # The words the request says, as this organisation means them ("订单" is every
            # order, quotations included; "报价" the ones not yet confirmed): the records
            # they name are never left out by the page the declaration went through. The
            # page's own scope stays, widened by what the words name.
            branches = []
            for convention in scopes:
                clauses: list | None = []
                for label, values in convention.conditions:
                    clause, _text = self._condition(meta, label, values, today=today)
                    if clause is None:
                        clauses = None
                        break
                    clauses += clause
                if clauses is not None:
                    branches.append(clauses)
            if branches:
                if any(not branch for branch in branches) or (page is not None and not domain):
                    domain = []
                elif page is not None:
                    domain = _any_of([domain] + branches)
                else:
                    domain = _any_of(branches)
                default_clauses, fixed = [], True
                described.append("as " + " / ".join(c.terms[0] for c in scopes) + " means here")
        metric = d.metric or Metric(op="count")
        page_filters = self._page_filters.get(action["action_id"], meta.filters) if action \
            else meta.filters
        declared_page_filters = list(metric.page_filters) + [
            f for f in d.page.filters_on if f not in metric.page_filters]
        for name in declared_page_filters:
            wanted = _norm(name)
            item = next((f for f in page_filters if _norm(f.label) == wanted), None) \
                or meta.filter_by_name(name)
            if item is not None and item.date_field and item.domain is None:
                # A date filter switched on without a period narrows nothing.
                continue
            if item is None and page is not None and not page.filters:
                # The page serves no filters, so the name is the model's own words for
                # the page's scope ("Vendor Bills" on Bills), which the page already has.
                continue
            resolved = item.domain_at(today) if item is not None else None
            if resolved is None or "uid" in str(resolved):
                return Resolved(kind=d.kind, problem=f"filter {name!r} is not usable here",
                                repair=(f"{name!r} is not a filter of this page; its filters "
                                        "are: " + ", ".join(f.label for f in page_filters[:20])
                                        + "; a condition on a field goes in metric.filters"))
            domain += through(resolved)
            described.append(item.label)
        for name in d.page.filters_off:
            # A filter switched off excludes what it would select ("Can be Sold" on,
            # "Can be Purchased" off): its clauses, negated as one expression.
            wanted = _norm(name)
            item = next((f for f in page_filters if _norm(f.label) == wanted), None) \
                or meta.filter_by_name(name)
            resolved = item.domain_at(today) if item is not None else None
            if resolved is None or "uid" in str(resolved):
                return Resolved(kind=d.kind, problem=f"filter {name!r} is not usable here")
            clauses = through([c for c in resolved if isinstance(c, (list, tuple))])
            if any(c in ("|", "!") for c in resolved) or not clauses:
                return Resolved(kind=d.kind, problem=f"filter {name!r} cannot be excluded")
            domain += ["!"] + ["&"] * (len(clauses) - 1) + clauses
            described.append(f"not {item.label}")
        for condition in metric.filters:
            if not condition.values:
                # A field named with nothing to compare against says nothing.
                continue
            exact_field = any(_norm(i.get("string", "")) == _norm(condition.field)
                              and i.get("searchable") is not False
                              for i in meta.fields.values())
            if not exact_field:
                # A date filter's name ("Accounting Date") stands for the field it offers.
                dated = next((f for f in page_filters + meta.filters
                              if f.date_field in meta.fields and _norm(f.label) == _norm(condition.field)),
                             None)
                if dated is not None:
                    condition = Filter(field=str(meta.fields[dated.date_field].get("string")
                                                 or dated.date_field),
                                       values=condition.values, op=condition.op)
                    exact_field = True
            if not exact_field:
                # Not a field by its exact label: perhaps the page's own filter, named by
                # its label or under a generic word ("filter": "To pay").
                named = [f for f in page_filters
                         if any(_norm(f.label) == _norm(v) or (len(_norm(v)) >= 4
                                and _norm(f.label).startswith(_norm(v).rstrip("y")))
                                for v in condition.values)]
                if named and named[0].domain_at(today) is not None:
                    domain += named[0].domain_at(today)
                    described.append(named[0].label)
                    continue
            clause, text = self._condition(meta, condition.field, condition.values,
                                           op=condition.op, today=today)
            if clause is None:
                named_field = meta.field_by_label(condition.field)
                if (named_field is not None and text.startswith("no ")
                        and meta.fields[named_field].get("type") == "many2one"):
                    # No record by that name: the names this field actually has among the
                    # records the other conditions describe (the products on S00010's
                    # lines), for the person's words to be matched against.
                    scope = list(domain)
                    for other in metric.filters:
                        if other is condition or not other.values:
                            continue
                        other_clause, _ = self._condition(meta, other.field, other.values,
                                                          op=other.op, today=today)
                        if other_clause:
                            scope += other_clause
                    names = self._names_of(meta.model, named_field, scope)
                    if names:
                        return Resolved(kind=d.kind, problem=text, repair=(
                            f"{text}; the {condition.field} values here are: "
                            + ", ".join(names) + "; use the one the person means, written "
                            "exactly as it is here"))
                if meta.field_by_label(condition.field) is None:
                    repair = (f"{condition.field!r} is not a field of {meta.label}; its fields "
                              "are: " + (_fields_of(index, model) or ", ".join(
                                  str(i.get("string") or f) for f, i in list(meta.fields.items())[:30])))
                else:
                    repair = (f"the value {' / '.join(condition.values)!r} cannot be used on "
                              f"{condition.field}: {text}; use a choice label from the index, "
                              "a number, or a date (2026-09-12, 2026-09, last month)")
                return Resolved(kind=d.kind, problem=text, repair=repair)
            domain += clause
            described.append(text)
        # Search words within a condition's value, or holding it ("Gemini" beside the
        # customer Gemini Furniture), say again what the condition says.
        said = {_norm(v) for c in metric.filters for v in c.values if _norm(v)} | (
            {_norm(d.record)} if d.record else set())
        words = _norm(d.page.search)
        if search and d.page.search and not any(words in v or v in words for v in said):
            clause, text = self._search_clause(meta, model, domain, d.page.search)
            if clause is None and default_clauses:
                # Nothing within the page's default filters (customers only): the words
                # may still name something the page's entity holds once those are lifted.
                lifted = [c for c in domain if c not in default_clauses]
                clause, text = self._search_clause(meta, model, lifted, d.page.search)
                if clause is not None:
                    domain, default_clauses = lifted, []
                    described.append("beyond the page's default filters")
            domain += clause or [["display_name", "ilike", d.page.search]]
            described.append(text)
        return _Scope(model=model, meta=meta, page=page, action=action, domain=domain,
                      described=described, default_clauses=default_clauses, fixed=fixed)

    def _locate(self, d: Deliverable, index: ApplicationIndex):
        """The one record a description picks out ("the quotation sent to Joel Willis",
        "last month's customer invoice"): exactly one record within the scope, or, when
        the page's own defaults or domain hide it, exactly one beyond them. Anything else
        is a problem the reply states, never a guess."""
        scope = self._scope(d, index, search=True)
        if isinstance(scope, Resolved):
            return scope
        meta, model = scope.meta, scope.model
        subject = " · ".join(scope.described)
        domains = [list(scope.domain)]
        if scope.default_clauses:
            domains.append([c for c in scope.domain if c not in scope.default_clauses])
        page_domain = _literal_domain(str(scope.action.get("domain") or "")) if scope.action else None
        if page_domain and not scope.fixed:
            domains.append([c for c in domains[-1] if c not in page_domain])
        rows: list = []
        kwargs: dict = {"fields": ["display_name"], "limit": 4}
        if d.sort:
            # "The higher one", "the latest": ordered, the first is the one meant.
            order_field = meta.field_by_label(d.sort)
            if order_field is None:
                return Resolved(kind=d.kind, problem=f"cannot sort by {d.sort!r}")
            kwargs = {"fields": ["display_name"], "limit": 1,
                      "order": f"{order_field} {'desc' if d.descending else 'asc'}, id"}
        for domain in domains:
            rows = self._call(model, "search_read", [domain], kwargs)
            if rows:
                break
        if not rows and d.page.search and (d.metric and d.metric.filters or d.page.filters_on):
            # The words matched nothing but the conditions may ("order dated" beside a
            # date): the conditions alone.
            return self._locate(replace(d, page=replace(d.page, search="")), index)
        if not rows:
            elsewhere = self._other_link(d, meta, index) or self._elsewhere(d, meta, index)
            if elsewhere is not None:
                return elsewhere
            names = self._names_repair(model, meta) if any(
                _foreign(v) for c in (d.metric.filters if d.metric else ()) for v in c.values) else ""
            return Resolved(kind=d.kind, problem=f"no {meta.label} matches {subject}",
                            repair=(f"no {meta.label} matches {subject}; re-read the request: "
                                    "another page scope (invoices versus bills, quotations "
                                    "versus orders), field or value may be meant"
                                    + (f"; {names}" if names else "")))
        if len(rows) > 1:
            names = tuple(str(r.get("display_name") or "") for r in rows)
            return Resolved(kind=d.kind, problem=f"{subject} matches several {meta.label} records",
                            candidates=names,
                            repair=(f"{subject} matches several records ({', '.join(names)}); "
                                    "add a condition the request gives (amount, date, status, "
                                    "customer); when it gives none, leave them for the person "
                                    "to choose"))
        return model, int(rows[0]["id"]), str(rows[0].get("display_name") or "")

    def _other_link(self, d: Deliverable, meta: _ModelMeta, index: ApplicationIndex):
        """A description that fits nothing, with a name put on one link of the entity
        ("Joel's quotation" read as Joel the salesperson, when Joel is the customer): the
        one record it fits with that name on another link the index shows for the
        entity. Several such readings, or none, find nothing here."""
        conditions = list(d.metric.filters) if d.metric else []
        entity = index.entity(meta.model)
        if entity is None or not conditions:
            return None
        links = [f.label for f in entity.fields if f.kind == "link"]
        found: dict[int, tuple] = {}
        for i, condition in enumerate(conditions):
            name = meta.field_by_label(condition.field)
            if name is None or (meta.fields.get(name) or {}).get("type") != "many2one":
                continue
            for label in links:
                other = meta.field_by_label(label)
                if other is None or other == name:
                    continue
                swapped = replace(d, metric=replace(d.metric, filters=tuple(
                    conditions[:i]) + (replace(condition, field=label),) + tuple(conditions[i + 1:])))
                scope = self._scope(swapped, index, search=True)
                if isinstance(scope, Resolved):
                    continue
                try:
                    rows = self._call(meta.model, "search_read", [scope.domain],
                                      {"fields": ["display_name"], "limit": 2})
                except BusinessToolError:
                    continue
                if len(rows) == 1:
                    found[int(rows[0]["id"])] = (meta.model, int(rows[0]["id"]),
                                                 str(rows[0].get("display_name") or ""))
        return next(iter(found.values())) if len(found) == 1 else None

    def _elsewhere(self, d: Deliverable, meta: _ModelMeta, index: ApplicationIndex):
        """A record described by who, how much and when (Gemini's one of 1,541.50) that
        no record of the declared entity fits: the one record of another document entity
        the same description fits exactly. A person says 那张单 of an order as readily as
        of an invoice; the conditions, not the word, tell them apart. A description that
        needs a field the other entity lacks, or that several records there fit, finds
        nothing here."""
        conditions = [c for c in (d.metric.filters if d.metric else ()) if c.values]
        if len(conditions) < 2 or d.page.search:
            return None
        found = []
        for entity in index.entities:
            other = self._models.get(entity.key)
            if (other is None or entity.key == meta.model or entity.lines_of or other.parent
                    or self._default_page(entity.key, index) is None):
                continue
            mapped = _mapped_conditions(conditions, meta, other)
            if mapped is None:
                continue
            domain: list | None = []
            for condition in mapped:
                clause, _text = self._condition(other, condition.field, condition.values,
                                                op=condition.op)
                if clause is None:
                    domain = None
                    break
                domain += clause
            if not domain:
                continue
            try:
                rows = self._call(entity.key, "search_read", [domain],
                                  {"fields": ["display_name"], "limit": 2})
            except BusinessToolError:
                continue
            if len(rows) > 1:
                return None
            if rows:
                found.append((entity.key, int(rows[0]["id"]), str(rows[0].get("display_name") or "")))
        return found[0] if len(found) == 1 else None

    def _names_repair(self, model: str, meta: _ModelMeta | None) -> str:
        """Every name of a small entity, for a request that describes one of them in
        another language than the application's ("带屏风的桌架" for a product called
        "Desk Stand with Screen"): the declarer picks the name, the names come from here."""
        if meta is None:
            return ""
        try:
            count = self._call(model, "search_count", [[]], {})
            if not count or count > _NAMES_LISTED:
                return ""
            rows = self._call(model, "search_read", [[]],
                              {"fields": ["display_name"], "limit": _NAMES_LISTED, "order": "id"})
        except BusinessToolError:
            return ""
        names = [str(r.get("display_name") or "") for r in rows if r.get("display_name")]
        return (f"the {meta.label} records here are: " + "; ".join(names)
                + "; if the person described one of them in their own words, declare the same "
                  "deliverable again with that name, exactly as it is here, as its record")

    # -- aggregates -------------------------------------------------------------------
    def _aggregate(self, d: Deliverable, index: ApplicationIndex) -> Resolved:
        scope = self._scope(d, index, search=d.kind == "list")
        if isinstance(scope, Resolved):
            return scope
        model, meta, page, action = scope.model, scope.meta, scope.page, scope.action
        domain, described, default_clauses = scope.domain, scope.described, scope.default_clauses
        metric = d.metric or Metric(op="count")
        subject = " · ".join(described)
        if d.kind == "list" and default_clauses and not self._call(model, "search_count", [domain], {}):
            # The page's own default filters (customers only) hid every match; the
            # person asked about the page's entity, so the answer says so and looks past
            # the defaults.
            for clause in default_clauses:
                domain.remove(clause)
            described.append("beyond the page's default filters")
        if d.kind == "list":
            columns = [f for f in (meta.field_by_label(label) for label in d.fields) if f]
            columns = list(dict.fromkeys(columns))
            read_fields = ["display_name"] + columns
            if any(meta.fields[f].get("type") == "monetary" for f in columns) \
                    and "currency_id" in meta.fields:
                read_fields.append("currency_id")
            limit = d.limit or _RECORD_LIMIT
            kwargs: dict = {"fields": read_fields, "limit": limit + 1}
            if d.sort:
                order_field = meta.field_by_label(d.sort)
                if order_field is None:
                    return Resolved(kind=d.kind, problem=f"cannot sort by {d.sort!r}")
                kwargs["order"] = f"{order_field} {'desc' if d.descending else 'asc'}, id"
                described.append(f"by {meta.fields[order_field].get('string', d.sort)}"
                                 + (" desc" if d.descending else ""))
            rows = self._call(model, "search_read", [domain], kwargs)
            names = []
            for row in rows[:limit]:
                cells = [f"{meta.fields[f].get('string') or f}: {self._display(row.get(f), meta.fields[f], row)}"
                         for f in columns]
                names.append(str(row.get("display_name") or "")
                             + (" — " + "; ".join(cells) if cells else ""))
            total = self._call(model, "search_count", [domain], {})
            if len(rows) == 1:
                self._found_one = (model, int(rows[0]["id"]), str(rows[0].get("display_name") or ""))
            # The list is the deliverable; the page it opens afterwards carries no facets,
            # whose failure would otherwise mark a correct answer as unfinished.
            bare = PageSpec(key=d.page.key if page is not None else "")
            plan = self._list_page(Deliverable(kind="page", entity=model, page=bare), index) \
                if (page is not None or self._default_page(model, index) is not None) else None
            page_plan = plan.page if plan is not None else None
            if not names:
                return Resolved(kind="list", page=page_plan,
                                problem=f"no {meta.label} matches "
                                        + (repr(d.page.search) if d.page.search else "these conditions"))
            return Resolved(kind="list", page=page_plan,
                            aggregate=Aggregate(subject=" · ".join(described), count=int(total),
                                                records=tuple(names),
                                                complete=len(rows) <= limit))
        count = int(self._call(model, "search_count", [domain], {}))
        widened = None
        if count == 0 and default_clauses:
            for clause in default_clauses:
                domain.remove(clause)
            count = int(self._call(model, "search_count", [domain], {}))
            if count:
                described.append("beyond the page's default filters")
        if count == 0 and action and _literal_domain(str(action.get("domain") or "")):
            # Nothing in this page's scope; the same conditions over the whole entity say
            # whether the records exist at all (quotations, when the page shows orders).
            outer = [clause for clause in domain
                     if clause not in _literal_domain(str(action.get("domain") or ""))]
            widened = int(self._call(model, "search_count", [outer], {}))
        sums: list[tuple[str, str]] = []
        if metric.op == "sum":
            field = meta.field_by_label(metric.field) if metric.field else None
            info = meta.fields.get(field or "", {})
            if field is None or info.get("type") not in ("monetary", "float", "integer"):
                return Resolved(kind=d.kind, problem=f"{metric.field!r} cannot be summed")
            groups = self._call(model, "read_group", [domain, [f"{field}:sum"], []],
                                {"lazy": False})
            total = float((groups[0].get(field) if groups else 0) or 0.0)
            currency = ""
            if info.get("type") == "monetary" and count:
                sample = self._call(model, "search_read", [domain], {
                    "fields": ["currency_id"], "limit": 1})
                value = sample[0].get("currency_id") if sample else None
                currency = value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else ""
            sums.append((str(info.get("string") or field), _money(total, currency)))
        groups_out: list[tuple[str, int]] = []
        if metric.group_by:
            field = meta.field_by_label(metric.group_by)
            if field is None:
                return Resolved(kind=d.kind, problem=f"cannot group by {metric.group_by!r}")
            spec = f"{field}:month" if meta.fields[field].get("type") in ("date", "datetime") \
                else field
            rows = self._call(model, "read_group", [domain, [field], [spec]], {
                "lazy": False, "orderby": "__count desc",
                "limit": metric.top or _GROUP_LIMIT})
            for row in rows:
                value = row.get(spec, row.get(field))
                name = (value[1] if isinstance(value, (list, tuple)) and len(value) > 1
                        else str(value))
                groups_out.append((str(name), int(row.get("__count") or 0)))
        note = ""
        if widened:
            note = f"{meta.label}, any status: {widened}"
        return Resolved(kind="aggregate", aggregate=Aggregate(
            subject=subject, count=count, sums=tuple(sums), groups=tuple(groups_out),
            note=note))

    def _search_clause(self, meta: _ModelMeta, model: str, domain: list, words: str):
        """What search words mean on this entity within `domain`, or None when nothing."""
        today = self._today()
        if _period(words, today) is not None:
            shown_dates = [f for f in dict.fromkeys(meta.list_fields + meta.form_fields)
                           if meta.fields.get(f, {}).get("type") in ("date", "datetime")]
            if shown_dates:
                field = shown_dates[0]
                clause, text = self._date_condition(field, meta.fields[field], (words,), "", today)
                if clause is not None:
                    return clause, text
        clause = [["display_name", "ilike", words]]
        if self._call(model, "search_count", [domain + clause], {}):
            return clause, f"~{words}"
        number = re.fullmatch(r"[0-9][0-9,]*(?:\.[0-9]+)?", words.strip())
        if number:
            # A bare number that names nothing is an amount (an order's total).
            field = self._amount_field(meta, words.strip(), domain)
            if field is not None:
                return ([[field, "=", float(words.replace(",", ""))]],
                        f"{meta.fields[field].get('string')} = {words}")
        # No name contains the words; they may name a linked record instead ("United
        # States" on customers is their country). Only a record actually named by the
        # words counts, and several links to the same record are one hit, on the field
        # the list shows.
        shown = list(dict.fromkeys(meta.list_fields + meta.form_fields))
        hits: dict[tuple, str] = {}
        for field, info in meta.fields.items():
            if info.get("type") != "many2one" or field not in shown:
                continue
            found = self._find_record(str(info["relation"]), words)
            # A contact of a company is displayed "Company, Person"; the person's own name
            # is the last part, as `locate_reference` reads it.
            if (isinstance(found, tuple) and not isinstance(found, Ambiguous)
                    and _norm(words) in (_norm(_strip_code(found[2])),
                                         _norm(str(found[2]).rsplit(",", 1)[-1]))):
                key = (found[0], found[1])
                if key not in hits or (field in meta.list_fields and hits[key] not in meta.list_fields):
                    hits[key] = field
        if len(hits) == 1:
            (_m, rid), field = next(iter(hits.items()))
            operator = "child_of" if meta.fields[field].get("relation") == "res.partner" else "in"
            clause = [[field, operator, [rid]]]
            if self._call(model, "search_count", [domain + clause], {}):
                return clause, f"{meta.fields[field].get('string') or field} = {words}"
        # Or the words are in one text field the pages show (a city).
        texts = [f for f in shown if meta.fields.get(f, {}).get("type") == "char"
                 and f != "name" and meta.fields[f].get("searchable") is not False]
        matched = [f for f in texts
                   if self._call(model, "search_count", [domain + [[f, "ilike", words]]], {})]
        if len(matched) == 1:
            return [[matched[0], "ilike", words]], f"{meta.fields[matched[0]].get('string')} ~ {words}"
        return None, f"~{words}"

    def _condition(self, meta: _ModelMeta, label: str, values: tuple[str, ...], *,
                   op: str = "", today: date | None = None):
        field = meta.field_by_label(label)
        if field is None:
            return None, f"no field {label!r} on {meta.label}"
        info = meta.fields[field]
        kind = info.get("type")
        text = f"{info.get('string') or field} {op or '='} {' / '.join(values)}"
        if not values:
            return None, f"no value for {label!r}"
        if info.get("searchable") is False:
            # A computed, unstored field: the application would silently ignore the
            # condition and count everything, which is worse than saying no.
            return None, f"{info.get('string') or field} cannot be used as a condition"
        emptiness = _norm(values[0]) if len(values) == 1 else ""
        if emptiness in _EMPTY or emptiness in _NOT_EMPTY:
            # "Lines with a product", "orders without a salesperson".
            # "not empty", or "not equal to empty": the field is set; "empty": it is not.
            wanted_set = emptiness in _NOT_EMPTY or op == "!="
            return ([[field, "!=" if wanted_set else "=", False]],
                    f"{info.get('string') or field} {'set' if wanted_set else 'not set'}")
        if kind in ("date", "datetime"):
            return self._date_condition(field, info, values, op, today or self._today())
        if op == "!=" and kind in ("many2one", "selection"):
            clause, text = self._condition(meta, label, values, today=today)
            if clause is None:
                return clause, text
            negated = [[c[0], "not in" if c[1] in ("in", "child_of") else "!=", c[2]]
                       if isinstance(c, list) and len(c) == 3 else c for c in clause]
            return negated, text.replace(" = ", " != ", 1)
        if kind == "many2one":
            # A name as the application displays it may carry its code in brackets
            # ("[FURN_0269] Office Chair Black"); the record's name is what follows.
            values = [_strip_code(v) or v for v in values]
            ids = []
            for value in values:
                found = self._find_record(str(info["relation"]), value)
                if found is None or isinstance(found, Ambiguous) or op == "contains":
                    # Not one record's name: the records whose names contain the words
                    # ("Office" among products), which the search box would also find.
                    clause = ["|"] * (len(values) - 1) + [[field, "ilike", v] for v in values]
                    if found is None and not self._call(meta.model, "search_count", [clause], {}):
                        # The words in any order, a plural as its singular ("acoustic
                        # screens" for "Acoustic Bloc Screens", "black office chair" for
                        # "Office Chair Black"): every word of each value in the name.
                        # A variant is displayed with its attributes in parentheses
                        # ("Acoustic Bloc Screens (White)"); its name is what precedes.
                        bare = [re.sub(r"\s*\([^()]*\)$", "", v) for v in values]
                        if bare != values:
                            plain = ["|"] * (len(bare) - 1) + [[field, "ilike", v] for v in bare]
                            if self._call(meta.model, "search_count", [plain], {}):
                                return plain, f"{info.get('string') or field} ~ {' / '.join(values)}"
                        worded = _worded(field, values)
                        if worded and self._call(meta.model, "search_count", [worded], {}):
                            return worded, f"{info.get('string') or field} ~ {' / '.join(values)}"
                        return None, f"no {info.get('string')} matches {value!r}"
                    return clause, f"{info.get('string') or field} ~ {' / '.join(values)}"
                ids.append(found[1])
            operator = "child_of" if info.get("relation") == "res.partner" else "in"
            return [[field, operator, ids]], text
        if kind == "selection":
            keys = []
            for value in values:
                wanted = _norm(value)
                match = next((k for k, lab in info.get("selection") or ()
                              if _norm(str(lab)) == wanted or k == value), None)
                if match is None:
                    # A choice named in the person's language, as the application
                    # translates it (已过账 for Posted).
                    forms = {_norm(e) for e in self._translations.back(value)}
                    match = next((k for k, lab in info.get("selection") or ()
                                  if _norm(str(lab)) in forms), None)
                if match is None:
                    match = next((k for k, lab in info.get("selection") or ()
                                  if wanted in _norm(str(lab))), None)
                if match is None:
                    return None, f"{value!r} is not a {info.get('string')} choice"
                keys.append(match)
            return [[field, "in", keys]], text
        if kind == "boolean":
            truthy = _norm(values[0]) in ("true", "yes", "1", "是")
            return [[field, "=", truthy]], text
        if kind in ("integer", "float", "monetary"):
            lead = re.match(r"^\s*(>=|<=|!=|>|<|=)\s*(.+)$", values[0]) if len(values) == 1 else None
            if lead and not op:
                # ">20000" written as the value.
                op, values = lead.group(1), (lead.group(2),)
            spans = [re.split(r"\s*(?:\.\.|~|到|至)\s*", v) for v in values]
            if len(values) == 1 and len(spans[0]) == 2:
                values, op = (spans[0][0], spans[0][1]), "between"
            try:
                numbers = [float(str(v).replace(",", "")) for v in values]
            except ValueError:
                return None, f"{values[0]!r} is not a number"
            if op == "between" and len(numbers) >= 2:
                return [[field, ">=", numbers[0]], [field, "<=", numbers[1]]], text
            return [[field, op if op in ("<", "<=", ">", ">=", "!=") else "=", numbers[0]]], text
        operator = {"=": "=ilike", "!=": "not ilike"}.get(op, "ilike")
        if op == "starts with":
            # A prefix: the pattern form of "equals", anchored at the start.
            operator, values = "=ilike", tuple(v.replace("%", "") + "%" for v in values)
        if len(values) == 1:
            return [[field, operator, values[0]]], text
        return ["|"] * (len(values) - 1) + [[field, operator, v] for v in values], text

    def _date_condition(self, field: str, info: dict, values: tuple[str, ...], op: str,
                        today: date):
        label = info.get("string") or field
        is_datetime = info.get("type") == "datetime"

        def bound(day: date) -> str:
            return self._utc_boundary(day) if is_datetime else day.isoformat()

        if op == "between" and len(values) >= 2:
            first, second = _period(values[0], today), _period(values[1], today)
            if first is None or second is None or first[0] is None or second[1] is None:
                return None, f"{values[0]!r}..{values[1]!r} is not a date range"
            return ([[field, ">=", bound(first[0])], [field, "<", bound(second[1])]],
                    f"{label} between {values[0]} and {values[1]}")
        period = _period(values[0], today)
        if period is None:
            return None, f"{values[0]!r} is not a date or period"
        start, end = period
        text = f"{label} {op or 'in'} {values[0]}"
        if op in ("<", "<="):
            edge = (end if op == "<=" else start)
            if edge is None:
                edge = end
            return [[field, "<", bound(edge)]], text
        if op in (">", ">="):
            edge = (start if op == ">=" else end)
            if edge is None:
                edge = start
            return [[field, ">=", bound(edge)]], text
        clauses = []
        if start is not None:
            clauses.append([field, ">=", bound(start)])
        if end is not None:
            clauses.append([field, "<", bound(end)])
        return clauses, text


#: The most names of one entity a repair lists for a description in another language.
_NAMES_LISTED = 80


def _foreign(text) -> bool:
    """Words with no Latin letter or digit: written in the person's language, where the
    application's names are not."""
    text = str(text or "")
    return bool(text.strip()) and re.search(r"[A-Za-z0-9]", text) is None


def _main_date(meta: _ModelMeta) -> str | None:
    return next((f for f in dict.fromkeys(meta.list_fields + meta.form_fields)
                 if (meta.fields.get(f) or {}).get("type") in ("date", "datetime")), None)


def _same_field(name: str, source: _ModelMeta, target: _ModelMeta) -> str | None:
    """The target's field for one of the source's: the same field (name, type and
    relation), or, for a date, the target's main date."""
    info = source.fields.get(name) or {}
    there = target.fields.get(name)
    if (there is not None and there.get("type") == info.get("type")
            and there.get("relation") == info.get("relation")):
        return name
    if info.get("type") in ("date", "datetime"):
        return _main_date(target)
    return None


def _mapped_conditions(conditions, source: _ModelMeta, target: _ModelMeta):
    out = []
    for condition in conditions:
        name = source.field_by_label(condition.field)
        there = _same_field(name, source, target) if name else None
        if there is None:
            return None
        out.append(Filter(field=str(target.fields[there].get("string") or there),
                          values=condition.values, op=condition.op))
    return out


def _mapped_fields(labels, source: _ModelMeta, target: _ModelMeta) -> tuple[str, ...]:
    out = []
    for label in labels:
        name = source.field_by_label(label)
        there = _same_field(name, source, target) if name else None
        if there is not None:
            out.append(str(target.fields[there].get("string") or there))
    return tuple(dict.fromkeys(out))


def _worded(field: str, values) -> list:
    """A condition matching a link whose name holds every word of a value, in any
    order, a plural matched as its singular; values are alternatives. Empty when a
    value has no word to match."""
    alternatives = []
    for value in values:
        words = [w for w in re.split(r"[^0-9A-Za-z]+", str(value)) if len(w) > 1]
        if not words:
            return []
        stems = [w[:-1] if w.lower().endswith("s") and len(w) > 3 else w for w in words]
        clause = ["&"] * (len(stems) - 1) + [[field, "ilike", w] for w in stems]
        alternatives.append(clause)
    out = ["|"] * (len(alternatives) - 1)
    for clause in alternatives:
        out += clause
    return out

