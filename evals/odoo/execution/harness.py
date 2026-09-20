"""What every P7.4 batch runs with: private inputs, header bindings, gateway budgets and driver.

The zero-model qualification and the formal entry both use this module, so a qualification batch
and a formal batch bind their header facts and drive their attempts the same way. They differ
only in the backend behind the gateway -- scripted responses or the evaluation provider -- and
in their schedules.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.pageagent.host import PageAgentProviderContract

from evals.odoo.domain import population as population_module, protocol, tasks
from evals.odoo.fixture import manifest
from evals.odoo.reporting import ledger
from evals.odoo.execution.driver import Driver, DriverConfig
from evals.odoo.provider.gateway import Backend, GatewayBudgets, ProviderGateway
from evals.odoo.execution.runner_pageagent import PageAgentRunnerConfig
from evals.odoo.execution.runner_woa import ActorCredential, WoaRunnerConfig
from evals.odoo.fixture.stack import REPOSITORY, Stack

PRIVATE = REPOSITORY / ".private" / "p74-odoo-v1"
TASKSETS = REPOSITORY / "evals" / "odoo" / "tasksets"
DEVELOPMENT_TASK_SET = TASKSETS / "p74-odoo-development-v1.json"
DEVELOPMENT_SEAL = TASKSETS / "p74-odoo-development-v1.seal.json"
HELDOUT_SEAL = TASKSETS / "p74-odoo-heldout-v1.seal.json"
SPLIT_VERIFICATION = TASKSETS / "p74-odoo-split-v1.json"
ACTORS = ("writer", "reader", "other")


POPULATIONS = ("development", "heldout")


def heldout_task_set_path(private_dir: Path) -> Path:
    """The held-out task set, kept only in the private directory and its escrow."""
    return private_dir / "heldout" / "p74-odoo-heldout-v1.json"


def seal_path(population: str) -> Path:
    return {"development": DEVELOPMENT_SEAL, "heldout": HELDOUT_SEAL}[population]


@dataclass
class Context:
    stack: Stack
    population: dict[str, Any]
    seed_ids: dict[str, Any]
    task_set: dict[str, Any]
    keys: dict[str, Path]
    actors: dict[str, ActorCredential]
    bundle: Path
    root: Path
    #: Which split this context runs: its seed, task set and seal.
    population_name: str = "development"

    @property
    def private_dir(self) -> Path:
        return self.stack.private_dir

    def record(self, role: str) -> dict[str, Any]:
        return population_module.record(self.population, role)

    def inputs(self, task_id: str) -> tuple[tasks.RunnerInput, tasks.EvaluatorInput]:
        return tasks.instantiate(self.task_set, task_id, population=self.population,
                                 seed_ids=self.seed_ids)


def load_context(root: Path | None = None, *, private_dir: Path = PRIVATE,
                 population: str = "development") -> Context:
    """One split's population, task set and actors; ``root`` holds this run's ledgers.

    The fixture holds one population at a time. A context of the other split still loads, and
    every binding checked against the running stack -- the manifest, the seeded identities --
    refuses it.
    """
    if population not in POPULATIONS:
        raise ValueError("a context runs the development or the held-out split")
    seed = (private_dir / "seeds" / f"{population}.seed").read_text().strip()
    task_set = (DEVELOPMENT_TASK_SET if population == "development"
                else heldout_task_set_path(private_dir))
    root = root or private_dir / "qualification"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Context(
        stack=Stack(private_dir=private_dir),
        population=population_module.generate(seed),
        seed_ids=json.loads((private_dir / "seeded" / "truth.json").read_text()),
        task_set=json.loads(task_set.read_text()),
        keys={role: private_dir / "keys" / f"{role}.api-key.json" for role in ACTORS},
        actors={role: ActorCredential(f"p74-{role}@example.test",
                                      private_dir / "stack" / f"{role}-login-password")
                for role in ACTORS},
        bundle=REPOSITORY / ".artifacts/evals/pageagent/pageagent-baseline.js",
        root=root, population_name=population)


def fixture_manifest_path(private_dir: Path) -> Path:
    return private_dir / "qualification" / "fixture-manifest.json"


def protocol_budgets(**overrides: Any) -> GatewayBudgets:
    """The gateway budgets and request parameters the machine protocol fixes."""
    parameters = protocol.PROVIDER["request_parameters"]
    values = {"requested_model": protocol.PROVIDER["requested_model_alias"],
              "max_model_calls": protocol.BUDGETS["max_model_calls_per_attempt"],
              "max_output_tokens": protocol.BUDGETS["max_output_tokens_per_call"],
              "max_request_bytes": protocol.BUDGETS["max_request_bytes"],
              "temperature": parameters["temperature"],
              "model_options": dict(parameters["model_options"])}
    return GatewayBudgets(**{**values, **overrides})


def batch_bindings(ctx: Context) -> dict[str, Any]:
    """What a batch header binds, each fact verified first, for the context's split.

    The candidate is the running Agent image built from this clean checkout; the fixture
    manifest must describe the fixture in front of us, on the recorded baseline; the task set
    must still match its committed seal. The manifest is checked by what made the fixture, not
    by the candidate: rebuilding the Agent changes what is under test, never what it is tested on.
    """
    candidate = ctx.stack.verify_candidate()
    recovery = json.loads((ctx.private_dir / "recovery" / "recovery.json").read_text())
    record = manifest.verify_manifest(
        json.loads(fixture_manifest_path(ctx.private_dir).read_text()),
        inputs=manifest.fixture_inputs(ctx.stack, population=ctx.population),
        baseline_guard_digest=recovery["baseline_digest"])
    seal = json.loads(seal_path(ctx.population_name).read_text())
    tasks.verify_seal(ctx.task_set, seal, population=ctx.population)
    return {"task_set_seal_digest": seal["seal_digest"],
            "fixture": manifest.batch_fixture(record),
            "capability_digests": {arm: protocol.arm_capability_digest(arm)
                                   for arm in protocol.ARMS},
            "candidate": candidate}


@contextmanager
def open_driver(ctx: Context, path: Path, playwright: Any, *, backend: Backend,
                budgets: GatewayBudgets | None = None, stack: Stack | None = None,
                recovery_dir: Path | None = None,
                attempt_wall_s: float | None = None) -> Iterator[Driver]:
    """A driver and its gateway bound to an existing ledger; the gateway closes on exit.

    ``attempt_wall_s`` shortens the attempt's automatic wall clock for a qualification probe;
    formal batches run the protocol's.
    """
    stack = stack or ctx.stack
    gateway = ProviderGateway(
        ledger_path=path, backend=backend, port=stack.gateway_port,
        budgets=budgets or protocol_budgets()).start()
    evidence = ctx.root / "evidence" / path.stem
    turn_wall_s = protocol.BUDGETS["turn_wall_s"]
    attempt_wall_s = attempt_wall_s or protocol.BUDGETS["attempt_automatic_wall_s"]
    config = DriverConfig(
        stack=stack, ledger_path=path, evidence_root=evidence,
        recovery_dir=recovery_dir or ctx.private_dir / "recovery", actor_key_files=ctx.keys,
        woa=WoaRunnerConfig(base_url=stack.base_url, actors=ctx.actors,
                            evidence_dir=evidence / "screens", turn_timeout_s=turn_wall_s,
                            attempt_wall_s=attempt_wall_s),
        pageagent=PageAgentRunnerConfig(
            base_url=stack.base_url, actors=ctx.actors, evidence_dir=evidence / "screens",
            bundle_path=ctx.bundle,
            bundle_sha256="sha256:" + hashlib.sha256(ctx.bundle.read_bytes()).hexdigest(),
            max_steps=protocol.BUDGETS["pageagent_max_steps"],
            max_page_actions=protocol.BUDGETS["pageagent_max_page_actions"],
            wall_time_s=turn_wall_s, attempt_wall_s=attempt_wall_s),
        contract=PageAgentProviderContract(
            model=protocol.PROVIDER["requested_model_alias"], temperature=0.0, top_p=1.0,
            max_completion_tokens=protocol.BUDGETS["max_output_tokens_per_call"],
            max_model_calls=protocol.BUDGETS["max_model_calls_per_attempt"],
            wall_time_s=protocol.BUDGETS["turn_wall_s"]),
        fixture_digest=json.loads(fixture_manifest_path(ctx.private_dir).read_text())[
            "manifest_digest"],
        capability_digests={arm: protocol.arm_capability_digest(arm) for arm in protocol.ARMS},
        provider_config_digest=protocol.provider_config_digest())
    try:
        yield Driver(config, gateway=gateway, backend=backend, playwright=playwright)
    finally:
        gateway.close()


def finish_batch(path: Path) -> dict[str, Any]:
    """Seal a batch that owes a seal, or complete one whose every slot has an attempt.

    A batch holding for a call whose cost is unknown does neither: it stays open until the
    provider's bill accounts for that call, and then runs the slots it has left. Only a fault it
    already met still seals it -- a safety event does not wait on a bill.
    """
    _, summary = ledger.read(path)
    if summary["disposition"] == "must_seal":
        return ledger.append(path, "batch_sealed", {"reason": summary["pending_seal"]})
    if (summary["disposition"] == "running"
            and all(slot["attempts"] for slot in summary["slots"])):
        return ledger.append(path, "batch_completed", {})
    return summary
