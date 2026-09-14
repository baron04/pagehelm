"""The three P7.4 freezes as a machine-checked sequence.

The first freeze binds both task-set seals, their split verification and an accepted
independent family review; without an accepted review there is no first freeze. The second
binds that first freeze to the candidate: its commit, the Agent image labelled with it, the
apparatus and machine-protocol files read from the commit itself, and the capability, provider
and statistics digests. The third derives the held-out schedule seed from the commit that
recorded the second freeze, fixes that schedule and seals the protocol document.

Every record carries its own digest and names the digest of the freeze before it.
:func:`verify_chain` refuses a record of the wrong kind, a record changed after it was sealed
and a broken link, so ``protocol.document`` cannot be handed an arbitrary object as a freeze;
the ``verify_*`` functions additionally recompute a record from the artifacts it names.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import protocol, tasks
from .population import digest_json
from .revision import (REPOSITORY, RevisionError, commit_exists, file_at, require_clean_head,
                       require_commit)

FREEZE_VERSION = 1
REVIEW_VERDICTS = ("ACCEPT", "REVISE")
REVIEW_CHECKS = ("family_list", "duplicate_families", "shape_distribution")
#: The machine protocol itself. The protocol digest cannot cover the file that computes it, so
#: these files are not in ``protocol.APPARATUS_MODULES``; the second freeze binds them instead.
MACHINE_PROTOCOL_FILES = ("evals/odoo/protocol.py", "evals/odoo/freeze.py")
_REVIEWER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@+-]{1,79}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class FreezeError(ValueError):
    """A freeze is missing, malformed, changed, or does not bind what it claims to bind."""


def _sealed(body: dict[str, Any], field: str) -> dict[str, Any]:
    return {**body, field: digest_json(body)}


def _body(record: Any, kind: str, field: str) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("kind") != kind:
        raise FreezeError(f"expected a {kind} record")
    body = {key: value for key, value in record.items() if key != field}
    if record.get(field) != digest_json(body):
        raise FreezeError(f"the {kind} record was changed after it was sealed")
    return body


def _utc(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise FreezeError(f"{name} must be a UTC timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FreezeError(f"{name} is not a timestamp") from exc
    return value


def _split(development_seal: Any, heldout_seal: Any) -> dict[str, Any]:
    try:
        return tasks.verify_split_separation(development_seal, heldout_seal)
    except tasks.TaskSetError as exc:
        raise FreezeError(f"the task-set seals do not verify: {exc}") from exc


# -- the independent family review ---------------------------------------------------------------
def family_review_record(*, reviewer: str, reviewed_at: str, verdict: str,
                         development_seal: dict[str, Any], heldout_seal: dict[str, Any],
                         findings: Any = ()) -> dict[str, Any]:
    """What an independent reviewer concluded about both splits' families, bound to the seals."""
    split = _split(development_seal, heldout_seal)
    if verdict not in REVIEW_VERDICTS:
        raise FreezeError("the review verdict is ACCEPT or REVISE")
    if not isinstance(reviewer, str) or not _REVIEWER.fullmatch(reviewer):
        raise FreezeError("the review names its reviewer")
    findings = list(findings or ())
    if len(findings) > 50 or any(not isinstance(item, str) or not item.strip() or len(item) > 500
                                 for item in findings):
        raise FreezeError("review findings are at most 50 lines of at most 500 characters")
    return _sealed({
        "artifact_version": FREEZE_VERSION, "kind": "p74_family_review",
        "protocol_id": protocol.PROTOCOL_ID, "reviewer": reviewer,
        "reviewed_at": _utc(reviewed_at, "reviewed_at"), "verdict": verdict,
        "checks": list(REVIEW_CHECKS),
        "development_seal_digest": split["development_seal"],
        "heldout_seal_digest": split["heldout_seal"], "findings": findings,
    }, "review_digest")


def verify_family_review(review: Any, *, development_seal: dict[str, Any],
                         heldout_seal: dict[str, Any]) -> dict[str, Any]:
    body = _body(review, "p74_family_review", "review_digest")
    expected = family_review_record(
        reviewer=body.get("reviewer"), reviewed_at=body.get("reviewed_at"),
        verdict=body.get("verdict"), development_seal=development_seal,
        heldout_seal=heldout_seal, findings=body.get("findings"))
    if review != expected:
        raise FreezeError("the family review does not describe these seals")
    return review


