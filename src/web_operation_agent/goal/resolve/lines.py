"""Line entities: the lines a record's page embeds (order lines, invoice lines), how they
are found, scoped and listed, and how conditions that describe the owning record are
moved onto it."""
from __future__ import annotations

import re
from dataclasses import replace

from ..data import EVERYTHING, Compare, any_of
from ..ports import Fact, Resolved
from ..spec import Deliverable, Filter, Metric, PageSpec
from .words import DAY, LAST_DAYS, MONTH, PERIODS, RANGE, norm, period


class LinesMixin:
    """Mixed into `Resolver`."""

    def _parent_link(self, line_model: str) -> tuple[str, str] | None:
        """(owner entity, link field) for an entity some record's page embeds as lines."""
        line_view = self.view(line_model)
        if line_view is None:
            return None
        owner = line_view.schema.lines_of
        owners = [owner] if owner else [
            e.key for e in self.schema.entities
            if e.key != line_model and self._embedded_lines(e.key, line_model) is not None]
        for parent in owners:
            link = next((k for k, f in line_view.fields.items()
                         if f.kind == "link" and f.relation == parent), None)
            if link is not None:
                return parent, link
        return None

    def _embedded_lines(self, parent_model: str, line_model: str):
        """(lines field, the condition picking the embedded lines, their number columns)
        as the owner's page embeds them, or None."""
        view = self.view(parent_model)
        if view is None or self.view(line_model) is None:
            return None
        for key, field in view.fields.items():
            if field.kind == "lines" and field.relation == line_model and field.on_form and field.columns:
                return key, field.embedded, list(field.columns)
        return None

    def _line_facts(self, model: str, rid: str, name: str, labels: list[str]):
        """Values of the record's lines that the labels name ("Office Lamp" on S00004)."""
        view = self.view(model)
        if view is None:
            return None
        for key, info in view.fields.items():
            line_view = self.view(info.relation) if info.kind == "lines" else None
            if line_view is None:
                continue
            parent = next((k for k, f in line_view.fields.items()
                           if f.kind == "link" and f.relation == model), None)
            embedded = self._embedded_lines(model, info.relation)
            if parent is None or embedded is None:
                continue
            _field, line_scope, columns = embedded
            by_name = [k for k in (_item_field(line_view, parent), line_view.schema.name_field) if k]
            facts = []
            for label in labels:
                match = any_of(*(Compare(k, "contains", label) for k in dict.fromkeys(by_name)))
                rows = self._rows(info.relation, [Compare(parent, "eq", rid), line_scope, match],
                                  fields=columns, limit=5)
                for row in rows:
                    if self._evidence is not None:
                        self._evidence.facts(info.relation, line_view, row, columns)
                    for column in columns:
                        facts.append(Fact(subject=f"{name} · {label}", label=line_view.label_of(column),
                                          value=str(row.shown.get(column, "(empty)"))))
            if facts:
                return Resolved(kind="fact", facts=tuple(facts))
        return None

    def _lines_list(self, model: str, line_field: str, owner: tuple, d: Deliverable) -> Resolved:
        """The lines a record's page embeds, as a list of the line entity."""
        view = self.view(model)
        line_model = view.info(line_field).relation
        line_view = self.view(line_model)
        parent = next((k for k, f in line_view.fields.items()
                       if f.kind == "link" and f.relation == model), None)
        if parent is None:
            return Resolved(kind=d.kind, problem=f"{line_view.label} has no link to {model}")
        embedded = self._embedded_lines(model, line_model)
        if embedded is not None and embedded[1] != EVERYTHING:
            self._line_scope = [embedded[1]]
        line_label = norm(view.label_of(line_field))
        columns = tuple(f for f in d.fields if norm(f) != line_label)
        return self._aggregate(Deliverable(
            kind="list", entity=line_model, fields=columns, sort=d.sort,
            descending=d.descending, limit=d.limit,
            metric=Metric(op="count", filters=(Filter(field=line_view.label_of(parent),
                                                      values=(owner[2],)),))))

    def _line_record_from_conditions(self, d: Deliverable):
        """On a line entity, the conditions that are really about the owning record.

        A condition on the link to the owner, or on a name the line entity does not have,
        names the owner or describes it; a condition on a field the owner has is the
        owner's too, and so are the owner page's filters. The owner is found by name, else
        picked out by those conditions like any described record; the deliverable is
        rewritten onto the owner's own lines, keeping the conditions that are the lines'
        own. Returns the deliverable, or a `Resolved` problem when the owner is described
        but not found.
        """
        from .resolver import Ambiguous
        view = self.view(d.entity) if d.entity else None
        parent = self._parent_link(d.entity) if view is not None else None
        if parent is None or (d.metric is None and not d.record and self._found_one is None):
            return d
        if d.metric is None:
            d = replace(d, metric=Metric(op="count"))
        parent_view = self.view(parent[0])
        owners = [e.key for e in self.schema.entities
                  if e.key != d.entity and any(
                      f.kind == "lines" and self.view(f.relation) is not None and f.relation != e.key
                      and self._embedded_lines(e.key, f.relation) is not None
                      for f in (self.view(e.key).fields.values() if self.view(e.key) else ()))]
        owners = [parent[0]] + [m for m in owners if m != parent[0]]

        def proper(hit):
            return isinstance(hit, tuple) and not isinstance(hit, Ambiguous)

        def owner_named(value):
            for model in owners:
                hit = self._find_record(model, value)
                if proper(hit):
                    return hit
            return None

        owner = owner_named(d.record) if d.record else None
        line_filters, parent_filters, words = [], [], []
        if d.record and owner is None and d.kind in ("list", "aggregate") and not proper(
                self._find_record(d.entity, d.record)):
            # The "record" is a description of the owner ("11750"), not a name.
            words.append(d.record)
        for condition in d.metric.filters:
            key = view.field_by_label(condition.field)
            is_link = key == parent[1]
            single = len(condition.values) == 1 and condition.op in ("", "=", "contains")
            value = condition.values[0] if single else ""
            if key is not None and not is_link:
                # A document number under some other field's name still names the owner.
                # A record the field itself links to is the line's own.
                info = view.info(key)
                own = single and info.kind == "link" and proper(self._find_record(info.relation, value))
                hit = owner_named(value) if single and not own and re.search(r"[0-9]", value) \
                    and info.kind not in ("number", "money") else None
                if hit is not None:
                    owner = owner or hit
                    continue
                line_filters.append(condition)
                continue
            if single and d.record and norm(value) == norm(d.record):
                continue
            if single and is_link:
                direct = self._find_record(view.info(key).relation, value)
                if proper(direct):
                    line_filters.append(condition)
                    continue
            hit = owner_named(value) if single else None
            if hit is not None:
                owner = owner or hit
                continue
            if key is None and parent_view is not None \
                    and parent_view.field_by_label(condition.field) is not None:
                parent_filters.append(condition)
            elif single:
                words.append(value)
            elif is_link:
                line_filters.append(condition)
        if owner is None and not words and not parent_filters and self._found_one is not None \
                and self._found_one[0] in owners and not d.record:
            # "The order still to invoice -- its lines": the one record a list in this turn
            # found is the owner.
            owner = self._found_one
        if owner is None and not words and not parent_filters:
            if len(line_filters) == len(d.metric.filters):
                return d
            return replace(d, metric=replace(d.metric, filters=tuple(line_filters)))
        if owner is None:
            page = self.index.page(d.page.key) if d.page.key else None
            on_parent_page = page is not None and page.entity == parent[0]
            mapped, search = [], []
            for value in words:
                parsed, leftover = self._owner_conditions(parent_view, value) if parent_view \
                    else ([], value)
                mapped += parsed
                if leftover:
                    search.append(leftover)
            page_filters = tuple(d.page.filters_on) + tuple(d.metric.page_filters) \
                if on_parent_page else ()
            probe = Deliverable(
                kind="list", entity=parent[0], sort=d.sort, descending=d.descending,
                page=PageSpec(key=page.key if on_parent_page else "", filters_on=page_filters,
                              search=" ".join(search)),
                metric=Metric(op="count", filters=tuple(parent_filters) + tuple(mapped)))
            located = self._locate(probe)
            movable = [f for f in line_filters if parent_view is not None
                       and parent_view.field_by_label(f.field) is not None
                       and view.info(view.field_by_label(f.field)).kind
                       in ("money", "number", "date", "datetime")]
            if isinstance(located, Resolved) and located.candidates and movable:
                # Several owners: an amount or date the lines also have ("Total") was the
                # owner's, said once for both.
                retry = replace(probe, metric=replace(probe.metric,
                                                      filters=probe.metric.filters + tuple(movable)))
                again = self._locate(retry)
                if not isinstance(again, Resolved):
                    located = again
                    line_filters = [f for f in line_filters if f not in movable]
            if isinstance(located, Resolved):
                return replace(located, kind=d.kind)
            owner = located
        line_model = d.entity
        if owner[0] != parent[0]:
            owner_view = self.view(owner[0])
            line_model = next((f.relation for f in owner_view.fields.values()
                               if f.kind == "lines" and self.view(f.relation) is not None
                               and self._embedded_lines(owner[0], f.relation) is not None),
                              d.entity)
        link = self._parent_link(line_model)
        if link is None:
            return d
        line_view = self.view(line_model)
        embedded = self._embedded_lines(link[0], line_model)
        if embedded is not None and embedded[1] != EVERYTHING:
            self._line_scope = [embedded[1]]
        kind = "aggregate" if d.kind == "aggregate" or d.metric.op == "sum" else "list"
        return replace(d, kind=kind, entity=line_model, record="", page=PageSpec(search=d.page.search),
                       metric=replace(d.metric, page_filters=(),
                                      filters=(Filter(field=line_view.label_of(link[1]), values=(owner[2],)),)
                                      + tuple(line_filters)))

    def _owner_conditions(self, view, value: str) -> tuple[list[Filter], str]:
        """What a descriptive value says about a record, as conditions plus leftover words:
        "1541.50 USD last month" is the record's total and its main date; a choice label
        is the field offering it; whatever remains is search words."""
        shown = view.shown
        dates = [k for k in shown if view.info(k).kind in ("date", "datetime")]
        amounts = [k for k in shown if view.info(k).kind == "money"]
        filters: list[Filter] = []
        rest = " " + value.strip() + " "
        today = self.today()
        for phrase in sorted(PERIODS, key=len, reverse=True):
            if dates and re.search(rf"(?<![0-9a-z]){re.escape(phrase)}(?![0-9a-z])", rest, re.I):
                filters.append(Filter(field=view.label_of(dates[0]), values=(phrase,)))
                rest = re.sub(rf"(?<![0-9a-z]){re.escape(phrase)}(?![0-9a-z])", " ", rest, flags=re.I)
                break
        for pattern in (RANGE, DAY, MONTH, LAST_DAYS):
            match = re.search(pattern.pattern.strip("^$"), rest, re.I) if dates else None
            if match and period(match.group(0).strip(), today) is not None:
                filters.append(Filter(field=view.label_of(dates[0]), values=(match.group(0).strip(),)))
                rest = rest.replace(match.group(0), " ")
                break
        money = re.search(r"(?<![0-9.])([0-9][0-9,]*\.[0-9]+|[0-9]{2,}[0-9,]*)\s*(?:[A-Z]{3}|\$|€|美元|元|块)?(?![0-9])", rest)
        amount_field = self._amount_field(view, money.group(1)) if money and amounts else None
        if amount_field is not None:
            filters.append(Filter(field=view.label_of(amount_field), values=(money.group(1),)))
            rest = rest.replace(money.group(0), " ")
        words = " ".join(rest.split())
        wanted = norm(words)
        if wanted:
            for key in shown:
                info = view.info(key)
                if info.kind == "choice" and any(norm(c.label) == wanted for c in info.choices):
                    filters.append(Filter(field=info.label, values=(words,)))
                    words = ""
                    break
        return filters, words

    def _amount_field(self, view, number: str, domain: list | None = None) -> str | None:
        """The money field a bare number refers to: the record's total first (whether or
        not the pages show it), then the money fields the pages show, the first that some
        record actually carries."""
        total = view.schema.total if view.info(view.schema.total) is not None else ""
        shown = ([total] if total else []) + [k for k in view.shown
                                              if view.info(k).kind == "money" and k != total]
        try:
            amount = float(number.replace(",", ""))
        except ValueError:
            return None
        for key in shown[:4]:
            if self._count(view.key, list(domain or []) + [Compare(key, "eq", amount)]):
                return key
        return None


def _item_field(line_view, parent: str) -> str:
    """What a line is of (its product): the first link its list shows besides its owner."""
    return next((k for k in line_view.listed if line_view.info(k).kind == "link" and k != parent), "")


__all__ = ["LinesMixin"]
