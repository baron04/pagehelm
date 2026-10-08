"""The core resolver: a goal declaration answered from an application's `DataPort`.

What a condition means, which record a name picks out, when a page's own defaults are
lifted, how lines relate to their document, and which record a description fits are
decided here, the same way for every application. The port only reads.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..data import (EVERYTHING, ID, NAME, Compare, Cond, DataPort, DataRefused, LinkValue, Ref, Row, Schema,
                    all_of)
from ..index import ApplicationIndex, IndexPage
from ..ports import Fact, PagePlan, Resolution, Resolved
from ..spec import Deliverable, Filter, GoalSpec, Metric
from .lines import LinesMixin
from .locate import LocateMixin
from .phrases import Phrases
from .scope import ScopeMixin
from .values import ValuesMixin
from .view import EntityView
from .words import foreign, norm, period, tokens

#: Deliverable kinds answered by reading records; one may be answered as another.
_READS = frozenset({"fact", "list", "aggregate"})
_LINK_SCOPE_LIMIT = 200


class Ambiguous(tuple):
    """Several records match a reference; the tuple holds their display names."""


class Found(tuple):
    """(entity, id, display) of the one record a reference names, with its `Ref`."""
    ref: Ref

    def __new__(cls, ref: Ref):
        self = super().__new__(cls, (ref.entity, ref.id, ref.display))
        self.ref = ref
        return self

    @property
    def personal(self) -> str:
        """The record's own part of its display, without the record it belongs to."""
        ref = self.ref
        if ref.owner and ref.display.startswith(ref.owner):
            return ref.display[len(ref.owner):].lstrip(" ,")
        return ref.display


