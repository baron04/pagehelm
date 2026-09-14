import copy
import json
import tempfile
import unittest
from pathlib import Path

from evals.odoo import ledger, report, stats
from evals.odoo.outcomes import ApparatusEvent, Evaluation, classify
from evals.odoo.tests.test_ledger import CLEAN_AGENT, DIGEST, metrics

OTHER = "sha256:" + "c" * 64
WOA = "woa_product"
PAGEAGENT = "pageagent_browser_only"

PASS = classify(product_started=True, dispatch_state="none", runner_terminal="completed",
                evaluation=Evaluation("PASS", decisive=True))
FAIL = classify(product_started=True, dispatch_state="none", runner_terminal="completed",
                evaluation=Evaluation("FAIL", decisive=True))
OUTAGE = classify(product_started=True, dispatch_state="none", runner_terminal=None,
                  apparatus_events=[ApparatusEvent("provider_http_5xx",
                                                   "gateway_response_record")])
UNSAFE = classify(product_started=True, dispatch_state="acknowledged",
                  runner_terminal="completed",
                  evaluation=Evaluation("FAIL", decisive=True,
                                        safety_events=("duplicate_write",)))


def slot(arm, task_id, replicate="primary"):
    prefix = "w" if arm == WOA else "p"
    return {"slot_id": f"{prefix}-{task_id}-{replicate}", "arm": arm, "task_id": task_id,
            "replicate": replicate}


def normal(read_only=True):
    return {"class": "normal", "stratum": "sales", "read_only": read_only}


REFUSAL = {"class": "refusal_or_limitation", "stratum": "policy", "read_only": True}


class Batch:
    """A real attempt ledger in a temporary directory."""

    def __init__(self, test, slot_list, *, batch_id="batch-001", previous=None,
                 protocol=DIGEST):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "attempts.jsonl"
        self.slots = {item["slot_id"]: item for item in slot_list}
        self.counter = 0
        ledger.create(self.path, {
            "batch_id": batch_id, "protocol_id": "p74-odoo-v1", "protocol_digest": protocol,
            "schedule_digest": ledger.schedule_digest(slot_list), "purpose": "development",
            "provider_config_digest": DIGEST, "slots": slot_list, "previous_batch": previous})

    def run(self, slot_id, classification, *, reset="NOT_REQUIRED", replacement_of=None,
            wall_ms=1200, approval_ms=0, calls=()):
        """Record one attempt; ``calls`` lists per-call costs, None meaning usage unknown."""
        self.counter += 1
        attempt_id = f"attempt-{self.counter:03d}"
        item = self.slots[slot_id]
        ledger.append(self.path, "attempt_started", {
            "attempt_id": attempt_id, "slot_id": slot_id, "arm": item["arm"],
            "task_id": item["task_id"], "replicate": item["replicate"],
            "run_id": f"run-{self.counter:03d}", "fixture_digest": DIGEST,
            "capability_digest": DIGEST, "provider_config_digest": DIGEST,
            "replacement_of": replacement_of})
        for seq, cost in enumerate(calls, start=1):
            ledger.append(self.path, "model_request_dispatched", {
                "attempt_id": attempt_id, "request_seq": seq, "body_digest": DIGEST,
                "requested_model": "qwen3.8-flash", "max_output_tokens": 2048})
            ledger.append(self.path, "model_response_received", {
                "attempt_id": attempt_id, "request_seq": seq, "status": "ok",
                "http_status": 200, "provider_request_id": f"req-{seq}",
                "response_model": "scripted-model",
                "usage": None if cost is None else {
                    "input_tokens": 10, "output_tokens": 2, "cached_tokens": 0,
                    "reasoning_tokens": 0},
                "cost_cny": cost, "error_code": None})
        known = [cost for cost in calls if cost is not None]
        unavailable = len(calls) - len(known)
        ledger.append(self.path, "attempt_outcome", {
            "attempt_id": attempt_id, **classification.as_record(),
            "evaluation_digest": None, "truth_digest": None})
        ledger.append(self.path, "reset_outcome", {
            "attempt_id": attempt_id, "outcome": reset, "basis": [], "guard_digest": None,
            "agent": CLEAN_AGENT if item["arm"] == WOA else None})
        ledger.append(self.path, "attempt_cost", {
            "attempt_id": attempt_id, "model_calls": len(calls),
            "usage_unavailable_calls": unavailable, "input_tokens": 10 * len(known),
            "output_tokens": 2 * len(known), "cached_tokens": 0, "reasoning_tokens": 0,
            "known_cost_cny": round(sum(known), 6),
            "total_cost_cny": None if unavailable else round(sum(known), 6)})
        ledger.append(self.path, "attempt_metrics",
                      {**metrics(attempt_id, wall_ms), "approval_wait_ms": approval_ms})
        ledger.append(self.path, "attempt_terminal", {"attempt_id": attempt_id})
        return attempt_id

    def complete(self):
        ledger.append(self.path, "batch_completed", {})
        return self.summary()

    def seal(self, reason):
        ledger.append(self.path, "batch_sealed", {"reason": reason})
        return self.summary()

    def summary(self):
        return ledger.read(self.path)[1]


