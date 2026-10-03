"""A REST-backed implementation of the application-neutral goal contract.

The synthetic Contacts app has no ORM, action registry, or source index.  Its port
reads the signed-in actor's REST collection and resolves only the operations that
application can substantiate; unsupported goals remain explicit failures.
"""
from __future__ import annotations

import hashlib

from web_operation_agent.goal.index import (ApplicationIndex, IndexEntity, IndexField,
                                             IndexPage)
from web_operation_agent.goal.ports import (Aggregate, Candidate, Fact, FormPlan, PagePlan, Resolution,
                                            Resolved)
from web_operation_agent.goal.spec import Deliverable, GoalSpec
from web_operation_agent.goal.resolve.resolver import navigation_only, navigation_refused
from web_operation_agent.state.identity import OperationContext, PrincipalError

from .adapter import APPLICATION_ID, MinimalContactsAdapter


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


#: The vendor desks: read-only pages with two tabs each, by page key.
_DESKS = {"desk-partner": "/contacts/workflows/partner",
          "desk-supplier": "/contacts/workflows/supplier"}
_DESK_SECTIONS = ("Overview", "Terms")


class ContactsGoalPort:
    """Resolve a small, real REST application's goals under its delegated actor."""

    def __init__(self, adapter: MinimalContactsAdapter, *, form_prefill: bool = False):
        if not isinstance(form_prefill, bool):
            raise TypeError("form_prefill must be boolean")
        self._form_prefill = form_prefill
        self._adapter = adapter
        self._executor = adapter.business_tools._executor

    def _require_actor(self, context: OperationContext | None) -> frozenset[str]:
        if context is None:
            raise PrincipalError("a goal read needs an actor-bound operation context")
        context.require_live(application_id=APPLICATION_ID,
                             required_scope="contacts:read")
        assertion, permissions = self._adapter.identity._whoami(
            {"Authorization": f"Bearer {self._executor._api_token}"}, "delegated_api")
        principal = context.principal
        if (assertion.actor_id != principal.actor_id
                or assertion.workspace_id != principal.workspace_id
                or assertion.tenant_id != principal.tenant_id
                or "contacts:read" not in permissions):
            raise PrincipalError("goal read actor differs from delegated API actor")
        return frozenset(permissions)

    def index(self, *, context: OperationContext | None = None) -> ApplicationIndex:
        permissions = self._require_actor(context)
        return self._index(context, permissions)[0]

    def _index(self, context, permissions):
        assert context is not None
        catalog = self._executor._page_entries._port.catalog(context=context)
        digest = hashlib.sha256(("contacts-goal-v2\0" + catalog.binding_digest).encode()).hexdigest()
        pages = tuple(IndexPage(key=entry.reference, label=entry.label,
            breadcrumb=" / ".join(entry.breadcrumbs) or entry.label,
            entity="contacts" if entry.subject_reference == "contact" else "",
            views=entry.views, sections=_DESK_SECTIONS if entry.reference in _DESKS else (),
            operations=("create",) if entry.reference == "contacts" and "contacts:write" in permissions else ())
            for entry in catalog.entries if entry.route)
        return ApplicationIndex(
            entities=(IndexEntity(
                key="contacts", label="Contacts", aliases=("联系人", "通讯录"),
                fields=(IndexField("id", "Identifier", "text"),
                        IndexField("name", "Name", "text"),
                        IndexField("email", "Email", "text"))),),
            pages=pages, complete=catalog.complete, source_digest=digest), {
                entry.reference: entry.route for entry in catalog.entries if entry.route}

    def capabilities(self) -> frozenset[str]:
        """Beyond the required level this application offers tabs on its vendor desks,
        the creation handoff and candidate values (its contacts); it has no page filters,
        search box or lines."""
        return frozenset({"sections", "handoffs", "values"} | ({"fill"} if self._form_prefill else set()))

    def candidates(self, *, entities: tuple[str, ...] = (), known: tuple = (),
                   dates: tuple[str, ...] = (), context: OperationContext | None = None):
        """The contacts this actor sees, when the request is about contacts: a small
        application offers its records outright, each with the email that tells two
        similar names apart."""
        self._require_actor(context)
        if not any(_norm(e) in {"contacts", "contact"} for e in entities):
            return ()
        return tuple(Candidate(kind="record", entity="contacts", display=row["name"],
                               detail=row.get("email") or "")
                     for row in self._executor._contact_rows()[:20])

    def resolve(self, spec: GoalSpec, *, context: OperationContext | None = None,
                current_path: str = "") -> Resolution:
        permissions = self._require_actor(context)
        index, page_routes = self._index(context, permissions)
        needs_records = any((not d.page.key or (p := index.page(d.page.key)) is not None and p.entity)
                            and (d.kind in {"fact", "aggregate", "list"} or d.record or d.via or d.page.search)
                            for d in spec.deliverables)
        rows = self._executor._contact_rows() if needs_records else []
        return Resolution(
            deliverables=tuple(self._resolve_one(
                d, rows, index=index, page_routes=page_routes,
                can_create="contacts:write" in permissions)
                for d in spec.deliverables),
            reads=(("GET", "/api/contacts"),) if needs_records else ())

    @staticmethod
    def _field(label: str) -> str:
        lookup = {"identifier": "id", "id": "id", "name": "name", "email": "email"}
        return lookup.get(_norm(label), "")

    def _record(self, reference: str, rows: list[dict]) -> tuple[dict | None, str]:
        exact = [row for row in rows if _norm(reference) in
                 {_norm(row["id"]), _norm(row["name"])}]
        if len(exact) == 1:
            return exact[0], ""
        if len(exact) > 1:
            return None, "record reference is ambiguous"
        partial = [row for row in rows if _norm(reference) in _norm(row["name"])]
        if len(partial) == 1:
            return partial[0], ""
        return None, ("record reference is ambiguous" if partial else
                      "record is not visible to this actor")

    def _resolve_one(self, d: Deliverable, rows: list[dict], *, index: ApplicationIndex,
                     page_routes: dict[str, str], can_create: bool) -> Resolved:
        page = index.page(d.page.key) if d.page.key else None
        if page is not None and not page.entity:
            if not navigation_only(d):
                return navigation_refused(d, page)
            path = page_routes[page.key]
            return Resolved(kind="page", page=PagePlan(url=path, expected_path=path,
                label=page.label, requires_page_route=True))
        if d.page.key and page is None:
            return Resolved(kind=d.kind, problem="page is not in this actor's current index")
        if _norm(d.entity) not in {"", "contacts", "contact", "联系人", "通讯录"}:
            return Resolved(kind=d.kind, problem="entity is not in this application's index")
        if d.via or d.page.filters_on or d.page.filters_off or d.page.group_by:
            return Resolved(kind=d.kind, problem="requested relation or page part is unsupported")
        if d.kind == "page" and d.page.key in _DESKS:
            # A vendor desk: a read-only page with two tabs, reached by its own route.
            section = next((s for s in _DESK_SECTIONS if _norm(s) == _norm(d.section)), "")
            if d.section and not section:
                return Resolved(kind=d.kind, problem="this desk has no such section")
            path = _DESKS[d.page.key]
            label = d.page.key.replace("desk-", "").title() + " desk"
            return Resolved(kind=d.kind, page=PagePlan(
                url=path, expected_path=path,
                label=f"{label} · {section}" if section else label, section=section))
        if d.section:
            return Resolved(kind=d.kind, problem="this page has no sections")
        if self._form_prefill and d.kind == "handoff" and not d.record and (d.value or d.field_values):
            if d.field_values and _norm(d.action) not in {"", "create", "new"}:
                return Resolved(kind=d.kind, problem="this batch needs the new-contact form entry")
            requested = d.field_values or ((d.action, d.value),)
            values = tuple((self._field(label), value) for label, value in requested)
            if (any(key not in {"name", "email"} for key, _ in values)
                    or len({key for key, _ in values}) != len(values)
                    or d.page.key not in {"", "contacts"}):
                return Resolved(kind=d.kind, problem="field is not in the new-contact form")
            if not can_create:
                return Resolved(kind=d.kind, problem="creation is not available to this actor", denied="create")
            return Resolved(kind=d.kind, page=PagePlan(url="/contacts/new", expected_path="/contacts/new",
                label="New contact", form=FormPlan("contacts-create-v1", values)))
        if d.field_values:
            return Resolved(kind=d.kind, unoffered="fill", problem="this target does not offer a reviewed form batch")
        if d.kind == "handoff" and _norm(d.action) == "create":
            if d.page.key and _norm(d.page.key) != "contacts":
                return Resolved(kind=d.kind, problem="page is not in the index")
            if not can_create:
                return Resolved(kind=d.kind, problem="creation is not available to this actor")
            return Resolved(kind=d.kind, page=PagePlan(
                url="/contacts/new", expected_path="/contacts/new", label="New contact",
                highlight="Name", highlight_effect="create"))
        if d.kind == "handoff" and d.record and d.action:
            # A change to one record is the person's: its page is opened and the control
            # named is marked, wherever the page keeps it. Whether this account is offered
            # the control at all is the page's to show, not this port's to decide.
            record, problem = self._record(d.record, rows)
            if record is None:
                return Resolved(kind=d.kind, problem=problem)
            path = f"/contacts/{record['id']}"
            return Resolved(kind=d.kind, page=PagePlan(
                url=path, expected_path=path, label=record["name"], highlight=d.action))
        if d.kind == "fact":
            record, problem = self._record(d.record, rows) if d.record else (None, "no record was named")
            if record is None:
                return Resolved(kind=d.kind, problem=problem)
            keys = [self._field(label) for label in d.fields]
            if not keys or not all(keys):
                return Resolved(kind=d.kind, problem="requested field is not in the index")
            labels = {"id": "Identifier", "name": "Name", "email": "Email"}
            return Resolved(kind=d.kind, facts=tuple(
                Fact(subject=record["name"], label=labels[key], value=record[key])
                for key in keys))
        if d.kind == "page":
            if d.page.key and _norm(d.page.key) not in {"contacts", "联系人", "通讯录"}:
                return Resolved(kind=d.kind, problem="page is not in the index")
            if d.page.view and _norm(d.page.view) != "list":
                return Resolved(kind=d.kind, problem="requested view is unavailable")
            if d.page.search:
                return Resolved(kind=d.kind, problem="this page has no verified search control")
            if d.record:
                record, problem = self._record(d.record, rows)
                if record is None:
                    return Resolved(kind=d.kind, problem=problem)
                path = f"/contacts/{record['id']}"
                return Resolved(kind=d.kind, page=PagePlan(
                    url=path, expected_path=path, label=record["name"]))
            return Resolved(kind=d.kind, page=PagePlan(
                url="/contacts", expected_path="/contacts", label="Contacts",
                view="list" if d.page.view else ""))
        if d.kind in {"aggregate", "list"}:
            metric = d.metric
            if (metric is not None and (metric.op != "count" or metric.group_by
                                        or metric.page_filters or metric.top)):
                return Resolved(kind=d.kind, problem="requested metric is unsupported")
            if d.page.key and _norm(d.page.key) != "contacts":
                return Resolved(kind=d.kind, problem="page scope is not in the index")
            selected = list(rows)
            if d.page.search:
                selected = [row for row in selected if _norm(d.page.search)
                            in _norm(row["name"])]
            for condition in (metric.filters if metric is not None else ()):
                key = self._field(condition.field)
                if not key or not condition.values or condition.op not in {"", "=", "contains"}:
                    return Resolved(kind=d.kind, problem="requested filter is unsupported")
                wanted = {_norm(value) for value in condition.values}
                selected = [row for row in selected if
                            (any(value in _norm(row[key]) for value in wanted)
                             if condition.op == "contains" else _norm(row[key]) in wanted)]
            names = tuple(row["name"] for row in selected[:20]) if d.kind == "list" else ()
            return Resolved(kind=d.kind, aggregate=Aggregate(
                subject="Contacts visible to this actor", count=len(selected),
                records=names, complete=len(selected) <= 20))
        return Resolved(kind=d.kind, problem="this application cannot verify that handoff")


__all__ = ["ContactsGoalPort"]
