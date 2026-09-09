"""Qualify the PageAgent read-only runner against an isolated real Twenty fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from evals.pageagent.scripted import ScriptedProviderClient, query_answer
from evals.pageagent.twenty_runner import PageAgentTwentyConfig, run_case
from evals.twenty.evaluator import digest_json, evaluate, validate_state
from evals.twenty.fixture import TwentyPostgresFixture, atomic_write_json, utc_now
from evals.twenty.taskset import instantiate_task, validate_task_set
from web_operation_agent.twenty import TwentyApplicationAdapter


QUALIFICATION_VERSION = 1
_CASE_GATE_FIELDS = (
    "runner_completed", "evaluator_passed", "state_unchanged",
    "identity_unchanged", "accounting_complete",
)


def _active_companies(state: dict[str, Any]) -> list[dict[str, str]]:
    normalized = validate_state(state, expected_role="baseline")
    return [{"id": row["id"], "name": row["name"], "domain": row["domain"]}
            for row in normalized["projection"]["companies"]
            if row["deleted_at"] is None]


def _actions(case: dict[str, Any], companies: list[dict[str, str]]) -> list[dict[str, Any]]:
    task = case["evaluator_task"]
    if task["kind"] == "query":
        return [
            {"query_companies": {"limit": 50}},
            {"done": {"text": query_answer(companies), "success": True}},
        ]
    target = next(
        (company for company in companies if company["id"] == task.get("company_id")), None)
    if target is None:
        raise ValueError("open qualification target is absent from the baseline")
    route = f"/object/company/{target['id']}"
    return [
        {"query_companies": {"name_contains": target["name"], "limit": 20}},
        {"open_allowed_route": {"route": route}},
        {"done": {"text": "Opened the requested company record.", "success": True}},
    ]


def qualify(*, fixture: TwentyPostgresFixture, adapter: TwentyApplicationAdapter,
            storage_state_path: Path, bundle_path: Path,
            development_taskset: dict[str, Any],
            task_ids: tuple[str, str] = ("dev-query-en-1", "dev-open-en-1"),
            browser_executable: Path | None = None) -> dict[str, Any]:
    """Run fixed query/open scripts through the real browser, API, evaluator, and reset guard."""
    taskset = validate_task_set(development_taskset)
    if taskset["split"] != "development":
        raise ValueError("runner qualification requires the public development task set")
    if len(task_ids) != 2 or len(set(task_ids)) != 2:
        raise ValueError("runner qualification requires distinct query and open tasks")
    bundle = bundle_path.read_bytes()
    bundle_digest = "sha256:" + hashlib.sha256(bundle).hexdigest()
    initial = fixture.capture(role="baseline")
    companies = _active_companies(initial)
    if not companies:
        raise ValueError("runner qualification fixture has no active companies")
    case_checks: dict[str, dict[str, Any]] = {}
    run_digests: list[str] = []
    evaluation_digests: list[str] = []
    for position, task_id in enumerate(task_ids, start=1):
        before = fixture.capture(role="baseline")
        reset_match = before.get("digest") == initial.get("digest")
        if not reset_match:
            raise ValueError("read-only fixture reset guard does not match the baseline")
        case = instantiate_task(taskset, task_id, before)
        actions = _actions(case, companies)
        run = run_case(
            case=case, adapter=adapter, client=ScriptedProviderClient(actions),
            config=PageAgentTwentyConfig(
                bundle_path=bundle_path, bundle_digest=bundle_digest,
                storage_state_path=storage_state_path,
                language="zh-CN" if "-zh-" in task_id else "en-US",
                max_steps=6, max_model_calls=6, max_page_actions=3,
                max_business_calls=2, wall_time_s=90,
                browser_executable=browser_executable),
            run_id=f"live-qualification-{position}")
        after = fixture.capture(role="observation")
        evaluation = evaluate(
            task=case["evaluator_task"], before=before, after=after,
            evidence=run["evidence"])
        unchanged = before.get("digest") == after.get("digest")
        case_checks[case["evaluator_task"]["kind"]] = {
            "runner_status": run.get("status"),
            "runner_error_code": run.get("error_code"),
            "apparatus_code": run.get("apparatus_code"),
            "runner_completed": run.get("status") == "completed",
            "evaluator_passed": evaluation.get("status") == "PASS",
            "state_unchanged": unchanged,
            "identity_unchanged": run.get("session_binding_unchanged") is True,
            "accounting_complete": bool(
                isinstance(run.get("trial"), dict)
                and isinstance(run["trial"].get("accounting"), dict)
                and run["trial"]["accounting"].get("usage_unavailable_calls") == 0
                and run["trial"]["accounting"].get("total_cost_cny") == 0),
        }
        run_digests.append(run["run_digest"])
        evaluation_digests.append(digest_json(evaluation))
    checks = {
        "fixture_reset_guard": all(
            item["state_unchanged"] for item in case_checks.values()),
        "query_runner_evaluator": all(
            case_checks.get("query", {}).get(field) is True
            for field in _CASE_GATE_FIELDS),
        "open_runner_evaluator": all(
            case_checks.get("open", {}).get(field) is True
            for field in _CASE_GATE_FIELDS),
        "scripted_provider_zero_cost": all(
            item["accounting_complete"] for item in case_checks.values()),
        "schedule_completed": set(case_checks) == {"query", "open"},
    }
    return {
        "artifact_version": QUALIFICATION_VERSION,
        "kind": "pageagent_twenty_live_qualification",
        "status": "READY" if all(checks.values()) else "FAIL",
        "application_release": "twenty@2.39.0",
        "bundle_digest": bundle_digest,
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
        description="Qualify PageAgent query/open on an isolated real Twenty fixture")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--storage-state", required=True)
    parser.add_argument("--oauth-token", required=True)
    parser.add_argument("--oauth-registration", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--db-user", required=True)
    parser.add_argument("--workspace-schema", required=True)
    parser.add_argument(
        "--taskset",
        default=str(Path(__file__).resolve().parents[1] / "twenty" / "tasksets"
                    / "twenty-companies-development-v1.json"))
    parser.add_argument("--browser-executable")
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
        bundle_path=Path(args.bundle),
        development_taskset=json.loads(Path(args.taskset).read_text(encoding="utf-8")),
        browser_executable=(
            Path(args.browser_executable) if args.browser_executable else None))
    atomic_write_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
