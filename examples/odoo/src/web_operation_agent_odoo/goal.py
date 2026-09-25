"""Odoo's side of a goal-directed turn: the index and the resolver.

Everything here is read-only JSON-RPC under the delegated user: the runtime descriptor
catalog, ``load_menus``, ``get_views``, ``fields_get``, ``name_search``, ``read``,
``search_count``, ``read_group`` and ``search_read``. Nothing here dispatches a page
command or a write; the core does the former and nothing does the latter.
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

#: Modules whose actions are the framework's own, not the business application's.
_TECHNICAL_MODULES = frozenset({
    "base", "web", "web_tour", "web_editor", "mail", "bus", "iap", "auth_signup",
    "auth_totp", "digest", "http_routing", "phone_validation", "onboarding",
    "resource", "utm", "html_editor", "spreadsheet", "spreadsheet_dashboard",
    "base_setup", "base_import", "web_unsplash", "google_gmail", "microsoft_outlook",
    "sms", "snailmail", "portal", "website", "woa_runtime", "payment"})
#: Field names that are chatter, activity or bookkeeping rather than business values.
_SKIP_FIELD_PREFIXES = ("message_", "activity_", "website_message", "rating_",
                        "has_message", "my_activity", "access_", "x_studio")
_SKIP_FIELDS = frozenset({"company_id", "currency_id", "id", "__last_update", "sequence",
                          "color", "display_name", "write_date", "create_date",
                          "create_uid", "write_uid", "country_code", "locked",
                          "authorized_transaction_ids", "tax_country_id"})
_MAX_FIELDS = 20
_FIELD_TAG = re.compile(r'<field\b[^>]*?\bname="([^"]+)"')
#: The order in which a person asks about fields: who and what first, amounts and dates
#: next, free text last. The index shows the first fields of an entity, so this decides
#: what a small model can name.
_KIND_RANK = {"link": 0, "choice": 1, "money": 2, "number": 3, "date": 4, "text": 5,
              "html": 6, "bool": 7}
_RECORD_LIMIT = 40
_GROUP_LIMIT = 20

#: Chinese aliases for the labels this index is expected to carry. Odoo's own zh_CN pack
#: is not installed on every deployment; these are the words the reference tasks use.
ALIASES = {
    "Quotations": ("报价单",), "Sales Orders": ("销售订单", "订单"),
    "Invoices": ("客户发票", "发票"), "Bills": ("供应商账单", "账单"),
    "Credit Notes": ("贷项通知单", "退款单"), "Refunds": ("供应商退款",),
    "Customer Payments": ("客户付款",), "Vendor Payments": ("供应商付款",),
    "Customers": ("客户",), "Vendors": ("供应商",), "Products": ("产品",),
    "Product Variants": ("产品变体", "变体"), "Product Tags": ("产品标签",),
    "Tags": ("标签",), "Units of Measure Categories": ("计量单位分类", "计量单位的分类"),
    "Units of Measure": ("计量单位",), "Sales Analysis": ("销售分析",),
    "Invoice Analysis": ("发票分析",), "Payment Terms": ("付款条件", "付款条款"),
    "Product Categories": ("产品分类", "产品类别"), "Sales Teams": ("销售团队",),
    "Contact": ("联系人", "客户", "供应商"), "Sales Order": ("销售订单", "报价单"),
    "Journal Entry": ("发票", "账单", "凭证"), "Product": ("产品",),
    "Product Variant": ("产品变体",), "Payments": ("付款",),
    "Sales Order Line": ("订单行", "订单明细", "报价单明细"),
    "Journal Item": ("发票行", "账单明细", "凭证行"),
}
#: Values that ask whether a field is set at all, in the index's language and in Chinese.
_EMPTY = frozenset({"empty", "notset", "none", "unset", "空", "为空", "没有"})
_NOT_EMPTY = frozenset({"notempty", "set", "any", "nonempty", "非空", "不为空", "有"})
#: Line fields that are bookkeeping for the form, not values a person asks about.
_SKIP_LINE_FIELDS = frozenset({"sequence", "display_type", "is_downpayment", "product_updatable",
                               "product_uom_category_id", "product_uom_readonly",
                               "technical_price_unit", "qty_delivered_method",
                               "company_currency_id", "tax_calculation_rounding_method",
                               "analytic_distribution", "partner_id", "product_type"})
_MAX_LINE_FIELDS = 24

#: Relative periods a date condition may name, in the index's language and in Chinese.
_PERIODS = {
    "today": "today", "今天": "today", "yesterday": "yesterday", "昨天": "yesterday",
    "this week": "this_week", "本周": "this_week", "last week": "last_week", "上周": "last_week",
    "this month": "this_month", "本月": "this_month", "这个月": "this_month",
    "last month": "last_month", "上月": "last_month", "上个月": "last_month",
    "this quarter": "this_quarter", "本季度": "this_quarter",
    "last quarter": "last_quarter", "上季度": "last_quarter", "上个季度": "last_quarter",
    "this year": "this_year", "今年": "this_year", "last year": "last_year", "去年": "last_year",
    "overdue": "overdue", "逾期": "overdue", "过期": "overdue",
}
_LAST_DAYS = re.compile(r"^(?:last|past|最近|过去)\s*(\d{1,3})\s*(?:days|天)$", re.I)
_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR = re.compile(r"^(\d{4})$")
_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*(?:\.\.|~|to|至|到)\s*(\d{4}-\d{2}-\d{2})$")



def _fields_of(index: ApplicationIndex, model: str) -> str:
    entity = index.entity(model) if model else None
    if entity is None:
        return ""
    return ", ".join(f.label for f in entity.fields[:20])


def _describes_record(d: Deliverable) -> bool:
    """The deliverable carries conditions that can pick a record out without its name."""
    return bool((d.metric is not None and d.metric.filters) or d.page.filters_on
                or d.page.search)


def _literal_domain(text: str) -> list | None:
    """A domain expressed as a Python literal, or None when it needs a context."""
    if not text or not text.strip():
        return []
    try:
        value = ast.literal_eval(text.strip())
    except (ValueError, SyntaxError):
        return None
    if not isinstance(value, list):
        return None
    return value


def _literal_dict(text: str) -> dict:
    try:
        value = ast.literal_eval(text.strip()) if text and text.strip() else {}
    except (ValueError, SyntaxError):
        return {}
    return value if isinstance(value, dict) else {}


def _norm(text: str) -> str:
    return "".join(ch for ch in html.unescape(text or "").casefold() if not ch.isspace())


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-z\u3400-\u9fff]+", (text or "").casefold()) if t}


def _strip_code(name: str) -> str:
    return re.sub(r"^\[[^\]]*\]\s*", "", name or "").strip()


def _plain_text(markup: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", markup or "")
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(html.unescape(text).split())


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.2f} {currency}".strip()


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    return date(day.year + month // 12, month % 12 + 1, 1)


def _period(value: str, today: date) -> tuple[date | None, date | None] | None:
    """A named or written period as [start, end) dates; None when it is not one."""
    text = " ".join(value.strip().lower().split())
    name = _PERIODS.get(text)
    if name == "today":
        return today, today + timedelta(days=1)
    if name == "yesterday":
        return today - timedelta(days=1), today
    if name == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    if name == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=7)
    if name == "this_month":
        return _month_start(today), _add_months(today, 1)
    if name == "last_month":
        return _add_months(today, -1), _month_start(today)
    if name in ("this_quarter", "last_quarter"):
        quarter_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        if name == "last_quarter":
            quarter_start = _add_months(quarter_start, -3)
        return quarter_start, _add_months(quarter_start, 3)
    if name == "this_year":
        return date(today.year, 1, 1), date(today.year + 1, 1, 1)
    if name == "last_year":
        return date(today.year - 1, 1, 1), date(today.year, 1, 1)
    if name == "overdue":
        return None, today
    match = _LAST_DAYS.match(text)
    if match:
        days = int(match.group(1))
        return today - timedelta(days=days), today + timedelta(days=1)
    match = _RANGE.match(text)
    if match:
        start, end = date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
        return start, end + timedelta(days=1)
    match = _DAY.match(text)
    if match:
        start = date.fromisoformat(text)
        return start, start + timedelta(days=1)
    match = _MONTH.match(text)
    if match:
        start = date(int(match.group(1)), int(match.group(2)), 1)
        return start, _add_months(start, 1)
    match = _YEAR.match(text)
    if match:
        return date(int(text), 1, 1), date(int(text) + 1, 1, 1)
    return None


_TODAY_CALLS = re.compile(r"(?:context_today\(\)|time)\.strftime\('([^']+)'\)")


def _domain_at(text: str, today: date) -> list | None:
    """A search-view domain, with the application's "today" written in for this turn."""
    if not text or not text.strip():
        return []
    literal = _literal_domain(text)
    if literal is not None:
        return literal
    substituted = _TODAY_CALLS.sub(lambda m: repr(today.strftime(m.group(1))), text)
    return _literal_domain(substituted)


