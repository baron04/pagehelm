from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from evals.twenty.evaluator import EvaluationInputError
from evals.twenty.power import build_power_sensitivity, verify_power_sensitivity


ARTIFACT = Path(__file__).resolve().parents[1] / "power-sensitivity-v1.json"


class PairedPowerPlanningTests(unittest.TestCase):
    def test_checked_in_sensitivity_recomputes(self):
        stored = json.loads(ARTIFACT.read_text(encoding="utf-8"))

        self.assertEqual(verify_power_sensitivity(stored)["status"], "READY")
        self.assertEqual(
            [(row["discordance_rate"], row["superiority_required_pairs"],
              row["noninferiority_required_pairs"])
             for row in stored["scenarios"]],
            [(0.1, 86, 347), (0.2, 174, 700), (0.3, 263, 1052), (0.5, 439, 1758)],
        )
        self.assertTrue(stored["scenarios"][0]["planned_supports_superiority"])
        self.assertFalse(any(
            row["planned_supports_noninferiority"] for row in stored["scenarios"]))

    def test_planned_count_is_independent_pairs_not_total_trials(self):
        plan = build_power_sensitivity(
            discordance_rates=[0.2], primary_hypotheses=3, planned_pairs=30)

        row = plan["scenarios"][0]
        self.assertEqual(plan["planned_pairs"], 30)
        self.assertLess(row["superiority_power_at_planned"], 0.20)
        self.assertLess(row["noninferiority_power_at_planned"], 0.10)

    def test_invalid_assumptions_and_edited_results_are_rejected(self):
        with self.assertRaisesRegex(EvaluationInputError, "discordance"):
            build_power_sensitivity(discordance_rates=[0.05])
        with self.assertRaisesRegex(EvaluationInputError, "positive integer"):
            build_power_sensitivity(discordance_rates=[0.2], planned_pairs=0)

        artifact = build_power_sensitivity(discordance_rates=[0.2])
        changed = copy.deepcopy(artifact)
        changed["scenarios"][0]["superiority_required_pairs"] = 30
        with self.assertRaisesRegex(EvaluationInputError, "does not recompute"):
            verify_power_sensitivity(changed)


if __name__ == "__main__":
    unittest.main()
