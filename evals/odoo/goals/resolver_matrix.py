"""Zero-model coverage matrix for the Odoo goal resolver.

Each case is a hand-written goal declaration (what the model would emit) and the outcome
the reference fixture must produce: values, counts, sums, groups, page addresses, tabs,
controls, or an honest problem. Nothing here calls a model; the matrix says how far the
deterministic layer reaches across entities, deliverable kinds and condition types before
a model is put in front of it.

    NO_PROXY=127.0.0.1,localhost PYTHONPATH=src:examples/odoo/src:. \\
        python -m evals.odoo.goals.resolver_matrix --credentials .private/odoo-reader

Expected values were derived from a raw dump of the fixture (orders, lines, invoices,
products, partners), not from the resolver. The fixture's clock matters for the relative
periods: "this month" cases assume the run happens in September 2026.
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
import time
from pathlib import Path

from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent_odoo.goal import OdooGoalPort
from web_operation_agent_odoo.server.executor import OdooBusinessToolExecutor
from web_operation_agent_odoo.server.identity import OdooApiKeyStore

ROOT = Path(__file__).resolve().parents[3]

# (id, entity, kind, condition, spec, expect)
# expect keys: facts {label: regex}, count, sums {label: regex}, groups [(name regex, count)],
#              note regex, records_len, records_any [regex], first_record regex,
#              page {url regex, section, highlight, view, on, off}, problem regex,
#              candidates_min.
CASES: list[tuple] = []


def case(id_, entity, kind, condition, spec, **expect):
    CASES.append((id_, entity, kind, condition, spec, expect))


def fact(entity, record, fields, via=""):
    d = {"kind": "fact", "entity": entity, "record": record, "fields": fields}
    if via:
        d["via"] = via
    return {"deliverables": [d]}


def agg(entity, page, op="count", field="", filters=(), page_filters=(), group_by="", top=0):
    return {"deliverables": [{"kind": "aggregate", "entity": entity,
                              "page": {"key": page} if page else None,
                              "metric": {"op": op, "field": field, "filters": list(filters),
                                         "page_filters": list(page_filters),
                                         "group_by": group_by, "top": top}}]}


def lst(entity, page="", search="", fields=(), filters=(), page_filters=(), sort="",
        descending=False, limit=0):
    d = {"kind": "list", "entity": entity, "fields": list(fields), "sort": sort,
         "descending": descending, "limit": limit,
         "page": {"key": page, "search": search}}
    if filters or page_filters:
        d["metric"] = {"op": "count", "filters": list(filters), "page_filters": list(page_filters)}
    return {"deliverables": [d]}


def page(key="", entity="", record="", via="", section="", view="", on=(), off=(), group_by=""):
    d = {"kind": "page", "entity": entity, "record": record, "via": via, "section": section,
         "page": {"key": key, "view": view, "filters_on": list(on), "filters_off": list(off),
                  "group_by": group_by}}
    return {"deliverables": [d]}


def handoff(entity, record, action):
    return {"deliverables": [{"kind": "handoff", "entity": entity, "record": record,
                              "action": action}], "write_intent": True}


F = lambda field, values, op="": {"field": field, "values": list(values), **({"op": op} if op else {})}  # noqa: E731

# -- facts --------------------------------------------------------------------------------
case("f-so-link", "sale.order", "fact", "none", fact("sale.order", "S00006", ["Customer"]),
     facts={"Customer": r"^Lumber Inc$"})
case("f-so-money", "sale.order", "fact", "none", fact("sale.order", "S00020", ["Total"]),
     facts={"Total": r"^2,947\.50 USD$"})
case("f-so-choice", "sale.order", "fact", "none", fact("sale.order", "S00004", ["Status"]),
     facts={"Status": r"^Sales Order$"})
case("f-so-date", "sale.order", "fact", "none", fact("sale.order", "S00009", ["Order Date"]),
     facts={"Order Date": r"^2026-09-05"})
case("f-so-user", "sale.order", "fact", "none", fact("sale.order", "S00008", ["Salesperson"]),
     facts={"Salesperson": r"^Marc Demo$"})
case("f-so-multi", "sale.order", "fact", "none",
     fact("sale.order", "S00007", ["Customer", "Total", "Invoice Status"]),
     facts={"Customer": r"Gemini", "Total": r"1,706\.00 USD", "Invoice Status": r"^To Invoice$"})
case("f-so-lines", "sale.order", "fact", "x2many", fact("sale.order", "S00004", ["Order Lines"]),
     facts={"Order Lines": r"^4 records: .*Virtual Interior Design"})
case("f-so-missing-field", "sale.order", "fact", "none", fact("sale.order", "S00006", ["Weight"]),
     problem=r"none of the fields")
case("f-so-ambiguous", "sale.order", "fact", "none", fact("sale.order", "S0001", ["Customer"]),
     problem=r"ambiguous", candidates_min=2)
case("f-so-absent", "sale.order", "fact", "none", fact("sale.order", "S99999", ["Customer"]),
     problem=r"no Sales Order matches")
case("f-am-terms", "account.move", "fact", "none",
     fact("account.move", "INV/2026/00001", ["Payment Terms"]),
     facts={"Payment Terms": r"^End of Following Month$"})
case("f-am-due", "account.move", "fact", "none", fact("account.move", "INV/2026/00004", ["Due Date"]),
     facts={"Due Date": r"^2026-08-28$"})
case("f-am-residual", "account.move", "fact", "none",
     fact("account.move", "INV/2026/00002", ["Amount Due"]),
     facts={"Amount Due": r"^41,750\.00 USD$"})
case("f-am-paystate", "account.move", "fact", "none",
     fact("account.move", "INV/2026/00003", ["Payment Status"]),
     facts={"Payment Status": r"^Not Paid$"})
case("f-am-bill-partner", "account.move", "fact", "none",
     fact("account.move", "BILL/2026/09/0001", ["Partner"]),
     facts={"Partner": r"^Azure Interior$"})
case("f-am-refund-type", "account.move", "fact", "none",
     fact("account.move", "RINV/2026/00002", ["Type"]),
     facts={"Type": r"^Customer Credit Note$"})
case("f-am-lines", "account.move", "fact", "x2many",
     fact("account.move", "INV/2026/00002", ["Invoice Lines"]),
     facts={"Invoice lines": r"^2 records: "})
case("f-partner-email", "res.partner", "fact", "none",
     fact("res.partner", "Gemini Furniture", ["Email"]),
     facts={"Email": r"^gemini_furniture@"})
case("f-partner-city-country", "res.partner", "fact", "none",
     fact("res.partner", "Lumber Inc", ["City", "Country"]),
     facts={"City": r"^Stockton$", "Country": r"^United States$"})
case("f-partner-notes-empty", "res.partner", "fact", "html",
     fact("res.partner", "Gemini Furniture", ["Notes"]), facts={"Notes": r"^\(empty\)$"})
case("f-prod-price", "product.template", "fact", "none",
     fact("product.template", "Office Chair", ["Sales Price"]), facts={"Sales Price": r"^70\.00$"})
case("f-prod-variants", "product.template", "fact", "none",
     fact("product.template", "Customizable Desk", ["Product Variants"]),
     facts={"# Product Variants": r"^5$"})
case("f-prod-category", "product.template", "fact", "none",
     fact("product.template", "Large Desk", ["Product Category"]),
     facts={"Product Category": r"Office Furniture"})
case("f-prod-type", "product.template", "fact", "none",
     fact("product.template", "Virtual Interior Design", ["Product Type"]),
     facts={"Product Type": r"^Service$"})
case("f-prod-code", "product.template", "fact", "none",
     fact("product.template", "Office Lamp", ["Internal Reference"]),
     facts={"Internal Reference": r"^FURN_8888$"})
case("f-via-customer-email", "sale.order", "fact", "via",
     fact("sale.order", "S00006", ["Email"], via="Customer"), facts={"Email": r"^lumber-inv92@"})
case("f-via-bill-vendor-city", "account.move", "fact", "via",
     fact("account.move", "BILL/2026/09/0001", ["City"], via="Partner"),
     facts={"City": r"^Fremont$"})
case("f-via-lines", "sale.order", "list", "via-lines",
     fact("sale.order", "S00004", ["Product", "Quantity"], via="Order Lines"),
     records_len=4, records_any=[r"Office Lamp.*Quantity: 10\.00"])
case("a-filter-by-name", "account.move", "aggregate", "page-filter",
     agg("account.move", "Invoicing / Customers / Invoices", filters=[F("filter", ["To pay"])]),
     count=4)
case("l-record-via-lines", "sale.order.line", "list", "via-lines",
     {"deliverables": [{"kind": "list", "entity": "sale.order.line", "record": "S00004",
                        "via": "Order Reference", "fields": ["Product", "Quantity"]}]},
     records_len=4, records_any=[r"Office Lamp.*Quantity: 10\.00"])
case("l-search-as-link-value", "res.partner", "list", "text",
     lst("res.partner", page="Invoicing / Vendors / Vendors", search="United States"),
     records_len=1, first_record=r"^Azure Interior")
case("p-menu-page-wins", "sale.order", "page", "none", page(key="Quotations"),
     page={"url": r"^/odoo/sales$"})
case("f-rehome-order-customer", "res.partner", "fact", "rehome",
     fact("res.partner", "S00006", ["Email"]), facts={"Email": r"^lumber-inv92@"})

# -- aggregates: sale.order ----------------------------------------------------------------
Q, O = "Sales / Orders / Quotations", "Sales / Orders / Orders"
case("a-so-count-quotations-page", "sale.order", "aggregate", "page", agg("sale.order", Q), count=20)
case("a-so-count-orders-page", "sale.order", "aggregate", "page", agg("sale.order", O), count=15)
case("a-so-count-link", "sale.order", "aggregate", "link",
     agg("sale.order", O, filters=[F("Customer", ["Gemini Furniture"])]), count=13)
case("a-so-count-choice", "sale.order", "aggregate", "choice",
     agg("sale.order", Q, filters=[F("Status", ["Quotation"])]), count=4)
case("a-so-count-choice-multi", "sale.order", "aggregate", "choice",
     agg("sale.order", Q, filters=[F("Status", ["Quotation", "Quotation Sent"])]), count=5)
case("a-so-sum", "sale.order", "aggregate", "page", agg("sale.order", O, op="sum", field="Total"),
     count=15, sums={"Total": r"^17,670\.50 USD$"})
case("a-so-sum-link", "sale.order", "aggregate", "link",
     agg("sale.order", O, op="sum", field="Total", filters=[F("Customer", ["Gemini Furniture"])]),
     count=13, sums={"Total": r"^13,973\.00 USD$"})
case("a-so-group-top", "sale.order", "aggregate", "group",
     agg("sale.order", O, group_by="Customer", top=1), groups=[(r"^Gemini Furniture$", 13)])
case("a-so-group-salesperson", "sale.order", "aggregate", "group",
     agg("sale.order", O, group_by="Salesperson"), groups=[(r"^Marc Demo$", 11), (r"^Mitchell Admin$", 4)])
case("a-so-group-month", "sale.order", "aggregate", "group-date",
     agg("sale.order", O, group_by="Order Date"), groups=[(r"^September 2026$", 10), (r"^August 2026$", 5)])
case("a-so-date-month", "sale.order", "aggregate", "date-abs",
     agg("sale.order", O, filters=[F("Order Date", ["2026-08"])]), count=5)
case("a-so-date-day", "sale.order", "aggregate", "date-abs",
     agg("sale.order", O, filters=[F("Order Date", ["2026-09-05"])]), count=1)
case("a-so-date-year", "sale.order", "aggregate", "date-abs",
     agg("sale.order", O, filters=[F("Order Date", ["2026"])]), count=15)
case("a-so-date-range", "sale.order", "aggregate", "date-abs",
     agg("sale.order", O, filters=[F("Order Date", ["2026-08-01..2026-08-31"])]), count=5)
case("a-so-date-between", "sale.order", "aggregate", "date-abs",
     agg("sale.order", O, filters=[F("Order Date", ["2026-08-01", "2026-08-31"], "between")]), count=5)
case("a-so-date-after", "sale.order", "aggregate", "date-abs",
     agg("sale.order", O, filters=[F("Order Date", ["2026-09-10"], ">")]), count=9)
case("a-so-date-before", "sale.order", "aggregate", "date-abs",
     agg("sale.order", O, filters=[F("Order Date", ["2026-09-01"], "<")]), count=5)
case("a-so-date-this-month", "sale.order", "aggregate", "date-rel",
     agg("sale.order", O, filters=[F("Order Date", ["this month"])]), count=10)
case("a-so-date-last-month", "sale.order", "aggregate", "date-rel",
     agg("sale.order", O, filters=[F("Order Date", ["last month"])]), count=5)
case("a-so-date-last-month-zh", "sale.order", "aggregate", "date-rel",
     agg("sale.order", O, filters=[F("Order Date", ["上个月"])]), count=5)
case("a-so-date-last-30-days", "sale.order", "aggregate", "date-rel",
     agg("sale.order", O, filters=[F("Order Date", ["last 30 days"])]), count=11)
case("a-so-date-this-year", "sale.order", "aggregate", "date-rel",
     agg("sale.order", O, filters=[F("Order Date", ["this year"])]), count=15)
case("a-so-date-this-quarter", "sale.order", "aggregate", "date-rel",
     agg("sale.order", O, filters=[F("Order Date", ["this quarter"])]), count=15)
case("a-so-date-last-quarter", "sale.order", "aggregate", "date-rel",
     agg("sale.order", O, filters=[F("Order Date", ["last quarter"])]), count=0)
case("a-so-number-gt", "sale.order", "aggregate", "number",
     agg("sale.order", O, filters=[F("Total", ["1000"], ">")]), count=7)
case("a-so-number-between", "sale.order", "aggregate", "number",
     agg("sale.order", O, filters=[F("Total", ["500", "1000"], "between")]), count=6)
case("a-so-text-contains", "sale.order", "aggregate", "text",
     agg("sale.order", O, filters=[F("Order Reference", ["S0001"], "contains")]), count=9)
case("a-so-not-link", "sale.order", "aggregate", "negation",
     agg("sale.order", O, filters=[F("Customer", ["Gemini Furniture"], "!=")]), count=2)
case("a-so-zero-widened", "sale.order", "aggregate", "link",
     agg("sale.order", O, filters=[F("Customer", ["Ready Mat"])]), count=0, note=r"any status: 2")
case("a-so-invoice-status", "sale.order", "aggregate", "choice",
     agg("sale.order", Q, filters=[F("Invoice Status", ["To Invoice"])]), count=1)
case("a-so-page-filter-personal", "sale.order", "aggregate", "page-filter",
     agg("sale.order", O, page_filters=["My Orders"]), problem=r"not usable")
case("a-so-multi-conditions", "sale.order", "aggregate", "multi",
     agg("sale.order", O, filters=[F("Customer", ["Gemini Furniture"]), F("Salesperson", ["Marc Demo"]),
                                   F("Order Date", ["2026-09"])]), count=7)
case("a-so-unstored-field", "product.template", "aggregate", "number",
     agg("product.template", "Sales / Products / Products",
         filters=[F("Product Variants", ["1"], ">")]), problem=r"cannot be used as a condition")

# -- aggregates: account.move ---------------------------------------------------------------
INV, CN, BILLS, VREF = ("Invoicing / Customers / Invoices", "Invoicing / Customers / Credit Notes",
                        "Invoicing / Vendors / Bills", "Invoicing / Vendors / Refunds")
case("a-am-count-invoices", "account.move", "aggregate", "page", agg("account.move", INV), count=4)
case("a-am-sum-total", "account.move", "aggregate", "page",
     agg("account.move", INV, op="sum", field="Total"), sums={"Total": r"^124,500\.00 USD$"})
case("a-am-page-filter-topay", "account.move", "aggregate", "page-filter",
     agg("account.move", INV, page_filters=["To pay"]), count=4)
case("a-am-page-filter-overdue", "account.move", "aggregate", "page-filter-today",
     agg("account.move", INV, page_filters=["Overdue"]), count=3)
case("a-am-page-filter-posted", "account.move", "aggregate", "page-filter",
     agg("account.move", BILLS, page_filters=["Posted"]), count=2)
case("a-am-date-invoice-month", "account.move", "aggregate", "date-abs",
     agg("account.move", INV, filters=[F("Invoice Date", ["2026-09"])]), count=3)
case("a-am-due-overdue", "account.move", "aggregate", "date-rel",
     agg("account.move", INV, filters=[F("Due Date", ["overdue"])]), count=3)
case("a-am-due-overdue-zh", "account.move", "aggregate", "date-rel",
     agg("account.move", INV, filters=[F("Due Date", ["逾期"])]), count=3)
case("a-am-due-after-today", "account.move", "aggregate", "date-rel",
     agg("account.move", INV, filters=[F("Due Date", ["today"], ">")]), count=1)
case("a-am-credit-notes", "account.move", "aggregate", "page", agg("account.move", CN), count=5)
case("a-am-bills", "account.move", "aggregate", "page", agg("account.move", BILLS), count=3)
case("a-am-bills-posted-sum", "account.move", "aggregate", "choice",
     agg("account.move", BILLS, op="sum", field="Total", filters=[F("Status", ["Posted"])]),
     count=2, sums={"Total": r"^652\.27 USD$"})
case("a-am-acme-residual", "account.move", "aggregate", "link",
     agg("account.move", INV, op="sum", field="Amount Due",
         filters=[F("Partner", ["Acme Corporation"]), F("Payment Status", ["Not Paid", "Partially Paid"])]),
     count=3, sums={"Amount Due": r"^92,750\.00 USD$"})
case("a-am-group-partner", "account.move", "aggregate", "group",
     agg("account.move", INV, group_by="Partner"), groups=[(r"^Acme Corporation$", 3), (r"^Azure Interior$", 1)])
case("a-am-vendor-refunds", "account.move", "aggregate", "page", agg("account.move", VREF), count=2)

# -- aggregates: partners and products --------------------------------------------------------
CUST, VEND, PROD = ("Invoicing / Customers / Customers / Sales / Orders / Customers",
                    "Invoicing / Vendors / Vendors", "Sales / Products / Products")
case("a-partner-customers-page", "res.partner", "aggregate", "page", agg("res.partner", CUST), count=2)
case("a-partner-companies-filter", "res.partner", "aggregate", "page-filter",
     agg("res.partner", CUST, page_filters=["Companies"]), count=2)
case("a-partner-vendors-country", "res.partner", "aggregate", "link",
     agg("res.partner", VEND, filters=[F("Country", ["United States"])]), count=1)
case("a-partner-entity-wide", "res.partner", "aggregate", "no-page",
     agg("res.partner", "", filters=[F("Country", ["Belgium"])]), count=1)
case("a-prod-page", "product.template", "aggregate", "page", agg("product.template", PROD), count=35)
case("a-prod-services-filter", "product.template", "aggregate", "page-filter",
     agg("product.template", PROD, page_filters=["Services"]), count=6)
case("a-prod-price-ge", "product.template", "aggregate", "number",
     agg("product.template", PROD, filters=[F("Sales Price", ["500"], ">=")]), count=10)
case("a-prod-group-category", "product.template", "aggregate", "group",
     agg("product.template", PROD, group_by="Product Category", top=1),
     groups=[(r"Office Furniture$", 27)])
case("a-prod-type-choice", "product.template", "aggregate", "choice",
     agg("product.template", PROD, filters=[F("Product Type", ["Service"])]), count=6)

# -- aggregates: lines ----------------------------------------------------------------------
case("a-sol-count-order", "sale.order.line", "aggregate", "link",
     agg("sale.order.line", "", filters=[F("Order Reference", ["S00004"])]), count=4)
case("a-sol-sum-order", "sale.order.line", "aggregate", "link",
     agg("sale.order.line", "", op="sum", field="Subtotal", filters=[F("Order Reference", ["S00004"])]),
     count=4, sums={"Subtotal": r"^2,240\.00 USD$"})
case("a-sol-count-all", "sale.order.line", "aggregate", "no-page", agg("sale.order.line", ""), count=44)
case("a-sol-sum-all", "sale.order.line", "aggregate", "no-page",
     agg("sale.order.line", "", op="sum", field="Subtotal"), sums={"Subtotal": r"^24,880\.50 USD$"})
case("a-sol-group-product", "sale.order.line", "aggregate", "group",
     agg("sale.order.line", "", group_by="Product", top=1), groups=[(r"Acoustic Bloc Screens", 12)])
case("a-sol-qty-product", "sale.order.line", "aggregate", "link",
     agg("sale.order.line", "", op="sum", field="Quantity", filters=[F("Product", ["Virtual Interior Design"])]),
     sums={"Quantity": r"^74\.00$"})
case("a-sol-subtotal-gt", "sale.order.line", "aggregate", "number",
     agg("sale.order.line", "", filters=[F("Subtotal", ["1000"], ">")]), count=8)
case("a-aml-count-invoice", "account.move.line", "aggregate", "link",
     agg("account.move.line", "", filters=[F("Journal Entry", ["INV/2026/00002"])]), count=3)

case("a-number-range", "sale.order", "aggregate", "number",
     agg("sale.order", O, filters=[F("Status", ["Sales Order"]), F("Total", ["500..1000"])]), count=6)
case("a-page-filters-on-declared-on-page", "account.move", "aggregate", "page-filter",
     {"deliverables": [{"kind": "aggregate", "entity": "account.move",
                        "page": {"key": BILLS, "filters_on": ["Posted"]},
                        "metric": {"op": "count"}}]}, count=2)
case("l-entity-over-page", "sale.order.line", "list", "via-lines",
     {"deliverables": [{"kind": "list", "entity": "sale.order.line", "record": "S00004",
                        "via": "Order Reference", "fields": ["Product", "Quantity"],
                        "page": {"key": O, "search": "Office Lamp"}}]},
     records_len=1, records_any=[r"Office Lamp.*Quantity: 10\.00"])
case("l-via-parent-fallback", "account.move.line", "list", "via-lines",
     {"deliverables": [{"kind": "list", "entity": "account.move.line", "record": "INV/2026/00001",
                        "via": "Move", "fields": ["Product"]}]},
     records_len=2, records_any=[r"Large Meeting Table", r"Four Person Desk"])
case("f-line-entity-named-by-parent", "sale.order.line", "list", "via-lines",
     {"deliverables": [{"kind": "fact", "entity": "sale.order.line", "record": "S00004",
                        "via": "Order Reference", "fields": ["Quantity"]}]},
     records_len=4, records_any=[r"Office Lamp.*Quantity: 10\.00"])
case("a-line-metric-named-by-invoice", "account.move.line", "aggregate", "via-lines",
     {"deliverables": [{"kind": "fact", "entity": "sale.order.line", "record": "INV/2026/00001",
                        "via": "Order Reference", "fields": ["Quantity"],
                        "metric": {"op": "sum", "field": "Quantity",
                                   "filters": [{"field": "Product", "values": ["Large Meeting Table"]}]}}]},
     count=1, sums={"Quantity": r"^5\.00$"})
case("l-search-words-are-a-period", "account.move", "list", "date-abs",
     lst("account.move", page="Invoicing / Customers / Invoices", search="2026-09"), records_len=3)
case("h-field-named-loosely", "sale.order", "handoff", "field",
     handoff("sale.order", "S00002", "Expiration Date"), page={"highlight": "Expiration"})
case("h-field-named-by-tokens", "account.move", "handoff", "field",
     handoff("account.move", "BILL/2026/09/0001", "Invoice Date"), page={"highlight": "Invoice/Bill Date"})
case("l-search-names-a-customer", "sale.order", "list", "text",
     lst("sale.order", page=Q, search="Ready Mat"), records_len=2,
     records_any=[r"^S00002", r"^S00003"])
case("l-search-in-a-text-field", "res.partner", "list", "text",
     lst("res.partner", page=CUST, search="Tracy"), records_any=[r"^Ready Mat", r"^The Jackson Group"])
case("l-page-defaults-kept-when-not-empty", "res.partner", "list", "page-filter",
     {"deliverables": [{"kind": "list", "entity": "res.partner",
                        "page": {"key": CUST, "filters_on": ["Individuals"]}}]},
     records_any=[r"^Acme Corporation, "])
case("p-search-names-a-record", "product.template", "page", "record",
     page(key=PROD) | {"deliverables": [{"kind": "page", "page": {"key": PROD, "view": "kanban", "search": "Large Desk"}}]},
     page={"url": r"^/odoo/products/13$"})
case("p-menu-less-record-model-route", "account.move", "page", "via",
     page(entity="account.move", record="INV/2026/00001", via="Payment Terms"),
     page={"url": r"^/odoo/account\.payment\.term/6$"})
case("f-line-as-field", "account.move", "fact", "via-lines",
     fact("account.move", "INV/2026/00001", ["Large Meeting Table"]), facts={"Quantity": r"^5\.00$"})
case("f-invoice-payment-status", "account.move", "fact", "none",
     fact("account.move", "INV/2026/00002", ["Payment Status", "Type"]),
     facts={"Payment Status": r"^Not Paid$", "Type": r"^Customer Invoice$"})

# -- lists ----------------------------------------------------------------------------------
case("l-sol-order-lines", "sale.order.line", "list", "link",
     lst("sale.order.line", fields=["Product", "Quantity", "Unit Price", "Subtotal"],
         filters=[F("Order Reference", ["S00004"])]),
     records_len=4, records_any=[r"Office Lamp.*Quantity: 10\.00.*Unit Price: 45\.00.*Subtotal: 450\.00"])
case("l-so-top3-total", "sale.order", "list", "sort",
     lst("sale.order", page=O, sort="Total", descending=True, limit=3),
     records_len=3, first_record=r"^S00020")
case("l-so-oldest", "sale.order", "list", "sort",
     lst("sale.order", page=Q, sort="Order Date", limit=1, fields=["Order Date"]),
     records_len=1, first_record=r"^S00018.*2026-08-08")
case("l-so-search", "sale.order", "list", "text", lst("sale.order", page=Q, search="S0001"),
     count=10, records_len=10)
case("l-prod-chair", "product.template", "list", "text", lst("product.template", page=PROD, search="Chair"),
     count=4, records_any=[r"Office Chair$", r"Conference Chair"])
case("l-prod-desk", "product.template", "list", "text", lst("product.template", page=PROD, search="Desk"),
     count=9)
case("l-prod-none", "product.template", "list", "text", lst("product.template", page=PROD, search="椅子"),
     problem=r"no Product matches")
case("l-am-overdue", "account.move", "list", "page-filter-today",
     lst("account.move", page=INV, page_filters=["Overdue"], fields=["Due Date", "Amount Due"]),
     records_len=3, records_any=[r"INV/2026/00004.*Due Date: 2026-08-28.*Amount Due: 31,750\.00 USD"])
case("l-so-fields", "sale.order", "list", "sort",
     lst("sale.order", page=O, fields=["Customer", "Total"], sort="Order Date", descending=True, limit=2),
     records_len=2, records_any=[r"Customer: .*; Total: .* USD"])
case("l-partner-belgium", "res.partner", "list", "no-page",
     lst("res.partner", filters=[F("Country", ["Belgium"])]), records_len=1, first_record=r"^OpenWood")

# -- pages ----------------------------------------------------------------------------------
case("p-list", "sale.order", "page", "none", page(key=Q), page={"url": r"^/odoo/sales$"})
case("p-view", "sale.order", "page", "view", page(key=O, view="kanban"),
     page={"url": r"^/odoo/orders\?view_type=kanban$", "view": "kanban"})
case("p-filter-off", "sale.order", "page", "facet", page(key=Q, off=["My Quotations"]),
     page={"off": ("My Quotations",)})
case("p-group", "sale.order", "page", "facet", page(key=O, group_by="Salesperson"),
     page={"on": ("Salesperson",)})
case("p-filter-on", "account.move", "page", "facet", page(key=INV, on=["To pay"]),
     page={"on": ("To pay",)})
case("p-record-order", "sale.order", "page", "record", page(entity="sale.order", record="S00006"),
     page={"url": r"^/odoo/orders/6$"})
case("p-record-quotation", "sale.order", "page", "record", page(entity="sale.order", record="S00002"),
     page={"url": r"^/odoo/sales/2$"})
case("p-record-section", "res.partner", "page", "section",
     page(entity="res.partner", record="Gemini Furniture", section="Internal Notes"),
     page={"url": r"^/odoo/customers/11$", "section": "Internal Notes"})
case("p-record-section-fuzzy", "product.template", "page", "section",
     page(entity="product.template", record="Large Desk", section="variants"),
     page={"url": r"^/odoo/products/13$", "section": "Attributes & Variants"})
case("p-via", "sale.order", "page", "via", page(entity="sale.order", record="S00006", via="Customer"),
     page={"url": r"^/odoo/customers/16$"})
case("p-via-label-fallback", "account.move", "page", "via",
     page(entity="account.move", record="BILL/2026/09/0001", via="Invoice Partner Display Name"),
     page={"url": r"^/odoo/customers/15$"})
case("p-via-no-action", "account.move", "page", "via",
     page(entity="account.move", record="INV/2026/00001", via="Payment Terms"),
     page={"url": r"/6$"})
case("p-nomenu-page", "product.tag", "page", "none", page(key="Product Tags"),
     page={"url": r"^/odoo/action-173$"})
case("p-config-page", "uom.category", "page", "none", page(key="Units of Measure Categories"),
     page={"url": r"^/odoo/action-90$"})
case("p-bad-view", "product.tag", "page", "view", page(key="Product Tags", view="pivot"),
     problem=r"no pivot view")
case("p-product-record", "product.template", "page", "record",
     page(entity="product.template", record="Office Chair"), page={"url": r"^/odoo/products/5$"})
case("p-bill-record", "account.move", "page", "record",
     page(entity="account.move", record="BILL/2026/09/0001"), page={"url": r"^/odoo/vendor-bills/5$"})
case("p-refund-record", "account.move", "page", "record",
     page(entity="account.move", record="RINV/2026/00002"), page={"url": r"^/odoo/customer-invoices/9$"})
case("p-rehome", "res.partner", "page", "rehome", page(entity="res.partner", record="S00006"),
     page={"url": r"^/odoo/customers/16$"})
case("p-entity-default-page", "sale.order", "page", "none", page(entity="sale.order"),
     page={"url": r"^/odoo/orders$"})

# -- handoffs -------------------------------------------------------------------------------
case("h-pay", "account.move", "handoff", "none", handoff("account.move", "INV/2026/00002", "Pay"),
     page={"url": r"^/odoo/customer-invoices/2$", "highlight": "Pay"})
case("h-confirm", "sale.order", "handoff", "none", handoff("sale.order", "S00002", "confirm"),
     page={"url": r"^/odoo/sales/2$", "highlight": "Confirm"})
case("h-send", "sale.order", "handoff", "none", handoff("sale.order", "S00005", "Send by Email"),
     page={"url": r"^/odoo/sales/5$", "highlight": "Send by Email"})
case("h-unknown-control", "sale.order", "handoff", "none", handoff("sale.order", "S00006", "Teleport"),
     page={"highlight": "Teleport"})
case("h-new-record", "res.partner", "handoff", "create", handoff("res.partner", "", "New"),
     page={"url": r"^/odoo/customers$", "highlight": "New"})
case("h-new-quotation", "sale.order", "handoff", "create", handoff("sale.order", "", ""),
     page={"url": r"^/odoo/sales$", "highlight": "New"})
case("h-new-named", "sale.order", "handoff", "create", handoff("sale.order", "Test Quote", "New"),
     page={"url": r"^/odoo/sales$", "highlight": "New"})
case("h-new-for-a-customer", "account.move", "handoff", "create",
     handoff("account.move", "Azure Interior", "New"),
     page={"url": r"^/odoo/customer-invoices$", "highlight": "New"})
case("h-field", "sale.order", "handoff", "field", handoff("sale.order", "S00002", "Expiration"),
     page={"url": r"^/odoo/sales/2$", "highlight": "Expiration"})


# -- records described, not named ------------------------------------------------------------
def described(kind, entity, filters, page="", action="", fields=(), via="", section="", search=""):
    d = {"kind": kind, "entity": entity, "record": "", "via": via, "fields": list(fields),
         "section": section, "action": action, "page": {"key": page, "search": search},
         "metric": {"op": "count", "filters": list(filters)}}
    return {"deliverables": [d], "write_intent": kind == "handoff"}


case("h-described-by-customer-and-status", "sale.order", "handoff", "described",
     described("handoff", "sale.order", [F("Customer", ["Joel Willis"]), F("Status", ["Quotation Sent"])],
               page="Quotations", action="Confirm"),
     page={"url": r"^/odoo/sales/19$", "highlight": "Confirm"})
case("h-described-by-amount", "sale.order", "handoff", "described",
     described("handoff", "sale.order", [F("Customer", ["Acme"]), F("Total", ["1740"]),
                                         F("Status", ["Quotation"])],
               page="Quotations", action="Send by Email"),
     page={"url": r"^/odoo/sales/1$", "highlight": "Send by Email"})
case("h-described-by-invoice-status", "sale.order", "handoff", "described",
     described("handoff", "sale.order", [F("Invoice Status", ["To Invoice"])],
               page="Sales Orders", action="Create Invoice"),
     page={"url": r"^/odoo/orders/7$"})
case("h-described-last-month-invoice", "account.move", "handoff", "described",
     described("handoff", "account.move", [F("Invoice Date", ["last month"])],
               page="Invoices", action="Pay"),
     page={"url": r"^/odoo/customer-invoices/4$", "highlight": "Pay"})
case("h-described-in-description-and-record", "sale.order", "handoff", "described",
     {"deliverables": [{"kind": "handoff", "entity": "sale.order",
                        "record": "the quotation sent to Joel Willis", "action": "Confirm",
                        "metric": {"op": "count", "filters": [F("Customer", ["Joel Willis"]),
                                                              F("Status", ["Quotation Sent"])]},
                        "page": {"key": "Quotations"}}], "write_intent": True},
     page={"url": r"^/odoo/sales/19$", "highlight": "Confirm"})
case("h-described-record-not-found-is-refused", "sale.order", "handoff", "described",
     handoff("sale.order", "the quotation sent to Joel Willis", "Confirm"),
     problem=r"no Sales Order matches")
case("h-described-ambiguous", "sale.order", "handoff", "described",
     described("handoff", "sale.order", [F("Customer", ["Gemini Furniture"])],
               page="Sales Orders", action="Confirm"),
     problem=r"several", candidates_min=2)
case("h-described-none", "sale.order", "handoff", "described",
     described("handoff", "sale.order", [F("Total", ["999"])], page="Quotations", action="Confirm"),
     problem=r"no Sales Order matches")
case("f-described-via-partner", "account.move", "fact", "described",
     described("fact", "account.move", [F("Invoice Date", ["2026-09"]), F("Total", ["30"])],
               page="Bills", via="Partner", fields=["Email"]),
     facts={"Email": r"@example\.com$"})
case("f-described-amount-beyond-page", "account.move", "fact", "described",
     described("fact", "account.move", [F("Total", ["31750"]), F("Type", ["Customer Credit Note"])],
               page="Invoices", fields=["Partner"]),
     facts={"Partner": r"^Acme Corporation$"})
case("p-described-section", "account.move", "page", "described",
     described("page", "account.move", [F("Total", ["31750"]), F("Type", ["Customer Credit Note"])],
               page="Credit Notes", section="Invoice Lines"),
     page={"url": r"^/odoo/[a-z-]+/12$", "section": "Invoice Lines"})
case("p-described-by-search-words", "res.partner", "page", "described",
     described("page", "res.partner", [], page="Customers", section="Internal Notes", search="Azure"),
     page={"url": r"^/odoo/customers/15$", "section": "Internal Notes"})

# -- lines of a record named or described through its own entity ------------------------------
case("l-lines-of-described-order", "sale.order.line", "list", "described",
     described("list", "sale.order", [F("Order Date", ["2026-08-22"])], page="Quotations",
               fields=["Order Lines", "Product", "Subtotal"]),
     records_len=2, records_any=[r"Subtotal"])
case("l-lines-of-named-order-by-field", "sale.order.line", "list", "via-lines",
     lst("sale.order", page="Quotations", fields=["Order Lines"]) | {"deliverables": [
         {"kind": "list", "entity": "sale.order", "record": "S00004", "fields": ["Order Lines"]}]},
     records_len=4)
case("p-section-reads-the-lines", "account.move.line", "page", "section",
     page(entity="account.move", record="BILL/2026/09/0001", section="Invoice Lines"),
     page={"section": "Invoice Lines"}, records_len=2)

# -- creation on the page declared, field names by whole words, filters switched off -----------
case("h-new-on-declared-page", "account.move", "handoff", "create",
     {"deliverables": [{"kind": "handoff", "entity": "account.move", "action": "New",
                        "page": {"key": "Bills"}}], "write_intent": True},
     page={"url": r"^/odoo/vendor-bills$", "highlight": "New"})
case("h-new-on-declared-credit-notes", "account.move", "handoff", "create",
     {"deliverables": [{"kind": "handoff", "entity": "account.move", "action": "New",
                        "page": {"key": "Credit Notes"}}], "write_intent": True},
     page={"url": r"^/odoo/credit-notes$", "highlight": "New"})
case("f-field-by-its-name", "sale.order", "fact", "none",
     fact("sale.order", "S00001", ["Validity Date"]), facts={"Expiration": r"^\d{4}-\d{2}-\d{2}"})
case("f-field-by-whole-words", "account.move", "fact", "none",
     fact("account.move", "BILL/2025/06/0001", ["Invoice Date"]),
     facts={"Invoice/Bill Date": r"^2025-06-17"})
case("f-field-trailing-word-is-not-a-field", "account.move", "fact", "none",
     fact("account.move", "BILL/2025/06/0001", ["Validity Date"]), problem=r"none of the fields")
case("l-filter-on-and-off", "product.template", "list", "page-filter",
     lst("product.template", page="Products") | {"deliverables": [
         {"kind": "list", "entity": "product.template",
          "page": {"key": "Products", "filters_on": ["Sales"], "filters_off": ["Purchase"]}}]},
     count=1)
case("p-view-the-page-lacks", "product.tag", "page", "view",
     page(key="Product Tags", view="pivot"), page={"missing_view": "pivot", "view": ""})


# -- lines whose owner is given as a condition the link cannot resolve ---------------------------
case("l-lines-invoice-number-on-order-lines", "account.move.line", "list", "via-lines",
     lst("sale.order.line", filters=[F("Order Reference", ["INV/2026/00002"])], fields=["Quantity"]),
     records_len=2)
case("l-lines-link-named-as-the-model", "account.move.line", "list", "via-lines",
     lst("account.move.line", filters=[F("Move", ["BILL/2026/09/0001"])], fields=["Quantity"]),
     records_len=2)
case("a-lines-sum-link-named-as-the-model", "account.move.line", "aggregate", "via-lines",
     agg("account.move.line", "", op="sum", field="Quantity", filters=[F("Move", ["BILL/2026/09/0001"])]),
     count=2)
case("l-lines-owner-described-by-amount", "sale.order.line", "list", "described",
     lst("sale.order.line", filters=[F("Order Reference", ["1541.50"])], fields=["Product", "Quantity"]),
     records_len=2, records_any=[r"Quantity"])
case("l-lines-owner-described-by-date", "sale.order.line", "list", "described",
     lst("sale.order.line", filters=[F("Order Reference", ["2026-08-22"])], fields=["Product"]),
     records_len=2)
case("l-lines-product-words", "sale.order.line", "list", "text",
     lst("sale.order.line", filters=[F("Order Reference", ["S00007"]), F("Product", ["Office"])]),
     records_len=2, records_any=[r"Office Lamp", r"Office Chair"])
case("l-partner-company-filter-by-value", "res.partner", "list", "page-filter",
     lst("res.partner", page="Customers", search="example.com",
         filters=[F("Company Type", ["Company"])]),
     records_any=[r"Azure Interior"])
case("h-described-sorted", "sale.order", "handoff", "described",
     {"deliverables": [{"kind": "handoff", "entity": "sale.order", "action": "Customer",
                        "sort": "Total", "descending": True, "page": {"key": "Quotations"},
                        "metric": {"op": "count", "filters": [F("Customer", ["Ready Mat"]),
                                                              F("Status", ["Quotation"])]}}],
      "write_intent": True},
     page={"url": r"^/odoo/sales/2$", "highlight": "Customer"})
case("h-after-a-list-that-found-one", "sale.order", "handoff", "described",
     {"deliverables": [{"kind": "list", "entity": "sale.order", "page": {"key": "Orders to Invoice"}},
                       {"kind": "handoff", "entity": "sale.order", "action": "Create Invoice"}],
      "write_intent": True},
     page={"url": r"^/odoo/orders/7$"}, deliverable=1)


# -- owner described through the line entity, page vocabulary as fields, emptiness, tabs ---------
case("l-lines-owner-by-status-word", "sale.order.line", "list", "described",
     lst("sale.order.line", page="Quotations", filters=[F("Order Reference", ["Quotation Sent"])],
         fields=["Product", "Quantity"]),
     records_len=3, records_any=[r"Quantity"])
case("l-lines-owner-by-period-and-total", "sale.order.line", "list", "described",
     lst("sale.order.line", filters=[F("Order Reference", ["last month"]), F("Total", ["1541.5"])],
         fields=["Product", "Quantity"]),
     records_len=2)
case("l-lines-owner-by-parent-page-and-date", "account.move.line", "list", "described",
     lst("account.move.line", page="Invoices", page_filters=["Invoices"],
         filters=[F("Invoice Date", ["2026-08-28"])], fields=["Product", "Unit Price"]),
     records_len=2, records_any=[r"Unit Price"])
case("l-lines-owner-described-not-found", "sale.order.line", "list", "described",
     lst("sale.order.line", filters=[F("Order Reference", ["last year"])], fields=["Product"]),
     problem=r"no Sales Order matches")
case("a-date-filter-name-as-field", "account.move", "aggregate", "date-abs",
     agg("account.move", "Bills", filters=[F("Accounting Date", ["2025-06-01", "2025-06-30"], "between")]),
     count=1)
case("p-described-by-date-filter-name", "account.move", "page", "described",
     described("page", "account.move", [F("Accounting Date", ["2026-09"]), F("Total", ["30"])],
               page="Bills", via="Partner", section="Internal Notes"),
     page={"url": r"^/odoo/customers/15$", "section": "Internal Notes"})
case("f-unknown-page-filter-on-a-page-without-filters", "account.move", "fact", "described",
     {"deliverables": [{"kind": "fact", "entity": "account.move", "via": "Partner", "fields": ["Email"],
                        "metric": {"op": "count", "filters": [F("Total", ["30"]), F("Invoice Date", ["2026-09"])],
                                   "page_filters": ["Vendor Bills"]},
                        "page": {"key": "Bills"}}]},
     facts={"Email": r"@example\.com$"})
case("a-lines-with-a-product", "account.move.line", "aggregate", "link",
     agg("account.move.line", "Refunds", filters=[F("Product", ["not empty"], "!=")]), count=2)
case("h-field-in-a-tab", "account.move", "handoff", "field",
     handoff("account.move", "BILL/2025/06/0001", "Payment Reference"),
     page={"url": r"^/odoo/vendor-bills/7$", "highlight": "Payment Reference"})
case("h-wizard-button", "sale.order", "handoff", "none", handoff("sale.order", "S00007", "Create Invoice"),
     page={"url": r"^/odoo/orders/7$", "highlight": "Create Invoice"})

case("l-lines-owner-described-in-one-value", "sale.order.line", "list", "described",
     lst("sale.order.line", page="Sales Orders", filters=[F("Order Reference", ["1541.50 USD last month"])],
         fields=["Product", "Quantity"]),
     records_len=2)
case("l-lines-owner-number-under-another-field", "account.move.line", "list", "via-lines",
     lst("account.move.line", page="Invoices", filters=[F("Invoice", ["INV/2026/00003"])],
         fields=["Product", "Quantity", "Subtotal"]),
     records_any=[r"Subtotal"])

case("l-lines-owner-in-record-text", "account.move.line", "list", "described",
     {"deliverables": [{"kind": "list", "entity": "account.move.line", "record": "11750",
                        "fields": ["Product", "Quantity"], "page": {"key": "Credit Notes"}}]},
     records_any=[r"Quantity"])
case("l-lines-after-a-list-that-found-one", "sale.order.line", "list", "described",
     {"deliverables": [{"kind": "list", "entity": "sale.order", "page": {"key": "Quotations"},
                        "metric": {"op": "count", "filters": [F("Status", ["Quotation Sent"])]}},
                       {"kind": "list", "entity": "sale.order.line", "fields": ["Product", "Quantity"]}]},
     records_len=3, deliverable=1)
case("a-number-with-its-operator", "account.move.line", "aggregate", "numeric",
     agg("account.move.line", "", filters=[F("Journal Entry", ["RINV/2026/00004"]), F("Subtotal", [">20000"])]),
     count=1)
case("h-locate-drops-words-that-match-nothing", "sale.order", "handoff", "described",
     {"deliverables": [{"kind": "handoff", "entity": "sale.order", "action": "Confirm",
                        "page": {"key": "Quotations", "search": "order dated"},
                        "metric": {"op": "count", "filters": [F("Order Date", ["2026-08-22"])]}}],
      "write_intent": True},
     page={"url": r"^/odoo/orders/11$"})


def _check(expect: dict, resolved) -> list[str]:
    errors = []
    d = resolved.deliverables[expect.get("deliverable", 0)]
    if "problem" in expect:
        if not re.search(expect["problem"], d.problem or ""):
            errors.append(f"problem {d.problem!r} !~ {expect['problem']!r}")
    elif d.problem and not (d.facts or d.aggregate or d.page):
        errors.append(f"unexpected problem {d.problem!r}")
    if "candidates_min" in expect and len(d.candidates) < expect["candidates_min"]:
        errors.append(f"candidates {d.candidates}")
    for label, pattern in expect.get("facts", {}).items():
        found = next((f for f in d.facts if f.label == label), None)
        if found is None:
            errors.append(f"fact {label!r} missing; have {[f.label for f in d.facts]}")
        elif not re.search(pattern, found.value):
            errors.append(f"fact {label}={found.value!r} !~ {pattern!r}")
    a = d.aggregate
    if "count" in expect and (a is None or a.count != expect["count"]):
        errors.append(f"count {a.count if a else None} != {expect['count']}")
    for label, pattern in expect.get("sums", {}).items():
        value = dict(a.sums).get(label) if a else None
        if value is None or not re.search(pattern, value):
            errors.append(f"sum {label}={value!r} !~ {pattern!r}")
    for i, (pattern, count) in enumerate(expect.get("groups", [])):
        group = a.groups[i] if a and i < len(a.groups) else None
        if group is None or not re.search(pattern, group[0]) or group[1] != count:
            errors.append(f"group[{i}] {group} != ({pattern!r}, {count})")
    if "note" in expect and (a is None or not re.search(expect["note"], a.note)):
        errors.append(f"note {a.note if a else None!r}")
    if "records_len" in expect and (a is None or len(a.records) != expect["records_len"]):
        errors.append(f"records {len(a.records) if a else None} != {expect['records_len']}"
                      + (f" (problem: {d.problem})" if a is None and d.problem else ""))
    for pattern in expect.get("records_any", []):
        if a is None or not any(re.search(pattern, r) for r in a.records):
            errors.append(f"no record ~ {pattern!r}: {list(a.records)[:3] if a else None}")
    if "first_record" in expect and (a is None or not a.records
                                     or not re.search(expect["first_record"], a.records[0])):
        errors.append(f"first record {a.records[0] if a and a.records else None!r}")
    p = d.page
    for key, want in expect.get("page", {}).items():
        have = getattr(p, key, None) if p is not None else None
        if key == "url":
            if have is None or not re.search(want, have):
                errors.append(f"page url {have!r} !~ {want!r}")
        elif key in ("on", "off"):
            have = getattr(p, "facets_on" if key == "on" else "facets_off", ()) if p else ()
            if tuple(have) != tuple(want):
                errors.append(f"page {key} {have} != {want}")
        elif have != want:
            errors.append(f"page {key} {have!r} != {want!r}")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8069")
    parser.add_argument("--credentials", type=Path, default=ROOT / ".private" / "odoo-reader")
    parser.add_argument("--only", default="")
    args = parser.parse_args(argv)
    client = OdooBusinessToolExecutor(
        base_url=args.base_url, keys=OdooApiKeyStore(args.credentials / "api-key.json"),
        timeout_s=20, allow_private_http=True)
    port = OdooGoalPort(client)
    t0 = time.monotonic()
    index = port.index()
    index_s = time.monotonic() - t0
    text = index.render()
    print(f"index: {len(index.entities)} entities, {len(index.pages)} pages, "
          f"{len(text)} chars, {index_s:.2f}s")

    def page_key(label):
        found = index.page(label)
        return found.key if found else label

    failures = []
    matrix = collections.defaultdict(lambda: [0, 0])
    reads = 0
    t_all = time.monotonic()
    for id_, entity, kind, condition, spec, expect in CASES:
        if args.only and args.only not in id_:
            continue
        for d in spec["deliverables"]:
            if d.get("page") and d["page"].get("key"):
                d["page"]["key"] = page_key(d["page"]["key"])
        try:
            resolution = port.resolve(GoalSpec.from_json(spec))
            errors = _check(expect, resolution)
            reads += len(resolution.reads)
        except Exception as exc:  # noqa: BLE001 -- a crash is a matrix failure, reported
            errors = [f"{type(exc).__name__}: {exc}"]
        matrix[(kind, condition)][1] += 1
        if errors:
            failures.append((id_, errors))
        else:
            matrix[(kind, condition)][0] += 1
    elapsed = time.monotonic() - t_all
    total = sum(v[1] for v in matrix.values())
    passed = sum(v[0] for v in matrix.values())
    print(f"\n{'kind':10} {'condition':18} pass/total")
    for (kind, condition), (ok, n) in sorted(matrix.items()):
        print(f"{kind:10} {condition:18} {ok}/{n}")
    print(f"\nTOTAL {passed}/{total} in {elapsed:.2f}s, {reads} read-only RPC calls, zero model calls")
    for id_, errors in failures:
        print(f"FAIL {id_}: " + " | ".join(errors))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
