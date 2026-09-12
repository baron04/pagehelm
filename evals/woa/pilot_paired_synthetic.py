"""Paired development pilot of both engines on random synthetic CRM data.

The model-free schedule qualification proves the driver, evaluator, ledger and report with a
PageAgent artifact assembled from known facts. This pilot replaces that stand-in with the
real PageAgent bundle running in headless Chromium and the real Web Operation Agent turn
executor over the simulated runtime, both driven by one provider client, on companies that
are generated at random and never came from a user or a Twenty instance.

Its purpose is preregistered as ``development_pilot``. The frozen schedule pairs the same
task across two arms, so the public report's per-comparison discordance is the first observed
input for ``evals/twenty/power.py``. The development set holds six distinct prompts that the
opaque task set repeats, so the estimate is indicative, not an independent-template count.
Nothing here is a success rate, a benchmark or a comparison result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evals.pageagent import twenty_runner as pageagent_runner
from evals.pageagent.scripted import (
    ScriptedProviderClient, SyntheticCompanyExecutor, SyntheticTwentyAdapter,
    SyntheticTwentyPage, company_ui_path, query_answer, synthetic_business_runtime,
    synthetic_companies, write_storage_state,
)
from evals.pageagent.twenty_runner import PageAgentTwentyConfig
from evals.twenty.driver import ScheduleDriverError, run_schedule
from evals.twenty.evaluator import digest_json
from evals.twenty.fixture import atomic_write_json, utc_now
from evals.twenty.protocol import freeze_protocol
from evals.twenty.records import initialize_batch, verify_batch
from evals.twenty.report import build_report, verify_report
from evals.twenty.taskset import seal_task_set, verify_split_separation
from evals.woa import twenty_runner as woa_runner
from evals.woa.pilot_breadth import _MODEL_KEYS, _REQUIRED_MODEL_KEYS, _environment
from evals.woa.qualify_schedule import (
    DEVELOPMENT_TASKSET, InMemoryFixture, opaque_task_set, qualification_protocol,
)
from evals.woa.scripted import ScriptedWoaChat, SimulatedRuntime, SyntheticPageModel
from evals.woa.twenty_runner import WoaTwentyConfig
from web_operation_agent.model import ChatModel, SpendMeter


PILOT_VERSION = 1
PROTOCOL_ID = "twenty-paired-synthetic-pilot-v1"
#: Fixed for the development pilot only. A formal benchmark seed is preregistered separately.
PILOT_SEED = "00000000000000000000000000000005"
APPARATUS = "random_synthetic_crm_no_user_data"
DEFAULT_BUNDLE = (Path(__file__).resolve().parents[2] / ".artifacts" / "evals" / "pageagent"
                  / "pageagent-baseline.js")
SKILL_ID = "synthetic.companies-query"
CREATE_SKILL_ID = "synthetic.companies-create"
CREATED_ID = "99999999-9999-4999-8999-999999999999"
SCRIPTED_MODEL = "pageagent-scripted-model"


class PilotConfigurationError(RuntimeError):
    pass


def pilot_protocol(task_ids: Sequence[str], *, seal: Mapping[str, Any],
                   development_seal: Mapping[str, Any], split: Mapping[str, Any],
                   requested_model: str, seed: str = PILOT_SEED) -> dict[str, Any]:
    """Derive the paired pilot draft from the qualification protocol with a real provider."""
    draft = qualification_protocol(
        task_ids, seal=seal, development_seal=development_seal, split=split)
    draft["purpose"] = "development_pilot"
    draft["protocol_id"] = PROTOCOL_ID
    draft["randomization_seed"] = seed
    draft["provider"] = {
        **draft["provider"],
        "provider_id": "openai-compatible-env",
        "requested_model": requested_model,
    }
    return draft


def scripted_woa_replies(case: Mapping[str, Any], arm: Mapping[str, Any],
                         companies: list[dict[str, str]]) -> list[tuple[str | None, Any]]:
    task = case["evaluator_task"]
    kind = task["kind"]
    target = next((item for item in companies if item["id"] == task.get("company_id")), None)
    if arm["capability_profile"] == "browser_only":
        if kind == "query":
            return [("finish", {"done": True, "why": query_answer(companies)})]
        return [("goto", {"url": company_ui_path(target)}),
                ("finish", {"done": True, "why": "The requested record is open."})]
    if kind == "create":
        replies: list[tuple[str | None, Any]] = [
            ("load_skill", {"skill_id": CREATE_SKILL_ID}),
            ("create_company", {"name": task["name"], "domain": task["domain"]}),
        ]
        if task["require_open"]:
            replies.append(("goto", {"url": f"/object/company/{CREATED_ID}"}))
        replies.append(("finish", {"done": True, "why": "Created."}))
        return replies
    if kind == "query":
        return [("load_skill", {"skill_id": SKILL_ID}),
                ("query_companies", {"limit": 50}),
                (None, query_answer(companies))]
    return [("load_skill", {"skill_id": SKILL_ID}),
            ("query_companies", {"name_contains": target["name"], "limit": 20}),
            ("goto", {"url": company_ui_path(target)}),
            ("finish", {"done": True, "why": "The requested record is open."})]


def scripted_pageagent_actions(case: Mapping[str, Any], arm: Mapping[str, Any],
                               companies: list[dict[str, str]]) -> list[dict[str, Any]]:
    task = case["evaluator_task"]
    kind = task["kind"]
    browser_only = arm["capability_profile"] == "browser_only"
    target = next((item for item in companies if item["id"] == task.get("company_id")), None)
    if kind == "query":
        actions: list[dict[str, Any]] = []
        if not browser_only:
            actions.append({"query_companies": {"limit": 50}})
        actions.append({"done": {"text": query_answer(companies), "success": True}})
        return actions
    if kind == "open":
        route = company_ui_path(target)
        if browser_only:
            return [{"click_element_by_index": {"index": companies.index(target)}},
                    {"done": {"text": "Opened.", "success": True}}]
        return [{"query_companies": {"name_contains": target["name"], "limit": 20}},
                {"open_allowed_route": {"route": route}},
                {"done": {"text": "Opened the requested company record.", "success": True}}]
    actions = [{"create_company": {"name": task["name"], "domain": task["domain"]}}]
    if task["require_open"]:
        actions.append({"open_allowed_route": {"route": f"/object/company/{CREATED_ID}"}})
    actions.append({"done": {"text": "Created.", "success": True}})
    return actions


class PairedPilot:
    """Own the shared synthetic state, both engine runners and the batch ledger."""

    def __init__(self, *, output_dir: Path, bundle_path: Path, provider: str,
                 model_env: Path | None, max_cost_cny: float, company_count: int = 3,
                 browser_executable: Path | None = None, seed: str = PILOT_SEED,
                 wall_time_s: int = 240):
        if provider not in {"scripted", "real"}:
            raise PilotConfigurationError("provider must be scripted or real")
        if not 0 < max_cost_cny <= 100:
            raise PilotConfigurationError("max-cost-cny must be from 0 to 100")
        if not bundle_path.is_file():
            raise PilotConfigurationError(f"PageAgent bundle is not built: {bundle_path}")
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.bundle_path = bundle_path
        self.bundle_digest = "sha256:" + hashlib.sha256(bundle_path.read_bytes()).hexdigest()
        self.provider = provider
        self.max_cost_cny = max_cost_cny
        self.browser_executable = browser_executable
        self.wall_time_s = wall_time_s
        self.spend_path = output_dir / "spend.jsonl"
        self.audit_path = output_dir / "audit.jsonl"
        self.ledger_path = output_dir / "batch.jsonl"
        self.client = self._client(model_env)
        self.model = self.client.model if provider == "real" else SCRIPTED_MODEL

        development = json.loads(DEVELOPMENT_TASKSET.read_text(encoding="utf-8"))
        self.development_seal = seal_task_set(development)
        self.task_set = opaque_task_set(development, set_id="twenty-companies-paired-pilot-v1")
        self.test_seal = seal_task_set(self.task_set)
        self.split = verify_split_separation(self.development_seal, self.test_seal)
        self.protocol = freeze_protocol(
            pilot_protocol(sorted(self.test_seal["task_digests"]), seal=self.test_seal,
                           development_seal=self.development_seal, split=self.split,
                           requested_model=self.model, seed=seed),
            development_seal=self.development_seal, test_seal=self.test_seal,
            split_verification=self.split)
        self.ledger_kwargs = {
            "protocol": self.protocol, "development_seal": self.development_seal,
            "test_seal": self.test_seal, "split_verification": self.split,
        }
        self.arms = {arm["arm_id"]: arm for arm in self.protocol["arms"]}

        self.companies = synthetic_companies(company_count)
        self.fixture = InMemoryFixture(self.companies)
        self.executor = SyntheticCompanyExecutor(
            self.companies, allow_mutations=True, shared_state=True,
            id_factory=lambda: CREATED_ID)
        self.page_model = SyntheticPageModel(
            self.companies, expose_company_list=True, shared_state=True)
        self.page = SyntheticTwentyPage(self.companies, expose_company_list=True, live=True)
        self.adapter: SyntheticTwentyAdapter | None = None
        self.state_path: Path | None = None
        self.storage_state: dict[str, Any] | None = None
        self.runtimes: list[SimulatedRuntime] = []
        self._directory: tempfile.TemporaryDirectory | None = None

    # -- composition ---------------------------------------------------------------------
    def _client(self, model_env: Path | None):
        if self.provider == "scripted":
            return None
        env = _environment((model_env or Path(".env")).expanduser(), os.environ)
        missing = [name for name in _REQUIRED_MODEL_KEYS if not env.get(name)]
        if missing:
            raise PilotConfigurationError(
                "paid pilot model configuration is incomplete: " + ", ".join(sorted(missing)))
        meter = SpendMeter(ledger=self.spend_path, batch_id="twenty-paired-synthetic-pilot",
                           batch_cost_stop=self.max_cost_cny)
        return ChatModel(
            meter, env={key: env[key] for key in _MODEL_KEYS if env.get(key)},
            model=env["AGENT_OPENAI_MODEL"], max_completion_tokens=2_048, timeout=180)

    def __enter__(self) -> "PairedPilot":
        self.page.__enter__()
        self._directory = tempfile.TemporaryDirectory()
        self.state_path = write_storage_state(
            Path(self._directory.name) / "state.json", origin=self.page.origin)
        self.storage_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.adapter = SyntheticTwentyAdapter(
            origin=self.page.origin, runtime=synthetic_business_runtime(self.executor))
        return self

    def __exit__(self, *_args: Any) -> None:
        for runtime in self.runtimes:
            runtime.stop()
        self.page.__exit__(None, None, None)
        if self._directory is not None:
            self._directory.cleanup()

    def _runtime_factory(self, *, approval_decision: str | None):
        def factory(*, channel, grant, registry, origin, entry_path):
            runtime = SimulatedRuntime(
                channel, grant, origin=origin, entry_path=entry_path,
                page_model=self.page_model, storage_state=self.storage_state,
                approval_decision=approval_decision)
            self.runtimes.append(runtime)
            return runtime
        return factory

    def _woa_client(self, case, arm):
        if self.provider == "real":
            return self.client
        return ScriptedWoaChat(scripted_woa_replies(case, arm, self.companies))

    def _pageagent_client(self, case, arm):
        if self.provider == "real":
            return self.client
        return ScriptedProviderClient(scripted_pageagent_actions(case, arm, self.companies))

    def woa_runner(self):
        def adapter(*, case, arm_id, task_id, schedule_position, run_id):
            arm = self.arms[arm_id]
            create = case["evaluator_task"]["kind"] == "create"
            source_map = None
            if arm["source_mode"] == "on":
                from web_operation_agent.schemas import SourceMap
                source_map = SourceMap(1, "pilot-source", "synthetic/source", "v1", [])
            return woa_runner.run_case(
                case=case, adapter=self.adapter, client=self._woa_client(case, arm),
                config=WoaTwentyConfig(
                    storage_state_path=self.state_path, wall_time_s=self.wall_time_s,
                    capability_profile=arm["capability_profile"],
                    source_mode=arm["source_mode"],
                    source_interface=arm["source_interface"], allow_create=create),
                ledger_path=self.audit_path,
                runtime_factory=self._runtime_factory(
                    approval_decision="approved" if create else None),
                page_sampler=lambda runtime: self.page_model.render(runtime.current_path)[1],
                run_id=run_id, source_map=source_map)
        return adapter

    def pageagent_runner(self):
        def adapter(*, case, arm_id, task_id, schedule_position, run_id):
            arm = self.arms[arm_id]
            create = case["evaluator_task"]["kind"] == "create"
            config = PageAgentTwentyConfig(
                bundle_path=self.bundle_path, bundle_digest=self.bundle_digest,
                storage_state_path=self.state_path, wall_time_s=self.wall_time_s,
                capability_profile=arm["capability_profile"], allow_create=create,
                browser_executable=self.browser_executable)
            return pageagent_runner.run_case(
                case=case, adapter=self.adapter, client=self._pageagent_client(case, arm),
                config=config, run_id=run_id,
                approval_decider=(lambda _message: "approved") if create else None)
        return adapter

    def runners(self) -> dict[str, Callable[..., dict[str, Any]]]:
        woa, pageagent = self.woa_runner(), self.pageagent_runner()
        return {arm_id: (woa if arm["engine"] == "web_operation_agent" else pageagent)
                for arm_id, arm in self.arms.items()}

    # -- execution -----------------------------------------------------------------------
    def batch_cost(self) -> float:
        return self.client.meter.batch_cost() if self.client is not None else 0.0

    def run(self, *, batch_id: str, max_trials: int | None = None) -> dict[str, Any]:
        """Drive the frozen schedule one trial at a time until done, terminal or over cost."""
        atomic_write_json(self.output_dir / "protocol.json", self.protocol)
        atomic_write_json(self.output_dir / "task-set.json", self.task_set)
        atomic_write_json(self.output_dir / "development-seal.json", self.development_seal)
        atomic_write_json(self.output_dir / "test-seal.json", self.test_seal)
        atomic_write_json(self.output_dir / "split.json", self.split)
        if not self.ledger_path.exists():
            initialize_batch(self.ledger_path, batch_id=batch_id, created_at=utc_now(),
                             **self.ledger_kwargs)
        runners = self.runners()
        stopped_reason: str | None = None
        driver_error: str | None = None
        executed = 0
        while True:
            status = verify_batch(self.ledger_path, **self.ledger_kwargs)
            if status["status"] not in {"OPEN"}:
                break
            if status["completed_trials"] >= status["expected_trials"]:
                break
            if max_trials is not None and executed >= max_trials:
                stopped_reason = "trial_limit"
                break
            if self.batch_cost() > self.max_cost_cny:
                stopped_reason = "cost_ceiling"
                break
            try:
                run_schedule(
                    task_set=self.task_set, fixture=self.fixture, runners=runners,
                    ledger_path=self.ledger_path, batch_id=batch_id,
                    restore=self.fixture.restore,
                    start_index=status["completed_trials"], max_trials=1,
                    **self.ledger_kwargs)
            except ScheduleDriverError as exc:
                driver_error = str(exc)
                stopped_reason = "driver_error"
                break
            executed += 1
        status = verify_batch(self.ledger_path, **self.ledger_kwargs)
        report = build_report(ledger_path=self.ledger_path, **self.ledger_kwargs)
        verify_report(report)
        atomic_write_json(self.output_dir / "report.json", report)
        summary = {
            "artifact_version": PILOT_VERSION,
            "kind": "twenty_paired_synthetic_pilot",
            "apparatus": APPARATUS,
            "provider": self.provider,
            "model": self.model,
            "protocol_id": self.protocol["protocol_id"],
            "protocol_digest": self.protocol["protocol_digest"],
            "bundle_digest": self.bundle_digest,
            "batch_status": status["status"],
            "expected_trials": status["expected_trials"],
            "completed_trials": status["completed_trials"],
            "stopped_reason": stopped_reason,
            "driver_error": driver_error,
            "known_cost_cny": report["known_cost_cny"],
            "total_cost_cny": report["total_cost_cny"],
            "cost_ceiling_cny": self.max_cost_cny,
            "outcomes": report["outcomes"],
            "comparisons": [
                {key: row[key] for key in (
                    "comparison_id", "comparison_type", "approval_boundary",
                    "pairs_scheduled", "pairs_completed", "pairs_both_valid",
                    "concordant_pairs", "discordant_pairs", "discordance_rate")}
                for row in report["comparisons"]
            ],
            "arms": [
                {key: arm[key] for key in (
                    "arm_id", "engine", "capability_profile", "trials", "valid_trials",
                    "outcomes", "terminal_reasons", "wall_ms", "known_cost_cny_per_trial")}
                for arm in report["arms"]
            ],
            "report_digest": report["report_digest"],
            "interpretation": (
                "Development pilot on random synthetic companies. The opaque task set repeats "
                "six development prompts, so discordance is indicative and pairs are not "
                "independent templates. Not a success rate, benchmark or comparison result."),
            "finished_at": utc_now(),
        }
        summary["summary_digest"] = digest_json(summary)
        atomic_write_json(self.output_dir / "summary.json", summary)
        return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the paired development pilot of both engines on synthetic data")
    parser.add_argument("--provider", choices=("scripted", "real"), default="scripted")
    parser.add_argument("--model-env", default=os.environ.get("WOA_MODEL_ENV", ".env"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--batch-id", default="paired-synthetic-pilot-v1")
    parser.add_argument("--bundle", default=os.environ.get("WOA_PAGEAGENT_BUNDLE")
                        or str(DEFAULT_BUNDLE))
    parser.add_argument("--browser-executable")
    parser.add_argument("--max-cost-cny", type=float, default=3.0)
    parser.add_argument("--max-trials", type=int)
    parser.add_argument("--wall-time-s", type=int, default=240)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with PairedPilot(
            output_dir=Path(args.output_dir), bundle_path=Path(args.bundle),
            provider=args.provider, model_env=Path(args.model_env) if args.model_env else None,
            max_cost_cny=args.max_cost_cny, wall_time_s=args.wall_time_s,
            browser_executable=(
                Path(args.browser_executable) if args.browser_executable else None),
    ) as pilot:
        summary = pilot.run(batch_id=args.batch_id, max_trials=args.max_trials)
    print(json.dumps({
        key: summary[key] for key in (
            "provider", "model", "batch_status", "completed_trials", "expected_trials",
            "stopped_reason", "known_cost_cny", "outcomes", "comparisons")
    }, ensure_ascii=False, sort_keys=True))
    return 0 if summary["batch_status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
