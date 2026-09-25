"""Run the Chinese one-sentence goal set on live Odoo with no task Skill, and score it independently.

Production configuration, as an ordinary salesperson: the reviewed click list, the page write
guard, the deployment's compiled source, and only the adapter's own generic typed-read Skills
-- no Skill written for any of these tasks. Each attempt is scored by `judge.score` from the
evaluator's final-page probe, the runtime's action receipts and the database fingerprint, and
a grading sheet is written for a person to score the same attempts without reading the
judge's verdict first.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import time
from pathlib import Path

from evals.odoo.goals.binding import bind_output, make_binding, recheck, recheck_checkout
from evals.odoo.goals.judge import score
from evals.odoo.goals.source_citations import bound_source
from evals.odoo.goals.probe import FINAL_STATE_PROBE
from evals.odoo.p87_gate import fingerprint
from web_operation_agent_odoo.demo.demo import OdooDemoConfig, run_demo

ROOT = Path(__file__).resolve().parents[3]
TASKSETS = {
    "v6-rules": (ROOT / "evals" / "odoo" / "tasksets" / "odoo-goals-zh-v6-rules.json", "7f2616cb8c34daa5b4bb0d2f29430454c7ce03021cb618243f48733b313fcfa2"),
    "v2": (ROOT / "evals" / "odoo" / "tasksets" / "odoo-goals-zh-v2.json", "d01fb4e3fa3d92b7e6916f3d36d6df35961a32442e7d9f2212c4c5eee152b57b"),
    "v4": (ROOT / "evals" / "odoo" / "tasksets" / "odoo-goals-zh-v4-holdout.json", "5fe83d925961164375d49ee7f9fe72d696f4370dae2b58be8c1a3dd7161e32e9"),
    "v3": (ROOT / "evals" / "odoo" / "tasksets" / "odoo-goals-zh-v3.json", "63ad3017827fce2dd055b4f0248672c3180e783eb8ba1b94a7fd979237b0c15b"),
    "v5": (ROOT / "evals" / "odoo" / "tasksets" / "odoo-goals-zh-v5-holdout.json", "01f20f8e91dbb215e397d667a2729debd20df95528a54d924373e1ca465ae1f5"),
}


def load_tasks(version: str = "v2") -> dict:
    path, digest = TASKSETS[version]
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise RuntimeError("the frozen goal task set changed")
    return json.loads(raw)


def run_attempt(task: dict, doc: dict, *, env: dict, credentials: Path, output: Path,
                model_env: Path, container: str, database: str, staged: bool = False) -> dict:
    source = (bound_source(doc, Path(env["WOA_ODOO_SOURCE_ROOT"]),
                           env["WOA_ODOO_SOURCE_RELEASE"])
              if "source_binding" in doc else None)
    before = fingerprint(container, database)
    started = time.monotonic()
    error = ""
    summary: dict = {}
    try:
        summary = run_demo(OdooDemoConfig(
            base_url=env["WOA_ODOO_BASE_URL"],
            api_key_path=credentials / "api-key.json",
            storage_state_path=credentials / "storage-state.json",
            page_controller_module=env["WOA_ODOO_PAGE_CONTROLLER_MODULE"],
            entry_path=doc["entry_path"], tasks=(task["prompt"],),
            source_root=Path(env["WOA_ODOO_SOURCE_ROOT"]),
            source_release=env["WOA_ODOO_SOURCE_RELEASE"],
            scripted=False, headless=True, output_dir=output, model_env=model_env,
            keep_open_seconds=0, turn_timeout_s=180.0,
            evaluation=True, evaluation_page_click=False,
            evaluation_reference_date=doc.get("reference_date", ""),
            confirm_navigation_in_page=True, goal_staged=staged,
            final_page_probe=lambda page: page.evaluate(FINAL_STATE_PROBE)))
    except Exception as exc:  # noqa: BLE001 -- one attempt's failure is a recorded result
        error = f"{type(exc).__name__}: {exc}"[:500]
    wall = round(time.monotonic() - started, 3)
    write_detected = fingerprint(container, database) != before
    run_dir = Path(summary["output"]) if summary.get("output") else None
    events = ([json.loads(line) for line in (run_dir / "audit.jsonl").read_text().splitlines()]
              if run_dir and (run_dir / "audit.jsonl").is_file() else [])
    from urllib.parse import urlsplit
    visited = [urlsplit(event.get("url_after") or "").path for event in events
               if event.get("event") == "action_receipt" and event.get("url_after")]
    turn = (summary.get("turns") or [{}])[0]
    usage = summary.get("model_usage") or {}
    probe = summary.get("final_page_probe") or {}
    answer = turn.get("answer", "")
    terminal = turn.get("terminal_reason") or ("harness_error" if error else "")
    verdict = score(task, probe=probe, visited=visited, answer=answer,
                    finished=terminal == "agent_finished", refused=terminal == "agent_refused",
                    write_detected=write_detected, source=source)
    return {
        "task": task["id"], "category": task.get("category", ""), "prompt": task["prompt"], "terminal_reason": terminal,
        "answer": answer, "probe": probe, "visited": visited,
        "write_detected": write_detected, "error": error, "wall_s": wall,
        "steps": turn.get("steps"), "provider_calls": usage.get("provider_calls", 0),
        "cost_cny": usage.get("cost_cny", 0.0),
        "clicks_refused": sum(1 for event in events
                              if event.get("event") == "click_policy_decision"
                              and not event.get("allowed")),
        **verdict.to_json(), "run_dir": str(run_dir) if run_dir else "",
    }


def sheet(rows: list[dict], doc: dict, destination: Path) -> None:
    head = ("<!doctype html><meta charset='utf-8'><title>目标任务评分表</title>"
            "<style>body{font:14px/1.6 system-ui;margin:24px;max-width:1200px}"
            "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;"
            "padding:8px;vertical-align:top}th{background:#f3f3f3;text-align:left}"
            "td.answer{white-space:pre-wrap}.note{background:#eef6ff;padding:12px;"
            "border:1px solid #9cc3ee}</style>")
    note = (f"<div class='note'>{html.escape(doc['note'])}<br>请先按"
            "“用户要求”和“最终页面”自己评分，再看最右列的自动评分。</div>")
    header = ("<tr><th>任务</th><th>用户要求</th><th>助手的回答</th><th>最终页面（评估器探针）"
              "</th><th>你的评分</th><th>自动评分</th></tr>")
    body = []
    for row in rows:
        probe = row["probe"]
        page = (f"{probe.get('path', '')}<br>面包屑：{probe.get('breadcrumb', '')}<br>"
                f"页签：{'、'.join(probe.get('active_tabs') or []) or '无'}<br>"
                f"视图：{probe.get('view_type', '')}<br>"
                f"筛选：{'、'.join(probe.get('facets') or []) or '无'}")
        parts = "<br>".join(
            f"[{index + 1}] {html.escape(part['quote'])}：实际"
            f"{'达成' if achieved else '未达成'}"
            for index, (part, achieved) in enumerate(zip(
                next(t for t in doc["tasks"] if t["id"] == row["task"])["parts"],
                row["achieved"])))
        verdict = ("成功" if row["success"] else "未成功") + "；" + (
            "如实" if row["honest"] else f"虚报第 {row['false_claims']} 部分")
        body.append(
            f"<tr><td>{html.escape(row['task'])}</td><td>{html.escape(row['prompt'])}</td>"
            f"<td class='answer'>{html.escape(row['answer'] or row['error'])}</td>"
            f"<td><small>{page}</small></td><td>　</td>"
            f"<td><small>{parts}<br><b>{verdict}</b></small></td></tr>")
    destination.write_text(head + note + "<table>" + header + "".join(body) + "</table>",
                           encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--taskset", "--version", choices=tuple(TASKSETS), default="v2")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-env", type=Path, default=ROOT / ".env")
    parser.add_argument("--base-url", help="browser-visible URL of the isolated Odoo deployment")
    parser.add_argument("--page-controller-module", help="controller module URL for that deployment")
    parser.add_argument("--credentials", type=Path, default=ROOT / ".private" / "odoo-reader")
    parser.add_argument("--container", default="woa-odoo-reference-db-1")
    parser.add_argument("--app-container", default="woa-odoo-reference-odoo-1")
    parser.add_argument("--database", default="woa_reference")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--lane", default="1/1")
    parser.add_argument("--staged", action="store_true",
                        help="take the staged declaration path (references, scope, details)")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--qualification", type=Path)
    arguments = parser.parse_args()
    env = {}
    for line in arguments.model_env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    if arguments.base_url:
        env["WOA_ODOO_BASE_URL"] = arguments.base_url
    if arguments.page_controller_module:
        env["WOA_ODOO_PAGE_CONTROLLER_MODULE"] = arguments.page_controller_module
    doc = load_tasks(arguments.taskset)
    if "source_binding" in doc:
        bound_source(doc, Path(env["WOA_ODOO_SOURCE_ROOT"]), env["WOA_ODOO_SOURCE_RELEASE"])
    binding = None
    if arguments.taskset == "v4":
        binding = make_binding(
            root=ROOT, task_digest=TASKSETS["v4"][1],
            reference_date=doc["reference_date"], db_container=arguments.container,
            database=arguments.database, app_container=arguments.app_container,
            source_release=env["WOA_ODOO_SOURCE_RELEASE"])
    if arguments.preflight_only:
        print(json.dumps({"taskset": arguments.taskset, "binding": binding},
                         ensure_ascii=False, sort_keys=True))
        return 0
    if binding is not None:
        if arguments.repeats != 1 or arguments.task:
            # Lanes may split a bound run, since every lane carries the same binding
            # and refuses to continue when it changes; a partial or repeated run is
            # not the one complete pass the seal calls for.
            raise RuntimeError("the sealed v4 holdout requires one complete run")
        from evals.odoo.goals.qualification import verify_qualification
        if arguments.qualification is None:
            raise RuntimeError("v4 requires --qualification from the zero-model oracle walk")
        verify_qualification(arguments.qualification, binding=binding, tasks=doc["tasks"])
    lane, lanes = (int(part) for part in arguments.lane.split("/"))
    arguments.output.mkdir(parents=True, exist_ok=True)
    (arguments.output / "attempts").mkdir(exist_ok=True)
    if binding is not None:
        bind_output(arguments.output, binding)
    rows = []
    position = -1
    for repeat in range(1, arguments.repeats + 1):
        for task in doc["tasks"]:
            if arguments.task and task["id"] not in arguments.task:
                continue
            position += 1
            if position % lanes != lane - 1:
                continue
            if binding is not None:
                recheck_checkout(binding, ROOT)
                recheck(binding, app_container=arguments.app_container,
                        db_container=arguments.container, database=arguments.database)
            path = arguments.output / "attempts" / f"r{repeat}-{task['id']}.json"
            if path.exists():
                row = json.loads(path.read_text())
            else:
                row = run_attempt(task, doc, env=env, credentials=arguments.credentials,
                                  output=arguments.output / "runs",
                                  model_env=arguments.model_env,
                                  container=arguments.container, database=arguments.database,
                                  staged=arguments.staged)
                row["repeat"] = repeat
                path.write_text(json.dumps(row, ensure_ascii=False, indent=1) + "\n")
            rows.append(row)
            print(json.dumps({key: row[key] for key in (
                "repeat", "task", "success", "honest", "achieved", "claimed",
                "false_claims",
                "terminal_reason", "provider_calls", "cost_cny",
                "write_detected")}, ensure_ascii=False), flush=True)
            if row["write_detected"]:
                print(json.dumps({"stopped": "write_detected", "task": row["task"]}),
                      flush=True)
                return 3
            if binding is not None:
                recheck_checkout(binding, ROOT)
                recheck(binding, app_container=arguments.app_container,
                        db_container=arguments.container, database=arguments.database)
    (arguments.output / f"projection-lane{lane}of{lanes}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
    sheet(rows, doc, arguments.output / f"grading-lane{lane}of{lanes}.html")
    normal = [row for row in rows if row["category"] != "impossible"]
    impossible = [row for row in rows if row["category"] == "impossible"]
    print(json.dumps({"attempts": len(rows), "success": sum(row["success"] for row in rows),
                      "reachable_success": sum(row["success"] for row in normal),
                      "reachable_total": len(normal),
                      "limitation_success": sum(row["success"] for row in impossible),
                      "limitation_total": len(impossible),
                      "honest": sum(row["honest"] for row in rows),
                      "false_claims": sum(len(row["false_claims"]) for row in rows),
                      "provider_calls": sum(row["provider_calls"] for row in rows),
                      "cost_cny": round(sum(row["cost_cny"] for row in rows), 4)},
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
