"""Page-location wording must retain every actor-authorized tool."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web_operation_agent.business.runtime import BusinessToolRuntime
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control, _obs


def _schema(name):
    return {"type": "function", "function": {
        "name": name, "description": "Fixture read.",
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False}}}


class _GenericExecutor:
    @staticmethod
    def tool_schemas():
        return tuple(_schema(name) for name in (
            "discover_entities", "resolve_record_reference", "find_page_entries",
            "query_records", "describe_entity", "prepare_record_form"))

    @staticmethod
    def tool_effects():
        return {schema["function"]["name"]: "read"
                for schema in _GenericExecutor.tool_schemas()}


class ToolSurfaceTests(unittest.TestCase):
    def test_page_location_request_keeps_the_full_authorized_surface(self):
        tools = BusinessToolRuntime((), _GenericExecutor(), numeric_goal_binding=True)
        with tempfile.TemporaryDirectory() as directory:
            session = ServerAgentSession(
                task=TaskSpec("mode", "产品标签在哪里管理？"),
                chat=_Chat([]), browser=_Browser([_obs(1)]),
                control=_Control([[], []]),
                ledger=SessionLedger(Path(directory) / "audit.jsonl", "mode"),
                business_tools=tools, allow_form_prefill=True)
            names = {schema["function"]["name"] for schema in session.tool_schemas()}
            self.assertTrue({"find_page_entries", "resolve_record_reference",
                             "discover_entities", "query_records", "compute",
                             "prefill_form"} <= names)


if __name__ == "__main__":
    unittest.main()
