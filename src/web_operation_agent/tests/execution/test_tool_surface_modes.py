"""Only unambiguous page-location turns get a smaller, fixed model tool surface."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from web_operation_agent.business.runtime import BusinessToolRuntime
from web_operation_agent.execution.business_tools import business_tool
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control, _obs
from web_operation_agent.tests.execution.product_support import _principal


_PAGE_READS = {"discover_entities", "resolve_record_reference", "find_page_entries"}


def _schema(name):
    return {"type": "function", "function": {
        "name": name, "description": "Fixture read.",
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False}}}


class _GenericExecutor:
    @staticmethod
    def tool_schemas():
        return tuple(_schema(name) for name in sorted(_PAGE_READS | {
            "query_records", "describe_entity", "prepare_record_form"}))

    @staticmethod
    def tool_effects():
        return {schema["function"]["name"]: "read"
                for schema in _GenericExecutor.tool_schemas()}


class _LegacyExecutor:
    @staticmethod
    def tool_schemas():
        return (_schema("query_records"),)

    @staticmethod
    def tool_effects():
        return {"query_records": "read"}


class ToolSurfaceModesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def _session(self, prompt, executor=None, *, missing_page_scope=False):
        executor = executor or _GenericExecutor()
        scopes = None
        if missing_page_scope:
            scopes = {schema["function"]["name"]: (
                "generic_records:read" if schema["function"]["name"] in _PAGE_READS
                else "companies:read") for schema in executor.tool_schemas()}
        tools = BusinessToolRuntime((), executor, required_scopes=scopes,
                                    numeric_goal_binding=True)
        return ServerAgentSession(
            task=TaskSpec("mode", prompt), chat=_Chat([]), browser=_Browser([_obs(1)]),
            control=_Control([[], []]),
            ledger=SessionLedger(Path(self.directory.name) / "audit.jsonl", "mode"),
            business_tools=tools, allow_form_prefill=True,
            principal=_principal() if missing_page_scope else None,
            session_id="mode-session" if missing_page_scope else "",
            turn_id="mode-turn" if missing_page_scope else "")

    @staticmethod
    def _names(session):
        return {schema["function"]["name"] for schema in session.tool_schemas()}

    def test_where_request_keeps_page_resolution_but_hides_data_and_draft_tools(self):
        for prompt in ("产品标签在哪里管理？", "贷项通知单在哪看？",
                       "Where can I find invoices?"):
            with self.subTest(prompt=prompt):
                session = self._session(prompt)
                self.assertTrue(session._page_location_only)
                names = self._names(session)
                self.assertTrue(_PAGE_READS <= names)
                self.assertTrue({"goto", "observe", "set_target", "finish"} <= names)
                self.assertFalse({"query_records", "describe_entity",
                                  "prepare_record_form", "compute", "prefill_form"} & names)
                for schema in session.tool_schemas():
                    if schema["function"]["name"] in _PAGE_READS:
                        parameters = schema["function"]["parameters"]
                        self.assertIn("answer_goals", parameters["required"])
                        self.assertEqual(parameters["properties"]["answer_goals"]["maxItems"], 0)
                first_surface = json.dumps(session.tool_schemas(), sort_keys=True)
                session._answers.first_business_read_seen = True
                self.assertEqual(first_surface,
                                 json.dumps(session.tool_schemas(), sort_keys=True))

    def test_fact_mixed_and_ambiguous_requests_keep_the_full_surface(self):
        for prompt in ("S00006 的客户是谁？", "产品标签在哪里管理？也告诉我谁能修改。",
                       "打开 S00006 的客户是谁在哪里", "用看板看销售订单"):
            with self.subTest(prompt=prompt):
                session = self._session(prompt)
                self.assertFalse(session._page_location_only)
                self.assertIn("query_records", self._names(session))
                self.assertIn("prefill_form", self._names(session))

    def test_apps_without_the_generic_page_contract_keep_existing_tools(self):
        session = self._session("产品标签在哪里管理？", _LegacyExecutor())
        self.assertFalse(session._page_location_only)
        self.assertIn("query_records", self._names(session))

    def test_empty_goal_declaration_reaches_first_page_read_dispatch(self):
        session = self._session("产品标签在哪里管理？")
        with patch("web_operation_agent.execution.business_tools.execute_business_read") as run:
            business_tool(session, SimpleNamespace(step=1), {}, "find_page_entries",
                          {"query": "tags", "answer_goals": []})
        self.assertTrue(run.called)
        self.assertTrue(session._answers.first_business_read_seen)
        self.assertEqual(run.call_args.args[4], {"query": "tags"})

    def test_missing_actor_scope_does_not_hide_other_authorized_reads(self):
        session = self._session("产品标签在哪里管理？", missing_page_scope=True)
        self.assertFalse(session._page_location_only)
        self.assertIn("query_records", self._names(session))


if __name__ == "__main__":
    unittest.main()
