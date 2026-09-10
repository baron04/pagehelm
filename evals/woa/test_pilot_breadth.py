from __future__ import annotations

import unittest

from evals.woa.pilot_breadth import TASK_IDS, _summary


class PaidBreadthPilotTests(unittest.TestCase):
    def test_summary_distinguishes_completed_diagnostic_from_success_rate(self):
        cases = [{"evaluation_status": "PASS", "runner_status": "completed",
                  "task_kind": "refuse" if index < 2 else "enumerate",
                  "usage_unavailable_calls": 0, "known_cost_cny": 0.1,
                  "wall_ms": 100 + index, "steps_used": 3, "model_calls": 3,
                  "page_actions": 0, "business_calls": 1,
                  "safety": {"unauthorized_writes": 0, "duplicate_writes": 0,
                             "forbidden_state_changes": 0,
                             "cross_identity_actions": 0}}
                 for index, _task_id in enumerate(TASK_IDS)]
        result = _summary(
            taskset={"safe": "digest input"}, initial={"digest": "sha256:" + "a" * 64},
            model="priced-model", max_cost_cny=3.0, cases=cases, stopped_reason=None)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["purpose"], "development_diagnostic_not_benchmark")
        self.assertEqual(result["evaluation_counts"]["PASS"], len(TASK_IDS))
        self.assertNotIn("success_rate", result)
        self.assertEqual(result["total_cost_cny"], 1.2)
        self.assertEqual(result["refusal_counts"], {"scheduled": 2, "passed": 2})
        self.assertEqual(result["metrics"]["wall_ms"]["count"], len(TASK_IDS))
        self.assertEqual(result["safety_totals"]["unauthorized_writes"], 0)

    def test_summary_keeps_unknown_cost_and_stop_reason_explicit(self):
        cases = [{"evaluation_status": "INVALID", "runner_status": "apparatus_invalid",
                  "usage_unavailable_calls": 1, "known_cost_cny": 0.0}]
        result = _summary(
            taskset={}, initial={"digest": "sha256:" + "b" * 64},
            model="priced-model", max_cost_cny=1.0, cases=cases,
            stopped_reason="apparatus_invalid")
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["unknown_cost_calls"], 1)
        self.assertIsNone(result["total_cost_cny"])
        self.assertEqual(result["apparatus_invalid_count"], 1)


if __name__ == "__main__":
    unittest.main()
