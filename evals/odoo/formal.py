"""The formal P7.4b entry points: the paid provider preflight and the development batch.

Nothing here contacts a provider until every prerequisite the protocol names has been verified
from its source rather than taken from a flag:

* the checkout is clean, so the apparatus that runs is the committed one;
* the first freeze verifies against both task-set seals, the split verification and an accepted
  independent family review, and both records were committed to ``.workbench`` exactly once and
  never changed; the development schedule seed derives from the commit that recorded the freeze;
* a custody record committed to ``.workbench`` shows the escrow of the held-out set and both
  seeds restore-verified, with the original, the archive and its secret in three different
  failure domains; a domain the machine cannot prove carries an explicit owner attestation;
* the evaluation provider is configured with an HTTPS endpoint and an owner-only key file;
* the operator passed ``--confirm-paid-calls``.

The preflight sends the fixed canary five times under the frozen request contract, accounts
every call to the product spend ledger under its own batch id and keeps only content-free
facts, including the runtime provider binding: provider id, endpoint digest, credential identity
digest, requested alias and request contract. A development batch additionally needs the newest
preflight READY under the same first freeze and the same runtime binding, so a changed endpoint
or key needs a new preflight; it also needs the verified candidate with a fixture
manifest taken on it, and the development task set its first-freeze seal describes. Its header
is committed to ``.workbench`` before the first attempt and its chain head when it ends; a
response model string other than the preflight's seals it; an open batch is resumed, never
restarted. Development batches form one immutable chain: a new candidate commit may follow any
finished batch, the same candidate continues only after an exogenous stop, and a candidate never
returns once another has followed it. The report gates only the newest batch of the chain, only
against the frozen plan and only if its ledger header is the one committed before its first
attempt. Held-out batches are not available here.

    python -m evals.odoo.formal status
    python -m evals.odoo.formal preflight --confirm-paid-calls
    python -m evals.odoo.formal development --confirm-paid-calls
    python -m evals.odoo.formal report
"""
from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import canary, escrow, freeze, harness, ledger, protocol, report
from .driver import AttemptPlan
from .gateway import (BackendResult, UpstreamBackend, digest_binding, provider_binding,
                      provider_binding_digest, read_evaluation_key, upstream_identity)
from .population import digest_json
from .revision import (REPOSITORY, RevisionError, commit_path, commit_time, committed_once,
                       require_clean_head)

FREEZES = "phase-7/P7.4/freezes"
CUSTODY = "phase-7/P7.4/custody/escrow-custody.json"
BATCHES = "phase-7/P7.4/batches"
PREFLIGHT_KIND = "p74_provider_preflight"
HELDOUT_UNAVAILABLE = ("held-out batches need the second and third freezes and a resolved "
                       "PageAgent answer channel")


class FormalError(RuntimeError):
    """A formal entry refused: a prerequisite is missing or does not verify."""


@dataclass(frozen=True)
class FormalPaths:
    repository: Path = REPOSITORY
    private_dir: Path = harness.PRIVATE
    workbench: Path = REPOSITORY / ".workbench"
    #: ``None`` accounts to the product's own spend ledger, like every paid call of the project.
    spend_ledger: Path | None = None

    @property
    def root(self) -> Path:
        return self.private_dir / "formal"

    @property
    def preflights(self) -> Path:
        return self.root / "preflight"

    @property
    def ledgers(self) -> Path:
        return self.root / "ledgers"

    @property
    def escrow_record(self) -> Path:
        return self.private_dir / "escrow.json"

    @property
    def provider_config(self) -> Path:
        return self.private_dir / "provider" / "provider.json"

    def spend(self) -> Path:
        from web_operation_agent.model import DEFAULT_LEDGER

        return Path(self.spend_ledger or os.environ.get("WOA_SPEND_LEDGER") or DEFAULT_LEDGER)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _write_private(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_json_bytes(value))


# -- records committed to .workbench -----------------------------------------------------------
def committed_record(workbench: Path, relative: str) -> tuple[dict[str, Any], str]:
    """A JSON record committed to ``.workbench`` exactly once, and the commit that recorded it."""
    try:
        commit = committed_once(workbench, relative)
        return json.loads((workbench / relative).read_text(encoding="utf-8")), commit
    except (OSError, ValueError, RevisionError) as exc:
        raise FormalError(f"{relative} is not a committed record: {exc}") from exc


