"""Line entities of the Odoo goal resolver.
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

from .text import (Ambiguous, _DAY, _FIELD_TAG, _LAST_DAYS, _MONTH, _ModelMeta, _PERIODS, _RANGE, _SKIP_LINE_FIELDS, _literal_domain, _norm, _period, _tokens)


class _LinesResolver:
    """The lines a record's form embeds (order lines, invoice lines): how they are found,
    scoped and listed, and how conditions that really describe the owning record are
    rewritten onto its lines. Mixed into ``OdooGoalPort``."""

    def _parent_link(self, line_model: str) -> tuple[str, str] | None:
        """(parent model, link field) for an entity some record's form embeds as lines."""
        line_meta = self._models.get(line_model)
        if line_meta is None:
            return None
        parents = [line_meta.parent] if line_meta.parent else [
            m for m, meta in self._models.items()
            if m != line_model and self._embedded_lines(m, line_model) is not None]
        for parent in parents:
            link = next((f for f, i in line_meta.fields.items()
                         if i.get("type") == "many2one" and i.get("relation") == parent), None)
            if link is not None:
                return parent, link
        return None

    def _embedded_lines(self, parent_model: str, line_model: str):
        """(one2many field, its static domain, numeric columns) as the parent's form embeds them."""
        meta = self._models.get(parent_model)
        line_meta = self._models.get(line_model)
        if meta is None or line_meta is None:
            return None
        for field, info in meta.fields.items():
            if (info.get("type") != "one2many" or info.get("relation") != line_model
                    or field not in meta.form_fields):
                continue
            domain = info.get("domain")
            domain = _literal_domain(domain) if isinstance(domain, str) else (
                domain if isinstance(domain, list) else [])
            inner = [f for f in dict.fromkeys(_FIELD_TAG.findall(meta.form_arch))
                     if f in line_meta.fields and f not in meta.fields]
            columns = [f for f in inner
                       if line_meta.fields[f].get("type") in ("float", "integer", "monetary")
                       and f not in _SKIP_LINE_FIELDS][:4]
            if not columns:
                continue
            return field, (domain or []), columns
        return None

    def _line_facts(self, model: str, rid: int, name: str, labels: list[str]):
        """Values of the record's lines that the labels name ("Office Lamp" on S00004)."""
        meta = self._models.get(model)
        if meta is None:
            return None
        for field, info in meta.fields.items():
            comodel = str(info.get("relation") or "")
            line_meta = self._models.get(comodel)
            if info.get("type") != "one2many" or line_meta is None:
                continue
            parent = next((f for f, i in line_meta.fields.items()
                           if i.get("type") == "many2one" and i.get("relation") == model), None)
            embedded = self._embedded_lines(model, comodel)
            if parent is None or embedded is None:
                continue
            _field, line_domain, columns = embedded
            read_fields = ["display_name"] + columns + (
                ["currency_id"] if "currency_id" in line_meta.fields else [])
            by_name = [f for f in ("product_id", "name") if f in line_meta.fields]
            facts = []
            for label in labels:
                match = ["|"] * (len(by_name) - 1) + [[f, "ilike", label] for f in by_name]
                rows = self._call(comodel, "search_read",
                                  [[[parent, "=", rid]] + list(line_domain) + match],
                                  {"fields": read_fields, "limit": 5})
                for row in rows:
                    for column in columns:
                        facts.append(Fact(subject=f"{name} · {label}",
                                          label=str(line_meta.fields[column].get("string") or column),
                                          value=self._display(row.get(column), line_meta.fields[column], row)))
            if facts:
                return Resolved(kind="fact", facts=tuple(facts))
        return None

    def _lines_list(self, model: str, line_field: str, owner: tuple, d: Deliverable,
                    index: ApplicationIndex) -> Resolved:
        """The lines a record's form embeds, as a list of the line entity."""
        line_model = str(self._models[model].fields[line_field]["relation"])
        line_meta = self._models[line_model]
        parent = next((f for f, i in line_meta.fields.items()
                       if i.get("type") == "many2one" and i.get("relation") == model), None)
        if parent is None:
            return Resolved(kind=d.kind, problem=f"{line_meta.label} has no link to {model}")
        embedded = self._embedded_lines(model, line_model)
        if embedded is not None and embedded[1]:
            self._line_scope = list(embedded[1])
        parent_label = str(line_meta.fields[parent].get("string") or parent)
        line_label = _norm(self._models[model].fields[line_field].get("string", ""))
        columns = tuple(f for f in d.fields if _norm(f) != line_label)
        return self._aggregate(Deliverable(
            kind="list", entity=line_model, fields=columns, sort=d.sort,
            descending=d.descending, limit=d.limit,
            metric=Metric(op="count", filters=(Filter(field=parent_label, values=(owner[2],)),))),
            index)

    def _line_record_from_conditions(self, d: Deliverable):
        """On a line entity, the conditions that are really about the owning record.

        A condition on the link to the owner, or on a name the line entity does not have,
        names the owner ("Order Reference = S00004", "Move = INV/2026/00002" -- even an
        invoice number on order lines) or describes it ("last month", "1541.50",
        "Quotation Sent"); a condition on a field the owner has ("Total") is the owner's
        too, and so are the owner page's filters. The owner is found by name, else picked
        out by those conditions like any described record; the deliverable is rewritten
        onto the owner's own lines, linked by its exact name, keeping the conditions that
        are the lines' own. Returns the deliverable, or a `Resolved` problem when the
        owner is described but not found.
        """
        meta = self._models.get(d.entity) if d.entity else None
        parent = self._parent_link(d.entity) if meta is not None else None
        if parent is None or (d.metric is None and not d.record and self._found_one is None):
            return d
        if d.metric is None:
            d = replace(d, metric=Metric(op="count"))
        parent_meta = self._models.get(parent[0])
        owners = [m for m, mm in self._models.items()
                  if m != d.entity and any(
                      i.get("type") == "one2many" and i.get("relation") in self._models
                      and i.get("relation") != m
                      and self._embedded_lines(m, str(i["relation"])) is not None
                      for i in mm.fields.values())]
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
            # The "record" is a description of the owner ("11750"), not a name of anything.
            words.append(d.record)
        for condition in d.metric.filters:
            field = meta.field_by_label(condition.field)
            is_link = field == parent[1]
            single = len(condition.values) == 1 and condition.op in ("", "=", "contains")
            value = condition.values[0] if single else ""
            if field is not None and not is_link:
                # A document number under some other field's name ("Invoice" for the
                # invoice date field) still names the owner.
                hit = owner_named(value) if single and re.search(r"[0-9]", value) \
                    and meta.fields[field].get("type") not in ("integer", "float", "monetary") else None
                if hit is not None:
                    owner = owner or hit
                    continue
                line_filters.append(condition)
                continue
            if single and d.record and _norm(value) == _norm(d.record):
                continue
            if single and is_link:
                direct = self._find_record(str(meta.fields[field]["relation"]), value)
                if proper(direct):
                    line_filters.append(condition)
                    continue
            hit = owner_named(value) if single else None
            if hit is not None:
                owner = owner or hit
                continue
            if field is None and parent_meta is not None \
                    and parent_meta.field_by_label(condition.field) is not None:
                parent_filters.append(condition)
            elif single:
                words.append(value)
            elif is_link:
                line_filters.append(condition)
        if owner is None and not words and not parent_filters and self._found_one is not None \
                and self._found_one[0] in owners and not d.record:
            # "The order still to invoice -- its lines": the one record a list in this
            # turn found is the owner.
            owner = self._found_one
        if owner is None and not words and not parent_filters:
            if len(line_filters) == len(d.metric.filters):
                return d
            return replace(d, metric=replace(d.metric, filters=tuple(line_filters)))
        if owner is None:
            page = self.index().page(d.page.key) if d.page.key else None
            on_parent_page = page is not None and page.entity == parent[0]
            mapped, search = [], []
            for value in words:
                parsed, leftover = self._owner_conditions(parent_meta, value) if parent_meta \
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
            located = self._locate(probe, self.index())
            movable = [f for f in line_filters if parent_meta is not None
                       and parent_meta.field_by_label(f.field) is not None
                       and meta.fields[meta.field_by_label(f.field)].get("type")
                       in ("monetary", "float", "integer", "date", "datetime")]
            if isinstance(located, Resolved) and located.candidates and movable:
                # Several owners: an amount or date the lines also have ("Total") was
                # the owner's, said once for both.
                retry = replace(probe, metric=replace(probe.metric,
                                                      filters=probe.metric.filters + tuple(movable)))
                again = self._locate(retry, self.index())
                if not isinstance(again, Resolved):
                    located = again
                    line_filters = [f for f in line_filters if f not in movable]
            if isinstance(located, Resolved):
                return located
            owner = located
        line_model = d.entity
        if owner[0] != parent[0]:
            line_model = next((str(i["relation"]) for i in self._models[owner[0]].fields.values()
                               if i.get("type") == "one2many" and i.get("relation") in self._models
                               and self._embedded_lines(owner[0], str(i["relation"])) is not None),
                              d.entity)
        link = self._parent_link(line_model)
        if link is None:
            return d
        line_meta = self._models[line_model]
        link_label = str(line_meta.fields[link[1]].get("string") or link[1])
        embedded = self._embedded_lines(link[0], line_model)
        if embedded is not None and embedded[1]:
            self._line_scope = list(embedded[1])
        kind = "aggregate" if d.kind == "aggregate" or d.metric.op == "sum" else "list"
        return replace(d, kind=kind, entity=line_model,
                       record="", page=PageSpec(search=d.page.search),
                       metric=replace(d.metric, page_filters=(),
                                      filters=(Filter(field=link_label, values=(owner[2],)),)
                                      + tuple(line_filters)))

    def _owner_conditions(self, meta: _ModelMeta, value: str) -> tuple[list[Filter], str]:
        """What a descriptive value says about a record, as conditions plus leftover words.

        "1541.50 USD last month" is the record's total and its main date; a choice label
        ("Quotation Sent") is the field offering it; whatever remains is search words.
        """
        shown = [f for f in dict.fromkeys(meta.list_fields + meta.form_fields) if f in meta.fields]
        dates = [f for f in shown if meta.fields[f].get("type") in ("date", "datetime")]
        amounts = [f for f in shown if meta.fields[f].get("type") == "monetary"]
        filters: list[Filter] = []
        rest = " " + value.strip() + " "
        today = self._today()
        # Periods: the longest named period first, then a day, month, year or range.
        for phrase in sorted(_PERIODS, key=len, reverse=True):
            if dates and re.search(rf"(?<![0-9a-z]){re.escape(phrase)}(?![0-9a-z])", rest, re.I):
                filters.append(Filter(field=str(meta.fields[dates[0]].get("string") or dates[0]),
                                      values=(phrase,)))
                rest = re.sub(rf"(?<![0-9a-z]){re.escape(phrase)}(?![0-9a-z])", " ", rest, flags=re.I)
                break
        for pattern in (_RANGE, _DAY, _MONTH, _LAST_DAYS):
            match = re.search(pattern.pattern.strip("^$"), rest, re.I) if dates else None
            if match and _period(match.group(0).strip(), today) is not None:
                filters.append(Filter(field=str(meta.fields[dates[0]].get("string") or dates[0]),
                                      values=(match.group(0).strip(),)))
                rest = rest.replace(match.group(0), " ")
                break
        money = re.search(r"(?<![0-9.])([0-9][0-9,]*\.[0-9]+|[0-9]{2,}[0-9,]*)\s*(?:[A-Z]{3}|\$|€|美元|元|块)?(?![0-9])", rest)
        amount_field = self._amount_field(meta, money.group(1)) if money and amounts else None
        if amount_field is not None:
            filters.append(Filter(field=str(meta.fields[amount_field].get("string") or amount_field),
                                  values=(money.group(1),)))
            rest = rest.replace(money.group(0), " ")
        words = " ".join(rest.split())
        wanted = _norm(words)
        if wanted:
            for f in shown:
                info = meta.fields[f]
                if info.get("type") == "selection" and any(_norm(str(lab)) == wanted
                                                           for _k, lab in info.get("selection") or ()):
                    filters.append(Filter(field=str(info.get("string") or f), values=(words,)))
                    words = ""
                    break
        return filters, words

    def _amount_field(self, meta: _ModelMeta, number: str, domain: list | None = None) -> str | None:
        """The money field a bare number refers to: among the money fields the pages
        show, a total before the rest, the first that some record actually carries."""
        # The record's total, whether or not the pages show it as a plain field, then
        # the money fields the pages do show.
        totals = [f for f, i in meta.fields.items()
                  if i.get("type") == "monetary" and _norm(str(i.get("string") or "")) == "total"]
        shown = totals + [f for f in dict.fromkeys(meta.list_fields + meta.form_fields)
                          if meta.fields.get(f, {}).get("type") == "monetary" and f not in totals]
        shown.sort(key=lambda f: (_norm(str(meta.fields[f].get("string") or f)) != "total",
                                  "total" not in _tokens(str(meta.fields[f].get("string") or f))))
        try:
            amount = float(number.replace(",", ""))
        except ValueError:
            return None
        for field in shown[:4]:
            if self._call(meta.model, "search_count", [(domain or []) + [[field, "=", amount]]], {}):
                return field
        return None
