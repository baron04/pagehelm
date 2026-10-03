"""Only adjacent identical-scope field reads may agree across grouping shapes."""
from dataclasses import replace
import unittest

from web_operation_agent.goal.spec import Deliverable, Filter, GoalSpec, Metric, PageSpec


class FactEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.read = Deliverable(kind="fact", entity="ticket", record="T1", fields=("Status", "Amount"))

    def test_split_reordered_and_repeated_fields_agree_without_rewriting_the_goal(self):
        first = GoalSpec((self.read,))
        second = GoalSpec((replace(self.read, fields=("Amount",)),
                           replace(self.read, fields=("Status", "Amount"))))
        self.assertEqual(first.differences(second), [])
        self.assertEqual(second.differences(first), [])
        self.assertEqual(len(second.to_json()["deliverables"]), 2)

    def test_changed_scope_or_missing_field_is_never_equivalent(self):
        for change in ({"record": "T2"}, {"entity": "team"}, {"via": "Owner"},
                       {"metric": Metric(op="count", filters=(Filter("Owner", ("Alpha",), "!="),))},
                       {"page": PageSpec(key="mine")}, {"action": "edit"}):
            split = GoalSpec((replace(self.read, fields=("Status",)),
                              replace(self.read, fields=("Amount",), **change)))
            self.assertTrue(GoalSpec((self.read,)).differences(split), change)
        self.assertTrue(GoalSpec((self.read,)).differences(
            GoalSpec((replace(self.read, fields=("Status",)),))))

    def test_rules_navigation_handoffs_and_intervening_goals_are_not_merged(self):
        for kind, rules in (("page", ()), ("handoff", ()), ("fact", ("guard",))):
            combined = replace(self.read, kind=kind, rules=rules)
            split = (replace(combined, fields=("Status",)), replace(combined, fields=("Amount",)))
            self.assertTrue(GoalSpec((combined,)).differences(GoalSpec(split)))
        page = Deliverable(kind="page", entity="ticket", record="T1")
        self.assertTrue(GoalSpec((self.read, page)).differences(GoalSpec((
            replace(self.read, fields=("Status",)), page, replace(self.read, fields=("Amount",))))))

    def test_write_intent_still_differs(self):
        self.assertTrue(GoalSpec((self.read,), write_intent=True).differences(
            GoalSpec((replace(self.read, fields=("Amount", "Status")),))))
