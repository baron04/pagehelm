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

from web_operation_agent import product
from web_operation_agent.application import (APPLICATION_ADAPTER_VERSION,
                                             AnonymousIdentityPort,
                                             ApplicationDescriptor,
                                             ApplicationPorts,
                                             ConservativeMutationReconciler,
                                             DeclaredDataClassifier,
                                             ResultPostconditionVerifier,
                                             bind_application)
from web_operation_agent.answering import StructuredQueryAnswer
from web_operation_agent.business import (BusinessSelectionOption, BusinessSkill,
                                          BusinessToolPermissionError, BusinessToolResult,
                                          BusinessToolRuntime)
from web_operation_agent.conversation import ConversationState
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.evidence import Origin, RecordEvidence
from web_operation_agent.identity import PrincipalContext
from web_operation_agent.model import ProviderError
from web_operation_agent.mutation import SQLiteMutationJournal
from web_operation_agent.session import NO_LAB
from web_operation_agent.product import (
    AgentTurnConfig,
    AgentTurnError,
    AgentTurnErrorCode,
    AgentTurnExecutor,
    AgentTurnMessage,
    AgentTurnStatus,
)
from web_operation_agent.remote import RemoteSessionClosed
from web_operation_agent.schemas import ApprovalDecision, ObservationEnvelope
from web_operation_agent.tests.test_business import (
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


class _Channel:
    def __init__(self, *, page_url: str = "http://app.test/companies",
                 observation: bool = True, decision: str = "approved",
                 cancelled: bool = False,
                 principal: PrincipalContext | None = None):
        self.session_id = "product-session"
        self.new_conversation_pending = False
        self.published = []
        self.approvals = []
        self.decision = decision
        self.cancelled = cancelled
        self.principal = principal
        self.latest_observation_envelope = (
            self.envelope(1, page_url) if observation else None)

    def message_cancelled(self, _message_id: str) -> bool:
        return self.cancelled

    def take_new_conversation(self) -> bool:
        pending = self.new_conversation_pending
        self.new_conversation_pending = False
        return pending

    def envelope(self, epoch: int, page_url: str) -> ObservationEnvelope:
        return ObservationEnvelope(
            schema_version=1,
            session_id=self.session_id,
            observation_id=f"observation-{epoch}",
            page_epoch=epoch,
            observation=_observation(epoch, page_url),
        )

    def publish(self, kind: str, payload: dict, *, turn_id: str = ""):
        self.published.append((kind, payload, turn_id))

    def request_approval(self, request):
        self.approvals.append(request)

    def wait_for_approval(self, approval_id: str, *, timeout_s: float):
        self.last_approval_timeout = timeout_s
        request = next(item for item in self.approvals
                       if item.approval_id == approval_id)
        return ApprovalDecision(
            schema_version=1,
            session_id=self.session_id,
            approval_id=approval_id,
            decision=self.decision,
            principal_digest=request.principal_digest,
            turn_id=request.turn_id,
            operation_id=request.operation_id,
        )


class _RemoteBrowser(_Browser):
    instances = []

    def __init__(self, channel, *, timeout_s, turn_id=""):
        super().__init__()
        self.channel = channel
        self.timeout_s = timeout_s
        self.turn_id = turn_id
        self.started = False
        self.closed = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True
        return {"ok": True, "embedded": True}

    def close(self):
        self.closed = True


class _PageChangingBrowser(_RemoteBrowser):
    def start(self):
        result = super().start()
        self.channel.latest_observation_envelope = self.channel.envelope(
            2, "http://app.test/companies")
        return result


class _ClosingAfterRunChannel(_Channel):
    def publish(self, kind: str, payload: dict, *, turn_id: str = ""):
        if payload.get("state") != "agent_started":
            raise RemoteSessionClosed()
        return super().publish(kind, payload, turn_id=turn_id)


class _BrokenAfterRunChannel(_Channel):
    def publish(self, kind: str, payload: dict, *, turn_id: str = ""):
        if payload.get("state") != "agent_started":
            raise RuntimeError("event transport failed")
        return super().publish(kind, payload, turn_id=turn_id)


def _message(page_url: str = "http://app.test/companies") -> dict:
    return {
        "message_id": "message-1",
        "text": "Which companies are available?",
        "page_url": page_url,
    }


def _principal(*, application_id: str = "fixture") -> PrincipalContext:
    now = int(__import__("time").time() * 1000)
    return PrincipalContext(
        1, application_id, "tenant-private", "workspace-private", "actor-private",
        "delegation-private", now - 1_000, now + 60_000,
        ("companies:read", "companies:write", "page:operate"),
    )


class AgentTurnConfigTests(unittest.TestCase):
    def test_config_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turn.jsonl"
            config = AgentTurnConfig(
                supported_scope="Read company data.", ledger_path=path)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                config.step_budget = 9

    def test_config_rejects_unsafe_or_unbounded_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turn.jsonl"
            for kwargs in (
                {"supported_scope": ""},
                {"step_budget": True},
                {"wall_budget_s": 0},
                {"transport_timeout_s": float("inf")},
                {"approval_ttl_s": 601},
                {"disabled_actions": {"fill"}},
                {"scope_first_policy": 1},
                {"allow_application_entry": "yes"},
                {"allow_business_mutations": 1},
                {"allow_clarification": 1},
                {"allow_form_prefill": 1},
                {"deterministic_read_navigation": 1},
                {"no_progress_repeat_limit": 1},
                {"require_identity": 1},
                {"deployment_profile": "staging"},
                {"plain_business_answer_min_page_actions": -1},
                {"max_user_answer_chars": 511},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    AgentTurnConfig(
                        supported_scope=kwargs.pop(
                            "supported_scope", "Read company data."),
                        ledger_path=path,
                        **kwargs,
                    )

    def test_mutation_policy_requires_identity_even_in_the_test_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "verified session identity"):
                AgentTurnConfig(
                    supported_scope="Create one company.",
                    ledger_path=Path(tmp) / "turn.jsonl",
                    allow_business_mutations=True,
                    deployment_profile="test",
                )

    def test_production_mutation_composition_requires_a_durable_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _write_skill(root / "write-skill.json")
            memory_tools = BusinessToolRuntime((skill,), _MutationExecutor())
            config = AgentTurnConfig(
                supported_scope="Create one company.",
                ledger_path=root / "turn.jsonl",
                allow_business_mutations=True,
                require_identity=True,
            )
            with self.assertRaisesRegex(ValueError, "configured mutation tool"):
                AgentTurnExecutor(
                    channel=_Channel(principal=_principal()), config=config,
                    chat=_Chat([]), business_tools=None,
                    control=ReadOnlyControl(),
                    run_id_factory=lambda _message: "missing-write-tool",
                )
            with self.assertRaisesRegex(ValueError, "durable mutation journal"):
                AgentTurnExecutor(
                    channel=_Channel(principal=_principal()), config=config,
                    chat=_Chat([]), business_tools=memory_tools,
                    control=ReadOnlyControl(),
                    run_id_factory=lambda _message: "unsafe-write",
                )

            durable_tools = BusinessToolRuntime(
                (skill,), _MutationExecutor(),
                mutation_journal=SQLiteMutationJournal(root / "mutations.sqlite3"),
            )
            executor = AgentTurnExecutor(
                channel=_Channel(principal=_principal()), config=config,
                chat=_Chat([]), business_tools=durable_tools,
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "durable-write",
            )
            self.assertIs(executor.business_tools, durable_tools)

    def test_memory_journal_requires_an_explicit_test_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _write_skill(root / "write-skill.json")
            tools = BusinessToolRuntime((skill,), _MutationExecutor())
            executor = AgentTurnExecutor(
                channel=_Channel(principal=_principal()),
                config=AgentTurnConfig(
                    supported_scope="Create one company.",
                    ledger_path=root / "turn.jsonl",
                    allow_business_mutations=True,
                    require_identity=True,
                    deployment_profile="test",
                ),
                chat=_Chat([]), business_tools=tools,
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "test-write",
            )
            self.assertIs(executor.business_tools, tools)


class AgentTurnBoundaryTests(unittest.TestCase):
    def _executor(self, root: Path, channel: _Channel) -> AgentTurnExecutor:
        return AgentTurnExecutor(
            channel=channel,
            config=AgentTurnConfig(
                supported_scope="Read company data.",
                ledger_path=root / "session.jsonl",
            ),
            chat=_Chat([]),
            business_tools=None,
            control=ReadOnlyControl(),
            run_id_factory=lambda _message: "turn-boundary",
        )

    def test_invalid_or_unbound_messages_raise_typed_errors_without_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel = _Channel(observation=False)
            with self.assertRaises(AgentTurnError) as caught:
                self._executor(root, channel).run_once(_message())
            self.assertEqual(
                caught.exception.code, AgentTurnErrorCode.OBSERVATION_UNAVAILABLE)
            self.assertTrue(caught.exception.retryable)
            self.assertFalse((root / "session.jsonl").exists())

            channel = _Channel()
            with self.assertRaises(AgentTurnError) as caught:
                self._executor(root, channel).run_once(
                    {**_message(), "unexpected": True})
            self.assertEqual(caught.exception.code, AgentTurnErrorCode.INVALID_MESSAGE)

    def test_message_from_an_old_page_is_rejected_before_a_run_id_is_allocated(self):
        channel = _Channel(page_url="http://app.test/current")
        called = []
        with tempfile.TemporaryDirectory() as tmp:
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read company data.",
                    ledger_path=Path(tmp) / "session.jsonl"),
                chat=_Chat([]), business_tools=None, control=ReadOnlyControl(),
                run_id_factory=lambda message: called.append(message) or "must-not-run",
            )
            with self.assertRaises(AgentTurnError) as caught:
                executor.run_once(_message("http://app.test/old"))
        self.assertEqual(caught.exception.code, AgentTurnErrorCode.STALE_MESSAGE)
        self.assertEqual(called, [])

    def test_bound_application_rejects_another_origin_before_browser_or_artifacts(self):
        called = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read invoices.",
                    ledger_path=root / "session.jsonl"),
                chat=_Chat([]), application=bind_application(_InvoiceAdapter()),
                control=ReadOnlyControl(),
                run_id_factory=lambda message: called.append(message) or "must-not-run",
            )
            with self.assertRaises(AgentTurnError) as caught:
                executor.run_once(_message())
        self.assertEqual(
            caught.exception.code, AgentTurnErrorCode.APPLICATION_MISMATCH)
        self.assertEqual(called, [])
        self.assertFalse((root / "session.jsonl").exists())

    def test_identity_required_rejects_an_unbound_channel_before_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read company data.",
                    ledger_path=root / "session.jsonl", require_identity=True),
                chat=_Chat([]), business_tools=None, control=ReadOnlyControl(),
                run_id_factory=lambda _message: "must-not-run",
            )
            with self.assertRaises(AgentTurnError) as caught:
                executor.run_once(_message())
            self.assertEqual(
                caught.exception.code, AgentTurnErrorCode.IDENTITY_UNAVAILABLE)
            self.assertFalse((root / "session.jsonl").exists())

    def test_page_change_during_browser_start_is_rejected_before_planning(self):
        channel = _Channel()
        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(Path(tmp), channel)
            with patch.object(product, "RemoteBrowserClient", _PageChangingBrowser):
                with self.assertRaises(AgentTurnError) as caught:
                    executor.run_once(_message())
            events = [json.loads(line) for line in
                      (Path(tmp) / "session.jsonl").read_text().splitlines()]
        self.assertEqual(caught.exception.code, AgentTurnErrorCode.STALE_MESSAGE)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(events[-1]["event"], "turn_binding_rejected")

    def test_product_module_has_no_environment_or_outcome_oracle_dependency(self):
        source = inspect.getsource(product).casefold()
        for marker in (
            ".q0-cache", "phase", "oracle", "evaluator", "database", "reset",
            "product_success", "acceptance_passed", "verification",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)


