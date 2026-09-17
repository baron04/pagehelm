"""The formal P7.4b entry points: the paid provider preflight, the development batch and the
lifecycle around them.

Nothing here contacts a provider until every prerequisite the protocol names has been verified
from its source rather than taken from a flag:

* the checkout is clean, so the apparatus that runs is the committed one;
* the first freeze verifies against both task-set seals, the split verification, an accepted
  independent family review and an accepted external adversarial review, and every record was
  committed to ``.workbench`` exactly once and never changed; the development schedule seed
  derives from the commit that recorded the freeze;
* the newest committed external review of this apparatus accepts it: its reviewed commit is in
  the checkout's history, no reviewed file has changed since, and it names this machine protocol;
* a custody record committed to ``.workbench`` shows the escrow of the held-out set and both
  seeds restore-verified, with the original, the archive and its secret in three different
  failure domains; a domain the machine cannot prove carries an explicit owner attestation;
* the spend ledger every call is accounted to can be written;
* the evaluation provider is configured with an HTTPS endpoint and an owner-only key file;
* the operator passed ``--confirm-paid-calls``.

The preflight sends the fixed canary five times under the frozen request contract, accounts
every call to the product spend ledger under its own batch id, stops at once when a call cannot
be accounted, and keeps only content-free facts, including the runtime provider binding:
provider id, endpoint digest, credential identity digest, requested alias and request contract.
A development batch additionally needs the newest preflight READY under the same first freeze
and the same runtime binding, so a changed endpoint or key needs a new preflight, as does a
batch sealed because the provider refused the credential; it also needs the verified candidate
with a fixture manifest taken on it, a fixture whose resets may run, the chain head of the batch
before it committed, the development task set its first-freeze seal describes, and a READY
zero-model qualification of that candidate on the fixture that manifest describes. The entry
itself checks that qualification, not only the command line, and a rehearsal's own
qualification counts only while its paid effects stay apart. Its header is
committed to ``.workbench`` before the first attempt and its chain head when it ends; a response
model string other than the preflight's seals it; an open batch is resumed, never restarted.
Development batches form one immutable chain: a new candidate commit may follow any finished
batch, the same candidate continues only after an exogenous stop, and a candidate never returns
once another has followed it. The report gates only the newest batch of the chain, only against
the frozen plan and only if its ledger header is the one committed before its first attempt; its
experience gate also needs the submit and cancel feedback checks of the candidate's newest READY
zero-model qualification. Held-out batches are not available here.

One development process runs on a formal root at a time. ``stop`` stops the open batch safely:
a running process seals it with ``operator_stop`` before its next attempt, and without one an
interrupted attempt is first closed from its persisted evidence. ``close`` commits the chain head
of any finished batch that lacks one. ``rearm-fixture`` restores the baseline after a failed or
uncertain reset and verifies it, or declares the fixture void. ``family-review`` and ``freeze``
generate, verify and commit each freeze record once, and ``verify-freezes`` checks the committed
chain. ``status`` sends and changes nothing; beyond the paid-call prerequisites it checks that
HEAD passed the zero-model qualification under this machine protocol, that the fixture can be
reset and that every finished batch's chain head is committed, and ``preflight`` and
``development`` refuse unless it allows them.

Each protocol version keeps its own formal directory and ``.workbench`` batch records, and every
batch header binds the ledger contract its reader applies, so a ledger of any other contract is
refused rather than read. A batch is announced with the full fixture manifest it binds. Before a
new version changes the ledger contract, ``archive-chain`` commits the finished chain's
recomputed report, its reader and those manifests, and ``verify-archive`` checks that record and,
under the same reader only, recomputes the report.

A call a spend ledger could not record, in a preflight or a batch, blocks every paid call until
``reconcile-billing`` commits the owner's reconciliation of it against the provider bill, and the
next batch's link names that record; a spend ledger that can be written again is not enough.

    python -m evals.odoo.formal status
    python -m evals.odoo.formal external-review --record REVIEW.json
    python -m evals.odoo.formal family-review --reviewer NAME --reviewed-at UTC --verdict ACCEPT
    python -m evals.odoo.formal freeze first
    python -m evals.odoo.formal preflight --confirm-paid-calls
    python -m evals.odoo.formal development --confirm-paid-calls
    python -m evals.odoo.formal stop
    python -m evals.odoo.formal close
    python -m evals.odoo.formal rearm-fixture --confirm-rearm
    python -m evals.odoo.formal report
    python -m evals.odoo.formal freeze second
    python -m evals.odoo.formal verify-freezes
    python -m evals.odoo.formal archive-chain
    python -m evals.odoo.formal verify-archive [--protocol-id ID] [--recompute]
    python -m evals.odoo.formal reconcile-billing --spend-batch ID --owner NAME
        --reconciled-at UTC --billed-calls N --billed-cost-cny X --evidence-digest SHA256
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (canary, escrow, fixture, freeze, harness, ledger, population as population_module,
               protocol, report, tasks)
from .driver import AttemptPlan
from .gateway import (BackendResult, ScriptedBackend, UpstreamBackend, digest_binding,
                      provider_binding, provider_binding_digest, read_evaluation_key,
                      upstream_identity)
from .population import digest_json
from .projection import actor_projection
from .revision import (REPOSITORY, RevisionError, blob_ids_at, commit_path, commit_time,
                       committed_once, content_blob_ids, file_at, first_parent_commits,
                       is_ancestor, path_history, repository_state, require_clean_head)
from .stack import StackError

FREEZES = "phase-7/P7.4/freezes"
#: External adversarial reviews, each committed once; the newest of the running apparatus decides.
REVIEWS = "phase-7/P7.4/reviews"
CUSTODY = "phase-7/P7.4/custody/escrow-custody.json"
#: Batch records and chain archives, each under its protocol version's own directory.
BATCHES = "phase-7/P7.4/batches"
ARCHIVES = "phase-7/P7.4/archives"
CHAIN_ARCHIVE_KIND = "p74_development_chain_archive"
#: The owner's reconciliation of a spend batch's unaccounted calls against the provider bill.
RECONCILIATIONS = "phase-7/P7.4/reconciliations"
RECONCILIATION_KIND = "p74_billing_reconciliation"
PREFLIGHT_KIND = "p74_provider_preflight"
HELDOUT_UNAVAILABLE = ("held-out batches need the second and third freezes and a resolved "
                       "PageAgent answer channel")
#: What ``status`` requires before a preflight, and additionally before a development batch.
PREFLIGHT_CHECKS = ("clean_checkout", "first_freeze", "external_review", "escrow_custody",
                    "spend_ledger", "billing_reconciliation", "provider_configuration")
DEVELOPMENT_CHECKS = ("preflight", "qualification", "fixture_recovery", "batch_records")


class FormalError(RuntimeError):
    """A formal entry refused: a prerequisite is missing or does not verify."""


@dataclass(frozen=True)
class FormalPaths:
    repository: Path = REPOSITORY
    private_dir: Path = harness.PRIVATE
    workbench: Path = REPOSITORY / ".workbench"
    #: ``None`` accounts to the product's own spend ledger, like every paid call of the project.
    spend_ledger: Path | None = None
    #: Where ledgers, preflights, evidence and reports are kept; ``None`` is this protocol
    #: version's own formal directory, so a later version never reads an earlier version's chain.
    #: A zero-model rehearsal of the formal entry sets this, the provider configuration, the
    #: workbench and the spend ledger apart from the formal records.
    formal_root: Path | None = None
    provider_config_file: Path | None = None
    #: A zero-model rehearsal's own qualification records, standing for the qualification a running
    #: qualification has not finished taking. They count only while the rehearsal keeps every paid
    #: effect apart: its own formal root, workbench, spend ledger and a loopback provider.
    rehearsal_qualifications: Path | None = None

    @property
    def root(self) -> Path:
        return self.formal_root or self.private_dir / "formal" / protocol.PROTOCOL_ID

    @property
    def preflights(self) -> Path:
        return self.root / "preflight"

    @property
    def ledgers(self) -> Path:
        return self.root / "ledgers"

    @property
    def stop_request(self) -> Path:
        return self.root / "stop-request.json"

    @property
    def lock(self) -> Path:
        return self.root / "development.lock"

    @property
    def escrow_record(self) -> Path:
        return self.private_dir / "escrow.json"

    @property
    def provider_config(self) -> Path:
        return self.provider_config_file or self.private_dir / "provider" / "provider.json"

    @property
    def recovery_dir(self) -> Path:
        return self.private_dir / "recovery"

    @property
    def qualifications(self) -> Path:
        return self.private_dir / "qualification"

    def spend(self) -> Path:
        from web_operation_agent.model import DEFAULT_LEDGER

        return Path(self.spend_ledger or os.environ.get("WOA_SPEND_LEDGER") or DEFAULT_LEDGER)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _strict_json(text: str) -> Any:
    """JSON in which no object names a key twice: a repeated key shows a reader one value while
    the parser keeps another."""
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("a JSON object names a key twice")
        return dict(pairs)

    return json.loads(text, object_pairs_hook=unique)


def _write_private(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_json_bytes(value))


def _clean_checkout(paths: FormalPaths, what: str) -> str:
    try:
        return require_clean_head(paths.repository)["commit"]
    except RevisionError as exc:
        raise FormalError(f"{what} runs only from a clean checkout: {exc}") from exc


# -- records committed to .workbench -----------------------------------------------------------
def committed_record(workbench: Path, relative: str) -> tuple[dict[str, Any], str]:
    """A JSON record committed to ``.workbench`` exactly once, and the commit that recorded it.

    The record is read as that commit holds it, and the working tree must hold the same bytes.
    """
    try:
        commit = committed_once(workbench, relative)
        data = file_at(workbench, commit, relative)
        if (workbench / relative).read_bytes() != data:
            raise RevisionError(f"{relative} differs from the commit that recorded it")
        return _strict_json(data.decode("utf-8")), commit
    except (OSError, ValueError, RevisionError) as exc:
        raise FormalError(f"{relative} is not a committed record: {exc}") from exc


def _committed_or_none(workbench: Path, relative: str) -> dict[str, Any] | None:
    try:
        return committed_record(workbench, relative)[0]
    except FormalError:
        return None


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


def batch_record_path(batch_id: str, name: str) -> str:
    """Where a development batch's ``name`` record is committed, under its protocol version."""
    return f"{BATCHES}/{protocol.PROTOCOL_ID}/{batch_id}/{name}.json"


