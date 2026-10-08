"""A field batch is one bounded form target; malformed batches cannot become page goals."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent.goal.resolve.resolver import Resolver
from web_operation_agent.goal.index import ApplicationIndex
from web_operation_agent.goal.data import Schema


class FormBatchSpecTests(unittest.TestCase):
    def spec(self, **changes):
        payload = {"kind": "handoff", "entity": "contacts", "field_values":
                   [{"field": "Name", "value": ""}, {"field": "Note", "value": "A\nB"}]}
        return GoalSpec.from_json({"deliverables": [{**payload, **changes}], "write_intent": True})

    def test_batch_preserves_explicit_clear_multiline_and_roundtrips(self):
        spec = self.spec()
        self.assertEqual(spec.deliverables[0].field_values, (("Name", ""), ("Note", "A\nB")))
        self.assertEqual(GoalSpec.from_json(spec.to_json()), spec)
        self.assertFalse(spec.differences(GoalSpec.from_json(spec.to_json())))

    def test_one_value_beside_one_field_is_that_fields_value(self):
        one = GoalSpec.from_json({"write_intent": True, "deliverables": [
            {"kind": "handoff", "entity": "contacts", "record": "Ada", "value": "a@x.invalid",
             "fields": ["Email"]}]}).deliverables[0]
        self.assertEqual((one.field_values, one.value, one.fields), ((("Email", "a@x.invalid"),), "", ()))
        self.assertEqual(GoalSpec.from_json({"write_intent": True, "deliverables": [
            {"kind": "handoff", "entity": "contacts", "record": "Ada",
             "field_values": [{"field": "Email", "value": "a@x.invalid"}]}]}).deliverables[0], one)
        # A named action keeps its own value; several fields, or a field with no value,
        # are still fields that lack a value; another kind is never a form.
        for other in ({"action": "Email", "value": "a@x.invalid"},
                      {"value": "a@x.invalid", "fields": ["Email", "Phone"]},
                      {"fields": ["Email"]}):
            kept = GoalSpec.from_json({"write_intent": True, "deliverables": [
                {"kind": "handoff", "entity": "contacts", **other}]}).deliverables[0]
            self.assertEqual(kept.field_values, ())
        fact = GoalSpec.from_json({"deliverables": [
            {"kind": "fact", "entity": "contacts", "record": "Ada", "fields": ["Email"]}]}).deliverables[0]
        self.assertEqual((fact.field_values, fact.fields), ((), ("Email",)))

    def test_batch_entry_action_remains_for_application_review(self):
        for action in ("edit", "create", "Reviewed entry", "Delete"):
            self.assertEqual(self.spec(action=action).deliverables[0].action, action)

    def test_malformed_batch_is_rejected_without_clipping_requested_work(self):
        cases = [{"kind": "fact"}, {"value": "other"},
                 {"field_values": "bad"}, {"field_values": [{"field":"Name","value":False}]},
                 {"field_values": [{"field":"Note","value":"x" * 4001}]},
                 {"field_values": [{"field":"Name","value":"a"},{"field":" name ","value":"b"}]},
                 {"field_values": [{"field":str(i),"value":"a"} for i in range(21)]},
                 {"field_values": [{"field":"Name","value":"a","selector":"secret"}]}]
        for case in cases:
            with self.subTest(case=case.keys()), self.assertRaises(ValueError): self.spec(**case)

    def test_untranslated_batch_is_not_silently_treated_as_navigation(self):
        port = SimpleNamespace(find=Mock(side_effect=AssertionError("read")))
        result = Resolver(port, ApplicationIndex(), Schema(entities=(),pages=())).resolve(self.spec())
        self.assertEqual(result.deliverables[0].unoffered, "fill")
        self.assertIsNone(result.deliverables[0].page)
        port.find.assert_not_called()
