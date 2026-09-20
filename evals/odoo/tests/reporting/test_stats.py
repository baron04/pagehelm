from __future__ import annotations

import math
import unittest
from statistics import NormalDist

from evals.odoo.reporting.stats import (
    binomial_upper_tail,
    exact_mcnemar_p,
    nearest_rank_percentile,
    newcombe_paired_difference_interval,
    one_sided_upper_bound,
    operating_characteristics,
    paired_transitions,
    proportion_summary,
    screening_threshold,
    wilson_interval,
    zero_event_upper_bound,
)


Z95 = NormalDist().inv_cdf(0.975)


def _binomial_cdf(events: int, n: int, p: float) -> float:
    return sum(math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(events + 1))


class ScreeningThresholdTests(unittest.TestCase):
    def test_threshold_is_integer_ceiling_of_ninety_percent(self):
        self.assertEqual(
            [screening_threshold(n) for n in (1, 10, 11, 20, 25, 30)],
            [1, 9, 10, 18, 23, 27])

    def test_invalid_sizes_are_rejected(self):
        for value in (0, -1, True, 20.0, "20"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                screening_threshold(value)


class WilsonIntervalTests(unittest.TestCase):
    def test_boundary_counts_keep_exact_ends_and_nonzero_width(self):
        low, high = wilson_interval(0, 10)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, Z95 ** 2 / (10 + Z95 ** 2), places=12)

        low, high = wilson_interval(10, 10)
        self.assertEqual(high, 1.0)
        self.assertAlmostEqual(low, 10 / (10 + Z95 ** 2), places=12)

    def test_known_interval_and_mirror_symmetry(self):
        low, high = wilson_interval(18, 20)
        self.assertAlmostEqual(low, 0.6990, places=4)
        self.assertAlmostEqual(high, 0.9721, places=4)

        mirror_low, mirror_high = wilson_interval(2, 20)
        self.assertAlmostEqual(mirror_low, 1 - high, places=12)
        self.assertAlmostEqual(mirror_high, 1 - low, places=12)

    def test_higher_confidence_widens_the_interval(self):
        narrow = wilson_interval(15, 20, 0.80)
        wide = wilson_interval(15, 20, 0.99)
        self.assertLess(wide[0], narrow[0])
        self.assertGreater(wide[1], narrow[1])

    def test_invalid_inputs_are_rejected(self):
        cases = [
            (-1, 10, 0.95), (11, 10, 0.95), (0, 0, 0.95), (True, 10, 0.95),
            (1, True, 0.95), (1.0, 10, 0.95), (1, 10, math.nan), (1, 10, math.inf),
            (1, 10, 0.0), (1, 10, 1.0), (1, 10, 1.5), (1, 10, True),
        ]
        for successes, n, confidence in cases:
            with self.subTest(successes=successes, n=n, confidence=confidence):
                with self.assertRaises(ValueError):
                    wilson_interval(successes, n, confidence)


class BinomialTailTests(unittest.TestCase):
    def test_exact_small_cases(self):
        self.assertEqual(binomial_upper_tail(3, 2, 0.5), 0.5)
        self.assertEqual(binomial_upper_tail(5, 0, 0.3), 1.0)
        self.assertEqual(binomial_upper_tail(5, 1, 0.0), 0.0)
        self.assertEqual(binomial_upper_tail(5, 5, 1.0), 1.0)
        self.assertAlmostEqual(binomial_upper_tail(20, 18, 0.9),
                               1 - _binomial_cdf(17, 20, 0.9), places=12)

    def test_invalid_inputs_are_rejected(self):
        cases = [
            (0, 0, 0.5), (5, -1, 0.5), (5, 6, 0.5), (True, 1, 0.5), (5, True, 0.5),
            (5, 1, -0.1), (5, 1, 1.1), (5, 1, math.nan), (5, 1, False),
        ]
        for n, threshold, p in cases:
            with self.subTest(n=n, threshold=threshold, p=p), self.assertRaises(ValueError):
                binomial_upper_tail(n, threshold, p)


class OperatingCharacteristicTests(unittest.TestCase):
    def test_protocol_published_values_reproduce(self):
        published = {
            20: [0.925, 0.677, 0.405, 0.206],
            25: [0.873, 0.537, 0.254, 0.098],
            30: [0.939, 0.647, 0.322, 0.123],
        }
        rates = [0.95, 0.90, 0.85, 0.80]
        for n, expected in published.items():
            with self.subTest(n=n):
                rows = operating_characteristics(n, rates)
                self.assertEqual([row["pass_probability"] for row in rows], expected)
                self.assertEqual([row["true_rate"] for row in rows], rates)
                self.assertEqual({row["threshold"] for row in rows},
                                 {screening_threshold(n)})

    def test_invalid_inputs_are_rejected(self):
        for n, rates in ((0, [0.9]), (20, []), (20, [1.2]), (20, [math.nan]),
                         (20, [True]), (20, "0.9"), (20, 0.9)):
            with self.subTest(n=n, rates=rates), self.assertRaises(ValueError):
                operating_characteristics(n, rates)


