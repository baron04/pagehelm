"""Crash, reset and mutation probes used by zero-model qualification."""
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

from evals.odoo.qualify import (
    SlotSpec, _plan, _slot_fields, batch_record, create_batch, open_driver,
)

ADAPTER_IN_IMAGE = "/usr/local/lib/python3.12/site-packages/web_operation_agent_odoo/adapter.py"
QUOTATION_DOMAIN = 'domain=[("state", "in", ("draft", "sent"))],'
MUTATED_QUOTATION_DOMAIN = 'domain=[("state", "in", ("draft", "sent", "sale"))],'

INTERRUPTED_SPECS = [SlotSpec("d01", "woa_product", "INTERRUPTED_THEN_REPLACED"),
                     SlotSpec("d02", "woa_product", "PASS")]


def _child_env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": os.pathsep.join(
        str(REPOSITORY / item) for item in ("src", "examples/odoo/src", "."))}


#: A driver killed once its first turn was read and written down: the recovered attempt is judged
#: from that turn, and this one's answer lacks a field, so the product's failure stands.
SETTLED_SPECS = [SlotSpec("d01", "woa_product", "FAIL", variant="missing_field"),
                 SlotSpec("d02", "woa_product", "PASS")]


def child_attempt(ctx: Context, path: Path, index: int, probe: str = "interrupted") -> int:
    """Start one scheduled attempt in this process and die where ``probe`` says: once its
    product has started, or once its first turn was read and written down."""
    from playwright.sync_api import sync_playwright

    specs, inject = ((INTERRUPTED_SPECS, "kill_after_product_start") if probe == "interrupted"
                     else (SETTLED_SPECS, "kill_after_turn_settled"))
    slots = ledger.read(path)[1]["slots"]
    spec = dataclasses.replace(specs[index], inject=frozenset({inject}))
    with sync_playwright() as playwright, open_driver(ctx, path, playwright) as driver:
        driver.run_attempt(_plan(ctx, spec, _slot_fields(slots[index])))
    return 0


def interrupted_probe(ctx: Context, playwright: Any) -> dict[str, Any]:
    """A driver process killed mid-attempt is recovered, replaced once, and the batch goes on."""
    path = create_batch(ctx, "p74a-interrupted-attempt", INTERRUPTED_SPECS)
    child = subprocess.run([sys.executable, "-m", "evals.odoo.qualify", "child-attempt",
                            str(path), "0"], cwd=REPOSITORY, env=_child_env(),
                           capture_output=True, timeout=900)
    # The dead driver's Agent may still be mid-turn. Cutting its provider path keeps a late
    # request from reaching the next gateway, while the Agent stays up for its audit.
    ctx.stack.compose("stop", "provider-relay", timeout=120)
    with open_driver(ctx, path, playwright) as driver:
        recovered = driver.recover_open_attempt()
        slots = ledger.read(path)[1]["slots"]
        interrupted = recovered["attempts"][-1] if recovered else None
        if interrupted is not None and interrupted["replacement_eligible"]:
            plan = _plan(ctx, INTERRUPTED_SPECS[0], _slot_fields(slots[0]))
            plan.replacement_of = interrupted["attempt_id"]
            driver.run_attempt(plan)
        if not ledger.read(path)[1]["pending_seal"]:
            driver.run_slot(_plan(ctx, INTERRUPTED_SPECS[1], _slot_fields(slots[1])))
    record = batch_record(path, finish_batch(path), INTERRUPTED_SPECS)
    record["child_exit_status"] = child.returncode
    return record


def settled_turn_probe(ctx: Context, playwright: Any) -> dict[str, Any]:
    """A driver killed after its first turn was read: recovery judges the turn it wrote down.

    Protocol section 7 named this as a limitation to fix before the held-out set: the visible
    answer reached the evidence only when the runner returned, so a killed driver lost every
    answer it had read and the attempt was replaced whatever it showed.
    """
    path = create_batch(ctx, "p74c-settled-turn", SETTLED_SPECS)
    child = subprocess.run([sys.executable, "-m", "evals.odoo.qualify", "child-attempt",
                            str(path), "0", "--probe", "settled"], cwd=REPOSITORY,
                           env=_child_env(), capture_output=True, timeout=900)
    ctx.stack.compose("stop", "provider-relay", timeout=120)
    with open_driver(ctx, path, playwright) as driver:
        recovered = driver.recover_open_attempt()
        slots = ledger.read(path)[1]["slots"]
        interrupted = recovered["attempts"][-1] if recovered else None
        if interrupted is not None and interrupted["replacement_eligible"]:
            plan = _plan(ctx, SETTLED_SPECS[0], _slot_fields(slots[0]))
            plan.replacement_of = interrupted["attempt_id"]
            driver.run_attempt(plan)
        if not ledger.read(path)[1]["pending_seal"]:
            driver.run_slot(_plan(ctx, SETTLED_SPECS[1], _slot_fields(slots[1])))
    record = batch_record(path, finish_batch(path), SETTLED_SPECS)
    record["child_exit_status"] = child.returncode
    return record


# -- reset rejection probes ----------------------------------------------------------------------

def _restore(ctx: Context, directory: Path, *, stack: Stack | None = None,
             rearm: bool = False) -> dict[str, Any]:
    stack = stack or ctx.stack
    return fixture.restore(stack, fixture.FixtureGuard(stack), directory,
                           actor_projection=lambda: actor_projection(stack.rpc_url, ctx.keys),
                           rearm_after_failure=rearm)


