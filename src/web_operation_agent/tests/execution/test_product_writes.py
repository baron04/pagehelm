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
from web_operation_agent.schemas import ApprovalDecision, ObservationEnvelope
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


class ProductWriteAndCancellationTests(unittest.TestCase):
    def setUp(self):
        _RemoteBrowser.instances.clear()
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
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
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.run_once(_message())

            self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
            self.assertTrue(execution.answer_fallback_used)
            self.assertEqual(execution.user_answer, "done")
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            self.assertEqual(len([
                event for event in events if event["event"] == "answer_render_failed"]), 1)
