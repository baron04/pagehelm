"""Strict preregistration contract for paired Twenty evaluation runs."""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from .evaluator import EvaluationInputError, digest_json
from .taskset import validate_seal, verify_split_separation


PROTOCOL_VERSION = 1
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{2,79}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RELEASE = re.compile(r"[a-z0-9][a-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-z0-9_.-]+)?\Z")
_LOCALE = re.compile(r"[a-z]{2,3}(?:-[A-Z]{2})?\Z")
_TIMEZONE = re.compile(r"[A-Za-z_+-]+(?:/[A-Za-z_+-]+)+\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_COMPARISON_TYPES = {
    "browser_execution", "skill_api", "source_runtime",
}
_SOURCE_REASONS = {
    "hidden_static_route", "hidden_static_schema", "hidden_static_rule",
    "runtime_or_visible_only",
}
_EXCLUDED_SOURCE_CLASSES = (
    "artifacts", "credentials", "database_and_fixtures", "evaluator",
    "runtime_and_user_data", "task_sets",
)


def _text(value: Any, name: str, *, maximum: int = 200) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(character) < 32 for character in value)):
        raise EvaluationInputError(f"{name} must be a bounded single-line string")
    return value.strip()


def _identifier(value: Any, name: str) -> str:
    selected = _text(value, name, maximum=80)
    if not _ID.fullmatch(selected):
        raise EvaluationInputError(f"{name} is invalid")
    return selected


def _digest(value: Any, name: str, *, allow_none: bool = False) -> str | None:
    if allow_none and value is None:
        return None
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise EvaluationInputError(f"{name} must be a SHA-256 digest")
    return value


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise EvaluationInputError(f"{name} is outside its allowed range")
    return value


def _number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationInputError(f"{name} must be a finite number")
    selected = float(value)
    if not math.isfinite(selected) or not minimum <= selected <= maximum:
        raise EvaluationInputError(f"{name} is outside its allowed range")
    return selected


