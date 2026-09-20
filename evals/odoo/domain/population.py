"""Seeded synthetic Odoo population shared by the development and held-out task sets.

One generator, two independently sealed seeds. Every record a task needs is created under a
stable *role* (``customer.lookup``, ``quotation.confirm``), so a task specification names
roles and never a record value; the development and held-out populations fill the same
roles with different synthetic names, amounts and references. Record references and invoice
numbers are decided here rather than by Odoo sequences at seeding time, which lets the
fixture truth be sealed before a single record exists and lets provisioning prove that the
database it built is the population that was sealed.

Everything is synthetic: names are assembled from invented word parts plus a seed-derived
tag, e-mail domains are under ``example.test``, and dates are absolute.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

POPULATION_VERSION = 1
GENERATOR_ID = "p74-odoo-population-v1"
_SEED = re.compile(r"[0-9a-f]{32}\Z")

_FIRST = ("Aldmere", "Brackwell", "Corvane", "Dunmoor", "Elsgrove", "Farrowby", "Glenhaw",
          "Hollin", "Ivydell", "Juniper", "Kestrel", "Larkmoor", "Marrow", "Nettle", "Orwin",
          "Pellam", "Quillon", "Rookery", "Sablecrest", "Tamsin", "Umberly", "Vantor",
          "Wexley", "Yarrow", "Zennor", "Ashcombe", "Birchall", "Coldridge", "Dewhurst",
          "Everdon", "Fenwick", "Greyholt", "Hartwell", "Ingram", "Jessop", "Kinloss")
_SECOND = ("Supply", "Works", "Studio", "Logistics", "Foods", "Tooling", "Textiles",
           "Interiors", "Analytics", "Freight", "Ceramics", "Outfitters", "Botanicals",
           "Metals", "Printing", "Joinery")
_CITIES = ("Port Alden", "Cresthaven", "Millbrook Falls", "Saltmarsh", "Oakridge Bay",
           "Fernhill", "Stonecross", "Riverton Vale", "Ashbury", "Northwick")
_PRODUCT_NOUNS = ("Desk Lamp", "Filing Cabinet", "Standing Desk", "Monitor Arm",
                  "Whiteboard", "Office Chair", "Bookshelf", "Cable Tray", "Footrest",
                  "Room Divider", "Coat Stand", "Label Printer")
_WOODS = ("Alder", "Birch", "Cedar", "Walnut", "Maple", "Ash")
_ORDER_DATE = "2026-03-{day:02d} 10:00:00"


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def digest_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()


def seed_commitment(seed: str) -> str:
    """A commitment that reveals nothing practical about a 128-bit seed."""
    if not isinstance(seed, str) or not _SEED.fullmatch(seed):
        raise ValueError("population seed must be 128-bit lowercase hex")
    return "sha256:" + hashlib.sha256(
        f"{GENERATOR_ID}\0{seed}".encode("utf-8")).hexdigest()


class _Names:
    """Unique synthetic organisation names for one population."""

    def __init__(self, rng: random.Random, tag: str):
        self._rng = rng
        self._tag = tag
        self._used: set[str] = set()

    def take(self, *, second: str | None = None, suffix: str = "") -> str:
        for _ in range(10_000):
            name = f"{self._rng.choice(_FIRST)} {second or self._rng.choice(_SECOND)}"
            name = f"{name} {suffix}".strip() if suffix else f"{name} {self._tag}"
            if name.casefold() not in self._used:
                self._used.add(name.casefold())
                return name
        raise RuntimeError("synthetic name space is exhausted")

    def reserve(self, name: str) -> str:
        if name.casefold() in self._used:
            raise RuntimeError("synthetic name collision")
        self._used.add(name.casefold())
        return name


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def generate(seed: str) -> dict[str, Any]:
    """Return the complete population for ``seed``; identical seeds give identical output."""
    commitment = seed_commitment(seed)
    rng = random.Random(int(seed, 16))
    tag = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2)) + str(
        rng.randrange(2, 10))
    names = _Names(rng, tag)
    customers: list[dict[str, Any]] = []
    roles: dict[str, Any] = {}

    def customer(role: str | None, *, company: str = "main", email: bool = True,
                 phone: bool = True, city: str | None = None, vat: bool = False,
                 name: str | None = None, rank: int = 1) -> dict[str, Any]:
        selected = names.reserve(name) if name else names.take()
        record = {
            "key": f"partner:{len(customers) + 1:04d}",
            "name": selected,
            "company": company,
            "customer_rank": rank,
            "email": f"{_slug(selected)}@{_slug(selected.split()[0])}.example.test"
            if email else None,
            "phone": f"+1 555 {rng.randrange(100, 1000)} {rng.randrange(1000, 10000)}"
            if phone else None,
            "city": city if city is not None else rng.choice(_CITIES),
            "vat": f"US{rng.randrange(10 ** 8, 10 ** 9)}" if vat else None,
        }
        customers.append(record)
        if role:
            roles[role] = record["key"]
        return record

    # Main-company customers with a role. The ambiguous pair shares an exact prefix that
    # neither name has alone, so a prefix request matches both and nothing else.
    for role in ("customer.lookup", "customer.lookup_page", "customer.order_history",
                 "customer.quotation_pair", "customer.single_quotation",
                 "customer.eur", "customer.related_order", "customer.delete_target",
                 "customer.mixed_currency", "customer.duplicate_name",
                 "customer.duplicate_email", "customer.invoice_related",
                 "customer.partial_payment"):
        customer(role, vat=role in {"customer.lookup", "customer.lookup_page"})
    # The bare prefix is reserved too, so no generated customer can take it and turn a
    # two-way ambiguity into a three-way one.
    shared = names.take()
    customer("customer.ambiguous_a", name=f"{shared} North")
    customer("customer.ambiguous_b", name=f"{shared} South")
    roles["customer.ambiguous_prefix"] = shared
    customer("customer.null_contact", email=False, phone=False, city="")
    group_city = "Wrenfield " + tag
    for index in range(3):
        customer(f"customer.city_group_{index + 1}", city=group_city)
    roles["city.group"] = group_city
    # Pagination scale: more customers than one adapter page (200) and one UI page (80).
    for _ in range(220):
        customer(None)
    for index in range(4):
        customer(f"contact.non_customer_{index + 1}", rank=0)
    other = [customer(f"customer.other_company_{index + 1}", company="second")
             for index in range(3)]
    del other

    products: list[dict[str, Any]] = []

    def product(role: str | None, name: str, price: Decimal, *, policy: str = "order",
                kind: str = "consu") -> dict[str, Any]:
        record = {"key": f"product:{len(products) + 1:03d}", "name": names.reserve(name),
                  "default_code": f"P{tag}-{len(products) + 1:03d}",
                  "list_price": _money(price), "invoice_policy": policy, "type": kind}
        products.append(record)
        if role:
            roles[role] = record["key"]
        return record

    nouns = rng.sample(_PRODUCT_NOUNS, 8)
    woods = rng.sample(_WOODS, 2)
    product("product.lookup", f"{nouns[0]} {tag}", Decimal(rng.randrange(8000, 20000)) / 100)
    product("product.ambiguous_a", f"{nouns[1]} {woods[0]} {tag}",
            Decimal(rng.randrange(20000, 30000)) / 100)
    product("product.ambiguous_b", f"{nouns[1]} {woods[1]} {tag}",
            Decimal(rng.randrange(30000, 40000)) / 100)
    roles["product.ambiguous_prefix"] = f"{nouns[1]}"
    for index in range(3, 7):
        product(None, f"{nouns[index]} {tag}", Decimal(rng.randrange(5000, 60000)) / 100)
    product("product.max_price", f"{nouns[7]} {tag}", Decimal(rng.randrange(90000, 99000)) / 100)
    product("product.delivery_service", f"Installation Service {tag}",
            Decimal(rng.randrange(10000, 20000)) / 100, policy="delivery", kind="service")
    consumables = [item for item in products if item["type"] == "consu"
                   and item["key"] != roles["product.max_price"]]

    orders: list[dict[str, Any]] = []
    references = rng.sample(range(10_000, 99_999), 64)

    def order(role: str | None, partner_role_or_key: str, *, state: str, currency: str = "USD",
              day: int, lines: list[tuple[dict, int]] | None = None, company: str = "main",
              client_ref: str | None = None) -> dict[str, Any]:
        partner = roles.get(partner_role_or_key, partner_role_or_key)
        chosen = lines or [(rng.choice(consumables), rng.randrange(1, 6))
                           for _ in range(rng.randrange(1, 4))]
        total = sum((Decimal(item["list_price"]) * qty for item, qty in chosen), Decimal(0))
        record = {
            "key": f"order:{len(orders) + 1:03d}",
            "name": f"S{references[len(orders)]:05d}",
            "partner": partner, "company": company, "currency": currency,
            "state": state, "date_order": _ORDER_DATE.format(day=day),
            "validity_date": f"2026-05-{day:02d}",
            "client_order_ref": client_ref,
            "lines": [{"product": item["key"], "quantity": qty,
                       "price_unit": item["list_price"]} for item, qty in chosen],
            "amount_total": _money(total),
            "invoice": None,
        }
        orders.append(record)
        if role:
            roles[role] = record["key"]
        return record

    # Quotations (draft/sent). Amounts for the USD maximum are forced strictly above the rest.
    order("quotation.confirm", "customer.lookup", state="draft", day=2)
    order("quotation.deny", "customer.related_order", state="draft", day=3)
    order("quotation.pair_1", "customer.quotation_pair", state="draft", day=4)
    order("quotation.pair_2", "customer.quotation_pair", state="draft", day=5)
    order("quotation.single", "customer.single_quotation", state="draft", day=6)
    for index in range(3):
        order(f"quotation.sent_{index + 1}", "customer.order_history", state="sent",
              day=7 + index)
    order("quotation.eur_1", "customer.eur", state="draft", currency="EUR", day=10)
    order("quotation.eur_2", "customer.mixed_currency", state="sent", currency="EUR", day=11)
    # Twenty of the dearest product exceeds any random quotation (at most three lines of
    # five units under 600.00 each), so the USD maximum is unique by construction.
    order("quotation.max_usd", "customer.lookup_page", state="draft", day=12,
          lines=[(products[-2], 20)])
    # Confirmed orders.
    order("order.to_invoice", "customer.lookup", state="sale", day=13)
    order("order.eur_to_invoice", "customer.eur", state="sale", currency="EUR", day=14)
    order("order.fully_invoiced", "customer.order_history", state="sale", day=15)
    order("order.delivery_no", "customer.single_quotation", state="sale", day=16,
          lines=[(products[-1], 2)])
    order("order.history_2", "customer.order_history", state="sale", day=17)
    order("order.related", "customer.related_order", state="sale", day=18)
    order("order.page_fact", "customer.lookup_page", state="sale", day=19,
          client_ref=f"PO-{tag}-{rng.randrange(1000, 10000)}")
    order("order.mixed_usd", "customer.mixed_currency", state="sale", day=20)
    order("order.mixed_eur", "customer.mixed_currency", state="sale", currency="EUR", day=21)
    order("order.invoice_related", "customer.invoice_related", state="sale", day=22)
    order("order.cancelled", "customer.delete_target", state="cancel", day=23)
    # April orders bound the March date-range tasks.
    for index in range(2):
        record = order(f"order.april_{index + 1}", "customer.duplicate_name", state="sale",
                       day=24 + index)
        record["date_order"] = f"2026-04-{2 + index:02d} 10:00:00"
    order("quotation.other_company", "customer.other_company_1", state="draft", day=26,
          company="second")
    usd_quotes = [item for item in orders
                  if item["state"] in {"draft", "sent"} and item["currency"] == "USD"
                  and item["company"] == "main"]
    top = next(item for item in usd_quotes if item["key"] == roles["quotation.max_usd"])
    others = max(Decimal(item["amount_total"]) for item in usd_quotes if item is not top)
    if Decimal(top["amount_total"]) <= others:
        raise RuntimeError("population generator failed to make the USD maximum unique")

    # Invoices, posted in this order so their journal numbers are predictable.
    invoices: list[dict[str, Any]] = []

    def invoice(role: str, source_role: str, *, day: int, paid: str | None = None,
                post: bool = True) -> None:
        source = next(item for item in orders if item["key"] == roles[source_role])
        record = {
            "key": f"invoice:{len(invoices) + 1:03d}", "source_order": source["key"],
            "partner": source["partner"], "currency": source["currency"],
            "invoice_date": f"2026-03-{day:02d}",
            "amount_total": source["amount_total"], "post": post,
            "paid": paid, "amount_residual": source["amount_total"],
        }
        if paid is not None:
            record["amount_residual"] = _money(Decimal(source["amount_total"]) - Decimal(paid))
        invoices.append(record)
        source["invoice"] = record["key"]
        roles[role] = record["key"]

    invoice("invoice.fully_invoiced", "order.fully_invoiced", day=16)
    invoice("invoice.related", "order.invoice_related", day=23)
    history = next(item for item in orders if item["key"] == roles["order.history_2"])
    partial_amount = _money(Decimal(history["amount_total"]) / 2)
    invoice("invoice.partial", "order.history_2", day=18, paid=partial_amount)
    invoice("invoice.mixed_eur", "order.mixed_eur", day=22)
    sequence = 0
    for record in invoices:
        if record["post"]:
            sequence += 1
            record["name"] = f"INV/2026/{sequence:05d}"
        else:
            record["name"] = None

    new_records = {
        role: {"name": names.take(), "email": None} for role in (
            "new.create_customer", "new.create_customer_open", "new.missing_email",
            "new.deny_create", "new.reader_create", "new.duplicate_email_name")}
    for value in new_records.values():
        value["email"] = f"{_slug(value['name'])}@new.example.test"
    absent = {
        "absent.customer": names.take(),
        "absent.quotation_reference": f"S{references[-1]:05d}",
    }
    return {
        "artifact_version": POPULATION_VERSION,
        "kind": "p74_odoo_population",
        "generator": GENERATOR_ID,
        "seed_commitment": commitment,
        "tag": tag,
        "companies": {
            "main": {"name": f"Harbor Lane Trading {tag}", "currency": "USD"},
            "second": {"name": f"Northfield Components {tag}", "currency": "USD"},
        },
        "currency_rates": [{"currency": "EUR", "company": "main", "name": "2026-01-01",
                            "rate": "0.920000"}],
        "users": {
            "writer": {"login": "p74-writer@example.test", "company": "main",
                       "profile": "sales_and_accounting_manager"},
            "reader": {"login": "p74-reader@example.test", "company": "main",
                       "profile": "sales_and_accounting_reader"},
            "other": {"login": "p74-other@example.test", "company": "second",
                      "profile": "sales_and_accounting_reader"},
        },
        "customers": customers,
        "products": products,
        "orders": orders,
        "invoices": invoices,
        "roles": dict(sorted(roles.items())),
        "new_records": new_records,
        "absent": absent,
    }


def population_digest(population: dict[str, Any]) -> str:
    return digest_json(population)


def record(population: dict[str, Any], role: str) -> dict[str, Any]:
    """The record filling ``role``."""
    key = population["roles"].get(role)
    if not isinstance(key, str):
        raise KeyError(role)
    kind = key.split(":", 1)[0]
    table = {"partner": "customers", "product": "products", "order": "orders",
             "invoice": "invoices"}[kind]
    for item in population[table]:
        if item["key"] == key:
            return item
    raise KeyError(role)


def record_names(population: dict[str, Any]) -> frozenset[str]:
    """Every distinctive synthetic value a leak check must not find in product artifacts."""
    values = {population["companies"]["main"]["name"], population["companies"]["second"]["name"],
              population["tag"]}
    for table in ("customers", "products"):
        values.update(item["name"] for item in population[table])
    values.update(item["name"] for item in population["orders"])
    values.update(item["name"] for item in population["invoices"] if item.get("name")
                  and not str(item["name"]).startswith("INV/2026/"))
    values.update(item["name"] for item in population["new_records"].values())
    values.update(item["email"] for item in population["customers"] if item["email"])
    values.update(value for value in population["absent"].values())
    return frozenset(value for value in values if isinstance(value, str) and len(value) >= 3)
