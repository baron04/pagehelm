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
from web_operation_agent.business.control import ReadOnlyControl
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
from web_operation_agent.schemas import (POTENTIALLY_WRITING_ACTIONS, ApprovalDecision,
                                         ObservationEnvelope)
from web_operation_agent.tests.support.business import (
    _Browser,
    _Chat,
    _Executor,
    _MutationExecutor,
    _observation,
    _skill,
    _write_skill,
)
from web_operation_agent.tests.support.route_fixture import (FIXTURE_APPLICATION_ID,
                                                     FIXTURE_ROUTE_PORT, fixture_route)
from web_operation_agent.tests.support.application import _InvoiceAdapter


from web_operation_agent.tests.execution.product_support import (
    _BrokenAfterRunChannel, _Channel, _ClosingAfterRunChannel, _PageChangingBrowser,
    _RemoteBrowser, _message, _principal,
)


READ_ONLY_NAVIGATION_POLICY = POTENTIALLY_WRITING_ACTIONS - {"goto"}


class ProductAnswerAndContinuationTests(unittest.TestCase):
    def setUp(self):
        _RemoteBrowser.instances.clear()
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
                    disabled_actions=READ_ONLY_NAVIGATION_POLICY,
                ),
                chat=chat,
                business_tools=BusinessToolRuntime((skill,), TwoCompanyExecutor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda message: f"turn-{message.message_id}",
                conversation_state=state,
            )
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            self.assertIn("query_companies", chat.tool_names[3])
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            self.assertEqual([item["state"] for item in progress if item["state"] != "step"],
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
                from web_operation_agent.state.ledger import SessionLedger
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(_message())

            self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].run_id, execution.run_id)
            self.assertEqual(
                captured[0].of_kind("product_turn_finished")[-1]["status"],
                "finished",
            )
