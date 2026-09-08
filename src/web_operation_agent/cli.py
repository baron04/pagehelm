"""Command line for the product.

    attest      prove the deployment, the source tree and the enforcement point agree
    compile     compile the deployed release's descriptors into a cited SourceMap
    retrieve    show the evidence bundle a task would be planned from (no model call)
    contract    generate and validate an OperationContract for a task
    run         run one task end to end: contract -> browser -> control plane -> receipts

Nothing here knows a task id, a business prompt or an evaluator. A task is a JSON file the
operator writes; the authorisation the run is allowed to perform is a second JSON file that
the model never sees.
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
import uuid
from pathlib import Path

from .attest import AttestationError, DeploymentConfig, attest
from .browser import ThinBrowserClient, TransportError
from .contract import PlanningFailed, SourceRetriever
from .controlplane import (ControlPlaneClient, ControlPlaneError,
                           assert_scope_is_not_the_answer, plan_from_spec)
from .ledger import SessionLedger
from .model import ChatModel, ProviderError, SpendMeter
from .schemas import OperationContract, SourceMap
from .session import ServerAgentSession, TaskSpec
from .sourcemap import OFBizCompiler
from .sourcetree import SourceTree

WORK = Path(os.environ.get("WOA_WORK", os.path.expanduser("~/.cache/web-operation-agent/runtime")))
# This cache version preserves declarative menu-link parameter bindings. Older cache payloads
# do not contain the exact evidence required by typed source affordances.
SOURCEMAP_CACHE_VERSION = 4


def _work(name: str) -> Path:
    return WORK / name


def _deployment(path: str) -> DeploymentConfig:
    return DeploymentConfig.from_file(path)


# -- attest --------------------------------------------------------------------------
def cmd_attest(a) -> int:
    att = attest(deployment=_deployment(a.deployment),
                 include_browser=not a.no_browser,
                 surface_digest_cache=str(_work("surface-digest.json")))
    output = _work("attestation.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(att.to_json(), indent=1))
    print(json.dumps(att.to_json(), indent=1))
    return 0


# -- compile -------------------------------------------------------------------------
def load_or_compile(policy: str, source_root: str | Path, attestation_id: str, *,
                    refresh: bool = False) -> SourceMap:
    """The compile is deterministic in the attested tree, so it is cached by binding id."""
    cache = _work(f"sourcemap-v{SOURCEMAP_CACHE_VERSION}-{attestation_id[:16]}.pkl")
    if cache.exists() and not refresh:
        smap = pickle.loads(cache.read_bytes())
        if smap.attestation_id == attestation_id:
            return smap
    tree = SourceTree.load(policy, source_root)
    smap = OFBizCompiler(tree, attestation_id).compile()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps(smap))
    return smap


def cmd_compile(a) -> int:
    deployment = _deployment(a.deployment)
    att = attest(deployment=deployment, include_browser=False,
                 surface_digest_cache=str(_work("surface-digest.json")))
    t0 = time.time()
    smap = load_or_compile(deployment.source_policy, deployment.source_root,
                           att.source_binding, refresh=a.refresh)
    kinds: dict[str, int] = {}
    for r in smap.relations:
        kinds[r.kind] = kinds.get(r.kind, 0) + 1
    out = {"binding_id": att.binding_id, "source_binding": att.source_binding,
           "release": smap.release,
           "relations": len(smap.relations), "by_kind": kinds,
           "unsupported": len(smap.unsupported), "seconds": round(time.time() - t0, 1),
           "content_digest": smap.content_digest}
    if a.out:
        Path(a.out).write_text(json.dumps(smap.to_json(), ensure_ascii=False))
        out["written"] = a.out
    print(json.dumps(out, indent=1))
    return 0


# -- retrieve / contract --------------------------------------------------------------
def _task_from(path: str) -> TaskSpec:
    d = json.loads(Path(path).read_text())
    return TaskSpec(task_ref=d["task_ref"], prompt=d["prompt"],
                    entry_path=d.get("entry_path", "/"),
                    supported_scope=d.get("supported_scope", ""))


def cmd_retrieve(a) -> int:
    deployment = _deployment(a.deployment)
    att = attest(deployment=deployment, include_browser=False,
                 surface_digest_cache=str(_work("surface-digest.json")))
    smap = load_or_compile(
        deployment.source_policy, deployment.source_root, att.source_binding)
    task = _task_from(a.task)
    bundle = SourceRetriever(smap).retrieve(task.task_ref, task.prompt)
    print(bundle.render())
    print("\n--- " + json.dumps(bundle.stats))
    return 0


def cmd_contract(a) -> int:
    deployment = _deployment(a.deployment)
    att = attest(deployment=deployment, include_browser=False,
                 surface_digest_cache=str(_work("surface-digest.json")))
    smap = load_or_compile(
        deployment.source_policy, deployment.source_root, att.source_binding)
    task = _task_from(a.task)
    meter = SpendMeter(batch_id=a.batch_id or "contract-cli")
    chat = ChatModel(meter, model=a.model)
    from .contract import ContractPlanner
    bundle = SourceRetriever(smap).retrieve(task.task_ref, task.prompt)
    try:
        contract, attempts = ContractPlanner(chat, smap).plan(task.task_ref, task.prompt, bundle,
                                                              f"contract-{task.task_ref}")
    except PlanningFailed as e:
        print(json.dumps({"ok": False, "error": str(e), "spend": meter.report()}, indent=1))
        return 2
    payload = {"ok": True, "attempts": attempts, "spend": meter.report(),
               "contract": contract.to_json()}
    if a.out:
        Path(a.out).write_text(json.dumps(contract.to_json(), ensure_ascii=False, indent=1))
        payload["written"] = a.out
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


# -- run -----------------------------------------------------------------------------
def build_session(*, task: TaskSpec, arm: str, deployment: DeploymentConfig, model: str | None,
                  batch_id: str, run_id: str, step_budget: int, wall_budget: int,
                  oracle_path: str | None, ledger_dir: Path,
                  headless: bool = True,
                  runtime_source: bool = False,
                  source_affordances: str = "off",
                  ) -> tuple[ServerAgentSession, ThinBrowserClient, SpendMeter]:
    # Use one enforcement configuration for attestation and execution. The application,
    # browser and control-plane client must all bind to the same deployment.
    if source_affordances not in {"off", "auto", "empty"}:
        raise ValueError("--source-affordances must be off, auto, or empty")
    if runtime_source and source_affordances != "off":
        raise ValueError("--runtime-source cannot be combined with --source-affordances auto or empty")
    if runtime_source and arm.upper() != "S":
        raise ValueError("--runtime-source requires arm S")
    if source_affordances == "auto" and arm.upper() != "S":
        raise ValueError("--source-affordances auto requires arm S and a SourceMap")
    if source_affordances == "empty" and arm.upper() != "P":
        raise ValueError("--source-affordances empty requires arm P")
    if not isinstance(deployment, DeploymentConfig):
        raise TypeError("build_session requires an explicit DeploymentConfig")
    att = attest(deployment=deployment, include_browser=True,
                 surface_digest_cache=str(_work("surface-digest.json")))
    smap = None
    oracle = None
    # `auto` and `empty` both use a page-only contract and differ only in whether the typed
    # source candidate set is populated. `off` selects the ordinary planning mode.
    plan_from_page = arm.upper() == "P" or source_affordances in {"auto", "empty"}
    source_contract = arm.upper() == "S" and source_affordances == "off"
    if arm.upper() == "S":
        smap = load_or_compile(
            deployment.source_policy, deployment.source_root, att.source_binding)
    elif arm.upper() == "O":
        if not oracle_path:
            raise SystemExit("arm O needs --oracle <contract.json>")
        oracle = OperationContract.from_json(json.loads(Path(oracle_path).read_text()))
    meter = SpendMeter(batch_id=batch_id)
    chat = ChatModel(meter, model=model)
    ledger = SessionLedger(ledger_dir / f"{run_id}.jsonl", run_id)
    ledger.append("attested", binding_id=att.binding_id,
                  release=att.source["release"], version=att.application["version"],
                  gateway=att.enforcement["gateway_loaded_sha256"][:16],
                  proxy=att.enforcement["proxy_sha256"][:16])
    browser = ThinBrowserClient(deployment.application_base_url, headless=headless)
    session = ServerAgentSession(task=task, arm=arm.upper(), chat=chat, browser=browser,
                                 control=ControlPlaneClient(deployment.gateway),
                                 ledger=ledger, attestation=att,
                                 source_map=smap, plan_from_page=plan_from_page,
                                 source_contract=source_contract,
                                 oracle_contract=oracle,
                                 step_budget=step_budget, wall_budget_s=wall_budget,
                                 run_id=run_id, terms_cache=_work("terms"),
                                 runtime_source=runtime_source,
                                 source_affordance_mode=source_affordances)
    return session, browser, meter


def cmd_run(a) -> int:
    task = _task_from(a.task)
    run_id = a.run_id or f"{task.task_ref}-{a.arm.upper()}-{uuid.uuid4().hex[:6]}"
    ledger_dir = Path(a.ledger_dir or _work("ledger"))
    try:
        deployment = _deployment(a.deployment)
        session, browser, meter = build_session(
            task=task, arm=a.arm, deployment=deployment, model=a.model,
            batch_id=a.batch_id or "cli", run_id=run_id, step_budget=a.steps,
            wall_budget=a.wall, oracle_path=a.oracle, ledger_dir=ledger_dir,
            headless=not a.headed, runtime_source=a.runtime_source,
            source_affordances=a.source_affordances)
    except AttestationError as e:
        print(json.dumps({"terminal_reason": "attestation_failed", "detail": str(e)}, indent=1))
        return 3
    except (ControlPlaneError, ProviderError) as e:
        print(json.dumps({"terminal_reason": "control_plane_error", "detail": str(e)}, indent=1))
        return 3

    result = None
    try:
        if a.authorise:
            ops = json.loads(Path(a.authorise).read_text())
            plan = plan_from_spec(f"plan-{run_id}", task.task_ref,
                                  ops["operations"] if isinstance(ops, dict) else ops)
            assert_scope_is_not_the_answer(task.supported_scope, plan, task.prompt)
            session.install_control_plan(plan)
        browser.start(entry_path=task.entry_path)
        session.observe_entry()
        session.plan()
        result = session.run()
    except (TransportError, ControlPlaneError) as e:
        session.ledger.append("apparatus_error", detail=str(e))
        print(json.dumps({"run_id": run_id, "terminal_reason": "transport_error",
                          "detail": str(e), "spend": meter.report()}, indent=1))
        return 3
    finally:
        browser.close()
    print(json.dumps(result.to_json(), ensure_ascii=False, indent=1))
    return 0 if result.terminal_reason in ("agent_finished", "agent_refused") else 1


# -- argument parsing ------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="web-operation-agent")
    p.add_argument(
        "--deployment", required=True,
        help="deployment JSON containing source, application and enforcement identity",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("attest"); s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_attest)

    s = sub.add_parser("compile")
    s.add_argument("--refresh", action="store_true"); s.add_argument("--out")
    s.set_defaults(fn=cmd_compile)

    s = sub.add_parser("retrieve"); s.add_argument("--task", required=True)
    s.set_defaults(fn=cmd_retrieve)

    s = sub.add_parser("contract")
    s.add_argument("--task", required=True); s.add_argument("--out")
    s.add_argument("--model"); s.add_argument("--batch-id")
    s.set_defaults(fn=cmd_contract)

    s = sub.add_parser("run")
    s.add_argument("--task", required=True)
    # U plans nothing; P plans from the page; S plans from the page and source; O is the
    # hand-written positive control.
    s.add_argument("--arm", default="U", choices=["U", "P", "S", "O", "u", "p", "s", "o"])
    s.add_argument("--authorise", help="JSON file of operations the run is permitted to perform")
    s.add_argument("--oracle", help="hand-written contract, arm O only")
    s.add_argument("--model"); s.add_argument("--batch-id"); s.add_argument("--run-id")
    s.add_argument("--steps", type=int, default=20)
    s.add_argument("--wall", type=int, default=300)
    s.add_argument("--ledger-dir")
    s.add_argument("--headed", action="store_true")
    s.add_argument("--runtime-source", action="store_true",
                   help="development: enable on-demand cited source lookup in arm S")
    s.add_argument("--source-affordances", choices=("off", "auto", "empty"), default="off",
                   help="inject typed source affordances, or an empty causal control")
    s.set_defaults(fn=cmd_run)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except AttestationError as e:
        print(json.dumps({"error": "attestation_failed", "detail": str(e)}, indent=1),
              file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
