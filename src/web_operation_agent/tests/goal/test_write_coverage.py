import json
import unittest
from types import SimpleNamespace

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexField
from web_operation_agent.goal.write_coverage import (bind_requirements, field_catalog,
    require_coverage, scope_tool, WriteFieldConflict, reviewed_requirements)
from web_operation_agent.goal.spec import DECLARE_SCOPE_TOOL


class WriteCoverageTests(unittest.TestCase):
    def test_missing_change_and_wrong_part_cannot_pool_fields(self):
        definitions = (SimpleNamespace(name="edit_record", input_fields=(
            ("changes.phone", "records", "phone"), ("changes.enabled", "records", "enabled"))),)
        parts = [{"kind": "handoff"}, {"kind": "handoff"}]
        bind_requirements([{"part": 0, "entity": "records", "fields": ["phone"]},
            {"part": 1, "entity": "records", "fields": ["enabled"]}], parts, selected=True)
        steps = [{"kind": "operation", "operation": "edit_record", "request_part": 0,
                  "inputs": {"changes": {"phone": "5550100", "enabled": False}}}]
        with self.assertRaises(BusinessToolRequestError):
            require_coverage({"steps": steps}, parts, definitions)
        steps.append({"kind": "operation", "operation": "edit_record", "request_part": 1,
                      "inputs": {"changes": {"enabled": False}}})
        with self.assertRaises(BusinessToolRequestError):
            require_coverage({"steps": steps}, parts, definitions)
        steps[0]["inputs"]["changes"].pop("enabled")
        require_coverage({"steps": steps}, parts, definitions)
        steps[-1]["request_part"] = True
        with self.assertRaises(BusinessToolRequestError):
            require_coverage({"steps": steps}, parts, definitions)

    def test_unknown_fields_remain_requirements_and_empty_lists_do_not_pass(self):
        parts = [{"kind": "handoff"}]
        bind_requirements([{"part": 0, "entity": "records", "fields": ["unavailable-field"]}], parts, selected=True)
        with self.assertRaises(BusinessToolRequestError):
            require_coverage({"steps": []}, parts, ())
        with self.assertRaises(ValueError):
            bind_requirements([], [{"kind": "handoff"}], selected=True)
        for value in ({"part": True, "entity": "records", "fields": ["phone"]},
                      {"part": 0, "entity": "records", "fields": []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bind_requirements([value], [{"kind": "handoff"}], selected=True)

    def test_full_actor_vocabulary_keeps_fields_absent_from_write_schema(self):
        index = ApplicationIndex(entities=(IndexEntity("records", "Records", fields=(
            IndexField("phone", "Phone", "text"), IndexField("fax", "Fax", "text"))),))
        definition = SimpleNamespace(input_fields=(("phone", "records", "phone"),))
        catalog = json.loads(field_catalog(index, (definition,)))
        self.assertEqual({f["key"] for f in catalog[0]["fields"]}, {"phone", "fax"})
        offered = scope_tool(DECLARE_SCOPE_TOOL)
        self.assertNotIn("write_requirements", DECLARE_SCOPE_TOOL["function"]["parameters"]["properties"])
        self.assertIn("write_requirements", offered["function"]["parameters"]["properties"])

    def test_read_and_explanation_require_no_write_fields(self):
        bind_requirements([], [{"kind": "fact"}, {"kind": "handoff", "rules": True}], selected=True)

    def test_review_is_complete_typed_part_bound_and_does_not_mutate_original_scope(self):
        conflict = WriteFieldConflict(((0, "records", "email", False, True),
                                       (1, "records", "fax", True, False)))
        parts = [{"kind": "handoff", "write_fields": [{"entity": "records", "fields": ["name"]}]},
                 {"kind": "handoff", "write_fields": [{"entity": "records", "fields": ["fax", "phone"]}]}]
        before = json.dumps(parts)
        result = reviewed_requirements(conflict, {"decisions": [
            {"field_id": 0, "requested": True}, {"field_id": 1, "requested": False}]}, parts)
        self.assertEqual(json.dumps(parts), before)
        self.assertEqual(result[0]["write_fields"][0]["fields"], ["name", "email"])
        self.assertEqual(result[1]["write_fields"][0]["fields"], ["phone"])
        for decisions in ([], [{"field_id": 0, "requested": True}] * 2,
                [{"field_id": True, "requested": True}, {"field_id": 0, "requested": False}],
                [{"field_id": 0, "requested": "yes"}, {"field_id": 1, "requested": False}],
                [{"field_id": 0, "requested": True}, {"field_id": 2, "requested": False}]):
            with self.subTest(decisions=decisions), self.assertRaises(BusinessToolRequestError):
                reviewed_requirements(conflict, {"decisions": decisions}, parts)
