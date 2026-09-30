"""Records picked out by a description rather than a name, and the readings tried when
a description fits nothing as declared."""
from __future__ import annotations

from dataclasses import replace

from ..data import ID, Compare, DataRefused
from ..ports import Resolved
from ..spec import Deliverable, Filter
from .scope import says
from .words import foreign


class LocateMixin:
    """Mixed into `Resolver`."""

    def _locate(self, d: Deliverable):
        """The one record a description picks out ("the quotation sent to Joel Willis"):
        exactly one record within the scope, or, when the page's own defaults or scope
        hide it, exactly one beyond them. Anything else is a problem the reply states,
        never a guess."""
        from .resolver import Found
        scope = self._scope(d, search=True)
        if isinstance(scope, Resolved):
            return scope
        view, model = scope.view, scope.model
        subject = " · ".join(scope.described)
        domains = [list(scope.domain)]
        if scope.default_clauses:
            domains.append([c for c in scope.domain if c not in scope.default_clauses])
        page_parts = ()
        if scope.action is not None:
            from ..data import EVERYTHING, parts
            page_parts = parts(scope.action.scope) if scope.action.scope != EVERYTHING else ()
        if page_parts and not scope.fixed:
            domains.append([c for c in domains[-1] if c not in page_parts])
        order, limit = (), 4
        if d.sort:
            # "The higher one", "the latest": ordered, the first is the one meant.
            order_field = view.field_by_label(d.sort)
            if order_field is None:
                return Resolved(kind=d.kind, problem=f"cannot sort by {d.sort!r}")
            order, limit = ((order_field, d.descending),), 1
        rows: tuple = ()
        for domain in domains:
            rows = self._rows(model, domain, order=order, limit=limit)
            if rows:
                break
        if not rows and d.page.search and (d.metric and d.metric.filters or d.page.filters_on):
            # The words matched nothing but the conditions may: the conditions alone.
            return self._locate(replace(d, page=replace(d.page, search="")))
        if not rows:
            elsewhere = (self._other_link(d, view) or self._without_foreign(d, view)
                         or self._elsewhere(d, view))
            if elsewhere is not None:
                return elsewhere
            names = self._names_repair(model) if any(
                foreign(v) for c in (d.metric.filters if d.metric else ()) for v in c.values) else ""
            return Resolved(kind=d.kind, problem=f"no {view.label} matches {subject}",
                            repair=(f"no {view.label} matches {subject}; re-read the request: "
                                    "another page scope (invoices versus bills, quotations "
                                    "versus orders), field or value may be meant"
                                    + (f"; {names}" if names else "")))
        if len(rows) > 1:
            names = tuple(r.display for r in rows)
            return Resolved(kind=d.kind, problem=f"{subject} matches several {view.label} records",
                            candidates=names,
                            repair=(f"{subject} matches several records ({', '.join(names)}); "
                                    "add a condition the request gives (amount, date, status, "
                                    "customer); when it gives none, leave them for the person "
                                    "to choose"))
        return _found(model, rows[0])

    def _name_on_link(self, d: Deliverable, scope):
        """A count or list that finds nothing because a record's name was written into a
        text field ("the contacts of Acme" as Company Name = Acme Corporation): the one
        link of the entity on which that name is a record, when the records it selects
        exist. Returns (deliverable, scope) or None."""
        view, model = scope.view, scope.model
        conditions = list(d.metric.filters) if d.metric else []
        entity = self.index.entity(model)
        if not conditions or entity is None or scope.fixed:
            return None
        try:
            if self._count(model, scope.domain):
                return None
        except DataRefused:
            return None
        found = []
        for i, condition in enumerate(conditions):
            key = view.field_by_label(condition.field)
            if key is None or view.info(key).kind != "text" or not condition.values:
                continue
            for field_ in entity.fields:
                other = view.field_by_label(field_.label) if field_.kind == "link" else None
                info = view.info(other)
                if info is None or not info.searchable or not all(
                        _named(self._find_record(info.relation, v)) for v in condition.values):
                    continue
                moved = replace(d, metric=replace(d.metric, filters=tuple(conditions[:i]) + (
                    replace(condition, field=field_.label, op=""),) + tuple(conditions[i + 1:])))
                again = self._scope(moved, search=d.kind == "list")
                if isinstance(again, Resolved):
                    continue
                domain = list(again.domain)
                if not self._count(model, domain) and again.default_clauses:
                    domain = [c for c in domain if c not in again.default_clauses]
                    if self._count(model, domain):
                        again.domain, again.default_clauses = domain, []
                        again.described.append(self.say.beyond_defaults())
                if self._count(model, again.domain):
                    found.append((moved, again))
        return found[0] if len(found) == 1 else None

    def _other_link(self, d: Deliverable, view):
        """A description that fits nothing, with a name put on one link of the entity
        ("Joel's quotation" read as Joel the salesperson, when Joel is the customer): the
        one record it fits with that name on another link the index shows."""
        conditions = list(d.metric.filters) if d.metric else []
        entity = self.index.entity(view.key)
        if entity is None or not conditions:
            return None
        links = [f.label for f in entity.fields if f.kind == "link"]
        found: dict[str, tuple] = {}
        for i, condition in enumerate(conditions):
            key = view.field_by_label(condition.field)
            if key is None or view.info(key).kind not in ("link", "text"):
                continue
            for label in links:
                other = view.field_by_label(label)
                if other is None or other == key:
                    continue
                swapped = replace(d, metric=replace(d.metric, filters=tuple(
                    conditions[:i]) + (replace(condition, field=label),) + tuple(conditions[i + 1:])))
                scope = self._scope(swapped, search=True)
                if isinstance(scope, Resolved):
                    continue
                try:
                    rows = self._rows(view.key, scope.domain, limit=2)
                except DataRefused:
                    continue
                if len(rows) == 1:
                    found[rows[0].id] = _found(view.key, rows[0])
        return next(iter(found.values())) if len(found) == 1 else None

    def _linked_instead(self, d: Deliverable, rid: str, name: str):
        """A record named with conditions it does not meet ("Wood Corner's manager": the
        company, with Job Position = Manager) is the way to the records linked to it that
        meet them: the one such record, the several to choose from, or None."""
        view = self.view(d.entity)
        entity = self.index.entity(d.entity)
        scope = self._scope(replace(d, record=""), search=False)
        if isinstance(scope, Resolved) or entity is None or view is None:
            return None
        try:
            if self._count(view.key, scope.domain + [Compare(ID, "eq", rid)]):
                return None
        except DataRefused:
            return None
        link = next((f.label for f in entity.fields
                     if f.kind == "link" and f.relation == view.key
                     and (view.info(view.field_by_label(f.label)) is None
                          or view.info(view.field_by_label(f.label)).searchable)), None)
        if link is None:
            return None
        return self._locate(replace(d, record="", metric=replace(
            d.metric, filters=d.metric.filters + (Filter(field=link, values=(name,)),))))

    def _without_foreign(self, d: Deliverable, view):
        """Words in the person's language on a text field ("三人沙发" as the name) cannot
        match names the application writes in another; when nothing fits, the other
        conditions pick the one record, or none."""
        conditions = list(d.metric.filters) if d.metric else []
        kept = [c for c in conditions
                if not ((view.info(view.field_by_label(c.field)) is not None
                         and view.info(view.field_by_label(c.field)).kind in ("text", "html"))
                        and c.values and all(foreign(v) for v in c.values))]
        if not kept or len(kept) == len(conditions):
            return None
        scope = self._scope(replace(d, metric=replace(d.metric, filters=tuple(kept))), search=True)
        if isinstance(scope, Resolved):
            return None
        try:
            rows = self._rows(view.key, scope.domain, limit=2)
        except DataRefused:
            return None
        if len(rows) != 1:
            return None
        return _found(view.key, rows[0])

    def _elsewhere(self, d: Deliverable, view):
        """A record described by who, how much and when that no record of the declared
        entity fits: the one record of another document entity the same description fits
        exactly. The conditions, not the word, tell an order from an invoice."""
        from .resolver import _mapped_conditions
        conditions = [c for c in (d.metric.filters if d.metric else ()) if says(c)]
        if len(conditions) < 2 or d.page.search:
            return None
        found = []
        for entity in self.index.entities:
            other = self.view(entity.key)
            if (other is None or entity.key == view.key or entity.lines_of or other.schema.lines_of
                    or self._default_page(entity.key) is None):
                continue
            mapped = _mapped_conditions(conditions, view, other)
            if mapped is None:
                continue
            domain: list | None = []
            for condition in mapped:
                clause, _text = self._condition(other, condition.field, condition.values, op=condition.op)
                if clause is None:
                    domain = None
                    break
                domain += clause
            if not domain:
                continue
            try:
                rows = self._rows(entity.key, domain, limit=2)
            except DataRefused:
                continue
            if len(rows) > 1:
                return None
            if rows:
                found.append(_found(entity.key, rows[0]))
        return found[0] if len(found) == 1 else None


def _found(entity: str, row):
    from ..data import Ref
    from .resolver import Found
    return Found(Ref(entity=entity, id=row.id, display=row.display))


def _named(found) -> bool:
    from .resolver import Ambiguous
    return isinstance(found, tuple) and not isinstance(found, Ambiguous)


__all__ = ["LocateMixin"]
