"""Run the Chinese held-out task set as an ordinary user, and lay out the grading sheet.

Every earlier Odoo result was produced as the database's administrator against tasks whose
answers an automatic judge could check. This runs the production configuration -- the
deployment's compiled source, its own Skills with Skill first, the reviewed click list and
the page write guard -- as the non-administrator `woa.reader`, on goals written in Chinese,
and makes no judgement of its own: it records what the assistant said and did and writes a
sheet for a person to grade.

The task set is a draft until its prompts are real users' words; the sheet says so.
"""
from __future__ import annotations

import argparse
import html
import json
import time
from pathlib import Path

from evals.odoo.p87_gate import fingerprint
from web_operation_agent_odoo.demo.demo import OdooDemoConfig, run_demo

ROOT = Path(__file__).resolve().parents[3]
TASKSET = ROOT / "evals" / "odoo" / "tasksets" / "odoo-heldout-zh-v1.json"


def run_task(task: dict, *, env: dict, credentials: Path, output: Path, model_env: Path,
             container: str, database: str) -> dict:
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
            entry_path=task["entry_path"], tasks=(task["prompt"],),
            source_root=Path(env["WOA_ODOO_SOURCE_ROOT"]),
            source_release=env["WOA_ODOO_SOURCE_RELEASE"],
            scripted=False, headless=True, output_dir=output, model_env=model_env,
            keep_open_seconds=0, turn_timeout_s=180.0,
            # The production surface: the reviewed click list decides clicks, the addon's
            # window makes the session read-only, and generic forms stay opt-in (off).
            evaluation=True, evaluation_page_click=False,
            allow_operation_in_page=True))
    except Exception as exc:  # noqa: BLE001 -- one task's failure is a recorded result
        error = f"{type(exc).__name__}: {exc}"[:500]
    wall = round(time.monotonic() - started, 3)
    after = fingerprint(container, database)
    turn = (summary.get("turns") or [{}])[0]
    usage = summary.get("model_usage") or {}
    run_dir = summary.get("output", "")
    events = []
    if run_dir and (Path(run_dir) / "audit.jsonl").is_file():
        events = [json.loads(line) for line in
                  (Path(run_dir) / "audit.jsonl").read_text().splitlines()]
    refused_clicks = [event for event in events
                      if event.get("event") == "click_policy_decision"
                      and event.get("allowed") is False]
    return {
        "id": task["id"], "category": task["category"], "prompt": task["prompt"],
        "expectation": task["expectation"], "entry_path": task["entry_path"],
        "answer": turn.get("answer", ""), "terminal_reason": turn.get("terminal_reason", ""),
        "status": turn.get("status", ""), "steps": turn.get("steps", 0),
        "page_actions": turn.get("page_actions", 0),
        "final_url": summary.get("last_url", ""),
        "provider_calls": usage.get("provider_calls", 0),
        "cost_cny": usage.get("cost_cny", 0.0), "wall_s": wall,
        "write_detected": before != after,
        "clicks_refused_by_review": len(refused_clicks),
        "skill_auto_loaded": [event.get("skill_id") for event in events
                              if event.get("event") == "business_skill_auto_loaded"],
        "provider_retries": len([event for event in events
                                 if event.get("event") == "provider_retry"]),
        "run_dir": run_dir, "error": error,
    }


def sheet(rows: list[dict], doc: dict, destination: Path) -> None:
    head = ("<!doctype html><meta charset='utf-8'><title>留出任务集评分表</title>"
            "<style>body{font:14px/1.6 system-ui;margin:24px;max-width:1100px}"
            "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;"
            "padding:8px;vertical-align:top}th{background:#f3f3f3;text-align:left}"
            "td.answer{white-space:pre-wrap}.note{background:#fff8e1;padding:12px;"
            "border:1px solid #e0c060}</style>")
    note = (f"<div class='note'><b>{html.escape(doc['status'])}</b>："
            f"{html.escape(doc['note'])}<br>评分标准："
            f"{html.escape(doc['grading']['rule'])}</div>")
    header = ("<tr><th>任务</th><th>用户目标</th><th>期望（给评分者）</th>"
              "<th>助手的回答</th><th>停在哪一页 / 动作</th><th>评分</th></tr>")
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{html.escape(row['id'])}<br><small>{html.escape(row['category'])}</small></td>"
            f"<td>{html.escape(row['prompt'])}</td>"
            f"<td>{html.escape(row['expectation'])}</td>"
            f"<td class='answer'>{html.escape(row['answer'] or row['error'])}</td>"
            f"<td><small>{html.escape(row['final_url'])}<br>"
            f"页面动作 {row['page_actions']}，步数 {row['steps']}，"
            f"{'检测到写入' if row['write_detected'] else '零写入'}</small></td>"
            "<td>　</td></tr>")
    destination.write_text(head + note + "<table>" + header + "".join(body) + "</table>",
                           encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-env", type=Path, default=ROOT / ".env")
    parser.add_argument("--credentials", type=Path,
                        default=ROOT / ".private" / "odoo-reader")
    parser.add_argument("--container", default="woa-odoo-reference-db-1")
    parser.add_argument("--database", default="woa_reference")
    parser.add_argument("--task", action="append", default=[])
    arguments = parser.parse_args()
    env = {}
    for line in arguments.model_env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    doc = json.loads(TASKSET.read_text(encoding="utf-8"))
    arguments.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for task in doc["tasks"]:
        if arguments.task and task["id"] not in arguments.task:
            continue
        row = run_task(task, env=env, credentials=arguments.credentials,
                       output=arguments.output / "runs", model_env=arguments.model_env,
                       container=arguments.container, database=arguments.database)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if row["write_detected"]:
            print(json.dumps({"stopped": "write_detected", "task": row["id"]}), flush=True)
            break
    (arguments.output / "results.json").write_text(
        json.dumps({"taskset": doc["suite_id"], "status": doc["status"], "rows": rows},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    sheet(rows, doc, arguments.output / "grading.html")
    print(json.dumps({"tasks": len(rows),
                      "writes": sum(row["write_detected"] for row in rows),
                      "errors": sum(bool(row["error"]) for row in rows),
                      "cost_cny": round(sum(row["cost_cny"] for row in rows), 4)},
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
