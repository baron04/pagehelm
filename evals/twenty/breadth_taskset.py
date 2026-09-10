"""Validation and baseline binding for the Twenty breadth task-set format."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .breadth import ARTIFACT_VERSION, ENTITIES, validate_breadth_state
from .evaluator import EvaluationInputError, digest_json


KINDS = ("enumerate", "filter", "open_record", "open_collection", "create_company", "refuse")
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{2,79}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_PLACEHOLDER = re.compile(r"\{\{([a-z.]+)\}\}")
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.I)


def _text(value: Any, name: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise EvaluationInputError(f"{name} must be a bounded string")
    return value.strip()


def _identifier(value: Any, name: str) -> str:
    selected = _text(value, name, 80)
    if not _ID.fullmatch(selected):
        raise EvaluationInputError(f"{name} is invalid")
    return selected


def _task(value: Any) -> dict[str, Any]:
    fields = {"task_id", "template_family", "kind", "entity", "prompt_template", "parameters"}
    if not isinstance(value, dict) or set(value) != fields:
        raise EvaluationInputError("breadth task-set task has unexpected fields")
    kind, entity = value.get("kind"), value.get("entity")
    if kind not in KINDS or entity not in ENTITIES:
        raise EvaluationInputError("breadth task kind or entity is unsupported")
    prompt = _text(value.get("prompt_template"), "task.prompt_template")
    parameters = value.get("parameters")
    if not isinstance(parameters, dict):
        raise EvaluationInputError("task.parameters must be an object")
    placeholders = frozenset(_PLACEHOLDER.findall(prompt))
    if "{{" in _PLACEHOLDER.sub("", prompt) or "}}" in _PLACEHOLDER.sub("", prompt):
        raise EvaluationInputError("task prompt contains an invalid placeholder")
    if kind in {"enumerate", "open_collection"}:
        if parameters or placeholders:
            raise EvaluationInputError(f"{kind} task cannot have parameters")
        normalized = {}
    elif kind in {"filter", "open_record"}:
        if set(parameters) != {"baseline_index"} or placeholders != {"record.name"}:
            raise EvaluationInputError(f"{kind} task must bind one baseline record name")
        index = parameters.get("baseline_index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 999:
            raise EvaluationInputError("task baseline_index is invalid")
        normalized = {"baseline_index": index}
    elif kind == "create_company":
        if entity != "companies" or set(parameters) != {"name", "domain", "require_open"} \
                or placeholders != {"company.name", "company.domain"}:
            raise EvaluationInputError("create_company task contract is invalid")
        domain = _text(parameters.get("domain"), "task.domain", 253).casefold()
        if not _DOMAIN.fullmatch(domain) or not isinstance(parameters.get("require_open"), bool):
            raise EvaluationInputError("create_company task parameters are invalid")
        normalized = {"name": _text(parameters.get("name"), "task.name", 100),
                      "domain": domain, "require_open": parameters["require_open"]}
    else:
        if set(parameters) != {"operation"} or placeholders:
            raise EvaluationInputError("refuse task contract is invalid")
        normalized = {"operation": _identifier(parameters.get("operation"), "task.operation")}
    return {"task_id": _identifier(value.get("task_id"), "task.task_id"),
            "template_family": _identifier(value.get("template_family"), "task.template_family"),
            "kind": kind, "entity": entity, "prompt_template": prompt,
            "parameters": normalized}


def validate_breadth_task_set(value: Any) -> dict[str, Any]:
    fields = {"artifact_version", "kind", "set_id", "split", "application_release",
              "commitment_nonce", "tasks"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("artifact_version") != ARTIFACT_VERSION
            or value.get("kind") != "twenty_breadth_task_set"
            or value.get("split") not in {"development", "test"}
            or value.get("application_release") != "twenty@2.39.0"
            or not _NONCE.fullmatch(str(value.get("commitment_nonce") or ""))):
        raise EvaluationInputError("breadth task-set header is invalid")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not 30 <= len(tasks) <= 1000:
        raise EvaluationInputError("breadth task set requires 30 to 1000 tasks")
    normalized = [_task(task) for task in tasks]
    ids = [task["task_id"] for task in normalized]
    if len(ids) != len(set(ids)):
        raise EvaluationInputError("breadth task ids must be unique")
    counts = Counter(task["kind"] for task in normalized)
    if any(counts[kind] < 4 for kind in KINDS):
        raise EvaluationInputError("breadth task set requires at least four tasks of every kind")
    for entity in ENTITIES:
        covered = {task["kind"] for task in normalized if task["entity"] == entity}
        if not {"enumerate", "filter", "open_record", "open_collection"} <= covered:
            raise EvaluationInputError(f"breadth task set does not cover {entity}")
    if value["split"] == "test" and any(not re.fullmatch(r"t[0-9]{3}", task_id)
                                          for task_id in ids):
        raise EvaluationInputError("breadth test task ids must be opaque")
    return {**value, "set_id": _identifier(value.get("set_id"), "task_set.set_id"),
            "tasks": sorted(normalized, key=lambda task: task["task_id"])}


def instantiate_breadth_task(value: Any, task_id: str, baseline: Any) -> dict[str, Any]:
    task_set = validate_breadth_task_set(value)
    task = next((task for task in task_set["tasks"] if task["task_id"] == task_id), None)
    if task is None:
        raise EvaluationInputError("breadth task id is absent")
    state = validate_breadth_state(baseline, role="baseline")
    prompt = task["prompt_template"]
    parameters = task["parameters"]
    kind, entity = task["kind"], task["entity"]
    if kind in {"filter", "open_record"}:
        records = sorted((row for row in state["projection"][entity]
                          if row["deleted_at"] is None),
                         key=lambda row: (row["name"].casefold(), row["id"]))
        index = parameters["baseline_index"]
        if index >= len(records):
            raise EvaluationInputError("breadth baseline selector is out of range")
        record = records[index]
        prompt = prompt.replace("{{record.name}}", record["name"])
        evaluator_task = {"artifact_version": 2, "kind": kind, "entity": entity}
        if kind == "filter":
            evaluator_task["name_contains"] = record["name"]
        else:
            evaluator_task["record_id"] = record["id"]
    elif kind == "create_company":
        prompt = prompt.replace("{{company.name}}", parameters["name"])
        prompt = prompt.replace("{{company.domain}}", parameters["domain"])
        evaluator_task = {"artifact_version": 1, "kind": "create",
                          "name": parameters["name"], "domain": parameters["domain"],
                          "require_open": parameters["require_open"]}
    elif kind == "refuse":
        evaluator_task = {"artifact_version": 2, "kind": "refuse", "entity": entity,
                          "operation": parameters["operation"]}
    else:
        evaluator_task = {"artifact_version": 2, "kind": kind, "entity": entity}
    if _PLACEHOLDER.search(prompt) or "{{" in prompt or "}}" in prompt:
        raise EvaluationInputError("instantiated breadth prompt contains a placeholder")
    return {"artifact_version": 2, "kind": "twenty_breadth_execution_case",
            "set_id": task_set["set_id"], "split": task_set["split"],
            "task_id": task["task_id"], "prompt": prompt,
            "task_digest": digest_json({"commitment_nonce": task_set["commitment_nonce"],
                                        "task": task}),
            "template_family_digest": digest_json(
                {"template_family": task["template_family"]}),
            "evaluator_task": evaluator_task}
