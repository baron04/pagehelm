from __future__ import annotations

import dataclasses
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent import product
from web_operation_agent.business import BusinessToolResult, BusinessToolRuntime
from web_operation_agent.conversation import ConversationState
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.identity import PrincipalContext
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


class _Channel:
    def __init__(self, *, page_url: str = "http://app.test/companies",
                 observation: bool = True, decision: str = "approved",
                 cancelled: bool = False,
                 principal: PrincipalContext | None = None):
        self.session_id = "product-session"
        self.published = []
        self.approvals = []
        self.decision = decision
        self.cancelled = cancelled
        self.principal = principal
        self.latest_observation_envelope = (
            self.envelope(1, page_url) if observation else None)

    def message_cancelled(self, _message_id: str) -> bool:
        return self.cancelled

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
                {"require_identity": 1},
                {"plain_business_answer_min_page_actions": -1},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    AgentTurnConfig(
                        supported_scope=kwargs.pop(
                            "supported_scope", "Read company data."),
                        ledger_path=path,
                        **kwargs,
                    )


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

    def test_second_turn_resolves_reference_but_must_refresh_before_opening(self):
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
            self.assertEqual(_RemoteBrowser.instances[1].actions,
                             ["observe", "observe", "goto"])
            events = [json.loads(line) for line in
                      (root / "session.jsonl").read_text().splitlines()]
            self.assertEqual(
                sum(event["event"] == "page_route_rejected" for event in events), 1)
            self.assertEqual(state.turn_count, 2)

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

            def ledger_factory(path, run_id):
                from web_operation_agent.ledger import SessionLedger
                ledger = SessionLedger(path, run_id)
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
            channel = _Channel(decision="denied")
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Create the supplied company only.",
                    ledger_path=root / "session.jsonl",
                    approval_ttl_s=17,
                    allow_business_mutations=True,
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
            channel = _Channel(decision="approved")
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Create the supplied company only.",
                    ledger_path=root / "session.jsonl",
                    allow_business_mutations=True),
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
                    ("load_skill", {"skill_id": skill.skill_id}),
                    ("create_company", {"name": "Acme"}),
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

            self.assertEqual(execution.status, AgentTurnStatus.FAILED)
            self.assertEqual(execution.terminal_reason, "approval_unavailable")
            self.assertEqual(channel.approvals, [])
            self.assertEqual(mutation_executor.executions, 0)

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


if __name__ == "__main__":
    unittest.main()
