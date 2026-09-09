"""Pure, deterministic Twenty business-state evaluation.

This module consumes files produced by an evaluator process.  It does not import the
product package, call an Agent, or connect to Twenty.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


ARTIFACT_VERSION = 1
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.I,
)
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.I,
)
_COMPANY_FIELDS = ("id", "name", "domain", "created_at", "updated_at", "deleted_at")
_GUARD_NAMES = ("people", "opportunities")


class EvaluationInputError(ValueError):
    """An artifact cannot support a deterministic decision."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvaluationInputError("artifact is not canonical JSON") from exc


def digest_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise EvaluationInputError(f"{field} must be a non-empty string")
    return value


def validate_fixture(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise EvaluationInputError("fixture identity must be an object")
    required = {"container", "database", "db_user", "workspace_schema"}
    if set(value) != required:
        raise EvaluationInputError("fixture identity has unexpected fields")
    fixture = {key: _text(value.get(key), f"fixture.{key}") for key in required}
    if not _CONTAINER.fullmatch(fixture["container"]):
        raise EvaluationInputError("fixture.container is invalid")
    for key in ("database", "db_user", "workspace_schema"):
        if not _IDENTIFIER.fullmatch(fixture[key]):
            raise EvaluationInputError(f"fixture.{key} is invalid")
    if not fixture["workspace_schema"].startswith("workspace_"):
        raise EvaluationInputError("fixture.workspace_schema must name a Twenty workspace schema")
    return {key: fixture[key] for key in sorted(fixture)}


def _normalize_domain(value: Any, *, allow_empty: bool = False,
                      candidate_value: bool = False) -> str:
    if allow_empty and (value is None or value == ""):
        return ""
    domain = _text(value, "company.domain").strip().casefold()
    domain = re.sub(r"^https?://", "", domain).rstrip("/")
    if not _DOMAIN.fullmatch(domain):
        if candidate_value:
            return domain
        raise EvaluationInputError("company.domain must be a bare domain")
    return domain


def _normalize_company(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(_COMPANY_FIELDS):
        raise EvaluationInputError("company projection has unexpected fields")
    company_id = _text(value.get("id"), "company.id")
    if not _UUID.fullmatch(company_id):
        raise EvaluationInputError("company.id must be a UUID")
    deleted_at = value.get("deleted_at")
    if deleted_at is not None and not isinstance(deleted_at, str):
        raise EvaluationInputError("company.deleted_at must be a string or null")
    deleted = deleted_at is not None
    raw_name = value.get("name")
    company: dict[str, Any] = {
        "id": company_id,
        "name": ("" if deleted and raw_name is None
                 else _text(raw_name, "company.name", allow_empty=deleted)),
        "domain": _normalize_domain(value.get("domain"), allow_empty=True),
    }
    for key in ("created_at", "updated_at"):
        company[key] = _text(value.get(key), f"company.{key}")
    company["deleted_at"] = deleted_at
    return company


def _normalize_guard(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"row_count", "digest"}:
        raise EvaluationInputError(f"{name} guard has unexpected fields")
    count = value.get("row_count")
    digest = value.get("digest")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise EvaluationInputError(f"{name} guard row_count is invalid")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise EvaluationInputError(f"{name} guard digest is invalid")
    return {"row_count": count, "digest": digest}


def normalize_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"companies", "guards"}:
        raise EvaluationInputError("business projection has unexpected fields")
    rows = value.get("companies")
    guards = value.get("guards")
    if not isinstance(rows, list) or not isinstance(guards, dict) \
            or set(guards) != set(_GUARD_NAMES):
        raise EvaluationInputError("business projection is incomplete")
    companies = []
    for index, row in enumerate(rows):
        try:
            companies.append(_normalize_company(row))
        except EvaluationInputError as exc:
            row_id = row.get("id") if isinstance(row, dict) else None
            identity = f" id={row_id}" if isinstance(row_id, str) else ""
            raise EvaluationInputError(
                f"companies[{index}]{identity}: {exc}") from exc
    companies.sort(key=lambda row: row["id"])
    ids = [row["id"] for row in companies]
    if len(ids) != len(set(ids)):
        raise EvaluationInputError("company ids must be unique")
    return {
        "companies": companies,
        "guards": {name: _normalize_guard(guards[name], name) for name in _GUARD_NAMES},
    }


def make_state(*, fixture: dict[str, str], companies: list[dict[str, Any]],
               guards: dict[str, dict[str, Any]], captured_at: str,
               role: str = "observation") -> dict[str, Any]:
    if role not in {"baseline", "observation"}:
        raise EvaluationInputError("state role is invalid")
    projection = normalize_projection({"companies": companies, "guards": guards})
    return {
        "artifact_version": ARTIFACT_VERSION,
        "kind": "twenty_state",
        "role": role,
        "status": "READY",
        "fixture": validate_fixture(fixture),
        "captured_at": _text(captured_at, "captured_at"),
        "projection": projection,
        "digest": digest_json(projection),
    }


def make_unavailable_state(*, fixture: dict[str, str], captured_at: str,
                           role: str, reason: str = "fixture_unavailable") -> dict[str, Any]:
    if role not in {"baseline", "observation"}:
        raise EvaluationInputError("state role is invalid")
    return {
        "artifact_version": ARTIFACT_VERSION,
        "kind": "twenty_state",
        "role": role,
        "status": "INDETERMINATE",
        "fixture": validate_fixture(fixture),
        "captured_at": _text(captured_at, "captured_at"),
        "reason": _text(reason, "reason"),
    }


def validate_state(value: Any, *, expected_role: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("artifact_version") != ARTIFACT_VERSION \
            or value.get("kind") != "twenty_state" or value.get("role") != expected_role:
        raise EvaluationInputError(f"{expected_role} state header is invalid")
    fixture = validate_fixture(value.get("fixture"))
    status = value.get("status")
    if status == "INDETERMINATE":
        if set(value) != {"artifact_version", "kind", "role", "status", "fixture",
                          "captured_at", "reason"}:
            raise EvaluationInputError(f"{expected_role} unavailable state has unexpected fields")
        _text(value.get("captured_at"), f"{expected_role}.captured_at")
        _text(value.get("reason"), f"{expected_role}.reason")
        return {"status": status, "fixture": fixture}
    if status != "READY":
        raise EvaluationInputError(f"{expected_role} state status is invalid")
    if set(value) != {"artifact_version", "kind", "role", "status", "fixture",
                      "captured_at", "projection", "digest"}:
        raise EvaluationInputError(f"{expected_role} state has unexpected fields")
    projection = normalize_projection(value.get("projection"))
    digest = value.get("digest")
    if not isinstance(digest, str) or digest != digest_json(projection):
        raise EvaluationInputError(f"{expected_role} state digest does not match its projection")
    _text(value.get("captured_at"), f"{expected_role}.captured_at")
    return {"status": status, "fixture": fixture, "projection": projection,
            "digest": digest}


def _active_companies(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in state["projection"]["companies"] if row["deleted_at"] is None]


def _reported_companies(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise EvaluationInputError("query evidence companies must be a list")
    result = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"name", "domain"}:
            raise EvaluationInputError("query evidence company has unexpected fields")
        result.append({"name": _text(row.get("name"), "evidence.company.name"),
                       "domain": _normalize_domain(
                           row.get("domain"), allow_empty=True, candidate_value=True)})
    return sorted(result, key=lambda row: (row["name"].casefold(), row["domain"]))


def _result(status: str, kind: str, *, checks: dict[str, bool] | None = None,
            reason: str = "", before_digest: str = "", after_digest: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "kind": "twenty_evaluation",
        "status": status,
        "task_kind": kind,
        "checks": checks or {},
    }
    if reason:
        result["reason"] = reason
    if before_digest:
        result["before_digest"] = before_digest
    if after_digest:
        result["after_digest"] = after_digest
    return result


def _task(value: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, dict) or value.get("artifact_version") != ARTIFACT_VERSION:
        raise EvaluationInputError("task header is invalid")
    kind = value.get("kind")
    if kind == "query":
        if set(value) != {"artifact_version", "kind"}:
            raise EvaluationInputError("query task has unexpected fields")
        return kind, {}
    if kind == "open":
        if set(value) != {"artifact_version", "kind", "company_id"}:
            raise EvaluationInputError("open task has unexpected fields")
        company_id = _text(value.get("company_id"), "task.company_id")
        if not _UUID.fullmatch(company_id):
            raise EvaluationInputError("task.company_id must be a UUID")
        return kind, {"company_id": company_id}
    if kind == "create":
        if set(value) != {"artifact_version", "kind", "name", "domain", "require_open"}:
            raise EvaluationInputError("create task has unexpected fields")
        require_open = value.get("require_open")
        if not isinstance(require_open, bool):
            raise EvaluationInputError("task.require_open must be a boolean")
        return kind, {
            "name": _text(value.get("name"), "task.name"),
            "domain": _normalize_domain(value.get("domain")),
            "require_open": require_open,
        }
    raise EvaluationInputError("task kind is unsupported")


def _execution_status(evidence: Any) -> str:
    if not isinstance(evidence, dict) or evidence.get("artifact_version") != ARTIFACT_VERSION:
        raise EvaluationInputError("evidence header is invalid")
    status = evidence.get("execution_status")
    if status not in {"completed", "failed", "indeterminate"}:
        raise EvaluationInputError("evidence execution_status is invalid")
    return status


def _query_checks(before: dict[str, Any], after: dict[str, Any], evidence: dict[str, Any],
                  skip_completion_evidence: bool) -> dict[str, bool]:
    checks = {"business_state_unchanged": before["digest"] == after["digest"]}
    if skip_completion_evidence:
        return checks
    if set(evidence) != {"artifact_version", "execution_status", "complete", "companies"} \
            or not isinstance(evidence.get("complete"), bool):
        raise EvaluationInputError("completed query evidence is incomplete")
    expected = sorted(
        ({"name": row["name"], "domain": row["domain"]}
         for row in _active_companies(after)),
        key=lambda row: (row["name"].casefold(), row["domain"]),
    )
    checks["response_marked_complete"] = evidence["complete"] is True
    checks["reported_all_current_companies"] = (
        _reported_companies(evidence["companies"]) == expected)
    return checks


def _open_checks(before: dict[str, Any], after: dict[str, Any], task: dict[str, Any],
                 evidence: dict[str, Any], skip_completion_evidence: bool) -> dict[str, bool]:
    targets = [row for row in _active_companies(before) if row["id"] == task["company_id"]]
    if len(targets) != 1:
        raise EvaluationInputError("open task target is absent or ambiguous in the baseline")
    target = targets[0]
    checks = {"business_state_unchanged": before["digest"] == after["digest"]}
    if skip_completion_evidence:
        return checks
    if set(evidence) != {"artifact_version", "execution_status", "page"}:
        raise EvaluationInputError("completed open evidence has unexpected fields")
    page = evidence.get("page")
    if not isinstance(page, dict) or set(page) not in (
            {"path", "name", "domain"}, {"path", "visible_projection"}):
        raise EvaluationInputError("completed open page evidence is invalid")
    if set(page) == {"path", "visible_projection"}:
        visible = _text(page.get("visible_projection"), "evidence.page.visible_projection")
        if len(visible.encode("utf-8")) > 512 * 1024:
            raise EvaluationInputError("completed open visible projection is oversized")
        folded = visible.casefold()
        visible_match = (
            target["name"].casefold() in folded
            and (not target["domain"] or target["domain"] in folded))
    else:
        visible_match = (
            page.get("name") == target["name"]
            and _normalize_domain(page.get("domain"), allow_empty=True,
                                  candidate_value=True) == target["domain"])
    checks.update({
        "final_route_matches_target": page.get("path") == f"/object/company/{target['id']}",
        "visible_company_matches_target": visible_match,
    })
    return checks


def _create_checks(before: dict[str, Any], after: dict[str, Any], task: dict[str, Any],
                   evidence: dict[str, Any], skip_completion_evidence: bool) -> dict[str, bool]:
    before_rows = {row["id"]: row for row in before["projection"]["companies"]}
    after_rows = {row["id"]: row for row in after["projection"]["companies"]}
    new_rows = [row for row_id, row in after_rows.items() if row_id not in before_rows]
    created = new_rows[0] if len(new_rows) == 1 else None
    checks = {
        "exactly_one_company_added": len(new_rows) == 1 and len(after_rows) == len(before_rows) + 1,
        "existing_companies_unchanged": all(after_rows.get(row_id) == row
                                            for row_id, row in before_rows.items()),
        "people_unchanged": (before["projection"]["guards"]["people"]
                             == after["projection"]["guards"]["people"]),
        "opportunities_unchanged": (before["projection"]["guards"]["opportunities"]
                                    == after["projection"]["guards"]["opportunities"]),
        "created_company_matches_request": bool(
            created and created["deleted_at"] is None and created["name"] == task["name"]
            and created["domain"] == task["domain"]),
    }
    if skip_completion_evidence:
        return checks
    expected_keys = {"artifact_version", "execution_status", "write_receipt"}
    if task["require_open"]:
        expected_keys.add("page")
    if set(evidence) != expected_keys:
        raise EvaluationInputError("completed create evidence has unexpected fields")
    receipt = evidence.get("write_receipt")
    if not isinstance(receipt, dict) or set(receipt) != {
            "dispatch_count", "acknowledged", "same_actor_readback"}:
        raise EvaluationInputError("completed create write receipt is invalid")
    checks["write_completed_once"] = (
        type(receipt.get("dispatch_count")) is int
        and receipt.get("dispatch_count") == 1 and receipt.get("acknowledged") is True
        and receipt.get("same_actor_readback") is True
    )
    if task["require_open"]:
        page = evidence.get("page")
        if not isinstance(page, dict) or set(page) != {"path", "name", "domain"}:
            raise EvaluationInputError("completed create page evidence is invalid")
        checks["created_company_opened"] = bool(
            created and page.get("path") == f"/object/company/{created['id']}"
            and page.get("name") == created["name"]
            and _normalize_domain(page.get("domain"), allow_empty=True,
                                  candidate_value=True) == created["domain"])
    return checks


def evaluate(*, task: Any, before: Any, after: Any, evidence: Any) -> dict[str, Any]:
    """Return PASS, FAIL, INDETERMINATE, or INVALID without external I/O."""
    task_kind = str(task.get("kind") or "unknown") if isinstance(task, dict) else "unknown"
    try:
        task_kind, normalized_task = _task(task)
        baseline = validate_state(before, expected_role="baseline")
        if baseline["status"] != "READY":
            raise EvaluationInputError("baseline is unavailable")
        observation = validate_state(after, expected_role="observation")
        if baseline["fixture"] != observation["fixture"]:
            raise EvaluationInputError("baseline and observation identify different fixtures")
        if observation["status"] == "INDETERMINATE":
            return _result("INDETERMINATE", task_kind,
                           checks={"after_state_available": False},
                           reason="after_state_unavailable",
                           before_digest=baseline["digest"])
        execution_status = _execution_status(evidence)
        uncertain = execution_status == "indeterminate"
        incomplete_execution = execution_status in {"failed", "indeterminate"}
        if incomplete_execution and set(evidence) != {"artifact_version", "execution_status"}:
            raise EvaluationInputError("non-completed evidence has unexpected fields")
        if task_kind == "query":
            checks = _query_checks(baseline, observation, evidence, incomplete_execution)
        elif task_kind == "open":
            checks = _open_checks(
                baseline, observation, normalized_task, evidence, incomplete_execution)
        else:
            checks = _create_checks(
                baseline, observation, normalized_task, evidence, incomplete_execution)
        if uncertain:
            checks["execution_certain"] = False
            status, reason = "INDETERMINATE", "execution_outcome_uncertain"
        elif execution_status == "failed":
            checks["execution_completed"] = False
            status, reason = "FAIL", "execution_failed"
        else:
            status, reason = (
                ("PASS", "") if all(checks.values()) else ("FAIL", "postcondition_failed"))
        return _result(status, task_kind, checks=checks, reason=reason,
                       before_digest=baseline["digest"], after_digest=observation["digest"])
    except EvaluationInputError as exc:
        return _result("INVALID", task_kind, reason=str(exc))
