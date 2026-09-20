"""Run a bounded paid-model breadth diagnostic against an isolated local Twenty fixture.

This is a development pilot, not a benchmark. It uses a fixed stratified subset, stops on
apparatus invalidity or the spend ceiling, and stores no prompt, answer, page text, or fixture
rows in its summary artifact.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from evals.twenty.breadth import evaluate_breadth
from evals.twenty.breadth_taskset import (instantiate_breadth_task,
                                          validate_breadth_task_set)
from evals.twenty.evaluator import digest_json
from evals.twenty.fixture import TwentyPostgresFixture, atomic_write_json, utc_now
from evals.woa.browser_host import playwright_runtime_factory, sample_visible_page
from evals.woa.twenty_runner import WoaTwentyConfig, run_breadth_case
from web_operation_agent.modeling.client import ChatModel, SpendMeter, load_env
from web_operation_agent_twenty.adapter import TwentyApplicationAdapter


PILOT_VERSION = 1
TASK_IDS = (
    "dev-enumerate-people-en", "dev-enumerate-people-zh",
    "dev-enumerate-opportunities-en", "dev-enumerate-opportunities-zh",
    "dev-filter-people-en", "dev-filter-opportunities-zh",
    "dev-open-people-en", "dev-open-opportunities-zh",
    "dev-module-people-en", "dev-module-opportunities-zh",
    "dev-refuse-person-delete-en", "dev-refuse-opportunity-delete-zh",
)
DEVELOPMENT_TASKSET = (
    Path(__file__).resolve().parents[1] / "twenty" / "tasksets"
    / "twenty-breadth-development-v2.json")
_MODEL_KEYS = {"AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_API_KEY",
               "AGENT_OPENAI_MODEL", "AGENT_PROVIDER"}
_REQUIRED_MODEL_KEYS = {"AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_API_KEY",
                        "AGENT_OPENAI_MODEL"}
_LIVE_KEYS = {
    "base_url": "WOA_TWENTY_BASE_URL",
    "storage_state": "WOA_TWENTY_STORAGE_STATE",
    "oauth_token": "WOA_TWENTY_OAUTH_TOKEN",
    "oauth_registration": "WOA_TWENTY_OAUTH_REGISTRATION",
    "page_controller_module": "WOA_TWENTY_PAGE_CONTROLLER_MODULE",
    "container": "WOA_TWENTY_DB_CONTAINER",
    "database": "WOA_TWENTY_DB_NAME",
    "db_user": "WOA_TWENTY_DB_USER",
    "workspace_schema": "WOA_TWENTY_WORKSPACE_SCHEMA",
}


def _environment(path: Path, process: Mapping[str, str]) -> dict[str, str]:
    loaded = load_env(path)
    result = {key: value for key, value in loaded.items()
              if key in _MODEL_KEYS or key in _LIVE_KEYS.values()}
    result.update({key: process[key] for key in result.keys() | _MODEL_KEYS | set(_LIVE_KEYS.values())
                   if process.get(key)})
    return result


def _summary(*, taskset: dict[str, Any], initial: dict[str, Any], model: str,
             max_cost_cny: float, cases: list[dict[str, Any]],
             stopped_reason: str | None) -> dict[str, Any]:
    def distribution(field: str) -> dict[str, float | int | None]:
        values = sorted(float(item[field]) for item in cases
                        if isinstance(item.get(field), (int, float))
                        and not isinstance(item.get(field), bool)
                        and math.isfinite(float(item[field])))
        if not values:
            return {"count": 0, "median": None, "p90": None, "p95": None}

        def nearest_rank(percentile: float) -> float:
            return values[max(0, math.ceil(percentile * len(values)) - 1)]

        return {"count": len(values), "median": statistics.median(values),
                "p90": nearest_rank(.90), "p95": nearest_rank(.95)}

    completed = len(cases) == len(TASK_IDS) and stopped_reason is None
    evaluation_counts = {name: sum(item["evaluation_status"] == name for item in cases)
                         for name in ("PASS", "FAIL", "INVALID")}
    unknown_cost_calls = sum(item["usage_unavailable_calls"] for item in cases)
    known_cost = round(sum(item["known_cost_cny"] for item in cases), 6)
    safety_keys = ("unauthorized_writes", "duplicate_writes",
                   "forbidden_state_changes", "cross_identity_actions")
    safety_totals = {key: sum(int(item.get("safety", {}).get(key, 0)) for item in cases)
                     for key in safety_keys}
    refusal_cases = [item for item in cases if item.get("task_kind") == "refuse"]
    return {
        "artifact_version": PILOT_VERSION, "kind": "woa_twenty_breadth_paid_pilot",
        "status": "COMPLETE" if completed else "STOPPED",
        "purpose": "development_diagnostic_not_benchmark",
        "model": model, "task_set_digest": digest_json(taskset),
        "fixture_baseline_digest": initial["digest"],
        "scheduled_case_count": len(TASK_IDS), "completed_case_count": len(cases),
        "evaluation_counts": evaluation_counts,
        "apparatus_invalid_count": sum(
            item["runner_status"] == "apparatus_invalid" for item in cases),
        "known_cost_cny": known_cost,
        "total_cost_cny": None if unknown_cost_calls else known_cost,
        "unknown_cost_calls": unknown_cost_calls,
        "metrics": {
            field: distribution(field) for field in (
                "wall_ms", "steps_used", "model_calls", "page_actions",
                "business_calls", "known_cost_cny")
        },
        "safety_totals": safety_totals,
        "refusal_counts": {
            "scheduled": len(refusal_cases),
            "passed": sum(item.get("evaluation_status") == "PASS"
                          for item in refusal_cases),
        },
        "cost_ceiling_cny": max_cost_cny, "stopped_reason": stopped_reason,
        "cases": cases, "finished_at": utc_now(),
    }


def run_pilot(*, fixture: TwentyPostgresFixture, adapter: TwentyApplicationAdapter,
              client: ChatModel, taskset: dict[str, Any], storage_state_path: Path,
              page_controller_module: str | None, ledger_path: Path,
              max_cost_cny: float, entry_path: str = "/objects/companies",
              browser_executable: Path | None = None,
              headless: bool = True, runtime_factory: Any = None,
              page_sampler: Any = sample_visible_page) -> dict[str, Any]:
    selected_set = validate_breadth_task_set(taskset)
    if selected_set["split"] != "development":
        raise ValueError("paid pilot requires the public development task set")
    initial = fixture.capture_breadth(role="baseline")
    if runtime_factory is None:
        if not isinstance(page_controller_module, str) or not page_controller_module:
            raise ValueError("real-browser pilot requires a page controller module")
        factory = playwright_runtime_factory(
            storage_state_path=storage_state_path,
            page_controller_module=page_controller_module,
            browser_executable=browser_executable, headless=headless)
    else:
        factory = runtime_factory
    cases: list[dict[str, Any]] = []
    stopped_reason: str | None = None
    for position, task_id in enumerate(TASK_IDS, start=1):
        before = fixture.capture_breadth(role="baseline")
        if before["digest"] != initial["digest"]:
            stopped_reason = "fixture_drift"
            break
        case = instantiate_breadth_task(selected_set, task_id, before)
        run = run_breadth_case(
            case=case, adapter=adapter, client=client,
            config=WoaTwentyConfig(
                storage_state_path=storage_state_path, entry_path=entry_path,
                step_budget=10, max_page_actions=6, wall_time_s=180,
                transport_timeout_s=90),
            ledger_path=ledger_path, runtime_factory=factory,
            page_sampler=page_sampler,
            run_id=f"woa-breadth-pilot-{position:02d}")
        after = fixture.capture_breadth(role="observation")
        evaluation = evaluate_breadth(
            task=case["evaluator_task"], before=before, after=after,
            evidence=run["evidence"])
        trial = run.get("trial", {})
        accounting = trial.get("accounting") or {}
        safety = trial.get("safety") or {}
        cases.append({
            "position": position, "task_id": task_id,
            "task_kind": case["evaluator_task"]["kind"],
            "entity": case["evaluator_task"].get("entity"),
            "runner_status": run.get("status"),
            "runner_error_code": run.get("error_code"),
            "apparatus_code": run.get("apparatus_code"),
            "terminal_reason": (run.get("turn") or {}).get("terminal_reason"),
            "steps_used": (run.get("turn") or {}).get("steps_used"),
            "page_actions": run.get("trial", {}).get("page_actions"),
            "business_calls": run.get("trial", {}).get("business_calls"),
            "policy_blocks": (run.get("turn") or {}).get("policy_blocks"),
            "wall_ms": run.get("wall_ms"),
            "approvals_requested": trial.get("approvals_requested"),
            "approvals_approved": trial.get("approvals_approved"),
            "approvals_denied": trial.get("approvals_denied"),
            "automatic_retries": trial.get("automatic_retries"),
            "user_interventions": trial.get("user_interventions"),
            "safety": {
                key: int(safety.get(key, 0)) for key in (
                    "unauthorized_writes", "duplicate_writes",
                    "forbidden_state_changes", "cross_identity_actions")
            },
            "evaluation_status": evaluation.get("status"),
            "failed_checks": sorted(
                key for key, value in evaluation.get("checks", {}).items() if value is False),
            "state_unchanged": before["digest"] == after["digest"],
            "identity_unchanged": run.get("identity", {}).get(
                "session_binding_unchanged") is True,
            "model_calls": int(accounting.get("model_calls", 0)),
            "usage_unavailable_calls": int(accounting.get("usage_unavailable_calls", 0)),
            "known_input_tokens": int(accounting.get("known_input_tokens", 0)),
            "known_output_tokens": int(accounting.get("known_output_tokens", 0)),
            "known_cost_cny": float(accounting.get("known_cost_cny", 0.0)),
            "run_digest": run.get("run_digest"),
            "evaluation_digest": digest_json(evaluation),
        })
        if run.get("status") == "apparatus_invalid":
            stopped_reason = "apparatus_invalid"
            break
        if any(not cases[-1][field] for field in ("state_unchanged", "identity_unchanged")):
            stopped_reason = "safety_guard"
            break
        if client.meter.batch_cost() > max_cost_cny:
            stopped_reason = "cost_ceiling"
            break
    return _summary(
        taskset=selected_set, initial=initial, model=client.model,
        max_cost_cny=max_cost_cny, cases=cases, stopped_reason=stopped_reason)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run bounded Twenty breadth paid pilot")
    parser.add_argument("--model-env", default=os.environ.get("WOA_MODEL_ENV", ".env"))
    parser.add_argument("--taskset", default=str(DEVELOPMENT_TASKSET))
    parser.add_argument("--output", required=True)
    parser.add_argument("--spend-ledger", required=True)
    parser.add_argument("--audit-ledger", required=True)
    parser.add_argument("--max-cost-cny", type=float, default=3.0)
    parser.add_argument("--browser-executable")
    parser.add_argument("--headed", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 < args.max_cost_cny <= 100:
        raise ValueError("max-cost-cny must be from 0 to 100")
    env = _environment(Path(args.model_env).expanduser(), os.environ)
    missing = [name for name in (*_REQUIRED_MODEL_KEYS, *_LIVE_KEYS.values())
               if not env.get(name)]
    if missing:
        raise RuntimeError("paid pilot configuration is incomplete: " + ", ".join(sorted(missing)))
    meter = SpendMeter(
        ledger=Path(args.spend_ledger), batch_id="twenty-breadth-development-pilot",
        batch_cost_stop=args.max_cost_cny)
    client = ChatModel(
        meter, env={key: env[key] for key in _MODEL_KEYS},
        model=env["AGENT_OPENAI_MODEL"], max_completion_tokens=2_048, timeout=180)
    result = run_pilot(
        fixture=TwentyPostgresFixture(
            container=env[_LIVE_KEYS["container"]],
            database=env[_LIVE_KEYS["database"]], db_user=env[_LIVE_KEYS["db_user"]],
            workspace_schema=env[_LIVE_KEYS["workspace_schema"]]),
        adapter=TwentyApplicationAdapter(
            base_url=env[_LIVE_KEYS["base_url"]],
            token_path=env[_LIVE_KEYS["oauth_token"]],
            registration_path=env[_LIVE_KEYS["oauth_registration"]]),
        client=client,
        taskset=json.loads(Path(args.taskset).read_text(encoding="utf-8")),
        storage_state_path=Path(env[_LIVE_KEYS["storage_state"]]),
        page_controller_module=env[_LIVE_KEYS["page_controller_module"]],
        ledger_path=Path(args.audit_ledger), max_cost_cny=args.max_cost_cny,
        browser_executable=(Path(args.browser_executable) if args.browser_executable else None),
        headless=not args.headed)
    atomic_write_json(Path(args.output), result)
    print(json.dumps({
        "status": result["status"], "completed_case_count": result["completed_case_count"],
        "evaluation_counts": result["evaluation_counts"],
        "known_cost_cny": result["known_cost_cny"],
        "unknown_cost_calls": result["unknown_cost_calls"],
        "stopped_reason": result["stopped_reason"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
