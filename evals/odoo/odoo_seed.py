"""Odoo-shell program that builds the P7.4 fixture from one population JSON.

Runs inside the isolated evaluation Odoo container only, against a database initialised
without demo data. It is self-contained on purpose: the container has no checkout, and the
evaluator must not share code with the product. It creates exactly the population's records,
assigns the references the population decided, freezes scheduled actions, issues one API key
per synthetic user, verifies every predicted value against the database, and writes a
truth map plus the credentials to a private output directory. Any mismatch raises before
commit.
"""
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from odoo import fields

POPULATION = json.loads(Path("/run/p74/population.json").read_text())
OUTPUT = Path("/run/woa-generated")
DATABASE = "woa_trial"
READ_SCOPES = ["customers:read", "invoices:read", "products:read", "sale_orders:read"]
WRITE_SCOPES = READ_SCOPES + ["customers:write", "invoices:write", "sale_orders:write"]
PROFILES = {
    "sales_and_accounting_manager": ("base.group_user", "sales_team.group_sale_manager",
                                     "account.group_account_manager"),
    "sales_and_accounting_reader": ("base.group_user",
                                    "sales_team.group_sale_salesman_all_leads",
                                    "account.group_account_readonly"),
}


def require(condition, message):
    if not condition:
        raise RuntimeError("p74 fixture postcondition failed: " + message)


def secret(name):
    value = Path("/run/woa-private", name).read_text().strip()
    require(len(value) >= 20, name + " is too short")
    return value


if env["res.partner"].sudo().search_count([("customer_rank", ">", 0)]):
    raise RuntimeError("the p74 fixture must be seeded into an empty database")

usd = env.ref("base.USD")
eur = env.ref("base.EUR")
eur.sudo().write({"active": True})
main = env.company.sudo()
main.write({"name": POPULATION["companies"]["main"]["name"], "currency_id": usd.id})
second = env["res.company"].sudo().create({
    "name": POPULATION["companies"]["second"]["name"], "currency_id": usd.id})
companies = {"main": main, "second": second}
for rate in POPULATION["currency_rates"]:
    env["res.currency.rate"].sudo().create({
        "currency_id": eur.id, "company_id": companies[rate["company"]].id,
        "name": rate["name"], "rate": float(rate["rate"])})

users = {}
for role, spec in POPULATION["users"].items():
    company = companies[spec["company"]]
    groups = [env.ref(xmlid).id for xmlid in PROFILES[spec["profile"]]]
    users[role] = env["res.users"].sudo().with_context(no_reset_password=True).create({
        "name": "P74 " + role.title(), "login": spec["login"], "email": spec["login"],
        "password": secret(role + "-login-password"), "company_id": company.id,
        "company_ids": [(6, 0, [company.id])], "groups_id": [(6, 0, groups)],
        "tz": "UTC", "lang": "en_US"})

partners = {}
for spec in POPULATION["customers"]:
    partners[spec["key"]] = env["res.partner"].sudo().create({
        "name": spec["name"], "email": spec["email"] or False,
        "phone": spec["phone"] or False, "city": spec["city"] or False,
        "vat": spec["vat"] or False, "is_company": True,
        "customer_rank": spec["customer_rank"],
        "company_id": companies[spec["company"]].id})

products = {}
for spec in POPULATION["products"]:
    values = {"name": spec["name"], "default_code": spec["default_code"],
              "list_price": float(spec["list_price"]), "sale_ok": True,
              "type": spec["type"], "invoice_policy": spec["invoice_policy"],
              "taxes_id": [(6, 0, [])], "company_id": False}
    if spec["type"] == "service":
        values["service_type"] = "manual"
    products[spec["key"]] = env["product.template"].sudo().create(values)

pricelists = {}
for company_key, company in companies.items():
    for code, currency in (("USD", usd), ("EUR", eur)):
        pricelists[(company_key, code)] = env["product.pricelist"].sudo().create({
            "name": f"P74 {code} {company.name}", "currency_id": currency.id,
            "company_id": company.id})

orders = {}
for spec in POPULATION["orders"]:
    company = companies[spec["company"]]
    order = env["sale.order"].sudo().with_company(company).create({
        "name": spec["name"], "partner_id": partners[spec["partner"]].id,
        "company_id": company.id, "pricelist_id": pricelists[(spec["company"], spec["currency"])].id,
        "date_order": spec["date_order"], "validity_date": spec["validity_date"],
        "client_order_ref": spec["client_order_ref"] or False,
        "order_line": [(0, 0, {
            "product_id": products[line["product"]].product_variant_id.id,
            "product_uom_qty": line["quantity"], "price_unit": float(line["price_unit"]),
            "tax_id": [(6, 0, [])]}) for line in spec["lines"]]})
    orders[spec["key"]] = order