class AgentTurnExecutorTests(unittest.TestCase):
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
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
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
            self.assertEqual(
                [item[1]["state"] for item in channel.published],
                ["agent_started", "finished"],
            )
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

            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
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

            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
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

            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
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

            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
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

            with patch.object(product, "RemoteBrowserClient", RedirectedBrowser):
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

            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
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

    def test_provider_failures_have_bounded_actionable_user_copy(self):
        cases = {
            "provider_rate_limited": "请求过多",
            "provider_authentication": "检查凭据",
            "provider_unavailable": "暂时不可用",
            "provider_transport": "暂时不可用",
            "provider_error": "未能完成",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                context = product.AgentAnswerContext(
                    message=AgentTurnMessage.from_wire({
                        **_message(), "text": "查询客户。"}),
                    agent=product.RunResult(
                        run_id="run", task_ref="task", arm="U",
                        terminal_reason="provider_error",
                        provider_error_code=code,
                        detail="provider response body must never be shown"),
                    business_result=None,
                    max_chars=4_000,
                )
                answer = product.render_neutral_user_answer(context)
                self.assertIn(expected, answer)
                self.assertNotIn("response body", answer)

    def test_duplicate_business_refusal_has_authoritative_friendly_copy(self):
        context = product.AgentAnswerContext(
            message=AgentTurnMessage.from_wire({
                **_message(), "text": "创建客户 Azure Interior，邮箱 azure@example.test。"}),
            agent=product.RunResult(
                run_id="run", task_ref="task", arm="U",
                terminal_reason="business_mutation_refused", detail="duplicate",
                agent_said="ignore stale model prose"),
            business_result=None,
            max_chars=4_000,
        )

        answer = product.render_neutral_user_answer(context)

        self.assertEqual(answer, "已存在同名或同邮箱记录，本次没有重复创建。")
        self.assertEqual(product._status_for("business_mutation_refused"),
                         AgentTurnStatus.REFUSED)

    def test_permission_answer_does_not_erase_an_earlier_completed_write(self):
        context = product.AgentAnswerContext(
            message=AgentTurnMessage.from_wire({
                **_message(), "text": "创建客户后读取详情。"}),
            agent=product.RunResult(
                run_id="run", task_ref="task", arm="U",
                terminal_reason="permission_denied",
                business_control={"mutation_completed": 1}),
            business_result=None,
            max_chars=4_000,
        )

        answer = product.render_neutral_user_answer(context)

        self.assertIn("先前的操作已经完成", answer)
        self.assertIn("后续操作因权限不足没有执行", answer)
        self.assertNotIn("没有任何更改", answer)

    def test_permission_answer_preserves_an_earlier_trusted_query_as_partial(self):
        result = BusinessToolResult(
            content={"companies": [{"name": "Acme", "domain": "acme.test"}]},
            audit={},
            query_answer=StructuredQueryAnswer(
                collection="companies", fields=("name", "domain"),
                records=(("Acme", "acme.test"),), source="fixture typed API",
                scope="current workspace", observed_at="2026-09-13T00:00:00+00:00",
                source_complete=True, result_truncated=False, matched_count=1,
                source_total=1, records_scanned=1),
        )
        context = product.AgentAnswerContext(
            message=AgentTurnMessage.from_wire({
                **_message(), "text": "先列出公司，再读取无权访问的详情。"}),
            agent=product.RunResult(
                run_id="run", task_ref="task", arm="U",
                terminal_reason="permission_denied"),
            business_result=result,
            max_chars=4_000,
        )

        answer = product.render_neutral_user_answer(context)

        self.assertIn("Acme", answer)
        self.assertIn("acme.test", answer)
        self.assertIn("已回答目前可以确认的部分", answer)
        self.assertIn("后续读取因权限不足没有执行", answer)

    def test_a_later_refused_read_keeps_the_earlier_query_for_the_reader(self):
        class OneReadThenRefusal(_Executor):
            calls = 0

            def execute(self, name, args, *, context=None):
                self.calls += 1
                if self.calls > 1:
                    raise BusinessToolPermissionError("the application refused the detail")
                return BusinessToolResult(
                    content={"companies": [{"name": "Acme", "domain": "acme.test"}]},
                    audit={},
                    query_answer=StructuredQueryAnswer(
                        collection="companies", fields=("name", "domain"),
                        records=(("Acme", "acme.test"),), source="fixture typed API",
                        scope="current workspace",
                        observed_at="2026-09-13T00:00:00+00:00",
                        source_complete=True, result_truncated=False, matched_count=1,
                        source_total=1, records_scanned=1),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("query_companies", {}),
                # The refusal comes back to the model (owner ruling D13). This adapter
                # reports no answer evidence, so there is nothing for a typed answer to
                # cite, and prose cannot stand in for it: the turn ends on the refusal and
                # the reader still gets what was read before it.
                ("finish", {"done": True, "why": "Listed the companies."}),
            ])
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read company data only.",
                    ledger_path=root / "session.jsonl",
                    no_progress_repeat_limit=None),
                chat=chat,
                business_tools=BusinessToolRuntime((skill,), OneReadThenRefusal()),
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "permission-partial-001")
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once({
                    **_message(), "text": "先列出公司，再读取无权访问的详情。"})

            self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
            self.assertEqual(execution.terminal_reason, "permission_denied")
            self.assertIn("Acme", execution.user_answer)
            self.assertIn("已回答目前可以确认的部分", execution.user_answer)
            self.assertEqual(len(chat.messages), 4, "the refusal is returned once")
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            rendered = next(item for item in events
                            if item["event"] == "structured_query_answer_rendered")
            self.assertTrue(rendered["partial_due_to_permission"])

    def test_second_turn_resolves_reference_but_must_requery_before_opening(self):
        class TwoCompanyExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                if name != "query_companies" or args:
                    raise AssertionError("unexpected business call")
                return BusinessToolResult(
                    content={
                        "companies": [
                            {"name": "First Co", "domain": "first.test",
                             "ui_path": "/object/company/first"},
                            {"name": "Second Co", "domain": "second.test",
                             "ui_path": "/object/company/second"},
                        ],
                        "matched_count": 2,
                        "source_total": 2,
                        "completeness": {"source_complete": True,
                                         "result_truncated": False},
                    },
                    audit={"application": "fixture", "operation": name,
                           "source_complete": True},
                    allowed_routes=(
                        "/object/company/first", "/object/company/second"),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            state = ConversationState()
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                (None, {"content": "First Co and Second Co."}),
                ("goto", {"url": "/object/company/second"}),
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("goto", {"url": "/object/company/second"}),
                (None, {"content": "Opened Second Co."}),
            ])
            channel = _Channel()
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read and open one company.",
                    ledger_path=root / "session.jsonl",
                    plain_business_answer_min_page_actions=0,
                ),
                chat=chat,
                business_tools=BusinessToolRuntime((skill,), TwoCompanyExecutor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda message: f"turn-{message.message_id}",
                conversation_state=state,
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                first = executor.execute({
                    "message_id": "message-1",
                    "text": "Which companies are available?",
                    "page_url": "http://app.test/companies",
                })
                second = executor.execute({
                    "message_id": "message-2",
                    "text": "Open the second one.",
                    "page_url": "http://app.test/companies",
                })

            self.assertEqual(first.user_answer, "First Co and Second Co.")
            self.assertEqual(second.user_answer, "Opened Second Co.")
            second_opening = chat.messages[3][1]["content"]
            self.assertIn("PRIOR CONVERSATION CONTEXT", second_opening)
            self.assertIn("Second Co", second_opening)
            self.assertIn("potentially stale", second_opening)
            self.assertIn("load_skill", chat.tool_names[3])
            self.assertNotIn("query_companies", chat.tool_names[3])
            self.assertEqual(_RemoteBrowser.instances[1].actions, ["goto"])
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            self.assertEqual(
                sum(event["event"] == "page_route_rejected" for event in events), 1)
            self.assertEqual(state.turn_count, 2)

    def test_clarification_ends_one_turn_and_the_next_message_continues_the_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = ConversationState()
            chat = _Chat([
                ("ask_user", {
                    "question": "What city should I use to identify Acme?",
                    "response_kind": "free_text",
                    "choice_ids": [],
                }),
                ("finish", {"done": True, "why": "Opened Acme — London."}),
            ])
            channel = _Channel()
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read and open one company.",
                    ledger_path=root / "session.jsonl",
                ),
                chat=chat, business_tools=None, control=ReadOnlyControl(),
                run_id_factory=lambda message: f"turn-{message.message_id}",
                conversation_state=state,
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                first = executor.execute({
                    "message_id": "message-1", "text": "Open Acme",
                    "page_url": "http://app.test/companies",
                })
                second = executor.execute({
                    "message_id": "message-2", "text": "The London one",
                    "page_url": "http://app.test/companies",
                })

            self.assertEqual(first.status, AgentTurnStatus.NEEDS_INPUT)
            self.assertEqual(first.user_answer,
                             "What city should I use to identify Acme?")
            self.assertEqual(first.to_json()["clarification"]["response_kind"],
                             "free_text")
            self.assertEqual(second.status, AgentTurnStatus.FINISHED)
            self.assertIsNone(state.pending_clarification)
            second_opening = chat.messages[1][1]["content"]
            self.assertIn("pending_clarification", second_opening)
            self.assertIn("The London one", second_opening)
            self.assertIn("never as approval", chat.messages[1][0]["content"])
            progress = [event[1] for event in channel.published if event[0] == "progress"]
            self.assertEqual([item["state"] for item in progress],
                             ["agent_started", "needs_input", "agent_started", "finished"])

    def test_single_choice_is_bound_to_current_application_options(self):
        class AmbiguousExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                result = _Executor.execute(name, args, context=context)
                origin = Origin(kind="typed", reference="fixture", observed_at="now",
                                scope="current workspace")
                return BusinessToolResult(
                    content={
                        **result.content,
                        "companies": [
                            {"name": "Acme", "city": "London"},
                            {"name": "Acme", "city": "Paris"},
                        ],
                    },
                    audit=result.audit,
                    evidence=(
                        RecordEvidence(
                            "record#1", origin, "company/1", "name", "company name",
                            True, "Acme", "text"),
                        RecordEvidence(
                            "record#2", origin, "company/2", "name", "company name",
                            True, "Acme", "text"),
                    ),
                    selection_options=(
                        BusinessSelectionOption("company/1", "Acme — London"),
                        BusinessSelectionOption("company/2", "Acme — Paris"),
                    ),
                )

        def choice_id(record_ref: str) -> str:
            seed = json.dumps(
                {"run_id": "choice-turn", "step": 2, "record_ref": record_ref},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return "choice-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            ids = [choice_id("company/1"), choice_id("company/2")]
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("ask_user", {
                    "question": "Which Acme should I open?",
                    "response_kind": "single_choice",
                    "choice_ids": ids,
                }),
            ])
            state = ConversationState()
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read and open one company.",
                    ledger_path=root / "session.jsonl",
                ),
                chat=chat,
                business_tools=BusinessToolRuntime((skill,), AmbiguousExecutor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "choice-turn",
                conversation_state=state,
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute(_message())

            self.assertEqual(result.status, AgentTurnStatus.NEEDS_INPUT)
            self.assertEqual(
                result.user_answer,
                "Which Acme should I open?\n1. Acme — London\n2. Acme — Paris")
            pending = state.pending_clarification
            self.assertEqual([item["record_ref"] for item in pending["choices"]],
                             ["company/1", "company/2"])
            public = result.to_json()["clarification"]
            self.assertNotIn("record_ref", json.dumps(public))
            self.assertNotIn("record_ref", json.dumps(result.agent.to_json()))
            self.assertIn("CLARIFICATION CANDIDATES", chat.messages[2][-1]["content"])
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            candidate_event = next(
                event for event in events
                if event["event"] == "clarification_candidates_ready")
            self.assertEqual(candidate_event["count"], 2)
            self.assertNotIn("Acme", json.dumps(candidate_event))

    def test_fabricated_single_choice_ids_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = _Chat([
                ("ask_user", {
                    "question": "Which Acme should I open?",
                    "response_kind": "single_choice",
                    "choice_ids": ["choice-invented-1", "choice-invented-2"],
                }),
                ("finish", {"done": False, "why": "No verified choices are available."}),
            ])
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read and open one company.",
                    ledger_path=root / "session.jsonl",
                ),
                chat=chat, business_tools=None, control=ReadOnlyControl(),
                run_id_factory=lambda _message: "fabricated-choice-turn",
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute(_message())

            self.assertEqual(result.status, AgentTurnStatus.REFUSED)
            self.assertIsNone(result.agent.clarification)
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            self.assertEqual(
                [event["event"] for event in events].count("clarification_refused"), 1)

    def test_unchanged_repeat_is_not_dispatched_and_a_second_repeat_ends_the_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = _Channel()
            chat = _Chat([("observe", {}), ("observe", {}), ("observe", {})])
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read the current page.",
                    ledger_path=Path(tmp) / "session.jsonl",
                ),
                chat=chat, business_tools=None, control=ReadOnlyControl(),
                run_id_factory=lambda _message: "no-progress-turn",
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute(_message())
                browser = _RemoteBrowser.instances[-1]

            self.assertEqual(result.status, AgentTurnStatus.REFUSED)
            self.assertEqual(result.terminal_reason, "no_progress")
            self.assertEqual(browser.actions, ["observe"])
            self.assertIn("NO PROGRESS", chat.messages[2][-1]["content"])
            events = [json.loads(line) for line in
                      (Path(tmp) / "session.jsonl").read_text().splitlines()]
            self.assertEqual(
                [event["event"] for event in events].count("no_progress_detected"), 1)
            self.assertEqual(
                [event["event"] for event in events].count("no_progress_terminated"), 1)

    def test_principal_context_reaches_tool_and_redacted_audit(self):
        class ContextExecutor(_Executor):
            def __init__(self):
                self.context = None

            def execute(self, name, args, *, context=None):
                self.context = context
                return super().execute(name, args)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            tool_executor = ContextExecutor()
            tools = BusinessToolRuntime(
                (skill,), tool_executor, application_id="fixture",
                required_scopes={"query_companies": "companies:read"})
            channel = _Channel(principal=_principal())
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read company data.",
                    ledger_path=root / "session.jsonl", require_identity=True,
                    plain_business_answer_min_page_actions=0),
                chat=_Chat([
                    ("load_skill", {"skill_id": skill.skill_id}),
                    ("query_companies", {}),
                    (None, {"content": "Acme"}),
                ]),
                business_tools=tools, control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-principal-bound",
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.run_once(_message())

            self.assertEqual(result.status, AgentTurnStatus.FINISHED)
            self.assertIsNotNone(tool_executor.context)
            self.assertEqual(tool_executor.context.turn_id, "message-1")
            ledger_text = (root / "session.jsonl").read_text()
            self.assertIn(channel.principal.binding_digest, ledger_text)
            for raw in ("tenant-private", "workspace-private", "actor-private",
                        "delegation-private"):
                self.assertNotIn(raw, ledger_text)

    def test_injected_ledger_factory_returns_the_same_audit_to_the_composition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = []

            def ledger_factory(path, run_id, *, base=None):
                from web_operation_agent.ledger import SessionLedger
                ledger = SessionLedger(path, run_id, base=base)
                captured.append(ledger)
                return ledger

            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read the current page.",
                    ledger_path=root / "session.jsonl"),
                chat=_Chat([("finish", {"done": True, "why": "done"})]),
                business_tools=None,
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-captured-ledger",
                ledger_factory=ledger_factory,
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(_message())

            self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].run_id, execution.run_id)
            self.assertEqual(
                captured[0].of_kind("product_turn_finished")[-1]["status"],
                "finished",
            )

    def test_write_tool_uses_the_common_approval_gate_and_denial_is_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _write_skill(root / "write-skill.json")
            mutation_executor = _MutationExecutor()
            tools = BusinessToolRuntime((skill,), mutation_executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            channel = _Channel(decision="denied", principal=_principal())
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Create the supplied company only.",
                    ledger_path=root / "session.jsonl",
                    approval_ttl_s=17,
                    allow_business_mutations=True,
                    require_identity=True,
                    deployment_profile="test",
                ),
                chat=chat,
                business_tools=tools,
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-write-denied",
            )
            message = {
                "message_id": "message-write",
                "text": "Create company Acme",
                "page_url": "http://app.test/companies",
            }
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(message)

            self.assertEqual(execution.status, AgentTurnStatus.CANCELLED)
            self.assertEqual(execution.terminal_reason, "approval_denied")
            self.assertEqual(mutation_executor.executions, 0)
            self.assertEqual(len(channel.approvals), 1)
            self.assertEqual(channel.approvals[0].effect, "Create one company")
            self.assertEqual(channel.approvals[0].targets, ("Acme",))
            self.assertEqual(channel.last_approval_timeout, 17)
            self.assertEqual(execution.agent.business_control["approval_denied"], 1)
            self.assertNotIn("verified", channel.published[-1][1])

    def test_cancelled_message_stops_before_model_or_browser_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = _Chat([("finish", {"done": True, "why": "should not run"})])
            channel = _Channel(cancelled=True)
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read current company data.",
                    ledger_path=root / "session.jsonl"),
                chat=chat, business_tools=None, control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-user-cancelled",
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(_message())

            self.assertEqual(execution.status, AgentTurnStatus.CANCELLED)
            self.assertEqual(execution.terminal_reason, "user_cancelled")
            self.assertEqual(chat.messages, [])
            self.assertEqual(_RemoteBrowser.instances[-1].actions, [])
            self.assertEqual(channel.published[-1][1]["state"], "cancelled")

    def test_cancellation_reaches_an_inflight_cancellable_provider(self):
        class MessageCancellationChannel(_Channel):
            def __init__(self):
                super().__init__()
                self.cancelled_messages = set()

            def message_cancelled(self, message_id):
                return message_id in self.cancelled_messages

        class BlockingChat:
            model = "blocking-double"

            def __init__(self):
                self.started = threading.Event()
                self.calls = 0

            def complete(self, *_args, **_kwargs):
                raise AssertionError("the blocking fallback must not be used")

            def complete_cancellable(self, *_args, cancelled, **_kwargs):
                self.calls += 1
                self.started.set()
                if self.calls == 1:
                    while not cancelled():
                        time.sleep(.01)
                    raise ProviderError(
                        "provider call cancelled", code="provider_cancelled",
                        retryable=False, dispatched=True)
                return {"tool_calls": [{"id": "finish-recovery", "function": {
                    "name": "finish",
                    "arguments": json.dumps({"done": True, "why": "recovered"}),
                }}]}

        with tempfile.TemporaryDirectory() as tmp:
            chat = BlockingChat()
            channel = MessageCancellationChannel()
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read current company data.",
                    ledger_path=Path(tmp) / "session.jsonl"),
                chat=chat, business_tools=None, control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-cancel-provider",
            )
            outcome = []
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                worker = threading.Thread(
                    target=lambda: outcome.append(executor.run_once(_message())))
                worker.start()
                self.assertTrue(chat.started.wait(1))
                channel.cancelled_messages.add("message-1")
                worker.join(1)

                recovered = executor.run_once({
                    "message_id": "message-2",
                    "text": "continue after cancellation",
                    "page_url": "http://app.test/companies",
                })

            self.assertFalse(worker.is_alive())
            self.assertEqual(outcome[0].status, AgentTurnStatus.CANCELLED)
            self.assertEqual(outcome[0].terminal_reason, "user_cancelled")
            self.assertEqual(recovered.status, AgentTurnStatus.FINISHED)
            self.assertEqual(recovered.terminal_reason, "agent_finished")
            self.assertEqual(chat.calls, 2)
            self.assertEqual(channel.published[-1][1]["state"], "finished")

    def test_provider_failure_is_not_relabelled_when_cancellation_arrives(self):
        class FailureAtCancellation:
            model = "failure-at-cancellation"

            def __init__(self, channel):
                self.channel = channel

            def complete(self, *_args, **_kwargs):
                raise AssertionError("the cancellable path is required")

            def complete_cancellable(self, *_args, cancelled, **_kwargs):
                self.channel.cancelled = True
                raise ProviderError(
                    "provider failed", code="provider_http", retryable=True,
                    request_id="a" * 32, dispatched=True)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel = _Channel()
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read current company data.",
                    ledger_path=root / "session.jsonl"),
                chat=FailureAtCancellation(channel), business_tools=None,
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-provider-failed",
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(_message())

            self.assertEqual(execution.status, AgentTurnStatus.FAILED)
            self.assertEqual(execution.terminal_reason, "provider_error")
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            failed = next(item for item in events if item["event"] == "provider_failed")
            self.assertEqual(failed["provider_error_code"], "provider_http")
            self.assertFalse(any(item["event"] == "provider_cancelled" for item in events))

    def test_indeterminate_write_is_preserved_and_never_presented_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _write_skill(root / "write-skill.json")
            mutation_executor = _MutationExecutor(indeterminate=True)
            tools = BusinessToolRuntime((skill,), mutation_executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            channel = _Channel(decision="approved", principal=_principal())
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Create the supplied company only.",
                    ledger_path=root / "session.jsonl",
                    allow_business_mutations=True,
                    require_identity=True,
                    deployment_profile="test"),
                chat=chat, business_tools=tools, control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-write-indeterminate",
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once({
                    "message_id": "message-write",
                    "text": "Create company Acme",
                    "page_url": "http://app.test/companies",
                })

            self.assertEqual(execution.status, AgentTurnStatus.INDETERMINATE)
            self.assertEqual(execution.terminal_reason, "indeterminate_write")
            self.assertEqual(mutation_executor.executions, 1)
            self.assertIn("uncertain", execution.user_answer.casefold())
            self.assertEqual(channel.published[-1][1]["state"], "indeterminate")

    def test_mutation_capability_is_fail_closed_until_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _write_skill(root / "write-skill.json")
            mutation_executor = _MutationExecutor()
            channel = _Channel(decision="approved")
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read-only application access.",
                    ledger_path=root / "session.jsonl"),
                chat=_Chat([
                    ("create_company", {"name": "Acme"}),
                    ("finish", {"done": False,
                                "why": "Company creation is not enabled."}),
                ]),
                business_tools=BusinessToolRuntime((skill,), mutation_executor),
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-write-disabled",
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once({
                    "message_id": "message-write-disabled",
                    "text": "Create company Acme",
                    "page_url": "http://app.test/companies",
                })

            self.assertEqual(execution.status, AgentTurnStatus.REFUSED)
            self.assertEqual(execution.terminal_reason, "agent_refused")
            self.assertEqual(channel.approvals, [])
            self.assertEqual(mutation_executor.executions, 0)
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            candidates = next(event for event in events
                              if event["event"] == "business_skill_candidates")
            self.assertEqual(candidates["candidate_ids"], [])
            self.assertEqual(candidates["trigger_matches"], [skill.skill_id])
            self.assertFalse(candidates["mutations_enabled"])

    def test_closed_channel_does_not_erase_an_already_finished_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = AgentTurnExecutor(
                channel=_ClosingAfterRunChannel(),
                config=AgentTurnConfig(
                    supported_scope="Read the current page.",
                    ledger_path=root / "session.jsonl"),
                chat=_Chat([("finish", {"done": True, "why": "done"})]),
                business_tools=None,
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-closed-after-finish",
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(_message())

            self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["event"], "terminal_delivery_lost")

    def test_other_terminal_delivery_failure_also_preserves_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = AgentTurnExecutor(
                channel=_BrokenAfterRunChannel(),
                config=AgentTurnConfig(
                    supported_scope="Read the current page.",
                    ledger_path=root / "session.jsonl"),
                chat=_Chat([("finish", {"done": True, "why": "done"})]),
                business_tools=None,
                control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-broken-delivery",
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(_message())

            self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["event"], "terminal_delivery_lost")
            self.assertEqual(events[-1]["error_type"], "RuntimeError")

    def test_renderer_failure_keeps_the_agent_result_and_uses_neutral_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel = _Channel()
            chat = _Chat([("finish", {"done": True, "why": "done"})])
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read the current page.",
                    ledger_path=root / "session.jsonl"),
                chat=chat, business_tools=None, control=ReadOnlyControl(),
                run_id_factory=lambda _message: "turn-answer-fallback",
                answer_renderer=lambda _context: (_ for _ in ()).throw(
                    RuntimeError("presentation unavailable")),
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(_message())

            self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
            self.assertTrue(execution.answer_fallback_used)
            self.assertEqual(execution.user_answer, "done")
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            self.assertEqual(len([
                event for event in events if event["event"] == "answer_render_failed"]), 1)


