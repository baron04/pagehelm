"""Shared business-runtime test doubles; this module contains no test cases."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.business.runtime import (
    APPLICATION_SAVES_ON_LEAVE,
    BusinessMutationBlockedError, BusinessMutationIndeterminate,
    BusinessMutationRefusedError, BusinessMutationSpec,
    BusinessSelectionOption,
    BusinessSkill, Citation, FormDraft, FormDraftField,
    BusinessToolContractError, BusinessToolDispatchError, BusinessToolError,
    BusinessToolIdentityError,
    BusinessToolPermissionError, BusinessToolRequestError, BusinessToolResult,
    BusinessToolRuntime, BusinessToolUnavailableError,
)
from web_operation_agent.composition.ports import ResultPostconditionVerifier
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.answer.evidence import Origin, QueryEvidence, RecordEvidence
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.business.mutation import MemoryMutationJournal, MutationJournalError
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.execution.session import (CAPABILITY_GAP_TERMINAL,
                                        ServerAgentSession, TaskSpec)
from web_operation_agent.tests.chat_port_contract import CancellableChatDouble


SCHEMA = {"type": "function", "function": {
    "name": "query_companies", "description": "Read companies.",
    "parameters": {"type": "object", "properties": {},
                   "additionalProperties": False}}}
WRITE_SCHEMA = {"type": "function", "function": {
    "name": "create_company", "description": "Create one company.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string"}}, "required": ["name"],
        "additionalProperties": False}}}


class _Executor:
    @staticmethod
    def tool_schemas():
        return (SCHEMA,)

    @staticmethod
    def tool_effects():
        return {"query_companies": "read"}

    @staticmethod
    def execute(name, args, *, context=None):
        if name != "query_companies" or args:
            raise BusinessToolError("unexpected call")
        return BusinessToolResult(
            content={"companies": [{"name": "Acme", "domain": "acme.test",
                                     "ui_path": "/object/company/company-1"}],
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
            allowed_routes=("/object/company/company-1",))


class _ProjectedExecutor(_Executor):
    @staticmethod
    def execute(name, args, *, context=None):
        result = _Executor.execute(name, args, context=context)
        return BusinessToolResult(
            content=result.content, audit=result.audit,
            allowed_routes=result.allowed_routes,
            query_answer=StructuredQueryAnswer(
                collection="companies", fields=("name", "domain"),
                records=(("Acme", "acme.test"),), source="fixture API",
                scope="current_delegated_workspace",
                observed_at="2026-09-08T05:00:00+00:00",
                source_complete=True, result_truncated=False,
                matched_count=1, source_total=1, records_scanned=1))


def _skill(path: Path) -> BusinessSkill:
    path.write_text(json.dumps({
        "schema_version": 1,
        "skill_id": "fixture.companies-query",
        "title": "Query companies",
        "intent": "Read current companies.",
        "triggers": ["companies", "公司"],
        "instructions": ["Call query_companies and trust its current result."],
        "tool_names": ["query_companies"],
        "provenance": ["src:fixture@v1:companies.json#companies"],
        "read_only": True,
    }) + "\n")
    return BusinessSkill.load(path)


def _write_skill(path: Path) -> BusinessSkill:
    path.write_text(json.dumps({
        "schema_version": 1,
        "skill_id": "fixture.companies-create",
        "title": "Create company",
        "intent": "Create one current company.",
        "triggers": ["create company", "创建公司"],
        "instructions": ["Propose one company and wait for approval."],
        "tool_names": ["create_company"],
        "provenance": ["src:fixture@v1:companies.json#create"],
        "read_only": False,
    }) + "\n")
    return BusinessSkill.load(path)


class _MutationExecutor:
    def __init__(self, *, indeterminate=False):
        self.executions = 0
        self.indeterminate = indeterminate

    @staticmethod
    def tool_schemas():
        return (WRITE_SCHEMA,)

    @staticmethod
    def tool_effects():
        return {"create_company": "write"}

    @staticmethod
    def execute(_name, _args, *, context=None):
        raise AssertionError("write tools must not use the read dispatch path")

    @staticmethod
    def prepare_mutation(name, args, *, context=None):
        if name != "create_company" or set(args) != {"name"}:
            raise BusinessToolError("invalid create request")
        return BusinessMutationSpec(
            args={"name": str(args["name"]).strip()},
            effect="Create one company", targets=(str(args["name"]).strip(),))

    def execute_mutation(self, name, args, *, context=None):
        self.executions += 1
        self.context = context
        if self.indeterminate:
            raise BusinessMutationIndeterminate("fixture acknowledgement was lost")
        return BusinessToolResult(
            content={"created": True, "name": args["name"],
                     "ui_path": "/object/company/company-2"},
            audit={"operation": name, "records_created": 1},
            allowed_routes=("/object/company/company-2",))


def _operation_context(*, scopes=("companies:read", "companies:write"),
                       operation_id="op-1", application_id="fixture"):
    now = int(time.time() * 1000)
    principal = PrincipalContext(
        1, application_id, "tenant-1", "workspace-1", "actor-1", "delegation-1",
        now - 1_000, now + 60_000, tuple(scopes))
    return OperationContext.create(
        principal, session_id="session-1", turn_id="message-1",
        operation_id=operation_id, deadline_ms=now + 30_000, now_ms=now)


def _observation(epoch: int, url: str = "http://app.test/companies") -> PageObservation:
    return PageObservation(
        schema_version=1, epoch=epoch, url=url,
        title="Companies", landmarks=["Companies"], elements=[],
        content="Companies page")


class _Browser:
    def __init__(self):
        self.epoch = 1
        self.actions = []
        self.url = "http://app.test/companies"

    def observe(self):
        return _observation(self.epoch, self.url)

    def execute(self, action, args=None, expect_navigation=False):
        self.actions.append(action)
        before = self.url
        if action == "goto":
            self.url = "http://app.test" + str((args or {}).get("url") or "")
        self.epoch += 1
        return ActionReceipt(
            schema_version=1, command_id=f"c{len(self.actions)}", action=action,
            args_echo=dict(args or {}), ok=True,
            url_before=before,
            url_after=self.url, navigated=self.url != before,
            epoch_before=self.epoch - 1, epoch_after=self.epoch,
            observation=_observation(self.epoch, self.url))

    @staticmethod
    def console():
        return []


class _Chat(CancellableChatDouble):
    def __init__(self, script):
        self.script = list(script)
        self.tool_names = []
        self.messages = []

        class _Meter:
            @staticmethod
            def report():
                return {"provider_calls": 0, "cost_cny": 0.0}

        self.meter = _Meter()

    def complete(self, messages, tools, _run_id, **_kwargs):
        self.messages.append([dict(message) for message in messages])
        self.tool_names.append({tool["function"]["name"] for tool in tools})
        name, args = self.script.pop(0)
        if name is None:
            return {"content": args["content"]}
        return {"tool_calls": [{"id": str(len(self.messages)), "function": {
            "name": name, "arguments": json.dumps(args)}}]}
