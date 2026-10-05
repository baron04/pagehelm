"""Page-location wording must retain every actor-authorized tool."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from web_operation_agent.business.runtime import BusinessToolRuntime
from web_operation_agent.business.goal_candidates import (
    RESOLVE_GOAL_CANDIDATES_SCHEMA, RESOLVE_GOAL_CANDIDATES_TOOL)
from web_operation_agent.execution.business_tools import business_tool
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.execution.tools import answer_repair_tools
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
        return (RESOLVE_GOAL_CANDIDATES_SCHEMA,) + tuple(_schema(name) for name in (
            "discover_entities", "resolve_record_reference", "find_page_entries",
            "query_records", "describe_entity", "prepare_record_form"))

    @staticmethod
    def tool_effects():
        return {schema["function"]["name"]: "read"
                for schema in _GenericExecutor.tool_schemas()}


class ToolSurfaceTests(unittest.TestCase):
    def test_answer_phase_keeps_optional_source_reads_and_blocks_action_tools(self):
        tools = BusinessToolRuntime((), _GenericExecutor(), numeric_goal_binding=True)
        with tempfile.TemporaryDirectory() as directory:
            session = ServerAgentSession(
                task=TaskSpec('mixed', 'Prefill this form and explain its rule.'),
                chat=_Chat([]), browser=_Browser([_obs(1)]), control=_Control([[], []]),
                ledger=SessionLedger(Path(directory) / 'audit.jsonl', 'mixed'),
                business_tools=tools, allow_form_prefill=True)
            session._finalizing = True
            for runtime_source in (False, True):
                session.runtime_source = runtime_source
                exposed = {s['function']['name'] for s in session.tool_schemas()}
                permitted = answer_repair_tools(session)
                self.assertTrue({'observe', 'finish', 'query_records'} <= exposed)
                self.assertTrue(exposed <= permitted)
                self.assertEqual('lookup_source' in exposed, runtime_source)
                self.assertEqual('lookup_source_history' in permitted, runtime_source)
                self.assertFalse({'goto', 'click', 'fill', 'submit', 'prefill_form',
                    'find_form_entries', 'open_form_entry', 'prepare_form'} & permitted)

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

    def test_goal_candidate_lookup_does_not_prematurely_freeze_numeric_goal(self):
        tools = BusinessToolRuntime((), _GenericExecutor(), numeric_goal_binding=True)
        with tempfile.TemporaryDirectory() as directory:
            session = ServerAgentSession(
                task=TaskSpec("goal", "哪个客户订单最多？"),
                chat=_Chat([]), browser=_Browser([_obs(1)]),
                control=_Control([[], []]),
                ledger=SessionLedger(Path(directory) / "audit.jsonl", "goal"),
                business_tools=tools)
            schemas = {item["function"]["name"]: item
                       for item in session.tool_schemas()}
            self.assertNotIn("answer_goals", schemas[RESOLVE_GOAL_CANDIDATES_TOOL]
                             ["function"]["parameters"]["properties"])
            self.assertIn("answer_goals", schemas["query_records"]
                          ["function"]["parameters"]["required"])
            metric_tools = schemas["query_records"]["function"]["parameters"][
                "properties"]["answer_goals"]["items"]["properties"][
                    "target_read_tool"]["enum"]
            self.assertNotIn(RESOLVE_GOAL_CANDIDATES_TOOL, metric_tools)
            with patch("web_operation_agent.execution.business_tools.execute_business_read") as run:
                business_tool(session, SimpleNamespace(step=1), {},
                              RESOLVE_GOAL_CANDIDATES_TOOL, {"goals": [
                                  {"delivery": "answer", "subject_query": "orders"}]})
            self.assertTrue(run.called)
            self.assertFalse(session._answers.first_business_read_seen)
            with (patch("web_operation_agent.execution.business_tools.execute_business_read") as run,
                  patch.object(session, "_reply", return_value=None) as reply):
                business_tool(session, SimpleNamespace(step=2), {}, "query_records", {
                    "answer_goals": [{"target_read_tool": RESOLVE_GOAL_CANDIDATES_TOOL,
                                      "measures": ["count"]}]})
            self.assertFalse(run.called)
            self.assertIn("Invalid answer goals", reply.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
