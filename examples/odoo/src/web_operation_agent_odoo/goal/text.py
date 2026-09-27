"""Words, periods, domains and per-model metadata shared by the Odoo goal index and resolver.

Nothing here talks to Odoo; these are the pure helpers and value types the compiler and
the resolver both use.
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
                        "has_message", "my_activity", "access_", "x_studio",
                        # Quick-entry inputs of a draft form (Total (Tax inc.) is what the
                        # user types to prefill lines): stored empty, never the record's
                        # own value, and not summable.
                        "quick_edit_")

_SKIP_FIELDS = frozenset({"company_id", "currency_id", "id", "__last_update", "sequence",
                          "color", "display_name", "write_date", "create_date",
                          "create_uid", "write_uid", "country_code", "locked",
                          "authorized_transaction_ids", "tax_country_id"})

#: Fields compiled per entity. The full index renders the first `_SHOWN_FIELDS` of them
#: (what one declaration against the whole index can read); a focused neighbourhood
#: renders all of them for the entities chosen.
_MAX_FIELDS = 70

_FIELD_TAG = re.compile(r'<field\b[^>]*?\bname="([^"]+)"')

#: The order in which a person asks about fields: who and what first, amounts and dates
#: next, free text last. The index shows the first fields of an entity, so this decides
#: what a small model can name.
_KIND_RANK = {"link": 0, "choice": 1, "money": 2, "number": 3, "date": 4, "text": 5,
              "html": 6, "bool": 7}

_RECORD_LIMIT = 40

_GROUP_LIMIT = 20

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
                 form_arch: str = "", parent: str = "", list_arch: str = "",
                 english=None):
        self.model, self.label, self.fields = model, label, fields
        self.list_arch = list_arch
        #: The English labels a name in the person's language translates, from the
        #: application's own translations (`OdooTranslations.back`); none by default.
        self.english = english or (lambda _text: ())
        self.list_fields, self.form_fields = list_fields, form_fields
        self.sections, self.actions, self.filters = sections, actions, filters
        self.form_arch = form_arch
        #: For a line entity: the model whose record owns the lines.
        self.parent = parent

    def field_by_label(self, label: str) -> str | None:
        found = self._field_by_label(label)
        if found is None:
            # A label in the person's language: the field the application translates so.
            for english in self.english(label):
                found = self._field_by_label(english)
                if found is not None:
                    break
        return found

    def _field_by_label(self, label: str) -> str | None:
        wanted = _norm(label)
        if not wanted:
            return None
        if any(name.startswith("quick_edit_") and _norm(meta.get("string", "")) == wanted
               for name, meta in self.fields.items()):
            # A quick-entry input is named like the record's total; the words mean the
            # record's own value, found by the looser matches below.
            return self._field_by_label_among(
                label, {n: m for n, m in self.fields.items() if not n.startswith("quick_edit_")})
        return self._field_by_label_among(label, self.fields)

    def _field_by_label_among(self, label: str, fields: dict) -> str | None:
        wanted = _norm(label)
        if not wanted:
            return None
        for name, meta in fields.items():
            if _norm(meta.get("string", "")) == wanted or name == label:
                return name
        asked = _tokens(label)
        if not asked:
            return None
        # Whole words, never substrings: "ID" is not inside "Validity". A field whose
        # own name says exactly the words asked for (validity_date for "Validity Date",
        # invoice_date for "Invoice Date") is meant even when its label differs.
        named = [n for n in fields if _tokens(n.replace("_", " ")) == asked]
        if len(named) == 1:
            return named[0]
        # A label whose words include every word asked for ("Invoice/Bill Date" for
        # "Invoice Date", "# Product Variants" for "Product Variants"); the fewest extra
        # words, then the shortest, wins.
        covering = sorted(((len(_tokens(m.get("string", "")) - asked), len(_norm(m.get("string", ""))), n)
                           for n, m in fields.items() if asked <= _tokens(m.get("string", ""))))
        if covering:
            return covering[0][2]
        # Or the words asked for begin with a label and qualify it ("Customer name",
        # "Total amount"); a trailing word alone ("Date" in "Validity Date") names nothing.
        first = next(iter(re.findall(r"[0-9a-z\u3400-\u9fff]+", label.casefold())), "")
        contained = sorted(((-len(_norm(m.get("string", ""))), n) for n, m in fields.items()
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

__all__ = ["OdooGoalPort"]
