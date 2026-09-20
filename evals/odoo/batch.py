"""Run one evaluation batch on the live stack, account every paid call, and report it.

A batch needs four things, and this module puts them together:

* a candidate built from this clean checkout and qualified without a model on the fixture now in
  place;
* one attempt ledger, whose header binds the candidate, the fixture, the task set and the
  schedule before the first attempt starts;
* the driver, taking the slots in schedule order and closing an interrupted attempt first;
* the report, recomputed from that ledger alone.

Every call to the provider goes through the provider backend, which writes it to the spend ledger
when it settles. A call whose cost nobody knows holds the batch until it is accounted for: by its
own late answer when that answer carried usage, or by the owner's reconciliation against the
provider bill.

A held-out batch keeps what protects its conclusion: the task set was sealed before anyone saw a
result, the schedule is derived from that seal and the candidate commit so nobody chooses it, a
candidate runs the held-out set once (an exogenous stop is continued, never rerun), and while it
runs nothing but its progress is shown.

    python -m evals.odoo.batch run --confirm-paid-calls [--population heldout]
    python -m evals.odoo.batch status
    python -m evals.odoo.batch stop
    python -m evals.odoo.batch rearm --confirm-rearm
    python -m evals.odoo.batch report <batch id>
    python -m evals.odoo.batch reconcile <batch id> --owner NAME --billed-calls N
        --billed-cost-cny X --evidence-digest sha256:...
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.odoo.domain import protocol, tasks
from evals.odoo.execution import harness
from evals.odoo.fixture import fixture, revision
from evals.odoo.reporting import ledger, report
from evals.odoo.execution.driver import AttemptPlan
from evals.odoo.provider.gateway import (UpstreamBackend, priced_late_answers, provider_binding_digest,
                      read_evaluation_key, upstream_identity)
from evals.odoo.domain.population import digest_json
from evals.odoo.fixture.projection import actor_projection
from evals.odoo.fixture.stack import StackError

POPULATIONS = protocol.POPULATIONS


from evals.odoo.batch_setup import (
    BatchError, BatchPaths, binding_digest, header, plan, provider_backend,
    provider_settings, qualification, task_set,
)

# -- the batches on disk ---------------------------------------------------------------------------
Batch = tuple[Path, list[dict[str, Any]], dict[str, Any]]


def batches(paths: BatchPaths) -> list[Batch]:
    """Every batch this root holds, oldest first."""
    found = []
    for path in sorted(paths.ledgers.glob("*.jsonl")):
        try:
            entries, summary = ledger.read(path)
        except (OSError, ValueError) as exc:
            raise BatchError(f"{path.name} cannot be read: {exc}") from exc
        found.append((path, entries, summary))
    return found


def open_batch(paths: BatchPaths) -> Batch | None:
    """The one batch a previous run left open, if any; it is resumed, never restarted."""
    found = [item for item in batches(paths) if item[2]["status"] == "OPEN"]
    if len(found) > 1:
        raise BatchError("more than one batch is open: " + ", ".join(
            item[0].stem for item in found))
    return found[0] if found else None


def heldout_may_run(paths: BatchPaths, commit: str) -> None:
    """A candidate runs the held-out set once: a new held-out batch of it continues only a run
    that stopped for a reason the product did not cause."""
    earlier = [item for item in batches(paths) if item[2]["batch"]["purpose"] == "heldout"
               and item[2]["batch"]["candidate"]["commit"] == commit]
    if not earlier:
        return
    last = earlier[-1][2]
    if last["status"] == "COMPLETE" or last["seal_reason"] not in protocol.HELDOUT_CONTINUES_AFTER:
        raise BatchError(f"{last['batch']['batch_id']} already ran the held-out set on this "
                         "candidate; a new conclusion needs a new candidate or a new held-out set")


# -- calls whose cost is unknown ----------------------------------------------------------------
def _reconciled(paths: BatchPaths, batch_id: str) -> set[str]:
    """Calls the owner's reconciliations of this batch cover."""
    covered: set[str] = set()
    for path in sorted((paths.reconciliations / batch_id).glob("*.json")):
        try:
            covered.update(json.loads(path.read_text(encoding="utf-8"))["covered_calls"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BatchError(f"{path.name} cannot be read: {exc}") from exc
    return covered


def owed(paths: BatchPaths, item: Batch) -> list[str]:
    """The calls of one batch whose cost nobody knows: a call the spend ledger could not record,
    and a call left unresolved whose late answer did not price it. A reconciliation or a priced
    late answer covers it."""
    path, entries, _ = item
    names = {f"{entry['body']['attempt_id']}#{entry['body']['request_seq']}"
             for entry in entries
             if (entry["kind"] == "model_response_received"
                 and entry["body"]["status"] == "accounting_error")
             or entry["kind"] == "model_response_unresolved"}
    names -= set(priced_late_answers(path))
    names -= {name for entry in entries if entry["kind"] == "billing_reconciliation_applied"
              for name in entry["body"]["covered_calls"]}
    names -= _reconciled(paths, item[2]["batch"]["batch_id"])
    return sorted(names)


def lift_hold(paths: BatchPaths, item: Batch) -> None:
    """Apply what accounts for the calls an open batch holds for: their own priced late answers,
    and the owner's reconciliations."""
    path, _, summary = item
    held = set(summary["cost_unknown"])
    priced = {name: record for name, record in priced_late_answers(path).items()
              if name in held}
    batch_id = summary["batch"]["batch_id"]
    if priced:
        ledger.append(path, "billing_reconciliation_applied", {
            "covered_calls": sorted(priced),
            "reconciliation_digest": digest_json({"kind": "late_answers", "batch_id": batch_id,
                                                  "answers": [priced[name]
                                                              for name in sorted(priced)]}),
            "coverage_digest": digest_json({"batch_id": batch_id,
                                            "covered_calls": sorted(priced)})})
    covered = sorted((held - set(priced)) & _reconciled(paths, batch_id))
    if covered:
        ledger.append(path, "billing_reconciliation_applied", {
            "covered_calls": covered,
            "reconciliation_digest": digest_json({"batch_id": batch_id,
                                                  "records": sorted(
                                                      p.name for p in (paths.reconciliations
                                                                       / batch_id).glob("*.json"))}),
            "coverage_digest": digest_json({"batch_id": batch_id, "covered_calls": covered})})


def reconcile(paths: BatchPaths, batch_id: str, *, owner: str, billed_calls: int,
              billed_cost_cny: float, evidence_digest: str, notes: list[str] = ()) -> dict[str, Any]:
    """Record the owner's reconciliation of a batch's unaccounted calls against the provider
    bill. It keeps only the billed count and cost and a digest of the bill export."""
    item = next((entry for entry in batches(paths)
                 if entry[2]["batch"]["batch_id"] == batch_id), None)
    if item is None:
        raise BatchError(f"no batch {batch_id}")
    calls = owed(paths, item)
    if not calls:
        raise BatchError(f"{batch_id} owes no bill")
    if (not isinstance(evidence_digest, str) or not evidence_digest.startswith("sha256:")
            or len(evidence_digest) != 71):
        raise BatchError("the bill is named by the SHA-256 of its export, never by its content")
    if billed_calls < 0 or not 0 <= billed_cost_cny < float("inf"):
        raise BatchError("the billed calls and cost are non-negative")
    body = {"batch_id": batch_id, "covered_calls": calls, "owner": owner,
            "billed_calls": billed_calls, "billed_cost_cny": billed_cost_cny,
            "evidence_digest": evidence_digest, "notes": list(notes),
            "reconciled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    record = {**body, "digest": digest_json(body)}
    target = paths.reconciliations / batch_id / f"{record['digest'][7:23]}.json"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"record": str(target), "covered_calls": calls}


# -- running -----------------------------------------------------------------------------------
@contextmanager
def lock(paths: BatchPaths) -> Iterator[None]:
    """Hold the one paid process of this root; a second one is refused."""
    paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BatchError("another batch process is using this root") from exc
        yield
    finally:
        os.close(descriptor)


def running(paths: BatchPaths) -> bool:
    """Whether a batch process holds this root's lock; nothing is changed."""
    try:
        descriptor = os.open(paths.lock, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(descriptor)
    return False


def _stop_requested(paths: BatchPaths, batch_id: str) -> bool:
    try:
        return json.loads(paths.stop_request.read_text(encoding="utf-8")).get(
            "batch_id") == batch_id
    except (OSError, ValueError, AttributeError):
        return False


def _attempt_plan(ctx: harness.Context, slot: dict[str, Any]) -> AttemptPlan:
    runner, evaluator = ctx.inputs(slot["task_id"])
    return AttemptPlan(slot={key: slot[key] for key in ("slot_id", "arm", "task_id", "replicate")},
                       runner_input=runner, evaluator_input=evaluator)


def _seal_if_owed(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    if summary["disposition"] == "must_seal":
        return ledger.append(path, "batch_sealed", {"reason": summary["pending_seal"]})
    return summary


def drive(paths: BatchPaths, ctx: harness.Context, path: Path, backend: Any,
          playwright: Any) -> dict[str, Any]:
    """Run the remaining slots in schedule order.

    An attempt a previous run left open is closed from its evidence first, and the slot it ran
    gets its replacement if the ledger says it is owed one. A requested stop seals the batch
    before the next attempt; a batch whose ledger stops accepting attempts -- sealed, complete,
    or holding for a cost nobody knows -- stops here.
    """
    batch_id = ledger.read(path)[1]["batch"]["batch_id"]

    def stopped(summary: dict[str, Any]) -> bool:
        if summary["status"] == "OPEN" and _stop_requested(paths, batch_id):
            ledger.append(path, "batch_sealed",
                          {"reason": summary["pending_seal"] or "operator_stop"})
            return True
        return False

    with harness.open_driver(ctx, path, playwright, backend=backend) as runner:
        summary = ledger.read(path)[1]
        if summary["open_attempt"] is not None:
            summary = _seal_if_owed(path, runner.recover_open_attempt())
        last = summary["attempts"][-1] if summary["attempts"] else None
        if (last is not None and summary["disposition"] == "running"
                and last["replacement_eligible"] and last["replacement_of"] is None
                and not stopped(summary)):
            slot = next(item for item in summary["slots"] if item["slot_id"] == last["slot_id"])
            attempt = _attempt_plan(ctx, slot)
            attempt.replacement_of = last["attempt_id"]
            _seal_if_owed(path, runner.run_attempt(attempt))
        for slot in ledger.read(path)[1]["slots"]:
            current = ledger.read(path)[1]
            if next(item for item in current["slots"]
                    if item["slot_id"] == slot["slot_id"])["attempts"]:
                continue
            if current["disposition"] != "running" or stopped(current):
                break
            runner.run_slot(_attempt_plan(ctx, slot))
    summary = harness.finish_batch(path)
    if summary["status"] != "OPEN":
        paths.stop_request.unlink(missing_ok=True)
    return summary


def run(paths: BatchPaths, *, confirm_paid_calls: bool,
        population: str = "development") -> dict[str, Any]:
    """Check the candidate, open or resume the batch, drive it, and write its report."""
    if not confirm_paid_calls:
        raise BatchError("a batch makes paid model calls; pass --confirm-paid-calls")
    if population not in POPULATIONS:
        raise BatchError("a batch runs the development or the held-out split")
    with lock(paths):
        return _run(paths, population)


def _run(paths: BatchPaths, population: str) -> dict[str, Any]:
    try:
        head = revision.require_clean_head(paths.repository)["commit"]
    except revision.RevisionError as exc:
        raise BatchError(str(exc)) from exc
    resumed = open_batch(paths)
    if resumed is not None and resumed[2]["batch"]["purpose"] != population:
        raise BatchError(f"{resumed[0].stem} is an open {resumed[2]['batch']['purpose']} batch; "
                         "finish or stop it first")
    unpaid = {item[2]["batch"]["batch_id"]: owed(paths, item) for item in batches(paths)
              if item is not resumed and item[2]["status"] != "OPEN"}
    unpaid = {batch_id: calls for batch_id, calls in unpaid.items() if calls}
    if unpaid:
        raise BatchError("calls whose cost is unknown await the provider bill ("
                         + ", ".join(sorted(unpaid)) + "); run reconcile")
    try:
        recovery = fixture.recovery_record(paths.private_dir / "recovery")
    except (OSError, ValueError, StackError) as exc:
        raise BatchError(f"the fixture recovery record cannot be read: {exc}") from exc
    if recovery["status"] != "READY":
        # A reset that failed or never finished leaves the fixture uncertain: nothing runs on it
        # until it is restored and verified.
        raise BatchError(f"the fixture recovery record is {recovery['status']}; restore the "
                         "baseline with rearm before a batch runs")
    ctx = harness.load_context(paths.root, private_dir=paths.private_dir, population=population)
    try:
        bindings = harness.batch_bindings(ctx)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        raise BatchError(f"the candidate, fixture manifest or task set does not verify: {exc}") \
            from exc
    if bindings["candidate"]["commit"] != head:
        raise BatchError("the running candidate was not built from this checkout")
    qualification(paths, bindings["candidate"], bindings["fixture"]["manifest_digest"])
    tasks_run, _ = task_set(paths, population)
    paths.ledgers.mkdir(parents=True, exist_ok=True)
    if resumed is not None:
        path, entries, summary = resumed
        batch_header = entries[0]["body"]
        # A resumed batch keeps the bindings it started with; it does not continue on another
        # candidate, fixture or provider.
        expected = header(batch_id=batch_header["batch_id"], population=population,
                          bindings=bindings, tasks_run=tasks_run,
                          provider_binding=binding_digest(paths))
        if batch_header != expected:
            raise BatchError(f"{path.stem} is open under other bindings; it cannot be resumed "
                             "on this candidate, fixture or provider")
        if summary["cost_unknown"]:
            lift_hold(paths, resumed)
            if ledger.read(path)[1]["cost_unknown"]:
                raise BatchError(f"{path.stem} holds for calls whose cost is unknown; run "
                                 "reconcile")
    else:
        if population == "heldout":
            heldout_may_run(paths, head)
        batch_id = f"{population}-{len(batches(paths)) + 1:03d}"
        path = paths.ledgers / f"{batch_id}.jsonl"
        batch_header = header(batch_id=batch_id, population=population, bindings=bindings,
                              tasks_run=tasks_run, provider_binding=binding_digest(paths))
        try:
            ledger.create(path, batch_header)
        except ledger.LedgerError as exc:
            raise BatchError(f"the attempt ledger refuses this batch: {exc}") from exc
    backend = provider_backend(paths, batch_id=batch_header["batch_id"])
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        summary = drive(paths, ctx, path, backend, playwright)
    written = write_report(paths, batch_header["batch_id"]) if summary["status"] != "OPEN" else None
    costs = [item["cost"] for item in summary["attempts"] if item["cost"]]
    return {"batch_id": batch_header["batch_id"], "status": summary["status"],
            "seal_reason": summary["seal_reason"], "attempts": len(summary["attempts"]),
            "model_calls": sum(cost["model_calls"] for cost in costs),
            "usage_unavailable_calls": sum(cost["usage_unavailable_calls"] for cost in costs),
            "known_cost_cny": round(sum(cost["known_cost_cny"] for cost in costs), 6),
            "report": written}


def stop(paths: BatchPaths) -> dict[str, Any]:
    """Stop the open batch: a running process seals it before its next attempt; without one it
    seals now, for the seal it already owes if it owes one."""
    item = open_batch(paths)
    if item is None:
        raise BatchError("no batch is open")
    path, _, summary = item
    batch_id = summary["batch"]["batch_id"]
    if running(paths):
        paths.stop_request.write_text(json.dumps({"batch_id": batch_id}), encoding="utf-8")
        return {"status": "STOP_REQUESTED", "batch_id": batch_id}
    if summary["open_attempt"] is not None:
        raise BatchError(f"{batch_id} has an interrupted attempt; resume it with run, which "
                         "closes that attempt first, and stop it then")
    summary = ledger.append(path, "batch_sealed",
                            {"reason": summary["pending_seal"] or "operator_stop"})
    return {"status": summary["status"], "batch_id": batch_id, "seal_reason": summary["seal_reason"]}


def rearm(paths: BatchPaths, *, confirm_rearm: bool) -> dict[str, Any]:
    """Restore the fixture baseline after a failed or uncertain reset, and verify it.

    A baseline that cannot be restored and verified voids the fixture: it has to be provisioned
    again, which gives it a new manifest.
    """
    if not confirm_rearm:
        raise BatchError("re-arming rebuilds the fixture database from its baseline; pass "
                         "--confirm-rearm")
    recovery_dir = paths.private_dir / "recovery"
    try:
        record = fixture.recovery_record(recovery_dir)
    except (OSError, ValueError, StackError) as exc:
        raise BatchError(f"the fixture recovery record cannot be read: {exc}") from exc
    if record["status"] == "READY":
        return {"status": "READY", "restored": False}
    with lock(paths):
        current = open_batch(paths)
        if current is not None and current[2]["open_attempt"] is not None:
            raise BatchError("an interrupted attempt is still open; resume the batch with run, "
                             "which closes that attempt first")
        ctx = harness.load_context(paths.root, private_dir=paths.private_dir)
        result = fixture.restore(
            ctx.stack, fixture.FixtureGuard(ctx.stack), recovery_dir,
            actor_projection=lambda: actor_projection(ctx.stack.rpc_url, ctx.keys),
            rearm_after_failure=True)
    if result["status"] != "PASS":
        raise BatchError("the baseline could not be restored and verified ("
                         + ", ".join(result.get("basis") or ["no outcome"]) + "); the fixture "
                         "is void: provision it again with python -m evals.odoo.provision "
                         "--destroy-first")
    return {"status": "READY", "restored": True, "guard_digest": result["guard_digest"]}


# -- status and report ---------------------------------------------------------------------------
def status(paths: BatchPaths) -> dict[str, Any]:
    """Every batch and how it ended. An open held-out batch shows only how far it got: no
    outcome, and not the reason a seal is owed, which can itself be a result."""
    rows = []
    for _, _, summary in batches(paths):
        header_ = summary["batch"]
        finished = [item for item in summary["attempts"] if item.get("terminal") is not None]
        row = {"batch_id": header_["batch_id"], "purpose": header_["purpose"],
               "candidate": header_["candidate"]["commit"][:12], "status": summary["status"],
               "scheduled_slots": len(summary["slots"]), "attempts_finished": len(finished),
               "invalid_attempts": sum(1 for item in finished
                                       if item.get("apparatus_integrity") == "INVALID"),
               "holds_for_a_bill": bool(summary["cost_unknown"])}
        if summary["status"] == "OPEN":
            row["owes_a_seal"] = summary["pending_seal"] is not None
        else:
            row["seal_reason"] = summary["seal_reason"]
        if header_["purpose"] == "development" or summary["status"] != "OPEN":
            row["product_outcomes"] = {
                outcome: sum(1 for item in finished if item.get("product_outcome") == outcome)
                for outcome in ("PASS", "FAIL", "INDETERMINATE", "NOT_EVALUABLE")}
        rows.append(row)
    return {"running": running(paths), "batches": rows}


def build(paths: BatchPaths, batch_id: str) -> dict[str, Any]:
    """The report of one batch, recomputed from its ledger and the task set it ran."""
    item = next((entry for entry in batches(paths)
                 if entry[2]["batch"]["batch_id"] == batch_id), None)
    if item is None:
        raise BatchError(f"no batch {batch_id}")
    _, entries, summary = item
    batch_header = entries[0]["body"]
    population = batch_header["purpose"]
    if population == "heldout" and summary["status"] == "OPEN":
        raise BatchError(f"{batch_id} is still open; a held-out result is read once it ends")
    tasks_run, seal = task_set(paths, population)
    # The classes the gate divides by are the sealed ones the batch ran.
    if seal["seal_digest"] != batch_header["task_set_seal_digest"]:
        raise BatchError("the task set on disk is not the one this batch ran")
    metadata = {task["task_id"]: {"class": task["class"], "stratum": task["stratum"],
                                  "read_only": task["max_mutations"] == 0}
                for task in tasks_run["tasks"]}
    try:
        feedback = qualification(paths, batch_header["candidate"],
                                 batch_header["fixture"]["manifest_digest"])["feedback"]
    except BatchError:
        feedback = None
    scopes = (protocol.pageagent_comparison_scopes(tasks_run) if population == "heldout"
              else {"final_page": [], "answer": []})
    value = report.build_report(batches=[summary], tasks=metadata, plan=plan(batch_header),
                                paired_task_ids=scopes["final_page"],
                                answer_task_ids=scopes["answer"], feedback=feedback)
    report.verify_report(value)
    return value


def write_report(paths: BatchPaths, batch_id: str) -> str:
    value = build(paths, batch_id)
    paths.reports.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = paths.reports / f"{batch_id}.json"
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(target)


# -- command line ------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.odoo.batch")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run", help="run or resume one batch (paid)")
    start.add_argument("--confirm-paid-calls", action="store_true")
    start.add_argument("--population", choices=POPULATIONS, default="development")
    commands.add_parser("status")
    commands.add_parser("stop")
    commands.add_parser("rearm", help="restore the fixture baseline after a failed reset"
                        ).add_argument("--confirm-rearm", action="store_true")
    shown = commands.add_parser("report", help="recompute and write one batch's report")
    shown.add_argument("batch_id")
    bill = commands.add_parser("reconcile", help="record the owner's bill reconciliation")
    bill.add_argument("batch_id")
    bill.add_argument("--owner", required=True)
    bill.add_argument("--billed-calls", required=True, type=int)
    bill.add_argument("--billed-cost-cny", required=True, type=float)
    bill.add_argument("--evidence-digest", required=True)
    bill.add_argument("--note", action="append", default=[])
    args = parser.parse_args(argv)
    paths = BatchPaths()
    try:
        if args.command == "run":
            result = run(paths, confirm_paid_calls=args.confirm_paid_calls,
                         population=args.population)
        elif args.command == "status":
            result = status(paths)
        elif args.command == "stop":
            result = stop(paths)
        elif args.command == "rearm":
            result = rearm(paths, confirm_rearm=args.confirm_rearm)
        elif args.command == "reconcile":
            result = reconcile(paths, args.batch_id, owner=args.owner,
                               billed_calls=args.billed_calls,
                               billed_cost_cny=args.billed_cost_cny,
                               evidence_digest=args.evidence_digest, notes=args.note)
        else:
            result = {"report": write_report(paths, args.batch_id)}
    except BatchError as exc:
        print(json.dumps({"refused": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
