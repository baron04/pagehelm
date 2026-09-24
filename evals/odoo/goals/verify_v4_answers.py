"""Compare every sealed v4 answer oracle with independent read-only database queries.

These queries check underlying fixture truth, not the ordinary actor's ACL or the
Agent's ability to retrieve it. The page verifier checks UI reachability separately.
No query or expected value is sent to the model.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from evals.odoo.goals.run_goals import load_tasks

_SCOPE = """
WITH orders AS (
  SELECT s.*, partner.name AS customer, salesperson.name AS salesperson
  FROM sale_order s
  LEFT JOIN res_partner partner ON partner.id=s.partner_id
  LEFT JOIN res_users usr ON usr.id=s.user_id
  LEFT JOIN res_partner salesperson ON salesperson.id=usr.partner_id
), order_lines AS (
  SELECT line.*, ord.name AS order_name, template.name->>'en_US' AS product_name
  FROM sale_order_line line
  JOIN sale_order ord ON ord.id=line.order_id
  LEFT JOIN product_product variant ON variant.id=line.product_id
  LEFT JOIN product_template template ON template.id=variant.product_tmpl_id
), moves AS (
  SELECT move.*, partner.name AS partner_name
  FROM account_move move LEFT JOIN res_partner partner ON partner.id=move.partner_id
), move_lines AS (
  SELECT line.quantity, line.price_subtotal, move.name AS move_name,
         template.name->>'en_US' AS product_name
  FROM account_move_line line
  JOIN account_move move ON move.id=line.move_id
  LEFT JOIN product_product variant ON variant.id=line.product_id
  LEFT JOIN product_template template ON template.id=variant.product_tmpl_id
  WHERE line.display_type='product'
), products AS (
  SELECT *, name->>'en_US' AS english_name FROM product_template
)
"""

# The mapping is deliberately independent of the answer regexes in the sealed JSON.
# It is fixture qualification code, not a source of runtime business knowledge.
_QUERY = {
    "f01": "SELECT customer FROM orders WHERE name='S00006'",
    "f02": "SELECT amount_total::text||' '||customer FROM orders WHERE name='S00020'",
    "f03": "SELECT customer FROM orders WHERE name='S00007'",
    "f04": "SELECT salesperson FROM orders WHERE name='S00008'",
    "f05": "SELECT term.name->>'en_US' FROM moves m JOIN account_payment_term term ON term.id=m.invoice_payment_term_id WHERE m.name='INV/2026/00001'",
    "f06": "SELECT partner_name FROM moves WHERE name='BILL/2026/09/0001'",
    "f07": "SELECT replace(payment_state,'_',' ') FROM moves WHERE name='INV/2026/00002'",
    "f08": "SELECT CASE move_type WHEN 'out_refund' THEN 'Customer Credit Note' ELSE move_type END FROM moves WHERE name='RINV/2026/00002'",
    "f09": "SELECT product_uom_qty::text FROM order_lines WHERE order_name='S00004' AND product_name='Office Lamp'",
    "f10": "SELECT price_subtotal::text FROM order_lines WHERE order_name='S00002' AND product_name='Virtual Home Staging'",
    "f11": "SELECT quantity::text FROM move_lines WHERE move_name='INV/2026/00001' AND product_name='Large Meeting Table'",
    "f12": "SELECT price_subtotal::text FROM move_lines WHERE move_name='INV/2026/00002' AND product_name='Three-Seat Sofa'",
    "f13": "SELECT default_code FROM products WHERE english_name='Office Lamp'",
    "f14": "SELECT list_price::text FROM products WHERE english_name='Desk Organizer'",
    "f15": "SELECT type FROM products WHERE english_name='Virtual Interior Design'",
    "f16": "SELECT type FROM products WHERE english_name='Office Combo'",
    "f17": "SELECT email FROM res_partner WHERE name='Gemini Furniture'",
    "f18": "SELECT city FROM res_partner WHERE name='Lumber Inc'",
    "a01": "SELECT count(*) FROM orders WHERE state='sale'",
    "a02": "SELECT sum(amount_total) FROM orders WHERE state='sale'",
    "a03": "SELECT count(*) FROM orders WHERE state='sale' AND customer='Gemini Furniture'",
    "a04": "SELECT sum(amount_total) FROM orders WHERE state='sale' AND customer='Gemini Furniture'",
    "a05": "SELECT count(*) FROM orders WHERE state='draft'",
    "a06": "SELECT count(*) FROM orders WHERE state='sent'",
    "a07": "SELECT count(*) FROM orders WHERE state='sale' AND amount_total>1000",
    "a08": "SELECT count(*) FROM orders WHERE state='sale' AND amount_total BETWEEN 500 AND 1000",
    "a09": "SELECT count(*) FROM orders WHERE state='sale' AND date_order>='2026-09-01' AND date_order<'2026-10-01'",
    "a10": "SELECT sum(amount_total) FROM orders WHERE state='sale' AND date_order>='2026-08-01' AND date_order<'2026-09-01'",
    "a11": "SELECT count(*) FROM orders WHERE state='sale' AND date_order>='2026-09-01' AND date_order<'2026-10-01'",
    "a12": "SELECT count(*) FROM orders WHERE state='sale' AND date_order>='2026-04-01' AND date_order<'2026-07-01'",
    "a13": "SELECT sum(amount_total) FROM moves WHERE move_type='out_invoice'",
    "a14": "SELECT count(*) FROM moves WHERE move_type='out_refund'",
    "a15": "SELECT count(*)::text||' '||sum(amount_residual)::text FROM moves WHERE move_type='out_invoice' AND partner_name='Acme Corporation' AND payment_state='not_paid'",
    "a16": "SELECT count(*) FROM moves WHERE move_type='out_invoice' AND partner_name='Azure Interior'",
    "a17": "SELECT count(*) FROM moves WHERE move_type='in_invoice' AND state='posted'",
    "a18": "SELECT count(*) FROM moves WHERE move_type='out_invoice' AND payment_state='not_paid'",
    "a19": "SELECT count(*) FROM moves WHERE move_type='out_invoice' AND payment_state='not_paid' AND invoice_date_due<'2026-09-24'",
    "a20": "SELECT count(*) FROM moves WHERE move_type='out_invoice' AND invoice_date_due>'2026-09-24'",
    "a21": "SELECT sum(price_subtotal) FROM order_lines WHERE order_name='S00004'",
    "a22": "SELECT count(*) FROM order_lines WHERE order_name='S00002'",
    "a23": "SELECT count(*) FROM order_lines WHERE product_uom_qty>10",
    "a24": "SELECT count(*) FROM order_lines WHERE price_subtotal>1000",
    "a25": "SELECT count(*) FROM products WHERE type='service' AND sale_ok",
    "a26": "SELECT count(*) FROM products WHERE type='consu' AND sale_ok",
    "l01": "SELECT string_agg(name,' ' ORDER BY name) FROM orders WHERE customer='Ready Mat' AND state IN ('draft','sent')",
    "l02": "SELECT string_agg(name,' ' ORDER BY name) FROM orders WHERE customer='Acme Corporation' AND state IN ('draft','sent')",
    "l03": "SELECT string_agg(name,' ' ORDER BY name) FROM moves WHERE move_type='out_invoice' AND invoice_date>='2026-08-01' AND invoice_date<'2026-09-01'",
    "l04": "SELECT string_agg(name,' ' ORDER BY name)||' '||count(*)::text FROM moves WHERE move_type='out_invoice' AND invoice_date>='2026-09-01' AND invoice_date<'2026-10-01'",
    "l05": "SELECT string_agg(product_name,' ' ORDER BY product_name) FROM order_lines WHERE order_name='S00002'",
    "l06": "SELECT string_agg(product_name,' ' ORDER BY product_name) FROM order_lines WHERE order_name='S00003'",
    "l07": "SELECT string_agg(product_name,' ' ORDER BY product_name) FROM move_lines WHERE move_name='INV/2026/00001'",
    "l08": "SELECT string_agg(product_name,' ' ORDER BY product_name) FROM move_lines WHERE move_name='INV/2026/00002'",
    "l09": "SELECT string_agg(english_name,' ' ORDER BY english_name) FROM products WHERE english_name ILIKE '%Chair%' AND sale_ok",
    "l10": "SELECT string_agg(english_name,' ' ORDER BY english_name) FROM products WHERE english_name ILIKE '%Cabinet%' AND sale_ok",
    "l11": "SELECT string_agg(name,' ' ORDER BY name) FROM res_partner WHERE NOT is_company",
    "l12": "SELECT string_agg(name,' ' ORDER BY name) FROM res_partner WHERE is_company",
    "l13": "SELECT string_agg(partner.name,' ' ORDER BY partner.name) FROM res_partner partner JOIN res_country country ON country.id=partner.country_id WHERE partner.is_company AND country.code='BE'",
    "l14": "SELECT string_agg(name,' ' ORDER BY name) FROM res_partner WHERE is_company AND city='Tracy'",
}


def _read_value(db_container: str, database: str, query: str) -> str:
    command = ["docker", "exec", "-i", db_container, "psql", "-X", "-qAt", "-U", "odoo",
               "-d", database]
    result = subprocess.run(command, input="BEGIN READ ONLY;\n" + _SCOPE + query
                            + ";\nROLLBACK;\n", capture_output=True, text=True,
                            timeout=60, check=True)
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-container", default="woa-odoo-reference-db-1")
    parser.add_argument("--database", default="woa_reference")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tasks = [task for task in load_tasks("v4")["tasks"] if task["category"] == "answer"]
    if {task["id"] for task in tasks} != set(_QUERY):
        raise RuntimeError("the independent answer witness registry does not cover v4")
    rows = []
    for task in tasks:
        value = _read_value(args.db_container, args.database, _QUERY[task["id"]])
        checks = [re.search(part["check"]["answer_regex"], value) is not None
                  for part in task["parts"]]
        rows.append({"id": task["id"], "ok": all(checks), "parts": checks,
                     "witness": value if not all(checks) else ""})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"checked": len(rows), "passed": sum(row["ok"] for row in rows),
                      "failed_ids": [row["id"] for row in rows if not row["ok"]]},
                     ensure_ascii=False))
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
