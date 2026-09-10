"""Qualify the Web Operation Agent read-only runner against an isolated real Twenty fixture.

The qualification runs one query and one open case from the public development task set
through the real embedded runtime in Chromium, the delegated Twenty API, the product turn
executor, the database before/after projection and the independent evaluator. The provider is
scripted, so it spends no model tokens. The output contains only versions, digests, counts
and Boolean checks.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from evals.pageagent.scripted import query_answer
from evals.twenty.evaluator import digest_json, evaluate, validate_state
from evals.twenty.fixture import TwentyPostgresFixture, atomic_write_json, utc_now
from evals.twenty.taskset import instantiate_task, validate_task_set
from evals.woa.browser_host import playwright_runtime_factory, sample_visible_page
from evals.woa.scripted import ScriptedWoaChat
from evals.woa.twenty_runner import WoaTwentyConfig, run_case
from web_operation_agent.twenty import TwentyApplicationAdapter


QUALIFICATION_VERSION = 1
SKILL_ID = "twenty.crm.companies-query"
_CASE_GATE_FIELDS = (
    "runner_completed", "evaluator_passed", "state_unchanged",
    "identity_unchanged", "accounting_complete",
)
DEVELOPMENT_TASKSET = (
    Path(__file__).resolve().parents[1] / "twenty" / "tasksets"
    / "twenty-companies-development-v1.json")


def _active_companies(state: dict[str, Any]) -> list[dict[str, str]]:
    normalized = validate_state(state, expected_role="baseline")
    return [{"id": row["id"], "name": row["name"], "domain": row["domain"]}
            for row in normalized["projection"]["companies"]
            if row["deleted_at"] is None]


def _replies(case: dict[str, Any], companies: list[dict[str, str]]) -> list[tuple]:
    """Script the exact product tool sequence a faithful Agent would choose."""
    task = case["evaluator_task"]
    if task["kind"] == "query":
        return [
            ("load_skill", {"skill_id": SKILL_ID}),
            ("query_companies", {"limit": 50}),
            (None, query_answer(companies)),
        ]
    target = next(
        (item for item in companies if item["id"] == task.get("company_id")), None)
    if target is None:
        raise ValueError("open qualification target is absent from the baseline")
    return [
        ("load_skill", {"skill_id": SKILL_ID}),
        ("query_companies", {"name_contains": target["name"], "limit": 20}),
        ("goto", {"url": f"/object/company/{target['id']}"}),
        # The packaged Skill allows one bounded wait for SPA loading before reporting.
        ("wait_for", {"ms": 2_000}),
        ("finish", {"done": True, "why": "The requested company record is open."}),
    ]


def qualify(*, fixture: TwentyPostgresFixture, adapter: TwentyApplicationAdapter,
            storage_state_path: Path, page_controller_module: str,
            development_taskset: dict[str, Any], ledger_path: Path,
            entry_path: str = "/objects/companies",
            task_ids: tuple[str, str] = ("dev-query-en-1", "dev-open-en-1"),
            browser_executable: Path | None = None,
            headless: bool = True) -> dict[str, Any]:
    """Run fixed query/open scripts through the real browser, API, evaluator and reset guard."""
    taskset = validate_task_set(development_taskset)
    if taskset["split"] != "development":
        raise ValueError("runner qualification requires the public development task set")
    if len(task_ids) != 2 or len(set(task_ids)) != 2:
        raise ValueError("runner qualification requires distinct query and open tasks")
    initial = fixture.capture(role="baseline")
    companies = _active_companies(initial)
    if not companies:
        raise ValueError("runner qualification fixture has no active companies")
    factory = playwright_runtime_factory(
        storage_state_path=storage_state_path,
        page_controller_module=page_controller_module,
        browser_executable=browser_executable, headless=headless)
    case_checks: dict[str, dict[str, Any]] = {}
    run_digests: list[str] = []
    evaluation_digests: list[str] = []
    for position, task_id in enumerate(task_ids, start=1):
        before = fixture.capture(role="baseline")
        if before.get("digest") != initial.get("digest"):
            raise ValueError("read-only fixture reset guard does not match the baseline")
        case = instantiate_task(taskset, task_id, before)
        run = run_case(
            case=case, adapter=adapter,
            client=ScriptedWoaChat(_replies(case, companies)),
            config=WoaTwentyConfig(
                storage_state_path=storage_state_path, entry_path=entry_path,
                step_budget=7, max_page_actions=4, wall_time_s=120),
            ledger_path=ledger_path, runtime_factory=factory,
            page_sampler=sample_visible_page, run_id=f"woa-live-qualification-{position}")
        after = fixture.capture(role="observation")
        evaluation = evaluate(
            task=case["evaluator_task"], before=before, after=after,
            evidence=run["evidence"])
        accounting = run.get("trial", {}).get("accounting")
        case_checks[case["evaluator_task"]["kind"]] = {
            "runner_status": run.get("status"),
            "runner_error_code": run.get("error_code"),
            "apparatus_code": run.get("apparatus_code"),
            "terminal_reason": (run.get("turn") or {}).get("terminal_reason"),
            "runner_completed": run.get("status") == "completed",
            "evaluator_passed": evaluation.get("status") == "PASS",
            "state_unchanged": before.get("digest") == after.get("digest"),
            "identity_unchanged": run.get("identity", {}).get(
                "session_binding_unchanged") is True,
            "accounting_complete": bool(
                isinstance(accounting, dict)
                and accounting.get("usage_unavailable_calls") == 0
                and accounting.get("total_cost_cny") == 0),
        }
        run_digests.append(run["run_digest"])
        evaluation_digests.append(digest_json(evaluation))
    checks = {
        "fixture_reset_guard": all(
            item["state_unchanged"] for item in case_checks.values()),
        "query_runner_evaluator": all(
            case_checks.get("query", {}).get(field) is True for field in _CASE_GATE_FIELDS),
        "open_runner_evaluator": all(
            case_checks.get("open", {}).get(field) is True for field in _CASE_GATE_FIELDS),
        "scripted_provider_zero_cost": all(
            item["accounting_complete"] for item in case_checks.values()),
        "schedule_completed": set(case_checks) == {"query", "open"},
    }
    return {
        "artifact_version": QUALIFICATION_VERSION,
        "kind": "woa_twenty_live_qualification",
        "status": "READY" if all(checks.values()) else "FAIL",
        "application_release": "twenty@2.39.0",
        "engine_release": "web-operation-agent@" + __import__(
            "web_operation_agent").__version__,
        "fixture_baseline_digest": initial["digest"],
        "task_set_digest": digest_json(taskset),
        "case_count": len(case_checks),
        "checks": checks,
        "case_checks": case_checks,
        "run_digests": run_digests,
        "evaluation_digests": evaluation_digests,
        "qualified_at": utc_now(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Qualify the Web Operation Agent runner on an isolated real Twenty fixture")
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
        browser_executable=(
            Path(args.browser_executable) if args.browser_executable else None),
        headless=not args.headed)
    atomic_write_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