class UpperBoundTests(unittest.TestCase):
    def test_zero_event_bound_matches_closed_form(self):
        self.assertAlmostEqual(zero_event_upper_bound(60), 0.0487, places=4)
        self.assertAlmostEqual(zero_event_upper_bound(30, 0.90), 1 - 0.1 ** (1 / 30),
                               places=15)

    def test_one_sided_bound_solves_the_exact_binomial_cdf(self):
        bound = one_sided_upper_bound(1, 10)
        self.assertAlmostEqual(bound, 0.3942, places=4)
        self.assertLessEqual(_binomial_cdf(1, 10, bound), 0.05 + 1e-12)
        self.assertAlmostEqual(_binomial_cdf(1, 10, bound), 0.05, places=8)
        self.assertLess(one_sided_upper_bound(1, 60), one_sided_upper_bound(2, 60))

    def test_one_sided_bound_edges(self):
        self.assertEqual(one_sided_upper_bound(0, 60), zero_event_upper_bound(60))
        self.assertEqual(one_sided_upper_bound(0, 40, 0.8), zero_event_upper_bound(40, 0.8))
        self.assertEqual(one_sided_upper_bound(12, 12), 1.0)

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            zero_event_upper_bound(0)
        with self.assertRaises(ValueError):
            zero_event_upper_bound(True)
        with self.assertRaises(ValueError):
            zero_event_upper_bound(10, math.nan)
        for events, n, confidence in ((-1, 10, 0.95), (11, 10, 0.95), (True, 10, 0.95),
                                      (1, 0, 0.95), (1, 10, 1.0), (1, 10, -0.5)):
            with self.subTest(events=events, n=n, confidence=confidence):
                with self.assertRaises(ValueError):
                    one_sided_upper_bound(events, n, confidence)


class McNemarTests(unittest.TestCase):
    def test_exact_values(self):
        self.assertEqual(exact_mcnemar_p(0, 0), 1.0)
        self.assertEqual(exact_mcnemar_p(5, 0), 0.0625)
        self.assertEqual(exact_mcnemar_p(10, 2), 2 * 79 / 4096)
        self.assertEqual(exact_mcnemar_p(2, 10), exact_mcnemar_p(10, 2))
        self.assertEqual(exact_mcnemar_p(3, 3), 1.0)

    def test_invalid_inputs_are_rejected(self):
        for b, c in ((-1, 0), (0, -1), (True, 0), (0, False), (1.0, 2)):
            with self.subTest(b=b, c=c), self.assertRaises(ValueError):
                exact_mcnemar_p(b, c)


