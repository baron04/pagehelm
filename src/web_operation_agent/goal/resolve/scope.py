"""Scopes and conditions: which records a declaration's page, filters and conditions
describe, and the counts, totals, groups and lists over them."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..data import (EVERYTHING, NAME, Compare, Cond, Native, Not, PageSchema, Via,
                    all_of, any_of, parts)
from ..index import IndexPage
from ..ports import Aggregate, Resolved
from ..spec import Deliverable, Filter, Metric, PageSpec
from .view import EntityView
from .words import EMPTY, NOT_EMPTY, norm, period, worded

_RECORD_LIMIT = 40
_GROUP_LIMIT = 20


@dataclass
class Scope:
    """What a declaration's page, filters and conditions select."""
    model: str
    view: EntityView
    page: IndexPage | None
    action: PageSchema | None
    domain: list = field(default_factory=list)
    described: list = field(default_factory=list)
    default_clauses: list = field(default_factory=list)
    #: The records were chosen by the deployment's conventions for the words the request
    #: says; the page's own scope and defaults are part of that choice and are not lifted.
    fixed: bool = False


def says(condition: Filter) -> bool:
    """Whether a condition compares against something: a value, or none with not-equal
    or equal, which say the field is set or is not."""
    return bool(condition.values) or condition.op in ("!=", "=")


def _field_of(cond: Cond) -> str | None:
    if isinstance(cond, Compare):
        return cond.field
    if isinstance(cond, Native) and len(cond.fields) == 1:
        return cond.fields[0]
    return None