def _exact(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise EvaluationInputError(f"{name} has unexpected fields")
    return value


def _artifact_bindings(value: Any, development_seal: Any, test_seal: Any,
                       split_verification: Any) -> dict[str, str]:
    fields = {"development_seal", "test_seal", "split_verification"}
    raw = _exact(value, fields, "protocol.task_sets")
    development = validate_seal(development_seal)
    test = validate_seal(test_seal)
    expected_split = verify_split_separation(development, test)
    if split_verification != expected_split:
        raise EvaluationInputError("split verification does not match the supplied seals")
    expected = {
        "development_seal": development["seal_digest"],
        "test_seal": test["seal_digest"],
        "split_verification": digest_json(expected_split),
    }
    normalized = {key: _digest(raw.get(key), f"protocol.task_sets.{key}") for key in fields}
    if normalized != expected:
        raise EvaluationInputError("protocol task-set bindings do not match supplied artifacts")
    return expected


def _environment(value: Any, application_release: str) -> dict[str, Any]:
    fields = {
        "application_release", "browser_release", "locale", "timezone",
        "viewport", "fixture_qualification_digest", "reset_contract_digest",
        "cache_policy",
    }
    raw = _exact(value, fields, "protocol.environment")
    release = _text(raw.get("application_release"), "environment.application_release", maximum=80)
    browser = _text(raw.get("browser_release"), "environment.browser_release", maximum=80)
    if release != application_release or not _RELEASE.fullmatch(release):
        raise EvaluationInputError("environment application release does not match the task set")
    if not _RELEASE.fullmatch(browser):
        raise EvaluationInputError("environment browser release is not exact")
    locale = _text(raw.get("locale"), "environment.locale", maximum=16)
    timezone = _text(raw.get("timezone"), "environment.timezone", maximum=64)
    if not _LOCALE.fullmatch(locale) or not _TIMEZONE.fullmatch(timezone):
        raise EvaluationInputError("environment locale or timezone is invalid")
    viewport = _exact(raw.get("viewport"), {"width", "height", "device_scale_factor"},
                      "environment.viewport")
    normalized_viewport = {
        "width": _integer(viewport.get("width"), "viewport.width", minimum=320, maximum=3840),
        "height": _integer(viewport.get("height"), "viewport.height", minimum=480, maximum=2160),
        "device_scale_factor": _number(
            viewport.get("device_scale_factor"), "viewport.device_scale_factor",
            minimum=0.5, maximum=4.0),
    }
    cache_policy = raw.get("cache_policy")
    if cache_policy not in {"cold_each_case", "warm_within_session"}:
        raise EvaluationInputError("environment cache policy is invalid")
    return {
        "application_release": release,
        "browser_release": browser,
        "locale": locale,
        "timezone": timezone,
        "viewport": normalized_viewport,
        "fixture_qualification_digest": _digest(
            raw.get("fixture_qualification_digest"), "environment.fixture_qualification_digest"),
        "reset_contract_digest": _digest(
            raw.get("reset_contract_digest"), "environment.reset_contract_digest"),
        "cache_policy": cache_policy,
    }


def _provider(value: Any) -> dict[str, Any]:
    fields = {
        "provider_id", "endpoint_class", "requested_model", "snapshot_policy",
        "temperature", "top_p", "max_output_tokens", "context_window_tokens",
        "seed_mode", "seed", "request_schema_digest", "response_schema_digest",
        "pair_window_minutes",
    }
    raw = _exact(value, fields, "protocol.provider")
    endpoint_class = raw.get("endpoint_class")
    snapshot_policy = raw.get("snapshot_policy")
    seed_mode = raw.get("seed_mode")
    if endpoint_class != "openai_compatible_https":
        raise EvaluationInputError("provider endpoint class is invalid")
    if snapshot_policy != "record_reported_model_and_utc_window":
        raise EvaluationInputError("provider snapshot policy is invalid")
    if seed_mode not in {"unsupported", "provider_verified"}:
        raise EvaluationInputError("provider seed mode is invalid")
    seed = raw.get("seed")
    if seed_mode == "unsupported" and seed is not None:
        raise EvaluationInputError("an unsupported provider seed must be null")
    if seed_mode == "provider_verified":
        seed = _integer(seed, "provider.seed", minimum=0, maximum=2 ** 31 - 1)
    return {
        "provider_id": _identifier(raw.get("provider_id"), "provider.provider_id"),
        "endpoint_class": endpoint_class,
        "requested_model": _text(raw.get("requested_model"), "provider.requested_model", maximum=120),
        "snapshot_policy": snapshot_policy,
        "temperature": _number(raw.get("temperature"), "provider.temperature", minimum=0, maximum=2),
        "top_p": _number(raw.get("top_p"), "provider.top_p", minimum=0, maximum=1),
        "max_output_tokens": _integer(
            raw.get("max_output_tokens"), "provider.max_output_tokens", minimum=1, maximum=131_072),
        "context_window_tokens": _integer(
            raw.get("context_window_tokens"), "provider.context_window_tokens",
            minimum=1_024, maximum=4_000_000),
        "seed_mode": seed_mode,
        "seed": seed,
        "request_schema_digest": _digest(
            raw.get("request_schema_digest"), "provider.request_schema_digest"),
        "response_schema_digest": _digest(
            raw.get("response_schema_digest"), "provider.response_schema_digest"),
        "pair_window_minutes": _integer(
            raw.get("pair_window_minutes"), "provider.pair_window_minutes", minimum=1, maximum=120),
    }


def _budgets(value: Any, provider: dict[str, Any]) -> dict[str, int]:
    fields = {
        "max_model_calls", "max_page_actions", "max_business_calls",
        "max_wall_time_s", "input_token_abort_threshold",
        "max_completion_tokens_per_call",
        "automatic_retries", "user_interventions",
    }
    raw = _exact(value, fields, "protocol.budgets")
    normalized = {
        "max_model_calls": _integer(raw.get("max_model_calls"), "budgets.max_model_calls", minimum=1, maximum=100),
        "max_page_actions": _integer(raw.get("max_page_actions"), "budgets.max_page_actions", minimum=0, maximum=200),
        "max_business_calls": _integer(raw.get("max_business_calls"), "budgets.max_business_calls", minimum=0, maximum=200),
        "max_wall_time_s": _integer(raw.get("max_wall_time_s"), "budgets.max_wall_time_s", minimum=1, maximum=3_600),
        "input_token_abort_threshold": _integer(
            raw.get("input_token_abort_threshold"), "budgets.input_token_abort_threshold",
            minimum=1, maximum=4_000_000),
        "max_completion_tokens_per_call": _integer(
            raw.get("max_completion_tokens_per_call"),
            "budgets.max_completion_tokens_per_call", minimum=1, maximum=131_072),
        "automatic_retries": _integer(raw.get("automatic_retries"), "budgets.automatic_retries", minimum=0, maximum=0),
        "user_interventions": _integer(raw.get("user_interventions"), "budgets.user_interventions", minimum=0, maximum=0),
    }
    if normalized["max_completion_tokens_per_call"] > provider["max_output_tokens"]:
        raise EvaluationInputError("task output budget exceeds the provider request limit")
    if (normalized["input_token_abort_threshold"]
            + normalized["max_completion_tokens_per_call"]
            > provider["context_window_tokens"]):
        raise EvaluationInputError("task token budgets exceed the provider context window")
    return normalized


def _source_policy(value: Any) -> dict[str, Any]:
    fields = {
        "allowlist_digest", "release_digest", "excluded_classes",
        "runtime_data_access", "test_artifact_access", "generated_from_heldout",
        "citation_required", "fail_closed_on_stale",
    }
    raw = _exact(value, fields, "protocol.source_policy")
    excluded = raw.get("excluded_classes")
    if not isinstance(excluded, list) or tuple(excluded) != _EXCLUDED_SOURCE_CLASSES:
        raise EvaluationInputError("source exclusions are incomplete or not canonical")
    required_flags = {
        "runtime_data_access": False,
        "test_artifact_access": False,
        "generated_from_heldout": False,
        "citation_required": True,
        "fail_closed_on_stale": True,
    }
    if any(raw.get(key) is not expected for key, expected in required_flags.items()):
        raise EvaluationInputError("source policy weakens a required isolation control")
    return {
        "allowlist_digest": _digest(raw.get("allowlist_digest"), "source_policy.allowlist_digest"),
        "release_digest": _digest(raw.get("release_digest"), "source_policy.release_digest"),
        "excluded_classes": list(_EXCLUDED_SOURCE_CLASSES),
        **required_flags,
    }


def _artifacts(value: Any) -> dict[str, str]:
    fields = {
        "evaluator_digest", "runner_digest", "control_policy_digest",
        "approval_policy_digest",
    }
    raw = _exact(value, fields, "protocol.artifacts")
    return {key: _digest(raw.get(key), f"protocol.artifacts.{key}") for key in sorted(fields)}


def _arm(value: Any, source_policy_digest: str) -> dict[str, Any]:
    fields = {
        "arm_id", "engine", "engine_release", "implementation_digest",
        "capability_profile", "source_mode", "source_policy_digest",
        "skill_digest", "tool_catalog_digest",
    }
    raw = _exact(value, fields, "protocol.arm")
    engine = raw.get("engine")
    capability = raw.get("capability_profile")
    source_mode = raw.get("source_mode")
    if engine not in {"pageagent", "pageagent_core", "web_operation_agent"}:
        raise EvaluationInputError("arm engine is invalid")
    release = _text(raw.get("engine_release"), "arm.engine_release", maximum=100)
    if not _RELEASE.fullmatch(release):
        raise EvaluationInputError("arm engine release is not exact")
    if capability not in {"browser_only", "skill_api"} or source_mode not in {"off", "on"}:
        raise EvaluationInputError("arm capability or source mode is invalid")
    source_digest = _digest(
        raw.get("source_policy_digest"), "arm.source_policy_digest", allow_none=True)
    skill_digest = _digest(raw.get("skill_digest"), "arm.skill_digest", allow_none=True)
    tool_digest = _digest(
        raw.get("tool_catalog_digest"), "arm.tool_catalog_digest", allow_none=True)
    if source_mode == "on" and source_digest != source_policy_digest:
        raise EvaluationInputError("source-on arm is not bound to the protocol source policy")
    if source_mode == "off" and source_digest is not None:
        raise EvaluationInputError("source-off arm cannot carry a source policy")
    if capability == "browser_only" and (skill_digest is not None or tool_digest is not None):
        raise EvaluationInputError("browser-only arm cannot receive Skill or tool artifacts")
    if capability == "skill_api" and (skill_digest is None or tool_digest is None):
        raise EvaluationInputError("skill/API arm must pin Skill and tool artifacts")
    if engine != "web_operation_agent" and source_mode != "off":
        raise EvaluationInputError("only Web Operation Agent may enable SourceProvider")
    return {
        "arm_id": _identifier(raw.get("arm_id"), "arm.arm_id"),
        "engine": engine,
        "engine_release": release,
        "implementation_digest": _digest(
            raw.get("implementation_digest"), "arm.implementation_digest"),
        "capability_profile": capability,
        "source_mode": source_mode,
        "source_policy_digest": source_digest,
        "skill_digest": skill_digest,
        "tool_catalog_digest": tool_digest,
    }


def _comparison(value: Any, task_ids: set[str], arms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fields = {
        "comparison_id", "comparison_type", "left_arm", "right_arm",
        "eligible_task_ids", "minimum_useful_effect_pp",
        "success_noninferiority_margin_pp", "efficiency_improvement_percent",
    }
    raw = _exact(value, fields, "protocol.comparison")
    comparison_type = raw.get("comparison_type")
    if comparison_type not in _COMPARISON_TYPES:
        raise EvaluationInputError("comparison type is invalid")
    left_id = _identifier(raw.get("left_arm"), "comparison.left_arm")
    right_id = _identifier(raw.get("right_arm"), "comparison.right_arm")
    if left_id == right_id or left_id not in arms or right_id not in arms:
        raise EvaluationInputError("comparison arms are absent or identical")
    eligible = raw.get("eligible_task_ids")
    if (not isinstance(eligible, list) or not eligible
            or any(not isinstance(item, str) for item in eligible)
            or eligible != sorted(set(eligible)) or not set(eligible) <= task_ids):
        raise EvaluationInputError("comparison task eligibility is invalid")
    left, right = arms[left_id], arms[right_id]
    if comparison_type == "browser_execution":
        valid = (
            left["engine"] == "pageagent" and right["engine"] == "web_operation_agent"
            and left["capability_profile"] == right["capability_profile"] == "browser_only"
            and left["source_mode"] == right["source_mode"] == "off"
        )
    elif comparison_type == "skill_api":
        valid = (
            left["engine"] == "pageagent_core" and right["engine"] == "web_operation_agent"
            and left["capability_profile"] == right["capability_profile"] == "skill_api"
            and left["source_mode"] == right["source_mode"] == "off"
            and left["skill_digest"] == right["skill_digest"]
            and left["tool_catalog_digest"] == right["tool_catalog_digest"]
        )
    else:
        varying_fields = {
            key for key in left if left[key] != right[key]
        }
        valid = (
            left["engine"] == right["engine"] == "web_operation_agent"
            and left["capability_profile"] == right["capability_profile"] == "skill_api"
            and {left["source_mode"], right["source_mode"]} == {"off", "on"}
            and varying_fields == {"arm_id", "source_mode", "source_policy_digest"}
        )
    if not valid:
        raise EvaluationInputError(f"{comparison_type} arms violate the fairness contract")
    return {
        "comparison_id": _identifier(raw.get("comparison_id"), "comparison.comparison_id"),
        "comparison_type": comparison_type,
        "left_arm": left_id,
        "right_arm": right_id,
        "eligible_task_ids": eligible,
        "minimum_useful_effect_pp": _integer(
            raw.get("minimum_useful_effect_pp"), "comparison.minimum_useful_effect_pp",
            minimum=1, maximum=50),
        "success_noninferiority_margin_pp": _integer(
            raw.get("success_noninferiority_margin_pp"),
            "comparison.success_noninferiority_margin_pp", minimum=-20, maximum=0),
        "efficiency_improvement_percent": _integer(
            raw.get("efficiency_improvement_percent"),
            "comparison.efficiency_improvement_percent", minimum=1, maximum=100),
    }


def _eligibility(value: Any, task_ids: set[str],
                 comparisons: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(task_ids):
        raise EvaluationInputError("eligibility map must cover every held-out task exactly once")
    rows: list[dict[str, Any]] = []
    for item in value:
        raw = _exact(item, {"task_id", "comparisons", "source_eligible", "source_reason"},
                     "protocol.eligibility row")
        task_id = raw.get("task_id")
        selected = raw.get("comparisons")
        source_eligible = raw.get("source_eligible")
        reason = raw.get("source_reason")
        if task_id not in task_ids or not isinstance(selected, list) or not selected:
            raise EvaluationInputError("eligibility row has an invalid task or empty comparison list")
        if selected != sorted(set(selected)) or not set(selected) <= set(comparisons):
            raise EvaluationInputError("eligibility row comparison list is invalid")
        if not isinstance(source_eligible, bool) or reason not in _SOURCE_REASONS:
            raise EvaluationInputError("eligibility row source classification is invalid")
        expected_source = reason != "runtime_or_visible_only"
        source_comparisons = {
            comparison_id for comparison_id in selected
            if comparisons[comparison_id]["comparison_type"] == "source_runtime"
        }
        if source_eligible is not expected_source or bool(source_comparisons) is not source_eligible:
            raise EvaluationInputError("source eligibility disagrees with its reason or comparisons")
        rows.append({
            "task_id": task_id,
            "comparisons": selected,
            "source_eligible": source_eligible,
            "source_reason": reason,
        })
    rows.sort(key=lambda row: row["task_id"])
    if {row["task_id"] for row in rows} != task_ids:
        raise EvaluationInputError("eligibility map has missing or duplicate task ids")
    for comparison_id, comparison in comparisons.items():
        mapped = sorted(row["task_id"] for row in rows if comparison_id in row["comparisons"])
        if mapped != comparison["eligible_task_ids"]:
            raise EvaluationInputError("eligibility rows disagree with a comparison task list")
    return rows


def _stop_policy(value: Any) -> dict[str, Any]:
    required = {
        "apparatus_invalid": "seal_batch",
        "safety_violation": "stop_batch",
        "reset_mismatch": "stop_batch",
        "spend_ledger_mismatch": "stop_batch",
        "provider_outage": "stop_batch",
        "rerun_valid_failure": False,
        "filter_invalid_primary_analysis": False,
    }
    raw = _exact(value, set(required), "protocol.stop_policy")
    if raw != required:
        raise EvaluationInputError("stop policy weakens a required fail-fast rule")
    return dict(required)


def _hash_order(seed: str, label: str) -> str:
    return hashlib.sha256(f"{seed}|{label}".encode("utf-8")).hexdigest()


def _schedule(comparisons: list[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
    pairs: list[dict[str, str]] = []
    for comparison in comparisons:
        comparison_id = comparison["comparison_id"]
        ordered_tasks = sorted(
            comparison["eligible_task_ids"],
            key=lambda task_id: _hash_order(seed, f"arm-order|{comparison_id}|{task_id}"),
        )
        orientation = int(_hash_order(seed, f"orientation|{comparison_id}"), 16) % 2
        for rank, task_id in enumerate(ordered_tasks):
            first_arm = comparison[
                "left_arm" if (rank + orientation) % 2 == 0 else "right_arm"]
            pairs.append({
                "comparison_id": comparison_id,
                "task_id": task_id,
                "first_arm": first_arm,
            })
    pairs.sort(key=lambda row: _hash_order(
        seed, f"pair-order|{row['comparison_id']}|{row['task_id']}"))
    return [{"position": position, **row} for position, row in enumerate(pairs, start=1)]


def _validate_draft(value: Any, development_seal: Any, test_seal: Any,
                    split_verification: Any) -> dict[str, Any]:
    fields = {
        "artifact_version", "kind", "status", "protocol_id", "task_sets",
        "environment", "provider", "budgets", "artifacts", "source_policy",
        "arms", "comparisons", "eligibility", "randomization_seed", "stop_policy",
    }
    raw = _exact(value, fields, "evaluation protocol draft")
    if (raw.get("artifact_version") != PROTOCOL_VERSION
            or raw.get("kind") != "twenty_evaluation_protocol"
            or raw.get("status") != "DRAFT"):
        raise EvaluationInputError("evaluation protocol draft header is invalid")
    bindings = _artifact_bindings(
        raw.get("task_sets"), development_seal, test_seal, split_verification)
    test = validate_seal(test_seal)
    task_ids = set(test["task_digests"])
    environment = _environment(raw.get("environment"), test["application_release"])
    provider = _provider(raw.get("provider"))
    budgets = _budgets(raw.get("budgets"), provider)
    source_policy = _source_policy(raw.get("source_policy"))
    source_policy_digest = digest_json(source_policy)
    raw_arms = raw.get("arms")
    if not isinstance(raw_arms, list) or len(raw_arms) != 5:
        raise EvaluationInputError("protocol must define exactly five preregistered arms")
    arm_list = [_arm(item, source_policy_digest) for item in raw_arms]
    arms = {item["arm_id"]: item for item in arm_list}
    if len(arms) != len(arm_list):
        raise EvaluationInputError("arm ids must be unique")
    raw_comparisons = raw.get("comparisons")
    if not isinstance(raw_comparisons, list) or len(raw_comparisons) != 3:
        raise EvaluationInputError("protocol must define exactly three primary comparisons")
    comparison_list = [_comparison(item, task_ids, arms) for item in raw_comparisons]
    comparisons = {item["comparison_id"]: item for item in comparison_list}
    if (len(comparisons) != 3
            or {item["comparison_type"] for item in comparison_list} != _COMPARISON_TYPES):
        raise EvaluationInputError("comparison ids or primary comparison types are incomplete")
    used_arms = {
        arm_id for item in comparison_list for arm_id in (item["left_arm"], item["right_arm"])
    }
    if used_arms != set(arms):
        raise EvaluationInputError("every arm must participate and the source-off WOA arm must be reused")
    eligibility = _eligibility(raw.get("eligibility"), task_ids, comparisons)
    seed = _text(raw.get("randomization_seed"), "protocol.randomization_seed", maximum=32)
    if not _NONCE.fullmatch(seed):
        raise EvaluationInputError("protocol randomization seed must be 128-bit lowercase hex")
    return {
        "artifact_version": PROTOCOL_VERSION,
        "kind": "twenty_evaluation_protocol",
        "status": "DRAFT",
        "protocol_id": _identifier(raw.get("protocol_id"), "protocol.protocol_id"),
        "task_sets": bindings,
        "environment": environment,
        "provider": provider,
        "budgets": budgets,
        "artifacts": _artifacts(raw.get("artifacts")),
        "source_policy": source_policy,
        "arms": sorted(arm_list, key=lambda item: item["arm_id"]),
        "comparisons": sorted(comparison_list, key=lambda item: item["comparison_id"]),
        "eligibility": eligibility,
        "randomization_seed": seed,
        "stop_policy": _stop_policy(raw.get("stop_policy")),
    }


def freeze_protocol(value: Any, *, development_seal: Any, test_seal: Any,
                    split_verification: Any) -> dict[str, Any]:
    draft = _validate_draft(value, development_seal, test_seal, split_verification)
    core = {
        **draft,
        "status": "FROZEN",
        "schedule": _schedule(draft["comparisons"], draft["randomization_seed"]),
    }
    return {**core, "protocol_digest": digest_json(core)}


def verify_protocol(value: Any, *, development_seal: Any, test_seal: Any,
                    split_verification: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvaluationInputError("frozen evaluation protocol must be an object")
    core = {key: value[key] for key in value if key != "protocol_digest"}
    if (set(value) != {
            "artifact_version", "kind", "status", "protocol_id", "task_sets",
            "environment", "provider", "budgets", "artifacts", "source_policy",
            "arms", "comparisons", "eligibility", "randomization_seed", "stop_policy",
            "schedule", "protocol_digest"}
            or value.get("status") != "FROZEN"
            or _digest(value.get("protocol_digest"), "protocol.protocol_digest") != digest_json(core)):
        raise EvaluationInputError("frozen evaluation protocol header or digest is invalid")
    draft = {key: core[key] for key in core if key != "schedule"}
    draft["status"] = "DRAFT"
    normalized = _validate_draft(
        draft, development_seal, test_seal, split_verification)
    expected_schedule = _schedule(
        normalized["comparisons"], normalized["randomization_seed"])
    if core.get("schedule") != expected_schedule:
        raise EvaluationInputError("protocol schedule does not match its frozen randomization")
    expected = {**normalized, "status": "FROZEN", "schedule": expected_schedule}
    if core != expected:
        raise EvaluationInputError("frozen evaluation protocol is not canonical")
    return {
        "artifact_version": PROTOCOL_VERSION,
        "kind": "twenty_evaluation_protocol_verification",
        "status": "READY",
        "protocol_id": normalized["protocol_id"],
        "protocol_digest": value["protocol_digest"],
        "scheduled_pairs": len(expected_schedule),
        "heldout_tasks": len(validate_seal(test_seal)["task_digests"]),
    }
