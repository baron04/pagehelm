"""Batch paths, provider binding, qualification and immutable header setup."""
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

class BatchError(RuntimeError):
    """A batch cannot start, continue or be reported, and why."""


@dataclass(frozen=True)
class BatchPaths:
    repository: Path = revision.REPOSITORY
    private_dir: Path = harness.PRIVATE
    #: Ledgers, attempt evidence, reports and reconciliations. None is ``<private_dir>/batches``.
    root_dir: Path | None = None
    #: The provider endpoint and key file. None is ``<private_dir>/provider/provider.json``.
    provider_config_file: Path | None = None
    #: None accounts to the product's own spend ledger, like every paid call of the project.
    spend_ledger: Path | None = None

    @property
    def root(self) -> Path:
        return self.root_dir or self.private_dir / "batches"

    @property
    def ledgers(self) -> Path:
        return self.root / "ledgers"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def reconciliations(self) -> Path:
        return self.root / "reconciliations"

    @property
    def stop_request(self) -> Path:
        return self.root / "stop-request.json"

    @property
    def lock(self) -> Path:
        return self.root / "batch.lock"

    @property
    def provider_config(self) -> Path:
        return self.provider_config_file or self.private_dir / "provider" / "provider.json"

    @property
    def qualifications(self) -> Path:
        return self.private_dir / "qualification"

    def spend(self) -> Path:
        from web_operation_agent.modeling.client import DEFAULT_LEDGER

        return Path(self.spend_ledger or os.environ.get("WOA_SPEND_LEDGER") or DEFAULT_LEDGER)


