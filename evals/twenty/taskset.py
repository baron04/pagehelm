"""Frozen, leak-resistant task-set artifacts for the Twenty evaluation vertical."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .evaluator import EvaluationInputError, digest_json, validate_state


TASK_SET_VERSION = 1
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{2,79}\Z")
_RELEASE = re.compile(r"twenty@[0-9]+\.[0-9]+\.[0-9]+\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.I,
)
_PLACEHOLDER = re.compile(r"\{\{([a-z.]+)\}\}")
_ALLOWED_PLACEHOLDERS = {
    "open": frozenset({"company.name", "company.domain"}),
    "create": frozenset({"company.name", "company.domain"}),
    "query": frozenset(),
}


def _text(value: Any, name: str, *, maximum: int = 500) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(character) < 32 and character not in "\n\t" for character in value)):
        raise EvaluationInputError(f"{name} must be a bounded display string")
    return value.strip()


def _identifier(value: Any, name: str) -> str:
    selected = _text(value, name, maximum=80)
    if not _ID.fullmatch(selected):
        raise EvaluationInputError(f"{name} is invalid")
    return selected


def _domain(value: Any, name: str) -> str:
    selected = _text(value, name, maximum=253).casefold()
    if not _DOMAIN.fullmatch(selected):
        raise EvaluationInputError(f"{name} must be a bare domain")
    return selected


def _task(value: Any, split: str) -> dict[str, Any]:
    fields = {"task_id", "template_family", "kind", "prompt_template", "parameters"}
    if not isinstance(value, dict) or set(value) != fields:
        raise EvaluationInputError("task-set task has unexpected fields")
    task_id = _identifier(value.get("task_id"), "task.task_id")
    family = _identifier(value.get("template_family"), "task.template_family")
    kind = value.get("kind")
    if kind not in {"query", "open", "create"}:
        raise EvaluationInputError("task.kind is unsupported")
    prompt = _text(value.get("prompt_template"), "task.prompt_template")
    parameters = value.get("parameters")
    if not isinstance(parameters, dict):
        raise EvaluationInputError("task.parameters must be an object")
    placeholders = frozenset(_PLACEHOLDER.findall(prompt))
    if "{{" in _PLACEHOLDER.sub("", prompt) or "}}" in _PLACEHOLDER.sub("", prompt):
        raise EvaluationInputError("task.prompt_template has an invalid placeholder")
    if not placeholders <= _ALLOWED_PLACEHOLDERS[kind]:
        raise EvaluationInputError("task.prompt_template uses a placeholder outside its kind")
    if kind == "query":
        if parameters or placeholders:
            raise EvaluationInputError("query task cannot have parameters or placeholders")
        normalized_parameters: dict[str, Any] = {}
    elif kind == "open":
        if set(parameters) != {"baseline_index"}:
            raise EvaluationInputError("open task parameters must contain baseline_index")
        index = parameters.get("baseline_index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 999:
            raise EvaluationInputError("open task baseline_index is invalid")
        if not placeholders:
            raise EvaluationInputError("open task prompt must identify the selected company")
        normalized_parameters = {"baseline_index": index}
    else:
        if set(parameters) != {"name", "domain", "require_open"}:
            raise EvaluationInputError("create task parameters are incomplete")
        require_open = parameters.get("require_open")
        if not isinstance(require_open, bool):
            raise EvaluationInputError("create task require_open must be a boolean")
        if placeholders != _ALLOWED_PLACEHOLDERS["create"]:
            raise EvaluationInputError("create task prompt must contain name and domain")
        normalized_parameters = {
            "name": _text(parameters.get("name"), "task.parameters.name", maximum=100),
            "domain": _domain(parameters.get("domain"), "task.parameters.domain"),
            "require_open": require_open,
        }
    if split == "test" and not re.fullmatch(r"t[0-9]{3}", task_id):
        raise EvaluationInputError("test task ids must be opaque tNNN identifiers")
    return {
        "task_id": task_id,
        "template_family": family,
        "kind": kind,
        "prompt_template": prompt,
        "parameters": normalized_parameters,
    }


def validate_task_set(value: Any) -> dict[str, Any]:
    fields = {
        "artifact_version", "kind", "set_id", "split", "application_release",
        "commitment_nonce", "tasks",
    }
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("artifact_version") != TASK_SET_VERSION
            or value.get("kind") != "twenty_task_set"):
        raise EvaluationInputError("task-set header is invalid")
    set_id = _identifier(value.get("set_id"), "task_set.set_id")
    split = value.get("split")
    if split not in {"development", "test"}:
        raise EvaluationInputError("task_set.split is invalid")
    release = _text(value.get("application_release"), "task_set.application_release", maximum=40)
    if not _RELEASE.fullmatch(release):
        raise EvaluationInputError("task_set.application_release must pin an exact Twenty release")
    nonce = _text(value.get("commitment_nonce"), "task_set.commitment_nonce", maximum=32)
    if not _NONCE.fullmatch(nonce):
        raise EvaluationInputError("task_set.commitment_nonce must be 128-bit lowercase hex")
    raw_tasks = value.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks or len(raw_tasks) > 1_000:
        raise EvaluationInputError("task_set.tasks must be a bounded non-empty array")
    tasks = [_task(item, split) for item in raw_tasks]
    ids = [item["task_id"] for item in tasks]
    if len(ids) != len(set(ids)):
        raise EvaluationInputError("task ids must be unique")
    kinds = Counter(item["kind"] for item in tasks)
    minimum = 2 if split == "development" else 4
    if any(kinds.get(kind, 0) < minimum for kind in ("query", "open", "create")):
        raise EvaluationInputError(
            f"{split} task set requires at least {minimum} tasks of every supported kind")
    create_targets = [
        (item["parameters"]["name"].casefold(), item["parameters"]["domain"])
        for item in tasks if item["kind"] == "create"
    ]
    if len(create_targets) != len(set(create_targets)):
        raise EvaluationInputError("create task targets must be unique")
    return {
        "artifact_version": TASK_SET_VERSION,
        "kind": "twenty_task_set",
        "set_id": set_id,
        "split": split,
        "application_release": release,
        "commitment_nonce": nonce,
        "tasks": sorted(tasks, key=lambda item: item["task_id"]),
    }


def seal_task_set(value: Any) -> dict[str, Any]:
    task_set = validate_task_set(value)
    task_digests = {
        task["task_id"]: digest_json({
            "commitment_nonce": task_set["commitment_nonce"], "task": task,
        }) for task in task_set["tasks"]
    }
    family_digests = sorted({
        digest_json({"template_family": task["template_family"]})
        for task in task_set["tasks"]
    })
    counts = dict(sorted(Counter(task["kind"] for task in task_set["tasks"]).items()))
    core = {
        "artifact_version": TASK_SET_VERSION,
        "kind": "twenty_task_set_seal",
        "status": "READY",
        "set_id": task_set["set_id"],
        "split": task_set["split"],
        "application_release": task_set["application_release"],
        "task_count": len(task_set["tasks"]),
        "task_kind_counts": counts,
        "task_digests": task_digests,
        "template_family_digests": family_digests,
        "task_set_digest": digest_json(task_set),
    }
    return {**core, "seal_digest": digest_json(core)}


def validate_seal(value: Any) -> dict[str, Any]:
    fields = {
        "artifact_version", "kind", "status", "set_id", "split",
        "application_release", "task_count", "task_kind_counts", "task_digests",
        "template_family_digests", "task_set_digest", "seal_digest",
    }
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("artifact_version") != TASK_SET_VERSION
            or value.get("kind") != "twenty_task_set_seal"
            or value.get("status") != "READY"):
        raise EvaluationInputError("task-set seal header is invalid")
    core = {key: value[key] for key in value if key != "seal_digest"}
    if value.get("seal_digest") != digest_json(core):
        raise EvaluationInputError("task-set seal digest does not match")
    set_id = _identifier(value.get("set_id"), "task_set_seal.set_id")
    split = value.get("split")
    if split not in {"development", "test"}:
        raise EvaluationInputError("task-set seal split is invalid")
    if not _RELEASE.fullmatch(str(value.get("application_release") or "")):
        raise EvaluationInputError("task-set seal release is invalid")
    count = value.get("task_count")
    task_digests = value.get("task_digests")
    family_digests = value.get("template_family_digests")
    kind_counts = value.get("task_kind_counts")
    if (isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1_000
            or not isinstance(task_digests, dict) or len(task_digests) != count
            or not isinstance(family_digests, list) or not family_digests
            or len(family_digests) != len(set(family_digests))
            or not isinstance(kind_counts, dict)
            or set(kind_counts) != {"create", "open", "query"}):
        raise EvaluationInputError("task-set seal counts are invalid")
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 1
           for item in kind_counts.values()):
        raise EvaluationInputError("task-set seal kind count is invalid")
    if sum(kind_counts.values()) != count:
        raise EvaluationInputError("task-set seal counts are invalid")
    minimum = 2 if split == "development" else 4
    if any(kind_counts[kind] < minimum for kind in ("create", "open", "query")):
        raise EvaluationInputError("task-set seal kind count is below the split minimum")
    for task_id, digest in task_digests.items():
        if not _ID.fullmatch(str(task_id)) or not _DIGEST.fullmatch(str(digest)):
            raise EvaluationInputError("task-set seal task digest is invalid")
        if split == "test" and not re.fullmatch(r"t[0-9]{3}", str(task_id)):
            raise EvaluationInputError("test task seal contains a non-opaque task id")
    if (any(not _DIGEST.fullmatch(str(item)) for item in family_digests)
            or not _DIGEST.fullmatch(str(value.get("task_set_digest")))
            or not _DIGEST.fullmatch(str(value.get("seal_digest")))):
        raise EvaluationInputError("task-set seal contains an invalid digest")
    return {**value, "set_id": set_id}


def verify_task_set(value: Any, seal: Any) -> dict[str, Any]:
    expected = seal_task_set(value)
    validate_seal(seal)
    if seal != expected:
        raise EvaluationInputError("task set no longer matches its frozen seal")
    return {
        "artifact_version": TASK_SET_VERSION,
        "kind": "twenty_task_set_verification",
        "status": "READY",
        "set_id": expected["set_id"],
        "split": expected["split"],
        "task_count": expected["task_count"],
        "seal_digest": expected["seal_digest"],
    }


def verify_split_separation(development_seal: Any, test_seal: Any) -> dict[str, Any]:
    development = validate_seal(development_seal)
    test = validate_seal(test_seal)
    if development["split"] != "development" or test["split"] != "test":
        raise EvaluationInputError("split seals were supplied in the wrong roles")
    if development["application_release"] != test["application_release"]:
        raise EvaluationInputError("development and test seals pin different releases")
    if set(development["task_digests"]) & set(test["task_digests"]):
        raise EvaluationInputError("development and test task ids overlap")
    if set(development["task_digests"].values()) & set(test["task_digests"].values()):
        raise EvaluationInputError("development and test tasks overlap")
    if set(development["template_family_digests"]) & set(test["template_family_digests"]):
        raise EvaluationInputError("development and test template families overlap")
    return {
        "artifact_version": TASK_SET_VERSION,
        "kind": "twenty_split_verification",
        "status": "READY",
        "application_release": development["application_release"],
        "development_seal": development["seal_digest"],
        "test_seal": test["seal_digest"],
        "development_tasks": development["task_count"],
        "test_tasks": test["task_count"],
        "template_families_disjoint": True,
    }


def instantiate_task(value: Any, task_id: str, baseline: Any) -> dict[str, Any]:
    task_set = validate_task_set(value)
    matches = [task for task in task_set["tasks"] if task["task_id"] == task_id]
    if len(matches) != 1:
        raise EvaluationInputError("task id is not present exactly once")
    task = matches[0]
    prompt = task["prompt_template"]
    if task["kind"] == "query":
        evaluator_task = {"artifact_version": 1, "kind": "query"}
    elif task["kind"] == "open":
        state = validate_state(baseline, expected_role="baseline")
        if state["status"] != "READY":
            raise EvaluationInputError("open task requires a ready baseline")
        companies = sorted(
            (row for row in state["projection"]["companies"] if row["deleted_at"] is None),
            key=lambda row: (row["name"].casefold(), row["domain"], row["id"]),
        )
        index = task["parameters"]["baseline_index"]
        if index >= len(companies):
            raise EvaluationInputError("open task baseline selector is out of range")
        company = companies[index]
        prompt = prompt.replace("{{company.name}}", company["name"])
        prompt = prompt.replace("{{company.domain}}", company["domain"])
        evaluator_task = {
            "artifact_version": 1, "kind": "open", "company_id": company["id"]}
    else:
        parameters = task["parameters"]
        prompt = prompt.replace("{{company.name}}", parameters["name"])
        prompt = prompt.replace("{{company.domain}}", parameters["domain"])
        evaluator_task = {
            "artifact_version": 1,
            "kind": "create",
            "name": parameters["name"],
            "domain": parameters["domain"],
            "require_open": parameters["require_open"],
        }
    if _PLACEHOLDER.search(prompt) or "{{" in prompt or "}}" in prompt:
        raise EvaluationInputError("instantiated prompt still contains a placeholder")
    return {
        "artifact_version": TASK_SET_VERSION,
        "kind": "twenty_execution_case",
        "set_id": task_set["set_id"],
        "split": task_set["split"],
        "task_id": task["task_id"],
        "template_family_digest": digest_json(
            {"template_family": task["template_family"]}),
        "task_digest": digest_json({
            "commitment_nonce": task_set["commitment_nonce"], "task": task,
        }),
        "prompt": prompt,
        "evaluator_task": evaluator_task,
    }
