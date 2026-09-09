from __future__ import annotations

import copy
import unittest

from evals.twenty.evaluator import EvaluationInputError, digest_json
from evals.twenty.protocol import freeze_protocol, verify_protocol
from evals.twenty.taskset import seal_task_set, verify_split_separation
from evals.twenty.tests.test_taskset import task_set


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def artifacts() -> tuple[dict, dict, dict]:
    development = seal_task_set(task_set())
    test = seal_task_set(task_set("test", family_prefix="heldout"))
    split = verify_split_separation(development, test)
    return development, test, split


def protocol_draft() -> dict:
    development, test, split = artifacts()
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
    source_policy_digest = digest_json(source_policy)
    shared_skill = _digest("3")
    shared_tools = _digest("4")
    woa_implementation = _digest("5")
    arms = [
        {
            "arm_id": "pageagent-browser",
            "engine": "pageagent",
            "engine_release": "pageagent@1.12.4",
            "implementation_digest": _digest("6"),
            "capability_profile": "browser_only",
            "source_mode": "off",
            "source_interface": "absent",
            "source_policy_digest": None,
            "skill_digest": None,
            "tool_catalog_digest": None,
        },
        {
            "arm_id": "woa-browser",
            "engine": "web_operation_agent",
            "engine_release": "web-operation-agent@0.1.0",
            "implementation_digest": woa_implementation,
            "capability_profile": "browser_only",
            "source_mode": "off",
            "source_interface": "absent",
            "source_policy_digest": None,
            "skill_digest": None,
            "tool_catalog_digest": None,
        },
        {
            "arm_id": "pageagent-skill",
            "engine": "pageagent_core",
            "engine_release": "pageagent@1.12.4",
            "implementation_digest": _digest("7"),
            "capability_profile": "skill_api",
            "source_mode": "off",
            "source_interface": "absent",
            "source_policy_digest": None,
            "skill_digest": shared_skill,
            "tool_catalog_digest": shared_tools,
        },
        {
            "arm_id": "woa-skill",
            "engine": "web_operation_agent",
            "engine_release": "web-operation-agent@0.1.0",
            "implementation_digest": woa_implementation,
            "capability_profile": "skill_api",
            "source_mode": "off",
            "source_interface": "absent",
            "source_policy_digest": None,
            "skill_digest": shared_skill,
            "tool_catalog_digest": shared_tools,
        },
        {
            "arm_id": "woa-source-off",
            "engine": "web_operation_agent",
            "engine_release": "web-operation-agent@0.1.0",
            "implementation_digest": woa_implementation,
            "capability_profile": "skill_api",
            "source_mode": "off",
            "source_interface": "paired",
            "source_policy_digest": None,
            "skill_digest": shared_skill,
            "tool_catalog_digest": shared_tools,
        },
        {
            "arm_id": "woa-skill-source-on",
            "engine": "web_operation_agent",
            "engine_release": "web-operation-agent@0.1.0",
            "implementation_digest": woa_implementation,
            "capability_profile": "skill_api",
            "source_mode": "on",
            "source_interface": "paired",
            "source_policy_digest": source_policy_digest,
            "skill_digest": shared_skill,
            "tool_catalog_digest": shared_tools,
        },
    ]
    ids = sorted(test["task_digests"])
    comparison_tasks = {
        "h1-browser": [ids[0], ids[4], ids[8]],
        "h2-skill": ids,
        "h3-source": ids[4:8],
    }
    comparisons = [
        {
            "comparison_id": "h1-browser",
            "comparison_type": "browser_execution",
            "left_arm": "pageagent-browser",
            "right_arm": "woa-browser",
            "eligible_task_ids": comparison_tasks["h1-browser"],
            "minimum_useful_effect_pp": 10,
            "success_noninferiority_margin_pp": -5,
            "efficiency_improvement_percent": 25,
        },
        {
            "comparison_id": "h2-skill",
            "comparison_type": "skill_api",
            "left_arm": "pageagent-skill",
            "right_arm": "woa-skill",
            "eligible_task_ids": comparison_tasks["h2-skill"],
            "minimum_useful_effect_pp": 10,
            "success_noninferiority_margin_pp": -5,
            "efficiency_improvement_percent": 25,
        },
        {
            "comparison_id": "h3-source",
            "comparison_type": "source_runtime",
            "left_arm": "woa-source-off",
            "right_arm": "woa-skill-source-on",
            "eligible_task_ids": comparison_tasks["h3-source"],
            "minimum_useful_effect_pp": 10,
            "success_noninferiority_margin_pp": -5,
            "efficiency_improvement_percent": 25,
        },
    ]
    eligibility = []
    for task_id in ids:
        selected = sorted(
            comparison_id for comparison_id, selected_ids in comparison_tasks.items()
            if task_id in selected_ids)
        source_eligible = task_id in comparison_tasks["h3-source"]
        eligibility.append({
            "task_id": task_id,
            "comparisons": selected,
            "source_eligible": source_eligible,
            "source_reason": "hidden_static_route" if source_eligible else "runtime_or_visible_only",
        })
    return {
        "artifact_version": 1,
        "kind": "twenty_evaluation_protocol",
        "status": "DRAFT",
        "purpose": "apparatus_qualification",
        "protocol_id": "twenty-qualification-v1",
        "task_sets": {
            "development_seal": development["seal_digest"],
            "test_seal": test["seal_digest"],
            "split_verification": digest_json(split),
        },
        "environment": {
            "application_release": "twenty@2.39.0",
            "browser_release": "chromium@136.0.7103",
            "locale": "zh-CN",
            "timezone": "Asia/Shanghai",
            "viewport": {"width": 1440, "height": 900, "device_scale_factor": 1},
            "fixture_qualification_digest": _digest("8"),
            "reset_contract_digest": _digest("9"),
            "cache_policy": "cold_each_case",
        },
        "provider": {
            "provider_id": "alibaba-compatible",
            "endpoint_class": "openai_compatible_https",
            "requested_model": "qwen3.8-flash",
            "snapshot_policy": "record_reported_model_and_utc_window",
            "temperature": 0,
            "top_p": 1,
            "max_output_tokens": 4096,
            "context_window_tokens": 131072,
            "seed_mode": "unsupported",
            "seed": None,
            "request_schema_digest": _digest("a"),
            "response_schema_digest": _digest("b"),
            "pair_window_minutes": 30,
        },
        "budgets": {
            "max_model_calls": 12,
            "max_page_actions": 24,
            "max_business_calls": 12,
            "max_wall_time_s": 180,
            "input_token_abort_threshold": 120000,
            "max_completion_tokens_per_call": 4096,
            "automatic_retries": 0,
            "user_interventions": 0,
        },
        "artifacts": {
            "evaluator_digest": _digest("c"),
            "runner_digest": _digest("d"),
            "control_policy_digest": _digest("e"),
            "approval_policy_digest": _digest("f"),
        },
        "source_policy": source_policy,
        "arms": arms,
        "comparisons": comparisons,
        "eligibility": eligibility,
        "randomization_seed": "fedcba9876543210fedcba9876543210",
        "stop_policy": {
            "apparatus_invalid": "seal_batch",
            "safety_violation": "stop_batch",
            "reset_mismatch": "stop_batch",
            "spend_ledger_mismatch": "stop_batch",
            "provider_outage": "stop_batch",
            "rerun_valid_failure": False,
            "filter_invalid_primary_analysis": False,
        },
    }


