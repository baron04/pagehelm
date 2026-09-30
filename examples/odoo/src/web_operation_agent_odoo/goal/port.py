"""Odoo's goal data: the index and the reads a goal-directed turn resolves against.

Everything here is read-only JSON-RPC under the delegated user: ``name_search``, ``read``,
``search_count``, ``read_group`` and ``search_read``. The core resolves declarations
(``web_operation_agent.goal.resolve``); this adapter only compiles the index and schema
from Odoo's metadata, translates conditions into domains, and formats values.
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from web_operation_agent.goal.index import ApplicationIndex

from ..translations import OdooTranslations
from .data import _OdooData
from .index import _IndexCompiler
from .text import (_FIELD_TAG, _SKIP_LINE_FIELDS, _ModelMeta, _SearchFilter, _literal_domain,
                   _money, _plain_text)


class OdooGoalData(_IndexCompiler, _OdooData):
    """Index compilation and read-only data access over the delegated JSON-RPC client."""

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
        #: What this actor may do on each model (create, read, write, unlink).
        self._operations: dict[str, frozenset] = {}
        self._reads: list[tuple[str, str]] = []

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
        `release`. The index is recompiled on its next use."""
        with self._lock:
            self._source_map, self._source_release = source_map, release
            self._index = None

    def capabilities(self) -> frozenset[str]:
        """Everything optional; source rules only once a compiled map is attached."""
        base = {"facets", "sections", "lines", "handoffs", "fill", "search", "values"}
        if getattr(self, "_source_map", None) is not None:
            base.add("source")
        return frozenset(base)

    # -- helpers ------------------------------------------------------------------------------
    def _today(self) -> date:
        zone = self.time_zone()
        try:
            return datetime.now(ZoneInfo(zone)).date() if zone else date.today()
        except ZoneInfoNotFoundError:
            return date.today()

    def _call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        self._reads.append((method, model))
        return self._client.call(model, method, args, kwargs or {})

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

    def _action_url(self, action: dict, rid: int | None = None) -> str:
        route = str(action.get("route") or "")
        base = f"/odoo/{route}" if route and not route.startswith("action-") else (
            f"/odoo/action-{action['action_id']}")
        return f"{base}/{rid}" if rid is not None else base

    def _parent_link(self, line_model: str) -> tuple[str, str] | None:
        """(parent model, link field) for a model some record's form embeds as lines."""
        line_meta = self._models.get(line_model)
        if line_meta is None:
            return None
        parents = [line_meta.parent] if line_meta.parent else [
            m for m in self._models
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


__all__ = ["OdooGoalData"]