# -- provider ----------------------------------------------------------------------------------
def provider_settings(paths: BatchPaths) -> tuple[str, Path]:
    """The provider endpoint and key file; the key must be readable by its owner only."""
    try:
        settings = json.loads(paths.provider_config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BatchError("the evaluation provider is not configured") from exc
    if (not isinstance(settings, dict) or set(settings) != {"base_url", "key_file"}
            or not all(isinstance(value, str) for value in settings.values())):
        raise BatchError("the provider configuration names exactly base_url and key_file")
    key = Path(settings["key_file"])
    if not key.is_file() or key.stat().st_mode & 0o077:
        raise BatchError("the evaluation key must be a regular file only its owner can read")
    return settings["base_url"], key


def provider_backend(paths: BatchPaths, *, batch_id: str) -> UpstreamBackend:
    """The provider backend for one batch; constructing it sends nothing."""
    base_url, key = provider_settings(paths)
    spend = paths.spend()
    probe = spend if spend.exists() else next(
        (parent for parent in spend.parents if parent.exists()), None)
    if probe is None or not os.access(probe, os.W_OK):
        raise BatchError("the spend ledger cannot be written, and every paid call is accounted "
                         "to it")
    try:
        return UpstreamBackend(base_url=base_url, key_file=key,
                               requested_model=protocol.REQUESTED_MODEL_ALIAS,
                               spend_ledger=spend, batch_id=batch_id,
                               provider=protocol.PROVIDER["provider_id"])
    except (OSError, ValueError) as exc:
        raise BatchError(f"the evaluation provider configuration is invalid: {exc}") from exc


def binding_digest(paths: BatchPaths) -> str:
    """The provider binding this configuration describes; nothing is sent."""
    base_url, key = provider_settings(paths)
    try:
        identity = upstream_identity(base_url=base_url, key=read_evaluation_key(key),
                                     provider_id=protocol.PROVIDER["provider_id"])
    except (OSError, ValueError) as exc:
        raise BatchError(f"the evaluation provider configuration is invalid: {exc}") from exc
    return provider_binding_digest(identity, harness.protocol_budgets())


# -- the candidate's qualification ---------------------------------------------------------------
def _records(paths: BatchPaths, pattern: str) -> Iterator[tuple[Path, bytes, dict[str, Any]]]:
    """Qualification records matching ``pattern``, newest first; unreadable ones are skipped."""
    for path in sorted(paths.qualifications.glob(pattern), reverse=True):
        try:
            data = path.read_bytes()
            record = json.loads(data)
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            yield path, data, record


def _feedback(candidate: dict[str, Any], data: bytes, record: dict[str, Any]) -> dict[str, Any]:
    checks = record.get("checks") if isinstance(record.get("checks"), dict) else {}
    return {"candidate_commit": candidate["commit"],
            "submit_within_300ms": checks.get("submit_feedback_within_300ms") is True,
            "cancel_within_300ms": checks.get("cancel_feedback_within_300ms") is True,
            "qualification_digest": "sha256:" + hashlib.sha256(data).hexdigest()}


def _passed(path: Path, record: dict[str, Any], candidate: dict[str, Any],
            manifest_digest: str) -> None:
    """Refuse the newest record of this candidate unless it passed with this manifest and this
    Agent image."""
    if record.get("status") != "READY":
        raise BatchError(f"the newest qualification of this candidate, {path.name}, did not pass")
    if record.get("fixture_manifest_digest") != manifest_digest:
        raise BatchError(f"{path.name} was taken with another fixture manifest; qualify again")
    if record.get("candidate") != candidate:
        raise BatchError(f"{path.name} qualified another Agent image; qualify again")


def qualification(paths: BatchPaths, candidate: dict[str, Any],
                  manifest_digest: str) -> dict[str, Any]:
    """The zero-model qualification a candidate needs before a paid batch.

    The candidate's own full qualification, when it has one. Otherwise its smoke qualification,
    provided an earlier commit in its history passed the full one on this fixture and nothing
    under ``evals/`` has changed since: a change to the product alone is checked for what it can
    break, and the apparatus it runs on is the one already proved.
    """
    commit = candidate["commit"]
    for path, data, record in _records(paths, "p74a-qualification-*.json"):
        if (record.get("candidate") or {}).get("commit") == commit:
            _passed(path, record, candidate, manifest_digest)
            return {"artifact": path.name, "kind": "full",
                    "feedback": _feedback(candidate, data, record)}
    smoke = next(((path, data, record) for path, data, record
                  in _records(paths, "p74a-smoke-*.json")
                  if (record.get("candidate") or {}).get("commit") == commit), None)
    if smoke is None:
        raise BatchError("this candidate has not passed a zero-model qualification; run "
                         "python -m evals.odoo.qualify smoke, or qualify run after a change "
                         "under evals/")
    path, data, record = smoke
    _passed(path, record, candidate, manifest_digest)
    for full_path, _, full in _records(paths, "p74a-qualification-*.json"):
        base = (full.get("candidate") or {}).get("commit")
        if (full.get("status") != "READY" or full.get("fixture_manifest_digest") != manifest_digest
                or not isinstance(base, str)):
            continue
        try:
            if not revision.is_ancestor(paths.repository, base, commit):
                continue
            changed = revision.changed_between(paths.repository, base, commit, "evals/")
        except revision.RevisionError:
            continue
        if changed:
            raise BatchError(f"evals/ changed since {full_path.name} ({', '.join(changed[:3])}); "
                             "run the full qualification")
        return {"artifact": path.name, "kind": "smoke", "full_qualification": full_path.name,
                "feedback": _feedback(candidate, data, record)}
    raise BatchError("no earlier commit in this candidate's history passed the full "
                     "qualification on this fixture; run python -m evals.odoo.qualify run")


# -- the task set, the schedule and the header ---------------------------------------------------
def task_set(paths: BatchPaths, population: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A split's task set and the seal it must still match."""
    source = (harness.DEVELOPMENT_TASK_SET if population == "development"
              else harness.heldout_task_set_path(paths.private_dir))
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
        seal = tasks.verify_task_content(
            value, json.loads(harness.seal_path(population).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, tasks.TaskSetError) as exc:
        raise BatchError(f"the {population} task set does not match its seal: {exc}") from exc
    return value, seal


def header(*, batch_id: str, population: str, bindings: dict[str, Any],
           tasks_run: dict[str, Any], provider_binding: str) -> dict[str, Any]:
    """A batch header: what the batch runs on, fixed before it runs anything."""
    ids = sorted(task["task_id"] for task in tasks_run["tasks"])
    seed = protocol.schedule_seed(population, seal_digest=bindings["task_set_seal_digest"],
                                  commit=bindings["candidate"]["commit"])
    eligible = protocol.pageagent_eligibility(tasks_run) if population == "heldout" else ()
    slots = protocol.fixed_schedule(population, ids, seed=seed, pageagent_eligible=eligible)
    return {"batch_id": batch_id, "protocol_id": protocol.PROTOCOL_ID,
            "protocol_digest": protocol.document()["protocol_digest"],
            "schedule_digest": ledger.schedule_digest(slots), "purpose": population,
            "provider_config_digest": protocol.provider_config_digest(),
            "provider_binding_digest": provider_binding, "slots": slots,
            "schedule_seed": seed, **bindings}


def plan(batch_header: dict[str, Any]) -> dict[str, Any]:
    """What the report gates the batch against: the header it declared before it ran."""
    return {"purpose": batch_header["purpose"],
            "protocol_digest": batch_header["protocol_digest"],
            "task_set_seal_digest": batch_header["task_set_seal_digest"],
            "fixture_manifest_digest": batch_header["fixture"]["manifest_digest"],
            "capability_digests": batch_header["capability_digests"],
            "candidate_commit": batch_header["candidate"]["commit"],
            "provider_config_digest": batch_header["provider_config_digest"],
            "provider_binding_digest": batch_header["provider_binding_digest"],
            "schedule_digest": batch_header["schedule_digest"]}