class _Scope:
    """What a declaration's page, filters and conditions select, as a domain."""

    def __init__(self, *, model, meta, page, action, domain, described, default_clauses):
        self.model, self.meta, self.page, self.action = model, meta, page, action
        self.domain, self.described, self.default_clauses = domain, described, default_clauses


class Ambiguous(tuple):
    """Several records match a reference; the tuple holds their display names."""


class _SearchFilter:
    __slots__ = ("name", "label", "domain", "group_by", "raw", "date_field")

    def __init__(self, name: str, label: str, domain: list | None, group_by: str,
                 raw: str = "", date_field: str = ""):
        self.name, self.label, self.domain, self.group_by = name, label, domain, group_by
        self.raw = raw
        #: For a date filter ("Invoice Date", "Accounting Date"): the field it offers
        #: periods of; it narrows nothing by itself.
        self.date_field = date_field

    def domain_at(self, today: date) -> list | None:
        return self.domain if self.domain is not None else _domain_at(self.raw, today)


class _ModelMeta:
    """Per-model metadata the index and the resolver share."""

    def __init__(self, model: str, label: str, fields: dict, list_fields: list[str],
                 form_fields: list[str], sections: tuple[str, ...],
                 actions: tuple[str, ...], filters: list[_SearchFilter],
                 form_arch: str = "", parent: str = ""):
        self.model, self.label, self.fields = model, label, fields
        self.list_fields, self.form_fields = list_fields, form_fields
        self.sections, self.actions, self.filters = sections, actions, filters
        self.form_arch = form_arch
        #: For a line entity: the model whose record owns the lines.
        self.parent = parent

    def field_by_label(self, label: str) -> str | None:
        wanted = _norm(label)
        if not wanted:
            return None
        for name, meta in self.fields.items():
            if _norm(meta.get("string", "")) == wanted or name == label:
                return name
        asked = _tokens(label)
        if not asked:
            return None
        # Whole words, never substrings: "ID" is not inside "Validity". A field whose
        # own name says exactly the words asked for (validity_date for "Validity Date",
        # invoice_date for "Invoice Date") is meant even when its label differs.
        named = [n for n in self.fields if _tokens(n.replace("_", " ")) == asked]
        if len(named) == 1:
            return named[0]
        # A label whose words include every word asked for ("Invoice/Bill Date" for
        # "Invoice Date", "# Product Variants" for "Product Variants"); the fewest extra
        # words, then the shortest, wins.
        covering = sorted(((len(_tokens(m.get("string", "")) - asked), len(_norm(m.get("string", ""))), n)
                           for n, m in self.fields.items() if asked <= _tokens(m.get("string", ""))))
        if covering:
            return covering[0][2]
        # Or the words asked for begin with a label and qualify it ("Customer name",
        # "Total amount"); a trailing word alone ("Date" in "Validity Date") names nothing.
        first = next(iter(re.findall(r"[0-9a-z\u3400-\u9fff]+", label.casefold())), "")
        contained = sorted(((-len(_norm(m.get("string", ""))), n) for n, m in self.fields.items()
                            if first in _tokens(m.get("string", ""))
                            and _tokens(m.get("string", "")) <= asked))
        return contained[0][1] if contained else None

    def link_by_label(self, label: str) -> str | None:
        """The link (many2one) field a label names, or the one its words contain.

        A label like "Invoice Partner Display Name" names a text field; the link the
        person means is the one whose label those words contain ("Partner").
        """
        field = self.field_by_label(label)
        if field is not None and self.fields[field].get("type") == "many2one":
            return field
        wanted = _norm(label)
        links = sorted(((-len(_norm(m.get("string", ""))), n) for n, m in self.fields.items()
                        if m.get("type") == "many2one" and _norm(m.get("string", ""))
                        and (_norm(m.get("string", "")) in wanted
                             or wanted in _norm(m.get("string", "")))))
        return links[0][1] if links else None

    def filter_by_name(self, label: str) -> _SearchFilter | None:
        wanted = _norm(label)
        for item in self.filters:
            if _norm(item.label) == wanted or item.name == label:
                return item
        return None


