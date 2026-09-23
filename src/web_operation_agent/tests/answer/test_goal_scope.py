"""A true result from another collection must not complete the user's read goal."""
from __future__ import annotations

import unittest

from web_operation_agent.answer.evidence import (
    ComputationEvidence, Origin, QueryEvidence, TurnEvidence)
from web_operation_agent.answer.goal_scope import (
    assess_goal_conclusions, normalize_goal_entries, parse_answer_goals)


def _read(turn: TurnEvidence, tool: str, index: int, count: int) -> str:
    ref = f"call{index}-query#1"
    turn.record(QueryEvidence(
        evidence_id=ref,
        origin=Origin("typed", f"call{index}:{tool}", "2026-09-24T00:00:00Z",
                      f"actor-visible {tool}"),
        collection=tool, conditions=("exact subject id",), matched_count=count,
        records_scanned=count, covers_whole_scope=True, count_exact=True))
    return ref


def _metric(turn: TurnEvidence, ref: str, *, index: int, measure: str) -> str:
    computed = f"calc#{index}"
    turn.record(ComputationEvidence(
        evidence_id=computed,
        origin=Origin("computed", measure, "2026-09-24T00:00:01Z", "same scope"),
        operation="count()" if measure == "count" else "sum(amount)",
        inputs=(ref,), value="0" if measure == "count" else "2145.00",
        value_type="count" if measure == "count" else "money",
        unit="" if measure == "count" else "USD", covers_whole_scope=True))
    return computed


class GoalScopeTest(unittest.TestCase):
    def test_known_regression_order_zero_quotation_positive(self) -> None:
        turn = TurnEvidence()
        orders = _read(turn, "read_orders", 1, 0)
        quotes = _read(turn, "read_quotations", 2, 2)
        total = _metric(turn, quotes, index=1, measure="sum")
        goals = parse_answer_goals(
            [{"target_read_tool": "read_orders", "measures": ["count", "sum"]}],
            read_tools=frozenset({"read_orders", "read_quotations"}))
        findings = assess_goal_conclusions(turn, [
            {"kind": "selection", "evidence_ref": quotes},
            {"kind": "aggregate", "evidence_ref": total},
            {"kind": "not_found", "evidence_ref": orders},
        ], goals, {orders: "read_orders", quotes: "read_quotations"})
        self.assertIn("missing count from read_orders", findings)
        self.assertIn("missing sum from read_orders", findings)
        self.assertIn("unrequested selection from read_quotations", findings)
        self.assertIn("unrequested sum from read_quotations", findings)

    def test_two_requested_collections_are_independent(self) -> None:
        turn = TurnEvidence()
        active = _read(turn, "read_active_contacts", 1, 1)
        archived = _read(turn, "read_archived_contacts", 2, 2)
        count_active = _metric(turn, active, index=1, measure="count")
        count_archived = _metric(turn, archived, index=2, measure="count")
        goals = parse_answer_goals([
            {"target_read_tool": "read_active_contacts", "measures": ["count"]},
            {"target_read_tool": "read_archived_contacts", "measures": ["count"]},
        ], read_tools=frozenset({"read_active_contacts", "read_archived_contacts"}))
        self.assertEqual((), assess_goal_conclusions(turn, [
            {"kind": "aggregate", "evidence_ref": count_active},
            {"kind": "aggregate", "evidence_ref": count_archived},
        ], goals, {active: "read_active_contacts",
                   archived: "read_archived_contacts"}))

    def test_one_missing_metric_cannot_be_complete(self) -> None:
        turn = TurnEvidence()
        orders = _read(turn, "read_orders", 1, 0)
        count = _metric(turn, orders, index=1, measure="count")
        goals = parse_answer_goals(
            [{"target_read_tool": "read_orders", "measures": ["count", "sum"]}],
            read_tools=frozenset({"read_orders"}))
        self.assertEqual(("missing sum from read_orders",),
                         assess_goal_conclusions(turn, [
                             {"kind": "aggregate", "evidence_ref": count}], goals,
                             {orders: "read_orders"}))

    def test_goals_must_name_installed_read_tools(self) -> None:
        with self.assertRaisesRegex(ValueError, "offered read tool"):
            parse_answer_goals(
                [{"target_read_tool": "read_secret", "measures": ["count"]}],
                read_tools=frozenset({"read_orders"}))

    def test_malformed_unhashable_measure_is_refused_as_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "measures must be"):
            parse_answer_goals(
                [{"target_read_tool": "read_orders", "measures": [{"count": 1}]}],
                read_tools=frozenset({"read_orders"}))

    def test_unrequested_selection_is_omitted_and_same_query_sum_added(self) -> None:
        turn = TurnEvidence()
        orders = _read(turn, "read_orders", 1, 0)
        quotes = _read(turn, "read_quotations", 2, 2)
        count = _metric(turn, orders, index=1, measure="count")
        total = _metric(turn, orders, index=2, measure="sum")
        goals = parse_answer_goals(
            [{"target_read_tool": "read_orders", "measures": ["count", "sum"]}],
            read_tools=frozenset({"read_orders", "read_quotations"}))
        entries, removed = normalize_goal_entries(
            turn, [{"kind": "aggregate", "evidence_ref": count},
                   {"kind": "selection", "evidence_ref": quotes}],
            goals, {orders: "read_orders", quotes: "read_quotations"},
            frozenset({total}), complete=True)
        self.assertEqual((quotes,), removed)
        self.assertEqual([count, total], [entry["evidence_ref"] for entry in entries])

    def test_same_tool_different_query_subjects_cannot_complete_two_metrics(self) -> None:
        turn = TurnEvidence()
        customer_a = _read(turn, "read_orders", 1, 2)
        customer_b = _read(turn, "read_orders", 2, 3)
        count = _metric(turn, customer_a, index=1, measure="count")
        total = _metric(turn, customer_b, index=2, measure="sum")
        goals = parse_answer_goals(
            [{"target_read_tool": "read_orders", "measures": ["count", "sum"]}],
            read_tools=frozenset({"read_orders"}))
        findings = assess_goal_conclusions(
            turn, [{"kind": "aggregate", "evidence_ref": count},
                   {"kind": "aggregate", "evidence_ref": total}],
            goals, {customer_a: "read_orders", customer_b: "read_orders"})
        self.assertIn("unrequested inconsistent query scopes for read_orders", findings)


if __name__ == "__main__":
    unittest.main()
