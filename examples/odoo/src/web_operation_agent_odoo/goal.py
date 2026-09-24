"""Odoo's side of a goal-directed turn: the index and the resolver.

Everything here is read-only JSON-RPC under the delegated user: the runtime descriptor
catalog, ``load_menus``, ``get_views``, ``fields_get``, ``name_search``, ``read``,
``search_count``, ``read_group`` and ``search_read``. Nothing here dispatches a page
command or a write; the core does the former and nothing does the latter.
"""
from __future__ import annotations

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
from web_operation_agent.goal.spec import Deliverable, Filter, GoalSpec, Metric
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
_MAX_FIELDS = 16
_RECORD_LIMIT = 20
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
#: Line fields that are bookkeeping for the form, not values a person asks about.
_SKIP_LINE_FIELDS = frozenset({"sequence", "display_type", "is_downpayment", "product_updatable",
                               "product_uom_category_id", "product_uom_readonly",
                               "technical_price_unit", "qty_delivered_method",
                               "company_currency_id", "tax_calculation_rounding_method",
                               "analytic_distribution", "partner_id", "product_type"})
_MAX_LINE_FIELDS = 14

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


class Ambiguous(tuple):
    """Several records match a reference; the tuple holds their display names."""


class _SearchFilter:
    __slots__ = ("name", "label", "domain", "group_by", "raw")

    def __init__(self, name: str, label: str, domain: list | None, group_by: str,
                 raw: str = ""):
        self.name, self.label, self.domain, self.group_by = name, label, domain, group_by
        self.raw = raw

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
        # A label that contains the words asked for ("# Product Variants" for "Product
        # Variants") beats one the words merely contain ("Product"); among each, the
        # closest length wins.
        containing = sorted(((len(_norm(m.get("string", ""))), n) for n, m in self.fields.items()
                             if wanted in _norm(m.get("string", ""))))
        if containing:
            return containing[0][1]
        contained = sorted(((-len(_norm(m.get("string", ""))), n) for n, m in self.fields.items()
                            if _norm(m.get("string", "")) and _norm(m.get("string", "")) in wanted))
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
            filters, groups, same_as = [], [], ""
            if action.get("breadcrumbs"):
                self._page_filters[action["action_id"]] = self._search_filters(
                    action["model"], action["action_id"])
                for item in self._page_filters[action["action_id"]]:
                    (groups if item.group_by else filters).append(item.label)
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
                aliases=self._aliases.get(action["name"], ()), filters_as=same_as))
        entities = []
        for model in entity_models:
            meta = self._models[model]
            entities.append(IndexEntity(
                key=model, label=meta.label, aliases=self._aliases.get(meta.label, ()),
                fields=tuple(self._index_fields(meta)) if model in detailed else (),
                sections=meta.sections if model in detailed else (),
                actions=meta.actions if model in detailed else ()))
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
            "attributes": ["string", "type", "relation", "selection", "searchable"]})
        if not detailed:
            return _ModelMeta(model, label, fields, [], [], (), (), [])
        try:
            views = self._client.call(model, "get_views", [[[False, "list"], [False, "form"],
                                                             [False, "search"]]], {})
        except BusinessToolError:
            views = {"views": {}}
        arch = {kind: (views.get("views", {}).get(kind) or {}).get("arch", "")
                for kind in ("list", "form", "search")}
        list_fields = list(dict.fromkeys(re.findall(r'<field name="([^"]+)"', arch["list"])))
        form_fields = list(dict.fromkeys(re.findall(r'<field name="([^"]+)"', arch["form"])))
        sections = tuple(dict.fromkeys(
            html.unescape(s) for s in re.findall(r'<page[^>]*?\sstring="([^"]+)"', arch["form"])))
        actions = tuple(dict.fromkeys(
            html.unescape(s) for s in re.findall(
                r'<button[^>]*?\btype="object"[^>]*?\bstring="([^"]+)"', arch["form"])
            + re.findall(r'<button[^>]*?\bstring="([^"]+)"[^>]*?\btype="object"', arch["form"])))
        return _ModelMeta(model, label, fields, list_fields, form_fields, sections, actions,
                          _parse_filters(arch["search"]), form_arch=arch["form"])

    def _line_metas(self, meta: _ModelMeta) -> list[_ModelMeta]:
        """The line entities a record's form embeds, with the fields that form shows."""
        out = []
        for name, info in meta.fields.items():
            comodel = str(info.get("relation") or "")
            if (info.get("type") != "one2many" or comodel not in self._model_labels
                    or comodel in self._models or f'<field name="{name}"' not in meta.form_arch):
                continue
            # The embedded list's own fields are self-closing, so the first closing tag
            # after the x2many field ends its block.
            block = re.search(rf'<field name="{name}"[^>]*>(.*?)</field>', meta.form_arch, re.S)
            if block is None:
                continue
            inner = [f for f in dict.fromkeys(re.findall(r'<field name="([^"]+)"', block.group(1)))
                     if f not in _SKIP_LINE_FIELDS]
            if not inner:
                continue
            fields = self._client.call(comodel, "fields_get", [], {
                "attributes": ["string", "type", "relation", "selection", "searchable"]})
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
        for name in meta.list_fields + meta.form_fields:
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
        resolved = []
        for deliverable in spec.deliverables:
            try:
                resolved.append(self._resolve_one(deliverable, index))
            except BusinessToolError as exc:
                resolved.append(Resolved(kind=deliverable.kind,
                                         problem=f"the application refused a read: {exc}"[:200]))
        return Resolution(deliverables=tuple(resolved), reads=tuple(self._reads))

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
        if d.kind == "page" and not d.record and d.page.key:
            return self._list_page(d, index)
        if d.kind == "list" and d.record and d.via:
            # "The lines of S00004", declared on the line entity with the parent named as
            # the record and the link field as via: a list with that link as condition.
            return self._aggregate(Deliverable(
                kind="list", entity=d.entity, fields=d.fields, sort=d.sort,
                descending=d.descending, limit=d.limit, page=d.page,
                metric=Metric(op="count", filters=(Filter(field=d.via, values=(d.record,)),)
                              + (d.metric.filters if d.metric else ()))), index)
        if d.kind in ("aggregate", "list"):
            return self._aggregate(d, index)
        entity = index.entity(d.entity) if d.entity else None
        if d.kind == "page" and entity is None and d.page.key:
            return self._list_page(d, index)
        if entity is None:
            return Resolved(kind=d.kind, problem="the entity named is not in the index")
        if not d.record and d.kind == "handoff":
            # "Create a new X": the control lives on the entity's list page -- the widest
            # one, whose action restricts nothing (Quotations rather than Sales Orders),
            # since a new record starts in the state the narrower pages exclude.
            candidates = [a for a in self._actions if a["model"] == entity.key]
            action = next((a for a in candidates if a.get("breadcrumbs")
                           and not _literal_domain(str(a.get("domain") or ""))),
                          candidates[0] if candidates else None)
            page = index.page(f"p{action['action_id']}") if action else None
            if action is None or page is None:
                return Resolved(kind="handoff", problem="no page shows this entity")
            return Resolved(kind="handoff", page=PagePlan(
                url=self._action_url(action), label=page.breadcrumb,
                expected_path=self._action_url(action), highlight=d.action or "New"))
        if not d.record:
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
        found = self._find_record(entity.key, d.record)
        via = d.via
        if found is None and d.kind == "handoff":
            # A record that does not exist and a change asked for: the name is the record
            # to be created, and the control is the list page's own "New".
            return self._resolve_one(Deliverable(kind="handoff", entity=d.entity, action="New"),
                                     index)
        if found is None:
            # The reference may belong to a record that links to this entity ("S00006" is
            # an order; the person asked about its customer). Re-home the reference when
            # exactly one such entity holds it, and follow the link.
            rehomed = self._rehome(entity.key, d.record, index)
            if rehomed is None:
                return Resolved(kind=d.kind, problem=f"no {entity.label} matches {d.record!r}")
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
            plan = self._record_page(model, rid, name, section="", highlight=control)
            return Resolved(kind="handoff", page=plan)
        plan = self._record_page(model, rid, name, section=d.section, highlight="")
        return Resolved(kind="page", page=plan)

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
            return Resolved(kind="fact",
                            problem="none of the fields asked for exist on this record")
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

    def _record_page(self, model: str, rid: int, name: str, *, section: str,
                     highlight: str) -> PagePlan:
        meta = self._models.get(model)
        candidates = [a for a in self._actions
                      if a["model"] == model and "form" in (a.get("view_modes") or ())]
        candidates.sort(key=lambda a: (str(a.get("route") or "").startswith("action-"),
                                       not a.get("breadcrumbs")))
        chosen = None
        for action in candidates:
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
        if highlight and meta is not None:
            wanted = _norm(highlight)
            for item in meta.actions:
                if _norm(item) == wanted:
                    control = item
                    break
        label = name if not tab else f"{name} · {tab}"
        return PagePlan(url=url, label=label, expected_path=url, section=tab,
                        highlight=control)

    def _list_page(self, d: Deliverable, index: ApplicationIndex) -> Resolved:
        page = index.page(d.page.key) if d.page.key else None
        if page is None and d.entity:
            page = self._default_page(d.entity, index)
        if page is None:
            return Resolved(kind="page", problem="the page named is not in the index")
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
            facets_on=tuple(dict.fromkeys(on)), facets_off=tuple(dict.fromkeys(off))))

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
    def _aggregate(self, d: Deliverable, index: ApplicationIndex) -> Resolved:
        page = index.page(d.page.key) if d.page.key else None
        entity = index.entity(d.entity) if d.entity else None
        if page is None and entity is None:
            return Resolved(kind=d.kind, problem="no entity or page names this scope")
        # A declared page scopes the numbers by its own domain and default filters; an
        # entity alone means every record of it the person may read. The page a list
        # opens afterwards is chosen separately.
        model = page.entity if page is not None else entity.key
        meta = self._models.get(model) or self._model_meta(model)
        self._models.setdefault(model, meta)
        today = self._today()
        action = (next((a for a in self._actions if f"p{a['action_id']}" == page.key), None)
                  if page is not None else None)
        domain = list(_literal_domain(str(action.get("domain") or "")) or []) if action else []
        described = [page.label if page is not None else meta.label]
        # The page's own default filters apply too, except ones about the current user.
        context = _literal_dict(str(action.get("context") or "")) if action else {}
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
                resolved = next(([[field, "=", name]] for field, info in meta.fields.items()
                                 if info.get("type") == "selection"
                                 and any(k == name for k, _l in info.get("selection") or ())),
                                None)
            if resolved and "uid" not in str(resolved):
                domain += resolved
        metric = d.metric or Metric(op="count")
        page_filters = self._page_filters.get(action["action_id"], meta.filters) if action \
            else meta.filters
        for name in metric.page_filters:
            wanted = _norm(name)
            item = next((f for f in page_filters if _norm(f.label) == wanted), None) \
                or meta.filter_by_name(name)
            resolved = item.domain_at(today) if item is not None else None
            if resolved is None or "uid" in str(resolved):
                return Resolved(kind=d.kind, problem=f"filter {name!r} is not usable here")
            domain += resolved
            described.append(item.label)
        for condition in metric.filters:
            exact_field = any(_norm(i.get("string", "")) == _norm(condition.field)
                              for i in meta.fields.values())
            if not exact_field:
                # Not a field by its exact label: perhaps the page's own filter, named by
                # its label or under a generic word ("filter": "To pay").
                named = [f for f in page_filters
                         if any(_norm(f.label) == _norm(v) for v in condition.values)]
                if named and named[0].domain_at(today) is not None:
                    domain += named[0].domain_at(today)
                    described.append(named[0].label)
                    continue
            clause, text = self._condition(meta, condition.field, condition.values,
                                           op=condition.op, today=today)
            if clause is None:
                return Resolved(kind=d.kind, problem=text)
            domain += clause
            described.append(text)
        if d.kind == "list" and d.page.search:
            clause = [["display_name", "ilike", d.page.search]]
            if not self._call(model, "search_count", [domain + clause], {}):
                # No name contains the words; they may name a linked record instead
                # ("United States" on customers is their country). One link field with
                # exactly one such record decides; more than one is left alone.
                hits = []
                for field, info in meta.fields.items():
                    if info.get("type") != "many2one" or field not in meta.list_fields + meta.form_fields:
                        continue
                    found = self._find_record(str(info["relation"]), d.page.search)
                    # Only a record actually named by the words counts ("United States"
                    # is a country; it is not the currency whose long name contains it).
                    if (isinstance(found, tuple) and not isinstance(found, Ambiguous)
                            and _norm(_strip_code(found[2])) == _norm(d.page.search)):
                        hits.append((field, found))
                if len(hits) == 1:
                    field, (_m, rid, _n) = hits[0]
                    clause = [[field, "child_of" if meta.fields[field].get("relation") == "res.partner" else "in", [rid]]]
                    described.append(f"{meta.fields[field].get('string') or field} = {d.page.search}")
                else:
                    described.append(f"~{d.page.search}")
            else:
                described.append(f"~{d.page.search}")
            domain += clause
        subject = " · ".join(described)
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
            plan = self._list_page(Deliverable(kind="page", entity=model, page=d.page), index) \
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
                if found is None or isinstance(found, Ambiguous):
                    return None, f"no {info.get('string')} matches {value!r}"
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
            try:
                numbers = [float(v.replace(",", "")) for v in values]
            except ValueError:
                return None, f"{values[0]!r} is not a number"
            if op == "between" and len(numbers) >= 2:
                return [[field, ">=", numbers[0]], [field, "<=", numbers[1]]], text
            return [[field, op if op in ("<", "<=", ">", ">=", "!=") else "=", numbers[0]]], text
        operator = {"=": "=ilike", "!=": "not ilike"}.get(op, "ilike")
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
                                     domain, group_by, html.unescape(attrs.get("domain", ""))))
    return filters


def _kind_of(info: dict) -> str | None:
    kind = info.get("type")
    return {"many2one": "link", "monetary": "money", "float": "number", "integer": "number",
            "selection": "choice", "date": "date", "datetime": "date", "html": "html",
            "char": "text", "text": "text", "boolean": "bool", "many2many": None,
            "one2many": None}.get(kind, None)


__all__ = ["ALIASES", "OdooGoalPort"]
