"""Run the paid breadth diagnostic only on random synthetic CRM data."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Sequence

from evals.pageagent.scripted import SYNTHETIC_FIXTURE, SyntheticTwentyAdapter, write_storage_state
from evals.twenty.breadth import make_breadth_state
from evals.twenty.fixture import atomic_write_json, utc_now
from evals.woa.breadth_scripted import (SyntheticBreadthExecutor, SyntheticBreadthPageModel,
                                       breadth_projection, synthetic_breadth_records,
                                       synthetic_breadth_runtime)
from evals.woa.pilot_breadth import (DEVELOPMENT_TASKSET, _MODEL_KEYS,
                                     _REQUIRED_MODEL_KEYS, _environment, run_pilot)
from evals.woa.scripted import SimulatedRuntime
from web_operation_agent.modeling.client import ChatModel, SpendMeter


ORIGIN = "http://127.0.0.1:3020"


class _SyntheticFixture:
    def __init__(self, projection):
        self.projection = projection

    def capture_breadth(self, *, role: str):
        return make_breadth_state(
            fixture=SYNTHETIC_FIXTURE, projection=self.projection,
            captured_at=utc_now(), role=role)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run paid breadth pilot on synthetic data")
    parser.add_argument("--model-env", default=os.environ.get("WOA_MODEL_ENV", ".env"))
    parser.add_argument("--taskset", default=str(DEVELOPMENT_TASKSET))
    parser.add_argument("--output", required=True)
    parser.add_argument("--spend-ledger", required=True)
    parser.add_argument("--audit-ledger", required=True)
    parser.add_argument("--max-cost-cny", type=float, default=3.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 < args.max_cost_cny <= 100:
        raise ValueError("max-cost-cny must be from 0 to 100")
    env = _environment(Path(args.model_env).expanduser(), os.environ)
    missing = [name for name in _REQUIRED_MODEL_KEYS if not env.get(name)]
    if missing:
        raise RuntimeError("paid pilot model configuration is incomplete: "
                           + ", ".join(sorted(missing)))
    meter = SpendMeter(
        ledger=Path(args.spend_ledger), batch_id="twenty-breadth-synthetic-pilot",
        batch_cost_stop=args.max_cost_cny)
    client = ChatModel(
        meter, env={key: env[key] for key in _MODEL_KEYS if env.get(key)},
        model=env["AGENT_OPENAI_MODEL"], max_completion_tokens=2_048, timeout=180)
    records = synthetic_breadth_records()
    executor = SyntheticBreadthExecutor(records)
    adapter = SyntheticTwentyAdapter(
        origin=ORIGIN, runtime=synthetic_breadth_runtime(executor))
    page_model = SyntheticBreadthPageModel(records)
    with tempfile.TemporaryDirectory() as directory:
        state_path = write_storage_state(Path(directory) / "state.json", origin=ORIGIN)
        storage_state = json.loads(state_path.read_text(encoding="utf-8"))
        runtimes = []

        def factory(**kwargs):
            runtime = SimulatedRuntime(
                kwargs["channel"], kwargs["grant"], origin=kwargs["origin"],
                entry_path=kwargs["entry_path"], page_model=page_model,
                storage_state=storage_state)
            runtimes.append(runtime)
            return runtime

        def sample(runtime):
            return runtime.page_model.render(runtime.current_path)[1]

        result = run_pilot(
            fixture=_SyntheticFixture(breadth_projection(records)), adapter=adapter,
            client=client,
            taskset=json.loads(Path(args.taskset).read_text(encoding="utf-8")),
            storage_state_path=state_path, page_controller_module=None,
            ledger_path=Path(args.audit_ledger), max_cost_cny=args.max_cost_cny,
            runtime_factory=factory, page_sampler=sample)
        for runtime in runtimes:
            runtime.stop()
    result["apparatus"] = "random_synthetic_crm_no_user_data"
    atomic_write_json(Path(args.output), result)
    print(json.dumps({
        "status": result["status"], "apparatus": result["apparatus"],
        "completed_case_count": result["completed_case_count"],
        "evaluation_counts": result["evaluation_counts"],
        "known_cost_cny": result["known_cost_cny"],
        "unknown_cost_calls": result["unknown_cost_calls"],
        "stopped_reason": result["stopped_reason"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
