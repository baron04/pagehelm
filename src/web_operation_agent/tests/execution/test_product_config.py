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
            with patch.object(turn, "RemoteBrowserClient", _PageChangingBrowser):
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
