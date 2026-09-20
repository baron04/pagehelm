from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.execution import product, turn
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.descriptor import (
    APPLICATION_ADAPTER_VERSION,
    ApplicationDescriptor,
)
from web_operation_agent.composition.ports import (
    AnonymousIdentityPort,
    ApplicationPorts,
    ConservativeMutationReconciler,
    DeclaredDataClassifier,
    ResultPostconditionVerifier,
)
from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.business.runtime import (BusinessSelectionOption, BusinessSkill,
                                          BusinessToolPermissionError, BusinessToolResult,
                                          BusinessToolRuntime)
from web_operation_agent.state.conversation import ConversationState
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.answer.evidence import Origin, RecordEvidence
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.modeling.client import ProviderError
from web_operation_agent.business.mutation import SQLiteMutationJournal
from web_operation_agent.execution.session import NO_LAB
from web_operation_agent.execution.product import (
    AgentTurnConfig,
    AgentTurnError,
    AgentTurnErrorCode,
    AgentTurnExecutor,
    AgentTurnMessage,
    AgentTurnStatus,
)
from web_operation_agent.transport import RemoteSessionClosed
from web_operation_agent.schemas import ApprovalDecision, ObservationEnvelope
from web_operation_agent.tests.business.support import (
    _Browser,
    _Chat,
    _Executor,
    _MutationExecutor,
    _observation,
    _skill,
    _write_skill,
)
from web_operation_agent.tests.route_fixture import (FIXTURE_APPLICATION_ID,
                                                     FIXTURE_ROUTE_PORT, fixture_route)
from web_operation_agent.tests.test_application import _InvoiceAdapter


from web_operation_agent.tests.execution.product_support import (
    _BrokenAfterRunChannel, _Channel, _ClosingAfterRunChannel, _PageChangingBrowser,
    _RemoteBrowser, _message, _principal,
)


