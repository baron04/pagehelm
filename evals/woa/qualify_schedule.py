"""Model-free qualification of the common schedule driver for both engines.

The qualification instantiates a small frozen protocol over the public development task set,
runs the Web Operation Agent and official PageAgent runners through the one driver, evaluator,
projector and append-only ledger, and recomputes the public report. It uses scripted providers
and an isolated in-memory fixture, so it spends no model tokens and touches no real Twenty
instance or spend ledger.
"""
from __future__ import annotations

import copy
import json
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evals.pageagent.scripted import (
    SYNTHETIC_FIXTURE, ScriptedProviderClient, SyntheticCompanyExecutor, SyntheticTwentyAdapter,
    SyntheticTwentyPage, company_ui_path, evaluator_projection, query_answer,
    synthetic_business_runtime, synthetic_companies, write_storage_state,
)
from evals.twenty.evaluator import digest_json, make_state
from evals.twenty.fixture import utc_now
from evals.twenty.protocol import freeze_protocol
from evals.twenty.records import initialize_batch, verify_batch
from evals.twenty.report import build_report, verify_report
from evals.twenty.taskset import seal_task_set, verify_split_separation
from evals.woa.scripted import ScriptedWoaChat, SimulatedRuntime, SyntheticPageModel


QUALIFICATION_VERSION = 1
#: This seed controls only the model-free apparatus qualification. A formal benchmark seed
#: must be preregistered independently of this fixture and every observed result.
QUALIFICATION_SEED = "00000000000000000000000000000004"
DEVELOPMENT_TASKSET = (
    Path(__file__).resolve().parents[1] / "twenty" / "tasksets"
    / "twenty-companies-development-v1.json")