def cell(value, arm=WOA, replicate="primary", batch=0):
    return value["batches"][batch]["arms"][arm]["replicates"][replicate]


def normal_blocks(value):
    stack, found = [value], []
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "normal" in item and isinstance(item["normal"], dict):
                found.append(item["normal"])
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


class GateTests(unittest.TestCase):
    def test_primary_passes_and_replicate_fails_without_pooling(self):
        normal_tasks = ("task-01", "task-02", "task-03")
        slot_list = [slot(WOA, task, replicate) for replicate in ("primary", "replicate")
                     for task in (*normal_tasks, "ref-01")]
        batch = Batch(self, slot_list)
        for task in (*normal_tasks, "ref-01"):
            batch.run(f"w-{task}-primary", PASS)
        for task in (*normal_tasks, "ref-01"):
            batch.run(f"w-{task}-replicate", FAIL if task == "task-03" else PASS)
        tasks = {**{task: normal() for task in normal_tasks}, "ref-01": REFUSAL}
        value = report.build_report(batches=[batch.complete()], tasks=tasks)

        entry = value["batches"][0]
        self.assertEqual((entry["status"], entry["verdict_status"], entry["superseded"]),
                         ("COMPLETE", "gated", False))
        primary, replicate = cell(value)["normal"], cell(value, replicate="replicate")["normal"]
        self.assertEqual({**primary}, {**stats.proportion_summary(3, 3), "threshold": 3,
                                       "gate_verdict": "PASS", "sensitivity_credits": 3,
                                       "sensitivity_gate_verdict": "PASS", "robust": True})
        self.assertEqual((replicate["successes"], replicate["n"], replicate["gate_verdict"]),
                         (2, 3, "FAIL"))
        self.assertTrue(replicate["robust"])
        self.assertNotIn(6, [block["n"] for block in normal_blocks(value)])
        for name in ("primary", "replicate"):
            refusal = cell(value, replicate=name)["refusal_or_limitation"]
            self.assertEqual((refusal["successes"], refusal["n"]), (1, 1))
            self.assertNotIn("gate_verdict", refusal)
        self.assertEqual(cell(value, replicate="replicate")["axes"]["product_outcome"]["FAIL"], 1)
        transitions = entry["arms"][WOA]["primary_vs_replicate"]
        self.assertEqual((transitions["pass_pass"], transitions["pass_fail"], transitions["n"]),
                         (2, 1, 3))
        self.assertNotIn("primary_vs_replicate", entry["arms"][PAGEAGENT])
        comparison_arm = cell(value, arm=PAGEAGENT)["normal"]
        self.assertEqual((comparison_arm["n"], comparison_arm["rate"],
                          comparison_arm["gate_verdict"]), (0, None, None))
        self.assertEqual(value["safety"]["events"], 0)
        self.assertFalse(value["safety"]["safety_gate_failed"])
        self.assertAlmostEqual(value["safety"]["upper_bound"],
                               stats.zero_event_upper_bound(8), places=5)
        json.loads(json.dumps(value))
        self.assertEqual(report.verify_report(value)["status"], "READY")

    def test_replacement_that_decides_the_verdict_is_not_robust(self):
        batch = Batch(self, [slot(WOA, "task-01"), slot(WOA, "task-02")])
        batch.run("w-task-01-primary", PASS, calls=(0.01,))
        original = batch.run("w-task-02-primary", OUTAGE, calls=(0.02,))
        batch.run("w-task-02-primary", PASS, replacement_of=original, calls=(0.03,))
        value = report.build_report(batches=[batch.complete()],
                                    tasks={"task-01": normal(), "task-02": normal()})

        block = cell(value)
        self.assertEqual((block["normal"]["successes"], block["normal"]["n"],
                          block["normal"]["threshold"], block["normal"]["gate_verdict"]),
                         (2, 2, 2, "PASS"))
        self.assertEqual((block["normal"]["sensitivity_credits"],
                          block["normal"]["sensitivity_gate_verdict"],
                          block["normal"]["robust"]), (1, "FAIL", False))
        self.assertEqual((block["attempts"], block["replacements"]), (3, 1))
        self.assertEqual(block["apparatus_codes"]["provider_http_5xx"], 1)
        self.assertEqual(block["axes"]["product_outcome"]["PASS"], 2)
        self.assertEqual(block["axes"]["product_outcome"]["NOT_EVALUABLE"], 0)
        self.assertEqual((block["cost"]["model_calls"], block["cost"]["input_tokens"],
                          block["cost"]["known_cost_cny"], block["cost"]["total_cost_cny"]),
                         (3, 30, 0.06, 0.06))

    def test_sealed_batch_is_descriptive_but_its_safety_event_fails_the_gate(self):
        slot_list = [slot(WOA, "task-01"), slot(WOA, "task-02")]
        first = Batch(self, slot_list)
        first.run("w-task-01-primary", UNSAFE, reset="PASS")
        sealed = first.seal("critical_safety_event")
        second = Batch(self, slot_list, batch_id="batch-002", protocol=OTHER, previous={
            "batch_id": "batch-001", "protocol_digest": DIGEST,
            "chain_head": sealed["chain_head"], "termination_reason": "critical_safety_event"})
        second.run("w-task-01-primary", PASS)
        second.run("w-task-02-primary", PASS)
        value = report.build_report(batches=[sealed, second.complete()],
                                    tasks={"task-01": normal(), "task-02": normal()})

        old, new = value["batches"]
        self.assertEqual((old["status"], old["seal_reason"], old["superseded"],
                          old["verdict_status"]),
                         ("SEALED", "critical_safety_event", True, "descriptive_only"))
        self.assertEqual((new["superseded"], new["verdict_status"]), (False, "gated"))
        old_normal = cell(value)["normal"]
        self.assertEqual((old_normal["successes"], old_normal["n"], old_normal["threshold"],
                          old_normal["gate_verdict"], old_normal["robust"]),
                         (0, 2, 2, None, None))
        self.assertEqual(cell(value)["axes"]["product_outcome"]["MISSING"], 1)
        self.assertEqual(cell(value)["latency"]["reason"], "descriptive_only")
        self.assertEqual(cell(value, batch=1)["normal"]["gate_verdict"], "PASS")
        safety = value["safety"]
        self.assertEqual((safety["events"], safety["attempts"], safety["by_event"]
                          ["duplicate_write"], old["safety"]["events"]), (1, 3, 1, 1))
        self.assertTrue(safety["safety_gate_failed"])
        exact = stats.one_sided_upper_bound(1, 3)
        self.assertGreaterEqual(safety["upper_bound"], exact)
        self.assertAlmostEqual(safety["upper_bound"], exact, places=5)
        self.assertTrue(report.verify_report(value)["safety_gate_failed"])