for spec in POPULATION["orders"]:
    order = orders[spec["key"]].with_company(companies[spec["company"]])
    if spec["state"] == "sent":
        order.write({"state": "sent"})
    elif spec["state"] == "sale":
        order.action_confirm()
    elif spec["state"] == "cancel":
        order._action_cancel()
    # Confirmation stamps the current time; the population's absolute date is the fact.
    order.write({"date_order": spec["date_order"]})

bank = env["account.journal"].sudo().search(
    [("type", "=", "bank"), ("company_id", "=", main.id)], limit=1)
require(bank, "the main company has no bank journal")
invoices = {}
for spec in POPULATION["invoices"]:
    order = orders[spec["source_order"]].with_company(main)
    move = order._create_invoices()
    require(len(move) == 1, f"{spec['key']} did not create exactly one invoice")
    move.write({"invoice_date": spec["invoice_date"]})
    if spec["post"]:
        move.action_post()
    if spec["paid"] is not None:
        env["account.payment.register"].sudo().with_company(main).with_context(
            active_model="account.move", active_ids=move.ids).create({
                "amount": float(spec["paid"]), "payment_date": spec["invoice_date"],
                "journal_id": bank.id}).action_create_payments()
    invoices[spec["key"]] = move

crons = env["ir.cron"].sudo().with_context(active_test=False).search([])
frozen = sorted(cron.cron_name for cron in crons if cron.active)
crons.write({"active": False})
env.flush_all()

# -- verify every predicted value against the database -------------------------------------
for spec in POPULATION["orders"]:
    order = orders[spec["key"]]
    order.invalidate_recordset()
    require(order.name == spec["name"], spec["key"] + " reference")
    require(order.state == spec["state"], spec["key"] + " state " + order.state)
    require(order.currency_id.name == spec["currency"], spec["key"] + " currency")
    require(Decimal(str(order.amount_total)).quantize(Decimal("0.01"))
            == Decimal(spec["amount_total"]), spec["key"] + " amount")
    require(fields.Datetime.to_string(order.date_order) == spec["date_order"],
            spec["key"] + " date")
for spec in POPULATION["invoices"]:
    move = invoices[spec["key"]]
    move.invalidate_recordset()
    require((move.name or None) == spec["name"] or (not spec["post"] and move.state == "draft"),
            spec["key"] + " number " + str(move.name))
    require(Decimal(str(move.amount_residual)).quantize(Decimal("0.01"))
            == Decimal(spec["amount_residual"]), spec["key"] + " residual")
delivery = orders[POPULATION["roles"]["order.delivery_no"]]
require(delivery.invoice_status == "no", "delivery order must have nothing to invoice")
require(orders[POPULATION["roles"]["order.to_invoice"]].invoice_status == "to invoice",
        "to-invoice order status")
require(orders[POPULATION["roles"]["order.fully_invoiced"]].invoice_status == "invoiced",
        "fully invoiced order status")

keys = {}
for role, user in users.items():
    keys[role] = env["res.users.apikeys"].sudo().with_user(user)._generate(
        "rpc", "p74-" + role, fields.Datetime.now() + timedelta(days=90))
env.cr.commit()

truth = {
    "artifact_version": 1,
    "kind": "p74_odoo_seed_truth",
    "population_seed_commitment": POPULATION["seed_commitment"],
    "companies": {key: company.id for key, company in companies.items()},
    "users": {role: {"id": user.id, "login": user.login} for role, user in users.items()},
    "partners": {key: record.id for key, record in partners.items()},
    "products": {key: record.id for key, record in products.items()},
    "orders": {key: record.id for key, record in orders.items()},
    "invoices": {key: record.id for key, record in invoices.items()},
    "frozen_crons": frozen,
}
OUTPUT.mkdir(mode=0o700, exist_ok=True)
payloads = {"truth.json": truth, "delegations.json": {"schema_version": 1, "principals": {
    f"{DATABASE}:{users['writer'].id}": {
        "api_key_file": "delegated-keys/writer.api-key.json",
        "scopes": WRITE_SCOPES, "allow_mutations": True},
    f"{DATABASE}:{users['reader'].id}": {
        "api_key_file": "delegated-keys/reader.api-key.json",
        "scopes": READ_SCOPES, "allow_mutations": False},
    f"{DATABASE}:{users['other'].id}": {
        "api_key_file": "delegated-keys/other.api-key.json",
        "scopes": READ_SCOPES, "allow_mutations": False},
}}}
for role, key in keys.items():
    payloads[f"{role}.api-key.json"] = {"database": DATABASE, "login": users[role].login,
                                        "api_key": key}
for filename, payload in payloads.items():
    path = OUTPUT / filename
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)