class ApplicationEgressVocabulary(unittest.TestCase):
    """An adapter that declares session parameters must find them at the provider boundary.

    Composition, not runtime: a deployment that forgot to widen the egress policy would
    otherwise discover the gap by sending the secret. The check is scoped to adapters that
    actually declare something, so an application with no session parameters of its own -- the
    common case -- composes exactly as before.
    """

    def _adapter(self, keys):
        adapter = _InvoiceAdapter()
        object.__setattr__(adapter.ports.routes, "session_handoff_query_keys",
                           frozenset(keys))
        return bind_application(adapter)

    def _executor(self, application, chat):
        return AgentTurnExecutor(
            channel=_Channel(),
            config=AgentTurnConfig(supported_scope="Read invoice data.",
                                   ledger_path=self.output / "session.jsonl"),
            chat=chat, application=application, control=ReadOnlyControl(),
            run_id_factory=lambda _message: "egress-vocabulary")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_adapter_declaring_nothing_composes_unchanged(self):
        chat = _Chat([])
        self.assertIsNotNone(self._executor(self._adapter(()), chat))

    def test_composition_fails_when_the_policy_has_not_been_told(self):
        from web_operation_agent.egress import ProviderEgressPolicy

        class _ChatWithEgress(_Chat):
            egress_policy = ProviderEgressPolicy()

        with self.assertRaisesRegex(ValueError, "session_id"):
            self._executor(self._adapter({"session_id"}), _ChatWithEgress([]))

    def test_composition_succeeds_once_the_policy_covers_the_declaration(self):
        from web_operation_agent.egress import ProviderEgressPolicy

        class _ChatWithEgress(_Chat):
            egress_policy = ProviderEgressPolicy(
                session_handoff_keys=frozenset({"session_id"}))

        self.assertIsNotNone(
            self._executor(self._adapter({"session_id"}), _ChatWithEgress([])))


