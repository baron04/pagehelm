"""The Odoo goal index: entities, fields, pages, filters, sections, controls and scopes
compiled from runtime metadata, read-only, under the delegated user.
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

from .text import (_FIELD_TAG, _KIND_RANK, _MAX_FIELDS, _MAX_LINE_FIELDS, _ModelMeta, _SKIP_FIELDS, _SKIP_FIELD_PREFIXES, _SKIP_LINE_FIELDS, _SearchFilter, _TECHNICAL_MODULES, _kind_of, _literal_dict, _norm, _parse_filters)


class _IndexCompiler:
    """Compiles the application index from the descriptor catalog, per-action search
    views and ``fields_get`` under the delegated user. Mixed into ``OdooGoalPort``."""

    # -- index ----------------------------------------------------------------------
    def index(self, *, context: OperationContext | None = None) -> ApplicationIndex:
        with self._lock:
            if self._index is not None:
                return self._index
            self._index = self._compile()
            return self._index

    def _compile(self) -> ApplicationIndex:
        catalog = self._client.call("woa.runtime.descriptor", "catalog", [], {})
        self._model_labels = {m["model"]: m.get("label") or m["model"]
                              for m in catalog.get("models", [])}
        model_operations = {m["model"]: frozenset(m.get("operations") or ())
                            for m in catalog.get("models", [])}
        transient = {m["model"] for m in catalog.get("models", []) if m.get("transient")}
        actions = []
        for action in catalog.get("actions", []):
            module = str(action.get("xml_id") or "").split(".", 1)[0]
            text = f"{action.get('domain') or ''} {action.get('context') or ''}"
            if (not action.get("readable") or action.get("model") in transient
                    or module in _TECHNICAL_MODULES or "active_id" in text
                    or not action.get("view_modes")):
                continue
            actions.append(action)
        # Menu-reachable pages first, then the ones the application gave a path of their
        # own (its primary pages), then the rest; each group in action order. A page with
        # no menu that repeats a menu page's name and entity is the same page reached
        # another way, and only confuses the choice.
        actions.sort(key=lambda a: (not a.get("breadcrumbs"),
                                    str(a.get("route") or "").startswith("action-"),
                                    a.get("action_id", 0)))
        menu_names = {(_norm(a["name"]), a["model"]) for a in actions if a.get("breadcrumbs")}
        actions = [a for a in actions if a.get("breadcrumbs")
                   or (_norm(a["name"]), a["model"]) not in menu_names]
        self._actions = actions
        pages, entity_models = [], []
        for action in actions:
            model = action["model"]
            if model not in entity_models:
                entity_models.append(model)
        detailed = {a["model"] for a in actions if a.get("breadcrumbs")}
        for model in entity_models:
            self._models[model] = self._model_meta(model, detailed=model in detailed)
        for model in list(entity_models):
            if model not in detailed:
                continue
            for line in self._line_metas(self._models[model]):
                # A line model that also has a page of its own (journal items) keeps
                # its place in the list but takes the line description: the fields the
                # owner's form shows and the owner it belongs to.
                self._models[line.model] = line
                detailed.add(line.model)
                if line.model not in entity_models:
                    entity_models.insert(entity_models.index(model) + 1, line.model)
        rendered_filters: dict[tuple, str] = {}
        for action in actions:
            meta = self._models[action["model"]]
            breadcrumb = " / ".join(action.get("breadcrumbs") or ()) or (
                action["name"] if _norm(action["name"]) != _norm(meta.label)
                else f"{action['name']} ({meta.label})")
            filters, groups, same_as, scope = [], [], "", []
            if action.get("breadcrumbs"):
                self._page_filters[action["action_id"]] = self._search_filters(
                    action["model"], action["action_id"])
                for item in self._page_filters[action["action_id"]]:
                    (groups if item.group_by else filters).append(item.label)
                # The filters the page switches on by itself, except personal ones,
                # are the scope it shows: the model must know the page is narrower
                # than its entity.
                context = _literal_dict(str(action.get("context") or ""))
                for key, value in context.items():
                    if str(key).startswith("search_default_") and value:
                        item = next((f for f in self._page_filters[action["action_id"]]
                                     if f.name == key[len("search_default_"):]), None)
                        if item is not None and not item.group_by \
                                and "uid" not in str(item.raw):
                            scope.append(item.label)
                # Pages of one entity usually share a search view; the index names its
                # filters once and lets the other pages point at that page.
                signature = (action["model"], tuple(filters), tuple(groups))
                if signature in rendered_filters:
                    same_as, filters, groups = rendered_filters[signature], [], []
                elif filters or groups:
                    rendered_filters[signature] = f"p{action['action_id']}"
            pages.append(IndexPage(
                key=f"p{action['action_id']}", label=action["name"], breadcrumb=breadcrumb,
                entity=action["model"], views=tuple(action.get("view_modes") or ()),
                filters=tuple(dict.fromkeys(filters)), groups=tuple(dict.fromkeys(groups)),
                aliases=self._aliases.get(action["name"], ()), filters_as=same_as,
                scope=tuple(dict.fromkeys(scope)),
                operations=(("create",) if "create" in model_operations.get(action["model"], ())
                            and "form" in (action.get("view_modes") or ()) else ())))
        entities = []
        for model in entity_models:
            meta = self._models[model]
            owner = self._models.get(meta.parent) if meta.parent else None
            entities.append(IndexEntity(
                key=model, label=meta.label, aliases=self._aliases.get(meta.label, ()),
                fields=tuple(self._index_fields(meta)) if model in detailed else (),
                sections=meta.sections if model in detailed else (),
                actions=meta.actions if model in detailed else (),
                lines_of=owner.label if owner is not None else ""))
        return ApplicationIndex(entities=tuple(entities), pages=tuple(pages),
                                source_digest=str(catalog.get("descriptor_digest") or ""))

    def _search_filters(self, model: str, action_id: int) -> list[_SearchFilter]:
        """The filters and groups of the search view this page actually uses."""
        try:
            views = self._client.call(model, "get_views", [[[False, "search"]]],
                                      {"options": {"action_id": action_id}})
        except BusinessToolError:
            return []
        arch = (views.get("views", {}).get("search") or {}).get("arch", "")
        return _parse_filters(arch)

    def _model_meta(self, model: str, *, detailed: bool = True) -> _ModelMeta:
        label = self._model_labels.get(model, model)
        fields = self._client.call(model, "fields_get", [], {
            "attributes": ["string", "type", "relation", "selection", "searchable", "domain"]})
        if not detailed:
            return _ModelMeta(model, label, fields, [], [], (), (), [])
        try:
            views = self._client.call(model, "get_views", [[[False, "list"], [False, "form"],
                                                             [False, "search"]]], {})
        except BusinessToolError:
            views = {"views": {}}
        arch = {kind: (views.get("views", {}).get(kind) or {}).get("arch", "")
                for kind in ("list", "form", "search")}
        list_fields = list(dict.fromkeys(_FIELD_TAG.findall(arch["list"])))
        form_fields = list(dict.fromkeys(_FIELD_TAG.findall(arch["form"])))
        sections = tuple(dict.fromkeys(
            html.unescape(s) for s in re.findall(r'<page[^>]*?\sstring="([^"]+)"', arch["form"])))
        # Buttons that call a method and buttons that open an action (a wizard such as
        # "Create Invoice") are both controls the person can be pointed at.
        actions = tuple(dict.fromkeys(
            html.unescape(s) for s in re.findall(
                r'<button[^>]*?\btype="(?:object|action)"[^>]*?\bstring="([^"]+)"', arch["form"])
            + re.findall(r'<button[^>]*?\bstring="([^"]+)"[^>]*?\btype="(?:object|action)"', arch["form"])))
        return _ModelMeta(model, label, fields, list_fields, form_fields, sections, actions,
                          _parse_filters(arch["search"]), form_arch=arch["form"])

    def _line_metas(self, meta: _ModelMeta) -> list[_ModelMeta]:
        """The line entities a record's form embeds, with the fields that form shows."""
        out = []
        for name, info in meta.fields.items():
            comodel = str(info.get("relation") or "")
            already = self._models.get(comodel)
            if (info.get("type") != "one2many" or comodel not in self._model_labels
                    or (already is not None and already.parent)
                    or not re.search(rf'<field\b[^>]*?\bname="{name}"', meta.form_arch)):
                # A model already described as some record's lines is not described
                # again; one that only has a page of its own (journal items) still gets
                # its line description from the owner's form.
                continue
            fields = self._client.call(comodel, "fields_get", [], {
                "attributes": ["string", "type", "relation", "selection", "searchable", "domain"]})
            # The line fields the form shows are the ones named in it that belong to the
            # line model and not to the record (an embedded list's tags are not nested in
            # a way a regex can bound reliably).
            inner = [f for f in dict.fromkeys(_FIELD_TAG.findall(meta.form_arch))
                     if f in fields and f not in meta.fields and f not in _SKIP_LINE_FIELDS]
            if not inner:
                continue
            parent_link = next((f for f, i in fields.items()
                                if i.get("type") == "many2one" and i.get("relation") == meta.model),
                               None)
            shown = ([parent_link] if parent_link else []) + [f for f in inner if f in fields]
            try:
                views = self._client.call(comodel, "get_views", [[[False, "search"]]], {})
                filters = _parse_filters((views.get("views", {}).get("search") or {}).get("arch", ""))
            except BusinessToolError:
                filters = []
            out.append(_ModelMeta(comodel, self._model_labels.get(comodel, comodel), fields,
                                  shown[:_MAX_LINE_FIELDS + 1], [], (), (), filters,
                                  parent=meta.model))
        return out

    def _index_fields(self, meta: _ModelMeta) -> list[IndexField]:
        out: list[IndexField] = []
        seen: set[str] = set()
        shown = list(dict.fromkeys(meta.list_fields + meta.form_fields))
        ordered = sorted(shown, key=lambda n: (_KIND_RANK.get(_kind_of(meta.fields.get(n, {})) or "", 9),
                                               shown.index(n)))
        for name in ordered:
            if len(out) >= _MAX_FIELDS:
                break
            info = meta.fields.get(name)
            if (info is None or name in seen or name in _SKIP_FIELDS
                    or name.startswith(_SKIP_FIELD_PREFIXES)):
                continue
            kind = _kind_of(info)
            if kind is None:
                continue
            seen.add(name)
            relation = ""
            if kind == "link":
                relation = str(info.get("relation") or "")
            choices = ()
            if kind == "choice":
                choices = tuple(str(label) for _key, label in (info.get("selection") or ()))
            out.append(IndexField(key=name, label=str(info.get("string") or name), kind=kind,
                                  relation=relation, choices=choices))
        return out
