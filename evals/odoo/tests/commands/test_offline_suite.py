"""The sharded runner accounts for every test it was asked to run.

Comparing only how many tests the shards reported would let a shard that skipped one and a shard
that repeated another cancel out. What the runner refuses is checked here against made-up shard
results, so the accounting is tested without running a suite inside a suite.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[4]
_SPEC = importlib.util.spec_from_file_location("offline_suite",
                                               REPOSITORY / "scripts" / "offline_suite.py")
offline_suite = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(offline_suite)


class DiscoveryTests(unittest.TestCase):
    def test_discovery_finds_this_suite_and_orders_it_stably(self):
        ids = offline_suite.test_ids("evals/odoo/tests", ".")
        self.assertEqual(ids, sorted(ids))
        self.assertIn(f"{__name__}.DiscoveryTests.test_discovery_finds_this_suite_and_orders_"
                      "it_stably", ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_round_robin_assigns_every_id_to_exactly_one_shard(self):
        ids = [f"m.C.test_{index:03d}" for index in range(17)]
        groups = [ids[index::4] for index in range(4)]
        self.assertEqual(sorted(name for group in groups for name in group), ids)


class AccountingTests(unittest.TestCase):
    """What the runner does with the ids its shards report."""

    def outcome(self, discovered, shards, codes=None):
        """``shards`` is what each shard reported: (started ids, skipped ids), or None."""
        reported = [None if shard is None else (list(shard[0]), list(shard[1]))
                    for shard in shards]
        return offline_suite.account(discovered, reported, codes or [0] * len(shards))

    def test_every_discovered_test_ran_once(self):
        ids = ["a", "b", "c", "d"]
        self.assertEqual(self.outcome(ids, [(["a", "c"], []), (["b", "d"], [])]), 0)

    def test_a_missing_and_a_repeated_test_do_not_cancel_out(self):
        ids = ["a", "b", "c", "d"]
        # The counts match -- four started, four discovered -- and the set does not.
        self.assertEqual(self.outcome(ids, [(["a", "c"], []), (["b", "b"], [])]), 2)

    def test_a_skipped_test_is_refused_rather_than_counted_as_run(self):
        """unittest starts a skipped test and calls the run successful, so a skip is refused:
        a prerequisite nobody had would otherwise be reported as a suite that passed."""
        ids = ["a", "b"]
        self.assertEqual(self.outcome(ids, [(["a"], []), (["b"], ["b"])]), 2)

    def test_a_test_from_outside_the_suite_is_refused(self):
        self.assertEqual(self.outcome(["a", "b"], [(["a"], []),
                                                   (["b", "elsewhere.C.test_x"], [])]), 2)

    def test_a_shard_that_reported_nothing_is_refused(self):
        self.assertEqual(self.outcome(["a", "b"], [(["a", "b"], []), None]), 2)

    def test_a_failing_shard_fails_the_run_even_when_the_accounting_holds(self):
        self.assertEqual(self.outcome(["a", "b"], [(["a"], []), (["b"], [])], codes=[0, 1]), 1)


if __name__ == "__main__":
    unittest.main()