class LabApparatusNeverReachesTheProduct(unittest.TestCase):
    """A deployed turn must not be able to name an experiment knob at all.

    `ServerAgentSession` takes lab settings as one `LabApparatus` value, because they exist
    only so the comparison harness can define its arms. `oracle_contract` injects a
    known-good plan -- hidden expected state AGENTS.md forbids in an agent's context --
    and `plan_from_page` and `terms_cache` define one arm's planning apparatus.

    `product.py` is the only place a deployed turn constructs a session, so the check is
    what it actually passes. Before the constructor grouping this had to enumerate four
    keyword names and hope nobody added a fifth; now the whole family is one keyword, and
    its absence is the assertion.
    """

    def _recorded_keywords(self) -> dict:
        recorded = {}
        real = product.ServerAgentSession

        class _Recording(real):
            def __init__(self, **kwargs):
                recorded.update(kwargs)
                super().__init__(**kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read company data only.",
                    ledger_path=root / "session.jsonl",
                    plain_business_answer_min_page_actions=0,
                    disabled_actions=frozenset({"click", "fill", "select", "submit"}),
                ),
                chat=_Chat([("query_companies", {}),
                            (None, {"content": "done"})]),
                business_tools=BusinessToolRuntime((skill,), _Executor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda message: "turn-lab-apparatus",
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser), \
                    patch.object(product, "ServerAgentSession", _Recording):
                # Only the composition matters here; whether this synthetic turn reaches a
                # terminal result is a different test's subject.
                try:
                    executor.run_once(_message())
                except product.AgentTurnError:
                    pass
        self.assertTrue(recorded, "the product turn did not construct a session")
        return recorded

    def test_the_product_turn_passes_no_lab_apparatus(self):
        recorded = self._recorded_keywords()
        self.assertNotIn("lab", recorded, "a lab apparatus reached a product turn")
        # And the default it therefore gets carries no experiment: no injected plan, no
        # page-only planning arm, no shared term cache.
        self.assertIsNone(NO_LAB.oracle_contract)
        self.assertFalse(NO_LAB.plan_from_page)
        self.assertIsNone(NO_LAB.terms_cache)

    def test_the_product_turn_compiles_no_contract_from_source(self):
        recorded = self._recorded_keywords()
        capability = recorded["source"]
        # Source planning is a deployment capability, not a default: a product turn compiles
        # no contract, and looks up no source, unless a composition explicitly supplies one.
        self.assertIs(capability.source_contract, False)
        self.assertIs(capability.runtime_source, False)


