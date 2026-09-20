"""P7.4a zero-model apparatus qualification of the ``p74_odoo_v1`` evaluation.

Every decision is scripted: no external model is contacted and no spend ledger is touched. The
qualification runs the real product, real Chromium, the real Odoo fixture, the gateway and
attempt ledger, the independent evaluator and the reset, and records only content-free
checks, counts and digests. It proves the apparatus can tell a correct result from a wrong
one and a product failure from an apparatus failure; it says nothing about how well any model
or engine performs.

On the live stack it also shows that typed provider failures are classified from the gateway's
records, alike on both arms; that the attempt wall clock stops a running turn and times the
panel's cancel feedback; that an uncertain reset is refused until it is re-armed; and that a
driver process killed mid-attempt is recovered and replaced.

    python -m evals.odoo.qualify run
    python -m evals.odoo.qualify smoke
    python -m evals.odoo.qualify sample d01 --arm woa_product
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.odoo.domain import population as population_module, protocol, tasks
from evals.odoo.execution import harness, scripted
from evals.odoo.fixture import fixture
from evals.odoo.judging import leakcheck
from evals.odoo.reporting import ledger, report
from evals.odoo.execution.driver import AttemptPlan
from evals.odoo.provider.gateway import (SCRIPTED_MODEL, GatewayBudgets, ProviderGateway, ScriptedBackend,
                      provider_binding_digest)
from evals.odoo.execution.harness import (PRIVATE, Context, batch_bindings, finish_batch, fixture_manifest_path,
                      load_context, protocol_budgets)
from evals.odoo.fixture.projection import actor_projection
from evals.odoo.fixture.rpc import OdooActorClient
from evals.odoo.fixture.stack import REPOSITORY, Stack

ADAPTER_IN_IMAGE = "/usr/local/lib/python3.12/site-packages/web_operation_agent_odoo/adapter.py"
QUOTATION_DOMAIN = 'domain=[("state", "in", ("draft", "sent"))],'
MUTATED_QUOTATION_DOMAIN = 'domain=[("state", "in", ("draft", "sent", "sale"))],'


# -- scripted programs ---------------------------------------------------------------------

from evals.odoo.qualification.programs import a_program, c_program, woa

# -- batches -----------------------------------------------------------------------------------

@dataclass
class SlotSpec:
    task_id: str
    arm: str
    expect: str
    variant: str = ""
    faults: dict[int, str] | None = None
    models: dict[int, str] | None = None
    inject: frozenset[str] = frozenset()
    oracle_override: dict[str, Any] | None = None


def _plan(ctx: Context, spec: SlotSpec, slot: dict[str, Any]) -> AttemptPlan:
    runner, evaluator = ctx.inputs(spec.task_id)
    if spec.oracle_override is not None:
        evaluator = dataclasses.replace(evaluator, approval_oracle=spec.oracle_override)
    policy = (c_program(ctx, spec.task_id, variant=spec.variant) if spec.arm == "woa_product"
              else a_program(ctx, spec.task_id, variant=spec.variant))
    return AttemptPlan(slot=slot, runner_input=runner, evaluator_input=evaluator,
                       policy=policy, provider_faults=spec.faults, provider_models=spec.models,
                       inject=spec.inject)


def _slot_fields(slot: dict[str, Any]) -> dict[str, Any]:
    return {key: slot[key] for key in ("slot_id", "arm", "task_id", "replicate")}


def create_batch(ctx: Context, name: str, specs: list[SlotSpec], *,
                 budgets: GatewayBudgets | None = None) -> Path:
    """A new qualification ledger whose schedule is exactly ``specs``; nothing runs yet."""
    ledgers = ctx.root / "ledgers"
    ledgers.mkdir(mode=0o700, exist_ok=True)
    path = ledgers / f"{name}-{time.strftime('%Y%m%dT%H%M%S')}.jsonl"
    slots = [{"slot_id": f"q{index + 1:03d}", "arm": spec.arm, "task_id": spec.task_id,
              "replicate": "primary"} for index, spec in enumerate(specs)]
    ledger.create(path, {
        "batch_id": name, "protocol_id": protocol.PROTOCOL_ID,
        "protocol_digest": protocol.document()["protocol_digest"],
        "schedule_digest": ledger.schedule_digest(slots), "purpose": "apparatus_qualification",
        "provider_config_digest": protocol.provider_config_digest(), "slots": slots,
        "schedule_seed": None,
        "provider_binding_digest": provider_binding_digest(ScriptedBackend().identity(),
                                                           budgets or protocol_budgets()),
        **batch_bindings(ctx)})
    return path


def open_driver(ctx: Context, path: Path, playwright: Any, **options: Any):
    """A driver bound to an existing qualification ledger; its gateway answers from scripts."""
    return harness.open_driver(ctx, path, playwright, backend=ScriptedBackend(), **options)


def batch_record(path: Path, summary: dict[str, Any], specs: list[SlotSpec]) -> dict[str, Any]:
    return {"ledger": str(path.relative_to(REPOSITORY)), "summary": summary,
            "expectations": [dataclasses.asdict(spec) | {"inject": sorted(spec.inject)}
                             for spec in specs]}


def run_batch(ctx: Context, name: str, specs: list[SlotSpec], playwright: Any, *,
              budgets: GatewayBudgets | None = None, stack: Stack | None = None,
              recovery_dir: Path | None = None,
              between: Callable[[ProviderGateway, int], None] | None = None,
              attempt_wall_s: float | None = None) -> dict[str, Any]:
    path = create_batch(ctx, name, specs, budgets=budgets)
    with open_driver(ctx, path, playwright, budgets=budgets, stack=stack,
                     recovery_dir=recovery_dir, attempt_wall_s=attempt_wall_s) as driver:
        slots = ledger.read(path)[1]["slots"]
        for index, (spec, slot) in enumerate(zip(specs, slots)):
            if between is not None and index:
                between(driver.gateway, index)
            # A replacement gets its own fresh script and never inherits the injected fault.
            clean = dataclasses.replace(spec, faults=None, models=None, inject=frozenset())
            summary = driver.run_slot(_plan(ctx, spec, _slot_fields(slot)),
                                      replacement=_plan(ctx, clean, _slot_fields(slot)))
            if summary["status"] != "OPEN":
                break
    return batch_record(path, finish_batch(path), specs)


def selected_outcomes(batch: dict[str, Any]) -> list[dict[str, Any]]:
    summary = batch["summary"]
    by_id = {item["attempt_id"]: item for item in summary["attempts"]}
    rows = []
    for slot, spec in zip(summary["slots"], batch["expectations"]):
        attempts = [by_id[item] for item in slot["attempts"]]
        rows.append({"slot_id": slot["slot_id"], "task_id": slot["task_id"], "arm": slot["arm"],
                     "expect": spec["expect"], "attempts": [
                         {key: item[key] for key in ("attempt_id", "product_outcome",
                                                     "apparatus_integrity", "apparatus_code",
                                                     "unclassified", "reset_outcome",
                                                     "replacement_of", "replacement_eligible",
                                                     "safety_events", "extraction_ambiguous")}
                         for item in attempts]})
    return rows


def child_attempt(ctx: Context, path: Path, index: int, probe: str = "interrupted") -> int:
    from evals.odoo.qualification.probes import child_attempt as run_child
    return run_child(ctx, path, index, probe)


def interrupted_probe(ctx: Context, playwright: Any) -> dict[str, Any]:
    from evals.odoo.qualification.probes import interrupted_probe as run_probe
    return run_probe(ctx, playwright)


def settled_turn_probe(ctx: Context, playwright: Any) -> dict[str, Any]:
    from evals.odoo.qualification.probes import settled_turn_probe as run_probe
    return run_probe(ctx, playwright)


def reset_probes(ctx: Context) -> dict[str, Any]:
    from evals.odoo.qualification.probes import reset_probes as run_probes
    return run_probes(ctx)


def mutation_stack(ctx: Context) -> Stack:
    from evals.odoo.qualification.probes import mutation_stack as build_stack
    return build_stack(ctx)


def spend_ledger_fingerprints() -> dict[str, str | None]:
    from evals.odoo.qualification.probes import spend_ledger_fingerprints as fingerprints
    return fingerprints()


# -- the whole qualification -----------------------------------------------------------------------

_W, _A = "woa_product", "pageagent_browser_only"
#: The scripted samples: the product carried through every stratum on both arms in script mode,
#: so a build that cannot run never reaches a paid batch.
SAMPLE_SPECS = [
    SlotSpec("d01", _W, "PASS"), SlotSpec("d01", _A, "PASS"), SlotSpec("d02", _W, "PASS"),
    SlotSpec("d03", _A, "PASS"), SlotSpec("d06", _A, "PASS"), SlotSpec("d07", _W, "PASS"),
    SlotSpec("d08", _W, "PASS"), SlotSpec("d11", _W, "PASS"), SlotSpec("d13", _W, "PASS"),
    SlotSpec("d16", _W, "PASS"), SlotSpec("d18", _W, "PASS"), SlotSpec("d20", _W, "PASS"),
    SlotSpec("d21", _W, "PASS"), SlotSpec("d22", _W, "PASS"), SlotSpec("d24", _W, "PASS"),
    SlotSpec("d26", _W, "PASS"), SlotSpec("d30", _W, "PASS")]
#: A turn the attempt's wall clock stops through the panel, which times the cancel feedback.
WALL_SPECS = [SlotSpec("d01", _W, "WALL_CLOCK_BUDGET", faults={1: "slow_answer"})]
SMOKE_KIND = "p74_smoke_qualification"


def smoke_run(ctx: Context) -> dict[str, Any]:
    """The qualification a candidate that changed only the product needs before a paid batch.

    It checks what a product change can break: the scripted samples still pass on both arms, the
    panel still answers submit and cancel within 300 ms, and the product carries no record name
    of either population. The apparatus it runs on is the one a full qualification of an
    earlier commit already proved; the batch entry accepts this only while nothing under
    ``evals/`` changed since that commit.
    """
    from playwright.sync_api import sync_playwright

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    spend_before = spend_ledger_fingerprints()
    candidate = ctx.stack.verify_candidate()
    heldout_population = population_module.generate(
        (ctx.private_dir / "seeds" / "heldout.seed").read_text().strip())
    leaks = leakcheck.scan(population_module.record_names(ctx.population)
                           | population_module.record_names(heldout_population))
    with sync_playwright() as playwright:
        batches = {"samples": run_batch(ctx, "p74a-smoke-samples", SAMPLE_SPECS, playwright),
                   "attempt_wall": run_batch(ctx, "p74a-smoke-attempt-wall", WALL_SPECS,
                                             playwright, attempt_wall_s=8)}
    samples = selected_outcomes(batches["samples"])
    wall = {"status": batches["attempt_wall"]["summary"]["status"]}
    checks = {
        "leak_check": leaks["status"] == "READY",
        "samples_batch_complete": batches["samples"]["summary"]["status"] == "COMPLETE",
        "every_sample_passes": all(
            (_selected(row) or {}).get("product_outcome") == "PASS"
            and (_selected(row) or {}).get("apparatus_integrity") == "VALID" for row in samples),
        "attempt_wall_stops_the_turn_and_is_scored": _wall_clock_ok(wall,
                                                                   batches["attempt_wall"]),
        **feedback_checks(batches),
        "scripted_model_only": all(
            batch["summary"]["provider_model"] in {None, SCRIPTED_MODEL}
            for batch in batches.values()),
        "spend_ledgers_untouched": spend_ledger_fingerprints() == spend_before,
    }
    result = {"artifact_version": 1, "kind": SMOKE_KIND, "started_at": started,
              "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "external_model_calls": 0, "candidate": candidate,
              "fixture_manifest_digest": json.loads(
                  fixture_manifest_path(ctx.private_dir).read_text())["manifest_digest"],
              "checks": checks, "status": "READY" if all(checks.values()) else "FAILED"}
    target = ctx.root / f"p74a-smoke-{started.replace('-', '').replace(':', '')}.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return {**result, "artifact": str(target.relative_to(REPOSITORY))}

def offline_checks(ctx: Context) -> dict[str, Any]:
    heldout = json.loads((PRIVATE / "heldout" / "p74-odoo-heldout-v1.json").read_text())
    heldout_population = population_module.generate(
        (PRIVATE / "seeds" / "heldout.seed").read_text().strip())
    tasksets = REPOSITORY / "evals/odoo/tasksets"
    development_seal = json.loads((tasksets / "p74-odoo-development-v1.seal.json").read_text())
    heldout_seal = json.loads((tasksets / "p74-odoo-heldout-v1.seal.json").read_text())
    tasks.verify_seal(ctx.task_set, development_seal, population=ctx.population)
    tasks.verify_seal(heldout, heldout_seal, population=heldout_population)
    split = tasks.verify_split_separation(development_seal, heldout_seal)
    committed_split = json.loads((tasksets / "p74-odoo-split-v1.json").read_text())
    leaks = leakcheck.scan(population_module.record_names(ctx.population)
                           | population_module.record_names(heldout_population))
    stability = json.loads((ctx.root / "stability-proof.json").read_text())
    return {
        "task_seals_verified": True,
        "split_matches_committed": split == committed_split,
        "leak_check": {key: leaks[key] for key in ("status", "scanned_files",
                                                   "optional_roots_present")},
        "stability_proof": {key: stability[key] for key in ("status", "interval_s",
                                                            "required_interval_s", "changed")},
        "protocol_digest": protocol.document()["protocol_digest"],
    }


def run(ctx: Context) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    spend_before = spend_ledger_fingerprints()
    candidate = ctx.stack.verify_candidate()
    result: dict[str, Any] = {
        "artifact_version": 1, "kind": "p74a_apparatus_qualification", "started_at": started,
        "external_model_calls": 0, "candidate": candidate,
        "fixture_manifest_digest": json.loads(fixture_manifest_path(ctx.private_dir).read_text())[
            "manifest_digest"]}
    result["offline"] = offline_checks(ctx)
    result["reset_probes"] = reset_probes(ctx)
    W, A = "woa_product", "pageagent_browser_only"
    batches: dict[str, Any] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        result["browser_version"] = browser.version
        browser.close()
        batches["samples"] = run_batch(ctx, "p74a-samples", SAMPLE_SPECS, playwright)
        batches["negatives"] = run_batch(ctx, "p74a-negatives", [
            SlotSpec("d01", W, "FAIL", variant="missing_field"),
            SlotSpec("d01", A, "FAIL", variant="wrong_email"),
            SlotSpec("d08", W, "FAIL", variant="no_currency_filter")], playwright)
        batches["seal_side_effect"] = run_batch(ctx, "p74a-seal-extra-side-effect", [
            SlotSpec("d01", W, "SEAL:critical_safety_event",
                     inject=frozenset({"extra_side_effect"})),
            SlotSpec("d02", W, "NOT_RUN")], playwright)
        batches["interrupted"] = interrupted_probe(ctx, playwright)
        batches["settled_turn"] = settled_turn_probe(ctx, playwright)
        batches["faults_provider"] = run_batch(ctx, "p74a-faults-provider", [
            SlotSpec("d01", W, "REPLACED_PASS", faults={1: "http_503"}),
            SlotSpec("d02", W, "CAP_SEAL", faults={2: "connection_lost"})], playwright)
        batches["faults_harness"] = run_batch(ctx, "p74a-faults-harness", [
            SlotSpec("d01", W, "REPLACED_PASS", inject=frozenset({"evaluator_exit"})),
            SlotSpec("d07", W, "CAP_SEAL", inject=frozenset({"browser_crash"}))], playwright)
        batches["faults_prestart"] = run_batch(ctx, "p74a-faults-prestart", [
            SlotSpec("d01", W, "REPLACED_PASS", inject=frozenset({"baseline_drift"})),
            # Replacement caps are per arm, so the cap probe stays on the same arm.
            SlotSpec("d02", W, "CAP_SEAL", inject=frozenset({"identity_before_start"}))],
            playwright)
        batches["seal_model"] = run_batch(ctx, "p74a-seal-model-string", [
            SlotSpec("d01", W, "SEAL:model_identity_changed", models={2: "p74-scripted/v2"}),
            SlotSpec("d02", W, "NOT_RUN")], playwright)

        def stray(gateway: ProviderGateway, _index: int) -> None:
            request = urllib.request.Request(
                gateway.base_url + "/chat/completions", method="POST",
                data=json.dumps({"model": "qwen3.8-flash", "messages": []}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + secrets.token_urlsafe(32)})
            try:
                urllib.request.urlopen(request, timeout=10)
            except urllib.error.HTTPError:
                pass

        batches["seal_unowned"] = run_batch(ctx, "p74a-seal-unowned-request", [
            SlotSpec("d01", W, "PASS"), SlotSpec("d02", W, "SEAL:unowned_model_request")],
            playwright, between=stray)
        _, d16 = ctx.inputs("d16")
        wrong_oracle = {**d16.approval_oracle,
                        "targets": [d16.approval_oracle["targets"][0], "邮箱：wrong@example.test"]}
        batches["seal_safety"] = run_batch(ctx, "p74a-seal-safety", [
            SlotSpec("d16", W, "SEAL:critical_safety_event", oracle_override=wrong_oracle),
            SlotSpec("d01", W, "NOT_RUN")], playwright)
        batches["budget"] = run_batch(ctx, "p74a-budget-refusal", [
            SlotSpec("d01", W, "FAIL")], playwright,
            budgets=protocol_budgets(max_model_calls=2))
        broken = ctx.root / "reset-probes" / "reset-fail-recovery"
        shutil.rmtree(broken, ignore_errors=True)
        shutil.copytree(PRIVATE / "recovery", broken)
        (broken / "baseline-filestore.tar").write_bytes(b"not an archive")
        batches["seal_reset"] = run_batch(ctx, "p74a-seal-reset-failure", [
            SlotSpec("d16", W, "SEAL:reset_failed"), SlotSpec("d01", W, "NOT_RUN")], playwright,
            recovery_dir=broken)
        result["post_reset_failure_restore"] = _restore(ctx, PRIVATE / "recovery", rearm=True)[
            "status"]
        batches["domain_mutation"] = run_batch(ctx, "p74a-domain-mutation", [
            SlotSpec("d08", W, "FAIL")], playwright, stack=mutation_stack(ctx))
        # Typed provider failures, classified from the gateway's records on either arm.
        batches["typed_rate_limit"] = run_batch(ctx, "p74a-typed-rate-limit", [
            SlotSpec("d01", W, "REPLACED_PASS", faults={1: "http_429"})], playwright)
        batches["typed_rate_limit_pageagent"] = run_batch(
            ctx, "p74a-typed-rate-limit-pageagent",
            [SlotSpec("d01", A, "REPLACED_PASS", faults={1: "http_429"})], playwright)
        batches["typed_response_contract"] = run_batch(ctx, "p74a-typed-response-contract", [
            SlotSpec("d01", W, "REPLACED_PASS", faults={1: "malformed_response"})], playwright)
        batches["typed_context_length"] = run_batch(ctx, "p74a-typed-context-length", [
            SlotSpec("d01", W, "FAIL", faults={1: "context_length"})], playwright)
        # A write task the product never wrote. The readback fails it; the confirmed orders
        # that did not move are that and not a write to the wrong target. The rehearsal met
        # this only when its schedule put a write task under the drifted answer, so it is
        # exercised here on every run.
        batches["write_not_done"] = run_batch(ctx, "p74a-write-not-done", [
            SlotSpec("d20", W, "FAIL", faults={1: "context_length"})], playwright)
        batches["typed_credential"] = run_batch(ctx, "p74a-typed-credential", [
            SlotSpec("d01", W, "SEAL:provider_authentication_failed", faults={1: "http_401"}),
            SlotSpec("d02", W, "NOT_RUN")], playwright)
        # A provider's HTTP 408 is its request deadline: the product's scorable ending on either
        # arm, never an outage to replace.
        batches["typed_deadline"] = run_batch(ctx, "p74a-typed-deadline", [
            SlotSpec("d01", W, "FAIL", faults={1: "http_408"})], playwright)
        batches["typed_deadline_pageagent"] = run_batch(ctx, "p74a-typed-deadline-pageagent", [
            SlotSpec("d01", A, "FAIL", faults={1: "http_408"})], playwright)
        # The product's 2,048-token cap exceeds this gateway's, so every request breaks it.
        batches["typed_request_contract"] = run_batch(ctx, "p74a-typed-request-contract", [
            SlotSpec("d01", W, "SEAL:request_contract_violation"), SlotSpec("d02", W, "NOT_RUN")],
            playwright, budgets=protocol_budgets(max_output_tokens=1024))
        # A turn still waiting for the provider when the attempt's wall clock runs out is stopped
        # through the panel, and its stop feedback is timed.
        batches["attempt_wall"] = run_batch(ctx, "p74a-attempt-wall", WALL_SPECS, playwright,
                                            attempt_wall_s=8)
    result["batches"] = {name: {"ledger": batch["ledger"], "status": batch["summary"]["status"],
                                "seal_reason": batch["summary"]["seal_reason"],
                                "slots": selected_outcomes(batch)}
                         for name, batch in batches.items()}
    metadata = {task["task_id"]: {"class": task["class"], "stratum": task["stratum"],
                                  "read_only": task["max_mutations"] == 0}
                for task in ctx.task_set["tasks"]}
    # Every qualification batch starts its own chain, so each gets its own report.
    for batch in batches.values():
        report.verify_report(report.build_report(batches=[batch["summary"]], tasks=metadata))
    result["report_verified"] = True
    result["spend_ledgers_untouched"] = spend_ledger_fingerprints() == spend_before
    result["checks"] = evaluate_checks(result, batches)
    result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result["status"] = "READY" if all(result["checks"].values()) else "FAILED"
    # Every run keeps its own artifact; an earlier run's record is never overwritten.
    target = ctx.root / f"p74a-qualification-{started.replace('-', '').replace(':', '')}.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True,
                                default=str) + "\n")
    return {**result, "artifact": str(target.relative_to(REPOSITORY))}


# -- the held-out fixture ----------------------------------------------------------------------------

HELDOUT_KIND = "p74c_heldout_fixture_qualification"


def _giveup_plan(ctx: Context, task_id: str, arm: str, slot: dict[str, Any]) -> AttemptPlan:
    """An attempt whose scripted model gives up at once. It reads nothing of the task: the
    held-out fixture is qualified without any program written for a held-out task."""
    runner, evaluator = ctx.inputs(task_id)
    policy = (S.WoaProgram({}).policy() if arm == "woa_product"
              else S.PageAgentProgram(()).policy())
    return AttemptPlan(slot=slot, runner_input=runner, evaluator_input=evaluator, policy=policy)


def _truth_reached(ctx: Context, task_id: str, directory: Path) -> bool:
    """Whether the evaluator process reaches expected truth for one task on this fixture. What
    it computed stays in the private directory; only whether it did is recorded."""
    _, evaluator = ctx.inputs(task_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / "evaluator-job.json").write_text(json.dumps({
        "evaluator_input": dataclasses.asdict(evaluator),
        "scope": {"attempt_id": "truth", "run_id": "truth", "arm": "woa_product"},
        "rpc_url": ctx.stack.rpc_url,
        "actor_key_files": {role: str(path) for role, path in ctx.keys.items()}}))
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONPATH"] = str(REPOSITORY)
    completed = subprocess.run(
        [sys.executable, "-m", "evals.odoo.judging.evaluator", "truth",
         "--job", str(directory / "evaluator-job.json"),
         "--output", str(directory / "evaluator-truth.json")],
        cwd=REPOSITORY, env=env, capture_output=True, timeout=300, check=False)
    return completed.returncode == 0 and (directory / "evaluator-truth.json").exists()


def heldout_run(ctx: Context) -> dict[str, Any]:
    """The candidate's zero-model qualification on the held-out fixture.

    What it asks needs no knowledge of any held-out task: the task set matches its seal, the
    running fixture is the held-out population at its baseline, every task instantiates, the
    evaluator reaches truth for every one, and give-up attempts on both arms and on one task
    that needs a reset run with the apparatus intact and the reset passing. Only counts, the
    task ids it picked and digests are recorded; a scripted give-up says nothing about the
    product.
    """
    from playwright.sync_api import sync_playwright

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    spend_before = spend_ledger_fingerprints()
    candidate = ctx.stack.verify_candidate()
    bindings = batch_bindings(ctx)
    manifest = json.loads(fixture_manifest_path(ctx.private_dir).read_text())
    recovery = fixture.recovery_record(ctx.private_dir / "recovery")
    seal = json.loads(harness.HELDOUT_SEAL.read_text())
    ids = sorted(task["task_id"] for task in ctx.task_set["tasks"])
    instantiated = 0
    for task_id in ids:
        ctx.inputs(task_id)
        instantiated += 1
    truth_root = ctx.root / "heldout-truth" / started.replace("-", "").replace(":", "")
    reached = sum(_truth_reached(ctx, task_id, truth_root / task_id) for task_id in ids)
    eligible = protocol.pageagent_eligibility(ctx.task_set)
    first = (eligible or ids)[0]
    reset_task = next((task["task_id"] for task in sorted(ctx.task_set["tasks"],
                                                          key=lambda item: item["task_id"])
                       if task["reset_profile"] != "none"), None)
    picks = [(first, "woa_product")]
    if eligible:
        picks.append((first, "pageagent_browser_only"))
    if reset_task is not None:
        picks.append((reset_task, "woa_product"))
    specs = [SlotSpec(task_id, arm, "GIVE_UP") for task_id, arm in picks]
    path = create_batch(ctx, "p74c-heldout-fixture", specs)
    with sync_playwright() as playwright, open_driver(ctx, path, playwright) as driver:
        for spec, slot in zip(specs, ledger.read(path)[1]["slots"]):
            fields = _slot_fields(slot)
            summary = driver.run_slot(_giveup_plan(ctx, spec.task_id, spec.arm, fields),
                                      replacement=_giveup_plan(ctx, spec.task_id, spec.arm,
                                                               fields))
            if summary["status"] != "OPEN":
                break
    summary = finish_batch(path)
    attempts = summary["attempts"]
    checks = {
        "heldout_task_set_sealed": bindings["task_set_seal_digest"] == seal["seal_digest"],
        "fixture_is_the_heldout_population": (manifest["population_seed_commitment"]
                                              == seal["population_seed_commitment"]),
        "fixture_stable": manifest["stability_proof"]["status"] == "READY",
        "fixture_at_its_baseline": (recovery["status"] == "READY"
                                    and bindings["fixture"]["baseline_guard_digest"]
                                    == recovery["baseline_digest"]),
        "every_task_instantiates": instantiated == len(ids) == protocol.TASK_COUNT,
        "evaluator_reaches_every_truth": reached == len(ids),
        "give_up_batch_complete": summary["status"] == "COMPLETE",
        "reset_task_present": reset_task is not None,
        "give_up_attempts_valid": bool(attempts) and all(
            item["apparatus_integrity"] == "VALID" and not item["unclassified"]
            and item["reset_outcome"] in {"NOT_REQUIRED", "PASS"} and not item["safety_events"]
            for item in attempts),
        "reset_ran_on_the_heldout_baseline": any(
            item["task_id"] == reset_task and item["reset_outcome"] == "PASS"
            for item in attempts),
        "scripted_model_only": summary["provider_model"] in {None, SCRIPTED_MODEL},
        "spend_ledgers_untouched": spend_ledger_fingerprints() == spend_before,
    }
    result = {
        "artifact_version": 1, "kind": HELDOUT_KIND, "started_at": started,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "external_model_calls": 0, "candidate": candidate,
        "fixture_manifest_digest": manifest["manifest_digest"],
        "protocol_digest": protocol.document()["protocol_digest"],
        "counts": {"tasks": len(ids), "instantiated": instantiated, "truth_reached": reached},
        "give_up": {"ledger": str(path.relative_to(REPOSITORY)), "picks": [list(item)
                                                                          for item in picks],
                    "attempts": len(attempts), "status": summary["status"]},
        "checks": checks, "status": "READY" if all(checks.values()) else "FAILED",
    }
    target = ctx.root / f"p74c-heldout-qualification-{started.replace('-', '').replace(':', '')}.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return {**result, "artifact": str(target.relative_to(REPOSITORY))}


from evals.odoo.qualification.checks import (
    _selected, _wall_clock_ok, evaluate_checks, feedback_checks,
)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.odoo.qualify")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    child = commands.add_parser("child-attempt")
    child.add_argument("ledger")
    child.add_argument("index", type=int)
    child.add_argument("--probe", choices=("interrupted", "settled"), default="interrupted")
    sample = commands.add_parser("sample")
    sample.add_argument("task_id")
    sample.add_argument("--arm", default="woa_product",
                        choices=["woa_product", "pageagent_browser_only"])
    sample.add_argument("--variant", default="")
    commands.add_parser("heldout", help="qualify the running candidate on the held-out fixture")
    commands.add_parser("smoke", help="the qualification a product-only change needs")
    args = parser.parse_args(argv)
    if args.command == "heldout":
        result = heldout_run(load_context(population="heldout"))
        print(json.dumps({"status": result["status"], "artifact": result["artifact"],
                          "checks": result["checks"]}, indent=1))
        return 0 if result["status"] == "READY" else 1
    ctx = load_context()
    if args.command == "smoke":
        result = smoke_run(ctx)
        print(json.dumps({"status": result["status"], "artifact": result["artifact"],
                          "checks": result["checks"]}, indent=1))
        return 0 if result["status"] == "READY" else 1
    if args.command == "child-attempt":
        return child_attempt(ctx, Path(args.ledger), args.index, args.probe)
    if args.command == "run":
        result = run(ctx)
        print(json.dumps({"status": result["status"], "artifact": result["artifact"],
                          "checks": result["checks"]}, indent=1))
        return 0 if result["status"] == "READY" else 1
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        batch = run_batch(ctx, f"debug-{args.task_id}",
                          [SlotSpec(args.task_id, args.arm, "debug", variant=args.variant)],
                          playwright)
    print(json.dumps({"status": batch["summary"]["status"],
                      "slots": selected_outcomes(batch)}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
