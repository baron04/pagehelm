from __future__ import annotations

import unittest

from evals.woa.pilot_breadth import TASK_IDS, _summary


class PaidBreadthPilotTests(unittest.TestCase):
    def test_summary_distinguishes_completed_diagnostic_from_success_rate(self):
        cases = [{"evaluation_status": "PASS", "runner_status": "completed",
                  "usage_unavailable_calls": 0, "known_cost_cny": 0.1}
                 for _ in TASK_IDS]
        result = _summary(
            taskset={"safe": "digest input"}, initial={"digest": "sha256:" + "a" * 64},
            model="priced-model", max_cost_cny=3.0, cases=cases, stopped_reason=None)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["purpose"], "development_diagnostic_not_benchmark")
        self.assertEqual(result["evaluation_counts"]["PASS"], len(TASK_IDS))
        self.assertNotIn("success_rate", result)

    def test_summary_keeps_unknown_cost_and_stop_reason_explicit(self):
        cases = [{"evaluation_status": "INVALID", "runner_status": "apparatus_invalid",
                  "usage_unavailable_calls": 1, "known_cost_cny": 0.0}]
        result = _summary(
            taskset={}, initial={"digest": "sha256:" + "b" * 64},
            model="priced-model", max_cost_cny=1.0, cases=cases,
            stopped_reason="apparatus_invalid")
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["unknown_cost_calls"], 1)
        self.assertEqual(result["apparatus_invalid_count"], 1)


if __name__ == "__main__":
    unittest.main()
