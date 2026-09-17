"""P7.4 task sets: specification, structural fingerprints, seals and instantiation.

A task names population *roles* and never a record value, so the development and held-out
sets can share one generator and still be instantiated against different synthetic data. Each
task carries a structural fingerprint; families must be unique within and across splits,
while the causal-shape distribution must match so the two splits describe one task
population. Seals follow the Twenty task-set discipline: a per-set 128-bit nonce salts every
task digest, so the public seal of a private held-out set does not reveal short prompts.

Instantiation produces two separate objects. ``RunnerInput`` holds only what a user would
type and where they start; ``EvaluatorInput`` holds the judgement specification. Runners are
typed to accept the former, so they cannot read the latter by accident.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from . import population as population_module
from .population import digest_json

TASK_SET_VERSION = 1
APPLICATION_RELEASE = "odoo@18.0-20260908"
STRATA = ("lookup_navigation", "filter_aggregate_completeness", "ambiguity_multiturn",
          "confirmed_write_readback", "permission_safety_refusal", "cross_page_mixed")
CLASSES = ("normal", "refusal_or_limitation")
LOCALES = ("zh-CN", "en-US")
ACTORS = {"writer": "odoo_writer_delegation_v1", "reader": "odoo_reader_delegation_v1",
          "other": "odoo_other_company_reader_v1"}
EVALUATOR_SHAPES = frozenset({
    "field_values", "open_record", "open_collection", "not_found", "count", "currency_sum",
    "argmax_open_or_name", "currency_breakdown", "clarify_then_open", "clarify_then_fields",
    "followup_fields", "followup_count", "clarify_then_cancel", "correction_open",
    "correction_fields", "create_customer", "confirm_quotation", "invoice_order",
    "approval_denied", "permission_refusal", "business_refusal", "cross_company_not_found",
    "unsupported_refusal", "related_open", "list_selection", "page_fact",
    "date_range_count_open",
})
RESET_PROFILES = ("none", "database_filestore_agent_state")
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{2,79}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+:)?([a-z0-9_.]+)\}\}")
_FINGERPRINT_FIELDS = ("causal_shape", "tools", "turns", "evaluator_type", "state_constraint",
                       "refusal_category", "mutation_cap", "entities", "conditions")


class TaskSetError(ValueError):
    """A task set, seal or instantiation violates the P7.4 task contract."""


def _text(value: Any, name: str, *, maximum: int = 600) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(ch) < 32 and ch not in "\n" for ch in value)):
        raise TaskSetError(f"{name} must be a bounded display string")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise TaskSetError(f"{name} is invalid")
    return value


def _strings(value: Any, name: str, *, allow_empty: bool = True) -> list[str]:
    if (not isinstance(value, list) or (not value and not allow_empty)
            or any(not isinstance(item, str) or not item for item in value)
            or len(value) != len(set(value))):
        raise TaskSetError(f"{name} must be a list of unique strings")
    return sorted(value)


def fingerprint_digest(fingerprint: dict[str, Any]) -> str:
    return digest_json({key: fingerprint[key] for key in _FINGERPRINT_FIELDS})


def validate_task(task: Any, split: str) -> dict[str, Any]:
    fields = {"task_id", "split", "stratum", "class", "family", "locale", "fingerprint",
              "messages", "entry_path", "preconditions", "actor", "capability_profile",
              "evaluator", "approval_oracle", "max_mutations", "reset_profile",
              "scope_statement"}
    if not isinstance(task, dict) or set(task) != fields:
        raise TaskSetError("task has unexpected fields")
    task_id = _identifier(task["task_id"], "task_id")
    if task["split"] != split:
        raise TaskSetError(f"{task_id} belongs to a different split")
    if split == "heldout" and not re.fullmatch(r"h[0-9]{3}", task_id):
        raise TaskSetError("held-out task ids must be opaque hNNN identifiers")
    if task["stratum"] not in STRATA or task["class"] not in CLASSES:
        raise TaskSetError(f"{task_id} stratum or class is invalid")
    if task["locale"] not in LOCALES:
        raise TaskSetError(f"{task_id} locale is invalid")
    _identifier(task["family"], "family")
    fingerprint = task["fingerprint"]
    if not isinstance(fingerprint, dict) or set(fingerprint) != set(_FINGERPRINT_FIELDS):
        raise TaskSetError(f"{task_id} fingerprint has unexpected fields")
    _identifier(fingerprint["causal_shape"], "causal_shape")
    for name in ("tools", "entities", "conditions"):
        if fingerprint[name] != _strings(fingerprint[name], name):
            raise TaskSetError(f"{task_id} fingerprint {name} must be sorted and unique")
    if fingerprint["evaluator_type"] not in EVALUATOR_SHAPES:
        raise TaskSetError(f"{task_id} evaluator type is unknown")
    if task["evaluator"].get("shape") != fingerprint["evaluator_type"]:
        raise TaskSetError(f"{task_id} evaluator shape disagrees with its fingerprint")
    messages = task["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= 4:
        raise TaskSetError(f"{task_id} needs one to four user messages")
    for message in messages:
        _text(message, "message")
    if fingerprint["turns"] != len(messages):
        raise TaskSetError(f"{task_id} fingerprint turns disagree with its messages")
    if task["entry_path"] is not None:
        _text(task["entry_path"], "entry_path", maximum=200)
    _strings(task["preconditions"], "preconditions")
    if task["actor"] not in ACTORS or task["capability_profile"] != ACTORS[task["actor"]]:
        raise TaskSetError(f"{task_id} capability profile must be fixed by its actor")
    if (isinstance(task["max_mutations"], bool) or task["max_mutations"] not in (0, 1)
            or fingerprint["mutation_cap"] != task["max_mutations"]):
        raise TaskSetError(f"{task_id} mutation cap is invalid")
    if task["reset_profile"] not in RESET_PROFILES:
        raise TaskSetError(f"{task_id} reset profile is invalid")
    if task["max_mutations"] and task["reset_profile"] == "none":
        raise TaskSetError(f"{task_id} can mutate and therefore needs a reset")
    oracle = task["approval_oracle"]
    if oracle is not None:
        if (not isinstance(oracle, dict) or set(oracle) != {"decision", "effect", "targets"}
                or oracle["decision"] not in {"approve", "deny"}):
            raise TaskSetError(f"{task_id} approval oracle is invalid")
        _text(oracle["effect"], "oracle effect")
        _strings(oracle["targets"], "oracle targets", allow_empty=False)
    if (task["class"] == "refusal_or_limitation") != bool(fingerprint["refusal_category"]):
        raise TaskSetError(f"{task_id} refusal category must match its class")
    _text(task["scope_statement"], "scope_statement")
    return task


def validate_task_set(value: Any) -> dict[str, Any]:
    fields = {"artifact_version", "kind", "set_id", "split", "application_release",
              "population_generator", "population_seed_commitment", "commitment_nonce",
              "tasks"}
    if (not isinstance(value, dict) or set(value) != fields
            or value["artifact_version"] != TASK_SET_VERSION
            or value["kind"] != "p74_odoo_task_set"):
        raise TaskSetError("task-set header is invalid")
    split = value["split"]
    if split not in {"development", "heldout"}:
        raise TaskSetError("task-set split is invalid")
    if value["application_release"] != APPLICATION_RELEASE:
        raise TaskSetError("task set must pin the qualified Odoo release")
    if value["population_generator"] != population_module.GENERATOR_ID:
        raise TaskSetError("task set must name the population generator")
    if not isinstance(value["commitment_nonce"], str) or not _NONCE.fullmatch(
            value["commitment_nonce"]):
        raise TaskSetError("commitment nonce must be 128-bit lowercase hex")
    tasks = [validate_task(task, split) for task in value["tasks"]]
    if len(tasks) != 30:
        raise TaskSetError("a P7.4 split has exactly 30 tasks")
    if Counter(task["stratum"] for task in tasks) != Counter({stratum: 5 for stratum in STRATA}):
        raise TaskSetError("each stratum needs exactly five families")
    for name, key in (("task ids", "task_id"), ("families", "family")):
        values = [task[key] for task in tasks]
        if len(values) != len(set(values)):
            raise TaskSetError(f"{name} must be unique within a split")
    prints = [fingerprint_digest(task["fingerprint"]) for task in tasks]
    if len(prints) != len(set(prints)):
        raise TaskSetError("structural fingerprints must be unique within a split")
    return {**value, "tasks": sorted(tasks, key=lambda task: task["task_id"])}


def _task_digest(nonce: str, task: dict[str, Any]) -> str:
    return digest_json({"commitment_nonce": nonce, "task": task})


def seal_task_set(value: Any, *, population: dict[str, Any]) -> dict[str, Any]:
    task_set = validate_task_set(value)
    if population["seed_commitment"] != task_set["population_seed_commitment"]:
        raise TaskSetError("population does not match the task set's seed commitment")
    nonce = task_set["commitment_nonce"]
    tasks = task_set["tasks"]
    core = {
        "artifact_version": TASK_SET_VERSION,
        "kind": "p74_odoo_task_set_seal",
        "status": "READY",
        "set_id": task_set["set_id"],
        "split": task_set["split"],
        "application_release": task_set["application_release"],
        "population_generator": task_set["population_generator"],
        "population_seed_commitment": task_set["population_seed_commitment"],
        "fixture_truth_digest": population_module.population_digest(population),
        "task_count": len(tasks),
        "stratum_counts": dict(sorted(Counter(task["stratum"] for task in tasks).items())),
        "class_counts": dict(sorted(Counter(task["class"] for task in tasks).items())),
        "shape_distribution": dict(sorted(Counter(
            task["fingerprint"]["causal_shape"] for task in tasks).items())),
        "task_digests": {task["task_id"]: _task_digest(nonce, task) for task in tasks},
        "family_digests": sorted(digest_json({"family": task["family"]}) for task in tasks),
        "fingerprint_digests": sorted(fingerprint_digest(task["fingerprint"]) for task in tasks),
        "evaluator_specification_digest": digest_json(
            {task["task_id"]: task["evaluator"] for task in tasks}),
        "task_set_digest": digest_json(task_set),
    }
    return {**core, "seal_digest": digest_json(core)}


def verify_seal(value: Any, seal: Any, *, population: dict[str, Any]) -> dict[str, Any]:
    expected = seal_task_set(value, population=population)
    if seal != expected:
        raise TaskSetError("task set no longer matches its frozen seal")
    return expected


def verify_task_content(value: Any, seal: Any) -> dict[str, Any]:
    """Every task is the one the seal froze, by the seal's own per-task digests.

    :func:`verify_seal` answers a wider question and needs the population to answer it. A reader
    that already holds the seal the freeze bound needs only this: that the tasks in front of it
    are the tasks that seal names, so a class or a stratum cannot be moved after a batch ran.
    """
    task_set = validate_task_set(value)
    sealed = _validated_seal(seal)
    nonce = task_set["commitment_nonce"]
    found = {task["task_id"]: _task_digest(nonce, task) for task in task_set["tasks"]}
    if found != sealed["task_digests"]:
        raise TaskSetError("task set no longer matches its frozen seal")
    return sealed


def _validated_seal(seal: Any) -> dict[str, Any]:
    if not isinstance(seal, dict) or seal.get("kind") != "p74_odoo_task_set_seal":
        raise TaskSetError("seal header is invalid")
    core = {key: value for key, value in seal.items() if key != "seal_digest"}
    if seal.get("seal_digest") != digest_json(core):
        raise TaskSetError("seal digest does not match")
    return seal


def verify_split_separation(development_seal: Any, heldout_seal: Any) -> dict[str, Any]:
    development = _validated_seal(development_seal)
    heldout = _validated_seal(heldout_seal)
    if development["split"] != "development" or heldout["split"] != "heldout":
        raise TaskSetError("split seals were supplied in the wrong roles")
    for key in ("application_release", "population_generator"):
        if development[key] != heldout[key]:
            raise TaskSetError(f"splits disagree on {key}")
    if development["population_seed_commitment"] == heldout["population_seed_commitment"]:
        raise TaskSetError("the two splits must use independent population seeds")
    checks = (("task ids", set(development["task_digests"]), set(heldout["task_digests"])),
              ("tasks", set(development["task_digests"].values()),
               set(heldout["task_digests"].values())),
              ("families", set(development["family_digests"]), set(heldout["family_digests"])),
              ("fingerprints", set(development["fingerprint_digests"]),
               set(heldout["fingerprint_digests"])))
    for name, left, right in checks:
        if left & right:
            raise TaskSetError(f"development and held-out {name} overlap")
    if development["shape_distribution"] != heldout["shape_distribution"]:
        raise TaskSetError("the splits' causal-shape distributions do not match")
    if development["class_counts"] != heldout["class_counts"]:
        raise TaskSetError("the splits' normal/refusal counts do not match")
    return {
        "artifact_version": TASK_SET_VERSION,
        "kind": "p74_odoo_split_verification",
        "status": "READY",
        "application_release": development["application_release"],
        "development_seal": development["seal_digest"],
        "heldout_seal": heldout["seal_digest"],
        "families_disjoint": True,
        "fingerprints_disjoint": True,
        "shape_distribution_matched": True,
        "normal_tasks_per_split": development["class_counts"].get("normal", 0),
        "refusal_or_limitation_tasks_per_split": development["class_counts"].get(
            "refusal_or_limitation", 0),
    }


# -- instantiation -------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RunnerInput:
    """Only what a person would type, where they start, and whose account they use."""
    task_id: str
    actor: str
    locale: str
    entry_path: str | None
    messages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluatorInput:
    task_id: str
    task_digest: str
    stratum: str
    task_class: str
    actor: str
    shape: str
    params: dict[str, Any]
    approval_oracle: dict[str, Any] | None
    max_mutations: int
    reset_profile: str


def _resolve(template: str, population: dict[str, Any], ids: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        prefix, path = match.group(1), match.group(2)
        if prefix == "id:":
            key = population["roles"][path]
            table = {"partner": "partners", "order": "orders", "invoice": "invoices",
                     "product": "products"}[key.split(":", 1)[0]]
            return str(ids[table][key])
        if prefix:
            raise TaskSetError("unknown placeholder prefix")
        if path in population["roles"] and isinstance(population["roles"][path], str) \
                and ":" not in population["roles"][path]:
            return population["roles"][path]
        if path.startswith("absent."):
            return population["absent"][path]
        head, _, field = path.rpartition(".")
        if head.startswith("new."):
            return str(population["new_records"][head][field])
        value = population_module.record(population, head).get(field)
        if value is None:
            raise TaskSetError(f"placeholder {path} resolves to an empty value")
        return str(value)

    resolved = _PLACEHOLDER.sub(replace, template)
    if "{{" in resolved or "}}" in resolved:
        raise TaskSetError("instantiated text still contains a placeholder")
    return resolved


def _resolve_params(value: Any, population: dict[str, Any], ids: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _resolve(value, population, ids) if "{{" in value else value
    if isinstance(value, list):
        return [_resolve_params(item, population, ids) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_params(item, population, ids) for key, item in value.items()}
    return value


def instantiate(task_set: dict[str, Any], task_id: str, *, population: dict[str, Any],
                seed_ids: dict[str, Any]) -> tuple[RunnerInput, EvaluatorInput]:
    validated = validate_task_set(task_set)
    if population["seed_commitment"] != validated["population_seed_commitment"]:
        raise TaskSetError("population does not belong to this task set")
    if seed_ids.get("population_seed_commitment") != population["seed_commitment"]:
        raise TaskSetError("seeded fixture ids belong to a different population")
    matches = [task for task in validated["tasks"] if task["task_id"] == task_id]
    if len(matches) != 1:
        raise TaskSetError("task id is not present exactly once")
    task = matches[0]
    for role in task["preconditions"]:
        if role not in population["roles"] and not role.startswith(("new.", "absent.")):
            raise TaskSetError(f"precondition role {role} is not in the population")
    runner = RunnerInput(
        task_id=task["task_id"], actor=task["actor"], locale=task["locale"],
        entry_path=(_resolve(task["entry_path"], population, seed_ids)
                    if task["entry_path"] else None),
        messages=tuple(_resolve(message, population, seed_ids)
                       for message in task["messages"]))
    oracle = task["approval_oracle"]
    evaluator = EvaluatorInput(
        task_id=task["task_id"],
        task_digest=_task_digest(validated["commitment_nonce"], task),
        stratum=task["stratum"], task_class=task["class"], actor=task["actor"],
        shape=task["evaluator"]["shape"],
        params=_resolve_params(task["evaluator"].get("params", {}), population, seed_ids),
        approval_oracle=(_resolve_params(oracle, population, seed_ids) if oracle else None),
        max_mutations=task["max_mutations"], reset_profile=task["reset_profile"])
    return runner, evaluator