def chain_archive_path(protocol_id: str | None = None) -> str:
    """Where a protocol version's archived development chain is committed."""
    return f"{ARCHIVES}/{protocol_id or protocol.PROTOCOL_ID}/development-chain.json"


# -- the freezes ---------------------------------------------------------------------------------
def _seals() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (json.loads(harness.DEVELOPMENT_SEAL.read_text()),
            json.loads(harness.HELDOUT_SEAL.read_text()),
            json.loads(harness.SPLIT_VERIFICATION.read_text()))


def external_review_path(record: dict[str, Any]) -> str:
    """Where an external review is recorded: named by the time it names and its own digest."""
    stamp = record["reviewed_at"].replace("-", "").replace(":", "")
    return f"{REVIEWS}/external-review-{stamp}-{record['review_digest'][7:19]}.json"


def committed_external_reviews(paths: FormalPaths) -> list[tuple[dict[str, Any], str]]:
    """Every recorded external review, each verified as sealed, in the order it was recorded.

    The order is the ``.workbench`` history's, never a time a reviewer wrote. A review is read from
    that history rather than from whatever the working tree holds, so a recorded review that was
    changed or removed -- committed or not -- and a review file that was never recorded are
    refused rather than skipped.
    """
    try:
        history = path_history(paths.workbench, REVIEWS)
    except RevisionError as exc:
        raise FormalError(f"the external review history cannot be read: {exc}") from exc
    recorded: list[str] = []
    for _, status, path in history:
        name = Path(path).name
        if (status != "A" or path in recorded or Path(path).parent.as_posix() != REVIEWS
                or not (name.startswith("external-review-") and name.endswith(".json"))):
            raise FormalError(f"{path} was changed, removed or misplaced after reviews were "
                              "recorded; a recorded review is never changed")
        recorded.append(path)
    per_commit = Counter(commit for commit, _, _ in history)
    if any(count > 1 for count in per_commit.values()):
        raise FormalError("a commit records more than one external review; each review is recorded "
                          "by a commit of its own, so the history alone orders them")
    try:
        main_line = first_parent_commits(paths.workbench)
    except RevisionError as exc:
        raise FormalError(f"the external review history cannot be read: {exc}") from exc
    if any(commit not in main_line for commit in per_commit):
        # Merged branches have no one order: which review came last would depend on the merge.
        raise FormalError("an external review was recorded on a branch merged into .workbench; "
                          "reviews are recorded on its main line only")
    directory = paths.workbench / REVIEWS
    present = ({f"{REVIEWS}/{item.name}" for item in directory.iterdir()}
               if directory.is_dir() else set())
    if present - set(recorded):
        raise FormalError("a review file that was never recorded sits among the external "
                          f"reviews ({', '.join(sorted(present - set(recorded)))})")
    reviews = []
    for path in recorded:
        record, commit = committed_record(paths.workbench, path)
        try:
            freeze.verify_external_review(record)
            recorded_at = commit_time(paths.workbench, commit)
        except freeze.FreezeError as exc:
            raise FormalError(f"{Path(path).name} does not verify: {exc}") from exc
        except RevisionError as exc:
            raise FormalError(f"{Path(path).name} cannot be dated: {exc}") from exc
        if path != external_review_path(record):
            # A name is a function of the review's own time and digest, so a review recorded once
            # cannot be recorded again under another name.
            raise FormalError(f"{Path(path).name} is not named by the time and digest it records")
        if _moment(record["reviewed_at"]) > recorded_at:
            raise FormalError(f"{Path(path).name} names a time later than the commit that recorded "
                              "it")
        reviews.append((record, commit))
    return reviews


def check_review_binding(paths: FormalPaths, review: dict[str, Any]) -> None:
    """Refuse a review of anything but the apparatus in this checkout: its commit must be in the
    checkout's history with every reviewed file unchanged since, under this machine protocol."""
    files = freeze.reviewed_files()
    try:
        head = repository_state(paths.repository)["commit"]
        in_history = is_ancestor(paths.repository, review["reviewed_commit"], head)
        reviewed = blob_ids_at(paths.repository, review["reviewed_commit"], files)
        current = content_blob_ids(paths.repository, files)
    except RevisionError as exc:
        raise FormalError(f"the reviewed commit cannot be checked: {exc}") from exc
    if not in_history:
        raise FormalError("the reviewed commit is not in this checkout's history")
    changed = sorted(path for path in files if reviewed[path] != current[path])
    if changed:
        raise FormalError("the apparatus changed after the reviewed commit ("
                          + ", ".join(changed[:5]) + "); it needs a new external review")
    if review["reviewed_protocol_digest"] != protocol.document()["protocol_digest"]:
        raise FormalError("the review names another machine protocol digest")


def verified_external_review(paths: FormalPaths) -> tuple[dict[str, Any], str]:
    """The newest committed external review of the apparatus running now, provided it accepts."""
    current = protocol.document()["protocol_digest"]
    reviews = [item for item in committed_external_reviews(paths)
               if item[0]["reviewed_protocol_digest"] == current]
    if not reviews:
        raise FormalError("no committed external review names this machine protocol; record one "
                          "with external-review")
    record, commit = reviews[-1]
    check_review_binding(paths, record)
    if record["verdict"] != "ACCEPT":
        raise FormalError(f"the newest external review of this apparatus is {record['verdict']}")
    return record, commit