# -- first freeze -----------------------------------------------------------------------------------
def first_freeze_record(*, development_seal: dict[str, Any], heldout_seal: dict[str, Any],
                        split_verification: dict[str, Any],
                        family_review: dict[str, Any]) -> dict[str, Any]:
    split = _split(development_seal, heldout_seal)
    if split_verification != split:
        raise FreezeError("the split verification does not match these seals")
    review = verify_family_review(family_review, development_seal=development_seal,
                                  heldout_seal=heldout_seal)
    if review["verdict"] != "ACCEPT":
        raise FreezeError("the first freeze needs an accepted independent family review")
    return _sealed({
        "artifact_version": FREEZE_VERSION, "kind": "p74_first_freeze",
        "protocol_id": protocol.PROTOCOL_ID,
        "development_seal_digest": development_seal["seal_digest"],
        "heldout_seal_digest": heldout_seal["seal_digest"],
        "split_verification_digest": digest_json(split),
        "development_population_commitment": development_seal["population_seed_commitment"],
        "heldout_population_commitment": heldout_seal["population_seed_commitment"],
        "development_fixture_truth_digest": development_seal["fixture_truth_digest"],
        "heldout_fixture_truth_digest": heldout_seal["fixture_truth_digest"],
        "normal_tasks_per_split": split["normal_tasks_per_split"],
        "refusal_or_limitation_tasks_per_split": split["refusal_or_limitation_tasks_per_split"],
        "family_review_digest": review["review_digest"],
        "family_reviewer": review["reviewer"], "family_reviewed_at": review["reviewed_at"],
    }, "first_freeze_digest")


def verify_first_freeze(record: Any, *, development_seal: dict[str, Any],
                        heldout_seal: dict[str, Any], split_verification: dict[str, Any],
                        family_review: dict[str, Any]) -> dict[str, Any]:
    _body(record, "p74_first_freeze", "first_freeze_digest")
    expected = first_freeze_record(development_seal=development_seal, heldout_seal=heldout_seal,
                                   split_verification=split_verification,
                                   family_review=family_review)
    if record != expected:
        raise FreezeError("the first freeze does not match its seals and review")
    return record


# -- the candidate and the second freeze ---------------------------------------------------------------
def candidate_record(value: Any, *, repository: Path = REPOSITORY) -> dict[str, Any]:
    """A candidate: an existing commit and the Agent image whose revision label names it."""
    if not isinstance(value, dict) or set(value) != {"commit", "agent_image_id",
                                                     "agent_image_revision"}:
        raise FreezeError("a candidate names its commit, Agent image id and image revision")
    try:
        commit = require_commit(value["commit"], "candidate commit")
    except RevisionError as exc:
        raise FreezeError(str(exc)) from exc
    if not commit_exists(repository, commit):
        raise FreezeError("the candidate commit is not in this repository")
    if value["agent_image_revision"] != commit:
        raise FreezeError("the candidate image is labelled with another commit")
    if not isinstance(value["agent_image_id"], str) or not _DIGEST.fullmatch(
            value["agent_image_id"]):
        raise FreezeError("the candidate image id is not a content digest")
    return dict(value)


def files_at(commit: str, paths: Any, *, repository: Path = REPOSITORY) -> dict[str, str]:
    """SHA-256 of each path as committed in ``commit``."""
    try:
        return {path: "sha256:" + hashlib.sha256(file_at(repository, commit, path)).hexdigest()
                for path in paths}
    except RevisionError as exc:
        raise FreezeError(str(exc)) from exc


def implementation_digests_at(commit: str, *,
                              repository: Path = REPOSITORY) -> dict[str, dict[str, str]]:
    return {role: files_at(commit, paths, repository=repository)
            for role, paths in protocol.APPARATUS_MODULES.items()}


def _second_freeze_body(first_freeze_digest: str, candidate: dict[str, Any],
                        development_chain_head: str | None, repository: Path) -> dict[str, Any]:
    commit = candidate["commit"]
    return {
        "artifact_version": FREEZE_VERSION, "kind": "p74_second_freeze",
        "protocol_id": protocol.PROTOCOL_ID, "first_freeze_digest": first_freeze_digest,
        "candidate": candidate,
        "implementation_digests": implementation_digests_at(commit, repository=repository),
        "machine_protocol_digests": files_at(commit, MACHINE_PROTOCOL_FILES,
                                             repository=repository),
        "capability_digests": {arm: protocol.arm_capability_digest(arm) for arm in protocol.ARMS},
        "provider_config_digest": protocol.provider_config_digest(),
        "statistics_plan_digest": digest_json(protocol.STATISTICS),
        "development_chain_head": development_chain_head,
    }


def second_freeze_record(*, first_freeze: dict[str, Any], candidate: dict[str, Any],
                         development_chain_head: str | None = None,
                         repository: Path = REPOSITORY) -> dict[str, Any]:
    """Freeze the candidate, taken from its own clean checkout after development qualification."""
    _body(first_freeze, "p74_first_freeze", "first_freeze_digest")
    record = candidate_record(candidate, repository=repository)
    try:
        require_clean_head(repository, commit=record["commit"])
    except RevisionError as exc:
        raise FreezeError(f"the second freeze is taken from the clean candidate checkout: {exc}") \
            from exc
    if development_chain_head is not None and (
            not isinstance(development_chain_head, str)
            or not _DIGEST.fullmatch(development_chain_head)):
        raise FreezeError("the development chain head must be a SHA-256 digest")
    return _sealed(_second_freeze_body(first_freeze["first_freeze_digest"], record,
                                       development_chain_head, repository),
                   "second_freeze_digest")