class EntryScopesReachTheProductTurn(unittest.TestCase):
    """A compiled SourceMap the product path never consults is worse than none.

    `resolve_source_affordances` needs the scopes the *entry page* actually exposes, and the
    adapter derives them from that page. For as long as the product turn assigned
    `entry_observation` directly, `served_webapps` stayed empty and every candidate in auto
    mode was silently zero — the mechanism worked in a directly composed session and did
    nothing in a deployment. This pins the wiring, not the affordance logic.
    """

    def test_the_turn_derives_served_scopes_from_its_entry_observation(self):
        from web_operation_agent.schemas import ElementRef, PageObservation

        recorded = {}
        real = product.ServerAgentSession

        class _Recording(real):
            def adopt_entry(self, obs):
                result = super().adopt_entry(obs)
                recorded["served_webapps"] = set(self.served_webapps)
                recorded["entry_url"] = self.entry_observation.url
                return result

        class _EntryLinkBrowser(_RemoteBrowser):
            def observe(self):
                return PageObservation(
                    schema_version=1, epoch=1, url="http://app.test/companies",
                    title="Companies", landmarks=[], elements=[ElementRef(
                        "e1", "link", "Invoices", "a", 1,
                        {"href": "/ledger/do/findInvoices"})])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read company data only.",
                    ledger_path=root / "session.jsonl",
                    plain_business_answer_min_page_actions=0,
                    disabled_actions=frozenset({"click", "fill", "select", "submit"}),
                ),
                chat=_Chat([("query_companies", {}), (None, {"content": "done"})]),
                business_tools=BusinessToolRuntime((skill,), _Executor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda message: "turn-entry-scopes",
            )
            with patch.object(product, "RemoteBrowserClient", _EntryLinkBrowser), \
                    patch.object(product, "ServerAgentSession", _Recording):
                try:
                    executor.run_once(_message())
                except product.AgentTurnError:
                    pass

        self.assertEqual(recorded.get("entry_url"), "http://app.test/companies")
        # No adapter is bound in this composition, so the scope set is empty on purpose --
        # what is asserted is that the derivation ran at all, which is the step that was
        # missing. The application-bound case is covered by the live Odoo gate's
        # `source_treatment_is_non_empty` check.
        self.assertIn("served_webapps", recorded)