class InMemoryFixture:
    """Capture and restore one shared synthetic business projection without a database."""

    def __init__(self, companies: list[dict[str, str]]):
        self.companies = companies
        self.baseline = [dict(company) for company in companies]
        self.captures = 0

    def capture(self, *, role: str) -> dict[str, Any]:
        self.captures += 1
        projection = evaluator_projection(self.companies)
        return make_state(
            fixture=SYNTHETIC_FIXTURE, companies=projection["companies"],
            guards=projection["guards"], captured_at="2026-09-09T00:00:00+00:00",
            role=role)

    def restore(self) -> dict[str, Any]:
        self.companies[:] = [dict(company) for company in self.baseline]
        return {"status": "PASS"}


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def qualification_protocol(task_ids: Sequence[str], *, seal: Mapping[str, Any],
                           development_seal: Mapping[str, Any],
                           split: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze a protocol whose held-out seal is the supplied qualification seal."""
    source_policy = {
        "allowlist_digest": _digest("1"),
        "release_digest": _digest("2"),
        "excluded_classes": [
            "artifacts", "credentials", "database_and_fixtures", "evaluator",
            "runtime_and_user_data", "task_sets",
        ],
        "runtime_data_access": False,
        "test_artifact_access": False,
        "generated_from_heldout": False,
        "citation_required": True,
        "fail_closed_on_stale": True,
    }
    shared_skill, shared_tools = _digest("3"), _digest("4")
    woa_implementation = _digest("5")
    arms = [
        {"arm_id": "pageagent-browser", "engine": "pageagent",
         "engine_release": "pageagent@1.12.4", "implementation_digest": _digest("6"),
         "capability_profile": "browser_only", "source_mode": "off",
         "source_interface": "absent",
         "source_policy_digest": None, "skill_digest": None, "tool_catalog_digest": None},
        {"arm_id": "woa-browser", "engine": "web_operation_agent",
         "engine_release": "web-operation-agent@0.1.0",
         "implementation_digest": woa_implementation,
         "capability_profile": "browser_only", "source_mode": "off",
         "source_interface": "absent",
         "source_policy_digest": None, "skill_digest": None, "tool_catalog_digest": None},
        {"arm_id": "pageagent-skill", "engine": "pageagent_core",
         "engine_release": "pageagent@1.12.4", "implementation_digest": _digest("7"),
         "capability_profile": "skill_api", "source_mode": "off",
         "source_interface": "absent",
         "source_policy_digest": None, "skill_digest": shared_skill,
         "tool_catalog_digest": shared_tools},
        {"arm_id": "woa-skill", "engine": "web_operation_agent",
         "engine_release": "web-operation-agent@0.1.0",
         "implementation_digest": woa_implementation,
         "capability_profile": "skill_api", "source_mode": "off",
         "source_interface": "absent",
         "source_policy_digest": None, "skill_digest": shared_skill,
         "tool_catalog_digest": shared_tools},
        {"arm_id": "woa-source-off", "engine": "web_operation_agent",
         "engine_release": "web-operation-agent@0.1.0",
         "implementation_digest": woa_implementation,
         "capability_profile": "skill_api", "source_mode": "off",
         "source_interface": "paired",
         "source_policy_digest": None, "skill_digest": shared_skill,
         "tool_catalog_digest": shared_tools},
        {"arm_id": "woa-skill-source-on", "engine": "web_operation_agent",
         "engine_release": "web-operation-agent@0.1.0",
         "implementation_digest": woa_implementation,
         "capability_profile": "skill_api", "source_mode": "on",
         "source_interface": "paired",
         "source_policy_digest": digest_json(source_policy), "skill_digest": shared_skill,
         "tool_catalog_digest": shared_tools},
    ]
    selected = sorted(task_ids)
    read_only = selected[:8]
    comparisons = [
        {"comparison_id": "h1-browser", "comparison_type": "browser_execution",
         "left_arm": "pageagent-browser", "right_arm": "woa-browser",
         "eligible_task_ids": read_only, "minimum_useful_effect_pp": 10,
         "success_noninferiority_margin_pp": -5, "efficiency_improvement_percent": 25},
        {"comparison_id": "h2-skill", "comparison_type": "skill_api",
         "left_arm": "pageagent-skill", "right_arm": "woa-skill",
         "eligible_task_ids": selected, "minimum_useful_effect_pp": 10,
         "success_noninferiority_margin_pp": -5, "efficiency_improvement_percent": 25},
        {"comparison_id": "h3-source", "comparison_type": "source_runtime",
         "left_arm": "woa-source-off", "right_arm": "woa-skill-source-on",
         "eligible_task_ids": [read_only[4]], "minimum_useful_effect_pp": 10,
         "success_noninferiority_margin_pp": -5, "efficiency_improvement_percent": 25},
    ]
    eligibility = []
    for task_id in selected:
        rows = sorted(item["comparison_id"] for item in comparisons
                      if task_id in item["eligible_task_ids"])
        source_eligible = "h3-source" in rows
        eligibility.append({
            "task_id": task_id, "comparisons": rows, "source_eligible": source_eligible,
            "source_reason": ("hidden_static_route" if source_eligible
                              else "runtime_or_visible_only")})
    draft = {
        "artifact_version": 1,
        "kind": "twenty_evaluation_protocol",
        "status": "DRAFT",
        "purpose": "apparatus_qualification",
        "protocol_id": "twenty-driver-qualification-v1",
        "task_sets": {
            "development_seal": development_seal["seal_digest"],
            "test_seal": seal["seal_digest"],
            "split_verification": digest_json(split),
        },
        "environment": {
            "application_release": seal["application_release"],
            "browser_release": "chromium@136.0.7103",
            "locale": "en-US", "timezone": "Asia/Shanghai",
            "viewport": {"width": 1440, "height": 1000, "device_scale_factor": 1},
            "fixture_qualification_digest": _digest("8"),
            "reset_contract_digest": _digest("9"),
            "cache_policy": "cold_each_case",
        },
        "provider": {
            "provider_id": "scripted-local", "endpoint_class": "openai_compatible_https",
            "requested_model": "pageagent-scripted-model",
            "snapshot_policy": "record_reported_model_and_utc_window",
            "temperature": 0, "top_p": 1, "max_output_tokens": 4096,
            "context_window_tokens": 131072, "seed_mode": "unsupported", "seed": None,
            "request_schema_digest": _digest("a"), "response_schema_digest": _digest("b"),
            "pair_window_minutes": 30,
        },
        "budgets": {
            "max_model_calls": 12, "max_page_actions": 24, "max_business_calls": 12,
            "max_wall_time_s": 300, "input_token_abort_threshold": 120000,
            "max_completion_tokens_per_call": 4096, "automatic_retries": 0,
            "user_interventions": 0,
        },
        "artifacts": {
            "evaluator_digest": _digest("c"), "runner_digest": _digest("d"),
            "control_policy_digest": _digest("e"), "approval_policy_digest": _digest("f"),
        },
        "source_policy": source_policy,
        "arms": arms,
        "comparisons": comparisons,
        "eligibility": eligibility,
        "randomization_seed": QUALIFICATION_SEED,
        "stop_policy": {
            "apparatus_invalid": "seal_batch", "safety_violation": "stop_batch",
            "reset_mismatch": "stop_batch", "spend_ledger_mismatch": "stop_batch",
            "provider_outage": "stop_batch", "rerun_valid_failure": False,
            "filter_invalid_primary_analysis": False,
        },
    }
    return draft


def opaque_task_set(source: Mapping[str, Any], *, set_id: str) -> dict[str, Any]:
    """Re-identify the public development tasks as an opaque held-out-shaped set.

    The frozen protocol validator requires opaque ``tNNN`` identifiers and at least four
    tasks of each kind. The qualification therefore renames and repeats the public
    development prompts; it reads no private held-out content.
    """
    tasks = []
    sequence = 1
    for kind in ("query", "open", "create"):
        originals = [task for task in source["tasks"] if task["kind"] == kind]
        for index in range(4):
            original = copy.deepcopy(originals[index % len(originals)])
            original["task_id"] = f"t{sequence:03d}"
            original["template_family"] = f"qualification-{kind}-{index + 1}"
            if kind == "create":
                original["parameters"] = {
                    **original["parameters"],
                    "name": f"Qualification Company {sequence:02d}",
                    "domain": f"qualification-{sequence:02d}.example.com",
                }
            tasks.append(original)
            sequence += 1
    return {
        "artifact_version": 1,
        "kind": "twenty_task_set",
        "set_id": set_id,
        "split": "test",
        "application_release": source["application_release"],
        "commitment_nonce": "1234567890abcdef1234567890abcdef",
        "tasks": tasks,
    }
