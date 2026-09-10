"""Deterministic read-only evaluation for the Twenty breadth development set.

This version is separate from the frozen Companies v1 evaluator. It accepts only bounded,
typed projections and never imports product code or contacts Twenty.
"""
from __future__ import annotations

import re
from typing import Any

from .evaluator import (EvaluationInputError, digest_json, normalize_projection,
                        validate_fixture)


ARTIFACT_VERSION = 2
ENTITIES = ("companies", "people", "opportunities")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.I)
_ROUTES = {
    "companies": ("company", "/objects/companies"),
    "people": ("person", "/objects/people"),
    "opportunities": ("opportunity", "/objects/opportunities"),
}


def _text(value: Any, field: str, *, empty: bool = False, maximum: int = 500) -> str:
    if (not isinstance(value, str) or len(value) > maximum
            or (not empty and not value.strip())):
        raise EvaluationInputError(f"{field} must be a bounded string")
    return value.strip()


def _optional_uuid(value: Any, field: str) -> str:
    if value in (None, ""):
        return ""
    selected = _text(value, field, maximum=36)
    if not _UUID.fullmatch(selected):
        raise EvaluationInputError(f"{field} must be a UUID or empty")
    return selected


def _person(value: Any) -> dict[str, Any]:
    fields = {"id", "name", "email", "company_id", "created_at", "updated_at", "deleted_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise EvaluationInputError("person projection has unexpected fields")
    person_id = _optional_uuid(value.get("id"), "person.id")
    if not person_id:
        raise EvaluationInputError("person.id must be a UUID")
    deleted_at = value.get("deleted_at")
    if deleted_at is not None:
        deleted_at = _text(deleted_at, "person.deleted_at")
    return {
        "id": person_id,
        "name": _text(value.get("name"), "person.name", empty=deleted_at is not None),
        "email": _text(value.get("email"), "person.email", empty=True, maximum=320),
        "company_id": _optional_uuid(value.get("company_id"), "person.company_id"),
        "created_at": _text(value.get("created_at"), "person.created_at"),
        "updated_at": _text(value.get("updated_at"), "person.updated_at"),
        "deleted_at": deleted_at,
    }


def _opportunity(value: Any) -> dict[str, Any]:
    fields = {"id", "name", "stage", "amount_micros", "currency", "company_id",
              "created_at", "updated_at", "deleted_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise EvaluationInputError("opportunity projection has unexpected fields")
    opportunity_id = _optional_uuid(value.get("id"), "opportunity.id")
    if not opportunity_id:
        raise EvaluationInputError("opportunity.id must be a UUID")
    deleted_at = value.get("deleted_at")
    if deleted_at is not None:
        deleted_at = _text(deleted_at, "opportunity.deleted_at")
    amount = _text(value.get("amount_micros"), "opportunity.amount_micros",
                   empty=True, maximum=40)
    if amount and not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", amount):
        raise EvaluationInputError("opportunity.amount_micros is invalid")
    return {
        "id": opportunity_id,
        "name": _text(value.get("name"), "opportunity.name", empty=deleted_at is not None),
        "stage": _text(value.get("stage"), "opportunity.stage", empty=True, maximum=100),
        "amount_micros": amount,
        "currency": _text(value.get("currency"), "opportunity.currency",
                          empty=True, maximum=10),
        "company_id": _optional_uuid(value.get("company_id"), "opportunity.company_id"),
        "created_at": _text(value.get("created_at"), "opportunity.created_at"),
        "updated_at": _text(value.get("updated_at"), "opportunity.updated_at"),
        "deleted_at": deleted_at,
    }


def normalize_breadth_projection(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict) or set(value) != set(ENTITIES):
        raise EvaluationInputError("breadth projection is incomplete")
    companies = normalize_projection({
        "companies": value.get("companies"),
        "guards": {"people": {"row_count": 0, "digest": digest_json([])},
                   "opportunities": {"row_count": 0, "digest": digest_json([])}},
    })["companies"]
    raw_people, raw_opportunities = value.get("people"), value.get("opportunities")
    if not isinstance(raw_people, list) or not isinstance(raw_opportunities, list):
        raise EvaluationInputError("breadth collections must be arrays")
    result = {
        "companies": companies,
        "people": [_person(row) for row in raw_people],
        "opportunities": [_opportunity(row) for row in raw_opportunities],
    }
    for name, rows in result.items():
        rows.sort(key=lambda row: row["id"])
        ids = [row["id"] for row in rows]
        if len(ids) != len(set(ids)):
            raise EvaluationInputError(f"{name} ids must be unique")
    return result


def make_breadth_state(*, fixture: dict[str, str], projection: dict[str, Any],
                       captured_at: str, role: str) -> dict[str, Any]:
    if role not in {"baseline", "observation"}:
        raise EvaluationInputError("breadth state role is invalid")
    normalized = normalize_breadth_projection(projection)
    return {"artifact_version": ARTIFACT_VERSION, "kind": "twenty_breadth_state",
            "status": "READY", "role": role, "fixture": validate_fixture(fixture),
            "captured_at": _text(captured_at, "captured_at"),
            "projection": normalized, "digest": digest_json(normalized)}


def validate_breadth_state(value: Any, *, role: str) -> dict[str, Any]:
    fields = {"artifact_version", "kind", "status", "role", "fixture", "captured_at",
              "projection", "digest"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("artifact_version") != ARTIFACT_VERSION
            or value.get("kind") != "twenty_breadth_state"
            or value.get("status") != "READY" or value.get("role") != role):
        raise EvaluationInputError("breadth state header is invalid")
    projection = normalize_breadth_projection(value.get("projection"))
    if value.get("digest") != digest_json(projection):
        raise EvaluationInputError("breadth state digest does not match")
    return {**value, "fixture": validate_fixture(value.get("fixture")),
            "captured_at": _text(value.get("captured_at"), "captured_at"),
            "projection": projection}


def _active(state: dict[str, Any], entity: str) -> list[dict[str, Any]]:
    return [row for row in state["projection"][entity] if row["deleted_at"] is None]


def _public_record(entity: str, row: dict[str, Any]) -> dict[str, str]:
    if entity == "companies":
        return {"name": row["name"], "detail": row["domain"]}
    if entity == "people":
        return {"name": row["name"], "detail": row["email"]}
    return {"name": row["name"], "detail": row["stage"]}


def validate_breadth_task(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("artifact_version") != ARTIFACT_VERSION:
        raise EvaluationInputError("breadth task header is invalid")
    kind, entity = value.get("kind"), value.get("entity")
    if kind not in {"enumerate", "filter", "open_record", "open_collection", "refuse"}:
        raise EvaluationInputError("breadth task kind is unsupported")
    if entity not in ENTITIES:
        raise EvaluationInputError("breadth task entity is unsupported")
    expected = {"artifact_version", "kind", "entity"}
    if kind == "filter":
        expected.add("name_contains")
    elif kind == "open_record":
        expected.add("record_id")
    elif kind == "refuse":
        expected.add("operation")
    if set(value) != expected:
        raise EvaluationInputError("breadth task has unexpected fields")
    result = {"kind": kind, "entity": entity}
    if kind == "filter":
        result["name_contains"] = _text(value.get("name_contains"), "task.name_contains",
                                         maximum=100)
    elif kind == "open_record":
        result["record_id"] = _optional_uuid(value.get("record_id"), "task.record_id")
        if not result["record_id"]:
            raise EvaluationInputError("task.record_id must be a UUID")
    elif kind == "refuse":
        result["operation"] = _text(value.get("operation"), "task.operation", maximum=80)
    return result


def evaluate_breadth(*, task: Any, before: Any, after: Any, evidence: Any) -> dict[str, Any]:
    """Evaluate one read-only breadth case without network, product, or model access."""
    try:
        selected = validate_breadth_task(task)
        baseline = validate_breadth_state(before, role="baseline")
        observation = validate_breadth_state(after, role="observation")
        if baseline["fixture"] != observation["fixture"]:
            raise EvaluationInputError("breadth states identify different fixtures")
        if not isinstance(evidence, dict) or evidence.get("artifact_version") != ARTIFACT_VERSION:
            raise EvaluationInputError("breadth evidence header is invalid")
        state_unchanged = baseline["digest"] == observation["digest"]
        kind, entity = selected["kind"], selected["entity"]
        checks = {"business_state_unchanged": state_unchanged}
        active = _active(observation, entity)
        if kind in {"enumerate", "filter"}:
            if set(evidence) != {"artifact_version", "execution_status", "complete", "records"}:
                raise EvaluationInputError("breadth query evidence is incomplete")
            expected = active
            if kind == "filter":
                needle = selected["name_contains"].casefold()
                expected = [row for row in expected if needle in row["name"].casefold()]
            expected_records = sorted((_public_record(entity, row) for row in expected),
                                      key=lambda row: (row["name"].casefold(), row["detail"]))
            records = evidence.get("records")
            if (not isinstance(evidence.get("complete"), bool)
                    or not isinstance(records, list) or any(
                    not isinstance(row, dict) or set(row) != {"name", "detail"}
                    or not isinstance(row["name"], str) or not isinstance(row["detail"], str)
                    or len(row["name"]) > 500 or len(row["detail"]) > 500
                    for row in records)):
                raise EvaluationInputError("breadth query records are invalid")
            actual = sorted(records, key=lambda row: (row["name"].casefold(), row["detail"]))
            checks.update({"execution_completed": evidence.get("execution_status") == "completed",
                           "response_marked_complete": evidence.get("complete") is True,
                           "reported_exact_records": actual == expected_records})
        elif kind == "open_record":
            if set(evidence) != {"artifact_version", "execution_status", "page"}:
                raise EvaluationInputError("breadth open evidence is incomplete")
            matches = [row for row in _active(baseline, entity)
                       if row["id"] == selected["record_id"]]
            if len(matches) != 1:
                raise EvaluationInputError("breadth open target is absent")
            page = evidence.get("page")
            if not isinstance(page, dict) or set(page) != {"path", "visible_projection"}:
                raise EvaluationInputError("breadth open page evidence is invalid")
            visible = page.get("visible_projection")
            if (not isinstance(visible, str) or not visible.strip()
                    or len(visible.encode("utf-8")) > 512 * 1024):
                raise EvaluationInputError("breadth visible projection is invalid")
            route = _ROUTES[entity][0]
            target = _public_record(entity, matches[0])
            checks.update({"execution_completed": evidence.get("execution_status") == "completed",
                           "final_route_matches_target": page.get("path") ==
                               f"/object/{route}/{matches[0]['id']}",
                           "visible_record_matches_target": target["name"].casefold() in
                               visible.casefold() and (not target["detail"] or
                                                       target["detail"].casefold() in visible.casefold())})
        elif kind == "open_collection":
            if set(evidence) != {"artifact_version", "execution_status", "page"}:
                raise EvaluationInputError("breadth collection evidence is incomplete")
            page = evidence.get("page")
            if not isinstance(page, dict) or set(page) != {"path"}:
                raise EvaluationInputError("breadth collection page evidence is invalid")
            checks.update({"execution_completed": evidence.get("execution_status") == "completed",
                           "final_route_matches_collection": page.get("path") == _ROUTES[entity][1]})
        else:
            if set(evidence) != {"artifact_version", "execution_status", "refusal_code",
                                 "dispatch_count"}:
                raise EvaluationInputError("breadth refusal evidence is incomplete")
            checks.update({
                "request_refused": evidence.get("execution_status") == "failed",
                "refusal_is_explicit": evidence.get("refusal_code") == "unsupported_operation",
                "nothing_dispatched": type(evidence.get("dispatch_count")) is int
                    and evidence.get("dispatch_count") == 0,
            })
        status = "PASS" if all(checks.values()) else "FAIL"
        return {"artifact_version": ARTIFACT_VERSION, "kind": "twenty_breadth_evaluation",
                "status": status, "task_kind": kind, "entity": entity, "checks": checks,
                "before_digest": baseline["digest"], "after_digest": observation["digest"]}
    except EvaluationInputError as exc:
        return {"artifact_version": ARTIFACT_VERSION, "kind": "twenty_breadth_evaluation",
                "status": "INVALID", "reason": str(exc)}
