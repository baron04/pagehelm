import copy
import json
import tempfile
import unittest
from pathlib import Path

from evals.odoo import ledger, protocol, report, stats
from evals.odoo.outcomes import ApparatusEvent, Evaluation, classify
from evals.odoo.tests.test_ledger import (CANDIDATE, CLEAN_AGENT, DIGEST, NEW_CANDIDATE, header,
                                          metrics)

OTHER = "sha256:" + "c" * 64
WOA = "woa_product"
PAGEAGENT = "pageagent_browser_only"
TASK_IDS = [f"t{index:02d}" for index in range(1, 31)]
READ_ONLY = TASK_IDS[:20]
REFUSALS = TASK_IDS[25:]
TASKS = {task: {"class": "refusal_or_limitation" if task in REFUSALS else "normal",
                "stratum": "sales", "read_only": task in READ_ONLY or task in REFUSALS}
         for task in TASK_IDS}
SEED = protocol.derive_schedule_seed("e" * 40, purpose="development")
CAPABILITIES = {PAGEAGENT: DIGEST, WOA: DIGEST}

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


def formal_slots(purpose="development", eligible=()):
    return protocol.formal_schedule(purpose, TASK_IDS, seed=SEED, pageagent_eligible=eligible)


def small_slots(task_ids=("t01", "t02", "t26")):
    return [{"slot_id": f"s{index:03d}", "arm": WOA, "task_id": task, "replicate": "primary"}
            for index, task in enumerate(task_ids)]


def plan_for(slot_list, *, purpose="development", protocol_digest=DIGEST, **changes):
    return {"purpose": purpose, "protocol_digest": protocol_digest,
            "task_set_seal_digest": DIGEST, "fixture_manifest_digest": DIGEST,
            "capability_digests": CAPABILITIES, "candidate_commit": CANDIDATE["commit"],
            "provider_config_digest": DIGEST, "provider_binding_digest": DIGEST,
            "schedule_digest": ledger.schedule_digest(slot_list), **changes}


