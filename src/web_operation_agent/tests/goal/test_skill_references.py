"""Nested/set references bind only verified effects and leave literal text alone."""
import copy
import unittest

from web_operation_agent.goal.skill_plan import bind_references, reference_dependencies


class SkillReferenceSets(unittest.TestCase):
    def test_nested_scalar_and_complete_relation_set_bind_without_mutating_the_plan(self):
        inputs = {"changes": {"owner": "@result:1"},
                  "members": ["Existing (ID=4)", "@result:0", "@result:1"],
                  "notes": "@result:0"}
        before = copy.deepcopy(inputs)
        receipts = {0: {"verified": True, "reference": "New (ID=2)"},
                    1: {"verified": True, "reference": "Owner (ID=3)"}}
        paths = ("changes.owner", "members")
        self.assertEqual(reference_dependencies(inputs, paths), (1, 0, 1))
        self.assertEqual(bind_references(inputs, receipts, paths), {
            "changes": {"owner": "Owner (ID=3)"},
            "members": ["Existing (ID=4)", "New (ID=2)", "Owner (ID=3)"],
            "notes": "@result:0"})
        self.assertEqual(inputs, before)

    def test_missing_unverified_or_invalid_member_does_not_return_a_smaller_set(self):
        inputs = {"members": ["Existing", "@result:0", "@result:1"]}
        for invalid in (None, {}, {"verified": False, "reference": "Second"},
                        {"verified": True, "reference": 42},
                        {"verified": True, "reference": ""}):
            receipts = {0: {"verified": True, "reference": "First"}, 1: invalid}
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                bind_references(inputs, receipts, ("members",))
            self.assertEqual(inputs["members"], ["Existing", "@result:0", "@result:1"])

    def test_undeclared_text_has_no_dependency_semantics(self):
        self.assertEqual(reference_dependencies({"note": "@result:0"}), ())
        self.assertEqual(bind_references({"note": "@result:0"}, {}), {"note": "@result:0"})
