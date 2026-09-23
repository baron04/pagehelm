"""Walk every frozen v2 goal oracle before a run, with no model: are the checks reachable and sound?

For each task the evaluator reaches the target state by hand, as the ordinary reader, and
scores it with the same judge a run uses. A sound oracle is met at its target, met by no
part on the entry page, and -- for a question part -- refuses a wrong sample answer. This
is how odoo-goals-zh-v2 was checked before it was frozen; a later goal set needs its own
recipes and samples.

    PYTHONPATH=src:examples/odoo/src:. python -m evals.odoo.goals.verify_oracles
"""
from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from evals.odoo.goals.judge import _check
from evals.odoo.goals.probe import FINAL_STATE_PROBE
from evals.odoo.goals.run_goals import ROOT, load_tasks



def G(path: str) -> tuple[str, str]:
    return ("goto", path)


def TAB(name: str) -> tuple[str, str]:
    return ("tab", name)


def LINK(href: str) -> tuple[str, str]:
    return ("link", href)


#: How the evaluator reaches each page target, as the reader would. Action numbers are the
#: reference database's; another database resolves its own.
PAGE = {
 "where-product-tags": [G("/odoo/action-173")], "where-credit-notes": [G("/odoo/credit-notes")],
 "where-vendor-payments": [G("/odoo/action-239")], "where-uom": [G("/odoo/action-90")],
 "where-order-tags": [G("/odoo/action-195")], "where-variants": [G("/odoo/action-177")],
 "gemini-notes": [G("/odoo/customers/11"), TAB("Internal Notes")],
 "s2-otherinfo": [G("/odoo/sales/2"), TAB("Other Info")],
 "organizer-sales": [G("/odoo/products/31"), TAB("Sales")],
 "lumber-invoicing": [G("/odoo/customers/16"), TAB("Invoicing")],
 "largedesk-variants": [G("/odoo/products/13"), TAB("Attributes & Variants")],
 "orders-kanban": [G("/odoo/orders?view_type=kanban")],
 "analysis-pivot": [G("/odoo/action-318?view_type=pivot")],
 "all-quotations": [G("/odoo/sales"), ("unfacet", "My Quotations")],
 "invoices-topay": [G("/odoo/invoicing"), ("filter", "To pay")],
 "orders-by-salesperson": [G("/odoo/orders"), ("group", "Salesperson")],
 "products-chair": [G("/odoo/products"), ("search", "Chair")],
 "s6-customer-profile": [G("/odoo/orders/6"), LINK("/odoo/res.partner/16")],
 "inv1-term-detail": [G("/odoo/invoicing/1"), LINK("/odoo/account.payment.term/6")],
 "bill-vendor-profile": [G("/odoo/bills/5"), LINK("/odoo/res.partner/15")],
 "rinv2-customer": [G("/odoo/credit-notes/9"), LINK("/odoo/res.partner/15")],
}
#: A right and a wrong sample answer for every question part.
ANS = {
 "s6-customer": ("**Customer**：Lumber Inc", "**Customer**：Gemini Furniture"),
 "inv1-term": ("**Payment Terms**：End of Following Month", "**Payment Terms**：30 Days"),
 "bill-vendor": ("**Vendor**：Azure Interior", "**Vendor**：Acme Corporation"),
 "chair-price": ("**Sales Price**：70.00 USD", "**Sales Price**：170.00 USD"),
 "s4-state": ("**Status**：Sales Order", "**Status**：Quotation"),
 "desk-variants": ("**数量**：5", "**数量**：15"),
 "gemini-notes": ("内部备注是空的，没有填写内容。", "备注：优先发货"),
 "invoices-topay": ("INV/2026/00001、INV/2026/00002、INV/2026/00003、INV/2026/00004", "INV/2026/00002"),
 "products-chair": ("Office Chair、Conference Chair、Office Chair Black", "Office Lamp"),
 "acme-unpaid": ("**数量**：3\n**合计**：92,750.00 USD", "**数量**：4\n**合计**：41,750.00 USD"),
 "gemini-confirmed": ("**数量**：13", "**数量**：18"),
 "s20-amount-customer": ("**Total**：2947.50\n**Customer**：Joel Willis", "**Total**：1740.00\n**Customer**：Ready Mat"),
 "top-customer": ("Gemini Furniture（13 单）", "Acme Corporation"),
 "unsent-quotes": ("**数量**：4", "**数量**：5"),
 "readymat-orders": ("**数量**：2", "**数量**：12"),
 "inv1-term-detail": ("付款条款是 End of Following Month：次月月末全额付清", "30 Days"),
 "rinv2-customer": ("**Customer**：Azure Interior", "**Customer**：Acme Corporation"),
}