def reset_probes(ctx: Context) -> dict[str, Any]:
    real = PRIVATE / "recovery"
    probes = ctx.root / "reset-probes"
    shutil.rmtree(probes, ignore_errors=True)
    results: dict[str, Any] = {}

    def copy(name: str) -> Path:
        target = probes / name
        shutil.copytree(real, target)
        return target

    wrong = copy("wrong-archive")
    data = bytearray((wrong / "baseline.pgdump").read_bytes())
    data[-1] ^= 0xFF
    (wrong / "baseline.pgdump").write_bytes(bytes(data))
    results["wrong_archive"] = _restore(ctx, wrong)
    results["wrong_project"] = _restore(
        ctx, copy("wrong-project"), stack=dataclasses.replace(ctx.stack,
                                                              project="woa-p74-not-this-project"))

    # Missing object: an archive that restores cleanly but lacks one customer.
    missing = copy("missing-object")
    victim = next(item for item in ctx.population["customers"]
                  if item["key"] not in ctx.population["roles"].values()
                  and item["company"] == "main" and item["customer_rank"] > 0)
    partner_id = ctx.seed_ids["partners"][victim["key"]]
    ctx.stack.psql(f"delete from res_partner where id = {int(partner_id)}")
    archive = ctx.stack.run(["docker", "exec", ctx.stack.container("db"), "pg_dump", "-U",
                             "odoo", "-d", "woa_trial", "--format=custom", "--no-owner",
                             "--no-privileges"], timeout=900).stdout
    (missing / "baseline.pgdump").write_bytes(archive)
    record = json.loads((missing / "recovery.json").read_text())
    record["database_archive"] = {"sha256": "sha256:" + hashlib.sha256(archive).hexdigest(),
                                  "bytes": len(archive)}
    record = fixture._seal(record)
    (missing / "recovery.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    results["missing_object"] = _restore(ctx, missing)

    # Half restore: the database comes back, the attachment objects do not.
    half = copy("half-restore")
    empty = ctx.stack.compose("run", "--rm", "--no-deps", "--entrypoint", "sh", "odoo", "-c",
                              "mkdir -p /tmp/empty/woa_trial && tar -C /tmp/empty -cf - "
                              "woa_trial").stdout
    (half / "baseline-filestore.tar").write_bytes(empty)
    record = json.loads((half / "recovery.json").read_text())
    record["filestore_archive"] = {"sha256": "sha256:" + hashlib.sha256(empty).hexdigest(),
                                   "bytes": len(empty)}
    record = fixture._seal(record)
    (half / "recovery.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    results["half_restore"] = _restore(ctx, half)

    # An interrupted reset leaves an uncertain record: refused until an operator re-arms it, and
    # then restored and verified like any other.
    uncertain = copy("uncertain-reset")
    record = fixture._seal({**json.loads((uncertain / "recovery.json").read_text()),
                            "status": "INDETERMINATE"})
    (uncertain / "recovery.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    results["uncertain_refused"] = _restore(ctx, uncertain)
    results["uncertain_rearmed"] = _restore(ctx, uncertain, rearm=True)

    # Positive control: a real write, put back by the real baseline.
    writer = OdooActorClient(base_url=ctx.stack.rpc_url, api_key_file=ctx.keys["writer"])
    writer.execute("res.partner", "create", [{"name": "P74 Reset Probe Customer",
                                              "customer_rank": 1}])
    results["positive_reset"] = _restore(ctx, real, rearm=True)
    results["repeat_reset"] = _restore(ctx, real)
    expected = {"wrong_archive": "FAIL", "wrong_project": "FAIL", "missing_object": "FAIL",
                "half_restore": "FAIL", "uncertain_refused": "INDETERMINATE",
                "uncertain_rearmed": "PASS", "positive_reset": "PASS", "repeat_reset": "PASS"}
    return {"results": {key: {"status": value["status"], "basis": value.get("basis", [])}
                        for key, value in results.items()},
            "as_expected": all(results[key]["status"] == value for key, value in expected.items())}


# -- mutation probe ------------------------------------------------------------------------------

def mutation_stack(ctx: Context) -> Stack:
    source = ctx.stack.compose("run", "--rm", "--no-deps", "--entrypoint", "cat", "agent",
                               ADAPTER_IN_IMAGE).stdout.decode("utf-8")
    if source.count(QUOTATION_DOMAIN) != 1:
        raise RuntimeError("the adapter no longer contains the mutated quotation domain")
    directory = ctx.root / "mutation"
    directory.mkdir(mode=0o700, exist_ok=True)
    mutated = directory / "adapter.py"
    mutated.write_text(source.replace(QUOTATION_DOMAIN, MUTATED_QUOTATION_DOMAIN))
    os.chmod(mutated, 0o644)
    overlay = directory / "compose.mutation.yml"
    overlay.write_text("services:\n  agent:\n    volumes:\n"
                       f"      - {mutated}:{ADAPTER_IN_IMAGE}:ro\n")
    return dataclasses.replace(ctx.stack, extra_compose=(overlay,))


def spend_ledger_fingerprints() -> dict[str, str | None]:
    """The project's two spend ledgers, so a run can show it wrote to neither."""
    from web_operation_agent.modeling.client import DEFAULT_LEDGER

    ledgers = {"default": Path(os.environ.get("WOA_SPEND_LEDGER") or DEFAULT_LEDGER),
               "acceptance": REPOSITORY / ".artifacts" / "acceptance" / "spend.jsonl"}
    return {name: ("sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                   if path.exists() else None) for name, path in ledgers.items()}


