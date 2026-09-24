"""Zero-model checks that v4's declared limitations match the reference deployment.

An absent module or hidden New control is evidence about this actor and build, not a
general statement about Odoo. This does not grade the eventual wording of an answer.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

from evals.odoo.goals.binding import fixture_signature
from evals.odoo.goals.run_goals import ROOT, load_tasks


def _query(container: str, database: str, sql: str) -> str:
    result = subprocess.run(["docker", "exec", "-i", container, "psql", "-X", "-qAt",
                             "-U", "odoo", "-d", database],
                            input="BEGIN READ ONLY;\n" + sql + ";\nROLLBACK;\n",
                            capture_output=True, text=True, timeout=60, check=True)
    return result.stdout.strip()


def _no_record(container: str, database: str, table: str, name: str) -> bool:
    if table not in ("sale_order", "account_move"):
        raise ValueError("unreviewed limitation table")
    return _query(container, database, f"SELECT count(*) FROM {table} WHERE name='{name}'") == "0"


def _uninstalled(container: str, database: str, module: str) -> bool:
    if module not in ("stock", "project", "mrp", "hr_payroll"):
        raise ValueError("unreviewed module")
    return _query(container, database,
                  f"SELECT coalesce((SELECT state FROM ir_module_module WHERE name='{module}'),"
                  "'absent')") in ("uninstalled", "absent")


def _ordinary_actor(container: str, database: str) -> bool:
    return _query(container, database, """
SELECT count(*) FROM res_groups_users_rel membership
JOIN res_users usr ON usr.id=membership.uid
JOIN ir_model_data marker ON marker.res_id=membership.gid AND marker.model='res.groups'
WHERE usr.login='woa.reader' AND marker.module='base'
AND marker.name IN ('group_system','group_erp_manager')
""") == "0"


def _no_new_button(page, base_url: str, path: str) -> bool:
    page.goto(base_url.rstrip("/") + path, wait_until="domcontentloaded")
    page.wait_for_function("() => !!document.querySelector('.o_breadcrumb')", timeout=15000)
    page.wait_for_timeout(700)
    return not page.get_by_role("button", name="New", exact=True).count()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-container", default="woa-odoo-reference-db-1")
    parser.add_argument("--database", default="woa_reference")
    parser.add_argument("--base-url", default="http://127.0.0.1:8069")
    parser.add_argument("--storage-state", type=Path,
                        default=ROOT / ".private" / "odoo-reader" / "storage-state.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ids = {task["id"] for task in load_tasks("v4")["tasks"]
           if task["category"] == "impossible"}
    expected = {*(f"missing{i:02}" for i in range(1, 6)),
                *(f"module{i:02}" for i in range(1, 6)),
                *(f"permission{i:02}" for i in range(1, 4)), "create01", "create02"}
    if ids != expected:
        raise RuntimeError("the limitation witness registry does not cover v4")
    before = fixture_signature(args.db_container, args.database)
    checks = {
        "missing01": _no_record(args.db_container, args.database, "sale_order", "S99991"),
        "missing02": _no_record(args.db_container, args.database, "sale_order", "S99992"),
        "missing03": _no_record(args.db_container, args.database, "account_move", "INV/2026/99991"),
        "missing04": _no_record(args.db_container, args.database, "account_move", "BILL/2026/09/9999"),
        "missing05": _no_record(args.db_container, args.database, "account_move", "RINV/2026/99991"),
        "module01": _uninstalled(args.db_container, args.database, "stock"),
        "module02": _uninstalled(args.db_container, args.database, "stock"),
        "module03": _uninstalled(args.db_container, args.database, "project"),
        "module04": _uninstalled(args.db_container, args.database, "project"),
        "module05": _uninstalled(args.db_container, args.database, "mrp"),
        "permission01": _ordinary_actor(args.db_container, args.database),
        "permission02": (_ordinary_actor(args.db_container, args.database)
                         and _uninstalled(args.db_container, args.database, "hr_payroll")),
        "permission03": _ordinary_actor(args.db_container, args.database),
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(args.storage_state))
        page = context.new_page()
        try:
            checks["create01"] = _no_new_button(page, args.base_url, "/odoo/products")
            checks["create02"] = _no_new_button(page, args.base_url, "/odoo/customers")
        finally:
            browser.close()
    after = fixture_signature(args.db_container, args.database)
    if after != before:
        raise RuntimeError("the fixture changed during limitation qualification")
    rows = [{"id": key, "ok": checks[key]} for key in sorted(expected)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"checked": len(rows), "passed": sum(row["ok"] for row in rows),
                      "failed_ids": [row["id"] for row in rows if not row["ok"]]},
                     ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
