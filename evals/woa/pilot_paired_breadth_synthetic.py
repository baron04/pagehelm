"""Paired development pilot of both engines on the breadth development set, synthetic data.

The Companies v1 paired pilot saturated: both engines passed every task, so its discordance
carried no information for power planning. This pilot pairs the same two engines on the v2
breadth development templates (people, opportunities, filters, collection navigation and
refusals) with the shared Skill/API contract, on randomly generated records that never came
from a user or a Twenty instance.

It is a development diagnostic outside the frozen protocol: the breadth catalog is not a
sealed held-out set and the formal protocol validator still knows Companies v1 only. The
summary therefore reports paired concordance per task kind and overall, without direction,
and never a success rate, a benchmark or a comparison result.

Browser-only arms are not paired here. The synthetic breadth apparatus exposes no browser
surface with record links for the simulated runtime, so only the Skill/API comparison runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evals.pageagent import twenty_runner as pageagent_runner
from evals.pageagent.scripted import (
    SYNTHETIC_FIXTURE, ScriptedProviderClient, SyntheticBreadthPage, SyntheticTwentyAdapter,
    write_storage_state,
)
from evals.pageagent.twenty_runner import PageAgentTwentyConfig
from evals.twenty.breadth import evaluate_breadth, make_breadth_state
from evals.twenty.breadth_taskset import instantiate_breadth_task, validate_breadth_task_set
from evals.twenty.evaluator import digest_json
from evals.twenty.fixture import atomic_write_json, utc_now
from evals.woa import twenty_runner as woa_runner
from evals.woa.breadth_scripted import (
    SyntheticBreadthExecutor, SyntheticBreadthPageModel, breadth_projection,
    synthetic_breadth_records, synthetic_breadth_runtime,
)
from evals.woa.pilot_breadth import _MODEL_KEYS, _REQUIRED_MODEL_KEYS, _environment
from evals.woa.pilot_paired_synthetic import DEFAULT_BUNDLE, SCRIPTED_MODEL, PilotConfigurationError
from evals.woa.qualify_schedule import DEVELOPMENT_TASKSET as _COMPANIES_TASKSET
from evals.woa.scripted import ScriptedWoaChat, SimulatedRuntime
from evals.woa.twenty_runner import WoaTwentyConfig
from web_operation_agent.model import ChatModel, SpendMeter


PILOT_VERSION = 1
APPARATUS = "random_synthetic_crm_no_user_data"
COMPARISON_ID = "h2-skill"
ARMS = ("pageagent-skill", "woa-skill")
BREADTH_TASKSET = _COMPANIES_TASKSET.with_name("twenty-breadth-development-v2.json")
#: Task kinds this pilot pairs. Company creation is excluded: its approval boundary differs
#: between the engines by design and its evaluator is the frozen v1 mutation evaluator.
PAIRED_KINDS = ("enumerate", "filter", "open_record", "open_collection", "refuse")
_SKILLS = {"companies": "synthetic.companies-query", "people": "synthetic.people-query",
           "opportunities": "synthetic.opportunities-query"}
_TOOLS = {"companies": "query_companies", "people": "query_people",
          "opportunities": "query_opportunities"}
_SINGULAR = {"companies": "company", "people": "person", "opportunities": "opportunity"}


def scripted_woa_replies(case: Mapping[str, Any],
                         records: Mapping[str, list[dict[str, str]]]) -> list[tuple[str | None, Any]]:
    task = case["evaluator_task"]
    kind, entity = task["kind"], task["entity"]
    skill, tool = _SKILLS[entity], _TOOLS[entity]
    if kind == "enumerate":
        return [("load_skill", {"skill_id": skill}), (tool, {"limit": 50}),
                (None, "Here are the current records.")]
    if kind == "filter":
        return [("load_skill", {"skill_id": skill}),
                (tool, {"name_contains": task["name_contains"], "limit": 20}),
                (None, "Here are the matching records.")]
    if kind == "open_record":
        target = next(row for row in records[entity] if row["id"] == task["record_id"])
        return [("load_skill", {"skill_id": skill}),
                (tool, {"name_contains": target["name"], "limit": 20}),
                ("goto", {"url": f"/object/{_SINGULAR[entity]}/{target['id']}"}),
                ("finish", {"done": True, "why": "The record is visible."})]
    if kind == "open_collection":
        return [("load_skill", {"skill_id": skill}), (tool, {"limit": 50}),
                ("goto", {"url": f"/objects/{entity}"}),
                ("finish", {"done": True, "why": "The collection is open."})]
    return [("finish", {"done": False,
                        "why": "That operation is not supported in this assistant."})]


def scripted_pageagent_actions(case: Mapping[str, Any],
                               records: Mapping[str, list[dict[str, str]]]) -> list[dict[str, Any]]:
    task = case["evaluator_task"]
    kind, entity = task["kind"], task["entity"]
    tool = _TOOLS[entity]
    if kind == "enumerate":
        return [{tool: {"limit": 50}},
                {"done": {"text": "Here are the current records.", "success": True}}]
    if kind == "filter":
        return [{tool: {"name_contains": task["name_contains"], "limit": 20}},
                {"done": {"text": "Here are the matching records.", "success": True}}]
    if kind == "open_record":
        target = next(row for row in records[entity] if row["id"] == task["record_id"])
        return [{tool: {"name_contains": target["name"], "limit": 20}},
                {"open_allowed_route": {"route": f"/object/{_SINGULAR[entity]}/{target['id']}"}},
                {"done": {"text": "Opened the record.", "success": True}}]
    if kind == "open_collection":
        return [{tool: {"limit": 50}},
                {"open_allowed_route": {"route": f"/objects/{entity}"}},
                {"done": {"text": "Opened the collection.", "success": True}}]
    return [{"done": {"text": "That operation is not supported in this assistant.",
                      "success": False}}]


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    selected = sorted(float(value) for value in values
                      if isinstance(value, (int, float)) and not isinstance(value, bool)
                      and math.isfinite(float(value)))
    if not selected:
        return {"count": 0, "median": None, "p90": None, "p95": None}

    def nearest_rank(percentile: float) -> float:
        return selected[max(0, math.ceil(percentile * len(selected)) - 1)]

    return {"count": len(selected), "median": statistics.median(selected),
            "p90": nearest_rank(.90), "p95": nearest_rank(.95)}


def summarize_pairs(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Paired concordance overall and per task kind, with no direction and no rate of success."""
    def tally(selected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        both_valid = [pair for pair in selected
                      if all(arm["evaluation_status"] in {"PASS", "FAIL"}
                             for arm in pair["arms"].values())]
        discordant = sum(
            len({arm["evaluation_status"] for arm in pair["arms"].values()}) == 2
            for pair in both_valid)
        return {
            "pairs_completed": len(selected),
            "pairs_both_valid": len(both_valid),
            "concordant_pairs": len(both_valid) - discordant,
            "discordant_pairs": discordant,
            "discordance_rate": (round(discordant / len(both_valid), 6) if both_valid else None),
        }

    by_kind = {}
    for kind in PAIRED_KINDS:
        selected = [pair for pair in pairs if pair["task_kind"] == kind]
        if selected:
            by_kind[kind] = tally(selected)
    return {"overall": tally(pairs), "by_kind": by_kind}


class PairedBreadthPilot:
    """Own the shared synthetic breadth state, both engines and the pair loop."""

    def __init__(self, *, output_dir: Path, bundle_path: Path, provider: str,
                 model_env: Path | None, max_cost_cny: float, record_count: int = 5,
                 browser_executable: Path | None = None, wall_time_s: int = 180,
                 kinds: Sequence[str] = PAIRED_KINDS):
        if provider not in {"scripted", "real"}:
            raise PilotConfigurationError("provider must be scripted or real")
        if not 0 < max_cost_cny <= 100:
            raise PilotConfigurationError("max-cost-cny must be from 0 to 100")
        if not bundle_path.is_file():
            raise PilotConfigurationError(f"PageAgent bundle is not built: {bundle_path}")
        if not set(kinds) <= set(PAIRED_KINDS) or not kinds:
            raise PilotConfigurationError("kinds must be a non-empty subset of the paired kinds")
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.bundle_path = bundle_path
        self.bundle_digest = "sha256:" + hashlib.sha256(bundle_path.read_bytes()).hexdigest()
        self.provider = provider
        self.max_cost_cny = max_cost_cny
        self.browser_executable = browser_executable
        self.wall_time_s = wall_time_s
        self.kinds = tuple(kinds)
        self.spend_path = output_dir / "spend.jsonl"
        self.audit_path = output_dir / "audit.jsonl"
        self.client = self._client(model_env)
        self.model = self.client.model if provider == "real" else SCRIPTED_MODEL
        self.task_set = validate_breadth_task_set(
            json.loads(BREADTH_TASKSET.read_text(encoding="utf-8")))
        self.records = synthetic_breadth_records(record_count)
        self.executor = SyntheticBreadthExecutor(self.records)
        self.page_model = SyntheticBreadthPageModel(self.records)
        self.page = SyntheticBreadthPage(self.records)
        self.adapter: SyntheticTwentyAdapter | None = None
        self.state_path: Path | None = None
        self.storage_state: dict[str, Any] | None = None
        self.runtimes: list[SimulatedRuntime] = []
        self._directory: tempfile.TemporaryDirectory | None = None

    def _client(self, model_env: Path | None):
        if self.provider == "scripted":
            return None
        env = _environment((model_env or Path(".env")).expanduser(), os.environ)
        missing = [name for name in _REQUIRED_MODEL_KEYS if not env.get(name)]
        if missing:
            raise PilotConfigurationError(
                "paid pilot model configuration is incomplete: " + ", ".join(sorted(missing)))
        meter = SpendMeter(ledger=self.spend_path, batch_id="twenty-paired-breadth-pilot",
                           batch_cost_stop=self.max_cost_cny)
        return ChatModel(
            meter, env={key: env[key] for key in _MODEL_KEYS if env.get(key)},
            model=env["AGENT_OPENAI_MODEL_PRO"], max_completion_tokens=2_048, timeout=180)

    def __enter__(self) -> "PairedBreadthPilot":
        self.page.__enter__()
        self._directory = tempfile.TemporaryDirectory()
        self.state_path = write_storage_state(
            Path(self._directory.name) / "state.json", origin=self.page.origin)
        self.storage_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.adapter = SyntheticTwentyAdapter(
            origin=self.page.origin, runtime=synthetic_breadth_runtime(self.executor))
        return self

    def __exit__(self, *_args: Any) -> None:
        for runtime in self.runtimes:
            runtime.stop()
        self.page.__exit__(None, None, None)
        if self._directory is not None:
            self._directory.cleanup()

    # -- engines -------------------------------------------------------------------------
    def capture(self, *, role: str) -> dict[str, Any]:
        return make_breadth_state(fixture=SYNTHETIC_FIXTURE,
                                  projection=breadth_projection(self.records),
                                  captured_at=utc_now(), role=role)

    def _runtime_factory(self):
        def factory(*, channel, grant, registry, origin, entry_path):
            runtime = SimulatedRuntime(
                channel, grant, origin=origin, entry_path=entry_path,
                page_model=self.page_model, storage_state=self.storage_state)
            self.runtimes.append(runtime)
            return runtime
        return factory

    def run_woa(self, case: dict[str, Any], run_id: str) -> dict[str, Any]:
        client = (self.client if self.provider == "real"
                  else ScriptedWoaChat(scripted_woa_replies(case, self.records)))
        return woa_runner.run_breadth_case(
            case=case, adapter=self.adapter, client=client,
            config=WoaTwentyConfig(
                storage_state_path=self.state_path, step_budget=10, max_page_actions=6,
                wall_time_s=self.wall_time_s, transport_timeout_s=90),
            ledger_path=self.audit_path, runtime_factory=self._runtime_factory(),
            page_sampler=lambda runtime: self.page_model.render(runtime.current_path)[1],
            run_id=run_id)

    def run_pageagent(self, case: dict[str, Any], run_id: str) -> dict[str, Any]:
        client = (self.client if self.provider == "real"
                  else ScriptedProviderClient(scripted_pageagent_actions(case, self.records)))
        return pageagent_runner.run_breadth_case(
            case=case, adapter=self.adapter, client=client,
            config=PageAgentTwentyConfig(
                bundle_path=self.bundle_path, bundle_digest=self.bundle_digest,
                storage_state_path=self.state_path, wall_time_s=self.wall_time_s,
                capability_profile="skill_api", max_page_actions=6,
                browser_executable=self.browser_executable),
            run_id=run_id)

    def _arm_row(self, run: dict[str, Any], evaluation: dict[str, Any],
                 before: dict[str, Any], after: dict[str, Any], run_id: str) -> dict[str, Any]:
        trial = run.get("trial") or {}
        accounting = trial.get("accounting") or {}
        return {
            "run_id": run_id,
            "runner_status": run.get("status"),
            "runner_error_code": run.get("error_code"),
            "apparatus_code": run.get("apparatus_code"),
            "evaluation_status": evaluation.get("status"),
            "failed_checks": sorted(key for key, value in evaluation.get("checks", {}).items()
                                    if value is False),
            "invalid_reason": evaluation.get("reason"),
            "state_unchanged": before["digest"] == after["digest"],
            "wall_ms": run.get("wall_ms"),
            "page_actions": trial.get("page_actions"),
            "business_calls": trial.get("business_calls"),
            "model_calls": int(accounting.get("model_calls", 0) or 0),
            "usage_unavailable_calls": int(accounting.get("usage_unavailable_calls", 0) or 0),
            "known_cost_cny": float(accounting.get("known_cost_cny", 0.0) or 0.0),
            "run_digest": run.get("run_digest"),
            "evaluation_digest": digest_json(evaluation),
        }

    def batch_cost(self) -> float:
        return self.client.meter.batch_cost() if self.client is not None else 0.0

    def run(self, *, max_pairs: int | None = None) -> dict[str, Any]:
        engines: dict[str, Callable[[dict[str, Any], str], dict[str, Any]]] = {
            "pageagent-skill": self.run_pageagent, "woa-skill": self.run_woa}
        tasks = [task for task in self.task_set["tasks"] if task["kind"] in self.kinds]
        pairs: list[dict[str, Any]] = []
        stopped_reason: str | None = None
        initial = self.capture(role="baseline")
        for position, task in enumerate(tasks, start=1):
            if max_pairs is not None and len(pairs) >= max_pairs:
                stopped_reason = "pair_limit"
                break
            if self.batch_cost() > self.max_cost_cny:
                stopped_reason = "cost_ceiling"
                break
            # Alternate which engine goes first so order effects do not align with one arm.
            order = ARMS if position % 2 else tuple(reversed(ARMS))
            arms: dict[str, dict[str, Any]] = {}
            for arm_order, arm_id in enumerate(order, start=1):
                before = self.capture(role="baseline")
                if before["digest"] != initial["digest"]:
                    stopped_reason = "fixture_drift"
                    break
                case = instantiate_breadth_task(self.task_set, task["task_id"], before)
                run_id = f"paired-breadth-{position:03d}-{arm_order}"
                run = engines[arm_id](case, run_id)
                after = self.capture(role="observation")
                evaluation = evaluate_breadth(
                    task=case["evaluator_task"], before=before, after=after,
                    evidence=run.get("evidence"))
                arms[arm_id] = self._arm_row(run, evaluation, before, after, run_id)
                if run.get("status") == "apparatus_invalid":
                    stopped_reason = "apparatus_invalid"
                    break
                if not arms[arm_id]["state_unchanged"]:
                    stopped_reason = "safety_guard"
                    break
            pairs.append({
                "position": position, "task_id": task["task_id"],
                "template_family": task["template_family"], "task_kind": task["kind"],
                "entity": task["entity"], "first_arm": order[0], "arms": arms,
            })
            if stopped_reason is not None:
                break
        concordance = summarize_pairs(pairs)
        arm_rows: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ARMS}
        for pair in pairs:
            for arm_id, row in pair["arms"].items():
                arm_rows[arm_id].append(row)
        cost = round(sum(row["known_cost_cny"] for rows in arm_rows.values() for row in rows), 6)
        unknown = sum(row["usage_unavailable_calls"] for rows in arm_rows.values() for row in rows)
        summary = {
            "artifact_version": PILOT_VERSION,
            "kind": "twenty_paired_breadth_pilot",
            "purpose": "development_diagnostic_not_benchmark",
            "apparatus": APPARATUS,
            "provider": self.provider,
            "model": self.model,
            "comparison_id": COMPARISON_ID,
            "approval_boundary": "capability_only",
            "arms": list(ARMS),
            "bundle_digest": self.bundle_digest,
            "task_set_digest": digest_json(self.task_set),
            "fixture_baseline_digest": initial["digest"],
            "paired_kinds": list(self.kinds),
            "scheduled_pairs": len(tasks),
            "completed_pairs": len(pairs),
            "status": ("COMPLETE" if len(pairs) == len(tasks) and stopped_reason is None
                       else "STOPPED"),
            "stopped_reason": stopped_reason,
            "concordance": concordance,
            "arm_summary": {
                arm_id: {
                    "runs": len(rows),
                    "evaluation_counts": {name: sum(row["evaluation_status"] == name for row in rows)
                                          for name in ("PASS", "FAIL", "INVALID")},
                    "runner_error_codes": sorted({row["runner_error_code"] for row in rows
                                                  if row["runner_error_code"]}),
                    "wall_ms": _distribution([row["wall_ms"] for row in rows]),
                    "model_calls": _distribution([row["model_calls"] for row in rows]),
                    "page_actions": _distribution([row["page_actions"] for row in rows]),
                    "business_calls": _distribution([row["business_calls"] for row in rows]),
                    "known_cost_cny": _distribution([row["known_cost_cny"] for row in rows]),
                }
                for arm_id, rows in arm_rows.items()
            },
            "known_cost_cny": cost,
            "total_cost_cny": None if unknown else cost,
            "unknown_cost_calls": unknown,
            "cost_ceiling_cny": self.max_cost_cny,
            "pairs": pairs,
            "interpretation": (
                "Development diagnostic on random synthetic records, outside the frozen "
                "protocol. Concordance counts how often the two arms agreed on the same task "
                "and carries no direction. Each template appears once, so pairs are one "
                "observation per template; refusal pairs compare a product scope gate with a "
                "model's voluntary refusal and are reported separately. Not a success rate, a "
                "benchmark or a comparison result."),
            "finished_at": utc_now(),
        }
        summary["summary_digest"] = digest_json(summary)
        atomic_write_json(self.output_dir / "summary.json", summary)
        return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the paired breadth development pilot of both engines on synthetic data")
    parser.add_argument("--provider", choices=("scripted", "real"), default="scripted")
    parser.add_argument("--model-env", default=os.environ.get("WOA_MODEL_ENV", ".env"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--bundle", default=os.environ.get("WOA_PAGEAGENT_BUNDLE")
                        or str(DEFAULT_BUNDLE))
    parser.add_argument("--browser-executable")
    parser.add_argument("--max-cost-cny", type=float, default=8.0)
    parser.add_argument("--max-pairs", type=int)
    parser.add_argument("--kinds", default=",".join(PAIRED_KINDS))
    parser.add_argument("--wall-time-s", type=int, default=180)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with PairedBreadthPilot(
            output_dir=Path(args.output_dir), bundle_path=Path(args.bundle),
            provider=args.provider, model_env=Path(args.model_env) if args.model_env else None,
            max_cost_cny=args.max_cost_cny, wall_time_s=args.wall_time_s,
            kinds=tuple(kind for kind in args.kinds.split(",") if kind),
            browser_executable=(
                Path(args.browser_executable) if args.browser_executable else None),
    ) as pilot:
        summary = pilot.run(max_pairs=args.max_pairs)
    print(json.dumps({
        key: summary[key] for key in (
            "provider", "model", "status", "completed_pairs", "scheduled_pairs",
            "stopped_reason", "known_cost_cny", "concordance", "arm_summary")
    }, ensure_ascii=False, sort_keys=True, default=str)[:4000])
    return 0 if summary["status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
