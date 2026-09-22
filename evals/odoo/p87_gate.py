"""Paired Odoo gate: does deployment source change what the production Agent completes?

Each frozen task runs twice per repeat on the live reference Odoo, through the production
turn executor and `odoo_turn_config`, with zero task Skills:

* A -- the current page and the typed API, no deployment source;
* S -- A plus the production deployment-source capability.

The only evaluation seams are the ones `OdooDemoConfig.evaluation` names: page switches are
confirmed in the page, a hidden DOM probe reads the final page, and generic click is offered
with a scope statement that forbids every write. Fill, select, submit and back stay disabled.
A business-table fingerprint of the fixture database is taken around every attempt; any
change stops the run. The judge never enters Agent context.

This is an exploratory evidence batch on one fixture, not a reliability estimate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from evals.odoo.p86c import _operation_probe
from web_operation_agent_odoo.demo.demo import OdooDemoConfig, run_demo

ROOT = Path(__file__).resolve().parents[2]
TASKSET = ROOT / "evals" / "odoo" / "tasksets" / "p87-odoo-source-operations-v1.json"
TASKSET_SHA256 = "55f3c3ea4c6b8b60ad5f3e8188d814fdbfd3872136fa8b2f634e45b8be998955"
ARMS = ("A", "S")
FINGERPRINT_SQL = """
SELECT string_agg(t || ':' || c || ':' || coalesce(w, ''), ' ' ORDER BY t) FROM (
 SELECT 'res_partner' t, count(*)::text c, max(write_date)::text w FROM res_partner UNION ALL
 SELECT 'sale_order', count(*)::text, max(write_date)::text FROM sale_order UNION ALL
 SELECT 'sale_order_line', count(*)::text, max(write_date)::text FROM sale_order_line UNION ALL
 SELECT 'product_template', count(*)::text, max(write_date)::text FROM product_template UNION ALL
 SELECT 'product_pricelist', count(*)::text, max(write_date)::text FROM product_pricelist UNION ALL
 SELECT 'product_pricelist_item', count(*)::text, max(write_date)::text
   FROM product_pricelist_item UNION ALL
 SELECT 'account_move', count(*)::text, max(write_date)::text FROM account_move UNION ALL
 SELECT 'account_payment_term', count(*)::text, max(write_date)::text
   FROM account_payment_term UNION ALL
 SELECT 'account_fiscal_position', count(*)::text, max(write_date)::text
   FROM account_fiscal_position UNION ALL
 SELECT 'crm_tag', count(*)::text, max(write_date)::text FROM crm_tag UNION ALL
 SELECT 'uom_category', count(*)::text, max(write_date)::text FROM uom_category UNION ALL
 SELECT 'account_analytic_line', count(*)::text, max(write_date)::text
   FROM account_analytic_line UNION ALL
 SELECT 'ir_module_module', count(*) FILTER (WHERE state <> 'uninstalled')::text,
   max(write_date)::text FROM ir_module_module
) s;
"""
NAVIGATION_PROBE = """() => {
  const crumbs = [...document.querySelectorAll('.o_breadcrumb .breadcrumb-item, .o_breadcrumb .active')]
    .map(e => e.innerText.trim()).filter(Boolean);
  return {path: location.pathname,
          breadcrumb: (document.querySelector('.o_breadcrumb') || {}).innerText || '',
          breadcrumb_last: crumbs.length ? crumbs[crumbs.length - 1] : ''};
}"""


def _tasks() -> dict:
    raw = TASKSET.read_bytes()
    if hashlib.sha256(raw).hexdigest() != TASKSET_SHA256:
        raise RuntimeError("the frozen p87 taskset changed")
    doc = json.loads(raw)
    if doc.get("status") != "FROZEN" or len(doc.get("tasks", ())) != 17:
        raise RuntimeError("the p87 taskset has an invalid shape")
    return doc


def fingerprint(container: str, database: str) -> str:
    completed = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", "odoo", "-d", database, "-At"],
        input=FINGERPRINT_SQL, capture_output=True, text=True, timeout=60, check=True)
    return completed.stdout.strip()


def _probe(task: dict):
    if task["kind"] == "operation":
        probe = _operation_probe(task["probe"])
        if probe is None:
            raise RuntimeError(f"no probe for {task['id']}")
        return lambda page: {**probe(page), **page.evaluate(NAVIGATION_PROBE)}
    return lambda page: page.evaluate(NAVIGATION_PROBE)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().casefold()


def _decoded(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def judge(task: dict, row: dict) -> list[str]:
    """Failures for one attempt; empty means passed. Uses only the hidden projection."""
    failures: list[str] = []
    oracle = task["oracle"]
    probe = row.get("probe") or {}
    answer = row.get("answer") or ""
    if row.get("terminal_reason") != "agent_finished":
        failures.append(f"terminal:{row.get('terminal_reason')}")
    if row.get("write_detected"):
        failures.append("write_detected")
    if "breadcrumb_last" in oracle and _normalize(probe.get("breadcrumb_last", "")) != \
            _normalize(oracle["breadcrumb_last"]):
        failures.append("wrong_breadcrumb")
    if "path_equals" in oracle and probe.get("path") != oracle["path_equals"]:
        failures.append("wrong_path")
    for name, expected in oracle.get("probe_equals", {}).items():
        if _decoded(probe.get(name)) != _decoded(expected):
            failures.append(f"probe_mismatch:{name}")
    for name, minimum in oracle.get("probe_min", {}).items():
        value = probe.get(name)
        if not isinstance(value, (int, float)) or value < minimum:
            failures.append(f"probe_below_minimum:{name}")
    for name, pattern in oracle.get("probe_matches", {}).items():
        if re.search(pattern, str(probe.get(name) or "")) is None:
            failures.append(f"probe_no_match:{name}")
    if "answer_number" in oracle:
        numbers = {int(item) for item in re.findall(r"(?<![\d.])\d+(?![\d.])", answer)}
        if oracle["answer_number"] not in numbers:
            failures.append("wrong_number")
    for alternatives in oracle.get("all_concepts", []):
        if not any(_normalize(term) in _normalize(answer) for term in alternatives):
            failures.append("missing_concept:" + "|".join(alternatives))
    return failures


def _events(run_dir: Path) -> list[dict]:
    path = run_dir / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run_attempt(task: dict, arm: str, *, env: dict, output: Path, container: str,
                database: str, model_env: Path) -> dict:
    before = fingerprint(container, database)
    config = OdooDemoConfig(
        base_url=env["WOA_ODOO_BASE_URL"], api_key_path=Path(env["WOA_ODOO_API_KEY_FILE"]),
        storage_state_path=Path(env["WOA_ODOO_STORAGE_STATE"]),
        page_controller_module=env["WOA_ODOO_PAGE_CONTROLLER_MODULE"],
        entry_path=task["entry_path"],
        source_root=Path(env["WOA_ODOO_SOURCE_ROOT"]) if arm == "S" else None,
        source_release=env["WOA_ODOO_SOURCE_RELEASE"] if arm == "S" else "",
        scripted=False, headless=True, tasks=(task["prompt"],), output_dir=output,
        model_env=model_env, keep_open_seconds=0, turn_timeout_s=120.0,
        allow_generic_forms=True, evaluation=True, evaluation_page_click=True,
        confirm_navigation_in_page=True, final_page_probe=_probe(task))
    started = time.monotonic()
    error = ""
    summary: dict = {}
    try:
        summary = run_demo(config)
    except Exception as exc:  # noqa: BLE001 -- one attempt's failure is a recorded result
        error = f"{type(exc).__name__}: {exc}"[:500]
    wall = round(time.monotonic() - started, 3)
    after = fingerprint(container, database)
    run_dir = Path(summary["output"]) if summary.get("output") else None
    events = _events(run_dir) if run_dir else []
    finished = next((e for e in events if e.get("event") == "run_finished"), {})
    turn = (summary.get("turns") or [{}])[0]
    usage = summary.get("model_usage") or {}
    tools = [e.get("tool") for e in events if e.get("event") == "tool_call"]
    row = {
        "task": task["id"], "category": task["category"], "arm": arm,
        "terminal_reason": turn.get("terminal_reason") or ("harness_error" if error else ""),
        "answer": turn.get("answer", ""), "probe": summary.get("final_page_probe") or {},
        "write_detected": before != after, "error": error,
        "turn_wall_s": finished.get("wall_seconds"), "harness_wall_s": wall,
        "steps": turn.get("steps"), "page_actions": turn.get("page_actions"),
        "tools": tools,
        "source_lookups": sum(1 for e in events
                              if e.get("event") == "runtime_source_retrieved"),
        "source_candidates_opened": sum(1 for t in tools if t == "open_source_candidate"),
        "target_reached": any(e.get("event") == "target_state_reached" for e in events),
        "provider_calls": usage.get("provider_calls", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_tokens": usage.get("cached_tokens", 0),
        "reasoning_tokens": usage.get("reasoning_tokens", 0),
        "cost_cny": usage.get("cost_cny", 0.0),
        "run_dir": str(run_dir) if run_dir else "",
    }
    row["failures"] = judge(task, row)
    row["passed"] = not row["failures"]
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-env", type=Path, default=ROOT / ".env")
    parser.add_argument("--container", default="woa-odoo-reference-db-1")
    parser.add_argument("--database", default="woa_reference")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--select", default="",
                        help="diagnostic only: task_id:arm, e.g. nav-payment-terms:S")
    parser.add_argument("--plan", default="",
                        help="a verification subset: comma-separated task_id:ARMS entries, "
                             "e.g. op-product-sales-tab:AS,nav-payment-terms:S")
    args = parser.parse_args()
    doc = _tasks()
    env = {}
    for line in args.model_env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "attempts").mkdir(exist_ok=True)
    print(json.dumps({"output": str(args.output), "taskset_sha256": TASKSET_SHA256}),
          flush=True)
    plan: dict[str, set[str]] = {}
    for entry in filter(None, args.plan.split(",")):
        task_id, _, arms = entry.partition(":")
        if task_id not in {task["id"] for task in doc["tasks"]} or not arms \
                or set(arms) - set(ARMS):
            raise ValueError(f"invalid --plan entry: {entry}")
        plan[task_id] = set(arms)
    rows = []
    for repeat in range(1, args.repeats + 1):
        for index, task in enumerate(doc["tasks"]):
            arms = ARMS if (repeat + index) % 2 else tuple(reversed(ARMS))
            for arm in arms:
                identity = f"{task['id']}:{arm}"
                if args.select and identity != args.select:
                    continue
                if plan and arm not in plan.get(task["id"], set()):
                    continue
                path = args.output / "attempts" / f"r{repeat}-{task['id']}-{arm}.json"
                if path.exists():
                    row = json.loads(path.read_text())
                else:
                    row = run_attempt(task, arm, env=env,
                                      output=args.output / "runs",
                                      container=args.container, database=args.database,
                                      model_env=args.model_env)
                    row["repeat"] = repeat
                    path.write_text(json.dumps(row, ensure_ascii=False, indent=1) + "\n")
                rows.append(row)
                print(json.dumps({k: row[k] for k in (
                    "repeat", "task", "arm", "passed", "failures", "terminal_reason",
                    "turn_wall_s", "provider_calls", "cost_cny", "source_lookups",
                    "source_candidates_opened", "target_reached", "write_detected")},
                    ensure_ascii=False), flush=True)
                if row["write_detected"]:
                    print(json.dumps({"stopped": "write_detected", "attempt": identity}),
                          flush=True)
                    return 3
    (args.output / "projection.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"attempts": len(rows), "passed": sum(r["passed"] for r in rows),
                      "provider_calls": sum(r["provider_calls"] for r in rows),
                      "cost_cny": round(sum(r["cost_cny"] for r in rows), 4)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
