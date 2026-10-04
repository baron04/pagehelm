"""Repairs retain targets across generic entity and page declaration structures."""
import unittest

from web_operation_agent.goal.repair import lost_requirements
from web_operation_agent.goal.spec import GoalSpec


def spec(**value):
    return GoalSpec.from_json({"deliverables": [{"kind": "page", "entity": "contacts", **value}]})


class RepairRequirementsTests(unittest.TestCase):
    def test_a_record_description_can_be_corrected_to_selection_conditions(self):
        before = spec(record="the quotation sent to Acme", section="Items")
        after = spec(section="Items", metric={"op": "count", "filters": [
            {"field": "Customer", "values": ["Acme"]}]})
        self.assertEqual(lost_requirements(before, after), ())

    def test_corrected_names_and_labels_keep_the_same_typed_requirements(self):
        before = spec(record="Acme customer", section="Contacts", page={"key": "wrong", "view": "list"})
        after = spec(record="Acme", section="Contact people", page={"key": "people", "view": "list"})
        self.assertEqual(lost_requirements(before, after), ())

    def test_partial_query_and_exclusion_cannot_be_removed(self):
        before = spec(kind="list", metric={"op": "count", "group_by": "Region", "filters": [
            {"field": "Owner", "values": ["Acme"]},
            {"field": "Status", "op": "!=", "values": ["Archived"]}]})
        for conditions in ([{"field": "Owner", "values": ["Acme"]}],
                           [{"field": "Owner", "values": ["Acme"]},
                            {"field": "Status", "op": "=", "values": ["Archived"]}]):
            after = spec(kind="list", metric={"op": "count", "filters": conditions})
            self.assertIn("deliverable 1: query exclusion removed", lost_requirements(before, after))
            self.assertIn("deliverable 1: query grouping removed", lost_requirements(before, after))

    def test_correcting_a_query_label_does_not_lose_the_query(self):
        before = spec(kind="list", metric={"op": "count", "filters": [
            {"field": "Validity", "values": ["2026-09"]}]})
        after = spec(kind="list", metric={"op": "count", "filters": [
            {"field": "Expiration", "values": ["2026-09"]}]})
        self.assertEqual(lost_requirements(before, after), ())