class _FixtureGrammarAdapter:
    """A bound application whose route grammar is the core suite's synthetic one.

    A source-enabled session needs an application route port -- `lookup_source` licenses
    view-only routes, and something has to say which strings are routes at all. The port
    here is the fixture grammar (`/{scope}/do/{route}`), which is the grammar the fixture
    `SourceMap` was written against and is deliberately neither OFBiz's nor Odoo's.
    """

    _TOOL = {
        "type": "function",
        "function": {"name": "query_ledger",
                     "description": "Read ledger rows from the fixture application.",
                     "parameters": {"type": "object", "properties": {},
                                    "additionalProperties": False}},
    }

    class _Executor:
        def tool_schemas(self):
            return (_FixtureGrammarAdapter._TOOL,)

        def tool_effects(self):
            return {"query_ledger": "read"}

        def execute(self, name, args):
            route = fixture_route("greenhouse", "findTrays")
            return BusinessToolResult(
                content={"rows": [{"id": "row-1", "ui_path": route}]},
                audit={"operation": name, "count": 1},
                allowed_routes=(route,))

    def __init__(self):
        skill = BusinessSkill.from_bytes(json.dumps({
            "schema_version": 1, "skill_id": "fixture.ledger-query",
            "title": "Ledger lookup", "intent": "Read current ledger rows.",
            "triggers": ["ledger", "rows", "trays"],
            "instructions": ["Call query_ledger and trust its current result."],
            "tool_names": ["query_ledger"],
            "provenance": ["api:fixture@v0:/ledger#get"], "read_only": True,
        }, sort_keys=True).encode(), artifact_uri="memory://fixture/ledger.json")
        data = DeclaredDataClassifier((("query_ledger", ("business.ledger",)),))
        postconditions = ResultPostconditionVerifier(())
        self.business_tools = BusinessToolRuntime(
            (skill,), self._Executor(), application_id=FIXTURE_APPLICATION_ID,
            data_classifier=data, postcondition_verifier=postconditions,
            route_validator=FIXTURE_ROUTE_PORT)
        self.ports = ApplicationPorts(
            identity=AnonymousIdentityPort(), routes=FIXTURE_ROUTE_PORT,
            postconditions=postconditions,
            reconciliation=ConservativeMutationReconciler(), data=data)
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id=FIXTURE_APPLICATION_ID, display_name="Fixture Grammar",
            origin="https://fixture.test", navigation_mode="document",
            page_controller_module="https://fixture.test/assets/page-controller.js",
            catalog_sha256=self.business_tools.catalog_sha256)

    def health(self):
        return {"passed": True, "http_status": 200}


