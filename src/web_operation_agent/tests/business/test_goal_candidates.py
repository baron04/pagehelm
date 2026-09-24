"""A goal candidate lookup composes metadata without choosing or executing a target."""
from __future__ import annotations

import unittest

from web_operation_agent.business.goal_candidates import (
    GoalCandidateTools, RESOLVE_GOAL_CANDIDATES_SCHEMA)
from web_operation_agent.business.runtime import (BusinessToolRequestError,
                                                  BusinessToolResult)


class _Entities:
    def __init__(self):
        self.calls = []

    def execute(self, name, args, *, context=None):
        self.calls.append((name, args))
        if name == "discover_entities":
            return BusinessToolResult(content={
                "candidates": [{"entity_id": "entity_" + "1" * 32,
                                "label": "Work Items"}],
                "matched_count": 1,
                "catalog_complete": True}, audit={})
        if name == "describe_entity":
            return BusinessToolResult(content={
                "fields": [{"field_id": "field_" + "2" * 32,
                            "label": "Status", "type": "choice"}],
                "fields_truncated": False}, audit={})
        raise AssertionError(name)


class _References:
    def __init__(self):
        self.calls = []

    def execute(self, args, *, context=None):
        self.calls.append(args)
        return BusinessToolResult(content={
            "candidates": [{"record_id": "42", "ui_path": "/work/items/42"}],
            "truncated_or_incomplete": False}, audit={},
            allowed_routes=("/work/items/42",))


class _Pages:
    def __init__(self):
        self.calls = []

    def execute(self, args, *, context=None):
        self.calls.append(args)
        return BusinessToolResult(content={
            "candidates": [{"entry_id": "page_" + "3" * 32,
                            "label": "Work Items", "openable": True,
                            "ui_path": "/work/items"}],
            "matched_count": 1,
            "catalog_complete": True}, audit={}, allowed_routes=("/work/items",))


class GoalCandidateTests(unittest.TestCase):
    def setUp(self):
        self.entities, self.references, self.pages = _Entities(), _References(), _Pages()
        self.tools = GoalCandidateTools(
            self.entities, self.references, self.pages, application_id="fixture")

    def test_answer_and_page_deliverables_keep_distinct_candidates_and_routes(self):
        result = self.tools.execute({"goals": [
            {"delivery": "answer", "subject_query": "Work Items",
             "reference": "WI-42", "detail_query": "Status"},
            {"delivery": "page", "subject_query": "Work Items"}]})
        answer, page = result.content["goals"]
        self.assertEqual(answer["field_candidates"][0]["fields"][0]["label"], "Status")
        self.assertEqual(answer["record_candidates"][0]["record_id"], "42")
        self.assertEqual(page["page_candidates"][0]["label"], "Work Items")
        self.assertEqual(result.allowed_routes, ("/work/items/42", "/work/items"))
        self.assertNotIn("chosen_candidate", result.content)
        self.assertEqual(len(self.references.calls), 1)
        self.assertEqual(len(self.pages.calls), 1)
        self.assertEqual([name for name, _args in self.entities.calls],
                         ["discover_entities", "describe_entity", "discover_entities"])

    def test_handoff_uses_only_page_metadata(self):
        result = self.tools.execute({"goals": [
            {"delivery": "handoff", "subject_query": "Work Items"}]})
        self.assertIn("page_candidates", result.content["goals"][0])
        self.assertEqual(self.entities.calls, [])
        self.assertEqual(self.references.calls, [])

    def test_invalid_goal_is_rejected_before_any_lookup(self):
        for args in ({"goals": []}, {"goals": [{"delivery": "write",
                     "subject_query": "Work Items"}]},
                     {"goals": [{"delivery": [], "subject_query": "Work Items"}]},
                     {"goals": [{"delivery": "answer", "subject_query": " "}]},
                     {"goals": [{"delivery": "answer", "subject_query": "Work Items",
                                 "reference": " "}]},
                     {"goals": [{"delivery": "answer", "subject_query": "Work Items",
                                 "model": "internal.work.item"}]}):
            with self.subTest(args=args), self.assertRaises(BusinessToolRequestError):
                self.tools.execute(args)
        self.assertEqual(self.entities.calls, [])
        self.assertEqual(self.pages.calls, [])

    def test_schema_requires_bounded_goals_not_native_model_names(self):
        parameters = RESOLVE_GOAL_CANDIDATES_SCHEMA["function"]["parameters"]
        self.assertEqual(parameters["required"], ["goals"])
        self.assertEqual(parameters["properties"]["goals"]["maxItems"], 3)
        item = parameters["properties"]["goals"]["items"]
        self.assertEqual(item["required"], ["delivery", "subject_query"])
        self.assertNotIn("model", item["properties"])


if __name__ == "__main__":
    unittest.main()