class ProtocolQualificationTests(unittest.TestCase):
    def freeze(self, value: dict | None = None) -> tuple[dict, dict, dict, dict]:
        development, test, split = artifacts()
        frozen = freeze_protocol(
            value or protocol_draft(), development_seal=development,
            test_seal=test, split_verification=split)
        return frozen, development, test, split

    def test_protocol_freezes_a_deterministic_balanced_pair_schedule(self):
        frozen, development, test, split = self.freeze()
        result = verify_protocol(
            frozen, development_seal=development, test_seal=test,
            split_verification=split)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["scheduled_pairs"], 19)
        self.assertEqual([row["position"] for row in frozen["schedule"]], list(range(1, 20)))
        comparisons = {row["comparison_id"]: row for row in frozen["comparisons"]}
        for comparison_id, comparison in comparisons.items():
            first_counts = {
                arm: sum(row["first_arm"] == arm for row in frozen["schedule"]
                         if row["comparison_id"] == comparison_id)
                for arm in (comparison["left_arm"], comparison["right_arm"])
            }
            self.assertLessEqual(abs(first_counts[comparison["left_arm"]]
                                     - first_counts[comparison["right_arm"]]), 1)

    def test_protocol_digest_and_schedule_detect_tampering(self):
        frozen, development, test, split = self.freeze()
        changed = copy.deepcopy(frozen)
        changed["schedule"][0]["first_arm"] = "invented"
        core = {key: changed[key] for key in changed if key != "protocol_digest"}
        changed["protocol_digest"] = digest_json(core)
        with self.assertRaisesRegex(EvaluationInputError, "schedule"):
            verify_protocol(
                changed, development_seal=development, test_seal=test,
                split_verification=split)

    def test_source_ablation_rejects_any_difference_beyond_source_configuration(self):
        value = protocol_draft()
        source_on = next(arm for arm in value["arms"] if arm["source_mode"] == "on")
        source_on["implementation_digest"] = _digest("0")
        development, test, split = artifacts()
        with self.assertRaisesRegex(EvaluationInputError, "fairness"):
            freeze_protocol(
                value, development_seal=development, test_seal=test,
                split_verification=split)

    def test_h2_control_cannot_be_reused_as_the_h3_source_off_arm(self):
        value = protocol_draft()
        value["arms"] = [
            arm for arm in value["arms"] if arm["arm_id"] != "woa-source-off"]
        source = next(
            comparison for comparison in value["comparisons"]
            if comparison["comparison_type"] == "source_runtime")
        source["left_arm"] = "woa-skill"
        development, test, split = artifacts()
        with self.assertRaisesRegex(EvaluationInputError, "exactly six"):
            freeze_protocol(
                value, development_seal=development, test_seal=test,
                split_verification=split)

    def test_shared_skill_comparison_rejects_unequal_information(self):
        value = protocol_draft()
        pageagent = next(arm for arm in value["arms"] if arm["engine"] == "pageagent_core")
        pageagent["skill_digest"] = _digest("0")
        development, test, split = artifacts()
        with self.assertRaisesRegex(EvaluationInputError, "fairness"):
            freeze_protocol(
                value, development_seal=development, test_seal=test,
                split_verification=split)

    def test_source_eligibility_must_match_reason_and_comparison(self):
        value = protocol_draft()
        source_row = next(row for row in value["eligibility"] if row["source_eligible"])
        source_row["source_reason"] = "runtime_or_visible_only"
        development, test, split = artifacts()
        with self.assertRaisesRegex(EvaluationInputError, "source eligibility"):
            freeze_protocol(
                value, development_seal=development, test_seal=test,
                split_verification=split)

    def test_protocol_forbids_automatic_retries_and_non_finite_sampling(self):
        value = protocol_draft()
        value["budgets"]["automatic_retries"] = 1
        development, test, split = artifacts()
        with self.assertRaisesRegex(EvaluationInputError, "allowed range"):
            freeze_protocol(
                value, development_seal=development, test_seal=test,
                split_verification=split)

        value = protocol_draft()
        value["provider"]["temperature"] = float("nan")
        with self.assertRaisesRegex(EvaluationInputError, "allowed range"):
            freeze_protocol(
                value, development_seal=development, test_seal=test,
                split_verification=split)

    def test_protocol_is_bound_to_the_supplied_split_artifacts(self):
        value = protocol_draft()
        development, test, split = artifacts()
        changed_split = dict(split)
        changed_split["test_tasks"] += 1
        with self.assertRaisesRegex(EvaluationInputError, "split verification"):
            freeze_protocol(
                value, development_seal=development, test_seal=test,
                split_verification=changed_split)


if __name__ == "__main__":
    unittest.main()