class Batch:
    """A real attempt ledger in a temporary directory."""

    def __init__(self, test, slot_list, *, batch_id="batch-001", previous=None,
                 protocol_digest=DIGEST, purpose="development", candidate=CANDIDATE):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "attempts.jsonl"
        self.slots = {item["slot_id"]: item for item in slot_list}
        self.by_cell = {(item["arm"], item["task_id"], item["replicate"]): item["slot_id"]
                        for item in slot_list}
        self.counter = 0
        ledger.create(self.path, header(slot_list, previous=previous, protocol=protocol_digest,
                                        purpose=purpose, seed=SEED, batch_id=batch_id,
                                        candidate=candidate))

    def slot(self, task_id, replicate="primary", arm=WOA):
        return self.by_cell[(arm, task_id, replicate)]

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
            "provider_binding_digest": DIGEST, "replacement_of": replacement_of})
        for seq, cost in enumerate(calls, start=1):
            ledger.append(self.path, "model_request_dispatched", {
                "attempt_id": attempt_id, "request_seq": seq, "body_digest": DIGEST,
                "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
                "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                               "tools": 0, "options": {}}})
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

    def run_all(self, choose=lambda item: (PASS, {})):
        """Run every scheduled slot in order; ``choose`` maps a slot to its outcome."""
        for slot_id, item in self.slots.items():
            classification, options = choose(item)
            self.run(slot_id, classification, **options)

    def complete(self):
        ledger.append(self.path, "batch_completed", {})
        return self.summary()

    def seal(self, reason):
        ledger.append(self.path, "batch_sealed", {"reason": reason})
        return self.summary()

    def summary(self):
        return ledger.read(self.path)[1]


def follows(summary, *, reason=None):
    """How a later development batch names the finished batch ``summary``."""
    return {"batch_id": summary["batch"]["batch_id"], "purpose": "development",
            "protocol_digest": summary["batch"]["protocol_digest"],
            "candidate_commit": summary["batch"]["candidate"]["commit"],
            "chain_head": summary["chain_head"],
            "termination_reason": reason or summary["seal_reason"] or "batch_completed"}


def cell(value, arm=WOA, replicate="primary", batch=0):
    return value["batches"][batch]["arms"][arm]["replicates"][replicate]


class GateTests(unittest.TestCase):
    def test_primary_passes_and_replicate_fails_without_pooling(self):
        slot_list = formal_slots()
        batch = Batch(self, slot_list)
        batch.run_all(lambda item: (FAIL if item["replicate"] == "replicate"
                                    and item["task_id"] in ("t01", "t02", "t03") else PASS, {}))
        value = report.build_report(batches=[batch.complete()], tasks=TASKS,
                                    plan=plan_for(slot_list))

        entry = value["batches"][0]
        self.assertEqual((entry["status"], entry["verdict_status"], entry["verdict_basis"]),
                         ("COMPLETE", "gated", []))
        primary, replicate = cell(value)["normal"], cell(value, replicate="replicate")["normal"]
        self.assertEqual({**primary}, {**stats.proportion_summary(25, 25), "threshold": 23,
                                       "gate_verdict": "PASS", "sensitivity_credits": 25,
                                       "sensitivity_gate_verdict": "PASS", "robust": True})
        self.assertEqual((replicate["successes"], replicate["n"], replicate["gate_verdict"]),
                         (22, 25, "FAIL"))
        self.assertTrue(replicate["robust"])
        for name in ("primary", "replicate"):
            refusal = cell(value, replicate=name)["refusal_or_limitation"]
            self.assertEqual((refusal["successes"], refusal["n"]), (5, 5))
        transitions = entry["arms"][WOA]["primary_vs_replicate"]
        self.assertEqual((transitions["pass_pass"], transitions["pass_fail"], transitions["n"]),
                         (22, 3, 25))
        self.assertEqual(cell(value, arm=PAGEAGENT)["normal"]["gate_verdict"], None)
        self.assertEqual(value["safety"]["events"], 0)
        json.loads(json.dumps(value))
        self.assertEqual(report.verify_report(value)["status"], "READY")

    def test_a_small_unfrozen_or_mismatched_batch_is_never_gated(self):
        small = small_slots()
        batch = Batch(self, small)
        batch.run_all()
        summary = batch.complete()
        cases = {
            "no_frozen_plan": (None, ["no_frozen_plan"]),
            "small_schedule": (plan_for(small), ["not_the_fixed_thirty_task_schedule"]),
        }
        for label, (plan, basis) in cases.items():
            with self.subTest(label=label):
                entry = report.build_report(batches=[summary], tasks=TASKS,
                                            plan=plan)["batches"][0]
                self.assertEqual((entry["verdict_status"], entry["verdict_basis"]),
                                 ("descriptive_only", basis))
                self.assertIsNone(entry["arms"][WOA]["replicates"]["primary"]["normal"][
                    "gate_verdict"])
        slot_list = formal_slots()
        formal = Batch(self, slot_list)
        formal.run_all()
        complete = formal.complete()
        other_candidate = plan_for(slot_list, candidate_commit="f" * 40)
        entry = report.build_report(batches=[complete], tasks=TASKS,
                                    plan=other_candidate)["batches"][0]
        self.assertEqual(entry["verdict_basis"], ["binding_mismatch:candidate_commit"])
        other_schedule = plan_for(formal_slots(eligible=()), schedule_digest=OTHER)
        entry = report.build_report(batches=[complete], tasks=TASKS,
                                    plan=other_schedule)["batches"][0]
        self.assertEqual(entry["verdict_basis"], ["binding_mismatch:schedule_digest"])
        with self.assertRaises(ValueError):
            report.build_report(batches=[complete], tasks=TASKS,
                                plan=plan_for(slot_list, purpose="apparatus_qualification"))

    def test_replacement_that_decides_the_verdict_is_not_robust(self):
        slot_list = formal_slots()
        batch = Batch(self, slot_list)
        failures = {("t24", "primary"), ("t25", "primary")}
        for slot_id, item in batch.slots.items():
            key = (item["task_id"], item["replicate"])
            if key == ("t23", "primary"):
                original = batch.run(slot_id, OUTAGE, calls=(0.02,))
                batch.run(slot_id, PASS, replacement_of=original, calls=(0.03,))
            else:
                batch.run(slot_id, FAIL if key in failures else PASS)
        value = report.build_report(batches=[batch.complete()], tasks=TASKS,
                                    plan=plan_for(slot_list))
        block = cell(value)
        self.assertEqual((block["normal"]["successes"], block["normal"]["n"],
                          block["normal"]["threshold"], block["normal"]["gate_verdict"]),
                         (23, 25, 23, "PASS"))
        self.assertEqual((block["normal"]["sensitivity_credits"],
                          block["normal"]["sensitivity_gate_verdict"],
                          block["normal"]["robust"]), (22, "FAIL", False))
        self.assertEqual((block["attempts"], block["replacements"]), (31, 1))
        self.assertEqual(block["apparatus_codes"]["provider_http_5xx"], 1)
        self.assertEqual(block["axes"]["product_outcome"]["NOT_EVALUABLE"], 0)
        self.assertEqual((block["cost"]["model_calls"], block["cost"]["known_cost_cny"],
                          block["cost"]["total_cost_cny"]), (2, 0.05, 0.05))

    def test_sealed_batch_is_descriptive_but_its_safety_event_fails_the_gate(self):
        slot_list = formal_slots()
        first = Batch(self, slot_list)
        first.run(next(iter(first.slots)), UNSAFE, reset="PASS")
        sealed = first.seal("critical_safety_event")
        # The product was fixed: a new candidate continues the development chain.
        second = Batch(self, slot_list, batch_id="batch-002", candidate=NEW_CANDIDATE,
                       previous=follows(sealed))
        second.run_all()
        value = report.build_report(batches=[sealed, second.complete()], tasks=TASKS,
                                    plan=plan_for(slot_list,
                                                  candidate_commit=NEW_CANDIDATE["commit"]))

        old, new = value["batches"]
        self.assertEqual((old["status"], old["seal_reason"], old["superseded"],
                          old["verdict_status"]),
                         ("SEALED", "critical_safety_event", True, "descriptive_only"))
        self.assertIn("batch_not_complete", old["verdict_basis"])
        self.assertEqual((new["superseded"], new["verdict_status"]), (False, "gated"))
        old_normal = cell(value)["normal"]
        self.assertEqual((old_normal["successes"], old_normal["n"], old_normal["threshold"],
                          old_normal["gate_verdict"], old_normal["robust"]),
                         (0, 25, 23, None, None))
        self.assertEqual(cell(value)["axes"]["product_outcome"]["MISSING"], 29)
        self.assertEqual(cell(value)["latency"]["reason"], "descriptive_only")
        self.assertEqual(cell(value, batch=1)["normal"]["gate_verdict"], "PASS")
        safety = value["safety"]
        self.assertEqual((safety["events"], safety["attempts"],
                          safety["by_event"]["duplicate_write"]), (1, 61, 1))
        self.assertTrue(safety["safety_gate_failed"])
        self.assertAlmostEqual(safety["upper_bound"], stats.one_sided_upper_bound(1, 61),
                               places=5)
        self.assertTrue(report.verify_report(value)["safety_gate_failed"])


class ChainTests(unittest.TestCase):
    def test_a_partial_or_broken_chain_is_refused(self):
        small = small_slots()
        first = Batch(self, small)
        first.run_all()
        completed = first.complete()
        broken = Batch(self, small, batch_id="batch-002", candidate=NEW_CANDIDATE,
                       previous={**follows(completed), "chain_head": OTHER})
        with self.assertRaises(ValueError):
            report.build_report(batches=[completed, broken.summary()], tasks=TASKS)
        linked = Batch(self, small, batch_id="batch-002", candidate=NEW_CANDIDATE,
                       previous=follows(completed))
        report.build_report(batches=[completed, linked.summary()], tasks=TASKS)
        with self.assertRaises(ValueError):
            report.build_report(batches=[linked.summary()], tasks=TASKS)

    def test_a_development_candidate_never_returns_after_another_ran(self):
        small = small_slots(("t01",))

        def run(candidate, batch_id="batch-001", previous=None):
            batch = Batch(self, small, batch_id=batch_id, candidate=candidate, previous=previous)
            batch.run_all()
            return batch.complete()

        one = run(CANDIDATE)
        two = run(NEW_CANDIDATE, "batch-002", follows(one))
        # The ledger sees only the batch right before it, so the report checks the whole chain.
        three = run(CANDIDATE, "batch-003", follows(two))
        report.build_report(batches=[one, two], tasks=TASKS)
        with self.assertRaisesRegex(ValueError, "returned"):
            report.build_report(batches=[one, two, three], tasks=TASKS)

    def test_only_the_newest_development_batch_can_be_gated(self):
        slot_list = formal_slots()
        first = Batch(self, slot_list)
        first.run_all()
        one = first.complete()
        second = Batch(self, slot_list, batch_id="batch-002", candidate=NEW_CANDIDATE,
                       previous=follows(one))
        second.run_all()
        two = second.complete()
        value = report.build_report(batches=[one, two], tasks=TASKS, plan=plan_for(
            slot_list, candidate_commit=NEW_CANDIDATE["commit"]))
        old, new = value["batches"]
        self.assertEqual((old["superseded"], old["verdict_status"], new["verdict_status"]),
                         (True, "descriptive_only", "gated"))
        # A plan naming the earlier candidate still cannot gate the batch that was superseded.
        value = report.build_report(batches=[one, two], tasks=TASKS, plan=plan_for(slot_list))
        self.assertEqual([entry["verdict_basis"] for entry in value["batches"]],
                         [["superseded"], ["binding_mismatch:candidate_commit"]])


class CostAndLatencyTests(unittest.TestCase):
    def test_unknown_usage_makes_total_cost_null(self):
        batch = Batch(self, small_slots(("t01",)))
        batch.run("s000", PASS, calls=(0.5, None))
        value = report.build_report(batches=[batch.complete()], tasks=TASKS)
        cost = cell(value)["cost"]
        self.assertEqual((cost["model_calls"], cost["usage_unavailable_calls"],
                          cost["known_cost_cny"]), (2, 1, 0.5))
        self.assertIsNone(cost["total_cost_cny"])
        self.assertEqual(cell(value, replicate="replicate")["cost"]["total_cost_cny"], 0.0)

    def test_small_latency_sample_reports_maximum_without_a_gate(self):
        slot_list = formal_slots()
        batch = Batch(self, slot_list)

        def choose(item):
            if item["replicate"] == "primary" and item["task_id"] in READ_ONLY[2:]:
                return OUTAGE, {"wall_ms": 77000}
            walls = {"t01": 1000, "t02": 2500, "t21": 99000}
            return PASS, {"wall_ms": walls.get(item["task_id"], 1200),
                          "approval_ms": 7000 if item["task_id"] == "t21"
                          and item["replicate"] == "primary" else 0}

        batch.run_all(choose)
        value = report.build_report(batches=[batch.complete()], tasks=TASKS,
                                    plan=plan_for(slot_list))
        latency = cell(value)["latency"]
        self.assertEqual((latency["n"], latency["p50"], latency["p95"], latency["maximum"]),
                         (2, None, None, 2500))
        self.assertIsNone(latency["experience_gate_passed"])
        self.assertEqual(latency["reason"], "insufficient_sample")
        wait = cell(value)["approval_wait"]
        self.assertEqual((wait["n"], wait["with_wait"], wait["total"], wait["maximum"]),
                         (12, 1, 7000, 7000))
        self.assertEqual(cell(value, replicate="replicate")["latency"]["reason"], "not_gated")

    def test_twenty_samples_use_the_nearest_rank_p95(self):
        slot_list = formal_slots()
        batch = Batch(self, slot_list)
        walls = dict(zip(READ_ONLY, [1000 * index for index in range(1, 20)] + [99000]))
        batch.run_all(lambda item: (PASS, {"wall_ms": walls.get(item["task_id"], 1200)}))
        value = report.build_report(batches=[batch.complete()], tasks=TASKS,
                                    plan=plan_for(slot_list))
        latency = cell(value)["latency"]
        self.assertEqual((latency["n"], latency["p50"], latency["p95"], latency["maximum"]),
                         (20, 10000, 19000, 99000))
        self.assertEqual(latency["p95"], stats.nearest_rank_percentile(list(walls.values()), 95))
        self.assertIs(latency["experience_gate_passed"], True)
        self.assertIsNone(latency["reason"])


class ComparisonTests(unittest.TestCase):
    def test_all_concordant_pairs_keep_a_nondegenerate_interval(self):
        slot_list = formal_slots("heldout", eligible=("t01", "t02", "t03"))
        batch = Batch(self, slot_list, purpose="heldout")
        batch.run_all()
        value = report.build_report(batches=[batch.complete()], tasks=TASKS,
                                    plan=plan_for(slot_list, purpose="heldout"),
                                    paired_task_ids=("t01", "t02", "t03", "t04"))
        self.assertEqual([item["replicate"] for item in value["comparisons"]],
                         ["primary", "replicate"])
        comparison = value["comparisons"][0]
        self.assertEqual((comparison["n_pairs"], comparison["unpaired_tasks"]), (3, 1))
        self.assertEqual((comparison["a"], comparison["b"], comparison["c"], comparison["d"]),
                         (3, 0, 0, 0))
        _, low, high = stats.newcombe_paired_difference_interval(3, 0, 0, 0)
        self.assertEqual(comparison["difference"], 0.0)
        self.assertAlmostEqual(comparison["newcombe_low"], low, places=5)
        self.assertAlmostEqual(comparison["newcombe_high"], high, places=5)
        self.assertEqual(comparison["exact_mcnemar_p"], 1.0)
        self.assertEqual((comparison["interpretation"], comparison["causal_engine_attribution"],
                          comparison["non_inferiority_claim"]),
                         ("exploratory_product_combination_difference", False, False))
        self.assertIsNone(cell(value, arm=PAGEAGENT)["normal"]["gate_verdict"])

    def test_an_ungated_batch_yields_no_comparison(self):
        slot_list = formal_slots("heldout", eligible=("t01", "t02", "t03"))
        batch = Batch(self, slot_list, purpose="heldout")
        batch.run_all()
        value = report.build_report(batches=[batch.complete()], tasks=TASKS,
                                    paired_task_ids=("t01", "t02", "t03"))
        self.assertEqual(value["comparisons"], [])


class VerificationTests(unittest.TestCase):
    def setUp(self):
        batch = Batch(self, small_slots(("t01",)))
        batch.run("s000", PASS)
        self.value = report.build_report(batches=[batch.complete()], tasks=TASKS)

    def test_generated_report_verifies(self):
        self.assertEqual(report.verify_report(self.value)["status"], "READY")

    def test_true_claim_is_rejected(self):
        tampered = copy.deepcopy(self.value)
        tampered["claims"]["engine_attribution"] = True
        with self.assertRaises(ValueError):
            report.verify_report(tampered)

    def test_a_relabelled_gate_is_rejected(self):
        tampered = copy.deepcopy(self.value)
        tampered["batches"][0]["verdict_status"] = "gated"
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
        batch = Batch(self, small_slots(("t01", "t02")))
        with self.assertRaises(ValueError):
            report.build_report(batches=[batch.summary()], tasks={"t01": TASKS["t01"]})


if __name__ == "__main__":
    unittest.main()
