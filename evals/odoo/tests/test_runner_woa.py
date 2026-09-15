"""The WOA runner's wall-clock arithmetic, without a browser."""
import unittest

from evals.odoo.runner_woa import turn_budget


class TurnBudgetTests(unittest.TestCase):
    def test_a_turn_gets_the_smaller_of_its_own_limit_and_what_the_attempt_has_left(self):
        cases = {0: (150.0, "turn_wall"), 150: (150.0, "turn_wall"),
                 200: (100.0, "attempt_automatic_wall"), 300: (0.0, "attempt_automatic_wall"),
                 320: (0.0, "attempt_automatic_wall")}
        for used, expected in cases.items():
            with self.subTest(used=used):
                self.assertEqual(turn_budget(turn_timeout_s=150, attempt_wall_s=300,
                                             automatic_used_s=used), expected)


if __name__ == "__main__":
    unittest.main()