class _FixtureGrammarChat(_Chat):
    """The egress policy the assembly gate requires for this adapter's session vocabulary."""

    from web_operation_agent.egress import ProviderEgressPolicy as _Policy

    egress_policy = _Policy(
        session_handoff_keys=FIXTURE_ROUTE_PORT.session_handoff_query_keys)


class DeploymentSourceCapabilityReachesTheProductTurn(unittest.TestCase):
    """What the adapter declared is what the deployed turn runs with -- all four settings.

    The product turn used to build a `SourceCapability` of its own out of two of the four
    settings a capability carries, so an adapter that declared `runtime_source=True` -- Odoo
    does, at `adapter.py:source_capability` -- got a deployment where `lookup_source` and
    `open_source_route` were never on the tool surface. Nothing failed; the ability was
    simply absent, which is the shape of defect a passing suite does not catch.

    So the assertion is on the tool surface the model was actually offered, in both
    directions: declaring the capability puts the tools there, and withholding it takes them
    away. The capability itself must also arrive whole, because "the turn rebuilt one out of
    the fields it chose to keep" is the defect, not the symptom.
    """

    def _turn(self, capability):
        """Run one product turn under `capability` and return (session kwargs, tool surface)."""
        recorded = {}
        real = product.ServerAgentSession

        class _Recording(real):
            def __init__(self, **kwargs):
                recorded.update(kwargs)
                super().__init__(**kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = _FixtureGrammarChat(
                [("query_ledger", {}), (None, {"content": "done"})])
            executor = AgentTurnExecutor(
                channel=_Channel(page_url="https://fixture.test/greenhouse/do/main"),
                config=AgentTurnConfig(
                    supported_scope="Read ledger data only.",
                    ledger_path=root / "session.jsonl",
                    plain_business_answer_min_page_actions=0,
                    disabled_actions=frozenset({"click", "fill", "select", "submit"}),
                    source=capability,
                ),
                chat=chat,
                application=bind_application(_FixtureGrammarAdapter()),
                control=ReadOnlyControl(),
                run_id_factory=lambda message: "turn-source-capability",
            )
            with patch.object(product, "RemoteBrowserClient", _RemoteBrowser), \
                    patch.object(product, "ServerAgentSession", _Recording):
                try:
                    executor.run_once(
                        _message(page_url="https://fixture.test/greenhouse/do/main"))
                except product.AgentTurnError:
                    pass
        self.assertTrue(chat.tool_names, "the turn made no model call")
        return recorded, chat.tool_names[0]

    def _capability(self, root: Path, *, runtime_source: bool):
        from web_operation_agent.application import SourceCapability
        from web_operation_agent.tests.source_fixture import fixture_source_map

        return SourceCapability(source_map=fixture_source_map(root),
                                runtime_source=runtime_source,
                                source_affordance_mode="auto")

    def test_a_declared_runtime_source_capability_puts_the_tools_on_the_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            capability = self._capability(Path(tmp), runtime_source=True)
        recorded, tools = self._turn(capability)
        self.assertIn("lookup_source", tools)
        self.assertIn("open_source_route", tools)
        # Whole, not rebuilt: the identical value the composition was handed. Asserted after
        # the surface so a regression names the capability that was lost, not the plumbing.
        self.assertIs(recorded["source"], capability)

    def test_withholding_it_takes_the_same_tools_away(self):
        """The reverse direction: same composition, one field, and the tools are gone."""
        with tempfile.TemporaryDirectory() as tmp:
            capability = self._capability(Path(tmp), runtime_source=False)
        recorded, tools = self._turn(capability)
        self.assertNotIn("lookup_source", tools)
        self.assertNotIn("open_source_route", tools)
        self.assertIs(recorded["source"], capability)

    def test_the_turn_cannot_be_handed_source_settings_any_other_way(self):
        """There is no second door: the executor takes no source keyword of its own.

        While `source_map=` was an executor keyword and `source_affordance_mode` a config
        field, "what this deployment's source enables" had two owners and no single place
        that could refuse an incoherent pair.
        """
        accepted = set(inspect.signature(AgentTurnExecutor.__init__).parameters)
        self.assertEqual(accepted & {"source_map", "source", "source_affordance_mode",
                                     "source_contract", "runtime_source"}, set())
        fields = {field.name for field in dataclasses.fields(AgentTurnConfig)}
        self.assertIn("source", fields)
        self.assertEqual(fields & {"source_map", "source_affordance_mode",
                                   "source_contract", "runtime_source"}, set())

    def test_an_incoherent_capability_is_refused_in_one_place(self):
        from web_operation_agent.application import SourceCapability

        with self.assertRaisesRegex(ValueError, "version-bound SourceMap"):
            SourceCapability(runtime_source=True)
        with self.assertRaisesRegex(TypeError, "SourceCapability"):
            AgentTurnConfig(supported_scope="Read only.", ledger_path=Path("x.jsonl"),
                            source="auto")


if __name__ == "__main__":
    unittest.main()