class ProductReadAndNavigationTests(unittest.TestCase):
    def setUp(self):
        _RemoteBrowser.instances.clear()
    def test_read_turn_preserves_skill_transition_and_uses_injected_renderer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            tools = BusinessToolRuntime((skill,), _Executor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                (None, {"content": "model prose is not the table renderer"}),
            ])
            channel = _Channel()
            rendered_contexts = []
            allocated_messages = []

            def render_answer(context):
                rendered_contexts.append(context)
                company = context.business_result.content["companies"][0]
                return f"{company['name']} — {company['domain']}"

            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read company data only.",
                    ledger_path=root / "session.jsonl",
                    plain_business_answer_min_page_actions=0,
                    disabled_actions=frozenset({"click", "fill", "select", "submit"}),
                ),
                chat=chat,
                business_tools=tools,
                control=ReadOnlyControl(),
                run_id_factory=lambda message: (
                    allocated_messages.append(message) or "turn-read-001"),
                answer_renderer=render_answer,
            )
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(_message())

            self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
            self.assertEqual(execution.terminal_reason, "agent_finished")
            self.assertEqual(execution.user_answer, "Acme — acme.test")
            self.assertIsInstance(allocated_messages[0], AgentTurnMessage)
            self.assertEqual(rendered_contexts[0].business_result, execution.business_result)
            self.assertEqual(execution.business_result.content["matched_count"], 1)
            self.assertFalse(execution.answer_fallback_used)
            self.assertTrue(_RemoteBrowser.instances[0].started)
            self.assertFalse(
                _RemoteBrowser.instances[0].closed,
                "one turn must not close the conversation-owned browser channel",
            )

            self.assertNotIn("query_companies", chat.tool_names[0])
            self.assertIn("query_companies", chat.tool_names[1])
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["event"], "product_turn_finished")
            public_result = execution.to_json()
            self.assertEqual(public_result["status"], "finished")
            self.assertEqual(public_result["business_result"]["route_count"], 1)
            self.assertNotIn("content", public_result["business_result"])
            self.assertNotIn("ledger_path", public_result)
            self.assertNotIn("product_success", public_result)
            # `step` entries are the work itself being reported as it happens; what this
            # pins is the shape of the turn around them.
            self.assertEqual(
                [item[1]["state"] for item in channel.published
                 if item[1]["state"] != "step"],
                ["agent_started", "finished"],
            )
            self.assertIn("正在读取业务数据", " ".join(
                item[1].get("message", "") for item in channel.published))
            # Every entry a turn produces belongs to that turn. Published without one,
            # `agent_started` and the terminal answer carried an empty turn id, and the
            # panel -- which groups the timeline by it -- broke one exchange into four:
            # the goal alone, a step with no goal, the actions, then the answer.
            self.assertEqual({item[2] for item in channel.published}, {"message-1"})

    def test_exact_unique_read_skill_and_verified_route_finish_without_bookend_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            chat = _Chat([
                ("query_companies", {}),
                ("goto", {"url": "/object/company/company-1"}),
            ])
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read and open one company.",
                    ledger_path=root / "session.jsonl",
                    deterministic_read_navigation=True,
                ),
                chat=chat,
                business_tools=BusinessToolRuntime((skill,), _Executor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-fast-navigation",
            )

            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.execute({
                    "message_id": "message-1",
                    "text": "打开公司 Acme",
                    "page_url": "http://app.test/companies",
                })

            self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
            self.assertEqual(execution.user_answer, "已打开请求的页面。")
            self.assertEqual(len(chat.tool_names), 2)
            self.assertIn("query_companies", chat.tool_names[0])
            self.assertNotIn("load_skill", chat.tool_names[0])
            self.assertEqual(_RemoteBrowser.instances[-1].actions, ["goto"])
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            self.assertEqual(
                sum(item["event"] == "business_skill_auto_loaded" for item in events), 1)
            self.assertEqual(
                sum(item["event"] == "deterministic_navigation_completed"
                    for item in events), 1)

    def test_current_route_and_unique_result_remove_all_extra_navigation_model_calls(self):
        class UniqueRouteExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                return dataclasses.replace(
                    _Executor.execute(name, args, context=context),
                    navigation_target="/object/company/company-1")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            chat = _Chat([("query_companies", {})])
            runtime = BusinessToolRuntime(
                (skill,), UniqueRouteExecutor(),
                route_skill_hints=((r"^/companies$", skill.skill_id),))
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read and open one company.",
                    ledger_path=root / "session.jsonl",
                    deterministic_read_navigation=True,
                ),
                chat=chat, business_tools=runtime, control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-route-context-navigation",
            )

            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.execute({
                    "message_id": "message-1", "text": "打开 Acme",
                    "page_url": "http://app.test/companies",
                })

            self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
            self.assertEqual(execution.user_answer, "已打开请求的页面。")
            self.assertEqual(len(chat.tool_names), 1)
            self.assertIn("query_companies", chat.tool_names[0])
            self.assertEqual(_RemoteBrowser.instances[-1].actions, ["goto"])
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            candidates = next(item for item in events
                              if item["event"] == "business_skill_candidates")
            self.assertTrue(candidates["route_context_used"])
            self.assertFalse(candidates["fallback_used"])
            read = next(item for item in events
                        if item["event"] == "business_tool_result")
            self.assertEqual(len(read["navigation_target_digest"]), 64)
            self.assertNotIn("/object/company", json.dumps(read))
            completed = next(item for item in events
                             if item["event"] == "deterministic_navigation_completed")
            self.assertEqual(completed["source"], "business_result")

    def test_fallback_skill_still_requires_model_selection_but_skips_final_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("goto", {"url": "/object/company/company-1"}),
            ])
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read and open one company.",
                    ledger_path=root / "session.jsonl",
                    deterministic_read_navigation=True,
                ),
                chat=chat,
                business_tools=BusinessToolRuntime((skill,), _Executor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-fallback-navigation",
            )

            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.execute({
                    "message_id": "message-1", "text": "打开 Acme",
                    "page_url": "http://app.test/companies",
                })

            self.assertEqual(execution.user_answer, "已打开请求的页面。")
            self.assertEqual(len(chat.tool_names), 3)
            self.assertIn("load_skill", chat.tool_names[0])
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            self.assertFalse(any(
                item["event"] == "business_skill_auto_loaded" for item in events))

    def test_navigation_with_an_answer_goal_does_not_take_the_fast_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("goto", {"url": "/object/company/company-1"}),
                (None, {"content": "已打开 Acme；域名是 acme.test。"}),
            ])
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read and open one company.",
                    ledger_path=root / "session.jsonl",
                    deterministic_read_navigation=True,
                    plain_business_answer_min_page_actions=0,
                ),
                chat=chat,
                business_tools=BusinessToolRuntime((skill,), _Executor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-navigation-and-answer",
            )

            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.execute({
                    "message_id": "message-1",
                    "text": "打开公司 Acme，并告诉我域名",
                    "page_url": "http://app.test/companies",
                })

            self.assertEqual(execution.user_answer, "已打开 Acme；域名是 acme.test。")
            self.assertEqual(len(chat.tool_names), 4)

    def test_verified_route_must_be_the_fresh_page_before_fast_finish(self):
        class RedirectedBrowser(_RemoteBrowser):
            def execute(self, action, args=None, expect_navigation=False):
                receipt = super().execute(action, args, expect_navigation)
                if action != "goto":
                    return receipt
                self.url = "http://app.test/login"
                return dataclasses.replace(
                    receipt, url_after=self.url,
                    observation=_observation(self.epoch, self.url))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            chat = _Chat([
                ("query_companies", {}),
                ("goto", {"url": "/object/company/company-1"}),
                (None, {"content": "没有打开目标记录。"}),
            ])
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read and open one company.",
                    ledger_path=root / "session.jsonl",
                    deterministic_read_navigation=True,
                    plain_business_answer_min_page_actions=0,
                ),
                chat=chat,
                business_tools=BusinessToolRuntime((skill,), _Executor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-redirected-navigation",
            )

            with patch.object(turn, "RemoteBrowserClient", RedirectedBrowser):
                execution = executor.execute({
                    "message_id": "message-1", "text": "打开公司 Acme",
                    "page_url": "http://app.test/companies",
                })

            self.assertEqual(execution.user_answer, "没有打开目标记录。")
            self.assertEqual(len(chat.tool_names), 3)
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            self.assertFalse(any(
                item["event"] == "deterministic_navigation_completed"
                for item in events))

    def test_a_related_record_request_does_not_finish_on_the_first_unique_read(self):
        # "Open the company of order SO-1" names two collections. The order read returns one
        # record with a route, but that record is only the way to the company; opening it and
        # reporting "opened the requested page" would be a false success.
        order_schema = {"type": "function", "function": {
            "name": "query_orders", "description": "Read orders.",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False}}}

        class OrderAndCompanyExecutor(_Executor):
            @staticmethod
            def tool_schemas():
                return (*_Executor.tool_schemas(), order_schema)

            @staticmethod
            def tool_effects():
                return {**_Executor.tool_effects(), "query_orders": "read"}

            @staticmethod
            def execute(name, args, *, context=None):
                if name != "query_orders":
                    return _Executor.execute(name, args, context=context)
                return BusinessToolResult(
                    content={"orders": [{"name": "SO-1", "company": "Acme",
                                         "ui_path": "/object/order/order-1"}],
                             "matched_count": 1, "source_total": 1,
                             "scope": "current_delegated_workspace",
                             "observed_at": "2026-09-08T05:00:00+00:00",
                             "completeness": {"source_total_known": True,
                                              "source_complete": True,
                                              "result_truncated": False}},
                    audit={"application": "fixture", "operation": name,
                           "oauth_actor": "delegated_user", "returned_count": 1,
                           "scope": "current_delegated_workspace",
                           "source_complete": True},
                    allowed_routes=("/object/order/order-1",),
                    navigation_target="/object/order/order-1")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            companies = _skill(root / "query-skill.json")
            (root / "orders-skill.json").write_text(json.dumps({
                "schema_version": 1, "skill_id": "fixture.orders-query",
                "title": "Query orders", "intent": "Read current orders.",
                "triggers": ["orders", "订单"],
                "instructions": ["Call query_orders and trust its current result."],
                "tool_names": ["query_orders"],
                "provenance": ["src:fixture@v1:orders.json#orders"],
                "read_only": True,
            }) + "\n")
            orders = BusinessSkill.load(root / "orders-skill.json")
            chat = _Chat([
                ("load_skill", {"skill_id": orders.skill_id}),
                ("query_orders", {}),
                ("load_skill", {"skill_id": companies.skill_id}),
                ("query_companies", {}),
                ("goto", {"url": "/object/company/company-1"}),
                (None, {"content": "已打开订单 SO-1 对应的公司 Acme 的页面。"}),
            ])
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read orders and companies and open their pages.",
                    ledger_path=root / "session.jsonl",
                    deterministic_read_navigation=True,
                    plain_business_answer_min_page_actions=0,
                ),
                chat=chat,
                business_tools=BusinessToolRuntime((orders, companies),
                                                   OrderAndCompanyExecutor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-related-record-navigation",
            )

            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.execute({
                    "message_id": "message-1", "text": "打开订单 SO-1 对应的公司页面",
                    "page_url": "http://app.test/companies",
                })

            self.assertEqual(_RemoteBrowser.instances[-1].url,
                             "http://app.test/object/company/company-1")
            self.assertEqual(execution.user_answer, "已打开订单 SO-1 对应的公司 Acme 的页面。")
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            candidates = next(item for item in events
                              if item["event"] == "business_skill_candidates")
            self.assertEqual(len(candidates["candidate_ids"]), 2)
            self.assertFalse(any(item["event"] == "deterministic_navigation_completed"
                                 for item in events))

    def test_turn_answer_limit_reaches_the_session_before_product_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            long_answer = "A" * 1_200
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                (None, {"content": long_answer}),
            ])
            channel = _Channel()
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read company data only.",
                    ledger_path=root / "session.jsonl",
                    plain_business_answer_min_page_actions=0,
                    max_user_answer_chars=2_000,
                ),
                chat=chat,
                business_tools=BusinessToolRuntime((skill,), _Executor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-long-answer-001",
            )
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(_message())

            self.assertEqual(execution.user_answer, long_answer)
            self.assertEqual(execution.agent.agent_said, long_answer)
            self.assertEqual(channel.published[-1][1]["message"], long_answer)

    def test_typed_query_projects_facts_and_completeness_instead_of_model_prose(self):
        class ProjectedExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                result = _Executor.execute(name, args, context=context)
                return BusinessToolResult(
                    content=result.content,
                    audit=result.audit,
                    allowed_routes=result.allowed_routes,
                    query_answer=StructuredQueryAnswer(
                        collection="companies",
                        fields=("name", "domain"),
                        records=(("Acme", "acme.test"),),
                        source="fixture typed API",
                        scope="current_workspace",
                        observed_at="2026-09-10T00:00:00+00:00",
                        source_complete=True,
                        result_truncated=False,
                        matched_count=1,
                        source_total=1,
                        records_scanned=1,
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                (None, {"content": "Invented Co is definitely the complete result."}),
            ])
            channel = _Channel()
            custom_renderer_calls = []
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read company data only.",
                    ledger_path=root / "session.jsonl",
                    plain_business_answer_min_page_actions=0,
                ),
                chat=chat,
                business_tools=BusinessToolRuntime((skill,), ProjectedExecutor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-structured-answer-001",
                answer_renderer=lambda _context: (
                    custom_renderer_calls.append(True) or "untrusted replacement"),
            )
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(_message())

            self.assertIn("Acme", execution.user_answer)
            self.assertIn("acme.test", execution.user_answer)
            self.assertIn("Query result: companies (1 record)", execution.user_answer)
            self.assertNotIn("Completeness", execution.user_answer)
            self.assertNotIn("Invented Co", execution.user_answer)
            self.assertEqual(custom_renderer_calls, [])
            projection = execution.to_json()["business_result"]["answer_projection"]
            self.assertEqual(projection["records"], [
                {"name": "Acme", "domain": "acme.test"}])
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            event = next(item for item in events
                         if item["event"] == "structured_query_answer_rendered")
            self.assertFalse(event["model_facts_used"])
            self.assertFalse(event["presentation_truncated"])

    def test_query_projection_does_not_replace_a_later_navigation_completion(self):
        result = BusinessToolResult(
            content={"companies": [{"name": "Acme", "domain": "acme.test"}]},
            audit={},
            query_answer=StructuredQueryAnswer(
                collection="companies", fields=("name", "domain"),
                records=(("Acme", "acme.test"),), source="fixture typed API",
                scope="workspace", observed_at="2026-09-10T00:00:00+00:00",
                source_complete=True, result_truncated=False, matched_count=1,
                source_total=1, records_scanned=1,
            ),
        )
        context = product.AgentAnswerContext(
            message=AgentTurnMessage.from_wire(_message()),
            agent=product.RunResult(
                run_id="run", task_ref="task", arm="U",
                terminal_reason="agent_finished", agent_said="Opened Acme."),
            business_result=result,
            max_chars=4_000,
        )

        self.assertEqual(product.render_neutral_user_answer(context), "Opened Acme.")
