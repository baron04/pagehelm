"""Odoo as a core `DataPort`: the schema from its runtime metadata, conditions as domains.

Mixed into ``OdooGoalPort`` so that the schema shares the metadata the index compiles.
Everything is read-only JSON-RPC under the delegated user.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time as time_of_day, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from web_operation_agent.business.runtime import BusinessToolError
from web_operation_agent.goal.data import (EVERYTHING, ID, NAME, All, Any, Choice, Compare,
                                            Cond, DataRefused, EntitySchema, FieldSchema,
                                            FilterSchema, Group, LinkValue, Native, Not,
                                            PageSchema, Ref, Row, Schema, Via, all_of)

from .text import (_ModelMeta, _SearchFilter, _all_of, _domain_terms, _literal_dict,
                   _literal_domain, _strip_code, _tab_of)

_KINDS = {"char": "text", "text": "text", "integer": "number", "float": "number",
          "monetary": "money", "date": "date", "datetime": "datetime", "selection": "choice",
          "many2one": "link", "many2many": "links", "one2many": "lines", "html": "html",
          "boolean": "bool"}

_TRUE = [1, "=", 1]


class _OdooData:
    """The `DataPort` side of ``OdooGoalPort``."""

    # -- schema ---------------------------------------------------------------------------
    def schema(self) -> Schema:
        self.index()
        entities = tuple(self._entity_schema(model) for model in self._models)
        pages = tuple(self._page_schema(action) for action in self._actions)
        return Schema(entities=entities, pages=pages)

    def entity_schema(self, model: str) -> EntitySchema:
        """One entity's schema, compiling its metadata when the index did not."""
        if model not in self._models:
            self._models[model] = self._model_meta(model)
        return self._entity_schema(model)

    def _entity_schema(self, model: str) -> EntitySchema:
        meta: _ModelMeta = self._models[model]
        listed, on_form = set(meta.list_fields), set(meta.form_fields)
        order = list(dict.fromkeys(meta.list_fields + meta.form_fields))
        names = order + [n for n in meta.fields if n not in order]
        # A draft form's quick-entry inputs (Total (Tax inc.) typed to prefill lines) are
        # named like the record's own values but are never them.
        fields = tuple(self._field_schema(meta, name, name in listed, name in on_form)
                       for name in names if name in meta.fields and not name.startswith("quick_edit_"))
        return EntitySchema(key=model, label=meta.label, fields=fields,
                            filters=tuple(self._filter_schema(f) for f in meta.filters),
                            sections=meta.sections, actions=meta.actions,
                            operations=frozenset(_OPERATIONS.get(o, o)
                                                 for o in self._operations.get(model, ())),
                            lines_of=meta.parent,
                            name_field="name" if "name" in meta.fields else "",
                            total="amount_total" if "amount_total" in meta.fields else "")

    def _field_schema(self, meta: _ModelMeta, name: str, listed: bool, on_form: bool) -> FieldSchema:
        info = meta.fields[name]
        kind = _KINDS.get(str(info.get("type")), "other")
        relation = str(info.get("relation") or "")
        choices = tuple(Choice(key=str(k), label=str(l), aliases=self._translations.of(str(l)))
                        for k, l in (info.get("selection") or ())) if kind == "choice" else ()
        inverse, embedded, columns = "", EVERYTHING, ()
        if kind == "lines":
            embedded_lines = self._embedded_lines(meta.model, relation)
            if embedded_lines is not None and embedded_lines[0] == name:
                parent = self._parent_link(relation)
                inverse = parent[1] if parent is not None else ""
                embedded = (_native(list(embedded_lines[1]), "", list(embedded_lines[1]))
                            if embedded_lines[1] else EVERYTHING)
                columns = tuple(embedded_lines[2])
        label = str(info.get("string") or name)
        return FieldSchema(
            key=name, label=label, kind=kind, relation=relation, choices=choices,
            searchable=info.get("searchable") is not False, listed=listed, on_form=on_form,
            tab=_tab_of(meta.form_arch, name) if on_form else "",
            aliases=self._translations.of(label), inverse=inverse, embedded=embedded,
            columns=columns,
            # A customer's contacts count as the customer; a contact's own link to its
            # company selects the company's people, never the company itself.
            inclusive=relation == "res.partner" and relation != meta.model)

    def _filter_schema(self, item: _SearchFilter) -> FilterSchema:
        today = self._today()
        domain = item.domain_at(today)
        usable = domain is not None and "uid" not in str(domain)
        return FilterSchema(key=item.name, label=item.label,
                            cond=_native(item, item.label, domain) if domain is not None else None,
                            date_field=item.date_field, group_by=item.group_by, usable=usable,
                            relative=item.domain is None)

    def _page_schema(self, action: dict) -> PageSchema:
        model = action["model"]
        meta = self._models[model]
        domain = _literal_domain(str(action.get("domain") or ""))
        pool = self._page_filters.get(action["action_id"], meta.filters)
        defaults = []
        for key, value in _literal_dict(str(action.get("context") or "")).items():
            if not (str(key).startswith("search_default_") and value):
                continue
            name = key[len("search_default_"):]
            item = next((f for f in pool if f.name == name), None)
            if item is None:
                # The page's own search view was not served to this user, but the default
                # names a choice of a selection field, as these filters are written.
                field = next((f for f, info in meta.fields.items()
                              if info.get("type") == "selection"
                              and any(k == name for k, _l in info.get("selection") or ())), None)
                if field is None:
                    continue
                item = _SearchFilter(name, name, [[field, "=", name]], "")
            schema = self._filter_schema(item)
            if schema.usable and item.domain_at(self._today()):
                defaults.append(schema)
        return PageSchema(
            key=f"p{action['action_id']}", entity=model, label=action["name"],
            scope=_native(domain, action["name"], domain) if domain else EVERYTHING,
            defaults=tuple(defaults),
            filters=tuple(self._filter_schema(f) for f in pool),
            opens_records="form" in (action.get("view_modes") or ()),
            operations=(("create",) if "create" in self._operations.get(model, ())
                        and "form" in (action.get("view_modes") or ()) else ()),
            menu=bool(action.get("breadcrumbs")))

    # -- reads --------------------------------------------------------------------------------
    def time_zone(self) -> str:
        zone = getattr(self._client, "time_zone", None)
        if not callable(zone):
            return ""
        try:
            return str(zone() or "")
        except BusinessToolError:
            return ""

    def find(self, entity: str, text: str, *, limit: int = 8) -> tuple[Ref, ...]:
        rows = self._data_call(entity, "name_search", [text], {"limit": limit})
        plain = _strip_code(text)
        if not rows and plain and plain != text.strip():
            # A name as the application displays it, its code in brackets first
            # ("[FURN_6741] Large Meeting Table"): the name search knows the name alone.
            code = re.match(r"^\[([^\]]*)\]", text.strip())
            rows = [(rid, name) for rid, name in self._data_call(
                entity, "name_search", [plain], {"limit": limit})
                if not code or name.startswith(f"[{code.group(1)}]") or "[" not in name]
        return tuple(self._ref(entity, rid, name) for rid, name in rows)

    def labels_for(self, text: str) -> tuple[str, ...]:
        return tuple(self._translations.back(text))

    def name_of(self, display: str) -> str:
        return _strip_code(display) or display

    @staticmethod
    def _ref(entity: str, rid, name) -> Ref:
        display = str(name)
        # A contact of a company is displayed "Company, Person".
        owner = display.rsplit(",", 1)[0].strip() if "," in display else ""
        return Ref(entity=entity, id=str(rid), display=display, name=_strip_code(display),
                   owner=owner)

    def read(self, entity: str, id: str, fields: tuple[str, ...]) -> Row:
        meta = self._meta(entity)
        wanted = self._read_fields(meta, fields)
        rows = self._data_call(entity, "read", [[int(id)]], {"fields": wanted})
        return self._row(meta, rows[0] if rows else {"id": id}, fields)

    def count(self, entity: str, where: Cond) -> int:
        return int(self._data_call(entity, "search_count", [self.domain(entity, where)], {}))

    def rows(self, entity: str, where: Cond, *, fields: tuple[str, ...] = (),
             order: tuple[tuple[str, bool], ...] = (), limit: int = 80) -> tuple[Row, ...]:
        meta = self._meta(entity)
        kwargs: dict = {"fields": ["display_name"] + self._read_fields(meta, fields),
                        "limit": limit}
        if order:
            keys = [({NAME: "display_name", ID: "id"}.get(f, f), d) for f, d in order]
            kwargs["order"] = ", ".join(f"{f} {'desc' if d else 'asc'}" for f, d in keys)
            if keys[-1][0] != "id":
                kwargs["order"] += ", id"
        rows = self._data_call(entity, "search_read", [self.domain(entity, where)], kwargs)
        return tuple(self._row(meta, row, fields) for row in rows)

    def total(self, entity: str, where: Cond, field: str) -> tuple[float, str]:
        meta = self._meta(entity)
        domain = self.domain(entity, where)
        groups = self._data_call(entity, "read_group", [domain, [f"{field}:sum"], []], {"lazy": False})
        amount = float((groups[0].get(field) if groups else 0) or 0.0)
        unit = ""
        if meta.fields.get(field, {}).get("type") == "monetary" and "currency_id" in meta.fields:
            sample = self._data_call(entity, "search_read", [domain], {"fields": ["currency_id"], "limit": 1})
            value = sample[0].get("currency_id") if sample else None
            unit = value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else ""
        return amount, unit

    def groups(self, entity: str, where: Cond, field: str, *, limit: int = 12,
               by_month: bool = False) -> tuple[Group, ...]:
        spec = f"{field}:month" if by_month else field
        rows = self._data_call(entity, "read_group", [self.domain(entity, where), [field], [spec]],
                               {"lazy": False, "orderby": "__count desc", "limit": limit})
        out = []
        for row in rows:
            value = row.get(spec, row.get(field))
            if isinstance(value, (list, tuple)) and len(value) > 1:
                key, label = str(value[0]), str(value[1])
            else:
                key = label = str(value)
            out.append(Group(key=key, label=label, count=int(row.get("__count") or 0)))
        return tuple(out)

    def page_address(self, page: str, *, view: str = "") -> str:
        action = next((a for a in self._actions if f"p{a['action_id']}" == page), None)
        if action is None:
            return ""
        url = self._action_url(action)
        return f"{url}?view_type={view}" if view else url

    def record_address(self, entity: str, id: str) -> str:
        candidates = [a for a in self._actions
                      if a["model"] == entity and "form" in (a.get("view_modes") or ())]
        candidates.sort(key=lambda a: (str(a.get("route") or "").startswith("action-"),
                                       not a.get("breadcrumbs")))
        for action in candidates:
            if not action.get("breadcrumbs"):
                continue
            domain = _literal_domain(str(action.get("domain") or ""))
            if domain is None:
                continue
            if domain and not self._data_call(entity, "search_count",
                                              [[["id", "=", int(id)]] + domain], {}):
                continue
            return self._action_url(action, int(id))
        return f"/odoo/{entity}/{id}"

    # -- conditions ---------------------------------------------------------------------------
    def domain(self, entity: str, cond: Cond) -> list:
        """A condition as an Odoo domain in prefix notation."""
        if cond == EVERYTHING:
            return []
        if isinstance(cond, All):
            # At the top a domain's terms are and-ed without an operator, as Odoo writes it.
            return [x for c in cond.items for x in self._top(entity, c)]
        return self._top(entity, cond)

    def _top(self, entity: str, cond: Cond) -> list:
        if isinstance(cond, Native) and not isinstance(cond.payload, _SearchFilter):
            return list(cond.payload or [])
        return self._term(entity, cond)

    def _term(self, entity: str, cond: Cond) -> list:
        if isinstance(cond, All):
            if not cond.items:
                return [_TRUE]
            terms = [self._term(entity, c) for c in cond.items]
            return ["&"] * (len(terms) - 1) + [x for t in terms for x in t]
        if isinstance(cond, Any):
            if not cond.items:
                return [[0, "=", 1]]
            terms = [self._term(entity, c) for c in cond.items]
            return ["|"] * (len(terms) - 1) + [x for t in terms for x in t]
        if isinstance(cond, Not):
            return ["!"] + self._term(entity, cond.item)
        if isinstance(cond, Via):
            target = str(self._field_info(entity, cond.link).get("relation") or entity)
            return [[cond.link + "." + t[0], t[1], t[2]] if isinstance(t, (list, tuple)) and len(t) == 3
                    else t for t in self._term(target, cond.item)]
        if isinstance(cond, Native):
            payload = cond.payload
            if isinstance(payload, _SearchFilter):
                payload = payload.domain_at(self._today()) or []
            payload = list(payload or [])
            if not payload:
                return [_TRUE]
            return _all_of(payload) if len(_domain_terms(payload)) > 1 else payload
        return [self._clause(entity, cond)]

    def _clause(self, entity: str, cond: Compare) -> list:
        field = {NAME: "display_name", ID: "id"}.get(cond.field, cond.field)
        info = self._field_info(entity, field)
        kind = info.get("type")
        op, value = cond.op, cond.value
        if op == "set":
            return [field, "!=", False]
        if op == "unset":
            return [field, "=", False]
        if op in ("contains", "not_contains"):
            return [field, "ilike" if op == "contains" else "not ilike", value]
        if op == "starts":
            return [field, "=ilike", str(value).replace("%", "") + "%"]
        value = self._stored(info, value)
        if op == "under":
            return [field, "child_of", [int(v) for v in value]]
        if kind in ("many2one", "many2many", "one2many") or field == "id":
            if op in ("eq", "ne"):
                return [field, "=" if op == "eq" else "!=", int(value)]
            if op in ("in", "not_in"):
                return [field, "in" if op == "in" else "not in", [int(v) for v in value]]
        if op in ("in", "not_in"):
            return [field, "in" if op == "in" else "not in", list(value)]
        if kind in ("char", "text", "html") and op in ("eq", "ne"):
            return [field, "=ilike" if op == "eq" else "not ilike", value]
        return [field, {"eq": "=", "ne": "!=", "lt": "<", "le": "<=", "gt": ">",
                        "ge": ">="}[op], value]

    def _stored(self, info: dict, value):
        """A value as Odoo stores it: datetimes in UTC text, dates in ISO text."""
        if isinstance(value, tuple):
            return tuple(self._stored(info, v) for v in value)
        if info.get("type") == "datetime" and isinstance(value, date):
            moment = value if isinstance(value, datetime) else datetime.combine(value, time_of_day())
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=self._zone())
            return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(value, date):
            return value.isoformat()
        return value

    def _zone(self):
        zone = self.time_zone()
        try:
            return ZoneInfo(zone) if zone else timezone.utc
        except ZoneInfoNotFoundError:
            return timezone.utc

    def _field_info(self, entity: str, path: str) -> dict:
        model, info = entity, {}
        for part in path.split("."):
            info = self._meta(model).fields.get(part, {})
            model = str(info.get("relation") or model)
        return info

    # -- helpers ------------------------------------------------------------------------------
    def _meta(self, model: str) -> _ModelMeta:
        if model not in self._models:
            self._models[model] = self._model_meta(model)
        return self._models[model]

    def _data_call(self, model: str, method: str, args: list, kwargs: dict):
        try:
            return self._call(model, method, args, kwargs)
        except BusinessToolError as exc:
            raise DataRefused(str(exc)) from exc

    @staticmethod
    def _read_fields(meta: _ModelMeta, fields) -> list[str]:
        out = [f for f in fields if f in meta.fields]
        for f in tuple(out):
            if meta.fields[f].get("type") == "monetary":
                currency = meta.fields[f].get("currency_field") or "currency_id"
                if currency in meta.fields:
                    out.append(currency)
        return list(dict.fromkeys(out))

    def _row(self, meta: _ModelMeta, row: dict, fields) -> Row:
        values, shown, units = {}, {}, {}
        for field in fields:
            info = meta.fields.get(field)
            if info is None:
                continue
            raw = row.get(field)
            if info.get("type") == "many2one" and isinstance(raw, (list, tuple)) and len(raw) > 1:
                values[field] = LinkValue(str(raw[0]), str(raw[1]))
            elif raw is False and info.get("type") != "boolean":
                values[field] = None
            else:
                values[field] = raw
            shown[field] = self._display(raw, info, row)
            if info.get("type") == "monetary":
                currency = row.get(info.get("currency_field") or "currency_id")
                units[field] = (f"currency:{currency[0]}" if isinstance(currency, (list, tuple))
                                and len(currency) > 1 else "unknown-currency")
        return Row(id=str(row.get("id", "")), display=str(row.get("display_name") or ""),
                   values=values, shown=shown, units=units)


def _native(payload, label: str, domain) -> Cond:
    """A domain as core conditions: one `Native` per top-level clause, each naming the
    field it constrains, so that the core can replace the clauses on one field."""
    if isinstance(payload, _SearchFilter) or not isinstance(domain, list) or not domain:
        fields = tuple(dict.fromkeys(str(t[0]) for t in (domain or ())
                                     if isinstance(t, (list, tuple)) and len(t) == 3))
        return Native(payload, label, fields)
    terms = _domain_terms(list(domain))
    if len(terms) == 1 or any(len(t) != 1 for t in terms):
        fields = tuple(dict.fromkeys(str(t[0]) for t in domain if isinstance(t, (list, tuple)) and len(t) == 3))
        return Native(list(domain), label, fields)
    return all_of(*(Native([t[0]], label, (str(t[0][0]),)) for t in terms))


#: The catalog's operation names in the port's terms.
_OPERATIONS = {"unlink": "delete"}

__all__ = ["_OdooData"]