class CostAndLatencyTests(unittest.TestCase):
    def test_unknown_usage_makes_total_cost_null(self):
        batch = Batch(self, [slot(WOA, "task-01")])
        batch.run("w-task-01-primary", PASS, calls=(0.5, None))
        value = report.build_report(batches=[batch.complete()], tasks={"task-01": normal()})
        cost = cell(value)["cost"]
        self.assertEqual((cost["model_calls"], cost["usage_unavailable_calls"],
                          cost["known_cost_cny"]), (2, 1, 0.5))
        self.assertIsNone(cost["total_cost_cny"])
        self.assertEqual(cell(value, replicate="replicate")["cost"]["total_cost_cny"], 0.0)

    def test_small_latency_sample_reports_maximum_without_a_gate(self):
        order = ("task-01", "task-04", "task-02", "task-03", "ref-01")
        batch = Batch(self, [slot(WOA, task) for task in order])
        batch.run("w-task-01-primary", PASS, wall_ms=1000)
        batch.run("w-task-04-primary", OUTAGE, wall_ms=77000)
        batch.run("w-task-02-primary", PASS, wall_ms=2500)
        batch.run("w-task-03-primary", PASS, wall_ms=99000, approval_ms=7000)
        batch.run("w-ref-01-primary", PASS, wall_ms=88000)
        tasks = {"task-01": normal(), "task-02": normal(), "task-03": normal(read_only=False),
                 "task-04": normal(), "ref-01": REFUSAL}
        value = report.build_report(batches=[batch.complete()], tasks=tasks)
        latency = cell(value)["latency"]
        self.assertEqual((latency["n"], latency["p50"], latency["p95"], latency["maximum"]),
                         (2, None, None, 2500))
        self.assertIsNone(latency["experience_gate_passed"])
        self.assertEqual(latency["reason"], "insufficient_sample")
        wait = cell(value)["approval_wait"]
        self.assertEqual((wait["n"], wait["with_wait"], wait["total"], wait["maximum"]),
                         (4, 1, 7000, 7000))
        self.assertEqual(cell(value, replicate="replicate")["latency"]["reason"], "not_gated")

    def test_twenty_samples_use_the_nearest_rank_p95(self):
        task_ids = [f"task-{index:02d}" for index in range(1, 21)]
        batch = Batch(self, [slot(WOA, task) for task in task_ids])
        walls = [1000 * index for index in range(1, 20)] + [99000]
        for task, wall in zip(task_ids, walls):
            batch.run(f"w-{task}-primary", PASS, wall_ms=wall)
        value = report.build_report(batches=[batch.complete()],
                                    tasks={task: normal() for task in task_ids})
        latency = cell(value)["latency"]
        self.assertEqual((latency["n"], latency["p50"], latency["p95"], latency["maximum"]),
                         (20, 10000, 19000, 99000))
        self.assertEqual(latency["p95"], stats.nearest_rank_percentile(walls, 95))
        self.assertIs(latency["experience_gate_passed"], True)
        self.assertIsNone(latency["reason"])


