"""What a tool does is classified by entity and operation, and some classes never install.

A deployment's rule about what the assistant may do is written against entities and
operations -- "never delete anything" -- not against tool names, which an application can
rename. The catalog refuses to build when a tool performs a forbidden operation, so a
forbidden capability is absent from the session rather than refused at the step that tries
it, and every dispatched result carries the server's own words for what it did.

The tool surface itself does not grow with entities: the generic entity read is three
schemas whether the application exposes one entity or a hundred.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.business.entities import (ENTITY_READ_SCHEMAS, EntityCatalog,
                                                   EntityDescriptor, EntityReadTools)
from web_operation_agent.business.runtime import (BusinessSkill, BusinessToolResult,
                                                  BusinessToolRuntime, ForbiddenOperation,
                                                  TOOL_OPERATIONS, ToolOperation)

READ = {"type": "function", "function": {
    "name": "query_customers", "description": "Read customers.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
WRITE = {"type": "function", "function": {
    "name": "delete_customer", "description": "Delete one customer.",
    "parameters": {"type": "object", "properties": {"id": {"type": "integer"}},
                   "required": ["id"], "additionalProperties": False}}}


class _Executor:
    def __init__(self, schemas, effects):
        self._schemas, self._effects = schemas, effects

    def tool_schemas(self):
        return self._schemas

    def tool_effects(self):
        return dict(self._effects)

    def execute(self, name, args, *, context=None):
        return BusinessToolResult(
            content={"customers": [], "matched_count": 0, "scope": "fixture"},
            audit={"application": "fixture", "operation": name, "returned_count": 0,
                   "source_complete": True})


def _skill(directory: Path, tools, *, read_only=True) -> BusinessSkill:
    path = directory / f"skill-{'-'.join(tools)}.json"
    path.write_text(json.dumps({
        "schema_version": 1, "skill_id": "fixture." + "-".join(tools),
        "title": "Fixture", "intent": "Fixture skill.", "triggers": ["customers"],
        "instructions": ["Use the tool."], "tool_names": list(tools),
        "provenance": ["src:fixture@v1:skills.json"], "read_only": read_only}) + "\n")
    return BusinessSkill.load(path)


class ToolOperations(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)

    def _runtime(self, **kwargs):
        return BusinessToolRuntime(
            (_skill(self.tmp, ["query_customers"]),),
            _Executor((READ,), {"query_customers": "read"}), **kwargs)

    def test_the_vocabulary_is_closed(self):
        self.assertEqual(TOOL_OPERATIONS, {"read", "create", "update", "archive", "delete",
                                           "state_change", "issue_document"})
        for bad in ("", "READ", "drop", "read "):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ToolOperation("customer", bad)
        for bad in ("", "Customer", "customer-1", "x" * 65):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ToolOperation(bad, "read")

    def test_every_tool_must_be_classified_and_agree_with_its_effect(self):
        with self.assertRaisesRegex(ValueError, "classify every business tool"):
            self._runtime(operations={})
        with self.assertRaisesRegex(ValueError, "declared read but classified create"):
            self._runtime(operations={"query_customers": ToolOperation("customer", "create")})
        runtime = self._runtime(
            operations={"query_customers": ToolOperation("customer", "read")})
        self.assertEqual(runtime.operations["query_customers"].entity, "customer")

    def test_a_forbidden_operation_refuses_the_catalog_naming_tool_and_rule(self):
        executor = _Executor((READ, WRITE),
                             {"query_customers": "read", "delete_customer": "write"})
        skills = (_skill(self.tmp, ["query_customers"]),
                  _skill(self.tmp, ["delete_customer"], read_only=False))
        operations = {"query_customers": ToolOperation("customer", "read"),
                      "delete_customer": ToolOperation("customer", "delete")}
        with self.assertRaisesRegex(
                ValueError, r"delete_customer performs delete on any entity \(never\)"):
            BusinessToolRuntime(skills, executor, operations=operations,
                                forbidden_operations=(ForbiddenOperation(
                                    operation="delete", reason="never"),))
        # A rule for another entity leaves this capability installed.
        runtime = BusinessToolRuntime(
            skills, executor, operations=operations,
            forbidden_operations=(ForbiddenOperation(operation="delete", entity="invoice"),))
        self.assertIn("delete_customer", runtime.operations)

    def test_a_rule_without_a_classification_is_refused_rather_than_ignored(self):
        with self.assertRaisesRegex(ValueError, "require an entity and operation"):
            self._runtime(forbidden_operations=(ForbiddenOperation(operation="delete"),))

    def test_a_result_carries_the_server_s_own_entity_and_operation(self):
        runtime = self._runtime(
            operations={"query_customers": ToolOperation("customer", "read")})
        result = runtime.execute("query_customers", {}, {"fixture.query_customers"})
        self.assertEqual(result.audit["server_entity"], "customer")
        self.assertEqual(result.audit["server_operation"], "read")
        # The application's own audit is kept beside it, not replaced.
        self.assertEqual(result.audit["application"], "fixture")
        plain = self._runtime().execute("query_customers", {}, {"fixture.query_customers"})
        self.assertNotIn("server_entity", plain.audit)


class _ManyEntities:
    """An application whose entity count is whatever the deployment happens to have."""

    def __init__(self, count: int):
        self.count = count

    def catalog(self, *, context=None):
        return EntityCatalog(
            binding_digest="a" * 64,
            entities=tuple(EntityDescriptor(
                reference=f"internal.thing.n{index}", label=f"Thing {index}",
                search_text=f"thing {index}") for index in range(self.count)),
            complete=True, scope="entities visible to the fixture actor")

    def fields(self, entity, *, context=None):
        return ()

    def query(self, request, *, context=None):
        raise AssertionError("not called")


class ToolSurfaceSize(unittest.TestCase):
    def test_the_tool_surface_does_not_grow_with_entities(self):
        surfaces = {}
        for count in (1, 10, 400):
            port = _ManyEntities(count)
            tools = EntityReadTools(port, application_id="fixture")
            surfaces[count] = [schema["function"]["name"] for schema in tools.tool_schemas()]
            self.assertEqual(len(port.catalog().entities), count)
        self.assertEqual(len(set(map(tuple, surfaces.values()))), 1)
        self.assertEqual(len(surfaces[400]), len(ENTITY_READ_SCHEMAS))
        self.assertEqual(sorted(surfaces[400]),
                         ["describe_entity", "discover_entities", "query_entity_records"])


if __name__ == "__main__":
    unittest.main()
