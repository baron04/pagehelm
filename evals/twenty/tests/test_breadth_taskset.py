from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from evals.twenty.breadth_taskset import (instantiate_breadth_task,
                                          validate_breadth_execution_case,
                                          validate_breadth_task_set)
from evals.twenty.tests.test_breadth import COMPANY_ID, OPPORTUNITY_ID, PERSON_ID, states
from evals.twenty.evaluator import EvaluationInputError


TASKSET = Path(__file__).resolve().parents[1] / "tasksets/twenty-breadth-development-v2.json"


def load_taskset():
    return json.loads(TASKSET.read_text(encoding="utf-8"))


class BreadthTaskSetTests(unittest.TestCase):
    def test_public_development_set_has_six_kinds_three_entities_and_thirty_templates(self):
        task_set = validate_breadth_task_set(load_taskset())
        self.assertEqual(len(task_set["tasks"]), 30)
        self.assertEqual(len({task["template_family"] for task in task_set["tasks"]}), 30)
        self.assertEqual({task["kind"] for task in task_set["tasks"]}, {
            "enumerate", "filter", "open_record", "open_collection",
            "create_company", "refuse"})
        for entity in ("companies", "people", "opportunities"):
            kinds = {task["kind"] for task in task_set["tasks"] if task["entity"] == entity}
            self.assertTrue({"enumerate", "filter", "open_record", "open_collection"} <= kinds)

    def test_baseline_binding_produces_evaluator_tasks_without_placeholders(self):
        task_set = load_taskset()
        baseline, _ = states()
        examples = {
            "dev-filter-people-en": ("filter", PERSON_ID),
            "dev-open-opportunities-en": ("open_record", OPPORTUNITY_ID),
            "dev-open-companies-en": ("open_record", COMPANY_ID),
        }
        # The fixture in this unit test has only one record of each kind, so use index zero.
        for task in task_set["tasks"]:
            if task["task_id"] in examples:
                task["parameters"]["baseline_index"] = 0
        for task_id, (kind, record_id) in examples.items():
            case = instantiate_breadth_task(task_set, task_id, baseline)
            self.assertNotIn("{{", case["prompt"])
            self.assertEqual(case["evaluator_task"]["kind"], kind)
            if kind == "open_record":
                self.assertEqual(case["evaluator_task"]["record_id"], record_id)

        create = instantiate_breadth_task(task_set, "dev-create-company-en-a", baseline)
        self.assertEqual(create["evaluator_task"]["artifact_version"], 1)
        refusal = instantiate_breadth_task(
            task_set, "dev-refuse-person-delete-en", baseline)
        self.assertEqual(refusal["evaluator_task"]["operation"], "delete")
        self.assertEqual(validate_breadth_execution_case(refusal)["task_kind"], "refuse")
        self.assertEqual(validate_breadth_execution_case(create)["task_kind"], "create")

    def test_small_unbalanced_and_invalid_sets_fail_before_a_run(self):
        value = load_taskset()
        value["tasks"] = value["tasks"][:-1]
        with self.assertRaisesRegex(EvaluationInputError, "30 to 1000"):
            validate_breadth_task_set(value)

        value = load_taskset()
        for task in value["tasks"]:
            if task["kind"] == "refuse":
                task["kind"] = "enumerate"
                task["parameters"] = {}
        with self.assertRaisesRegex(EvaluationInputError, "four tasks"):
            validate_breadth_task_set(value)

        value = copy.deepcopy(load_taskset())
        value["tasks"][0]["prompt_template"] = "List {{record.secret}}"
        with self.assertRaisesRegex(EvaluationInputError, "cannot have parameters"):
            validate_breadth_task_set(value)


if __name__ == "__main__":
    unittest.main()