def commit_record(workbench: Path, relative: str, value: dict[str, Any], message: str) -> str:
    """Write and commit a record once; the same record again returns its commit."""
    target = workbench / relative
    data = _json_bytes(value)
    try:
        if target.exists():
            if target.read_bytes() != data:
                raise FormalError(f"{relative} already records something else")
            try:
                return committed_once(workbench, relative)
            except RevisionError:
                pass  # written but never committed: commit it now
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return commit_path(workbench, relative, message)
    except (OSError, RevisionError) as exc:
        raise FormalError(f"{relative} could not be committed to .workbench: {exc}") from exc


# -- prerequisites of any paid call --------------------------------------------------------------
def verified_first_freeze(paths: FormalPaths) -> tuple[dict[str, Any], str]:
    """The committed first freeze, verified against the seals, the split and its family review."""
    review, _ = committed_record(paths.workbench, f"{FREEZES}/family-review.json")
    record, commit = committed_record(paths.workbench, f"{FREEZES}/first-freeze.json")
    try:
        freeze.verify_first_freeze(
            record, development_seal=json.loads(harness.DEVELOPMENT_SEAL.read_text()),
            heldout_seal=json.loads(harness.HELDOUT_SEAL.read_text()),
            split_verification=json.loads(harness.SPLIT_VERIFICATION.read_text()),
            family_review=review)
    except freeze.FreezeError as exc:
        raise FormalError(f"the first freeze does not verify: {exc}") from exc
    return record, commit


def verified_custody(paths: FormalPaths) -> dict[str, Any]:
    """The committed escrow custody record, checked against the current private files."""
    record, _ = committed_record(paths.workbench, CUSTODY)
    try:
        return escrow.verify_custody(record, private_dir=paths.private_dir)
    except escrow.EscrowError as exc:
        raise FormalError(f"the escrow custody does not verify: {exc}") from exc