def act(page, step, base_url: str) -> None:
    kind, arg = step
    if kind == "goto": page.goto(base_url + arg)
    elif kind == "tab": page.locator(".o_notebook .nav-link", has_text=arg).first.click()
    elif kind == "link": page.locator(f"a[href='{arg}']").first.click()
    elif kind in ("filter", "group"):
        page.click(".o_searchview_dropdown_toggler"); page.wait_for_timeout(600)
        menu = ".o_filter_menu" if kind == "filter" else ".o_group_by_menu"
        page.locator(f"{menu} .o_menu_item", has_text=arg).first.click()
    elif kind == "search":
        page.fill(".o_searchview_input", arg); page.keyboard.press("Enter")
    elif kind == "unfacet":
        page.locator(".o_searchview_facet", has_text=arg).locator(".o_facet_remove").first.click()
    page.wait_for_timeout(2200)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8069")
    parser.add_argument("--storage-state", type=Path,
                        default=ROOT / ".private" / "odoo-reader" / "storage-state.json")
    arguments = parser.parse_args()
    doc = load_tasks()
    uncovered = [task["id"] for task in doc["tasks"]
                 if task["id"] not in PAGE and task["id"] not in ANS
                 and not all(part["check"].get("impossible") for part in task["parts"])]
    if uncovered:
        raise SystemExit(f"no recipe or sample answer for: {', '.join(uncovered)}")
    bad = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_context(storage_state=str(arguments.storage_state)).new_page()
        page.goto(arguments.base_url + "/odoo")
        page.wait_for_timeout(2500)
        entry = page.evaluate(FINAL_STATE_PROBE)
        for task in doc["tasks"]:
            probe, visited = entry, [entry["path"]]
            if task["id"] in PAGE:
                try:
                    visited = []
                    for step in PAGE[task["id"]]:
                        act(page, step, arguments.base_url)
                        visited.append(urlsplit(page.url).path)
                    probe = page.evaluate(FINAL_STATE_PROBE)
                except Exception as exc:  # noqa: BLE001 -- an unreachable target is a finding
                    bad += 1
                    print("BAD ", task["id"], "could not reach:", str(exc)[:100])
                    continue
            right, wrong = ANS.get(task["id"], ("", ""))
            parts = task["parts"]
            at_target = [_check(part["check"], probe, visited, right)
                         or bool(part["check"].get("impossible")) for part in parts]
            at_entry = [_check(part["check"], entry, [entry["path"]], "") for part in parts]
            with_wrong = ([_check(part["check"], entry, [entry["path"]], wrong) for part in parts]
                          if wrong else [])
            ok = all(at_target) and not any(at_entry) and not any(with_wrong)
            bad += not ok
            print(("OK  " if ok else "BAD ") + task["id"].ljust(22), at_target, "entry", at_entry,
                  "wrong", with_wrong, "|", probe.get("path"), probe.get("breadcrumb", "")[-38:],
                  probe.get("active_tabs"), probe.get("view_type"), probe.get("facets"))
        browser.close()
    print("bad", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
