"""A REST-backed implementation of the application-neutral goal contract.

The synthetic Contacts app has no ORM, action registry, or source index.  Its port
reads the signed-in actor's REST collection and resolves only the operations that
application can substantiate; unsupported goals remain explicit failures.
"""
from __future__ import annotations

import hashlib

from web_operation_agent.goal.index import (ApplicationIndex, IndexEntity, IndexField,
                                             IndexPage)
from web_operation_agent.goal.ports import Aggregate, Fact, PagePlan, Resolution, Resolved
from web_operation_agent.goal.spec import Deliverable, GoalSpec
from web_operation_agent.state.identity import OperationContext, PrincipalError

from .adapter import APPLICATION_ID, MinimalContactsAdapter


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


class ContactsGoalPort:
    """Resolve a small, real REST application's goals under its delegated actor."""

    def __init__(self, adapter: MinimalContactsAdapter):
        self._adapter = adapter
        self._executor = adapter.business_tools._executor

    def _require_actor(self, context: OperationContext | None) -> None:
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

    def index(self, *, context: OperationContext | None = None) -> ApplicationIndex:
        self._require_actor(context)
        assert context is not None
        digest = hashlib.sha256(("contacts-goal-v1\0"
                                 + context.principal.actor_authority_digest).encode()).hexdigest()
        return ApplicationIndex(
            entities=(IndexEntity(
                key="contacts", label="Contacts", aliases=("联系人", "通讯录"),
                fields=(IndexField("id", "Identifier", "text"),
                        IndexField("name", "Name", "text"),
                        IndexField("email", "Email", "text"))),),
            pages=(IndexPage(key="contacts", label="Contacts", breadcrumb="Contacts",
                             entity="contacts", views=("list",)),),
            source_digest=digest)

    def resolve(self, spec: GoalSpec, *, context: OperationContext | None = None,
                current_path: str = "") -> Resolution:
        self._require_actor(context)
        rows = self._executor._contact_rows()
        return Resolution(
            deliverables=tuple(self._resolve_one(d, rows) for d in spec.deliverables),
            reads=(("GET", "/api/contacts"),))

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

    def _resolve_one(self, d: Deliverable, rows: list[dict]) -> Resolved:
        if _norm(d.entity) not in {"", "contacts", "contact", "联系人", "通讯录"}:
            return Resolved(kind=d.kind, problem="entity is not in this application's index")
        if d.via or d.section or d.page.filters_on or d.page.filters_off or d.page.group_by:
            return Resolved(kind=d.kind, problem="requested relation or page part is unsupported")
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