def provider_settings(paths: FormalPaths) -> tuple[str, Path]:
    """The private provider endpoint and key file, checked for shape and key permissions."""
    try:
        settings = json.loads(paths.provider_config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FormalError("the evaluation provider is not configured") from exc
    if (not isinstance(settings, dict) or set(settings) != {"base_url", "key_file"}
            or not all(isinstance(value, str) for value in settings.values())):
        raise FormalError("the provider configuration names exactly base_url and key_file")
    key = Path(settings["key_file"])
    if not key.is_file() or key.stat().st_mode & 0o077:
        raise FormalError("the evaluation key must be a regular file only its owner can read")
    return settings["base_url"], key


def runtime_binding(paths: FormalPaths) -> dict[str, Any]:
    """The runtime provider binding the private configuration describes; nothing is sent."""
    base_url, key_file = provider_settings(paths)
    try:
        identity = upstream_identity(base_url=base_url, key=read_evaluation_key(key_file),
                                     provider_id=protocol.PROVIDER["provider_id"])
    except (OSError, ValueError) as exc:
        raise FormalError(f"the evaluation provider configuration is invalid: {exc}") from exc
    return provider_binding(identity, harness.protocol_budgets())


def provider_backend(paths: FormalPaths, *, batch_id: str) -> UpstreamBackend:
    """The evaluation provider from its private configuration; constructing it sends nothing."""
    base_url, key = provider_settings(paths)
    try:
        return UpstreamBackend(base_url=base_url, key_file=key,
                               requested_model=protocol.REQUESTED_MODEL_ALIAS,
                               spend_ledger=paths.spend(), batch_id=batch_id,
                               provider=protocol.PROVIDER["provider_id"])
    except (OSError, ValueError) as exc:
        raise FormalError(f"the evaluation provider configuration is invalid: {exc}") from exc


@dataclass(frozen=True)
class PaidCallPrerequisites:
    checkout: str
    first_freeze: dict[str, Any]
    first_freeze_commit: str
    custody: dict[str, Any]


def paid_call_prerequisites(paths: FormalPaths, *,
                            confirm_paid_calls: bool) -> PaidCallPrerequisites:
    """What any paid call needs, checked in this order before anything is sent."""
    if not confirm_paid_calls:
        raise FormalError("paid model calls need --confirm-paid-calls")
    try:
        checkout = require_clean_head(paths.repository)["commit"]
    except RevisionError as exc:
        raise FormalError(f"paid calls run only from a clean checkout: {exc}") from exc
    first, commit = verified_first_freeze(paths)
    return PaidCallPrerequisites(checkout, first, commit, verified_custody(paths))


# -- provider preflight ----------------------------------------------------------------------------
def _call_record(result: BackendResult) -> dict[str, Any]:
    return {"provider_request_id": result.provider_request_id, "status": result.status,
            "http_status": result.http_status, "response_model": result.response_model,
            "usage": result.usage, "cost_cny": result.cost_cny, "error_code": result.error_code}


def run_preflight(paths: FormalPaths, *, confirm_paid_calls: bool,
                  clock: Callable[[], str] = _utc) -> dict[str, Any]:
    """Five canaries through the evaluation provider, accounted on their own, kept content-free."""
    ready = paid_call_prerequisites(paths, confirm_paid_calls=confirm_paid_calls)
    budgets = harness.protocol_budgets()
    try:
        canary.canary_request(budgets)
    except ValueError as exc:
        raise FormalError(str(exc)) from exc
    started = clock()
    run_id = "p74b-preflight-" + started.replace("-", "").replace(":", "")
    target = paths.preflights / f"{run_id}.json"
    if target.exists():
        raise FormalError("a preflight was already recorded at this second")
    backend = provider_backend(paths, batch_id=run_id)
    calls: list[dict[str, Any]] = []

    def send(body: dict[str, Any]) -> dict[str, Any]:
        result = backend.complete(body, attempt_id=run_id, call_index=len(calls) + 1)
        calls.append(_call_record(result))
        return result.payload if result.status == "ok" else {"error": {"code": result.error_code}}

    evaluation = canary.run_preflight(send, budgets=budgets)
    known = [call["cost_cny"] for call in calls if call["cost_cny"] is not None]
    body = {
        "artifact_version": 1, "kind": PREFLIGHT_KIND, "protocol_id": protocol.PROTOCOL_ID,
        "first_freeze_digest": ready.first_freeze["first_freeze_digest"],
        "first_freeze_commit": ready.first_freeze_commit, "checkout": ready.checkout,
        "provider_config_digest": protocol.provider_config_digest(),
        "provider_binding": provider_binding(backend.identity(), budgets),
        "provider_binding_digest": provider_binding_digest(backend.identity(), budgets),
        "spend_batch_id": run_id,
        "started_at": started, "finished_at": clock(), "calls": calls,
        "known_cost_cny": round(sum(known), 6), "usage_unavailable_calls": len(calls) - len(known),
        "canary": evaluation, "status": evaluation["status"],
    }
    record = {**body, "preflight_digest": digest_json(body)}
    _write_private(target, record)
    return record


def verified_preflight(paths: FormalPaths, first_freeze: dict[str, Any]) -> dict[str, Any]:
    """The newest preflight, provided it passed under this freeze, provider and request contract."""
    records = sorted(paths.preflights.glob("p74b-preflight-*.json"))
    if not records:
        raise FormalError("no provider preflight has been run")
    try:
        record = json.loads(records[-1].read_text(encoding="utf-8"))
        body = {key: value for key, value in record.items() if key != "preflight_digest"}
    except (OSError, ValueError, AttributeError) as exc:
        raise FormalError("the newest preflight record cannot be read") from exc
    if record.get("kind") != PREFLIGHT_KIND or record.get("preflight_digest") != digest_json(body):
        raise FormalError("the newest preflight record was changed after it was written")
    if record.get("status") != "READY":
        raise FormalError("the newest provider preflight failed; rerun the canary or retire this "
                          "provider configuration")
    if (record.get("first_freeze_digest") != first_freeze["first_freeze_digest"]
            or record.get("provider_config_digest") != protocol.provider_config_digest()):
        raise FormalError("the newest preflight was taken under another first freeze or provider "
                          "configuration")
    if (not isinstance(record.get("provider_binding"), dict)
            or record.get("provider_binding_digest") != digest_binding(record["provider_binding"])
            or record["provider_binding_digest"] != digest_binding(runtime_binding(paths))):
        raise FormalError("the provider endpoint, credential or request contract changed since the "
                          "newest preflight; run the preflight again")
    return record


def preflight_record(paths: FormalPaths, digest: Any) -> dict[str, Any]:
    """The unchanged preflight record with this digest."""
    for path in sorted(paths.preflights.glob("p74b-preflight-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("preflight_digest") == digest:
            body = {key: value for key, value in record.items() if key != "preflight_digest"}
            if digest_json(body) == digest:
                return record
    raise FormalError("the preflight a batch announced is missing or was changed")


# -- development batches ---------------------------------------------------------------------------
def task_ids(task_set: dict[str, Any]) -> list[str]:
    return sorted(task["task_id"] for task in task_set["tasks"])


def development_header(*, batch_id: str, first_freeze: dict[str, Any], first_freeze_commit: str,
                       bindings: dict[str, Any], task_ids: list[str], binding_digest: str,
                       previous: dict[str, Any] | None) -> dict[str, Any]:
    """A development batch header; its seed and schedule derive from the first-freeze commit."""
    seed = protocol.derive_schedule_seed(first_freeze_commit, purpose="development")
    slots = protocol.formal_schedule("development", task_ids, seed=seed)
    return {"batch_id": batch_id, "protocol_id": protocol.PROTOCOL_ID,
            "protocol_digest": protocol.document(first_freeze=first_freeze)["protocol_digest"],
            "schedule_digest": ledger.schedule_digest(slots), "purpose": "development",
            "provider_config_digest": protocol.provider_config_digest(),
            "provider_binding_digest": binding_digest, "slots": slots,
            "previous_batch": previous, "schedule_seed": seed, **bindings}


def development_plan(*, first_freeze: dict[str, Any], first_freeze_commit: str,
                     header: dict[str, Any], task_ids: list[str],
                     binding_digest: str) -> dict[str, Any]:
    """What a development batch must match to be gated: the freeze and its announced bindings."""
    seed = protocol.derive_schedule_seed(first_freeze_commit, purpose="development")
    return {
        "purpose": "development",
        "protocol_digest": protocol.document(first_freeze=first_freeze)["protocol_digest"],
        "task_set_seal_digest": first_freeze["development_seal_digest"],
        "fixture_manifest_digest": header["fixture"]["manifest_digest"],
        "capability_digests": {arm: protocol.arm_capability_digest(arm) for arm in protocol.ARMS},
        "candidate_commit": header["candidate"]["commit"],
        "provider_config_digest": protocol.provider_config_digest(),
        "provider_binding_digest": binding_digest,
        "schedule_digest": ledger.schedule_digest(
            protocol.formal_schedule("development", task_ids, seed=seed)),
    }


def link_to(summary: dict[str, Any]) -> dict[str, Any]:
    """How the next batch names this finished one."""
    return {"batch_id": summary["batch"]["batch_id"], "purpose": summary["batch"]["purpose"],
            "protocol_digest": summary["batch"]["protocol_digest"],
            "candidate_commit": summary["batch"]["candidate"]["commit"],
            "chain_head": summary["chain_head"],
            "termination_reason": (summary["seal_reason"] if summary["status"] == "SEALED"
                                   else "batch_completed")}


def development_chain(paths: FormalPaths) -> list[tuple[Path, list[dict[str, Any]],
                                                        dict[str, Any]]]:
    """Every formal development ledger, first batch first; together they form one chain."""
    by_previous: dict[str | None, tuple[Path, list[dict[str, Any]], dict[str, Any]]] = {}
    for path in sorted(paths.ledgers.glob("*.jsonl")):
        try:
            entries, summary = ledger.read(path)
        except (OSError, ValueError) as exc:
            raise FormalError(f"{path.name} does not verify: {exc}") from exc
        previous = (summary["batch"]["previous_batch"] or {}).get("batch_id")
        if summary["batch"]["purpose"] != "development" or previous in by_previous:
            raise FormalError("the formal development ledgers do not form one chain")
        by_previous[previous] = (path, entries, summary)
    chain = []
    previous = None
    while previous in by_previous:
        item = by_previous.pop(previous)
        chain.append(item)
        previous = item[2]["batch"]["batch_id"]
    if by_previous:
        raise FormalError("a formal development ledger is not linked into the chain")
    return chain


def check_candidate_may_run(chain: list[tuple[Path, list[dict[str, Any]], dict[str, Any]]],
                            commit: str) -> None:
    """A candidate runs one development batch, continues only after an exogenous stop, and never
    returns to the chain once another candidate has followed it."""
    if not chain:
        return
    last = chain[-1][2]
    if last["batch"]["candidate"]["commit"] == commit:
        if not (last["status"] == "SEALED"
                and last["seal_reason"] in ledger.SAME_PROTOCOL_RESTART_REASONS):
            raise FormalError("this candidate already ran a development batch; commit a new "
                              "candidate, or continue the same one only after an exogenous stop")
    elif commit in {item[2]["batch"]["candidate"]["commit"] for item in chain}:
        raise FormalError("a candidate that another candidate followed never returns to the "
                          "development chain")


@dataclass(frozen=True)
class PreparedBatch:
    path: Path
    header: dict[str, Any]
    preflight: dict[str, Any]
    backend: UpstreamBackend
    header_commit: str


def _announce(paths: FormalPaths, header: dict[str, Any], extras: dict[str, Any]) -> str:
    relative = f"{BATCHES}/{header['batch_id']}/header.json"
    value = {"artifact_version": 1, "kind": "p74_batch_header_record", "header": header,
             **extras}
    target = paths.workbench / relative
    if target.exists():
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise FormalError(f"{relative} cannot be read") from exc
        if not isinstance(value, dict) or value.get("header") != header:
            raise FormalError(f"{relative} announces other bindings than this batch")
    return commit_record(paths.workbench, relative, value,
                         f"p7.4b: announce {header['batch_id']}")


def prepare_development(paths: FormalPaths, ctx: harness.Context, *,
                        confirm_paid_calls: bool) -> PreparedBatch:
    """Verify every prerequisite, then create or resume the batch and announce it; send nothing."""
    ready = paid_call_prerequisites(paths, confirm_paid_calls=confirm_paid_calls)
    preflight = verified_preflight(paths, ready.first_freeze)
    try:
        bindings = harness.batch_bindings(ctx)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        raise FormalError(f"the candidate, fixture manifest or task set does not verify: {exc}") \
            from exc
    if bindings["task_set_seal_digest"] != ready.first_freeze["development_seal_digest"]:
        raise FormalError("the development task set is not the one the first freeze sealed")
    if bindings["candidate"]["commit"] != ready.checkout:
        raise FormalError("the running candidate was not built from this checkout")
    chain = development_chain(paths)
    open_batch = chain[-1] if chain and chain[-1][2]["status"] == "OPEN" else None
    batch_id = (open_batch[1][0]["body"]["batch_id"] if open_batch is not None
                else f"p74b-development-{len(chain) + 1:03d}")
    backend = provider_backend(paths, batch_id=batch_id)
    derived = {"batch_id": batch_id, "first_freeze": ready.first_freeze,
               "first_freeze_commit": ready.first_freeze_commit, "bindings": bindings,
               "task_ids": task_ids(ctx.task_set),
               "binding_digest": provider_binding_digest(backend.identity(),
                                                         harness.protocol_budgets())}
    if open_batch is not None:
        path, entries, _ = open_batch
        header = entries[0]["body"]
        if header != development_header(previous=header["previous_batch"], **derived):
            raise FormalError("the open development batch runs under other bindings; seal it "
                              "with operator_stop before another starts")
    else:
        check_candidate_may_run(chain, bindings["candidate"]["commit"])
        header = development_header(previous=link_to(chain[-1][2]) if chain else None, **derived)
        path = paths.ledgers / f"{batch_id}.jsonl"
        try:
            ledger.create(path, header)
        except ledger.LedgerError as exc:
            raise FormalError(f"the attempt ledger refuses this batch: {exc}") from exc
    commit = _announce(paths, header, {
        "first_freeze_commit": ready.first_freeze_commit,
        "preflight_digest": preflight["preflight_digest"],
        "escrow_custody_digest": ready.custody["custody_digest"]})
    return PreparedBatch(path, header, preflight, backend, commit)


def _attempt_plan(ctx: harness.Context, slot: dict[str, Any]) -> AttemptPlan:
    runner, evaluator = ctx.inputs(slot["task_id"])
    return AttemptPlan(slot={key: slot[key] for key in ("slot_id", "arm", "task_id", "replicate")},
                       runner_input=runner, evaluator_input=evaluator)


def _seal_if_owed(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    if summary["status"] == "OPEN" and summary["open_attempt"] is None and summary["pending_seal"]:
        return ledger.append(path, "batch_sealed", {"reason": summary["pending_seal"]})
    return summary


def drive_batch(ctx: harness.Context, prepared: PreparedBatch, playwright: Any) -> dict[str, Any]:
    """Run the batch's remaining slots in schedule order, closing an interrupted attempt first."""
    path = prepared.path
    preflight_model = prepared.preflight["canary"]["model_strings"][0]
    with harness.open_driver(ctx, path, playwright, backend=prepared.backend) as driver:
        summary = ledger.read(path)[1]
        if summary["open_attempt"] is not None:
            summary = _seal_if_owed(path, driver.recover_open_attempt())
            last = summary["attempts"][-1]
            if (summary["status"] == "OPEN" and last["replacement_eligible"]
                    and last["replacement_of"] is None):
                slot = next(item for item in summary["slots"] if item["slot_id"] == last["slot_id"])
                plan = _attempt_plan(ctx, slot)
                plan.replacement_of = last["attempt_id"]
                summary = _seal_if_owed(path, driver.run_attempt(plan))
        for slot in summary["slots"]:
            current = ledger.read(path)[1]
            if current["status"] != "OPEN":
                break
            if next(item for item in current["slots"] if item["slot_id"] == slot["slot_id"])[
                    "attempts"]:
                continue
            current = driver.run_slot(_attempt_plan(ctx, slot))
            if (current["status"] == "OPEN" and current["provider_model"] is not None
                    and current["provider_model"] != preflight_model):
                # The provider now answers under a model string the preflight did not see.
                ledger.append(path, "batch_sealed", {"reason": "model_identity_changed"})
    return harness.finish_batch(path)


def close_batch(paths: FormalPaths, summary: dict[str, Any]) -> dict[str, Any]:
    """Commit a finished batch's chain head; an unfinished batch is left open to resume."""
    batch_id = summary["batch"]["batch_id"]
    costs = [item["cost"] for item in summary["attempts"] if item["cost"]]
    value = {"artifact_version": 1, "kind": "p74_batch_chain_head_record", "batch_id": batch_id,
             "status": summary["status"], "seal_reason": summary["seal_reason"],
             "chain_head": summary["chain_head"], "attempts": len(summary["attempts"]),
             "model_calls": sum(cost["model_calls"] for cost in costs),
             "usage_unavailable_calls": sum(cost["usage_unavailable_calls"] for cost in costs),
             "known_cost_cny": round(sum(cost["known_cost_cny"] for cost in costs), 6)}
    if summary["status"] != "OPEN":
        commit_record(paths.workbench, f"{BATCHES}/{batch_id}/chain-head.json", value,
                      f"p7.4b: close {batch_id}")
    return value


def run_development(paths: FormalPaths, *, confirm_paid_calls: bool) -> dict[str, Any]:
    ctx = harness.load_context(paths.root, private_dir=paths.private_dir)
    prepared = prepare_development(paths, ctx, confirm_paid_calls=confirm_paid_calls)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        summary = drive_batch(ctx, prepared, playwright)
    return close_batch(paths, summary)


def development_report(paths: FormalPaths) -> dict[str, Any]:
    """The public report over the whole development chain, gated on the frozen plan."""
    chain = development_chain(paths)
    if not chain:
        raise FormalError("no formal development batch exists")
    first, commit = verified_first_freeze(paths)
    for _, entries, _ in chain:
        header = entries[0]["body"]
        record, announced = committed_record(paths.workbench,
                                             f"{BATCHES}/{header['batch_id']}/header.json")
        if record.get("header") != header:
            raise FormalError(f"{header['batch_id']} ran under a header it did not announce")
        started = next((entry["at"] for entry in entries if entry["kind"] == "attempt_started"),
                       None)
        try:
            late = started is not None and commit_time(paths.workbench, announced) > \
                datetime.fromisoformat(started.replace("Z", "+00:00"))
        except RevisionError as exc:
            raise FormalError(str(exc)) from exc
        if late:
            raise FormalError(f"{header['batch_id']} was announced after its first attempt")
    task_set = json.loads(harness.DEVELOPMENT_TASK_SET.read_text())
    # ``record`` is now the newest batch's announcement, which names the preflight it relied on.
    preflight = preflight_record(paths, record.get("preflight_digest"))
    plan = development_plan(first_freeze=first, first_freeze_commit=commit,
                            header=chain[-1][1][0]["body"], task_ids=task_ids(task_set),
                            binding_digest=preflight["provider_binding_digest"])
    metadata = {task["task_id"]: {"class": task["class"], "stratum": task["stratum"],
                                  "read_only": task["max_mutations"] == 0}
                for task in task_set["tasks"]}
    value = report.build_report(batches=[summary for _, _, summary in chain], tasks=metadata,
                                plan=plan)
    report.verify_report(value)
    return value


# -- status and command line ------------------------------------------------------------------------
def status(paths: FormalPaths) -> dict[str, Any]:
    """Whether each paid step could start now and, if not, why; nothing is sent."""
    checks: dict[str, str] = {}
    found: dict[str, Any] = {}

    def check(name: str, step: Callable[[], Any]) -> None:
        try:
            found[name] = step()
        except (FormalError, RevisionError) as exc:
            checks[name] = f"refused: {exc}"
        else:
            checks[name] = "READY"

    check("clean_checkout", lambda: require_clean_head(paths.repository))
    check("first_freeze", lambda: verified_first_freeze(paths))
    check("escrow_custody", lambda: verified_custody(paths))
    check("provider_configuration", lambda: provider_backend(paths, batch_id="p74b-status"))
    if "first_freeze" in found:
        check("preflight", lambda: verified_preflight(paths, found["first_freeze"][0]))
    else:
        checks["preflight"] = "refused: needs the first freeze"
    preflight = all(checks[name] == "READY" for name in (
        "clean_checkout", "first_freeze", "escrow_custody", "provider_configuration"))
    return {"artifact_version": 1, "kind": "p74_formal_status", "checks": checks,
            "preflight_can_start": preflight,
            "development_can_start": preflight and checks["preflight"] == "READY",
            "development_also_needs": ("the verified candidate and a fixture manifest taken with "
                                       "it, checked when the batch starts"),
            "heldout": HELDOUT_UNAVAILABLE}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.odoo.formal")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    for name in ("preflight", "development"):
        commands.add_parser(name).add_argument("--confirm-paid-calls", action="store_true")
    commands.add_parser("report")
    args = parser.parse_args(argv)
    paths = FormalPaths()
    try:
        if args.command == "status":
            result = status(paths)
        elif args.command == "preflight":
            record = run_preflight(paths, confirm_paid_calls=args.confirm_paid_calls)
            result = {key: record[key] for key in ("status", "spend_batch_id", "known_cost_cny",
                                                   "usage_unavailable_calls")}
            result |= {"checks": record["canary"]["checks"],
                       "model_strings": record["canary"]["model_strings"]}
        elif args.command == "development":
            result = run_development(paths, confirm_paid_calls=args.confirm_paid_calls)
        else:
            value = development_report(paths)
            target = paths.root / "reports" / f"development-{_utc().replace(':', '')}.json"
            _write_private(target, value)
            result = {"report": str(target), "batches": [
                {key: entry[key] for key in ("batch_id", "status", "verdict_status",
                                             "verdict_basis")} for entry in value["batches"]]}
    except FormalError as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}, indent=1))
        return 2
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
