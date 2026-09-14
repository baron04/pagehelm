import copy
import dataclasses
import json
import unittest
from pathlib import Path

from evals.odoo import population, tasks

DEVELOPMENT = Path(__file__).resolve().parents[1] / "tasksets" / "p74-odoo-development-v1.json"
SEED = "0123456789abcdef0123456789abcdef"
OTHER_SEED = "fedcba9876543210fedcba9876543210"


def development_set():
    value = json.loads(DEVELOPMENT.read_text())
    pop = population.generate(SEED)
    value = copy.deepcopy(value)
    value["population_seed_commitment"] = pop["seed_commitment"]
    return value, pop


def synthetic_heldout(development, *, shared_family=False, drop_shape=False):
    """A structurally distinct twin of the development set, for separation checks only."""
    pop = population.generate(OTHER_SEED)
    heldout = copy.deepcopy(development)
    heldout.update(split="heldout", set_id="synthetic-heldout",
                   population_seed_commitment=pop["seed_commitment"],
                   commitment_nonce="a" * 32)
    for index, task in enumerate(heldout["tasks"], start=1):
        task["task_id"] = f"h{index:03d}"
        task["split"] = "heldout"
        if not (shared_family and index == 1):
            task["family"] = task["family"] + "-twin"
            task["fingerprint"]["conditions"] = sorted(
                task["fingerprint"]["conditions"] + ["twin"])
    if drop_shape:
        heldout["tasks"][0]["fingerprint"]["causal_shape"] = "some_other_shape"
    return heldout, pop


class TaskSetTests(unittest.TestCase):
    def test_development_set_is_valid_and_balanced(self):
        value, _ = development_set()
        validated = tasks.validate_task_set(value)
        classes = {task["class"] for task in validated["tasks"]}
        self.assertEqual(classes, {"normal", "refusal_or_limitation"})
        read_only_normal = [task for task in validated["tasks"] if task["class"] == "normal"
                            and task["max_mutations"] == 0 and task["stratum"] != "confirmed_write_readback"]
        self.assertGreaterEqual(len(read_only_normal), 20)

    def test_seal_detects_any_task_change(self):
        value, pop = development_set()
        seal = tasks.seal_task_set(value, population=pop)
        tasks.verify_seal(value, seal, population=pop)
        changed = copy.deepcopy(value)
        changed["tasks"][3]["messages"][0] += " "
        with self.assertRaises(tasks.TaskSetError):
            tasks.verify_seal(changed, seal, population=pop)
        with self.assertRaises(tasks.TaskSetError):
            tasks.seal_task_set(value, population=population.generate(OTHER_SEED))

    def test_family_fingerprint_and_count_rules(self):
        value, _ = development_set()
        duplicate = copy.deepcopy(value)
        duplicate["tasks"][1]["family"] = duplicate["tasks"][0]["family"]
        with self.assertRaises(tasks.TaskSetError):
            tasks.validate_task_set(duplicate)
        same_print = copy.deepcopy(value)
        same_print["tasks"][1]["fingerprint"] = copy.deepcopy(same_print["tasks"][0]["fingerprint"])
        same_print["tasks"][1]["evaluator"] = copy.deepcopy(same_print["tasks"][0]["evaluator"])
        with self.assertRaises(tasks.TaskSetError):
            tasks.validate_task_set(same_print)
        short = copy.deepcopy(value)
        short["tasks"].pop()
        with self.assertRaises(tasks.TaskSetError):
            tasks.validate_task_set(short)

    def test_capability_is_fixed_by_actor_not_by_task(self):
        value, _ = development_set()
        changed = copy.deepcopy(value)
        changed["tasks"][0]["capability_profile"] = tasks.ACTORS["reader"]
        with self.assertRaises(tasks.TaskSetError):
            tasks.validate_task_set(changed)

    def test_mutating_task_requires_reset(self):
        value, _ = development_set()
        changed = copy.deepcopy(value)
        writer = next(task for task in changed["tasks"] if task["max_mutations"] == 1)
        writer["reset_profile"] = "none"
        with self.assertRaises(tasks.TaskSetError):
            tasks.validate_task_set(changed)


class SplitTests(unittest.TestCase):
    def test_disjoint_families_with_matched_shapes_pass(self):
        development, dev_pop = development_set()
        heldout, ho_pop = synthetic_heldout(development)
        result = tasks.verify_split_separation(
            tasks.seal_task_set(development, population=dev_pop),
            tasks.seal_task_set(heldout, population=ho_pop))
        self.assertTrue(result["shape_distribution_matched"])

    def test_shared_family_or_mismatched_shapes_are_refused(self):
        development, dev_pop = development_set()
        dev_seal = tasks.seal_task_set(development, population=dev_pop)
        for kwargs in ({"shared_family": True}, {"drop_shape": True}):
            with self.subTest(kwargs=kwargs):
                heldout, ho_pop = synthetic_heldout(development, **kwargs)
                with self.assertRaises(tasks.TaskSetError):
                    tasks.verify_split_separation(
                        dev_seal, tasks.seal_task_set(heldout, population=ho_pop))

    def test_same_population_seed_is_refused(self):
        development, dev_pop = development_set()
        heldout, _ = synthetic_heldout(development)
        heldout["population_seed_commitment"] = development["population_seed_commitment"]
        with self.assertRaises(tasks.TaskSetError):
            tasks.verify_split_separation(
                tasks.seal_task_set(development, population=dev_pop),
                tasks.seal_task_set(heldout, population=dev_pop))


class InstantiationTests(unittest.TestCase):
    def test_runner_input_carries_no_evaluator_specification(self):
        value, pop = development_set()
        ids = {"population_seed_commitment": pop["seed_commitment"],
               "partners": {item["key"]: index + 10 for index, item in enumerate(pop["customers"])},
               "orders": {item["key"]: index + 1 for index, item in enumerate(pop["orders"])},
               "invoices": {}, "products": {}}
        for task in value["tasks"]:
            runner, evaluator = tasks.instantiate(value, task["task_id"], population=pop,
                                                  seed_ids=ids)
            fields = {item.name for item in dataclasses.fields(runner)}
            self.assertEqual(fields, {"task_id", "actor", "locale", "entry_path", "messages"})
            text = json.dumps(dataclasses.asdict(runner), ensure_ascii=False)
            self.assertNotIn("{{", text)
            self.assertNotIn(evaluator.shape, text)
        runner, _ = tasks.instantiate(value, "d05", population=pop, seed_ids=ids)
        key = pop["roles"]["customer.lookup_page"]
        self.assertEqual(runner.entry_path, f"/odoo/customers/{ids['partners'][key]}")

    def test_instantiation_refuses_foreign_seed_ids(self):
        value, pop = development_set()
        with self.assertRaises(tasks.TaskSetError):
            tasks.instantiate(value, "d01", population=pop,
                              seed_ids={"population_seed_commitment": "sha256:" + "0" * 64})


if __name__ == "__main__":
    unittest.main()
