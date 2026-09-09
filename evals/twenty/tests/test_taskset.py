from __future__ import annotations

import copy
import json
import unittest

from evals.twenty.evaluator import EvaluationInputError, digest_json, make_state
from evals.twenty.taskset import (instantiate_task, seal_task_set,
                                  verify_split_separation, verify_task_set)


COMPANY_ID = "7e172546-a20e-4c7a-861c-a6478a944d37"
FIXTURE = {
    "container": "twenty-test-db-1",
    "database": "default",
    "db_user": "postgres",
    "workspace_schema": "workspace_test",
}


def task_set(split: str = "development", *, family_prefix: str = "dev") -> dict:
    minimum = 2 if split == "development" else 4
    tasks = []
    sequence = 1
    for index in range(minimum):
        task_id = f"t{sequence:03d}" if split == "test" else f"d-query-{index + 1}"
        sequence += 1
        tasks.append({
            "task_id": task_id,
            "template_family": f"{family_prefix}-query-{index + 1}",
            "kind": "query",
            "prompt_template": f"List every company name and domain, wording {index + 1}.",
            "parameters": {},
        })
    for index in range(minimum):
        task_id = f"t{sequence:03d}" if split == "test" else f"d-open-{index + 1}"
        sequence += 1
        tasks.append({
            "task_id": task_id,
            "template_family": f"{family_prefix}-open-{index + 1}",
            "kind": "open",
            "prompt_template": "Open {{company.name}} at {{company.domain}}.",
            "parameters": {"baseline_index": 0},
        })
    for index in range(minimum):
        task_id = f"t{sequence:03d}" if split == "test" else f"d-create-{index + 1}"
        sequence += 1
        tasks.append({
            "task_id": task_id,
            "template_family": f"{family_prefix}-create-{index + 1}",
            "kind": "create",
            "prompt_template": "Create {{company.name}} with domain {{company.domain}}.",
            "parameters": {
                "name": f"Synthetic Company {family_prefix} {index + 1}",
                "domain": f"{family_prefix}-{index + 1}.example.com",
                "require_open": bool(index % 2),
            },
        })
    return {
        "artifact_version": 1,
        "kind": "twenty_task_set",
        "set_id": f"twenty-companies-{split}-v1",
        "split": split,
        "application_release": "twenty@2.39.0",
        "commitment_nonce": "0123456789abcdef0123456789abcdef",
        "tasks": tasks,
    }


def baseline() -> dict:
    company = {
        "id": COMPANY_ID,
        "name": "Synthetic Acme",
        "domain": "acme.example.com",
        "created_at": "2026-09-08T01:00:00+00:00",
        "updated_at": "2026-09-08T01:00:00+00:00",
        "deleted_at": None,
    }
    guards = {
        "people": {"row_count": 0, "digest": digest_json([])},
        "opportunities": {"row_count": 0, "digest": digest_json([])},
    }
    return make_state(
        fixture=FIXTURE, companies=[company], guards=guards,
        captured_at="2026-09-08T00:00:00+00:00", role="baseline")


class TaskSetQualificationTests(unittest.TestCase):
    def test_seal_contains_only_counts_and_digests(self):
        value = task_set()
        seal = seal_task_set(value)
        encoded = json.dumps(seal)

        self.assertEqual(seal["status"], "READY")
        self.assertEqual(seal["task_count"], 6)
        self.assertEqual(seal["task_kind_counts"], {
            "create": 2, "open": 2, "query": 2})
        self.assertNotIn("List every company", encoded)
        self.assertNotIn("Synthetic Company", encoded)
        self.assertNotIn(value["commitment_nonce"], encoded)
        self.assertEqual(verify_task_set(value, seal)["status"], "READY")

    def test_changed_prompt_or_parameter_breaks_the_frozen_seal(self):
        value = task_set()
        seal = seal_task_set(value)
        changed = copy.deepcopy(value)
        changed["tasks"][0]["prompt_template"] += " changed"
        with self.assertRaisesRegex(EvaluationInputError, "frozen seal"):
            verify_task_set(changed, seal)

    def test_split_check_rejects_shared_template_families(self):
        development = seal_task_set(task_set(family_prefix="shared"))
        test = seal_task_set(task_set("test", family_prefix="shared"))
        with self.assertRaisesRegex(EvaluationInputError, "template families overlap"):
            verify_split_separation(development, test)

        separated = seal_task_set(task_set("test", family_prefix="heldout"))
        result = verify_split_separation(development, separated)
        self.assertTrue(result["template_families_disjoint"])
        self.assertEqual(result["development_tasks"], 6)
        self.assertEqual(result["test_tasks"], 12)

    def test_open_task_is_bound_to_the_frozen_baseline_at_instantiation(self):
        case = instantiate_task(task_set(), "d-open-1", baseline())
        self.assertEqual(case["prompt"], "Open Synthetic Acme at acme.example.com.")
        self.assertEqual(case["evaluator_task"], {
            "artifact_version": 1,
            "kind": "open",
            "company_id": COMPANY_ID,
        })
        self.assertNotIn("template_family", case)
        self.assertIn("template_family_digest", case)

    def test_test_ids_are_opaque_and_every_kind_has_a_minimum(self):
        exposed = task_set("test")
        exposed["tasks"][0]["task_id"] = "test-query-company-list"
        with self.assertRaisesRegex(EvaluationInputError, "opaque"):
            seal_task_set(exposed)

        too_small = task_set("test")
        too_small["tasks"] = too_small["tasks"][:-1]
        with self.assertRaisesRegex(EvaluationInputError, "at least 4"):
            seal_task_set(too_small)

    def test_invalid_placeholder_and_duplicate_create_target_fail_before_sealing(self):
        value = task_set()
        value["tasks"][0]["prompt_template"] = "List {{company.name}}"
        with self.assertRaisesRegex(EvaluationInputError, "placeholder"):
            seal_task_set(value)

        value = task_set()
        creates = [task for task in value["tasks"] if task["kind"] == "create"]
        creates[1]["parameters"] = copy.deepcopy(creates[0]["parameters"])
        with self.assertRaisesRegex(EvaluationInputError, "targets must be unique"):
            seal_task_set(value)

        value = task_set()
        value["commitment_nonce"] = "predictable"
        with self.assertRaisesRegex(EvaluationInputError, "128-bit"):
            seal_task_set(value)

    def test_malformed_seal_counts_fail_as_evaluation_input(self):
        seal = seal_task_set(task_set())
        seal["task_kind_counts"]["query"] = "2"
        core = {key: seal[key] for key in seal if key != "seal_digest"}
        seal["seal_digest"] = digest_json(core)

        with self.assertRaisesRegex(EvaluationInputError, "kind count"):
            verify_split_separation(seal, seal_task_set(task_set(
                "test", family_prefix="heldout")))


if __name__ == "__main__":
    unittest.main()