def record_external_review(paths: FormalPaths, *, review_file: Path,
                           clock: Callable[[], str] = _utc) -> dict[str, Any]:
    """Generate, verify and commit a reviewer's item-by-item conclusions, whatever the verdict;
    only an ACCEPT of the running apparatus opens the first freeze and paid calls. A review is
    recorded after the time it names, and the newest recorded review decides."""
    _clean_checkout(paths, "recording an external review")
    committed_external_reviews(paths)
    try:
        supplied = _strict_json(Path(review_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FormalError("the review file cannot be read") from exc
    fields = {"reviewer", "reviewed_at", "reviewed_commit", "items", "findings"}
    if not isinstance(supplied, dict) or not set(supplied) <= fields:
        raise FormalError("the review file names only reviewer, reviewed_at, reviewed_commit, "
                          "items and findings")
    try:
        record = freeze.external_review_record(
            reviewer=supplied.get("reviewer"), reviewed_at=supplied.get("reviewed_at"),
            reviewed_commit=supplied.get("reviewed_commit"),
            protocol_digest=protocol.document()["protocol_digest"], items=supplied.get("items"),
            findings=supplied.get("findings"))
    except freeze.FreezeError as exc:
        raise FormalError(f"the external review does not verify: {exc}") from exc
    if record["reviewed_at"] > clock():
        raise FormalError("reviewed_at is later than now; a review is recorded after it concludes")
    check_review_binding(paths, record)
    relative = external_review_path(record)
    message = f"p7.4: record the external review of {record['reviewed_commit'][:12]}"
    return {"record": relative, "verdict": record["verdict"],
            "review_digest": record["review_digest"],
            "commit": commit_record(paths.workbench, relative, record, message)}


def verified_first_freeze(paths: FormalPaths) -> tuple[dict[str, Any], str]:
    """The committed first freeze, verified against the seals, the split and both reviews."""
    review, _ = committed_record(paths.workbench, f"{FREEZES}/family-review.json")
    record, commit = committed_record(paths.workbench, f"{FREEZES}/first-freeze.json")
    external = next((item for item, _ in committed_external_reviews(paths)
                     if item["review_digest"] == record.get("external_review_digest")), None)
    if external is None:
        raise FormalError("the external review the first freeze binds is not committed")
    development, heldout, split = _seals()
    try:
        freeze.verify_first_freeze(record, development_seal=development, heldout_seal=heldout,
                                   split_verification=split, family_review=review,
                                   external_review=external)
    except freeze.FreezeError as exc:
        raise FormalError(f"the first freeze does not verify: {exc}") from exc
    return record, commit


def verified_second_freeze(paths: FormalPaths,
                           first_freeze: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The committed second freeze, recomputed from its candidate commit."""
    record, commit = committed_record(paths.workbench, f"{FREEZES}/second-freeze.json")
    try:
        freeze.verify_second_freeze(record, first_freeze=first_freeze, repository=paths.repository)
    except freeze.FreezeError as exc:
        raise FormalError(f"the second freeze does not verify: {exc}") from exc
    return record, commit


def heldout_schedule_inputs(paths: FormalPaths) -> tuple[list[str], list[str]]:
    """The held-out task ids and PageAgent eligibility, from the task set its seal describes."""
    try:
        task_set = json.loads((paths.private_dir / "heldout" / "p74-odoo-heldout-v1.json")
                              .read_text(encoding="utf-8"))
        population = population_module.generate(
            (paths.private_dir / "seeds" / "heldout.seed").read_text().strip())
        tasks.verify_seal(task_set, json.loads(harness.HELDOUT_SEAL.read_text()),
                          population=population)
        ids = [task["task_id"] for task in task_set["tasks"]]
    except (OSError, ValueError, KeyError, TypeError, tasks.TaskSetError) as exc:
        raise FormalError(f"the held-out task set does not match its seal: {exc}") from exc
    return ids, protocol.pageagent_eligibility(task_set)


def record_family_review(paths: FormalPaths, *, reviewer: str, reviewed_at: str, verdict: str,
                         findings: list[str]) -> dict[str, Any]:
    """Generate, verify and commit the independent family review; only an ACCEPT is recorded."""
    _clean_checkout(paths, "recording a freeze")
    if verdict != "ACCEPT":
        raise FormalError("only an ACCEPT family review is recorded, because its record is "
                          "committed once; a REVISE review goes back to the task families")
    development, heldout, _ = _seals()
    try:
        record = freeze.family_review_record(reviewer=reviewer, reviewed_at=reviewed_at,
                                             verdict=verdict, development_seal=development,
                                             heldout_seal=heldout, findings=findings)
        freeze.verify_family_review(record, development_seal=development, heldout_seal=heldout)
    except freeze.FreezeError as exc:
        raise FormalError(f"the family review does not verify: {exc}") from exc
    relative = f"{FREEZES}/family-review.json"
    return {"record": relative, "review_digest": record["review_digest"],
            "commit": commit_record(paths.workbench, relative, record,
                                    "p7.4: record the independent family review")}


def record_first_freeze(paths: FormalPaths) -> dict[str, Any]:
    """Take the first freeze from both seals, the split and the committed family review."""
    _clean_checkout(paths, "recording a freeze")
    review, _ = committed_record(paths.workbench, f"{FREEZES}/family-review.json")
    external, _ = verified_external_review(paths)
    development, heldout, split = _seals()
    try:
        record = freeze.first_freeze_record(development_seal=development, heldout_seal=heldout,
                                            split_verification=split, family_review=review,
                                            external_review=external)
    except freeze.FreezeError as exc:
        raise FormalError(f"the first freeze cannot be taken: {exc}") from exc
    relative = f"{FREEZES}/first-freeze.json"
    commit = commit_record(paths.workbench, relative, record, "p7.4: record the first freeze")
    verified_first_freeze(paths)
    return {"record": relative, "first_freeze_digest": record["first_freeze_digest"],
            "commit": commit}


def record_second_freeze(paths: FormalPaths, *, accept_not_robust: bool = False) -> dict[str, Any]:
    """Freeze the candidate of the newest development batch, from its own clean checkout.

    That batch must be gated and pass the screening, safety and experience gates. A screening
    verdict that changes when replacements are scored as failures needs the owner's explicit,
    written acceptance, which ``accept_not_robust`` states was given.
    """
    first, _ = verified_first_freeze(paths)
    value = development_report(paths)
    newest = value["batches"][-1]
    primary = newest["arms"][report.GATED_ARM]["replicates"]["primary"]
    reasons = []
    if newest["verdict_status"] != "gated":
        reasons.append("the newest development batch is not gated")
    elif primary["normal"]["gate_verdict"] != "PASS":
        reasons.append("the screening gate did not pass")
    else:
        if primary["normal"]["robust"] is False and not accept_not_robust:
            reasons.append("the screening verdict changes when replacements are scored as "
                           "failures; pass --accept-not-robust only with the owner's written "
                           "acceptance")
        if primary["latency"]["experience_gate_passed"] is not True:
            reasons.append("the experience gate did not pass")
    if value["safety"]["safety_gate_failed"]:
        reasons.append("the safety gate failed")
    if reasons:
        raise FormalError("; ".join(reasons))
    _, entries, summary = development_chain(paths)[-1]
    try:
        record = freeze.second_freeze_record(
            first_freeze=first, candidate=entries[0]["body"]["candidate"],
            development_chain_head=summary["chain_head"], repository=paths.repository)
    except freeze.FreezeError as exc:
        raise FormalError(f"the second freeze cannot be taken: {exc}") from exc
    relative = f"{FREEZES}/second-freeze.json"
    return {"record": relative, "second_freeze_digest": record["second_freeze_digest"],
            "commit": commit_record(paths.workbench, relative, record,
                                    "p7.4: record the second freeze")}


def record_third_freeze(paths: FormalPaths) -> dict[str, Any]:
    """Derive the held-out schedule from the commit that recorded the second freeze, and seal
    the protocol document."""
    _clean_checkout(paths, "recording a freeze")
    first, _ = verified_first_freeze(paths)
    second, second_commit = verified_second_freeze(paths, first)
    ids, eligible = heldout_schedule_inputs(paths)
    try:
        record = freeze.third_freeze_record(first_freeze=first, second_freeze=second,
                                            second_freeze_commit=second_commit,
                                            heldout_task_ids=ids, pageagent_eligible=eligible)
    except freeze.FreezeError as exc:
        raise FormalError(f"the third freeze cannot be taken: {exc}") from exc
    relative = f"{FREEZES}/third-freeze.json"
    return {"record": relative, "third_freeze_digest": record["third_freeze_digest"],
            "commit": commit_record(paths.workbench, relative, record,
                                    "p7.4: record the third freeze")}


def verify_freezes(paths: FormalPaths) -> dict[str, Any]:
    """Verify the committed freeze records as far as the chain goes; nothing is written."""
    first, first_commit = verified_first_freeze(paths)
    result: dict[str, Any] = {"stage": "first_frozen", "first_freeze": {
        "digest": first["first_freeze_digest"], "commit": first_commit}}
    if not (paths.workbench / FREEZES / "second-freeze.json").exists():
        return result
    second, second_commit = verified_second_freeze(paths, first)
    result |= {"stage": "second_frozen", "second_freeze": {
        "digest": second["second_freeze_digest"], "commit": second_commit}}
    if not (paths.workbench / FREEZES / "third-freeze.json").exists():
        return result
    third, third_commit = committed_record(paths.workbench, f"{FREEZES}/third-freeze.json")
    ids, eligible = heldout_schedule_inputs(paths)
    try:
        freeze.verify_third_freeze(third, first_freeze=first, second_freeze=second,
                                   heldout_task_ids=ids, pageagent_eligible=eligible)
    except freeze.FreezeError as exc:
        raise FormalError(f"the third freeze does not verify: {exc}") from exc
    return result | {"stage": "third_frozen", "third_freeze": {
        "digest": third["third_freeze_digest"], "commit": third_commit}}


# -- prerequisites of any paid call --------------------------------------------------------------
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


def check_spend_ledger(paths: FormalPaths) -> Path:
    """The spend ledger, provided a record could be appended to it; nothing is written."""
    target = paths.spend()
    probe = target if target.exists() else next(
        (parent for parent in target.parents if parent.exists()), None)
    if (probe is None or (target.exists() and not target.is_file())
            or not os.access(probe, os.W_OK | (os.X_OK if probe.is_dir() else 0))):
        raise FormalError("the spend ledger cannot be written, and every paid call must be "
                          "accounted to it")
    return target


# -- calls a spend ledger could not record ---------------------------------------------------------
def _owner(value: Any) -> str:
    if (not isinstance(value, str) or not 2 <= len(value) <= 80 or value != value.strip()
            or not all(character.isalnum() or character in " ._@+-" for character in value)):
        raise FormalError("a reconciliation names the owner who concluded it")
    return value


def unaccounted_calls(paths: FormalPaths) -> dict[str, list[str]]:
    """The request ids of every paid call whose spend is not recorded, by spend batch id: in a
    preflight and in each development batch.

    A call the spend ledger could not record is one of them, and so is a call that never settled
    within the provider's own deadline: nobody knows what it cost, so the bill must say.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(paths.preflights.glob("p74b-preflight-*.json")):
        # A preflight whose calls cannot be read is not a preflight with no unaccounted call:
        # this gate answers what the bill must cover, and it does not answer from a file it
        # could not read.
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FormalError(f"{path.name} cannot be read, so the calls it made are "
                              "unaccounted for") from exc
        if (not isinstance(record, dict) or not isinstance(record.get("calls"), list)
                or not isinstance(record.get("spend_batch_id"), str)):
            raise FormalError(f"{path.name} does not record the calls it made")
        ids = [call["provider_request_id"] for call in record["calls"]
               if isinstance(call, dict) and call.get("status") == "accounting_error"]
        if ids:
            found[record["spend_batch_id"]] = sorted(map(str, ids))
    for path, entries, summary in development_chain(paths):
        bodies = [entry["body"] for entry in entries
                  if entry["kind"] == "model_response_received"
                  and entry["body"]["status"] == "accounting_error"]
        # A call left unresolved has no answer to carry a provider request id, so it is named
        # the way the ledger names it.
        bodies += [entry["body"] for entry in entries
                   if entry["kind"] == "model_response_unresolved"]
        ids = [body.get("provider_request_id") or f"{body['attempt_id']}#{body['request_seq']}"
               for body in bodies]
        if ids:
            found[summary["batch"]["batch_id"]] = sorted(map(str, ids))
    return found


def reconciliation_record(*, spend_batch_id: str, request_ids: list[str], owner: str,
                          reconciled_at: str, billed_calls: Any, billed_cost_cny: Any,
                          evidence_digest: Any, notes: Any = ()) -> dict[str, Any]:
    """The owner's reconciliation of unaccounted calls against the provider bill. It holds no bill
    content: only the billed calls and cost the bill shows and a digest of its export."""
    try:
        moment = _moment(reconciled_at) if reconciled_at.endswith("Z") else None
    except (AttributeError, ValueError):
        moment = None
    if moment is None:
        raise FormalError("reconciled_at must be a UTC timestamp")
    if isinstance(billed_calls, bool) or not isinstance(billed_calls, int) or billed_calls < 0:
        raise FormalError("the billed calls are a non-negative count")
    if (isinstance(billed_cost_cny, bool) or not isinstance(billed_cost_cny, (int, float))
            or not 0 <= billed_cost_cny < float("inf")):
        raise FormalError("the billed cost is a non-negative amount")
    if (not isinstance(evidence_digest, str) or len(evidence_digest) != 71
            or not evidence_digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in evidence_digest[7:])):
        raise FormalError("the bill is named by the SHA-256 of its export, never by its content")
    notes = list(notes or ())
    if len(notes) > 20 or any(not isinstance(item, str) or not item.strip() or len(item) > 500
                              for item in notes):
        raise FormalError("reconciliation notes are at most 20 lines of at most 500 characters")
    body = {"artifact_version": 1, "kind": RECONCILIATION_KIND,
            "protocol_id": protocol.PROTOCOL_ID, "spend_batch_id": spend_batch_id,
            "unaccounted_request_ids": sorted(request_ids),
            "provider_billed_calls": billed_calls, "provider_billed_cost_cny": billed_cost_cny,
            "billing_evidence_digest": evidence_digest, "owner": _owner(owner),
            "reconciled_at": reconciled_at, "conclusion": "RECONCILED", "notes": notes}
    return {**body, "reconciliation_digest": digest_json(body)}


def reconciliation_claims(paths: FormalPaths, spend_batch_id: str) -> dict[str, dict[str, Any]]:
    """Every committed reconciliation of that spend batch, by the calls it covers.

    A batch may owe a bill more than once -- a driver dies, the batch resumes, another dies --
    so each reconciliation is its own record, named by the digest of the calls it accounts for.
    A record is never rewritten; a later debt is a later record.
    """
    directory = paths.workbench / RECONCILIATIONS / spend_batch_id
    claims: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        relative = f"{RECONCILIATIONS}/{spend_batch_id}/{path.name}"
        record, _ = committed_record(paths.workbench, relative)
        body = {key: value for key, value in record.items() if key != "reconciliation_digest"}
        covered = record.get("unaccounted_request_ids")
        if (record.get("kind") != RECONCILIATION_KIND
                or record.get("reconciliation_digest") != digest_json(body)
                or record.get("spend_batch_id") != spend_batch_id
                or not isinstance(covered, list) or not covered
                or sorted(covered) != covered or len(set(covered)) != len(covered)
                or record.get("conclusion") != "RECONCILED"
                or path.name != f"{_claim_name(spend_batch_id, covered)}.json"):
            raise FormalError(f"{relative} does not reconcile the calls it names")
        for other, names in claims.items():
            if set(names["unaccounted_request_ids"]) & set(covered):
                raise FormalError(f"{relative} covers calls already reconciled by {other}")
        claims[relative] = record
    return claims


def _claim_name(spend_batch_id: str, request_ids: list[str]) -> str:
    """A reconciliation's file name: the digest of the calls it accounts for.

    The same debt is the same path, so a retry of the same reconciliation is refused as a
    duplicate rather than written twice, and another debt is another path.
    """
    return digest_json({"protocol_id": protocol.PROTOCOL_ID, "spend_batch_id": spend_batch_id,
                        "unaccounted_request_ids": sorted(request_ids)}).removeprefix("sha256:")


def record_billing_reconciliation(paths: FormalPaths, *, spend_batch_id: str, owner: str,
                                  reconciled_at: str, billed_calls: Any, billed_cost_cny: Any,
                                  evidence_digest: Any, notes: Any = ()) -> dict[str, Any]:
    """Generate and commit, once, the owner's reconciliation of the calls this spend batch still
    owes a bill for."""
    _clean_checkout(paths, "recording a reconciliation")
    calls = unaccounted_calls(paths)
    if spend_batch_id not in calls:
        raise FormalError("no unaccounted call is recorded under that spend batch id")
    covered = sorted(set(calls[spend_batch_id]) - {
        name for record in reconciliation_claims(paths, spend_batch_id).values()
        for name in record["unaccounted_request_ids"]})
    if not covered:
        raise FormalError("every call of that spend batch is already reconciled")
    record = reconciliation_record(
        spend_batch_id=spend_batch_id, request_ids=covered, owner=owner,
        reconciled_at=reconciled_at, billed_calls=billed_calls, billed_cost_cny=billed_cost_cny,
        evidence_digest=evidence_digest, notes=notes)
    relative = f"{RECONCILIATIONS}/{spend_batch_id}/{_claim_name(spend_batch_id, covered)}.json"
    return {"record": relative, "reconciliation_digest": record["reconciliation_digest"],
            "covered_calls": covered,
            "commit": commit_record(paths.workbench, relative, record,
                                    f"p7.4b: reconcile the provider bill for {spend_batch_id}")}


def check_reconciliations(paths: FormalPaths) -> dict[str, str]:
    """Every call whose cost is unknown reconciled in a committed record, and the digest that
    covers each spend batch; a spend ledger that can be written again is not enough.

    The records of one batch must cover its calls exactly: every call accounted for, none twice,
    and none that this batch never made.
    """
    debts = unaccounted_calls(paths)
    directory = paths.workbench / RECONCILIATIONS
    # A batch that owes nothing has no record to write, so a record standing against one is a
    # reconciliation of calls nobody made: it is read here rather than left unexamined.
    try:
        entries = sorted(directory.iterdir()) if directory.is_dir() else []
    except OSError as exc:
        # A directory of reconciliations this gate cannot list is not a batch that owes nothing.
        raise FormalError(f"{RECONCILIATIONS} cannot be read, so what it accounts for is "
                          "unknown") from exc
    stray = [item.name for item in entries if not item.is_dir()]
    if stray:
        # Each batch's reconciliations live in its own directory. A file beside them is a record
        # this check would never read, so it is refused rather than passed over.
        raise FormalError(f"{RECONCILIATIONS} holds a reconciliation outside any spend batch "
                          f"({', '.join(stray[:3])})")
    recorded = [item.name for item in entries]
    digests: dict[str, str] = {}
    missing = []
    for spend_batch_id in sorted({*debts, *recorded}):
        request_ids = debts.get(spend_batch_id)
        claims = reconciliation_claims(paths, spend_batch_id)
        covered = {name for record in claims.values()
                   for name in record["unaccounted_request_ids"]}
        if request_ids is None:
            if claims:
                raise FormalError(f"{RECONCILIATIONS}/{spend_batch_id} reconciles calls that "
                                  "spend batch never left unaccounted")
            continue
        if not claims or covered != set(request_ids):
            missing.append(spend_batch_id)
            continue
        digests[spend_batch_id] = digest_json({
            "spend_batch_id": spend_batch_id, "unaccounted_request_ids": sorted(request_ids),
            "reconciliation_digests": sorted(record["reconciliation_digest"]
                                             for record in claims.values())})
    if missing:
        raise FormalError("a call whose cost is unknown awaits its provider bill "
                          f"reconciliation ({', '.join(missing)}); run reconcile-billing")
    return digests


@dataclass(frozen=True)
class PaidCallPrerequisites:
    checkout: str
    first_freeze: dict[str, Any]
    first_freeze_commit: str
    external_review: dict[str, Any]
    custody: dict[str, Any]


def paid_call_prerequisites(paths: FormalPaths, *,
                            confirm_paid_calls: bool) -> PaidCallPrerequisites:
    """What any paid call needs, checked in this order before anything is sent."""
    if not confirm_paid_calls:
        raise FormalError("paid model calls need --confirm-paid-calls")
    checkout = _clean_checkout(paths, "a paid call")
    first, commit = verified_first_freeze(paths)
    external, _ = verified_external_review(paths)
    custody = verified_custody(paths)
    check_spend_ledger(paths)
    check_reconciliations(paths)
    return PaidCallPrerequisites(checkout, first, commit, external, custody)


# -- provider preflight ----------------------------------------------------------------------------
def _call_record(result: BackendResult) -> dict[str, Any]:
    return {"provider_request_id": result.provider_request_id, "status": result.status,
            "http_status": result.http_status, "response_model": result.response_model,
            "usage": result.usage, "cost_cny": result.cost_cny, "error_code": result.error_code}


def run_preflight(paths: FormalPaths, *, confirm_paid_calls: bool,
                  clock: Callable[[], str] = _utc) -> dict[str, Any]:
    """Five canaries through the evaluation provider, accounted on their own, kept content-free."""
    # The review of 8c4025d: a driver killed mid-attempt leaves a dispatched call that is
    # neither an accounting error nor yet unresolved -- the recovery writes it down -- so the
    # reconciliation gate saw no debt and five paid canaries went out before it existed. The
    # batch that owns that attempt settles its calls when it resumes; this waits for it, the
    # way rearm-fixture already does. It is asked here rather than among the shared
    # prerequisites, because the run that resumes the batch is the one that closes the attempt.
    # The review of f85c1c1: this read the chain once and never took the lock a development
    # batch holds, so a preflight could send all five canaries beside a running batch -- and a
    # cost that batch left unknown partway through reached none of them. The preflight is one of
    # the formal root's paid processes: it holds the same lock, so no batch runs beside it.
    with development_lock(paths):
        return _preflight_under_lock(paths, confirm_paid_calls=confirm_paid_calls, clock=clock)


def _preflight_under_lock(paths: FormalPaths, *, confirm_paid_calls: bool,
                          clock: Callable[[], str]) -> dict[str, Any]:
    chain = development_chain(paths)
    if chain and chain[-1][2]["open_attempt"] is not None:
        raise FormalError("an attempt of the newest development batch is still open; its calls "
                          "settle when that batch resumes, and no canary is sent before they do")
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

    # What the bill already owed when the prerequisites passed. They are reconciled -- the gate
    # above refuses otherwise -- so what matters before each canary is whether another one
    # appeared since, not whether any exists.
    owed = unaccounted_calls(paths)

    def send(body: dict[str, Any]) -> dict[str, Any]:
        # Asked before each canary, not once before the first: a cost that became unknown while
        # this preflight ran must stop the ones that have not gone out.
        if unaccounted_calls(paths) != owed:
            raise canary.PreflightStopped("accounting_failure")
        result = backend.complete(body, attempt_id=run_id, call_index=len(calls) + 1)
        calls.append(_call_record(result))
        if result.status == "accounting_error":
            raise canary.PreflightStopped("accounting_failure")
        return result.payload if result.status == "ok" else {"error": {"code": result.error_code}}

    evaluation = canary.run_preflight(send, budgets=budgets)
    known = [call["cost_cny"] for call in calls if call["cost_cny"] is not None]
    body = {
        "artifact_version": 1, "kind": PREFLIGHT_KIND, "protocol_id": protocol.PROTOCOL_ID,
        "first_freeze_digest": ready.first_freeze["first_freeze_digest"],
        "first_freeze_commit": ready.first_freeze_commit, "checkout": ready.checkout,
        "external_review_digest": ready.external_review["review_digest"],
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


def preflight_for_next_batch(paths: FormalPaths, first_freeze: dict[str, Any]) -> dict[str, Any]:
    """The verified newest preflight; after a batch sealed because the provider refused the
    credential, only one taken after that seal."""
    record = verified_preflight(paths, first_freeze)
    chain = development_chain(paths)
    # A refused credential is read from the gateway's records, so a batch that sealed for another
    # reason -- contract drift beside it, say -- still needs a later preflight.
    refused = bool(chain) and (
        chain[-1][2]["seal_reason"] == "provider_authentication_failed"
        or (chain[-1][2]["status"] != "OPEN" and any(
            entry["kind"] == "model_response_received"
            and entry["body"]["error_code"] == "provider_authentication"
            for entry in chain[-1][1])))
    if refused and (_moment(record["started_at"]) <= _moment(chain[-1][1][-1]["at"])):
        raise FormalError("the provider refused the evaluation credential in the last batch; run "
                          "the preflight again before the next batch")
    return record


# -- the qualification, the fixture and the batch records ----------------------------------------
def rehearsal_isolation_problems(paths: FormalPaths) -> list[str]:
    """What would let a rehearsal's effects reach a formal record or a paid provider."""
    from web_operation_agent.model import DEFAULT_LEDGER

    problems = []
    formal_dir = (paths.private_dir / "formal").resolve()
    if paths.formal_root is None or paths.formal_root.resolve().is_relative_to(formal_dir):
        problems.append("its own formal root")
    if paths.workbench.resolve() == (REPOSITORY / ".workbench").resolve():
        problems.append("its own workbench")
    shared = {Path(DEFAULT_LEDGER).expanduser().resolve(),
              (REPOSITORY / ".artifacts" / "acceptance" / "spend.jsonl").resolve()}
    if os.environ.get("WOA_SPEND_LEDGER"):
        shared.add(Path(os.environ["WOA_SPEND_LEDGER"]).expanduser().resolve())
    if paths.spend_ledger is None or paths.spend_ledger.resolve() in shared:
        problems.append("its own spend ledger")
    try:
        host = urllib.parse.urlsplit(provider_settings(paths)[0]).hostname or ""
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except (FormalError, ValueError):
        loopback = False
    if paths.provider_config_file is None or not loopback:
        problems.append("a loopback provider")
    return problems


def qualification_root(paths: FormalPaths) -> Path:
    """Where this checkout's qualification is read: the private qualification records, or a
    rehearsal's own records while the rehearsal keeps every paid effect apart."""
    if paths.rehearsal_qualifications is None:
        return paths.qualifications
    problems = rehearsal_isolation_problems(paths)
    if problems:
        raise FormalError("a rehearsal's own qualification counts only while its paid effects are "
                          "kept apart; it needs " + ", ".join(problems))
    return paths.rehearsal_qualifications


def qualification_for_head(paths: FormalPaths, *, candidate: dict[str, Any] | None = None,
                           manifest_digest: str | None = None) -> dict[str, Any]:
    """The newest zero-model qualification of this checkout's HEAD, provided it passed under the
    current machine protocol with the fixture manifest and Agent image in place now.

    The paid entry passes the candidate and manifest it verified on the running stack, so a
    manifest taken or an image built after the qualification leaves that qualification behind.
    """
    head = _clean_checkout(paths, "a qualified candidate")
    root = qualification_root(paths)
    try:
        current = json.loads(harness.fixture_manifest_path(paths.private_dir)
                             .read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FormalError("the fixture manifest cannot be read") from exc
    if not isinstance(current, dict) or (manifest_digest is not None
                                         and current.get("manifest_digest") != manifest_digest):
        raise FormalError("the fixture manifest on disk is not the one the batch binds")
    for path in sorted(root.glob("p74a-qualification-*.json"), reverse=True):
        try:
            data = path.read_bytes()
            record = json.loads(data)
        except (OSError, ValueError):
            continue
        qualified = record.get("candidate") if isinstance(record, dict) else None
        if not isinstance(qualified, dict) or qualified.get("commit") != head:
            continue
        if record.get("status") != "READY":
            raise FormalError(f"the newest qualification of this checkout, {path.name}, did not "
                              "pass")
        if (record.get("offline") or {}).get("protocol_digest") != protocol.document()[
                "protocol_digest"]:
            raise FormalError(f"{path.name} qualified another machine protocol")
        if record.get("fixture_manifest_digest") != current.get("manifest_digest"):
            raise FormalError(f"{path.name} was taken with another fixture manifest; qualify again")
        # The qualification names the Agent image it ran and the fixture it ran on. The manifest
        # no longer names a candidate -- rebuilding the Agent leaves the fixture as it was -- so
        # the image is checked against the one the caller verified on the running stack.
        if candidate is not None and qualified != candidate:
            raise FormalError(f"{path.name} qualified another Agent image; qualify again")
        return {"artifact": path.name, "started_at": record.get("started_at"),
                "qualification_digest": "sha256:" + hashlib.sha256(data).hexdigest()}
    raise FormalError("this checkout's HEAD has not passed a zero-model qualification")


def qualification_feedback(paths: FormalPaths, candidate_commit: str) -> dict[str, Any] | None:
    """The submit and cancel feedback checks of the newest READY qualification of a candidate."""
    for path in sorted(qualification_root(paths).glob("p74a-qualification-*.json"), reverse=True):
        try:
            data = path.read_bytes()
            record = json.loads(data)
        except (OSError, ValueError):
            continue
        if (isinstance(record, dict) and record.get("status") == "READY"
                and isinstance(record.get("candidate"), dict)
                and record["candidate"].get("commit") == candidate_commit):
            checks = record.get("checks") if isinstance(record.get("checks"), dict) else {}
            return {"candidate_commit": candidate_commit,
                    "submit_within_300ms": checks.get("submit_feedback_within_300ms") is True,
                    "cancel_within_300ms": checks.get("cancel_feedback_within_300ms") is True,
                    "qualification_digest": "sha256:" + hashlib.sha256(data).hexdigest()}
    return None


def fixture_recovery(paths: FormalPaths) -> dict[str, Any]:
    """The fixture's recovery record, provided resets may run on it."""
    try:
        record = fixture.recovery_record(paths.recovery_dir)
    except (OSError, ValueError, StackError) as exc:
        raise FormalError(f"the fixture recovery record cannot be read: {exc}") from exc
    if record["status"] != "READY":
        raise FormalError(f"the fixture recovery record is {record['status']}; run "
                          "rearm-fixture")
    return {"status": record["status"], "baseline_digest": record["baseline_digest"]}


def batch_records(paths: FormalPaths) -> list[dict[str, Any]]:
    """Every development batch with its state and whether its records are committed."""
    rows = []
    for _, entries, summary in development_chain(paths):
        batch_id = summary["batch"]["batch_id"]
        announced = _committed_or_none(paths.workbench, batch_record_path(batch_id, "header"))
        head = _committed_or_none(paths.workbench, batch_record_path(batch_id, "chain-head"))
        rows.append({
            "batch_id": batch_id, "status": summary["status"],
            "seal_reason": summary["seal_reason"], "pending_seal": summary["pending_seal"],
            "seal_reasons": summary["seal_reasons"],
            "open_attempt": summary["open_attempt"], "attempts": len(summary["attempts"]),
            "header_committed": bool(announced and announced.get("header") == entries[0]["body"]),
            "chain_head_committed": bool(head and head.get("chain_head") == summary["chain_head"]),
        })
    return rows


def check_batch_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refuse a finished batch whose chain head is not committed; the next batch names it."""
    missing = [row["batch_id"] for row in rows
               if row["status"] != "OPEN" and not row["chain_head_committed"]]
    if missing:
        raise FormalError(f"finished batches lack a committed chain head ({', '.join(missing)}); "
                          "run close")
    return rows


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
            "previous_batch": previous, "schedule_seed": seed,
            "ledger_contract_digest": ledger.LEDGER_CONTRACT_DIGEST, **bindings}


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


def link_to(summary: dict[str, Any], *, continuation_record: str | None = None) -> dict[str, Any]:
    """How the next batch names this finished one; after an unaccounted call the link also names
    the reconciliation of the provider bill."""
    return {"batch_id": summary["batch"]["batch_id"], "purpose": summary["batch"]["purpose"],
            "protocol_digest": summary["batch"]["protocol_digest"],
            "candidate_commit": summary["batch"]["candidate"]["commit"],
            "chain_head": summary["chain_head"],
            "termination_reason": (summary["seal_reason"] if summary["status"] == "SEALED"
                                   else "batch_completed"),
            "continuation_record": continuation_record}


def development_chain(paths: FormalPaths) -> list[tuple[Path, list[dict[str, Any]],
                                                        dict[str, Any]]]:
    """Every formal development ledger, first batch first; together they form one chain."""
    by_previous: dict[str | None, tuple[Path, list[dict[str, Any]], dict[str, Any]]] = {}
    for path in sorted(paths.ledgers.glob("*.jsonl")):
        try:
            entries, summary = ledger.read(path)
        except ledger.LedgerContractError as exc:
            raise FormalError(f"{path.name} was written under another ledger contract, so this "
                              "reader never reads it; a chain is read only by the reader that "
                              "wrote it") from exc
        except (OSError, ValueError) as exc:
            raise FormalError(f"{path.name} does not verify: {exc}") from exc
        if summary["batch"]["protocol_id"] != protocol.PROTOCOL_ID:
            raise FormalError(f"{path.name} belongs to protocol {summary['batch']['protocol_id']}, "
                              f"not {protocol.PROTOCOL_ID}")
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
                and last["seal_reason"] in ledger.EXOGENOUS_STOPS):
            raise FormalError("this candidate already ran a development batch; commit a new "
                              "candidate, or continue the same one only after an exogenous stop")
    elif commit in {item[2]["batch"]["candidate"]["commit"] for item in chain}:
        raise FormalError("a candidate that another candidate followed never returns to the "
                          "development chain")


@contextmanager
def development_lock(paths: FormalPaths) -> Iterator[None]:
    """Hold the one development process of a formal root; a second one is refused."""
    paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise FormalError("another development process is using these ledgers") from exc
        yield
    finally:
        os.close(descriptor)


def development_running(paths: FormalPaths) -> bool:
    """Whether a development process holds the formal root's lock; nothing is changed."""
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


def stop_requested(request: Path, batch_id: str) -> bool:
    """Whether the operator asked to stop this batch; a request for another batch is ignored."""
    try:
        value = json.loads(request.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("batch_id") == batch_id


def _request_stop(request: Path, batch_id: str) -> None:
    """Leave a stop request for the running process; it is a control file, not a record."""
    request.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = request.with_name(request.name + ".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_json_bytes({"batch_id": batch_id, "requested_at": _utc()}))
    os.replace(temporary, request)


@dataclass(frozen=True)
class PreparedBatch:
    path: Path
    header: dict[str, Any]
    preflight: dict[str, Any]
    backend: UpstreamBackend
    header_commit: str
    stop_request: Path


def _announce(paths: FormalPaths, header: dict[str, Any], extras: dict[str, Any]) -> str:
    relative = batch_record_path(header["batch_id"], "header")
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


def fixture_manifest_record(paths: FormalPaths, digest: str) -> dict[str, Any]:
    """The full fixture manifest a batch binds. It is announced with the batch, so no later reader
    depends on the file a rebuild replaces."""
    try:
        record = json.loads(harness.fixture_manifest_path(paths.private_dir)
                            .read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FormalError("the fixture manifest cannot be read") from exc
    if not isinstance(record, dict) or record.get("manifest_digest") != digest:
        raise FormalError("the fixture manifest on disk is not the one the batch binds")
    return record


def prepare_development(paths: FormalPaths, ctx: harness.Context, *,
                        confirm_paid_calls: bool) -> PreparedBatch:
    """Verify every prerequisite, then create or resume the batch and announce it; send nothing."""
    ready = paid_call_prerequisites(paths, confirm_paid_calls=confirm_paid_calls)
    preflight = preflight_for_next_batch(paths, ready.first_freeze)
    fixture_recovery(paths)
    try:
        bindings = harness.batch_bindings(ctx)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        raise FormalError(f"the candidate, fixture manifest or task set does not verify: {exc}") \
            from exc
    if bindings["task_set_seal_digest"] != ready.first_freeze["development_seal_digest"]:
        raise FormalError("the development task set is not the one the first freeze sealed")
    if bindings["candidate"]["commit"] != ready.checkout:
        raise FormalError("the running candidate was not built from this checkout")
    manifest = fixture_manifest_record(paths, bindings["fixture"]["manifest_digest"])
    # The paid entry itself, not only the command line, needs this candidate's qualification with
    # the manifest and Agent image it just verified on the running stack.
    qualification = qualification_for_head(paths, candidate=bindings["candidate"],
                                           manifest_digest=bindings["fixture"]["manifest_digest"])
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
        # A batch that held resumes on the bindings it began with: the candidate, the fixture
        # manifest and the provider binding it names are verified above and compared here, so a
        # batch cannot wait out a rebuild and then run on something else.
        if header != development_header(previous=header["previous_batch"], **derived):
            raise FormalError("the open development batch runs under other bindings; seal it "
                              "with operator_stop before another starts")
        held = ledger.read(path)[1]["cost_unknown"]
        if held:
            # Every prerequisite above already refused an unreconciled call, so what is left is
            # to apply the records that account for these ones: that is what lifts the hold.
            coverage = check_reconciliations(paths)[batch_id]
            for record in reconciliation_claims(paths, batch_id).values():
                covered = sorted(set(record["unaccounted_request_ids"]) & set(held))
                if covered:
                    ledger.append(path, "billing_reconciliation_applied", {
                        "covered_calls": covered,
                        "reconciliation_digest": record["reconciliation_digest"],
                        "coverage_digest": coverage})
    else:
        check_candidate_may_run(chain, bindings["candidate"]["commit"])
        check_batch_records(batch_records(paths))
        previous = None
        if chain:
            last = chain[-1][2]
            # After an unaccounted call the link names the reconciliation of the provider bill.
            record = (check_reconciliations(paths)[last["batch"]["batch_id"]]
                      if last["seal_reason"] == "accounting_failure" else None)
            previous = link_to(last, continuation_record=record)
        header = development_header(previous=previous, **derived)
        path = paths.ledgers / f"{batch_id}.jsonl"
        try:
            ledger.create(path, header)
        except ledger.LedgerError as exc:
            raise FormalError(f"the attempt ledger refuses this batch: {exc}") from exc
    commit = _announce(paths, header, {
        "first_freeze_commit": ready.first_freeze_commit,
        "preflight_digest": preflight["preflight_digest"],
        "escrow_custody_digest": ready.custody["custody_digest"],
        "external_review_digest": ready.external_review["review_digest"],
        "qualification_digest": qualification["qualification_digest"],
        "fixture_manifest": manifest})
    return PreparedBatch(path, header, preflight, backend, commit, paths.stop_request)


def _attempt_plan(ctx: harness.Context, slot: dict[str, Any]) -> AttemptPlan:
    runner, evaluator = ctx.inputs(slot["task_id"])
    return AttemptPlan(slot={key: slot[key] for key in ("slot_id", "arm", "task_id", "replicate")},
                       runner_input=runner, evaluator_input=evaluator)


def _check_provider_model(path: Path, summary: dict[str, Any],
                          preflight_model: str) -> dict[str, Any]:
    """Seal a running batch whose provider now answers under a model the preflight never saw."""
    if (summary["status"] == "OPEN" and summary["provider_model"] is not None
            and summary["provider_model"] != preflight_model):
        return ledger.append(path, "batch_sealed", {"reason": "model_identity_changed"})
    return summary


def _seal_if_owed(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    if summary["disposition"] == "must_seal":
        return ledger.append(path, "batch_sealed", {"reason": summary["pending_seal"]})
    return summary


def _seal(path: Path, summary: dict[str, Any], reason: str) -> dict[str, Any]:
    """Seal an open batch for ``reason``, or for the seal it already owes."""
    return ledger.append(path, "batch_sealed", {"reason": summary["pending_seal"] or reason})


def drive_batch(ctx: harness.Context, prepared: PreparedBatch, playwright: Any) -> dict[str, Any]:
    """Run the batch's remaining slots in schedule order, closing an interrupted attempt first.

    A stop the operator requested for this batch seals it with ``operator_stop`` before the next
    attempt would start; an attempt already running is never cut short.
    """
    path = prepared.path
    batch_id = prepared.header["batch_id"]
    preflight_model = prepared.preflight["canary"]["model_strings"][0]

    def stopped(summary: dict[str, Any]) -> bool:
        if summary["status"] == "OPEN" and stop_requested(prepared.stop_request, batch_id):
            _seal(path, summary, "operator_stop")
            return True
        return False

    with harness.open_driver(ctx, path, playwright, backend=prepared.backend) as driver:
        summary = ledger.read(path)[1]
        if summary["open_attempt"] is not None:
            summary = _seal_if_owed(path, driver.recover_open_attempt())
        # The review of f85c1c1: a batch that held while the provider had already answered under
        # another model string could not be sealed for it -- the ledger refused every reason
        # while it held -- so the entry resumed, ran the replacement and the next slot, and only
        # then compared. The drift is a fault the batch met, and §7 seals for it at once, so it
        # is asked here, before anything else this run would send.
        summary = _check_provider_model(path, summary, preflight_model)
        # The slot an interrupted attempt ran is owed its replacement whether the driver that ran
        # it died in this process or in an earlier one. The qualification of 6382d75: this stood
        # inside the recovery above, and a batch that held for a bill had already closed that
        # attempt in the run before, so the run that resumed it skipped the replacement its slot
        # was owed and went on to the next slot. A driver that died may also have left a call
        # nobody settled; while its cost is unknown neither the replacement nor a slot runs.
        last = summary["attempts"][-1] if summary["attempts"] else None
        if (last is not None and summary["disposition"] == "running"
                and last["replacement_eligible"] and last["replacement_of"] is None
                # Asked after the disposition, which `stopped` is not part of: it seals, and a
                # batch that holds refuses a seal, so asking it first would raise here.
                and not stopped(summary)):
            slot = next(item for item in summary["slots"] if item["slot_id"] == last["slot_id"])
            plan = _attempt_plan(ctx, slot)
            plan.replacement_of = last["attempt_id"]
            summary = _seal_if_owed(path, driver.run_attempt(plan))
            # The review of 8c4025d: the model string was compared against the preflight only
            # after a slot, never after this replacement -- and a batch whose killed attempt
            # answered nothing gets its first answer here.
            summary = _check_provider_model(path, summary, preflight_model)
        for slot in summary["slots"]:
            current = ledger.read(path)[1]
            if next(item for item in current["slots"] if item["slot_id"] == slot["slot_id"])[
                    "attempts"]:
                continue
            # Sealed, complete, holding or owing a seal: only a running batch takes the slot.
            if current["disposition"] != "running" or stopped(current):
                break
            current = driver.run_slot(_attempt_plan(ctx, slot))
            _check_provider_model(path, current, preflight_model)
    summary = harness.finish_batch(path)
    if summary["status"] != "OPEN" and stop_requested(prepared.stop_request, batch_id):
        prepared.stop_request.unlink(missing_ok=True)
    return summary


def close_batch(paths: FormalPaths, summary: dict[str, Any]) -> dict[str, Any]:
    """Commit a finished batch's chain head; an unfinished batch is left open to resume."""
    batch_id = summary["batch"]["batch_id"]
    costs = [item["cost"] for item in summary["attempts"] if item["cost"]]
    value = {"artifact_version": 1, "kind": "p74_batch_chain_head_record", "batch_id": batch_id,
             "status": summary["status"], "seal_reason": summary["seal_reason"],
             # What the batch sealed for is the most serious reason it met; the others are kept
             # here rather than lost behind it.
             "seal_reasons": summary["seal_reasons"],
             "chain_head": summary["chain_head"], "attempts": len(summary["attempts"]),
             "model_calls": sum(cost["model_calls"] for cost in costs),
             "usage_unavailable_calls": sum(cost["usage_unavailable_calls"] for cost in costs),
             "known_cost_cny": round(sum(cost["known_cost_cny"] for cost in costs), 6)}
    if summary["status"] != "OPEN":
        commit_record(paths.workbench, batch_record_path(batch_id, "chain-head"), value,
                      f"p7.4b: close {batch_id}")
    return value


def run_development(paths: FormalPaths, *, confirm_paid_calls: bool) -> dict[str, Any]:
    with development_lock(paths):
        ctx = harness.load_context(paths.root, private_dir=paths.private_dir)
        prepared = prepare_development(paths, ctx, confirm_paid_calls=confirm_paid_calls)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            summary = drive_batch(ctx, prepared, playwright)
        return close_batch(paths, summary)


def stop_development(paths: FormalPaths) -> dict[str, Any]:
    """Stop the open development batch safely.

    With a development process running, a stop request is left for it, and the batch seals with
    ``operator_stop`` before its next attempt. Otherwise an interrupted attempt is first closed
    from its persisted evidence, as a resumed batch would close it, and the batch seals at once,
    for the seal it already owes if there is one.
    """
    chain = development_chain(paths)
    if not chain or chain[-1][2]["status"] != "OPEN":
        raise FormalError("no development batch is open")
    path, _, summary = chain[-1]
    batch_id = summary["batch"]["batch_id"]
    if development_running(paths):
        _request_stop(paths.stop_request, batch_id)
        return {"status": "STOP_REQUESTED", "batch_id": batch_id}
    with development_lock(paths):
        if summary["open_attempt"] is not None:
            ctx = harness.load_context(paths.root, private_dir=paths.private_dir)
            from playwright.sync_api import sync_playwright

            # Recovery sends no model request; the scripted backend stands where none is used.
            with sync_playwright() as playwright, harness.open_driver(
                    ctx, path, playwright, backend=ScriptedBackend()) as driver:
                driver.recover_open_attempt()
        summary = _seal(path, ledger.read(path)[1], "operator_stop")
        paths.stop_request.unlink(missing_ok=True)
    return close_batch(paths, summary)


def close_finished_batches(paths: FormalPaths) -> list[dict[str, Any]]:
    """Commit the chain head of every finished development batch; committed ones are unchanged."""
    if development_running(paths):
        raise FormalError("a development process is running and commits its own chain head")
    return [close_batch(paths, summary) for _, _, summary in development_chain(paths)
            if summary["status"] != "OPEN"]


def archive_development_chain(paths: FormalPaths) -> dict[str, Any]:
    """Archive this protocol version's finished development chain for a successor that cannot
    read it, from the reader's own clean checkout.

    The record keeps the report this reader recomputes and its digest, the reader commit and
    protocol digest, and the full fixture manifest every batch was announced with. The ledgers,
    announcements and chain heads stay where they are; a later protocol version opens its own
    chain in its own directories and never reads this one.
    """
    commit = _clean_checkout(paths, "archiving a development chain")
    if development_running(paths):
        raise FormalError("a development process is running")
    chain = development_chain(paths)
    if not chain:
        raise FormalError("no formal development batch exists")
    if chain[-1][2]["status"] == "OPEN":
        raise FormalError("the newest development batch is open; stop it before archiving")
    check_batch_records(batch_records(paths))
    value = development_report(paths)
    manifests: dict[str, Any] = {}
    batches = []
    for _, entries, summary in chain:
        header = entries[0]["body"]
        record, announced = committed_record(paths.workbench,
                                             batch_record_path(header["batch_id"], "header"))
        manifest = record.get("fixture_manifest")
        if (not isinstance(manifest, dict)
                or manifest.get("manifest_digest") != header["fixture"]["manifest_digest"]):
            raise FormalError(f"{header['batch_id']} was announced without the fixture manifest "
                              "it binds")
        manifests[manifest["manifest_digest"]] = manifest
        batches.append({"batch_id": header["batch_id"], "status": summary["status"],
                        "seal_reason": summary["seal_reason"],
                        "chain_head": summary["chain_head"], "announcement_commit": announced})
    body = {"artifact_version": 1, "kind": CHAIN_ARCHIVE_KIND, "protocol_id": protocol.PROTOCOL_ID,
            "ledger_contract_digest": ledger.LEDGER_CONTRACT_DIGEST,
            "reader": {"commit": commit, "protocol_digest": protocol.document()["protocol_digest"],
                       "contract_files": list(ledger.CONTRACT_FILES)},
            "batches": batches, "report": value, "report_digest": digest_json(value),
            "fixture_manifests": manifests}
    record = {**body, "archive_digest": digest_json(body)}
    relative = chain_archive_path()
    return {"record": relative, "archive_digest": record["archive_digest"],
            "report_digest": record["report_digest"],
            "commit": commit_record(paths.workbench, relative, record,
                                    f"p7.4b: archive the {protocol.PROTOCOL_ID} development chain")}


def verify_chain_archive(paths: FormalPaths, *, protocol_id: str | None = None,
                         recompute: bool = False) -> dict[str, Any]:
    """A committed chain archive, unchanged since it was sealed.

    ``recompute`` also recomputes its report, which only the reader that wrote the chain can do:
    any other reader refuses rather than read the chain by rules it was not written under.
    """
    relative = chain_archive_path(protocol_id)
    record, commit = committed_record(paths.workbench, relative)
    body = {key: value for key, value in record.items() if key != "archive_digest"}
    if (record.get("kind") != CHAIN_ARCHIVE_KIND
            or record.get("archive_digest") != digest_json(body)
            or body.get("report_digest") != digest_json(body.get("report"))):
        raise FormalError(f"{relative} was changed after it was sealed")
    try:
        report.verify_report(record["report"])
    except ValueError as exc:
        raise FormalError(f"{relative} holds a report that does not verify: {exc}") from exc
    result = {"record": relative, "archive_digest": record["archive_digest"], "commit": commit,
              "reader_commit": record["reader"]["commit"], "recomputed": None}
    if recompute:
        if (record["ledger_contract_digest"] != ledger.LEDGER_CONTRACT_DIGEST
                or record["protocol_id"] != protocol.PROTOCOL_ID):
            raise FormalError("only the reader that wrote this chain recomputes its report; check "
                              f"out {record['reader']['commit']}")
        result["recomputed"] = digest_json(development_report(paths)) == record["report_digest"]
    return result


def rearm_fixture(paths: FormalPaths, *, confirm_rearm: bool) -> dict[str, Any]:
    """Restore the fixture baseline after a failed or uncertain reset, and verify it.

    A baseline that cannot be restored and verified voids the fixture: it has to be provisioned
    again, which gives it a new manifest that no earlier batch binds.
    """
    if not confirm_rearm:
        raise FormalError("re-arming rebuilds the fixture database from its baseline; pass "
                          "--confirm-rearm")
    try:
        record = fixture.recovery_record(paths.recovery_dir)
    except (OSError, ValueError, StackError) as exc:
        raise FormalError(f"the fixture recovery record cannot be read: {exc}") from exc
    if record["status"] == "READY":
        return {"status": "READY", "restored": False}
    if development_running(paths):
        raise FormalError("a development process is running")
    chain = development_chain(paths)
    if chain and chain[-1][2]["open_attempt"] is not None:
        raise FormalError("an interrupted attempt is still open; stop the batch first")
    ctx = harness.load_context(paths.root, private_dir=paths.private_dir)
    result = fixture.restore(
        ctx.stack, fixture.FixtureGuard(ctx.stack), paths.recovery_dir,
        actor_projection=lambda: actor_projection(ctx.stack.rpc_url, ctx.keys),
        rearm_after_failure=True)
    if result["status"] != "PASS":
        raise FormalError("the baseline could not be restored and verified ("
                          + ", ".join(result.get("basis") or ["no outcome"]) + "); the fixture "
                          "is void: provision it again with python -m evals.odoo.provision "
                          "--destroy-first")
    return {"status": "READY", "restored": True, "guard_digest": result["guard_digest"]}


def development_report(paths: FormalPaths) -> dict[str, Any]:
    """The public report over the whole development chain, gated on the frozen plan."""
    chain = development_chain(paths)
    if not chain:
        raise FormalError("no formal development batch exists")
    first, commit = verified_first_freeze(paths)
    for _, entries, _ in chain:
        header = entries[0]["body"]
        record, announced = committed_record(paths.workbench,
                                             batch_record_path(header["batch_id"], "header"))
        if record.get("header") != header:
            raise FormalError(f"{header['batch_id']} ran under a header it did not announce")
        started = next((entry["at"] for entry in entries if entry["kind"] == "attempt_started"),
                       None)
        try:
            late = started is not None and commit_time(paths.workbench, announced) > _moment(
                started)
        except RevisionError as exc:
            raise FormalError(str(exc)) from exc
        if late:
            raise FormalError(f"{header['batch_id']} was announced after its first attempt")
    # The review of 8c4025d: the report read the task set from disk and never held it against
    # the seal the freeze bound, so moving one task from normal to refusal after a batch
    # completed changed the denominator the gate divides by -- twenty-five to twenty-four --
    # and could turn a failing screening gate into a passing one, with both reports verifying.
    # The classes and strata it reads are the frozen ones or the report is refused.
    try:
        task_set = json.loads(harness.DEVELOPMENT_TASK_SET.read_text(encoding="utf-8"))
        seal = tasks.verify_task_content(
            task_set, json.loads(harness.DEVELOPMENT_SEAL.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, tasks.TaskSetError) as exc:
        raise FormalError(f"the development task set does not match its seal: {exc}") from exc
    if seal["seal_digest"] != first["development_seal_digest"]:
        raise FormalError("the development task set is not the one the first freeze sealed")
    # ``record`` is now the newest batch's announcement, which names the preflight it relied on.
    preflight = preflight_record(paths, record.get("preflight_digest"))
    plan = development_plan(first_freeze=first, first_freeze_commit=commit,
                            header=chain[-1][1][0]["body"], task_ids=task_ids(task_set),
                            binding_digest=preflight["provider_binding_digest"])
    metadata = {task["task_id"]: {"class": task["class"], "stratum": task["stratum"],
                                  "read_only": task["max_mutations"] == 0}
                for task in task_set["tasks"]}
    value = report.build_report(batches=[summary for _, _, summary in chain], tasks=metadata,
                                plan=plan, feedback=qualification_feedback(
                                    paths, plan["candidate_commit"]))
    report.verify_report(value)
    return value


# -- status and command line ---------------------------------------------------------------------
def status(paths: FormalPaths) -> dict[str, Any]:
    """Whether each step could start now and, if not, why; nothing is sent or changed."""
    checks: dict[str, str] = {}
    found: dict[str, Any] = {}

    def check(name: str, step: Callable[[], Any]) -> None:
        try:
            found[name] = step()
        except (FormalError, RevisionError) as exc:
            checks[name] = f"refused: {exc}"
        else:
            checks[name] = "READY"

    def records() -> list[dict[str, Any]]:
        found["batches"] = batch_records(paths)
        return check_batch_records(found["batches"])

    check("clean_checkout", lambda: require_clean_head(paths.repository))
    check("first_freeze", lambda: verified_first_freeze(paths))
    check("external_review", lambda: verified_external_review(paths))
    check("escrow_custody", lambda: verified_custody(paths))
    check("spend_ledger", lambda: check_spend_ledger(paths))
    check("billing_reconciliation", lambda: check_reconciliations(paths))
    check("provider_configuration", lambda: provider_backend(paths, batch_id="p74b-status"))
    if "first_freeze" in found:
        check("preflight", lambda: preflight_for_next_batch(paths, found["first_freeze"][0]))
    else:
        checks["preflight"] = "refused: needs the first freeze"
    check("qualification", lambda: qualification_for_head(paths))
    check("fixture_recovery", lambda: fixture_recovery(paths))
    check("batch_records", records)
    binding = None
    if checks["provider_configuration"] == "READY":
        try:
            binding = digest_binding(runtime_binding(paths))
        except FormalError:
            binding = None
    preflight = all(checks[name] == "READY" for name in PREFLIGHT_CHECKS)
    stop = None
    try:
        request = json.loads(paths.stop_request.read_text(encoding="utf-8"))
        stop = request.get("batch_id") if isinstance(request, dict) else None
    except (OSError, ValueError):
        stop = None
    return {"artifact_version": 1, "kind": "p74_formal_status", "checks": checks,
            "preflight_can_start": preflight,
            "development_can_start": preflight and all(
                checks[name] == "READY" for name in DEVELOPMENT_CHECKS),
            "development_also_needs": ("the verified candidate and a fixture manifest describing "
                                       "the fixture it runs on, checked when the batch starts"),
            "runtime_binding_digest": binding, "batches": found.get("batches"),
            "development_running": development_running(paths), "stop_requested_for": stop,
            "heldout": HELDOUT_UNAVAILABLE}


def _allowed(paths: FormalPaths, gate: str) -> None:
    state = status(paths)
    if not state[gate]:
        refused = {name: value for name, value in state["checks"].items() if value != "READY"}
        raise FormalError("status does not allow this step: "
                          + json.dumps(refused, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.odoo.formal")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    for name in ("preflight", "development"):
        commands.add_parser(name).add_argument("--confirm-paid-calls", action="store_true")
    commands.add_parser("report")
    commands.add_parser("stop")
    commands.add_parser("close")
    commands.add_parser("rearm-fixture").add_argument("--confirm-rearm", action="store_true")
    review = commands.add_parser("family-review")
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reviewed-at", required=True, help="UTC, such as 2026-09-20T08:00:00Z")
    review.add_argument("--verdict", required=True, choices=freeze.REVIEW_VERDICTS)
    review.add_argument("--finding", action="append", default=[])
    commands.add_parser("external-review").add_argument(
        "--record", required=True, help="the reviewer's JSON conclusions, one verdict per item")
    frozen = commands.add_parser("freeze")
    frozen.add_argument("stage", choices=("first", "second", "third"))
    frozen.add_argument("--accept-not-robust", action="store_true")
    commands.add_parser("verify-freezes")
    commands.add_parser("archive-chain")
    archived = commands.add_parser("verify-archive")
    archived.add_argument("--protocol-id", default=None)
    archived.add_argument("--recompute", action="store_true")
    reconciled = commands.add_parser("reconcile-billing")
    reconciled.add_argument("--spend-batch", required=True)
    reconciled.add_argument("--owner", required=True)
    reconciled.add_argument("--reconciled-at", required=True,
                            help="UTC, such as 2026-09-20T08:00:00Z")
    reconciled.add_argument("--billed-calls", required=True, type=int)
    reconciled.add_argument("--billed-cost-cny", required=True, type=float)
    reconciled.add_argument("--evidence-digest", required=True,
                            help="sha256: of the bill export, which stays outside the repository")
    reconciled.add_argument("--note", action="append", default=[])
    args = parser.parse_args(argv)
    paths = FormalPaths()
    try:
        if args.command == "status":
            result = status(paths)
        elif args.command == "preflight":
            if not args.confirm_paid_calls:
                raise FormalError("paid model calls need --confirm-paid-calls")
            _allowed(paths, "preflight_can_start")
            record = run_preflight(paths, confirm_paid_calls=True)
            result = {key: record[key] for key in ("status", "spend_batch_id", "known_cost_cny",
                                                   "usage_unavailable_calls")}
            result |= {"checks": record["canary"]["checks"],
                       "model_strings": record["canary"]["model_strings"],
                       "stopped": record["canary"]["stopped"]}
        elif args.command == "development":
            if not args.confirm_paid_calls:
                raise FormalError("paid model calls need --confirm-paid-calls")
            _allowed(paths, "development_can_start")
            result = run_development(paths, confirm_paid_calls=True)
        elif args.command == "stop":
            result = stop_development(paths)
        elif args.command == "close":
            result = {"closed": close_finished_batches(paths)}
        elif args.command == "rearm-fixture":
            result = rearm_fixture(paths, confirm_rearm=args.confirm_rearm)
        elif args.command == "external-review":
            result = record_external_review(paths, review_file=Path(args.record))
        elif args.command == "family-review":
            result = record_family_review(paths, reviewer=args.reviewer,
                                          reviewed_at=args.reviewed_at, verdict=args.verdict,
                                          findings=args.finding)
        elif args.command == "freeze":
            result = (record_first_freeze(paths) if args.stage == "first"
                      else record_second_freeze(paths, accept_not_robust=args.accept_not_robust)
                      if args.stage == "second" else record_third_freeze(paths))
        elif args.command == "verify-freezes":
            result = verify_freezes(paths)
        elif args.command == "archive-chain":
            result = archive_development_chain(paths)
        elif args.command == "verify-archive":
            result = verify_chain_archive(paths, protocol_id=args.protocol_id,
                                          recompute=args.recompute)
        elif args.command == "reconcile-billing":
            result = record_billing_reconciliation(
                paths, spend_batch_id=args.spend_batch, owner=args.owner,
                reconciled_at=args.reconciled_at, billed_calls=args.billed_calls,
                billed_cost_cny=args.billed_cost_cny, evidence_digest=args.evidence_digest,
                notes=args.note)
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
