"""Shared product-entry test doubles; this module contains no test cases."""

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