class OdooGoalPort:
    """Index compilation and goal resolution over the delegated JSON-RPC client."""

    def __init__(self, client, *, aliases: dict | None = None):
        self._client = client
        self._aliases = ALIASES if aliases is None else aliases
        self._lock = threading.Lock()
        self._index: ApplicationIndex | None = None
        self._actions: list[dict] = []
        self._models: dict[str, _ModelMeta] = {}
        self._page_filters: dict[int, list[_SearchFilter]] = {}
        self._model_labels: dict[str, str] = {}
        self._reads: list[tuple[str, str]] = []
        #: A one2many's static domain the next aggregate applies to its line scope.
        self._line_scope: list | None = None
        #: The one record a list in this turn found, for a later deliverable that names
        #: none ("the order still to invoice: I want to invoice it now").
        self._found_one: tuple | None = None

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
                self._models[line.model] = line
                detailed.add(line.model)
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
                scope=tuple(dict.fromkeys(scope))))
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
            if (info.get("type") != "one2many" or comodel not in self._model_labels
                    or comodel in self._models
                    or not re.search(rf'<field\b[^>]*?\bname="{name}"', meta.form_arch)):
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

    # -- resolution -----------------------------------------------------------------
    def resolve(self, spec: GoalSpec, *, context: OperationContext | None = None,
                current_path: str = "") -> Resolution:
        index = self.index(context=context)
        self._reads = []
        self._found_one = None
        resolved = []
        for deliverable in spec.deliverables:
            try:
                resolved.append(self._resolve_one(deliverable, index))
            except BusinessToolError as exc:
                resolved.append(Resolved(kind=deliverable.kind,
                                         problem=f"the application refused a read: {exc}"[:200]))
        return Resolution(deliverables=tuple(resolved), reads=tuple(self._reads))

    def today(self) -> str:
        """The current date where the application's users are, for the declaration."""
        return self._today().isoformat()

    def _today(self) -> date:
        zone = ""
        time_zone = getattr(self._client, "time_zone", None)
        if callable(time_zone):
            try:
                zone = str(time_zone() or "")
            except BusinessToolError:
                zone = ""
        try:
            return datetime.now(ZoneInfo(zone)).date() if zone else date.today()
        except ZoneInfoNotFoundError:
            return date.today()

    def _utc_boundary(self, day: date) -> str:
        zone = ""
        time_zone = getattr(self._client, "time_zone", None)
        if callable(time_zone):
            try:
                zone = str(time_zone() or "")
            except BusinessToolError:
                zone = ""
        try:
            local = datetime.combine(day, time_of_day(), ZoneInfo(zone)) if zone else \
                datetime.combine(day, time_of_day(), timezone.utc)
        except ZoneInfoNotFoundError:
            local = datetime.combine(day, time_of_day(), timezone.utc)
        return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        self._reads.append((method, model))
        return self._client.call(model, method, args, kwargs or {})

    def _resolve_one(self, d: Deliverable, index: ApplicationIndex) -> Resolved:
        d = self._line_record_from_conditions(d)
        if isinstance(d, Resolved):
            return d
        if d.kind == "page" and not d.record and d.page.key and not (d.metric and d.metric.filters):
            return self._list_page(d, index)
        if d.record and d.entity in self._models and self._parent_link(d.entity) is not None \
                and self._embedded_lines(self._parent_link(d.entity)[0], d.entity) is not None:
            lines = self._lines_of_named_record(d, index)
            if lines is not None:
                return lines
        if d.kind == "list" and d.record and d.via:
            # "The lines of S00004", declared on the line entity with the parent named as
            # the record and the link field as via: a list with that link as condition.
            # A via that is not a field of the line entity means its parent link.
            line_meta = self._models.get(d.entity) if d.entity else None
            via = d.via
            parent = self._parent_link(d.entity) if line_meta is not None else None
            if line_meta is not None and line_meta.field_by_label(via) is None and parent is not None:
                via = str(line_meta.fields[parent[1]].get("string") or parent[1])
            if parent is not None:
                embedded = self._embedded_lines(parent[0], d.entity)
                if embedded is not None and embedded[1]:
                    self._line_scope = list(embedded[1])
            return self._aggregate(Deliverable(
                kind="list", entity=d.entity, fields=d.fields, sort=d.sort,
                descending=d.descending, limit=d.limit, page=d.page,
                metric=Metric(op="count", filters=(Filter(field=via, values=(d.record,)),)
                              + (d.metric.filters if d.metric else ()))), index)
        entity = index.entity(d.entity) if d.entity else None
        described = _describes_record(d)
        if (d.kind in ("fact", "page", "handoff") and entity is not None and not d.record
                and not described and self._found_one is not None
                and self._found_one[0] == entity.key and _norm(d.action) not in ("new", "create")):
            d = replace(d, record=self._found_one[2])
        if d.kind == "list" and entity is not None and (d.record or described):
            # "The order of 22 August, its products and subtotals": a list of the lines
            # of one record, the record named or described, the line field among the
            # columns asked for.
            line_field = next((f for f, i in self._models[entity.key].fields.items()
                               if i.get("type") == "one2many" and i.get("relation") in self._models
                               and self._embedded_lines(entity.key, str(i["relation"])) is not None
                               and any(_norm(i.get("string", "")) == _norm(label)
                                       for label in d.fields)), None) \
                if entity.key in self._models else None
            if line_field is not None:
                owner = self._find_record(entity.key, d.record) if d.record else None
                if not (isinstance(owner, tuple) and not isinstance(owner, Ambiguous)):
                    owner = self._locate(d, index) if described else Resolved(
                        kind=d.kind, problem=f"no {entity.label} matches {d.record!r}")
                if isinstance(owner, Resolved):
                    return owner
                return self._lines_list(entity.key, line_field, owner, d, index)
        if d.kind in ("aggregate", "list"):
            return self._aggregate(d, index)
        if d.kind == "page" and entity is None and d.page.key:
            return self._list_page(d, index)
        if entity is None:
            return Resolved(kind=d.kind, problem="the entity named is not in the index",
                            repair=("entity must be one of the index's entity keys: "
                                    + ", ".join(e.key for e in index.entities[:30])))
        if not d.record and d.kind == "handoff" and not described:
            return self._creation(d, entity, index)
        if not d.record and not described:
            if d.kind == "page":
                page = self._default_page(entity.key, index)
                return self._list_page(Deliverable(kind="page", entity=d.entity,
                                                   page=type(d.page)(key=page.key if page else "",
                                                                     view=d.page.view,
                                                                     filters_on=d.page.filters_on,
                                                                     filters_off=d.page.filters_off,
                                                                     group_by=d.page.group_by)),
                                       index) if page else Resolved(
                    kind=d.kind, problem="no page shows this entity")
            return Resolved(kind=d.kind, problem="no record was named")
        found = self._find_record(entity.key, d.record) if d.record else None
        via = d.via
        if described and not (isinstance(found, tuple) and not isinstance(found, Ambiguous)):
            # Not a name but a description ("the quotation sent to Joel Willis", "the
            # 31,750 credit note"): the conditions pick the record out, or say why not.
            found = self._locate(d, index)
            if isinstance(found, Resolved):
                return found
        if d.kind == "handoff" and _norm(d.action) in ("", "new", "create") and (
                found is None or isinstance(found, Ambiguous)):
            # A change asked for and the name is no existing record: it is the record to
            # be created (or the customer it is for), and the control is the list page's
            # own "New". A change to a record that was named but not found is refused,
            # never turned into a creation.
            return self._creation(d, entity, index)
        if found is None:
            # The reference may belong to a record that links to this entity ("S00006" is
            # an order; the person asked about its customer). Re-home the reference when
            # exactly one such entity holds it, and follow the link.
            rehomed = self._rehome(entity.key, d.record, index)
            if rehomed is None:
                return Resolved(kind=d.kind, problem=f"no {entity.label} matches {d.record!r}",
                                repair=(f"no {entity.label} is named {d.record!r}; if the person "
                                        "described the record rather than naming it, leave "
                                        "record empty and put the description in "
                                        "metric.filters (customer, amount, status, date)"))
            found, via = rehomed
            if d.via:
                via = d.via
        if isinstance(found, Ambiguous):
            return Resolved(kind=d.kind, problem=f"{d.record!r} is ambiguous",
                            candidates=tuple(found))
        model, rid, name = found
        if via:
            meta = self._models.get(model) or self._model_meta(model)
            link = meta.link_by_label(via)
            info = meta.fields.get(link or "", {})
            if link is None:
                lines = meta.field_by_label(via)
                line_info = meta.fields.get(lines or "", {})
                if line_info.get("type") == "one2many" and line_info.get("relation") in self._models:
                    # "The lines of S00004": a list of the line entity linked to the record.
                    line_meta = self._models[line_info["relation"]]
                    parent = next((f for f, i in line_meta.fields.items()
                                   if i.get("type") == "many2one" and i.get("relation") == model),
                                  None)
                    if parent is not None:
                        parent_label = str(line_meta.fields[parent].get("string") or parent)
                        return self._aggregate(Deliverable(
                            kind="list", entity=line_info["relation"], fields=d.fields,
                            sort=d.sort, descending=d.descending, limit=d.limit,
                            metric=Metric(op="count", filters=(Filter(field=parent_label,
                                                                       values=(name,)),))), index)
                return Resolved(kind=d.kind, problem=f"{via!r} is not a link field")
            rows = self._call(model, "read", [[rid]], {"fields": [link]})
            target = rows[0].get(link) if rows else None
            if not target:
                return Resolved(kind=d.kind,
                                problem=f"{name} has no {info.get('string', via)}")
            model, rid, name = str(info["relation"]), int(target[0]), str(target[1])
            if model not in self._models:
                self._models[model] = self._model_meta(model)
        if d.kind == "fact":
            return self._facts(model, rid, name, d.fields)
        if d.kind == "handoff":
            meta = self._models.get(model) or self._model_meta(model)
            control = d.action
            if d.fields and not any(_norm(a) == _norm(d.action) for a in meta.actions):
                # The change is to a field, and the model named the field alongside a
                # button it guessed: the field is what the person will edit.
                control = d.fields[0]
            plan = self._record_page(model, rid, name, section="", highlight=control,
                                     value=d.value)
            repair = ""
            if d.value and not plan.fill:
                field = meta.field_by_label(control) if meta is not None else None
                info = meta.fields.get(field or "", {}) if meta is not None else {}
                if info.get("type") == "many2one":
                    repair = (f"no {info.get('string') or control} is named {d.value!r}; "
                              "value must be the exact name of an existing record")
            return Resolved(kind="handoff", page=plan, repair=repair)
        plan = self._record_page(model, rid, name, section=d.section, highlight="")
        if d.section and model in self._models:
            # "Its invoice lines": the section that embeds the record's lines is shown,
            # and the lines are read out too, since a request for a record's lines
            # is usually a question about them.
            line_field = next((f for f, i in self._models[model].fields.items()
                               if i.get("type") == "one2many" and i.get("relation") in self._models
                               and _norm(i.get("string", "")) == _norm(d.section)
                               and self._embedded_lines(model, str(i["relation"])) is not None),
                              None)
            if line_field is not None:
                lines = self._lines_list(model, line_field, (model, rid, name), d, index)
                if lines.aggregate is not None:
                    return Resolved(kind="page", page=plan, aggregate=lines.aggregate)
        return Resolved(kind="page", page=plan)

    def _creation(self, d: Deliverable, entity, index: ApplicationIndex) -> Resolved:
        """"Create a new X": the control is "New" on the entity's list page -- the one
        declared when it shows this entity (Bills rather than Invoices), else the widest
        one, whose action restricts nothing (Quotations rather than Sales Orders), since
        a new record starts in the state the narrower pages exclude."""
        candidates = [a for a in self._actions if a["model"] == entity.key]
        action = next((a for a in candidates if f"p{a['action_id']}" == d.page.key
                       and a.get("breadcrumbs")), None)
        if action is None:
            action = next((a for a in candidates if a.get("breadcrumbs")
                           and not _literal_domain(str(a.get("domain") or ""))),
                          candidates[0] if candidates else None)
        page = index.page(f"p{action['action_id']}") if action else None
        if action is None or page is None:
            return Resolved(kind="handoff", problem="no page shows this entity")
        return Resolved(kind="handoff", page=PagePlan(
            url=self._action_url(action), label=page.breadcrumb,
            expected_path=self._action_url(action), highlight=d.action or "New"))

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

    # -- records ----------------------------------------------------------------------
    def _rehome(self, model: str, reference: str, index: ApplicationIndex):
        hits = []
        for entity in index.entities:
            meta = self._models.get(entity.key)
            if entity.key == model or meta is None or not meta.list_fields:
                continue
            links = [name for name, info in meta.fields.items()
                     if info.get("type") == "many2one" and info.get("relation") == model
                     and name in meta.list_fields + meta.form_fields]
            if not links:
                continue
            found = self._find_record(entity.key, reference)
            if isinstance(found, tuple) and not isinstance(found, Ambiguous):
                hits.append((found, str(meta.fields[links[0]].get("string") or links[0])))
        return hits[0] if len(hits) == 1 else None

    def _find_record(self, model: str, reference: str):
        rows = self._call(model, "name_search", [reference], {"limit": 8})
        if not rows:
            return None
        wanted = _norm(reference)
        exact = [(rid, name) for rid, name in rows
                 if _norm(_strip_code(name)) == wanted or _norm(name) == wanted]
        if len(exact) == 1 or (exact and all(n == exact[0][1] for _r, n in exact)):
            rid, name = exact[0]
            return model, int(rid), str(name)
        if len(rows) == 1:
            rid, name = rows[0]
            return model, int(rid), str(name)
        prefix = [(rid, name) for rid, name in rows
                  if _norm(_strip_code(name)).startswith(wanted)]
        if len(prefix) == 1:
            rid, name = prefix[0]
            return model, int(rid), str(name)
        # Contacts of a company carry the company's name first: the company is meant.
        companies = [(rid, name) for rid, name in rows if "," not in str(name)]
        if len(companies) == 1:
            rid, name = companies[0]
            return model, int(rid), str(name)
        return Ambiguous(str(name) for _rid, name in rows[:6])

    def _facts(self, model: str, rid: int, name: str, labels: tuple[str, ...]) -> Resolved:
        meta = self._models.get(model) or self._model_meta(model)
        self._models.setdefault(model, meta)
        fields, missing = [], []
        for label in labels:
            field = meta.field_by_label(label)
            if field is None:
                missing.append(label)
            elif field not in fields:
                fields.append(field)
        if not fields:
            lines = self._line_facts(model, rid, name, missing)
            if lines is not None:
                return lines
            return Resolved(kind="fact",
                            problem="none of the fields asked for exist on this record",
                            repair=(f"{meta.label} has no field named "
                                    + ", ".join(repr(m) for m in missing)
                                    + "; its fields are: " + (_fields_of(self.index(), model)
                                                              or ", ".join(
                                        str(i.get("string") or f)
                                        for f, i in list(meta.fields.items())[:30]))))
        read_fields = list(fields)
        if any(meta.fields[f].get("type") == "monetary" for f in fields) \
                and "currency_id" in meta.fields:
            read_fields.append("currency_id")
        rows = self._call(model, "read", [[rid]], {"fields": read_fields})
        row = rows[0] if rows else {}
        facts = []
        for field in fields:
            info = meta.fields[field]
            facts.append(Fact(subject=name, label=str(info.get("string") or field),
                              value=self._display(row.get(field), info, row)))
        problem = ""
        if missing:
            problem = "not on this record: " + ", ".join(missing)
        return Resolved(kind="fact", facts=tuple(facts), problem=problem)

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

    def _display(self, value, info: dict, row: dict) -> str:
        kind = info.get("type")
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

    # -- pages ------------------------------------------------------------------------
    def _action_url(self, action: dict, rid: int | None = None) -> str:
        route = str(action.get("route") or "")
        base = f"/odoo/{route}" if route and not route.startswith("action-") else (
            f"/odoo/action-{action['action_id']}")
        return f"{base}/{rid}" if rid is not None else base

    def _default_page(self, model: str, index: ApplicationIndex) -> IndexPage | None:
        for action in self._actions:
            if action["model"] == model:
                return index.page(f"p{action['action_id']}")
        return None

    def _fill_for(self, meta: _ModelMeta, field: str, value: str) -> tuple[str, str, str]:
        """(what to put in, how, its visible label) for a value the person gave a field,
        or empty when the value cannot be typed with certainty: a date at day precision,
        a number, a choice the field offers, the exact name of one existing record, or
        plain text. A checkbox and a value that names no record are left to the person."""
        info = meta.fields.get(field, {})
        kind = info.get("type")
        value = value.strip()
        if not value:
            return "", "", ""
        if kind in ("date", "datetime"):
            period = _period(value, self._today())
            if period is None or period[0] is None or period[1] is None \
                    or (period[1] - period[0]).days != 1:
                return "", "", ""
            return period[0].isoformat(), "text", period[0].isoformat()
        if kind == "selection":
            wanted = _norm(value)
            for key, label in info.get("selection") or ():
                if _norm(str(label)) == wanted or key == value:
                    return str(key), "choice", str(label)
            return "", "", ""
        if kind == "many2one":
            found = self._find_record(str(info["relation"]), value)
            if not (isinstance(found, tuple) and not isinstance(found, Ambiguous)):
                return "", "", ""
            return found[2], "link", found[2]
        if kind in ("integer", "float", "monetary"):
            try:
                float(value.replace(",", ""))
            except ValueError:
                return "", "", ""
            return value.replace(",", ""), "text", value
        if kind in ("char", "text", "html"):
            return value, "text", value
        return "", "", ""

    def _record_page(self, model: str, rid: int, name: str, *, section: str,
                     highlight: str, value: str = "") -> PagePlan:
        meta = self._models.get(model)
        candidates = [a for a in self._actions
                      if a["model"] == model and "form" in (a.get("view_modes") or ())]
        candidates.sort(key=lambda a: (str(a.get("route") or "").startswith("action-"),
                                       not a.get("breadcrumbs")))
        chosen = None
        for action in candidates:
            if not action.get("breadcrumbs"):
                # Without a menu page the record's own route is the address people see.
                continue
            domain = _literal_domain(str(action.get("domain") or ""))
            if domain is None:
                continue
            if domain:
                if not self._call(model, "search_count", [[["id", "=", rid]] + domain], {}):
                    continue
            chosen = action
            break
        url = self._action_url(chosen, rid) if chosen else f"/odoo/{model}/{rid}"
        tab = ""
        if section and meta is not None:
            wanted = _norm(section)
            for item in meta.sections:
                if _norm(item) == wanted or wanted in _norm(item):
                    tab = item
                    break
            tab = tab or section
        control = highlight
        fill = ("", "", "")
        fill_field = ""
        if highlight and meta is not None:
            wanted = _norm(highlight)
            for item in meta.actions:
                if _norm(item) == wanted:
                    control = item
                    break
            else:
                # Not a button: the field the person wants to change, named loosely
                # ("Expiration Date" for Expiration, "Invoice Date" for Invoice/Bill Date).
                asked = _tokens(highlight)
                field = next((n for n, i in meta.fields.items()
                              if _norm(str(i.get("string") or "")) == wanted), None)
                if field is None:
                    field = next((n for n, i in meta.fields.items()
                                  if n in meta.form_fields and asked
                                  and asked <= _tokens(str(i.get("string") or ""))), None)
                if field is None:
                    field = meta.field_by_label(highlight)
                if field is not None and field in meta.form_fields:
                    control = str(meta.fields[field].get("string") or field)
                    if not tab:
                        # A field inside a tab is reached by selecting that tab first.
                        tab = _tab_of(meta.form_arch, field)
                    if value:
                        fill = self._fill_for(meta, field, value)
                        fill_field = field if fill[0] else ""
        label = name if not tab else f"{name} · {tab}"
        return PagePlan(url=url, label=label, expected_path=url, section=tab,
                        highlight=control, fill=fill[0], fill_kind=fill[1], fill_label=fill[2],
                        fill_field=fill_field)

    def _list_page(self, d: Deliverable, index: ApplicationIndex) -> Resolved:
        page = index.page(d.page.key) if d.page.key else None
        if page is None and d.entity:
            page = self._default_page(d.entity, index)
        if page is None:
            return Resolved(kind="page", problem="the page named is not in the index")
        if d.page.search and d.kind == "page":
            # "Show me Large Desk's card": the words name one record of the page's entity.
            # With a section asked for, a record is certainly meant, and the one record
            # the words pick out is it ("Azure" for Azure Interior).
            found = self._find_record(page.entity, d.page.search)
            if (isinstance(found, tuple) and not isinstance(found, Ambiguous)
                    and (d.section or _norm(_strip_code(found[2])) == _norm(d.page.search))):
                if page.entity not in self._models:
                    self._models[page.entity] = self._model_meta(page.entity)
                return Resolved(kind="page", page=self._record_page(
                    found[0], found[1], found[2], section=d.section, highlight=""))
            if found is None and not self._call(page.entity, "search_count",
                                                [[["display_name", "ilike", d.page.search]]], {}):
                # Nothing on this page is named so: opening the list would show the
                # person something else and say the words were found.
                return Resolved(kind="page", problem=f"no {page.label} record matches {d.page.search!r}")
        action = next((a for a in self._actions if f"p{a['action_id']}" == page.key), None)
        if action is None:
            return Resolved(kind="page", problem="the page named is not in the index")
        url = self._action_url(action)
        view = d.page.view
        if view and view not in page.views:
            view = ""
        # A facet the index does not list may still exist on the live page (the page's
        # own search view); it is passed through by name and settled by the runtime.
        on = [self._facet_name(page, name) or name for name in d.page.filters_on]
        if d.page.group_by:
            on.append(self._facet_name(page, d.page.group_by, group=True) or d.page.group_by)
        off = [self._facet_name(page, name) or name for name in d.page.filters_off]
        problem = ""
        if d.page.view and not view:
            problem = f"this page has no {d.page.view} view"
        expected = url
        if view:
            url = f"{url}?view_type={view}"
        return Resolved(kind="page", problem=problem, page=PagePlan(
            url=url, label=page.breadcrumb, expected_path=expected, view=view,
            facets_on=tuple(dict.fromkeys(on)), facets_off=tuple(dict.fromkeys(off)),
            missing_view=d.page.view if problem else ""))

    @staticmethod
    def _facet_name(page: IndexPage, label: str, *, group: bool = False) -> str | None:
        wanted = _norm(label)
        pool = page.groups if group else page.filters
        for item in pool:
            if _norm(item) == wanted:
                return item
        for item in pool:
            if wanted and (wanted in _norm(item) or _norm(item) in wanted):
                return item
        if not group:
            for item in page.groups:
                if _norm(item) == wanted:
                    return item
        return None

    # -- aggregates -------------------------------------------------------------------
    def _scope(self, d: Deliverable, index: ApplicationIndex, *, search: bool):
        """The records a deliverable's page, filters and conditions describe.

        Returns a `_Scope`, or a `Resolved` problem when a name in the declaration has no
        meaning here. The page scopes by its own domain and default filters (except ones
        about the current user); an entity alone means every record of it the person may
        read. Page filters switched on narrow, ones switched off exclude; conditions are
        field comparisons; search words, when asked for, are what the search box would do.
        """
        page = index.page(d.page.key) if d.page.key else None
        entity = index.entity(d.entity) if d.entity else None
        if page is None and entity is None:
            return Resolved(kind=d.kind, problem="no entity or page names this scope")
        prefix = ""
        if page is not None and entity is not None and page.entity != entity.key:
            parent = self._parent_link(entity.key)
            if parent is not None and parent[0] == page.entity:
                # The lines of the records a page shows (the product lines of vendor
                # refunds): the page's scope reaches the lines through their link.
                prefix = parent[1] + "."
            else:
                # The lines of an order declared with the orders page: the entity named
                # is the one asked about; the page was the way there, not the scope.
                page = None
        model = page.entity if page is not None and not prefix else entity.key
        meta = self._models.get(model) or self._model_meta(model)
        self._models.setdefault(model, meta)
        today = self._today()
        action = (next((a for a in self._actions if f"p{a['action_id']}" == page.key), None)
                  if page is not None else None)

        def through(clauses):
            return [[prefix + c[0], c[1], c[2]] if prefix and isinstance(c, (list, tuple))
                    and len(c) == 3 else c for c in clauses]

        domain = through(_literal_domain(str(action.get("domain") or "")) or []) if action else []
        if prefix and not self._line_scope:
            embedded = self._embedded_lines(page.entity, entity.key)
            if embedded is not None and embedded[1]:
                self._line_scope = list(embedded[1])
        if self._line_scope:
            # The lines as the record's form embeds them (product lines, not tax lines).
            domain += self._line_scope
            self._line_scope = None
        described = [page.label if page is not None else meta.label]
        context = _literal_dict(str(action.get("context") or "")) if action else {}
        default_clauses: list = []
        for key, value in context.items():
            if not (str(key).startswith("search_default_") and value):
                continue
            pool = self._page_filters.get(action["action_id"], meta.filters) if action \
                else meta.filters
            name = key[len("search_default_"):]
            item = next((f for f in pool if f.name == name), None)
            resolved = item.domain_at(today) if item is not None else None
            if resolved is None and item is None:
                # The action's own search view was not served to this user, but the
                # default names a choice of one of the model's selection fields, which
                # is how these filters are conventionally written ("in_invoice").
                page_meta = self._models.get(page.entity, meta) if page is not None else meta
                resolved = next(([[field, "=", name]] for field, info in page_meta.fields.items()
                                 if info.get("type") == "selection"
                                 and any(k == name for k, _l in info.get("selection") or ())),
                                None)
            if resolved and "uid" not in str(resolved):
                domain += through(resolved)
                default_clauses += through(resolved)
        metric = d.metric or Metric(op="count")
        page_filters = self._page_filters.get(action["action_id"], meta.filters) if action \
            else meta.filters
        declared_page_filters = list(metric.page_filters) + [
            f for f in d.page.filters_on if f not in metric.page_filters]
        for name in declared_page_filters:
            wanted = _norm(name)
            item = next((f for f in page_filters if _norm(f.label) == wanted), None) \
                or meta.filter_by_name(name)
            if item is not None and item.date_field and item.domain is None:
                # A date filter switched on without a period narrows nothing.
                continue
            if item is None and page is not None and not page.filters:
                # The page serves no filters, so the name is the model's own words for
                # the page's scope ("Vendor Bills" on Bills), which the page already has.
                continue
            resolved = item.domain_at(today) if item is not None else None
            if resolved is None or "uid" in str(resolved):
                return Resolved(kind=d.kind, problem=f"filter {name!r} is not usable here",
                                repair=(f"{name!r} is not a filter of this page; its filters "
                                        "are: " + ", ".join(f.label for f in page_filters[:20])
                                        + "; a condition on a field goes in metric.filters"))
            domain += through(resolved)
            described.append(item.label)
        for name in d.page.filters_off:
            # A filter switched off excludes what it would select ("Can be Sold" on,
            # "Can be Purchased" off): its clauses, negated as one expression.
            wanted = _norm(name)
            item = next((f for f in page_filters if _norm(f.label) == wanted), None) \
                or meta.filter_by_name(name)
            resolved = item.domain_at(today) if item is not None else None
            if resolved is None or "uid" in str(resolved):
                return Resolved(kind=d.kind, problem=f"filter {name!r} is not usable here")
            clauses = through([c for c in resolved if isinstance(c, (list, tuple))])
            if any(c in ("|", "!") for c in resolved) or not clauses:
                return Resolved(kind=d.kind, problem=f"filter {name!r} cannot be excluded")
            domain += ["!"] + ["&"] * (len(clauses) - 1) + clauses
            described.append(f"not {item.label}")
        for condition in metric.filters:
            if not condition.values:
                # A field named with nothing to compare against says nothing.
                continue
            exact_field = any(_norm(i.get("string", "")) == _norm(condition.field)
                              and i.get("searchable") is not False
                              for i in meta.fields.values())
            if not exact_field:
                # A date filter's name ("Accounting Date") stands for the field it offers.
                dated = next((f for f in page_filters + meta.filters
                              if f.date_field in meta.fields and _norm(f.label) == _norm(condition.field)),
                             None)
                if dated is not None:
                    condition = Filter(field=str(meta.fields[dated.date_field].get("string")
                                                 or dated.date_field),
                                       values=condition.values, op=condition.op)
                    exact_field = True
            if not exact_field:
                # Not a field by its exact label: perhaps the page's own filter, named by
                # its label or under a generic word ("filter": "To pay").
                named = [f for f in page_filters
                         if any(_norm(f.label) == _norm(v) or (len(_norm(v)) >= 4
                                and _norm(f.label).startswith(_norm(v).rstrip("y")))
                                for v in condition.values)]
                if named and named[0].domain_at(today) is not None:
                    domain += named[0].domain_at(today)
                    described.append(named[0].label)
                    continue
            clause, text = self._condition(meta, condition.field, condition.values,
                                           op=condition.op, today=today)
            if clause is None:
                if meta.field_by_label(condition.field) is None:
                    repair = (f"{condition.field!r} is not a field of {meta.label}; its fields "
                              "are: " + (_fields_of(index, model) or ", ".join(
                                  str(i.get("string") or f) for f, i in list(meta.fields.items())[:30])))
                else:
                    repair = (f"the value {' / '.join(condition.values)!r} cannot be used on "
                              f"{condition.field}: {text}; use a choice label from the index, "
                              "a number, or a date (2026-09-12, 2026-09, last month)")
                return Resolved(kind=d.kind, problem=text, repair=repair)
            domain += clause
            described.append(text)
        if search and d.page.search:
            clause, text = self._search_clause(meta, model, domain, d.page.search)
            if clause is None and default_clauses:
                # Nothing within the page's default filters (customers only): the words
                # may still name something the page's entity holds once those are lifted.
                lifted = [c for c in domain if c not in default_clauses]
                clause, text = self._search_clause(meta, model, lifted, d.page.search)
                if clause is not None:
                    domain, default_clauses = lifted, []
                    described.append("beyond the page's default filters")
            domain += clause or [["display_name", "ilike", d.page.search]]
            described.append(text)
        return _Scope(model=model, meta=meta, page=page, action=action, domain=domain,
                      described=described, default_clauses=default_clauses)

    def _locate(self, d: Deliverable, index: ApplicationIndex):
        """The one record a description picks out ("the quotation sent to Joel Willis",
        "last month's customer invoice"): exactly one record within the scope, or, when
        the page's own defaults or domain hide it, exactly one beyond them. Anything else
        is a problem the reply states, never a guess."""
        scope = self._scope(d, index, search=True)
        if isinstance(scope, Resolved):
            return scope
        meta, model = scope.meta, scope.model
        subject = " · ".join(scope.described)
        domains = [list(scope.domain)]
        if scope.default_clauses:
            domains.append([c for c in scope.domain if c not in scope.default_clauses])
        page_domain = _literal_domain(str(scope.action.get("domain") or "")) if scope.action else None
        if page_domain:
            domains.append([c for c in domains[-1] if c not in page_domain])
        rows: list = []
        kwargs: dict = {"fields": ["display_name"], "limit": 4}
        if d.sort:
            # "The higher one", "the latest": ordered, the first is the one meant.
            order_field = meta.field_by_label(d.sort)
            if order_field is None:
                return Resolved(kind=d.kind, problem=f"cannot sort by {d.sort!r}")
            kwargs = {"fields": ["display_name"], "limit": 1,
                      "order": f"{order_field} {'desc' if d.descending else 'asc'}, id"}
        for domain in domains:
            rows = self._call(model, "search_read", [domain], kwargs)
            if rows:
                break
        if not rows and d.page.search and (d.metric and d.metric.filters or d.page.filters_on):
            # The words matched nothing but the conditions may ("order dated" beside a
            # date): the conditions alone.
            return self._locate(replace(d, page=replace(d.page, search="")), index)
        if not rows:
            return Resolved(kind=d.kind, problem=f"no {meta.label} matches {subject}",
                            repair=(f"no {meta.label} matches {subject}; re-read the request: "
                                    "another page scope (invoices versus bills, quotations "
                                    "versus orders), field or value may be meant"))
        if len(rows) > 1:
            names = tuple(str(r.get("display_name") or "") for r in rows)
            return Resolved(kind=d.kind, problem=f"{subject} matches several {meta.label} records",
                            candidates=names,
                            repair=(f"{subject} matches several records ({', '.join(names)}); "
                                    "add a condition the request gives (amount, date, status, "
                                    "customer) or sort with limit 1 to pick one"))
        return model, int(rows[0]["id"]), str(rows[0].get("display_name") or "")

    # -- aggregates -------------------------------------------------------------------
    def _aggregate(self, d: Deliverable, index: ApplicationIndex) -> Resolved:
        scope = self._scope(d, index, search=d.kind == "list")
        if isinstance(scope, Resolved):
            return scope
        model, meta, page, action = scope.model, scope.meta, scope.page, scope.action
        domain, described, default_clauses = scope.domain, scope.described, scope.default_clauses
        metric = d.metric or Metric(op="count")
        subject = " · ".join(described)
        if d.kind == "list" and default_clauses and not self._call(model, "search_count", [domain], {}):
            # The page's own default filters (customers only) hid every match; the
            # person asked about the page's entity, so the answer says so and looks past
            # the defaults.
            for clause in default_clauses:
                domain.remove(clause)
            described.append("beyond the page's default filters")
        if d.kind == "list":
            columns = [f for f in (meta.field_by_label(label) for label in d.fields) if f]
            columns = list(dict.fromkeys(columns))
            read_fields = ["display_name"] + columns
            if any(meta.fields[f].get("type") == "monetary" for f in columns) \
                    and "currency_id" in meta.fields:
                read_fields.append("currency_id")
            limit = d.limit or _RECORD_LIMIT
            kwargs: dict = {"fields": read_fields, "limit": limit + 1}
            if d.sort:
                order_field = meta.field_by_label(d.sort)
                if order_field is None:
                    return Resolved(kind=d.kind, problem=f"cannot sort by {d.sort!r}")
                kwargs["order"] = f"{order_field} {'desc' if d.descending else 'asc'}, id"
                described.append(f"by {meta.fields[order_field].get('string', d.sort)}"
                                 + (" desc" if d.descending else ""))
            rows = self._call(model, "search_read", [domain], kwargs)
            names = []
            for row in rows[:limit]:
                cells = [f"{meta.fields[f].get('string') or f}: {self._display(row.get(f), meta.fields[f], row)}"
                         for f in columns]
                names.append(str(row.get("display_name") or "")
                             + (" — " + "; ".join(cells) if cells else ""))
            total = self._call(model, "search_count", [domain], {})
            if len(rows) == 1:
                self._found_one = (model, int(rows[0]["id"]), str(rows[0].get("display_name") or ""))
            # The list is the deliverable; the page it opens afterwards carries no facets,
            # whose failure would otherwise mark a correct answer as unfinished.
            bare = PageSpec(key=d.page.key if page is not None else "")
            plan = self._list_page(Deliverable(kind="page", entity=model, page=bare), index) \
                if (page is not None or self._default_page(model, index) is not None) else None
            page_plan = plan.page if plan is not None else None
            if not names:
                return Resolved(kind="list", page=page_plan,
                                problem=f"no {meta.label} matches "
                                        + (repr(d.page.search) if d.page.search else "these conditions"))
            return Resolved(kind="list", page=page_plan,
                            aggregate=Aggregate(subject=" · ".join(described), count=int(total),
                                                records=tuple(names),
                                                complete=len(rows) <= limit))
        count = int(self._call(model, "search_count", [domain], {}))
        widened = None
        if count == 0 and default_clauses:
            for clause in default_clauses:
                domain.remove(clause)
            count = int(self._call(model, "search_count", [domain], {}))
            if count:
                described.append("beyond the page's default filters")
        if count == 0 and action and _literal_domain(str(action.get("domain") or "")):
            # Nothing in this page's scope; the same conditions over the whole entity say
            # whether the records exist at all (quotations, when the page shows orders).
            outer = [clause for clause in domain
                     if clause not in _literal_domain(str(action.get("domain") or ""))]
            widened = int(self._call(model, "search_count", [outer], {}))
        sums: list[tuple[str, str]] = []
        if metric.op == "sum":
            field = meta.field_by_label(metric.field) if metric.field else None
            info = meta.fields.get(field or "", {})
            if field is None or info.get("type") not in ("monetary", "float", "integer"):
                return Resolved(kind=d.kind, problem=f"{metric.field!r} cannot be summed")
            groups = self._call(model, "read_group", [domain, [f"{field}:sum"], []],
                                {"lazy": False})
            total = float((groups[0].get(field) if groups else 0) or 0.0)
            currency = ""
            if info.get("type") == "monetary" and count:
                sample = self._call(model, "search_read", [domain], {
                    "fields": ["currency_id"], "limit": 1})
                value = sample[0].get("currency_id") if sample else None
                currency = value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else ""
            sums.append((str(info.get("string") or field), _money(total, currency)))
        groups_out: list[tuple[str, int]] = []
        if metric.group_by:
            field = meta.field_by_label(metric.group_by)
            if field is None:
                return Resolved(kind=d.kind, problem=f"cannot group by {metric.group_by!r}")
            spec = f"{field}:month" if meta.fields[field].get("type") in ("date", "datetime") \
                else field
            rows = self._call(model, "read_group", [domain, [field], [spec]], {
                "lazy": False, "orderby": "__count desc",
                "limit": metric.top or _GROUP_LIMIT})
            for row in rows:
                value = row.get(spec, row.get(field))
                name = (value[1] if isinstance(value, (list, tuple)) and len(value) > 1
                        else str(value))
                groups_out.append((str(name), int(row.get("__count") or 0)))
        note = ""
        if widened:
            note = f"{meta.label}, any status: {widened}"
        return Resolved(kind="aggregate", aggregate=Aggregate(
            subject=subject, count=count, sums=tuple(sums), groups=tuple(groups_out),
            note=note))

    def _search_clause(self, meta: _ModelMeta, model: str, domain: list, words: str):
        """What search words mean on this entity within `domain`, or None when nothing."""
        today = self._today()
        if _period(words, today) is not None:
            shown_dates = [f for f in dict.fromkeys(meta.list_fields + meta.form_fields)
                           if meta.fields.get(f, {}).get("type") in ("date", "datetime")]
            if shown_dates:
                field = shown_dates[0]
                clause, text = self._date_condition(field, meta.fields[field], (words,), "", today)
                if clause is not None:
                    return clause, text
        clause = [["display_name", "ilike", words]]
        if self._call(model, "search_count", [domain + clause], {}):
            return clause, f"~{words}"
        number = re.fullmatch(r"[0-9][0-9,]*(?:\.[0-9]+)?", words.strip())
        if number:
            # A bare number that names nothing is an amount (an order's total).
            field = self._amount_field(meta, words.strip(), domain)
            if field is not None:
                return ([[field, "=", float(words.replace(",", ""))]],
                        f"{meta.fields[field].get('string')} = {words}")
        # No name contains the words; they may name a linked record instead ("United
        # States" on customers is their country). Only a record actually named by the
        # words counts, and several links to the same record are one hit, on the field
        # the list shows.
        shown = list(dict.fromkeys(meta.list_fields + meta.form_fields))
        hits: dict[tuple, str] = {}
        for field, info in meta.fields.items():
            if info.get("type") != "many2one" or field not in shown:
                continue
            found = self._find_record(str(info["relation"]), words)
            if (isinstance(found, tuple) and not isinstance(found, Ambiguous)
                    and _norm(_strip_code(found[2])) == _norm(words)):
                key = (found[0], found[1])
                if key not in hits or (field in meta.list_fields and hits[key] not in meta.list_fields):
                    hits[key] = field
        if len(hits) == 1:
            (_m, rid), field = next(iter(hits.items()))
            operator = "child_of" if meta.fields[field].get("relation") == "res.partner" else "in"
            clause = [[field, operator, [rid]]]
            if self._call(model, "search_count", [domain + clause], {}):
                return clause, f"{meta.fields[field].get('string') or field} = {words}"
        # Or the words are in one text field the pages show (a city).
        texts = [f for f in shown if meta.fields.get(f, {}).get("type") == "char"
                 and f != "name" and meta.fields[f].get("searchable") is not False]
        matched = [f for f in texts
                   if self._call(model, "search_count", [domain + [[f, "ilike", words]]], {})]
        if len(matched) == 1:
            return [[matched[0], "ilike", words]], f"{meta.fields[matched[0]].get('string')} ~ {words}"
        return None, f"~{words}"

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

    def _lines_of_named_record(self, d: Deliverable, index: ApplicationIndex):
        """A line entity declared with a record reference that names the record owning
        the lines (S00004 on order lines; an invoice number even on order lines): the
        deliverable is about that record's lines."""
        found = self._find_record(d.entity, d.record)
        if isinstance(found, tuple) and not isinstance(found, Ambiguous):
            return None
        parent_link = self._parent_link(d.entity)
        line_model, owner = d.entity, None
        # Every record entity whose form embeds lines, the declared entity's owner first.
        owners: dict[str, str] = {}
        for model, meta in self._models.items():
            for info in meta.fields.values():
                relation = str(info.get("relation") or "")
                if (info.get("type") == "one2many" and relation in self._models
                        and relation != model and self._embedded_lines(model, relation) is not None):
                    owners.setdefault(model, relation)
        for model in [parent_link[0]] + [m for m in owners if m != parent_link[0]]:
            hit = self._find_record(model, d.record)
            if isinstance(hit, tuple) and not isinstance(hit, Ambiguous):
                owner = hit
                if model != parent_link[0]:
                    line_model = owners[model]
                break
        if owner is None:
            return None
        link = self._parent_link(line_model)
        if link is None:
            return None
        line_meta = self._models[line_model]
        link_label = str(line_meta.fields[link[1]].get("string") or link[1])
        embedded = self._embedded_lines(link[0], line_model)
        if embedded is not None and embedded[1]:
            self._line_scope = list(embedded[1])
        filters = (Filter(field=link_label, values=(owner[2],)),) + (d.metric.filters if d.metric else ())
        if d.metric is not None and (d.metric.op == "sum" or d.kind == "aggregate"):
            return self._aggregate(Deliverable(
                kind="aggregate", entity=line_model,
                metric=Metric(op=d.metric.op, field=d.metric.field, filters=filters,
                              group_by=d.metric.group_by, top=d.metric.top)), index)
        return self._aggregate(Deliverable(
            kind="list", entity=line_model, fields=d.fields, sort=d.sort,
            descending=d.descending, limit=d.limit, page=PageSpec(search=d.page.search),
            metric=Metric(op="count", filters=filters)), index)

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

    def _condition(self, meta: _ModelMeta, label: str, values: tuple[str, ...], *,
                   op: str = "", today: date | None = None):
        field = meta.field_by_label(label)
        if field is None:
            return None, f"no field {label!r} on {meta.label}"
        info = meta.fields[field]
        kind = info.get("type")
        text = f"{info.get('string') or field} {op or '='} {' / '.join(values)}"
        if not values:
            return None, f"no value for {label!r}"
        if info.get("searchable") is False:
            # A computed, unstored field: the application would silently ignore the
            # condition and count everything, which is worse than saying no.
            return None, f"{info.get('string') or field} cannot be used as a condition"
        emptiness = _norm(values[0]) if len(values) == 1 else ""
        if emptiness in _EMPTY or emptiness in _NOT_EMPTY:
            # "Lines with a product", "orders without a salesperson".
            # "not empty", or "not equal to empty": the field is set; "empty": it is not.
            wanted_set = emptiness in _NOT_EMPTY or op == "!="
            return ([[field, "!=" if wanted_set else "=", False]],
                    f"{info.get('string') or field} {'set' if wanted_set else 'not set'}")
        if kind in ("date", "datetime"):
            return self._date_condition(field, info, values, op, today or self._today())
        if op == "!=" and kind in ("many2one", "selection"):
            clause, text = self._condition(meta, label, values, today=today)
            if clause is None:
                return clause, text
            negated = [[c[0], "not in" if c[1] in ("in", "child_of") else "!=", c[2]]
                       if isinstance(c, list) and len(c) == 3 else c for c in clause]
            return negated, text.replace(" = ", " != ", 1)
        if kind == "many2one":
            ids = []
            for value in values:
                found = self._find_record(str(info["relation"]), value)
                if found is None or isinstance(found, Ambiguous) or op == "contains":
                    # Not one record's name: the records whose names contain the words
                    # ("Office" among products), which the search box would also find.
                    clause = ["|"] * (len(values) - 1) + [[field, "ilike", v] for v in values]
                    if found is None and not self._call(meta.model, "search_count", [clause], {}):
                        return None, f"no {info.get('string')} matches {value!r}"
                    return clause, f"{info.get('string') or field} ~ {' / '.join(values)}"
                ids.append(found[1])
            operator = "child_of" if info.get("relation") == "res.partner" else "in"
            return [[field, operator, ids]], text
        if kind == "selection":
            keys = []
            for value in values:
                wanted = _norm(value)
                match = next((k for k, lab in info.get("selection") or ()
                              if _norm(str(lab)) == wanted or k == value), None)
                if match is None:
                    match = next((k for k, lab in info.get("selection") or ()
                                  if wanted in _norm(str(lab))), None)
                if match is None:
                    return None, f"{value!r} is not a {info.get('string')} choice"
                keys.append(match)
            return [[field, "in", keys]], text
        if kind == "boolean":
            truthy = _norm(values[0]) in ("true", "yes", "1", "是")
            return [[field, "=", truthy]], text
        if kind in ("integer", "float", "monetary"):
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
                return [[field, ">=", numbers[0]], [field, "<=", numbers[1]]], text
            return [[field, op if op in ("<", "<=", ">", ">=", "!=") else "=", numbers[0]]], text
        operator = {"=": "=ilike", "!=": "not ilike"}.get(op, "ilike")
        if op == "starts with":
            # A prefix: the pattern form of "equals", anchored at the start.
            operator, values = "=ilike", tuple(v.replace("%", "") + "%" for v in values)
        if len(values) == 1:
            return [[field, operator, values[0]]], text
        return ["|"] * (len(values) - 1) + [[field, operator, v] for v in values], text

    def _date_condition(self, field: str, info: dict, values: tuple[str, ...], op: str,
                        today: date):
        label = info.get("string") or field
        is_datetime = info.get("type") == "datetime"

        def bound(day: date) -> str:
            return self._utc_boundary(day) if is_datetime else day.isoformat()

        if op == "between" and len(values) >= 2:
            first, second = _period(values[0], today), _period(values[1], today)
            if first is None or second is None or first[0] is None or second[1] is None:
                return None, f"{values[0]!r}..{values[1]!r} is not a date range"
            return ([[field, ">=", bound(first[0])], [field, "<", bound(second[1])]],
                    f"{label} between {values[0]} and {values[1]}")
        period = _period(values[0], today)
        if period is None:
            return None, f"{values[0]!r} is not a date or period"
        start, end = period
        text = f"{label} {op or 'in'} {values[0]}"
        if op in ("<", "<="):
            edge = (end if op == "<=" else start)
            if edge is None:
                edge = end
            return [[field, "<", bound(edge)]], text
        if op in (">", ">="):
            edge = (start if op == ">=" else end)
            if edge is None:
                edge = start
            return [[field, ">=", bound(edge)]], text
        clauses = []
        if start is not None:
            clauses.append([field, ">=", bound(start)])
        if end is not None:
            clauses.append([field, "<", bound(end)])
        return clauses, text


