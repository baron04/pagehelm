"""Zero-model reachability check for the v4 page and handoff oracles.

This verifies that the ordinary reader can reach each sealed target state. For
handoffs it checks that the named live control exists; it does not fake a product
highlight or claim that the Agent can navigate there. Answer and limitation truth
need separate qualification.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from evals.odoo.goals.judge import _check
from evals.odoo.goals.probe import FINAL_STATE_PROBE
from evals.odoo.goals.run_goals import ROOT, load_tasks


def candidate_paths(expression: str) -> list[str]:
    if not expression.startswith("^") or not expression.endswith("$"):
        raise ValueError("the v4 page oracle needs an anchored path")
    path = expression[1:-1].replace(r"\.", ".")
    match = re.search(r"\(([^()]+)\)", path)
    if match is None:
        return [path]
    return [path[:match.start()] + option + path[match.end():]
            for option in match.group(1).split("|")]


def _control_visible(page, expression: str) -> bool:
    pattern = re.compile(expression)
    selectors = "button, [role='button'], label, .o_form_label, input, textarea, select"
    for control in page.locator(selectors).all():
        if not control.is_visible():
            continue
        label = control.get_attribute("aria-label") or control.get_attribute("title") or ""
        if not label:
            label = control.inner_text(timeout=1000) if control.evaluate(
                "e => !['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName)") else ""
        if pattern.search(label.strip()):
            return True
    return False


def _apply_state(page, check: dict) -> None:
    if "active_tab" in check:
        page.locator(".o_notebook .nav-link", has_text=check["active_tab"]).first.click()
    if "facet_contains" in check:
        label = check["facet_contains"]
        if label in ("Chair", "Cabinet"):
            page.locator(".o_searchview_input").fill(label)
            page.keyboard.press("Enter")
        else:
            page.locator(".o_searchview_dropdown_toggler").click()
            page.locator(".o_filter_menu .o_menu_item, .o_group_by_menu .o_menu_item",
                         has_text=label).first.click()
    if "facet_absent" in check:
        label = check["facet_absent"]
        facet = page.locator(".o_searchview_facet", has_text=label)
        if facet.count():
            facet.locator(".o_facet_remove").first.click()


def verify_task(page, task: dict, base_url: str) -> dict:
    check = task["parts"][0]["check"]
    for path in candidate_paths(check["path_regex"]):
        try:
            suffix = "?view_type=" + check["view_type"] if "view_type" in check else ""
            page.goto(base_url.rstrip("/") + path + suffix, wait_until="domcontentloaded")
            page.wait_for_function("() => !!document.querySelector('.o_breadcrumb')", timeout=15000)
            _apply_state(page, check)
            page.wait_for_timeout(700)
            probe = page.evaluate(FINAL_STATE_PROBE)
            state_check = {key: value for key, value in check.items()
                           if key != "highlight_regex"}
            state_ok = _check(state_check, probe, [], "")
            control_ok = ("highlight_regex" not in check
                          or _control_visible(page, check["highlight_regex"]))
            if state_ok and control_ok:
                return {"id": task["id"], "ok": True, "path": path}
        except Exception as exc:  # noqa: BLE001 -- try an alternate reviewed route
            last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            continue
        last_error = "target state or live control was not observed"
    return {"id": task["id"], "ok": False, "reason": last_error}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8069")
    parser.add_argument("--storage-state", type=Path,
                        default=ROOT / ".private" / "odoo-reader" / "storage-state.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tasks = [task for task in load_tasks("v4")["tasks"]
             if task["category"] in ("open", "handoff")]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(args.storage_state))
        page = context.new_page()
        try:
            rows = [verify_task(page, task, args.base_url) for task in tasks]
        finally:
            browser.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"checked": len(rows), "passed": sum(row["ok"] for row in rows),
                      "failed_ids": [row["id"] for row in rows if not row["ok"]]},
                     ensure_ascii=False))
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
