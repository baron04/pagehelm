"""Frozen count/sum goals reuse trusted query evidence in the same tool step."""
from __future__ import annotations

import unittest

from web_operation_agent.answer.evidence import Origin, QueryEvidence, TurnEvidence
from web_operation_agent.answer.goal_scope import AnswerGoal
from web_operation_agent.execution.goal_metrics import materialize_goal_metrics


class GoalMetricsTest(unittest.TestCase):
    def _turn(self, *, count=0, whole=True, fields=("amount",)):
        turn = TurnEvidence()
        turn.record(QueryEvidence(
            "call1-query#1", Origin("typed", "adapter label", "now", "scope"),
            "orders", ("exact customer",), count, records_scanned=count if whole else 0,
            covers_whole_scope=whole, count_exact=True, summable_fields=fields))
        return turn

    def test_exact_empty_read_materializes_two_metrics_without_currency(self):
        turn = self._turn()
        refs = materialize_goal_metrics(
            turn, ("call1-query#1",), tool="read_orders",
            goals=(AnswerGoal("read_orders", frozenset({"count", "sum"})),), step=1)
        self.assertEqual(refs, ("goal-count#1", "goal-sum#1"))
        self.assertEqual(turn.get(refs[0]).value, "0")
        self.assertEqual((turn.get(refs[1]).value, turn.get(refs[1]).unit), ("0", ""))

    def test_incomplete_sum_is_not_materialized(self):
        turn = self._turn(count=2, whole=False)
        refs = materialize_goal_metrics(
            turn, ("call1-query#1",), tool="read_orders",
            goals=(AnswerGoal("read_orders", frozenset({"count", "sum"})),), step=1)
        self.assertEqual(refs, ("goal-count#1",))

    def test_multiple_additive_fields_are_not_guessed(self):
        turn = self._turn(fields=("net", "gross"))
        refs = materialize_goal_metrics(
            turn, ("call1-query#1",), tool="read_orders",
            goals=(AnswerGoal("read_orders", frozenset({"sum"})),), step=1)
        self.assertEqual(refs, ())


if __name__ == "__main__":
    unittest.main()