class Resolver(ScopeMixin, LocateMixin, LinesMixin, ValuesMixin):
    """Resolves one declaration against one index and one port; not shared across turns."""

    def __init__(self, port: DataPort, index: ApplicationIndex, schema: Schema, *,
                 views: dict[str, EntityView] | None = None, today: date | None = None,
                 evidence=None):
        self.port, self.index, self.schema = port, index, schema
        self._evidence = evidence
        self._views = views if views is not None else {}
        self._reads: list[tuple[str, str]] = []
        self._named_scopes: tuple = ()
        self._chosen: frozenset = frozenset()
        self._held_by: tuple = ()
        self._line_scope: list | None = None
        self._found_one: tuple | None = None
        self._listed_rows: tuple[str, tuple[Row, ...]] | None = None
        self._today_cache: date | None = today
        #: How the scopes are described to the person (the spec's language).
        self.say = Phrases()

    # -- entry ----------------------------------------------------------------------------
    def resolve(self, spec: GoalSpec) -> Resolution:
        self._named_scopes = tuple(getattr(spec, "conventions", ()) or ())
        self.say = Phrases(getattr(spec, "language", "") or "")
        self._chosen = frozenset(norm(self.port.name_of(n) or n)
                                 for n in getattr(spec, "chosen", ()) or ())
        self._listed_rows = None
        resolved = []
        for deliverable in spec.deliverables:
            if self._evidence is not None:
                self._evidence.begin()
            previous_list, self._listed_rows = self._listed_rows, None
            try:
                result = self._linked_list_facts(deliverable, previous_list,
                    preceding_list=bool(resolved and resolved[-1].kind == "list"))
                if result is None:
                    result = self._resolve_one(deliverable)
                if (result.kind != deliverable.kind and result.kind in _READS
                        and deliverable.kind in _READS):
                    # A fact about some lines is answered by listing them; it is still the
                    # fact that was asked, and its problems go to the same repair.
                    result = replace(result, kind=deliverable.kind)
                resolved.append(result)
            except DataRefused as exc:
                resolved.append(self._refused_read(deliverable, exc))
            if self._evidence is not None:
                self._evidence.settle(resolved[-1])
        return Resolution(deliverables=tuple(resolved), reads=tuple(self._reads))

    def _linked_list_facts(self, d, previous, *, preceding_list=False):
        """Follow explicitly requested links of the preceding complete list by ID.

        No unnamed global record selection, display-name lookup, new query scope,
        navigation or mutation. An unsupported link remains a repairable failure.
        """
        if (d.kind != "fact" or not d.via or d.record
                or d.metric is not None or not d.page.empty or d.section
                or d.action or d.rules or d.value or d.field_values):
            return None
        if previous is None:
            if preceding_list:
                return Resolved(kind="fact", problem="the preceding list is not a stable complete read",
                                repair="read a bounded complete list before following its links")
            return None
        model, rows = previous
        view = self.view(model)
        links = [f for f in view.schema.fields if f.kind == "link" and f.relation == d.entity
                 and norm(d.via) in {norm(f.key), norm(f.label), *(norm(a) for a in f.aliases)}]
        if len(links) != 1:
            return Resolved(kind="fact", problem="the preceding list has no unique requested link",
                            repair="via must name one link column of the preceding list")
        key = links[0].key
        if len(rows) > 50 or any(not isinstance(row.values.get(key), LinkValue)
                               or not row.values[key].id for row in rows):
            return Resolved(kind="fact", problem="the preceding list's requested links are unread or unbounded",
                            repair="include the requested link column in a bounded complete list")
        if not rows:
            from ..ports import Aggregate
            return Resolved(kind="fact", aggregate=Aggregate(subject=view.label, count=0))
        facts = []
        for row in rows:
            link = row.values[key]
            result = self._facts(d.entity, link.id, link.display, d.fields)
            if result.problem or result.read_failure or result.denied:
                return result
            facts.extend(replace(f, subject=f"{row.display} · {link.display}") for f in result.facts)
        return Resolved(kind="fact", facts=tuple(facts))

    def _refused_read(self, d: Deliverable, exc: DataRefused) -> Resolved:
        """A read the application refused. When a condition is the cause -- a field the
        application lists as searchable but cannot search -- the condition is named and
        sent back for repair instead of ending the deliverable."""
        problem = f"the application refused a read: {exc}"[:200]
        refused = exc.reason
        if exc.permission_denied:
            return Resolved(kind=d.kind, problem=problem, refused=refused, denied="read")
        failed = Resolved(kind=d.kind, problem=problem, refused=refused,
                          read_failure="unavailable" if exc.read_failed else "refused",
                          read_operation=d.metric.op if d.kind == "aggregate" and d.metric else d.kind,
                          read_fields=d.fields or ((d.metric.field,) if d.metric and d.metric.field else ()))
        if exc.read_failed:
            return failed
        filters = d.metric.filters if d.metric else ()
        if len(filters) < 2:
            return failed
        culprits = []
        for condition in filters:
            rest = tuple(f for f in filters if f is not condition)
            try:
                self._resolve_one(replace(d, metric=replace(d.metric, filters=rest)))
            except DataRefused:
                continue
            culprits.append(condition.field)
        if len(culprits) != 1:
            return failed
        return Resolved(kind=d.kind, problem=f"{culprits[0]!r} cannot be searched",
                        repair=(f"the application cannot search on {culprits[0]!r}; drop that "
                                "condition if the request did not ask for it, or use another "
                                "field that says the same"))

    # -- data -----------------------------------------------------------------------------
    def view(self, entity: str) -> EntityView | None:
        if not entity:
            return None
        found = self._views.get(entity)
        if found is None:
            schema = self.schema.entity(entity)
            if schema is None:
                extra = getattr(self.port, "entity_schema", None)
                schema = extra(entity) if callable(extra) else None
            if schema is None:
                return None
            found = self._views[entity] = EntityView(schema, getattr(self.port, "labels_for", None))
        return found

    def today(self) -> date:
        if self._today_cache is None:
            zone = self.port.time_zone()
            try:
                self._today_cache = datetime.now(ZoneInfo(zone)).date() if zone else date.today()
            except ZoneInfoNotFoundError:
                self._today_cache = date.today()
        return self._today_cache

    def _count(self, entity: str, where: list | Cond) -> int:
        self._reads.append(("count", entity))
        return int(self.port.count(entity, _cond(where)))

    def _rows(self, entity: str, where: list | Cond, *, fields=(), order=(), limit=80) -> tuple[Row, ...]:
        self._reads.append(("rows", entity))
        return self.port.rows(entity, _cond(where), fields=tuple(fields), order=tuple(order),
                              limit=limit)

    def _read(self, entity: str, id: str, fields) -> Row:
        self._reads.append(("read", entity))
        return self.port.read(entity, id, tuple(fields))

    def _find(self, entity: str, text: str, limit: int = 8) -> tuple[Ref, ...]:
        self._reads.append(("find", entity))
        return self.port.find(entity, text, limit=limit)

    def _own_name(self, display: str) -> str:
        return self.port.name_of(display) or display

    # -- one deliverable -------------------------------------------------------------------
    def _resolve_one(self, d: Deliverable) -> Resolved:
        index = self.index
        page = index.page(d.page.key) if d.page.key else None
        if page is not None and not page.entity:
            if not navigation_only(d):
                return navigation_refused(d, page)
            return self._list_page(d)
        if d.field_values:
            # Only an integration's reviewed form port can translate a batch; the
            # ordinary data resolver must not silently turn it into navigation.
            return Resolved(kind=d.kind, unoffered="fill",
                            problem="this application does not offer a reviewed form batch")
        if (d.kind == "handoff" and not d.action.strip() and not d.fields
                and (d.record or _describes_record(d) or d.section)):
            return Resolved(kind="handoff", problem="no control or field was declared for the handoff",
                            refused=("没有确定要准备的操作或字段，请说明要做什么" if self.say.zh else
                                     "No operation or field to prepare was determined; please specify the goal"),
                            repair="name the requested control or field in action; tab navigation is a page deliverable")
        d = self._line_record_from_conditions(d)
        if isinstance(d, Resolved):
            return d
        if d.kind == "page" and not d.record and d.page.key and not (d.metric and d.metric.filters):
            return self._list_page(d)
        if d.kind == "list" and d.record and d.via:
            # "The lines of S00004", declared on the line entity with the parent named as
            # the record and the link field as via: a list with that link as condition.
            # A via that is not a field of the line entity means its parent link.
            line_view = self.view(d.entity) if d.entity else None
            via = d.via
            parent = self._parent_link(d.entity) if line_view is not None else None
            if line_view is not None and line_view.field_by_label(via) is None and parent is not None:
                via = line_view.label_of(parent[1])
            if parent is not None:
                embedded = self._embedded_lines(parent[0], d.entity)
                if embedded is not None and embedded[1] != EVERYTHING:
                    self._line_scope = [embedded[1]]
            return self._aggregate(Deliverable(
                kind="list", entity=d.entity, fields=d.fields, sort=d.sort,
                descending=d.descending, limit=d.limit, page=d.page,
                metric=Metric(op="count", filters=(Filter(field=via, values=(d.record,)),)
                              + (d.metric.filters if d.metric else ()))))
        entity = index.entity(d.entity) if d.entity else None
        # A fact about a record the request does not name ("my leave request: when did
        # it start?") is about the one record in scope, or asks which when there are several.
        described = _describes_record(d) or (d.kind == "fact" and not d.record and not d.rules)
        if (d.kind in ("fact", "page", "handoff") and entity is not None and not d.record
                and not described and self._found_one is not None
                and self._found_one[0] == entity.key and norm(d.action) not in ("new", "create")):
            d = replace(d, record=self._found_one[2])
        if d.kind == "list" and entity is not None and (d.record or described):
            # "The order of 22 August, its products and subtotals": a list of the lines
            # of one record, the record named or described, the line field among the
            # columns asked for.
            view = self.view(entity.key)
            line_field = next((k for k, f in view.fields.items()
                               if f.kind == "lines" and self.view(f.relation) is not None
                               and self._embedded_lines(entity.key, f.relation) is not None
                               and any(norm(f.label) == norm(label) for label in d.fields)),
                              None) if view is not None else None
            if line_field is not None:
                owner = self._find_record(entity.key, d.record) if d.record else None
                if not _proper(owner):
                    owner = self._locate(d) if described else Resolved(
                        kind=d.kind, problem=f"no {entity.label} matches {d.record!r}")
                if isinstance(owner, Resolved):
                    return owner
                return self._lines_list(entity.key, line_field, owner, d)
        if d.kind in ("aggregate", "list"):
            return self._aggregate(d)
        schema = self.schema.entity(entity.key) if entity is not None else None
        operations = schema.operations if schema is not None else frozenset()
        if (d.kind in ("handoff", "page") and entity is not None and norm(d.action) == "delete"
                and operations and "delete" not in operations):
            # The application shows Delete only to an account that may delete these
            # records; for one that may not, no record of them can be deleted.
            return Resolved(kind="handoff", problem=f"this account may not delete {entity.label} records",
                            denied="delete")
        if (d.kind == "handoff" and entity is not None and operations
                and norm(d.action) not in ("new", "create")
                and "write" not in operations
                and (d.value or (entity.field_by_label(d.action) is not None
                                 and not any(norm(a) == norm(d.action) for a in entity.actions)))):
            # A field changed to a value: the form stays read-only to an account that may
            # not write these records, so nothing could be entered or saved.
            return Resolved(kind="handoff", problem=f"this account may not change {entity.label} records",
                            denied="write")
        if d.kind == "page" and entity is None and d.page.key:
            return self._list_page(d)
        if entity is None:
            return Resolved(kind=d.kind, problem="the entity named is not in the index",
                            repair=("entity must be one of the index's entity keys: "
                                    + ", ".join(e.key for e in index.entities[:30])))
        if d.kind == "fact" and d.fields and not d.rules:
            view = self.view(entity.key)
            if d.via and view is not None:
                link = view.link_by_label(d.via)
                if link is not None:
                    origin, view = view, self.view(view.info(link).relation)
                    if (view is not None
                            and all(view.field_by_label(label) is None for label in d.fields)
                            and all(origin.field_by_label(label) is not None for label in d.fields)):
                        # An owner condition is not a request to read the owner's
                        # fields. Do not silently discard via or guess the intent.
                        return Resolved(kind="fact",
                            problem="the declared link target has none of the requested fields",
                            repair=(f"via {d.via!r} follows {origin.key} to {view.key}, "
                                    f"which has none of {', '.join(d.fields)}. These fields belong "
                                    f"to {origin.key}. If the request asks for those values, omit "
                                    f"via and express the owner using metric.filters on {origin.key}; "
                                    "its condition fields are: " + ", ".join(
                                        f.label for f in origin.schema.fields[:30] if f.searchable)
                                    + ". Preserve every explicit exclusion; never substitute fields."))
            # A missing field cannot be resolved by asking which unrelated record
            # the person meant. Keep embedded-line fallback for parent schemas.
            if not d.record and view is not None and not any(f.kind == "lines" for f in view.fields.values()):
                missing = tuple(label for label in d.fields if view.field_by_label(label) is None)
                if len(missing) == len(d.fields):
                    return Resolved(kind="fact", missing_fields=missing,
                        problem="none of the fields asked for exist in this actor-visible schema",
                        repair=f"{view.label} has no requested fields: {', '.join(missing)}; "
                               "do not choose unrelated records or substitute neighbouring fields")
        if d.kind == "handoff" and not described and (
                not d.record or norm(d.action) in ("new", "create")):
            # A creation names no existing record; the name given, if any, is the new
            # record's or its customer's, and is not looked up.
            return self._creation(d, entity)
        if not d.record and not described:
            if d.kind == "page":
                page = self._default_page(entity.key)
                return self._list_page(Deliverable(kind="page", entity=d.entity,
                                                   page=type(d.page)(key=page.key if page else "",
                                                                     view=d.page.view,
                                                                     filters_on=d.page.filters_on,
                                                                     filters_off=d.page.filters_off,
                                                                     group_by=d.page.group_by))
                                       ) if page else Resolved(
                    kind=d.kind, problem="no page shows this entity")
            return Resolved(kind=d.kind, problem="no record was named")
        found = self._find_record(entity.key, d.record) if d.record else None
        via = d.via
        if described and not _proper(found):
            # Not a name but a description ("the quotation sent to Joel Willis", "the
            # 31,750 credit note"): the conditions pick the record out, or say why not.
            # A name several records carry ("张伟", beside the customer the person gives)
            # is one of the conditions: the records with that name the others describe.
            named = (replace(d, record="", page=replace(d.page, search=d.record))
                     if isinstance(found, Ambiguous) and not d.page.search else d)
            found = self._locate(named)
            if isinstance(found, Resolved):
                return found
        if d.kind == "handoff" and norm(d.action) in ("", "new", "create") and (
                found is None or isinstance(found, Ambiguous)):
            # A change asked for and the name is no existing record: it is the record to
            # be created (or the customer it is for), and the control is the list page's
            # own "New". A change to a record that was named but not found is refused.
            return self._creation(d, entity)
        if found is None:
            # Words before a name ("我的种子-L03"): the name that follows, when it is one
            # record's.
            found = self._after_leading_words(entity.key, d.record)
        if found is None:
            # A record this entity points at, named for its records ("the contract's
            # payment plan"): the one that points at it, or the several to choose from.
            found = self._pointing_at(entity.key, d.record)
        if found is None:
            # The reference may belong to a record that links to this entity ("S00006" is
            # an order; the person asked about its customer). Re-home the reference when
            # exactly one such entity holds it, and follow the link.
            rehomed = self._rehome(entity.key, d.record)
            if rehomed is None:
                names = self._names_repair(entity.key) if foreign(d.record) else ""
                elsewhere, holders = self._named_elsewhere(entity.key, d.record)
                return Resolved(kind=d.kind, problem=f"no {entity.label} matches {d.record!r}",
                                missing=d.record, elsewhere=holders,
                                repair=(f"no {entity.label} is named {d.record!r}; "
                                        + (f"{elsewhere}; " if elsewhere else "")
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
        if d.kind == "fact" and not via and model == entity.key and d.metric and d.metric.filters:
            linked = self._linked_instead(d, rid, name)
            if isinstance(linked, Resolved):
                return linked
            if linked is not None:
                model, rid, name = linked
        if model != entity.key and d.fields and not via and self.view(model) is not None:
            # Described on one entity, found on another (an order the model looked for
            # among invoices): the fields asked for, as the entity it is on has them.
            d = replace(d, fields=_mapped_fields(d.fields, self.view(entity.key),
                                                 self.view(model)) or d.fields)
        if via:
            view = self.view(model)
            link = view.link_by_label(via)
            if link is None:
                lines = view.field_by_label(via)
                line_info = view.info(lines)
                if line_info is not None and line_info.kind == "lines" and self.view(line_info.relation):
                    # "The lines of S00004": a list of the line entity linked to the record.
                    line_view = self.view(line_info.relation)
                    parent = next((k for k, f in line_view.fields.items()
                                   if f.kind == "link" and f.relation == model), None)
                    if parent is not None:
                        return self._aggregate(Deliverable(
                            kind="list", entity=line_info.relation, fields=d.fields,
                            sort=d.sort, descending=d.descending, limit=d.limit,
                            metric=Metric(op="count", filters=(Filter(
                                field=line_view.label_of(parent), values=(name,)),))))
                return Resolved(kind=d.kind, problem=f"{via!r} is not a link field")
            info = view.info(link)
            target = self._read(model, rid, (link,)).values.get(link)
            if not target:
                return Resolved(kind=d.kind, problem=f"{name} has no {info.label}")
            model, rid, name = info.relation, str(target.id), str(target.display)
        if d.kind == "fact":
            return self._facts(model, rid, name, d.fields)
        if d.kind == "handoff":
            view = self.view(model)
            control = d.action
            if d.fields and not any(norm(a) == norm(d.action) for a in view.schema.actions):
                # The change is to a field, and the model named the field alongside a
                # button it guessed: the field is what the person will edit.
                control = d.fields[0]
            plan = self._record_page(model, rid, name, section="", highlight=control, value=d.value)
            repair = ""
            if d.value and not plan.fill:
                info = view.info(view.field_by_label(control)) if view is not None else None
                if info is not None and info.kind == "link":
                    repair = (f"no {info.label or control} is named {d.value!r}; "
                              "value must be the exact name of an existing record")
            return Resolved(kind="handoff", page=plan, repair=repair)
        plan = self._record_page(model, rid, name, section=d.section, highlight="")
        view = self.view(model)
        if d.section and view is not None:
            # "Its invoice lines": the section that embeds the record's lines is shown,
            # and the lines are read out too, since a request for a record's lines is
            # usually a question about them.
            line_field = next((k for k, f in view.fields.items()
                               if f.kind == "lines" and self.view(f.relation) is not None
                               and norm(f.label) == norm(d.section)
                               and self._embedded_lines(model, f.relation) is not None), None)
            if line_field is not None:
                lines = self._lines_list(model, line_field, (model, rid, name), d)
                if lines.aggregate is not None:
                    return Resolved(kind="page", page=plan, aggregate=lines.aggregate)
        return Resolved(kind="page", page=plan)

    def _creation(self, d: Deliverable, entity) -> Resolved:
        """"Create a new X": the control is "New" on the entity's list page -- the one
        declared when it shows this entity, else the widest one, whose scope restricts
        nothing, since a new record starts in the state the narrower pages exclude."""
        candidates = [p for p in self.schema.pages if p.entity == entity.key]
        chosen = next((p for p in candidates if p.key == d.page.key and p.menu), None)
        if chosen is None:
            chosen = next((p for p in candidates if p.menu and p.scope == EVERYTHING),
                          candidates[0] if candidates else None)
        page = self.index.page(chosen.key) if chosen else None
        if chosen is None or page is None:
            return Resolved(kind="handoff", problem="no page shows this entity")
        if "create" not in page.operations:
            return Resolved(kind="handoff", problem="creation is not available to this actor",
                            denied="create")
        control = "New" if norm(d.action) in ("", "new", "create") else d.action
        url = self.port.page_address(chosen.key)
        return Resolved(kind="handoff", page=PagePlan(
            url=url, label=page.breadcrumb, expected_path=url, highlight=control,
            highlight_effect="create"))

    # -- records ---------------------------------------------------------------------------
    def _rehome(self, model: str, reference: str):
        hits = []
        for entity in self.index.entities:
            view = self.view(entity.key)
            if entity.key == model or view is None or not view.listed:
                continue
            shown = set(view.shown)
            links = [k for k, f in view.fields.items()
                     if f.kind == "link" and f.relation == model and k in shown]
            if not links:
                continue
            try:
                found = self._find_record(entity.key, reference)
            except DataRefused:
                # A related list can require page context. That must not hide an
                # absent record on the entity the person actually named.
                continue
            if _proper(found):
                hits.append((found, view.label_of(links[0])))
        return hits[0] if len(hits) == 1 else None

    def _links_within(self, model: str, field: str, domain: list, *,
                      complete: bool = False) -> list[tuple[str, str]]:
        """The distinct records a link field points to among the records `domain`
        describes, as (id, display name)."""
        try:
            count = self._count(model, domain) if complete else None
            if count is not None and count > _LINK_SCOPE_LIMIT:
                return []
            rows = self._rows(model, domain, fields=(field,), limit=_LINK_SCOPE_LIMIT)
            if count is not None and len(rows) != count:
                # A truncated or changing list cannot prove a unique related record.
                return []
        except DataRefused:
            return []
        links: dict[str, str] = {}
        for row in rows:
            value = row.values.get(field)
            if value is not None and hasattr(value, "id"):
                links.setdefault(str(value.id), str(value.display))
        return list(links.items())

    def _names_of(self, model: str, field: str, domain: list) -> list[str]:
        return [name for _id, name in self._links_within(model, field, domain)][:20]

    def _pointing_at(self, model: str, reference: str):
        """The records of `model` that point at the one record a reference names, through
        the one link of `model` whose entity has it: a triple for one, `Ambiguous` for
        several, None otherwise."""
        view = self.view(model)
        if view is None:
            return None
        hits = []
        for key, info in view.fields.items():
            if info.kind != "link" or not info.relation or info.relation == model:
                continue
            try:
                named = self._find_record(info.relation, reference)
            except DataRefused:
                continue
            if _proper(named):
                hits.append((key, named))
        if len(hits) != 1:
            return None
        key, named = hits[0]
        try:
            rows = self._rows(model, [Compare(key, "eq", named[1])], limit=7)
        except DataRefused:
            return None
        if len(rows) == 1:
            return (model, rows[0].id, rows[0].display)
        if 2 <= len(rows) <= 6:
            return Ambiguous(r.display for r in rows)
        return None

    def _after_leading_words(self, model: str, reference: str):
        """The one record named by what follows a few words in the person's language at
        the start of a reference, or None."""
        text = reference.strip()
        for cut in range(1, 4):
            head, rest = text[:cut], text[cut:].strip()
            if not foreign(head) or len(rest) < 3:
                break
            found = self._find_record(model, rest)
            if isinstance(found, tuple) and not isinstance(found, Ambiguous):
                return found
        return None

    def _named_elsewhere(self, model: str, reference: str) -> tuple[str, tuple[str, ...]]:
        """Where else a name that no record of `model` has is one record's, for the
        repair ("'X' is 客户 record 'X 有限公司' (erp/customer)"), and those entities."""
        hits, keys, several = [], [], []
        wanted = norm(self._own_name(reference))
        for other in self.index.entities:
            if other.key == model or self._default_page(other.key) is None:
                continue
            try:
                found = self._find_record(other.key, reference)
            except DataRefused:
                continue
            if isinstance(found, Ambiguous):
                # Several records whose names all begin with the name asked for ("专业版订阅"
                # beside "专业版订阅（年）", "…（两年）"): which one is the person's to say, on
                # that entity, so the repair is pointed there. Names that only hold it
                # further in ("杭州旭日企业版订阅项目") are other things named after it.
                names = list(found)
                if wanted and names and all(norm(name).startswith(wanted) for name in names):
                    several.append((other, names))
            elif isinstance(found, tuple):
                hits.append(f"{other.label} record {found[2]!r} (entity {other.key})")
                keys.append(other.key)
        if not hits and len(several) == 1:
            other, names = several[0]
            return (f"{reference!r} begins several {other.label} records ("
                    + ", ".join(repr(n) for n in names)
                    + f") (entity {other.key}); when one of those is meant, declare the "
                    "deliverable on that entity with the name as asked"), (other.key,)
        if not hits or len(hits) > 3:
            return "", ()
        return (f"{reference!r} is " + " and ".join(hits)
                + "; when that is the record meant, declare the deliverable on that entity"), tuple(keys)

    def _find_record(self, model: str, reference: str):
        """(entity, id, display) of the record a reference names, `Ambiguous` when several
        may be meant, None when none is."""
        refs = self._find(model, reference, 8)
        if not refs:
            return None
        wanted = norm(reference)
        plain = norm(self._own_name(reference))
        exact = [r for r in refs if norm(r.own_name) in (wanted, plain) or norm(r.display) == wanted]
        # Records whose names hold the name asked for ("Office Chair Black" beside "Office
        # Chair"): the same name may mean either, so the person is asked (owner,
        # 2026-09-28), rather than the exact one being taken. A company's people carry
        # its name first; they are not another record of the same name.
        longer = [r for r in refs if r not in exact and plain in norm(r.own_name)
                  and norm(r.owner) != plain]
        # A reference with a number in it (INV/2026/00001) is an identifier, not a name.
        if exact and longer and not re.search(r"\d", reference) and plain not in self._chosen:
            # Every record of that exact name (two people called 张伟, told apart by whose
            # they are), then the longer names.
            return Ambiguous(dict.fromkeys(r.display for r in (exact + longer)[:6]))
        if len(exact) == 1 or (exact and all(r.id == exact[0].id for r in exact)):
            return _triple(exact[0])
        if len(refs) == 1:
            return _triple(refs[0])
        # A company's people carry its name first: the company is meant.
        companies = [r for r in refs if not r.owner]
        if len(companies) == 1:
            return _triple(companies[0])
        # A number shown with more after it ("BILL/2025/06/0001 (INV/2025/0057)") is that
        # document. A name several names begin with may mean any of them: ask.
        prefix = [r for r in refs if norm(r.own_name).startswith(plain)]
        if len(prefix) == 1 and re.search(r"\d", reference):
            return _triple(prefix[0])
        return Ambiguous(r.display for r in refs[:6])

    def _facts(self, model: str, rid: str, name: str, labels: tuple[str, ...]) -> Resolved:
        view = self.view(model)
        fields, missing = [], []
        for label in labels:
            field = view.field_by_label(label)
            if field is None:
                missing.append(label)
            elif field not in fields:
                fields.append(field)
        if not fields:
            lines = self._line_facts(model, rid, name, missing)
            if lines is not None:
                return lines
            return Resolved(kind="fact",
                            missing_fields=tuple(missing),
                            problem="none of the fields asked for exist on this record",
                            repair=(f"{view.label} has no field named "
                                    + ", ".join(repr(m) for m in missing)
                                    + "; its fields are: " + (_fields_of(self.index, model)
                                                              or ", ".join(f.label for f in
                                                                           view.schema.fields[:30]))))
        row = self._read(model, rid, fields)
        if self._evidence is not None:
            self._evidence.facts(model, view, row, fields)
        facts = tuple(Fact(subject=name, label=view.label_of(f), value=str(row.shown.get(f, "(empty)")),
                           field_key=f,
                           native_value=(str(row.values[f]) if view.info(f).kind == "choice"
                               and type(row.values.get(f)) in (str, int)
                               and any(c.key == str(row.values[f]) for c in view.info(f).choices) else ""))
                      for f in fields)
        if self._evidence is not None and any(f not in row.values for f in fields):
            return Resolved(kind="fact", facts=facts,
                            problem="the application did not read every requested field",
                            repair="Unread fields are undetermined, not empty; repeat a supported typed read.")
        problem = "not on this record: " + ", ".join(missing) if missing else ""
        empty = tuple(view.label_of(f) for f in fields if row.values.get(f) in (None, ""))
        repair = ""
        if missing:
            # Some fields asked for are not fields of this entity: the declaration may
            # have added them, or named them otherwise.
            repair = (f"{view.label} has no field named " + ", ".join(repr(m) for m in missing)
                      + "; its fields are: " + (_fields_of(self.index, model) or ", ".join(
                          f.label for f in view.schema.fields[:30]))
                      + ". Declare only the fields the person asked about")
        if empty and len(empty) == len(fields) and not missing:
            # Nothing asked for holds a value: another field of the record may be the one
            # that answers ("phone" empty, "mobile" filled). The declarer judges which,
            # from the record's filled fields; the empty ones stay in the answer.
            others = [f.key for f in view.schema.fields if f.key not in fields][:40]
            held = self._read(model, rid, others) if others else None
            filled = [view.label_of(f) for f in others
                      if held is not None and held.values.get(f) not in (None, "")]
            if filled:
                repair = (f"{name}: {', '.join(empty)} holds no value on this record; its fields "
                          f"that hold one are: {', '.join(filled)}. When one of them answers "
                          "what the person asked, add it to the fields, keeping those asked for")
        return Resolved(kind="fact", facts=facts, problem=problem, empty=empty, repair=repair,
                        missing_fields=tuple(missing))

    # -- pages -----------------------------------------------------------------------------
    def _default_page(self, model: str) -> IndexPage | None:
        for page in self.schema.pages:
            if page.entity == model:
                return self.index.page(page.key)
        return None

    def _fill_for(self, view: EntityView, field: str, value: str) -> tuple[str, str, str]:
        """(what to put in, how, its visible label) for a value the person gave a field,
        or empty when it cannot be typed with certainty."""
        info = view.info(field)
        kind = info.kind if info is not None else ""
        value = value.strip()
        if not value:
            return "", "", ""
        if kind in ("date", "datetime"):
            span = period(value, self.today())
            if span is None or span[0] is None or span[1] is None or (span[1] - span[0]).days != 1:
                return "", "", ""
            return span[0].isoformat(), "text", span[0].isoformat()
        if kind == "choice":
            wanted = norm(value)
            for choice in info.choices:
                if norm(choice.label) == wanted or choice.key == value:
                    return choice.key, "choice", choice.label
            return "", "", ""
        if kind == "link":
            found = self._find_record(info.relation, value)
            if not _proper(found):
                return "", "", ""
            return found[2], "link", found[2]
        if kind in ("number", "money"):
            try:
                float(value.replace(",", ""))
            except ValueError:
                return "", "", ""
            return value.replace(",", ""), "text", value
        if kind in ("text", "html"):
            return value, "text", value
        return "", "", ""

    def _record_page(self, model: str, rid: str, name: str, *, section: str,
                     highlight: str, value: str = "") -> PagePlan:
        view = self.view(model)
        url = self.port.record_address(model, rid)
        collection = self._default_page(model)
        if collection is not None and url == self.port.page_address(collection.key):
            return PagePlan(url=url, label=collection.breadcrumb, expected_path=url,
                            missing_record=name)
        tab = ""
        if section and view is not None:
            wanted = norm(section)
            for item in view.schema.sections:
                if norm(item) == wanted or wanted in norm(item):
                    tab = item
                    break
            tab = tab or section
        control, fill, fill_field = highlight, ("", "", ""), ""
        if highlight and view is not None:
            wanted = norm(highlight)
            for item in view.schema.actions:
                if norm(item) == wanted:
                    control = item
                    break
            else:
                # Not a button: the field the person wants to change, named loosely
                # ("Expiration Date" for Expiration).
                asked = tokens(highlight)
                field = next((k for k, f in view.fields.items() if norm(f.label) == wanted), None)
                if field is None:
                    field = next((k for k, f in view.fields.items()
                                  if f.on_form and asked and asked <= tokens(f.label)), None)
                if field is None:
                    field = view.field_by_label(highlight)
                info = view.info(field)
                if info is not None and info.on_form:
                    control = info.label
                    if not tab:
                        # A field inside a tab is reached by selecting that tab first.
                        tab = info.tab
                    if value:
                        fill = self._fill_for(view, field, value)
                        fill_field = field if fill[0] else ""
        label = name if not tab else f"{name} · {tab}"
        return PagePlan(url=url, label=label, expected_path=url, section=tab,
                        highlight=control, fill=fill[0], fill_kind=fill[1], fill_label=fill[2],
                        fill_field=fill_field)

    def _list_page(self, d: Deliverable) -> Resolved:
        page = self.index.page(d.page.key) if d.page.key else None
        if page is None and d.entity:
            page = self._default_page(d.entity)
        if page is None and d.page.key and self.index.entity(d.page.key.strip("/")) is not None:
            # The page was named by the key of the entity it lists ("crm/contact" for the
            # contacts page): that entity's own page is the one meant.
            page = self._default_page(d.page.key.strip("/"))
        if page is None:
            return Resolved(kind="page", problem="the page named is not in the index")
        if d.page.search and d.kind == "page":
            # "Show me Large Desk's card": the words name one record of the page's entity.
            found = self._find_record(page.entity, d.page.search)
            if _proper(found) and (d.section or norm(self._own_name(found[2])) == norm(d.page.search)):
                return Resolved(kind="page", page=self._record_page(
                    found[0], found[1], found[2], section=d.section, highlight=""))
            if found is None and not self._count(page.entity, [Compare(NAME, "contains", d.page.search)]):
                # Nothing on this page is named so: opening the list would show the person
                # something else and say the words were found.
                return Resolved(kind="page", problem=f"no {page.label} record matches {d.page.search!r}",
                                repair=(f"no {page.label} record is named {d.page.search!r}; when the "
                                        "words name a related record (a customer, a product), put "
                                        "them in metric.filters on the field that links to it, and "
                                        "leave the search empty"))
        if self.schema.page(page.key) is None:
            return Resolved(kind="page", problem="the page named is not in the index")
        expected = self.port.page_address(page.key)
        view = d.page.view
        if view and view not in page.views:
            view = ""
        if not page.views or (view and len(page.views) == 1):
            # A page with one way of showing its records already shows the view the
            # request names ("the list"): there is nothing to switch to or to check.
            d = replace(d, page=replace(d.page, view=""))
            view = ""
        # A facet the index does not list may still exist on the live page; it is passed
        # through by name and settled by the runtime.
        on = [_facet_name(page, name) or name for name in d.page.filters_on]
        if d.page.group_by:
            on.append(_facet_name(page, d.page.group_by, group=True) or d.page.group_by)
        off = [_facet_name(page, name) or name for name in d.page.filters_off]
        problem = f"this page has no {d.page.view} view" if d.page.view and not view else ""
        url = self.port.page_address(page.key, view=view) if view else expected
        return Resolved(kind="page", problem=problem, page=PagePlan(
            url=url, label=page.breadcrumb, expected_path=expected, view=view,
            facets_on=tuple(dict.fromkeys(on)), facets_off=tuple(dict.fromkeys(off)),
            conditions=d.page.conditions,
            page_number=d.page.page_number, page_step=d.page.page_step, page_size=d.page.page_size,
            missing_view=d.page.view if problem else "", requires_page_route=not page.entity))

    def _names_repair(self, model: str) -> str:
        """Every name of a small entity, for a request that describes one of them in
        another language than the application's: the declarer picks the name."""
        view = self.view(model)
        if view is None:
            return ""
        try:
            count = self._count(model, EVERYTHING)
            if not count or count > _NAMES_LISTED:
                return ""
            rows = self._rows(model, EVERYTHING, order=((ID, False),), limit=_NAMES_LISTED)
        except DataRefused:
            return ""
        names = [r.display for r in rows if r.display]
        return (f"the {view.label} records here are: " + "; ".join(names)
                + "; if the person described one of them in their own words, declare the same "
                  "deliverable again with that name, exactly as it is here, as its record")


#: The most names of one entity a repair lists for a description in another language.
_NAMES_LISTED = 80


def _cond(where) -> Cond:
    return all_of(*where) if isinstance(where, list) else where


def _triple(ref: Ref) -> "Found":
    return Found(ref)


def _proper(found) -> bool:
    return isinstance(found, tuple) and not isinstance(found, Ambiguous)


def _describes_record(d: Deliverable) -> bool:
    """The deliverable carries conditions that can pick a record out without its name."""
    return bool((d.metric is not None and d.metric.filters) or d.page.filters_on or d.page.search)


def navigation_only(d: Deliverable) -> bool:
    """A direct page request carries no record, data, facet or form declaration."""
    return d.kind == "page" and not any((d.entity, d.record, d.via, d.fields, d.metric,
        d.page.view, d.page.filters_on, d.page.filters_off, d.page.group_by, d.page.search,
        d.section, d.action, d.value, d.field_values, d.sort, d.rules))


def navigation_refused(d: Deliverable, page: IndexPage) -> Resolved:
    """Typed refusal shared by data and application-owned goal resolvers."""
    return Resolved(kind=d.kind, problem=f"{page.label} offers navigation only; it has no records or form operations",
                    read_failure="refused", read_operation=d.kind,
                    read_fields=d.fields or ((d.metric.field,) if d.metric and d.metric.field else ()))


def _fields_of(index: ApplicationIndex, model: str) -> str:
    entity = index.entity(model) if model else None
    if entity is None:
        return ""
    return ", ".join(f.label for f in entity.fields[:20])


def _facet_name(page: IndexPage, label: str, *, group: bool = False) -> str | None:
    wanted = norm(label)
    pool = page.groups if group else page.filters
    for item in pool:
        if norm(item) == wanted:
            return item
    for item in pool:
        if wanted and (wanted in norm(item) or norm(item) in wanted):
            return item
    if not group:
        for item in page.groups:
            if norm(item) == wanted:
                return item
    return None


def _same_field(key: str, source: EntityView, target: EntityView) -> str | None:
    """The target's field for one of the source's: the same field (key, kind and
    relation), or, for a date, the target's main date."""
    info, there = source.info(key), target.info(key)
    if info is None:
        return None
    if there is not None and there.kind == info.kind and there.relation == info.relation:
        return key
    if info.kind in ("date", "datetime"):
        return target.schema.main_date or None
    return None


def _mapped_conditions(conditions, source: EntityView, target: EntityView):
    out = []
    for condition in conditions:
        key = source.field_by_label(condition.field)
        there = _same_field(key, source, target) if key else None
        if there is None:
            return None
        out.append(Filter(field=target.label_of(there), values=condition.values, op=condition.op))
    return out


def _mapped_fields(labels, source: EntityView, target: EntityView) -> tuple[str, ...]:
    out = []
    for label in labels:
        key = source.field_by_label(label)
        there = _same_field(key, source, target) if key else None
        if there is not None:
            out.append(target.label_of(there))
    return tuple(dict.fromkeys(out))


__all__ = ["Ambiguous", "Found", "Resolver", "navigation_only", "navigation_refused"]
