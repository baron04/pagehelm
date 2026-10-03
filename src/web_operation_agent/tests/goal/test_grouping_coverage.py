"""Explicit count groupings cannot silently become an overall total."""
import unittest

from web_operation_agent.goal.coverage import missing_group
from web_operation_agent.goal.executor import _coverage_gaps, _not_done
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexField
from web_operation_agent.goal.spec import Deliverable, GoalSpec, Metric


INDEX = ApplicationIndex(entities=(
    IndexEntity("people", "People", aliases=("人员",), fields=(
        IndexField("owner", "Owner", "link", aliases=("负责人",)),
        IndexField("state", "Status", "choice", aliases=("状态",)))),
    IndexEntity("orders", "Orders", aliases=("订单",), fields=(
        IndexField("owner", "Owner", "link", aliases=("负责人",)),))), pages=(), source_digest="fixture")


class GroupingCoverageTests(unittest.TestCase):
    def test_missing_or_wrong_group_is_repaired_without_inventing_a_declaration(self):
        for group in ("", "Status"):
            d = Deliverable("aggregate", entity="people", metric=Metric("count", group_by=group))
            failure = missing_group(d, "人员按负责人分组，各有多少？", INDEX)
            self.assertIsNotNone(failure)
            self.assertEqual(failure.missing_group, "Owner")
            self.assertIn("metric.group_by", failure.repair)
            self.assertIsNone(failure.aggregate)
            self.assertIn("分组统计", _not_done(failure, True))
            self.assertEqual(d.metric.group_by, group)

    def test_matching_index_aliases_and_english_group_by_are_accepted(self):
        for group, text in (("Owner", "Group by Owner"), ("负责人", "人员按负责人分组")):
            self.assertIsNone(missing_group(Deliverable("aggregate", entity="people",
                metric=Metric("count", group_by=group)), text, INDEX))

    def test_unknown_negated_implicit_and_other_entity_groups_do_not_change_a_count(self):
        d = Deliverable("aggregate", entity="people", metric=Metric("count"))
        for text in ("人员按地区分组", "人员不按负责人分组", "人员不要按负责人分组",
                     "Do not group by Owner", "各负责人的数量", "订单按负责人分组，人员总数是多少？"):
            with self.subTest(text=text):
                self.assertIsNone(missing_group(d, text, INDEX))

    def test_pages_facts_and_numeric_extremes_keep_their_contracts(self):
        for kind, op in (("page", "count"), ("fact", "count"), ("aggregate", "max"), ("aggregate", "min")):
            self.assertIsNone(missing_group(Deliverable(kind, entity="people", metric=Metric(op)),
                                           "人员按负责人分组", INDEX))

    def test_existing_coverage_repair_receives_explicit_group_gap(self):
        spec = GoalSpec(deliverables=(Deliverable("aggregate", entity="people", metric=Metric("count")),))
        self.assertEqual(len(_coverage_gaps(spec, [], "人员按负责人分组", "2026-10-03", index=INDEX)), 1)