class ComparisonTests(unittest.TestCase):
    def test_all_concordant_pairs_keep_a_nondegenerate_interval(self):
        slot_list = [item for task in ("task-01", "task-02", "task-03")
                     for item in (slot(WOA, task), slot(PAGEAGENT, task))]
        slot_list.append(slot(WOA, "task-04"))
        batch = Batch(self, slot_list)
        for item in slot_list:
            batch.run(item["slot_id"], PASS)
        tasks = {task: normal() for task in ("task-01", "task-02", "task-03", "task-04")}
        value = report.build_report(batches=[batch.complete()], tasks=tasks,
                                    paired_task_ids=("task-01", "task-02", "task-03",
                                                     "task-04"))
        self.assertEqual(len(value["comparisons"]), 1)
        comparison = value["comparisons"][0]
        self.assertEqual((comparison["replicate"], comparison["n_pairs"],
                          comparison["unpaired_tasks"]), ("primary", 3, 1))
        self.assertEqual((comparison["a"], comparison["b"], comparison["c"], comparison["d"]),
                         (3, 0, 0, 0))
        _, low, high = stats.newcombe_paired_difference_interval(3, 0, 0, 0)
        self.assertEqual(comparison["difference"], 0.0)
        self.assertLess(comparison["newcombe_low"], 0.0)
        self.assertGreater(comparison["newcombe_high"], 0.0)
        self.assertAlmostEqual(comparison["newcombe_low"], low, places=5)
        self.assertAlmostEqual(comparison["newcombe_high"], high, places=5)
        self.assertEqual(comparison["exact_mcnemar_p"], 1.0)
        self.assertEqual((comparison["interpretation"], comparison["causal_engine_attribution"],
                          comparison["non_inferiority_claim"]),
                         ("exploratory_product_combination_difference", False, False))
        self.assertIsNone(cell(value, arm=PAGEAGENT)["normal"]["gate_verdict"])


class VerificationTests(unittest.TestCase):
    def setUp(self):
        batch = Batch(self, [slot(WOA, "task-01")])
        batch.run("w-task-01-primary", PASS)
        self.value = report.build_report(batches=[batch.complete()],
                                         tasks={"task-01": normal()})

    def test_generated_report_verifies(self):
        self.assertEqual(report.verify_report(self.value)["status"], "READY")

    def test_true_claim_is_rejected(self):
        tampered = copy.deepcopy(self.value)
        tampered["claims"]["engine_attribution"] = True
        with self.assertRaises(ValueError):
            report.verify_report(tampered)

    def test_forbidden_key_at_any_depth_is_rejected(self):
        tampered = copy.deepcopy(self.value)
        tampered["batches"][0]["arms"][WOA]["replicates"]["primary"]["notes"] = [
            {"prompt": "summarise the quote"}]
        with self.assertRaises(ValueError):
            report.verify_report(tampered)

    def test_wrong_kind_is_rejected(self):
        tampered = {**copy.deepcopy(self.value), "kind": "twenty_public_report"}
        with self.assertRaises(ValueError):
            report.verify_report(tampered)

    def test_missing_task_metadata_is_rejected(self):
        batch = Batch(self, [slot(WOA, "task-01"), slot(WOA, "task-02")])
        with self.assertRaises(ValueError):
            report.build_report(batches=[batch.summary()], tasks={"task-01": normal()})


if __name__ == "__main__":
    unittest.main()
