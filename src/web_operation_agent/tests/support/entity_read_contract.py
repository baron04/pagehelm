"""Reusable black-box assertions for every in-repository entity-read adapter."""
from __future__ import annotations

from web_operation_agent.business.entities import (
    DESCRIBE_ENTITY_TOOL,
    DISCOVER_ENTITIES_TOOL,
    QUERY_ENTITY_RECORDS_TOOL,
    EntityReadTools,
)


def assert_entity_read_contract(case, tools: EntityReadTools, *, query: str,
                                entity_label: str, field_label: str,
                                filter_operator: str, filter_value: object,
                                expected_record_id: str,
                                expected_value: str) -> None:
    """Exercise the same discover/describe/query/evidence path against one adapter."""
    discovery = tools.execute(DISCOVER_ENTITIES_TOOL, {"query": query, "limit": 5})
    case.assertGreaterEqual(discovery.content["matched_count"], 1)
    candidate = next(item for item in discovery.content["candidates"]
                     if item["label"] == entity_label)
    case.assertRegex(candidate["entity_id"], r"^entity_[0-9a-f]{32}$")
    case.assertNotIn("reference", candidate)

    description = tools.execute(DESCRIBE_ENTITY_TOOL, {
        "entity_id": candidate["entity_id"], "field_query": field_label, "limit": 40,
    })
    field = next(item for item in description.content["fields"]
                 if item["label"] == field_label)
    case.assertRegex(field["field_id"], r"^field_[0-9a-f]{32}$")

    result = tools.execute(QUERY_ENTITY_RECORDS_TOOL, {
        "entity_id": candidate["entity_id"],
        "field_ids": [field["field_id"]],
        "filters": [{"field_id": field["field_id"],
                     "operator": filter_operator, "value": filter_value}],
        "limit": 10,
    })
    case.assertEqual(result.content["records"][0], {
        "record_id": expected_record_id,
        "values": [{"field_id": field["field_id"], "label": field_label,
                    "value": expected_value}],
    })
    case.assertTrue(result.content["completeness"]["matching_count_exact"])
    case.assertEqual(result.evidence[0].matched_count,
                     result.content["completeness"]["matched_count"])
    case.assertEqual(result.evidence[1].value, expected_value)
    case.assertEqual(result.query_answer.records[0][0], expected_record_id)


__all__ = ["assert_entity_read_contract"]