class NewcombeIntervalTests(unittest.TestCase):
    def test_all_concordant_table_does_not_degenerate(self):
        for table in ((27, 0, 0, 3), (30, 0, 0, 0), (0, 0, 0, 30), (15, 0, 0, 15)):
            with self.subTest(table=table):
                difference, lower, upper = newcombe_paired_difference_interval(*table)
                self.assertEqual(difference, 0.0)
                self.assertLess(lower, 0.0)
                self.assertGreater(upper, 0.0)

    def test_one_sided_discordance(self):
        difference, lower, upper = newcombe_paired_difference_interval(20, 5, 0, 5)
        self.assertEqual(difference, 5 / 30)
        self.assertGreater(lower, -1.0)
        self.assertLessEqual(upper, 1.0)
        self.assertLess(lower, difference)
        self.assertGreater(upper, difference)

    def test_swapping_discordant_cells_negates_and_swaps_bounds(self):
        for a, b, c, d in ((27, 2, 0, 1), (36, 12, 2, 0), (10, 3, 7, 10), (0, 4, 1, 0)):
            with self.subTest(table=(a, b, c, d)):
                forward = newcombe_paired_difference_interval(a, b, c, d)
                backward = newcombe_paired_difference_interval(a, c, b, d)
                self.assertAlmostEqual(backward[0], -forward[0], places=12)
                self.assertAlmostEqual(backward[1], -forward[2], places=12)
                self.assertAlmostEqual(backward[2], -forward[1], places=12)

    def test_bounds_stay_within_unit_difference_range(self):
        for table in ((0, 30, 0, 0), (0, 0, 30, 0), (0, 1, 0, 0), (1, 0, 0, 0),
                      (0, 15, 15, 0), (3, 9, 1, 2)):
            with self.subTest(table=table):
                _, lower, upper = newcombe_paired_difference_interval(*table)
                self.assertGreaterEqual(lower, -1.0)
                self.assertLessEqual(upper, 1.0)
                self.assertLessEqual(lower, upper)

    def test_method_ten_expression_on_negatively_associated_table(self):
        a, b, c, d = 36, 12, 2, 0
        p1, p2 = 48 / 50, 38 / 50
        l1, u1 = wilson_interval(48, 50)
        l2, u2 = wilson_interval(38, 50)
        phi = (a * d - b * c) / math.sqrt(48 * 2 * 38 * 12)
        expected_lower = 0.2 - math.sqrt(
            (p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2)
        expected_upper = 0.2 + math.sqrt(
            (p2 - l2) ** 2 - 2 * phi * (p2 - l2) * (u1 - p1) + (u1 - p1) ** 2)

        difference, lower, upper = newcombe_paired_difference_interval(a, b, c, d)
        self.assertAlmostEqual(difference, 0.2, places=12)
        self.assertAlmostEqual(lower, expected_lower, places=12)
        self.assertAlmostEqual(upper, expected_upper, places=12)
        self.assertAlmostEqual(lower, 0.0569, places=4)
        self.assertAlmostEqual(upper, 0.3404, places=4)

    def test_positive_association_uses_continuity_corrected_phi(self):
        # ad - bc = 81 - 0 = 81 > n/2, so phi* = (81 - 15) / 81 rather than 1.
        a, b, c, d = 27, 0, 0, 3
        p = 0.9
        low, high = wilson_interval(27, 30)
        phi = 66 / 81
        spread = math.sqrt((p - low) ** 2 - 2 * phi * (p - low) * (high - p) + (high - p) ** 2)
        _, lower, upper = newcombe_paired_difference_interval(a, b, c, d)
        self.assertAlmostEqual(lower, -spread, places=12)
        self.assertAlmostEqual(upper, spread, places=12)

    def test_invalid_inputs_are_rejected(self):
        cases = [
            (0, 0, 0, 0, 0.95), (-1, 1, 1, 1, 0.95), (1, True, 1, 1, 0.95),
            (1, 1, 1.0, 1, 0.95), (1, 1, 1, 1, math.nan), (1, 1, 1, 1, 1.0),
        ]
        for a, b, c, d, confidence in cases:
            with self.subTest(table=(a, b, c, d), confidence=confidence):
                with self.assertRaises(ValueError):
                    newcombe_paired_difference_interval(a, b, c, d, confidence)


class NearestRankPercentileTests(unittest.TestCase):
    def test_nearest_rank_values(self):
        self.assertEqual(nearest_rank_percentile(list(range(1, 21)), 95), 19)
        self.assertEqual(nearest_rank_percentile(list(range(1, 101)), 95), 95)
        self.assertEqual(nearest_rank_percentile([5, 1, 3], 50), 3)
        self.assertEqual(nearest_rank_percentile([5, 1, 3], 100), 5)
        self.assertEqual(nearest_rank_percentile([5, 1, 3], 0.1), 1)
        self.assertEqual(nearest_rank_percentile(list(range(1, 1001)), 99.9), 999)

    def test_invalid_inputs_are_rejected(self):
        cases = [
            ([], 50), ([1, 2], 0), ([1, 2], 101), ([1, 2], math.nan), ([1, math.inf], 50),
            ([1, True], 50), ([1, 2], True), ("12", 50), (None, 50),
        ]
        for values, q in cases:
            with self.subTest(values=values, q=q), self.assertRaises(ValueError):
                nearest_rank_percentile(values, q)


class PairedTransitionTests(unittest.TestCase):
    def test_counts_each_transition_without_pooling(self):
        result = paired_transitions([True, True, False, False, True],
                                    [True, False, True, False, True])
        self.assertEqual(result, {
            "pass_pass": 2, "pass_fail": 1, "fail_pass": 1, "fail_fail": 1,
            "agreement": 3, "n": 5, "agreement_rate": 0.6,
        })

    def test_invalid_inputs_are_rejected(self):
        cases = [
            ([True], [True, False]), ([], []), ([1], [True]), ([True], [None]),
            ("TF", "TF"), (None, [True]),
        ]
        for primary, replicate in cases:
            with self.subTest(primary=primary, replicate=replicate):
                with self.assertRaises(ValueError):
                    paired_transitions(primary, replicate)


class ProportionSummaryTests(unittest.TestCase):
    def test_summary_rounds_rate_and_interval(self):
        low, high = wilson_interval(18, 20)
        self.assertEqual(proportion_summary(18, 20), {
            "successes": 18, "n": 20, "rate": 0.9,
            "wilson_low": round(low, 6), "wilson_high": round(high, 6),
        })

    def test_empty_denominator_is_reported_as_none(self):
        self.assertEqual(proportion_summary(0, 0), {
            "successes": 0, "n": 0, "rate": None, "wilson_low": None, "wilson_high": None,
        })

    def test_invalid_inputs_are_rejected(self):
        cases = [
            (1, 0, 0.95), (-1, 5, 0.95), (6, 5, 0.95), (True, 5, 0.95), (1, -1, 0.95),
            (1, 5, math.nan), (1, 5, 0.0), (0, 0, 2.0),
        ]
        for successes, n, confidence in cases:
            with self.subTest(successes=successes, n=n, confidence=confidence):
                with self.assertRaises(ValueError):
                    proportion_summary(successes, n, confidence)


if __name__ == "__main__":
    unittest.main()
