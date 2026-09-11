"""Qualify Twenty breadth reads and navigation in real Chromium without a model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from evals.twenty.breadth import evaluate_breadth, validate_breadth_state
from evals.twenty.breadth_taskset import (instantiate_breadth_task,
                                          validate_breadth_task_set)
from evals.twenty.evaluator import digest_json
from evals.twenty.fixture import TwentyPostgresFixture, atomic_write_json, utc_now
from evals.woa.browser_host import playwright_runtime_factory, sample_visible_page
from evals.woa.scripted import ScriptedWoaChat
from evals.woa.twenty_runner import WoaTwentyConfig, run_breadth_case
from web_operation_agent_twenty.adapter import TwentyApplicationAdapter


QUALIFICATION_VERSION = 2
DEVELOPMENT_TASKSET = (
    Path(__file__).resolve().parents[1] / "twenty" / "tasksets"
    / "twenty-breadth-development-v2.json")
TASK_IDS = (
    "dev-enumerate-people-en", "dev-enumerate-opportunities-en",
    "dev-open-people-en", "dev-open-opportunities-en",
    "dev-module-people-en", "dev-module-opportunities-zh",
    "dev-refuse-person-delete-en",
)
_TOOLS = {"companies": "query_companies", "people": "query_people",
          "opportunities": "query_opportunities"}
_SKILLS = {"companies": "twenty.crm.companies-query",
           "people": "twenty.crm.people-query",
           "opportunities": "twenty.crm.opportunities-query"}
_SINGULAR = {"companies": "company", "people": "person",
             "opportunities": "opportunity"}


def _active(state: dict[str, Any], entity: str) -> list[dict[str, Any]]:
    selected = validate_breadth_state(state, role="baseline")
    return [row for row in selected["projection"][entity] if row["deleted_at"] is None]


def _replies(case: dict[str, Any], baseline: dict[str, Any]) -> list[tuple]:
    task = case["evaluator_task"]
    kind = task["kind"]
    if kind == "refuse":
        return [("finish", {"done": False, "why": "This mutation is not supported."})]
    entity = task["entity"]
    tool, skill = _TOOLS[entity], _SKILLS[entity]
    if kind in {"enumerate", "filter"}:
        args = {"limit": 50}
        if kind == "filter":
            args["name_contains"] = task["name_contains"]
        return [("load_skill", {"skill_id": skill}), (tool, args),
                (None, "The typed query completed.")]
    if kind == "open_record":
        target = next((row for row in _active(baseline, entity)
                       if row["id"] == task["record_id"]), None)
        if target is None:
            raise ValueError("breadth record target is absent from the baseline")
        route = f"/object/{_SINGULAR[entity]}/{target['id']}"
        return [
            ("load_skill", {"skill_id": skill}),
            (tool, {"name_contains": target["name"], "limit": 20}),
            ("goto", {"url": route}),
            ("wait_for", {"ms": 2_000}),
            ("finish", {"done": True, "why": "The requested record is visible."}),
        ]
    return [
        ("load_skill", {"skill_id": skill}), (tool, {"limit": 50}),
        ("goto", {"url": f"/objects/{entity}"}),
        ("wait_for", {"ms": 2_000}),
        ("finish", {"done": True, "why": "The requested collection is visible."}),
    ]


def qualify(*, fixture: TwentyPostgresFixture, adapter: TwentyApplicationAdapter,
            storage_state_path: Path, page_controller_module: str,
            development_taskset: dict[str, Any], ledger_path: Path,
            entry_path: str = "/objects/companies",
            task_ids: tuple[str, ...] = TASK_IDS,
            browser_executable: Path | None = None,
            headless: bool = True) -> dict[str, Any]:
    taskset = validate_breadth_task_set(development_taskset)
    if taskset["split"] != "development" or len(task_ids) != len(set(task_ids)):
        raise ValueError("breadth qualification requires distinct public development tasks")
    initial = fixture.capture_breadth(role="baseline")
    factory = playwright_runtime_factory(
        storage_state_path=storage_state_path,
        page_controller_module=page_controller_module,
        browser_executable=browser_executable, headless=headless)
    case_checks: dict[str, dict[str, Any]] = {}
    run_digests: list[str] = []
    evaluation_digests: list[str] = []
    for position, task_id in enumerate(task_ids, start=1):
        before = fixture.capture_breadth(role="baseline")
        if before["digest"] != initial["digest"]:
            raise ValueError("breadth fixture reset guard does not match the baseline")
        case = instantiate_breadth_task(taskset, task_id, before)
        run = run_breadth_case(
            case=case, adapter=adapter,
            client=ScriptedWoaChat(_replies(case, before)),
            config=WoaTwentyConfig(
                storage_state_path=storage_state_path, entry_path=entry_path,
                step_budget=8, max_page_actions=4, wall_time_s=120),
            ledger_path=ledger_path, runtime_factory=factory,
            page_sampler=sample_visible_page,
            run_id=f"woa-breadth-live-{position}")
        after = fixture.capture_breadth(role="observation")
        evaluation = evaluate_breadth(
            task=case["evaluator_task"], before=before, after=after,
            evidence=run["evidence"])
        accounting = run.get("trial", {}).get("accounting")
        kind = case["evaluator_task"]["kind"]
        expected_runner = (
            run.get("status") == "failed" and run.get("error_code") == "unsupported_operation"
            if kind == "refuse" else run.get("status") == "completed")
        case_checks[task_id] = {
            "task_kind": kind, "entity": case["evaluator_task"].get("entity"),
            "runner_status": run.get("status"), "runner_error_code": run.get("error_code"),
            "apparatus_code": run.get("apparatus_code"),
            "runner_outcome_expected": expected_runner,
            "evaluator_passed": evaluation.get("status") == "PASS",
            "state_unchanged": before["digest"] == after["digest"],
            "identity_unchanged": run.get("identity", {}).get(
                "session_binding_unchanged") is True,
            "accounting_complete": bool(
                isinstance(accounting, dict)
                and accounting.get("usage_unavailable_calls") == 0
                and accounting.get("total_cost_cny") == 0),
        }
        run_digests.append(run["run_digest"])
        evaluation_digests.append(digest_json(evaluation))
    required = ("runner_outcome_expected", "evaluator_passed", "state_unchanged",
                "identity_unchanged", "accounting_complete")
    checks = {
        "all_cases_passed": all(all(item[field] is True for field in required)
                                 for item in case_checks.values()),
        "schedule_completed": len(case_checks) == len(task_ids),
        "read_navigation_refusal_covered": {
            item["task_kind"] for item in case_checks.values()}
            == {"enumerate", "open_record", "open_collection", "refuse"},
        "new_entities_covered": {item["entity"] for item in case_checks.values()
                                 if item["entity"]} == {"people", "opportunities"},
    }
    return {
        "artifact_version": QUALIFICATION_VERSION,
        "kind": "woa_twenty_breadth_live_qualification",
        "status": "READY" if all(checks.values()) else "FAIL",
        "application_release": "twenty@2.39.0",
        "engine_release": "web-operation-agent@" + __import__(
            "web_operation_agent").__version__,
        "fixture_baseline_digest": initial["digest"],
        "task_set_digest": digest_json(taskset), "case_count": len(case_checks),
        "checks": checks, "case_checks": case_checks,
        "run_digests": run_digests, "evaluation_digests": evaluation_digests,
        "qualified_at": utc_now(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qualify WOA breadth on isolated real Twenty")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--storage-state", required=True)
    parser.add_argument("--oauth-token", required=True)
    parser.add_argument("--oauth-registration", required=True)
    parser.add_argument("--page-controller-module", required=True)
    parser.add_argument("--audit-ledger", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--db-user", required=True)
    parser.add_argument("--workspace-schema", required=True)
    parser.add_argument("--entry-path", default="/objects/companies")
    parser.add_argument("--taskset", default=str(DEVELOPMENT_TASKSET))
    parser.add_argument("--browser-executable")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    adapter = TwentyApplicationAdapter(
        base_url=args.base_url, token_path=args.oauth_token,
        registration_path=args.oauth_registration)
    result = qualify(
        fixture=TwentyPostgresFixture(
            container=args.container, database=args.database, db_user=args.db_user,
            workspace_schema=args.workspace_schema),
        adapter=adapter, storage_state_path=Path(args.storage_state),
        page_controller_module=args.page_controller_module,
        development_taskset=json.loads(Path(args.taskset).read_text(encoding="utf-8")),
        ledger_path=Path(args.audit_ledger), entry_path=args.entry_path,
        browser_executable=(Path(args.browser_executable) if args.browser_executable else None),
        headless=not args.headed)
    atomic_write_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