def verify_second_freeze(record: Any, *, first_freeze: dict[str, Any],
                         repository: Path = REPOSITORY) -> dict[str, Any]:
    """Recompute a second freeze from its commit and the current machine protocol."""
    body = _body(record, "p74_second_freeze", "second_freeze_digest")
    _body(first_freeze, "p74_first_freeze", "first_freeze_digest")
    if body.get("first_freeze_digest") != first_freeze["first_freeze_digest"]:
        raise FreezeError("the second freeze names another first freeze")
    candidate = candidate_record(body.get("candidate"), repository=repository)
    expected = _second_freeze_body(first_freeze["first_freeze_digest"], candidate,
                                   body.get("development_chain_head"), repository)
    if body != expected:
        raise FreezeError("the second freeze does not match its candidate commit and the "
                          "current machine protocol")
    return record


# -- third freeze ---------------------------------------------------------------------------------------
def _third_freeze_body(first_freeze: dict[str, Any], second_freeze: dict[str, Any],
                       second_freeze_commit: Any, heldout_task_ids: Any,
                       pageagent_eligible: Any) -> dict[str, Any]:
    try:
        commit = require_commit(second_freeze_commit, "second_freeze_commit")
    except RevisionError as exc:
        raise FreezeError(str(exc)) from exc
    ids = sorted(heldout_task_ids or ())
    eligible = sorted(pageagent_eligible or ())
    if len(ids) != protocol.FORMAL_TASK_COUNT or len(set(ids)) != len(ids):
        raise FreezeError(f"the held-out schedule covers exactly {protocol.FORMAL_TASK_COUNT} "
                          "distinct tasks")
    if not set(eligible) <= set(ids) or len(set(eligible)) != len(eligible):
        raise FreezeError("PageAgent eligibility names unknown or repeated held-out tasks")
    seed = protocol.derive_schedule_seed(commit, purpose="heldout")
    slots = protocol.formal_schedule("heldout", ids, seed=seed, pageagent_eligible=eligible)
    return {
        "artifact_version": FREEZE_VERSION, "kind": "p74_third_freeze",
        "protocol_id": protocol.PROTOCOL_ID,
        "second_freeze_digest": second_freeze["second_freeze_digest"],
        "second_freeze_commit": commit, "schedule_seed": seed,
        "pageagent_eligible": eligible, "slots": slots, "schedule_digest": digest_json(slots),
        "protocol_seal": protocol.document(first_freeze=first_freeze,
                                           second_freeze=second_freeze)["protocol_digest"],
    }


def third_freeze_record(*, first_freeze: dict[str, Any], second_freeze: dict[str, Any],
                        second_freeze_commit: str, heldout_task_ids: Any,
                        pageagent_eligible: Any) -> dict[str, Any]:
    verify_chain(first_freeze, second_freeze)
    return _sealed(_third_freeze_body(first_freeze, second_freeze, second_freeze_commit,
                                      heldout_task_ids, pageagent_eligible),
                   "third_freeze_digest")


def verify_third_freeze(record: Any, *, first_freeze: dict[str, Any],
                        second_freeze: dict[str, Any], heldout_task_ids: Any,
                        pageagent_eligible: Any) -> dict[str, Any]:
    body = _body(record, "p74_third_freeze", "third_freeze_digest")
    verify_chain(first_freeze, second_freeze)
    expected = _third_freeze_body(first_freeze, second_freeze, body.get("second_freeze_commit"),
                                  heldout_task_ids, pageagent_eligible)
    if body != expected:
        raise FreezeError("the third freeze's schedule or protocol seal was chosen, not derived")
    return record


# -- the chain -------------------------------------------------------------------------------------------
def verify_chain(first_freeze: Any = None, second_freeze: Any = None,
                 third_freeze: Any = None) -> str:
    """The stage a supplied freeze chain reaches, after checking kinds, digests and links."""
    if second_freeze is not None and first_freeze is None:
        raise FreezeError("a second freeze needs the first")
    if third_freeze is not None and second_freeze is None:
        raise FreezeError("a third freeze needs the second")
    if first_freeze is None:
        return "draft"
    _body(first_freeze, "p74_first_freeze", "first_freeze_digest")
    if second_freeze is None:
        return "first_frozen"
    second = _body(second_freeze, "p74_second_freeze", "second_freeze_digest")
    if second.get("first_freeze_digest") != first_freeze["first_freeze_digest"]:
        raise FreezeError("the second freeze names another first freeze")
    if third_freeze is None:
        return "second_frozen"
    third = _body(third_freeze, "p74_third_freeze", "third_freeze_digest")
    if third.get("second_freeze_digest") != second_freeze["second_freeze_digest"]:
        raise FreezeError("the third freeze names another second freeze")
    try:
        seed = protocol.derive_schedule_seed(third.get("second_freeze_commit"), purpose="heldout")
    except ValueError as exc:
        raise FreezeError(str(exc)) from exc
    if (third.get("schedule_seed") != seed
            or third.get("schedule_digest") != digest_json(third.get("slots"))
            or third.get("protocol_seal") != protocol.document(
                first_freeze=first_freeze, second_freeze=second_freeze)["protocol_digest"]):
        raise FreezeError("the third freeze's seed, schedule or protocol seal is inconsistent")
    return "third_frozen"