def _tab_of(form_arch: str, field: str) -> str:
    """The notebook page whose body holds the field, or empty when it is outside any."""
    for match in re.finditer(r'<page\b[^>]*?\sstring="([^"]+)"[^>]*>(.*?)</page>', form_arch or "", re.S):
        if re.search(rf'<field\b[^>]*\bname="{re.escape(field)}"', match.group(2)):
            return html.unescape(match.group(1))
    return ""


def _parse_filters(arch: str) -> list[_SearchFilter]:
    filters = []
    for tag in re.findall(r"<filter\b[^>]*>", arch or ""):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        if "string" not in attrs:
            continue
        context = _literal_dict(html.unescape(attrs.get("context", "")))
        group_by = str(context.get("group_by") or "") if context else ""
        domain = _literal_domain(html.unescape(attrs.get("domain", ""))) \
            if not group_by else []
        filters.append(_SearchFilter(attrs.get("name", ""), html.unescape(attrs["string"]),
                                     domain, group_by, html.unescape(attrs.get("domain", "")),
                                     attrs.get("date", "")))
    return filters


def _kind_of(info: dict) -> str | None:
    kind = info.get("type")
    return {"many2one": "link", "monetary": "money", "float": "number", "integer": "number",
            "selection": "choice", "date": "date", "datetime": "date", "html": "html",
            "char": "text", "text": "text", "boolean": "bool", "many2many": None,
            "one2many": None}.get(kind, None)


__all__ = ["ALIASES", "OdooGoalPort"]