class ScopeMixin:
    """Mixed into `Resolver`."""

    def _scope(self, d: Deliverable, *, search: bool):
        """The records a deliverable's page, filters and conditions describe, as a
        `Scope`, or a `Resolved` problem when a name in the declaration has no meaning.

        The page scopes by its own scope and default filters (except ones about the
        current user); an entity alone means every record of it the person may read.
        Page filters switched on narrow, ones switched off exclude; conditions are field
        comparisons; search words, when asked for, are what the search box would do.
        """
        index = self.index
        page = index.page(d.page.key) if d.page.key else None
        entity = index.entity(d.entity) if d.entity else None
        if page is None and entity is None:
            return Resolved(kind=d.kind, problem="no entity or page names this scope")
        prefix = ""
        if page is not None and entity is not None and page.entity != entity.key:
            parent = self._parent_link(entity.key)
            if parent is not None and parent[0] == page.entity:
                # The lines of the records a page shows: the page's scope reaches the
                # lines through their link.
                prefix = parent[1]
            else:
                # The lines of an order declared with the orders page: the entity named
                # is the one asked about; the page was the way there, not the scope.
                page = None
        model = page.entity if page is not None and not prefix else entity.key
        view = self.view(model)
        if view is None:
            return Resolved(kind=d.kind, problem="no entity or page names this scope")
        today = self.today()
        action = self.schema.page(page.key) if page is not None else None

        def through(conds):
            return [Via(prefix, c) if prefix else c for c in conds]

        domain = through(list(parts(action.scope))) if action is not None and action.scope != EVERYTHING else []
        if prefix and not self._line_scope:
            embedded = self._embedded_lines(page.entity, entity.key)
            if embedded is not None and embedded[1] != EVERYTHING:
                self._line_scope = [embedded[1]]
        if self._line_scope:
            # The lines as the record's form embeds them (product lines, not tax lines).
            domain += self._line_scope
            self._line_scope = None
        described = [page.label if page is not None else view.label]
        default_clauses: list = []
        if action is not None:
            for item in action.defaults:
                domain += through([item.cond])
                default_clauses += through([item.cond])
        fixed = False
        scopes = [c for c in self._named_scopes if c.entity == model] if not prefix else []
        if scopes:
            # The words the request says, as this organisation means them ("订单" is every
            # order, quotations included): the records they name are never left out by
            # the page the declaration went through. The page's own scope stays, widened
            # by what the words name.
            branches = []
            for convention in scopes:
                clauses: list | None = []
                for label, values in convention.conditions:
                    clause, _text = self._condition(view, label, values, today=today)
                    if clause is None:
                        clauses = None
                        break
                    clauses += clause
                if clauses is not None:
                    branches.append(clauses)
            if branches:
                # The words widen the page along what this organisation's words for the
                # entity are about (Status); the page's other clauses stay.
                dims = {view.field_by_label(label) for c in index.conventions
                        if c.entity == model for label, _values in c.conditions} - {None}
                kept: list = []
                if dims and all(_field_of(c) is not None for c in domain):
                    kept = [c for c in domain if _field_of(c) not in dims]
                    domain = [c for c in domain if _field_of(c) in dims]
                if any(not branch for branch in branches) or (page is not None and not domain):
                    domain = []
                elif page is not None:
                    domain = [any_of(all_of(*domain), *(all_of(*b) for b in branches))]
                else:
                    domain = [any_of(*(all_of(*b) for b in branches))]
                domain = kept + domain
                default_clauses = [c for c in default_clauses if c in kept]
                fixed = True
                described.append(self.say.as_meant([c.terms[0] for c in scopes]))
        metric = d.metric or Metric(op="count")
        page_filters = list(action.filters) if action is not None else list(view.schema.filters)
        declared = list(metric.page_filters) + [f for f in d.page.filters_on if f not in metric.page_filters]
        for name in declared:
            wanted = norm(name)
            item = next((f for f in page_filters if norm(f.label) == wanted), None) \
                or view.filter_by_name(name)
            if item is not None and item.date_field and item.relative:
                # A date filter switched on without a period narrows nothing.
                continue
            if item is None and page is not None and not page.filters:
                # The page serves no filters, so the name is the model's own words for the
                # page's scope, which the page already has.
                continue
            if item is None or item.cond is None or not item.usable:
                return Resolved(kind=d.kind, problem=f"filter {name!r} is not usable here",
                                repair=(f"{name!r} is not a filter of this page; its filters "
                                        "are: " + ", ".join(f.label for f in page_filters[:20])
                                        + "; a condition on a field goes in metric.filters"))
            domain += through([item.cond])
            described.append(item.label)
        for name in d.page.filters_off:
            # A filter switched off excludes what it would select.
            wanted = norm(name)
            item = next((f for f in page_filters if norm(f.label) == wanted), None) \
                or view.filter_by_name(name)
            if item is None or item.cond is None or not item.usable:
                return Resolved(kind=d.kind, problem=f"filter {name!r} is not usable here")
            domain += through([Not(item.cond)])
            described.append(self.say.excluding(item.label))
        for condition in metric.filters:
            if not says(condition):
                continue
            exact_field = any(norm(f.label) == norm(condition.field) and f.searchable
                              for f in view.fields.values())
            if not exact_field:
                # A date filter's name ("Accounting Date") stands for the field it offers.
                dated = next((f for f in page_filters + list(view.schema.filters)
                              if f.date_field in view.fields and norm(f.label) == norm(condition.field)),
                             None)
                if dated is not None:
                    condition = Filter(field=view.label_of(dated.date_field),
                                       values=condition.values, op=condition.op)
                    exact_field = True
            if not exact_field:
                # Perhaps the page's own filter, named by its label or under a generic
                # word ("filter": "To pay").
                named = [f for f in page_filters
                         if any(norm(f.label) == norm(v) or (len(norm(v)) >= 4
                                and norm(f.label).startswith(norm(v).rstrip("y")))
                                for v in condition.values)]
                if named and named[0].cond is not None:
                    domain += [named[0].cond]
                    described.append(named[0].label)
                    continue
            clause, text = self._condition(view, condition.field, condition.values,
                                           op=condition.op, today=today)
            if clause is None and text.startswith("several "):
                # A name several records hold is asked about only when more than one of
                # them is among the records the rest of the declaration describes.
                held, named_field = self._held_by, view.field_by_label(condition.field)
                scope = list(domain)
                complete_scope = condition.op != "!="
                for other in metric.filters:
                    if other is condition or not says(other):
                        continue
                    other_clause, _ = self._condition(view, other.field, other.values,
                                                      op=other.op, today=today)
                    complete_scope = complete_scope and other_clause is not None
                    scope += other_clause or []
                present = []
                if complete_scope:
                    present = [(rid, name) for rid, name in self._links_within(
                        model, named_field, scope, complete=True) if name in held]
                if len(present) != 1:
                    return Resolved(kind=d.kind, problem=text, candidates=tuple(
                        name for _rid, name in present) if len(present) > 1 else held)
                rid, name = present[0]
                clause, text = [Compare(named_field, "eq", rid)], self.say.compare(condition.field, "=", (name,))
            if clause is None:
                named_field = view.field_by_label(condition.field)
                if (named_field is not None and text.startswith("no ")
                        and view.info(named_field).kind == "link"):
                    # No record by that name: the names this field actually has among the
                    # records the other conditions describe, to match the words against.
                    scope = list(domain)
                    for other in metric.filters:
                        if other is condition or not says(other):
                            continue
                        other_clause, _ = self._condition(view, other.field, other.values,
                                                          op=other.op, today=today)
                        if other_clause:
                            scope += other_clause
                    names = self._names_of(model, named_field, scope)
                    if names:
                        return Resolved(kind=d.kind, problem=text, repair=(
                            f"{text}; the {condition.field} values here are: "
                            + ", ".join(names) + "; use the one the person means, written "
                            "exactly as it is here"))
                if view.field_by_label(condition.field) is None:
                    from .resolver import _fields_of
                    repair = (f"{condition.field!r} is not a field of {view.label}; its fields "
                              "are: " + (_fields_of(index, model) or ", ".join(
                                  f.label for f in view.schema.fields[:30])))
                else:
                    repair = (f"the value {' / '.join(condition.values)!r} cannot be used on "
                              f"{condition.field}: {text}; use a choice label from the index, "
                              "a number, or a date (2026-09-12, 2026-09, last month)")
                return Resolved(kind=d.kind, problem=text, repair=repair)
            domain += clause
            described.append(text)
        # Search words within a condition's value, or holding it, say again what the
        # condition says.
        said = {norm(v) for c in metric.filters for v in c.values if norm(v)} | (
            {norm(d.record)} if d.record else set())
        words = norm(d.page.search)
        if search and d.page.search and not any(words in v or v in words for v in said):
            clause, text = self._search_clause(view, model, domain, d.page.search)
            if clause is None and default_clauses:
                # Nothing within the page's default filters: the words may still name
                # something the page's entity holds once those are lifted.
                lifted = [c for c in domain if c not in default_clauses]
                clause, text = self._search_clause(view, model, lifted, d.page.search)
                if clause is not None:
                    domain, default_clauses = lifted, []
                    described.append(self.say.beyond_defaults())
            domain += clause or [Compare(NAME, "contains", d.page.search)]
            described.append(text)
        return Scope(model=model, view=view, page=page, action=action, domain=domain,
                     described=described, default_clauses=default_clauses, fixed=fixed)

    # -- aggregates and lists -----------------------------------------------------------------
    def _aggregate(self, d: Deliverable) -> Resolved:
        scope = self._scope(d, search=d.kind == "list")
        if isinstance(scope, Resolved):
            return scope
        moved = self._name_on_link(d, scope)
        if moved is not None:
            d, scope = moved
        model, view, page, action = scope.model, scope.view, scope.page, scope.action
        domain, described, default_clauses = scope.domain, scope.described, scope.default_clauses
        metric = d.metric or Metric(op="count")
        subject = " · ".join(described)
        if d.kind == "list" and default_clauses and not self._count(model, domain):
            # The page's own default filters hid every match; the person asked about the
            # page's entity, so the answer says so and looks past the defaults.
            for clause in default_clauses:
                domain.remove(clause)
            described.append(self.say.beyond_defaults())
        if d.kind == "list":
            columns = list(dict.fromkeys(f for f in (view.field_by_label(label) for label in d.fields) if f))
            limit = d.limit or _RECORD_LIMIT
            order = ()
            if d.sort:
                order_field = view.field_by_label(d.sort)
                if order_field is None:
                    return Resolved(kind=d.kind, problem=f"cannot sort by {d.sort!r}")
                order = ((order_field, d.descending),)
                described.append(self.say.ordered(view.label_of(order_field), d.descending))
            rows = self._rows(model, domain, fields=columns, order=order, limit=limit + 1)
            names = []
            for row in rows[:limit]:
                cells = [f"{view.label_of(f)}: {row.shown.get(f, '(empty)')}" for f in columns]
                names.append(row.display + (" — " + "; ".join(cells) if cells else ""))
            total = self._count(model, domain)
            if len(rows) == 1:
                self._found_one = (model, rows[0].id, rows[0].display)
            # Keep a link to the list page; a separate page declaration, when requested,
            # owns navigation and facet verification.
            bare = PageSpec(key=d.page.key if page is not None else "")
            plan = self._list_page(Deliverable(kind="page", entity=model, page=bare)) \
                if (page is not None or self._default_page(model) is not None) else None
            page_plan = plan.page if plan is not None else None
            if not names and not (d.metric and d.metric.filters) and not d.page.search:
                # Nothing was asked of the records but to list them: none is the answer.
                return Resolved(kind="list", page=page_plan,
                                aggregate=Aggregate(subject=" · ".join(described), count=0,
                                                    records=(), complete=True))
            return Resolved(kind="list", page=page_plan,
                            aggregate=Aggregate(subject=" · ".join(described), count=int(total),
                                                records=tuple(names), complete=len(rows) <= limit))
        count = self._count(model, domain)
        widened = None
        if count == 0 and default_clauses:
            for clause in default_clauses:
                domain.remove(clause)
            count = self._count(model, domain)
            if count:
                described.append(self.say.beyond_defaults())
        if count == 0 and action is not None and action.scope != EVERYTHING:
            # Nothing in this page's scope; the same conditions over the whole entity say
            # whether the records exist at all (quotations, when the page shows orders).
            page_parts = parts(action.scope)
            widened = self._count(model, [c for c in domain if c not in page_parts])
        sums: list[tuple[str, str]] = []
        if metric.op == "sum":
            key = view.field_by_label(metric.field) if metric.field else None
            info = view.info(key)
            if info is None or info.kind not in ("money", "number"):
                return Resolved(kind=d.kind, problem=f"{metric.field!r} cannot be summed")
            self._reads.append(("total", model))
            amount, unit = self.port.total(model, all_of(*domain), key)
            sums.append((info.label, f"{amount:,.2f} {unit if count else ''}".strip()))
        groups_out: list[tuple[str, int]] = []
        if metric.group_by:
            key = view.field_by_label(metric.group_by)
            if key is None:
                return Resolved(kind=d.kind, problem=f"cannot group by {metric.group_by!r}")
            self._reads.append(("groups", model))
            for group in self.port.groups(model, all_of(*domain), key, limit=metric.top or _GROUP_LIMIT,
                                          by_month=view.info(key).kind in ("date", "datetime")):
                groups_out.append((group.label, group.count))
        note = self.say.any_status(view.label, widened) if widened else ""
        return Resolved(kind="aggregate", aggregate=Aggregate(
            subject=subject, count=count, sums=tuple(sums), groups=tuple(groups_out), note=note))

    # -- conditions ---------------------------------------------------------------------------
    def _search_clause(self, view: EntityView, model: str, domain: list, words: str):
        """What search words mean on this entity within `domain`, or None when nothing."""
        today = self.today()
        if period(words, today) is not None:
            dates = [f for f in view.shown if view.info(f).kind in ("date", "datetime")]
            if dates:
                clause, text = self._date_condition(view, dates[0], (words,), "", today)
                if clause is not None:
                    return clause, text
        clause = [Compare(NAME, "contains", words)]
        if self._count(model, domain + clause):
            return clause, self.say.words(words)
        if re.fullmatch(r"[0-9][0-9,]*(?:\.[0-9]+)?", words.strip()):
            # A bare number that names nothing is an amount (an order's total).
            key = self._amount_field(view, words.strip(), domain)
            if key is not None:
                return ([Compare(key, "eq", float(words.replace(",", "")))],
                        f"{view.label_of(key)} = {words}")
        # No name contains the words; they may name a linked record instead ("United
        # States" on customers is their country). Only a record actually named by the
        # words counts, and several links to the same record are one hit, on the field
        # the list shows.
        shown, listed = view.shown, set(view.listed)
        hits: dict[tuple, str] = {}
        for key in shown:
            info = view.info(key)
            if info.kind != "link":
                continue
            found = self._find_record(info.relation, words)
            if not (isinstance(found, tuple) and not _ambiguous(found)):
                continue
            if norm(words) in (norm(self._own_name(found[2])), norm(found.personal)):
                hit = (found[0], found[1])
                if hit not in hits or (key in listed and hits[hit] not in listed):
                    hits[hit] = key
        if len(hits) == 1:
            (_m, rid), key = next(iter(hits.items()))
            clause = [Compare(key, "under" if view.info(key).inclusive else "in", (rid,))]
            if self._count(model, domain + clause):
                return clause, self.say.compare(view.label_of(key), "=", (words,))
        # Or the words are in one text field the pages show (a city).
        texts = [k for k in shown if view.info(k).kind == "text" and k != view.schema.name_field
                 and view.info(k).searchable]
        matched = [k for k in texts if self._count(model, domain + [Compare(k, "contains", words)])]
        if len(matched) == 1:
            return [Compare(matched[0], "contains", words)], self.say.holds(view.label_of(matched[0]), (words,))
        return None, self.say.words(words)

    def _condition(self, view: EntityView, label: str, values: tuple[str, ...], *,
                   op: str = "", today=None):
        key = view.field_by_label(label)
        if key is None:
            return None, f"no field {label!r} on {view.label}"
        info = view.info(key)
        kind = info.kind
        text = self.say.compare(info.label, op, values)
        if not values and op in ("!=", "="):
            # "Entries with a partner": not equal to nothing, so the field is set;
            # "orders with no campaign": equal to nothing, so it is not.
            return ([Compare(key, "set" if op == "!=" else "unset")],
                    self.say.filled(info.label, op == "!="))
        if not values:
            return None, f"no value for {label!r}"
        if not info.searchable:
            # A computed value: the application would ignore the condition and count
            # everything, which is worse than saying no.
            return None, f"{info.label} cannot be used as a condition"
        emptiness = norm(values[0]) if len(values) == 1 else ""
        if emptiness in EMPTY or emptiness in NOT_EMPTY:
            wanted_set = emptiness in NOT_EMPTY or op == "!="
            return ([Compare(key, "set" if wanted_set else "unset")],
                    self.say.filled(info.label, wanted_set))
        if kind in ("date", "datetime"):
            return self._date_condition(view, key, values, op, today or self.today())
        if op == "!=" and kind in ("link", "choice"):
            clause, text = self._condition(view, label, values, today=today)
            if clause is None:
                return clause, text
            if text == self.say.compare(info.label, "", values):
                text = self.say.compare(info.label, "!=", values)
            return [_negated(c) for c in clause], text
        if kind == "link":
            # A name as the application displays it may carry more than the name (a code
            # in brackets); the record's own name is what a person gives.
            values = [self._own_name(v) for v in values]
            ids = []
            for value in values:
                found = self._find_record(info.relation, value)
                if _ambiguous(found) and op != "contains":
                    # A name several records hold: which one is meant is asked, as for
                    # the record a request is about.
                    self._held_by = tuple(found)
                    return None, f"several {info.label} records are named {value!r}"
                if found is None or op == "contains":
                    # Not one record's name: the records whose names contain the words,
                    # which the search box would also find.
                    clause = any_of(*(Compare(key, "contains", v) for v in values))
                    if found is None and not self._count(view.key, [clause]):
                        # The words in any order, a plural as its singular; a variant's
                        # attributes in parentheses dropped.
                        bare = [re.sub(r"\s*\([^()]*\)$", "", v) for v in values]
                        if bare != values:
                            plain = any_of(*(Compare(key, "contains", v) for v in bare))
                            if self._count(view.key, [plain]):
                                return [plain], self.say.holds(info.label, values)
                        alternatives = worded(values)
                        if alternatives:
                            cond = any_of(*(all_of(*(Compare(key, "contains", w) for w in words))
                                            for words in alternatives))
                            if self._count(view.key, [cond]):
                                return [cond], self.say.holds(info.label, values)
                        return None, f"no {info.label} matches {value!r}"
                    return [clause], self.say.holds(info.label, values)
                ids.append(found[1])
            return [Compare(key, "under" if info.inclusive else "in", tuple(ids))], text
        if kind == "choice":
            keys = []
            for value in values:
                wanted = norm(value)
                spaced = " ".join(value.split())
                match = next((c.key for c in info.choices if norm(c.label) == wanted or c.key == value), None)
                if match is None:
                    # A choice named in the person's language, as the application
                    # translates it (已过账 for Posted).
                    match = next((c.key for c in info.choices if spaced in c.aliases), None)
                if match is None:
                    match = next((c.key for c in info.choices if wanted in norm(c.label)), None)
                if match is None:
                    return None, f"{value!r} is not a {info.label} choice"
                keys.append(match)
            return [Compare(key, "in", tuple(keys))], text
        if kind == "bool":
            return [Compare(key, "eq", norm(values[0]) in ("true", "yes", "1", "是"))], text
        if kind in ("number", "money"):
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
                return [Compare(key, "ge", numbers[0]), Compare(key, "le", numbers[1])], text
            return [Compare(key, _OPS.get(op, "eq"), numbers[0])], text
        if op == "starts with":
            clauses = [Compare(key, "starts", v.replace("%", "")) for v in values]
        else:
            clauses = [Compare(key, {"=": "eq", "!=": "not_contains"}.get(op, "contains"), v)
                       for v in values]
        return [any_of(*clauses)], text

    def _date_condition(self, view: EntityView, key: str, values: tuple[str, ...], op: str, today):
        label = view.label_of(key)
        if op == "between" and len(values) >= 2:
            first, second = period(values[0], today), period(values[1], today)
            if first is None or second is None or first[0] is None or second[1] is None:
                return None, f"{values[0]!r}..{values[1]!r} is not a date range"
            return ([Compare(key, "ge", first[0]), Compare(key, "lt", second[1])],
                    self.say.between(label, values[0], values[1]))
        span = period(values[0], today)
        if span is None:
            return None, f"{values[0]!r} is not a date or period"
        start, end = span
        text = self.say.during(label, op, values[0])
        if op in ("<", "<="):
            edge = end if op == "<=" else start
            return [Compare(key, "lt", edge if edge is not None else end)], text
        if op in (">", ">="):
            edge = start if op == ">=" else end
            return [Compare(key, "ge", edge if edge is not None else start)], text
        clauses = []
        if start is not None:
            clauses.append(Compare(key, "ge", start))
        if end is not None:
            clauses.append(Compare(key, "lt", end))
        if op == "!=":
            # An absent date is not a known date outside the requested period.
            # Keep this explicit so SQL and in-memory adapters agree on NULL.
            return [Compare(key, "set"), Not(all_of(*clauses))], text
        return clauses, text


_OPS = {"<": "lt", "<=": "le", ">": "gt", ">=": "ge", "!=": "ne", "=": "eq"}


def _negated(cond: Cond) -> Cond:
    if isinstance(cond, Compare) and cond.op in ("in", "under"):
        return Compare(cond.field, "not_in", cond.value)
    if isinstance(cond, Compare) and cond.op == "eq":
        return Compare(cond.field, "ne", cond.value)
    return Not(cond)


def _ambiguous(found) -> bool:
    from .resolver import Ambiguous
    return isinstance(found, Ambiguous)


__all__ = ["Scope", "ScopeMixin", "says"]
