"""Read-only button, source-selected navigation, and multistep workflow gate.

Uses the production turn executor and a disposable browser application. The frozen
taskset changes only the cited release rule, never the page or the hidden DOM probe.
No application writes are permitted. This intentionally small gate establishes a
capability boundary, not a reliability estimate for arbitrary applications.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path

from testapps.contacts.demo import DemoConfig, run_demo
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.planning.repository_source import RepositorySourceLookup
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent.schemas import SCHEMA_VERSIONS, SourceMap


TASKSET = Path(__file__).with_name("workflow-v1.json")
TASKSET_SHA256 = "e928738d800734b5caa9560f3024c63a893f235dc11d3038c915c822e0dd1657"


def _tasks() -> dict:
    raw = TASKSET.read_bytes()
    if hashlib.sha256(raw).hexdigest() != TASKSET_SHA256:
        raise RuntimeError("the frozen workflow taskset changed")
    tasks = json.loads(raw)
    if tasks.get("schema_version") != 1 or len(tasks.get("releases", [])) != 2:
        raise RuntimeError("the frozen workflow taskset has an invalid shape")
    return tasks


def _source(root: Path, release: str, desk: str) -> SourceCapability:
    file = root / "apps" / "contacts" / "vendor_workflow.py"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        f"# In deployed release {release}, vendor Orion's current operational "
        f"desk is the {desk.title()} desk. To inspect Orion's terms, open "
        f"/contacts/workflows/{desk} and select the Terms tab.\n",
        encoding="utf-8")
    tree = SourceTree({
        "repository_id": "pilot/contacts", "release": release,
        "allow_globs": ["apps/**/*.py"], "deny_globs": ["**/secrets/**"],
    }, root)
    source_map = SourceMap(
        schema_version=SCHEMA_VERSIONS["SourceMap"],
        attestation_id="synthetic-workflow-gate", repository_id=tree.repository_id,
        release=release, relations=[])
    return SourceCapability(source_map=source_map, runtime_source=True,
                            lookup=RepositorySourceLookup(tree))


def _probe(expected_path: str, expected_tab: str | None):
    def check(page):
        return page.evaluate("""expected => {
          const section = document.querySelector('[data-workflow-desk]');
          return {path_matched: location.pathname === expected.path,
                  tab_matched: expected.tab === null ||
                    (section !== null && section.dataset.activeTab === expected.tab)};
        }""", {"path": expected_path, "tab": expected_tab})
    return check


def _run(output: Path, case: dict, arm: str, model_env: Path) -> dict:
    release, desk, kind = case["release"], case["desk"], case["kind"]
    source = (_source(output / "sources" / release, release, desk)
              if arm == "B" else SourceCapability.none())
    start_path = ("/contacts/workflows/partner" if kind == "button"
                  else "/contacts/workflows")
    expected_path = (start_path if kind == "button"
                     else f"/contacts/workflows/{desk}")
    expected_tab = "terms" if kind in {"button", "multistep"} else None
    config = DemoConfig(
        headless=True, tasks=(case["prompt"],),
        output_dir=output / f"{kind}-{release}-{arm}", model_env=model_env,
        start_path=start_path, source=source, allow_page_click=True,
        final_page_probe=_probe(expected_path, expected_tab), turn_timeout_s=120,
        step_budget=10)
    started = time.monotonic()
    summary = run_demo(config)
    events = [json.loads(line) for line in
              (Path(summary["output"]) / "audit.jsonl").read_text().splitlines()]
    lookups = [event for event in events
               if event.get("event") == "runtime_source_retrieved"]
    rank = next((i for lookup in lookups for i, line in
                 enumerate(lookup.get("evidence", [])[:5], 1)
                 if f"{desk.title()} desk" in line), None)
    verdict = (summary.get("panel") or {}).get("final_page_probe") or {}
    turn = summary["turns"][0]
    usage = summary["model_usage"]
    writes = summary["application_state"]["write_requests"]
    created = summary["application_state"]["records_created"]
    row = {
        "case": kind, "release": release, "arm": arm,
        "passed": bool(verdict.get("path_matched") and verdict.get("tab_matched")
                       and turn["terminal_reason"] == "agent_finished"
                       and writes == created == 0),
        "path_matched": verdict.get("path_matched"),
        "tab_matched": verdict.get("tab_matched"),
        "terminal_reason": turn["terminal_reason"],
        "steps": turn["steps"], "page_actions": turn["page_actions"],
        "source_lookups": len(lookups), "target_rank": rank,
        "source_queries": [lookup.get("query") for lookup in lookups],
        "writes": writes, "created": created,
        "wall_s": round(time.monotonic() - started, 3),
        "provider_calls": usage.get("provider_calls", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_tokens": usage.get("cached_tokens", 0),
        "cost_cny": usage.get("cost_cny", 0.0),
    }
    if writes or created:
        raise RuntimeError("unexpected application write; stop the workflow gate")
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-env", type=Path, default=Path(".env"))
    parser.add_argument("--select", default="",
                        help="diagnostic only: case:release:arm, e.g. navigation:release-partner:B")
    args = parser.parse_args()
    tasks = _tasks()
    output = Path(tempfile.mkdtemp(prefix="woa-source-workflow-"))
    print(json.dumps({"private_output": str(output),
                      "taskset_sha256": TASKSET_SHA256}), flush=True)
    cases = []
    for release in tasks["releases"]:
        for kind in ("navigation", "multistep"):
            cases.append({**release, "kind": kind,
                          "prompt": tasks[f"{kind}_prompt"]})
    cases.append({**tasks["releases"][0], "kind": "button",
                  "prompt": tasks["button_prompt"]})
    rows = []
    for case in cases:
        for arm in ("A", "B"):
            identity = f"{case['kind']}:{case['release']}:{arm}"
            if args.select and identity != args.select:
                continue
            row = _run(output, case, arm, args.model_env)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)
    (output / "projection.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(json.dumps({"attempts": len(rows), "passed": sum(r["passed"] for r in rows),
                      "provider_calls": sum(r["provider_calls"] for r in rows),
                      "cost_cny": round(sum(r["cost_cny"] for r in rows), 4)},
                     sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
