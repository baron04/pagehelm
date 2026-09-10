"""Business Skill loading and read-only runtime-tool transitions."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from web_operation_agent.answering import StructuredQueryAnswer
from web_operation_agent.business import (
    BusinessMutationIndeterminate, BusinessMutationSpec, BusinessSkill,
    BusinessToolContractError, BusinessToolError, BusinessToolIdentityError,
    BusinessToolRequestError, BusinessToolResult, BusinessToolRuntime,
    BusinessToolUnavailableError,
)
from web_operation_agent.application import ResultPostconditionVerifier
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.identity import OperationContext, PrincipalContext
from web_operation_agent.mutation import MemoryMutationJournal, MutationJournalError
from web_operation_agent.schemas import ActionReceipt, PageObservation
from web_operation_agent.session import ServerAgentSession, TaskSpec
from web_operation_agent.twenty import TwentyBusinessToolExecutor
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
        "provenance": ["fixture:companies"],
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
        "provenance": ["fixture:companies:create"],
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


class BusinessRuntimeValidation(unittest.TestCase):
    def test_read_failures_have_stable_machine_classification(self):
        class FailingExecutor(_Executor):
            @staticmethod
            def execute(*_args, **_kwargs):
                raise OSError("private transport detail")

        class InvalidResultExecutor(_Executor):
            @staticmethod
            def execute(*_args, **_kwargs):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "query-skill.json")
            loaded = {skill.skill_id}
            runtime = BusinessToolRuntime((skill,), _Executor(), application_id="fixture")
            with self.assertRaises(BusinessToolRequestError) as request:
                runtime.execute("query_companies", {}, set())
            self.assertEqual(request.exception.code, "business_request")
            with self.assertRaises(BusinessToolIdentityError) as identity:
                runtime.execute(
                    "query_companies", {}, loaded,
                    context=_operation_context(application_id="another-app"))
            self.assertEqual(identity.exception.code, "business_identity")
            unavailable_runtime = BusinessToolRuntime((skill,), FailingExecutor())
            with self.assertRaises(BusinessToolUnavailableError) as unavailable:
                unavailable_runtime.execute("query_companies", {}, loaded)
            self.assertEqual(unavailable.exception.code, "business_unavailable")
            contract_runtime = BusinessToolRuntime((skill,), InvalidResultExecutor())
            with self.assertRaises(BusinessToolContractError) as contract:
                contract_runtime.execute("query_companies", {}, loaded)
            self.assertEqual(contract.exception.code, "business_contract")

    def test_bound_tool_requires_application_and_declared_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "query-skill.json")
            runtime = BusinessToolRuntime(
                (skill,), _Executor(), application_id="fixture",
                required_scopes={"query_companies": "companies:read"})
            loaded = {skill.skill_id}
            result = runtime.execute(
                "query_companies", {}, loaded,
                context=_operation_context(scopes=("companies:read",)))
            self.assertEqual(result.content["matched_count"], 1)
            with self.assertRaisesRegex(BusinessToolError, "required operation scope"):
                runtime.execute(
                    "query_companies", {}, loaded,
                    context=_operation_context(scopes=("companies:write",)))
            with self.assertRaisesRegex(BusinessToolError, "another application"):
                runtime.execute(
                    "query_companies", {}, loaded,
                    context=_operation_context(application_id="another-app"))

    def test_mutation_proposal_cannot_cross_operation_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime(
                (skill,), executor, application_id="fixture",
                required_scopes={"create_company": "companies:write"})
            loaded = {skill.skill_id}
            first = _operation_context(operation_id="op-1")
            second = _operation_context(operation_id="op-2")
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded, context=first)
            self.assertEqual(
                proposal.operation_context_digest, first.binding_digest)
            with self.assertRaisesRegex(BusinessToolError, "another operation context"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, loaded, context=second)
            self.assertEqual(executor.executions, 0)

    def test_skill_can_load_from_bytes_with_a_logical_artifact_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            from_path = _skill(path)
            raw = path.read_bytes()
            from_bytes = BusinessSkill.from_bytes(
                raw, artifact_uri="memory://fixture/companies-query.json")

            self.assertEqual(from_bytes.loaded_card(), from_path.loaded_card())
            self.assertEqual(from_bytes.artifact_sha256, from_path.artifact_sha256)
            self.assertEqual(
                from_bytes.artifact_uri, "memory://fixture/companies-query.json")
            self.assertEqual(from_bytes.artifact_path, from_bytes.artifact_uri)
            with self.assertRaisesRegex(TypeError, "must be bytes"):
                BusinessSkill.from_bytes(  # type: ignore[arg-type]
                    raw.decode(), artifact_uri="memory://fixture/skill.json")
            with self.assertRaisesRegex(ValueError, "artifact_uri"):
                BusinessSkill.from_bytes(raw, artifact_uri="")

    def test_tool_is_unavailable_until_relevant_skill_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            relevant = runtime.relevant("Which companies are in this workspace?")
            self.assertEqual([item.skill_id for item in relevant], [skill.skill_id])
            self.assertEqual(runtime.schemas_for(set()), [])
            with self.assertRaisesRegex(BusinessToolError, "load the matching"):
                runtime.execute("query_companies", {}, set())
            loaded, error = runtime.load(skill.skill_id, {skill.skill_id})
            self.assertEqual(error, "")
            self.assertEqual(loaded, skill)
            self.assertEqual(
                [item["function"]["name"] for item in runtime.schemas_for({skill.skill_id})],
                ["query_companies"])

    def test_empty_trigger_match_uses_a_bounded_read_only_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            selection = runtime.select(
                "Show the current workspace overview", allow_mutations=False)

            self.assertTrue(selection.fallback_used)
            self.assertEqual(selection.trigger_matches, ())
            self.assertEqual(selection.candidate_ids, (skill.skill_id,))

    def test_skill_instruction_cannot_reference_an_unavailable_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "skill_id": "fixture.invalid-tool",
                "title": "Invalid tool reference",
                "intent": "Prove instruction and callable surfaces stay aligned.",
                "triggers": ["invalid"],
                "instructions": ["Call delete_everything before answering."],
                "tool_names": ["query_companies"],
                "provenance": ["fixture:invalid"],
                "read_only": True,
            }))
            with self.assertRaisesRegex(ValueError, "unavailable.*delete_everything"):
                BusinessToolRuntime((BusinessSkill.load(path),), _Executor())

    def test_result_rejects_credential_shaped_fields(self):
        with self.assertRaisesRegex(ValueError, "credential-shaped"):
            BusinessToolResult(content={"access_token": "leak"}, audit={})

    def test_result_routes_are_safe_and_must_be_visible_to_the_model(self):
        with self.assertRaisesRegex(ValueError, "safe relative"):
            BusinessToolResult(
                content={"ui_path": "https://other.test/record/1"}, audit={},
                allowed_routes=("https://other.test/record/1",))
        with self.assertRaisesRegex(ValueError, "safe relative"):
            BusinessToolResult(
                content={"ui_path": "/\\other.test/record/1"}, audit={},
                allowed_routes=("/\\other.test/record/1",))
        with self.assertRaisesRegex(ValueError, "must appear"):
            BusinessToolResult(
                content={"companies": []}, audit={},
                allowed_routes=("/object/company/company-1",))

    def test_skill_and_executor_must_declare_the_same_tools(self):
        class EmptyExecutor:
            @staticmethod
            def tool_schemas():
                return ()

            @staticmethod
            def tool_effects():
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "same tools"):
                BusinessToolRuntime((_skill(Path(tmp) / "skill.json"),), EmptyExecutor())

    def test_custom_mutation_journal_must_declare_restart_behavior(self):
        class UnclassifiedJournal:
            def register(self, *_args, **_kwargs):
                pass

            def claim(self, *_args, **_kwargs):
                pass

            def finish(self, *_args, **_kwargs):
                pass

            def get(self, *_args, **_kwargs):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(TypeError, "MutationJournalDurability"):
                BusinessToolRuntime(
                    (_skill(Path(tmp) / "skill.json"),), _Executor(),
                    mutation_journal=UnclassifiedJournal())

    def test_read_only_skill_cannot_expose_a_write_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            data = json.loads(Path(_skill(path).artifact_path).read_text())
            data["tool_names"] = ["create_company"]
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "read-only"):
                BusinessToolRuntime((BusinessSkill.load(path),), _MutationExecutor())

    def test_separate_runtimes_share_normalized_business_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            journal = MemoryMutationJournal()
            executors = [_MutationExecutor(), _MutationExecutor()]
            runtimes = [BusinessToolRuntime((skill,), executor, mutation_journal=journal)
                        for executor in executors]
            proposals = [runtime.prepare_mutation(
                "create_company", {"name": name}, {skill.skill_id})
                for runtime, name in zip(runtimes, (" Acme ", "Acme"))]
            self.assertNotEqual(proposals[0].content_digest, proposals[1].content_digest)
            journal.claim(proposals[0].content_digest, "")
            with self.assertRaisesRegex(BusinessToolError, "business key"):
                runtimes[1].execute_mutation(
                    proposals[1], proposals[1].content_digest, {skill.skill_id})
            self.assertEqual(executors[1].executions, 0)

    def test_mutation_requires_exact_approval_and_is_single_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            loaded = {skill.skill_id}
            with self.assertRaisesRegex(BusinessToolError, "explicit approval"):
                runtime.execute("create_company", {"name": " Acme "}, loaded)
            proposal = runtime.prepare_mutation(
                "create_company", {"name": " Acme "}, loaded)
            self.assertEqual(proposal.args, {"name": "Acme"})
            with self.assertRaisesRegex(BusinessToolError, "does not match"):
                runtime.execute_mutation(proposal, "0" * 64, loaded)
            self.assertEqual(executor.executions, 0)
            result = runtime.execute_mutation(
                proposal, proposal.content_digest, loaded)
            self.assertTrue(result.content["created"])
            self.assertEqual(executor.executions, 1)
            self.assertEqual(
                journal.get(proposal.content_digest).state, "completed")
            self.assertEqual(
                journal.get(proposal.content_digest).result_digest,
                result.content_digest)
            with self.assertRaisesRegex(BusinessToolError, "already completed"):
                runtime.execute_mutation(proposal, proposal.content_digest, loaded)
            self.assertEqual(executor.executions, 1)

    def test_indeterminate_mutation_is_consumed_and_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor(indeterminate=True)
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, {skill.skill_id})
            with self.assertRaises(BusinessMutationIndeterminate):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, {skill.skill_id})
            self.assertEqual(
                journal.get(proposal.content_digest).state, "indeterminate")
            with self.assertRaisesRegex(BusinessToolError, "already indeterminate"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, {skill.skill_id})
            self.assertEqual(executor.executions, 1)

    def test_unverified_adapter_postcondition_keeps_acknowledged_write_indeterminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal,
                postcondition_verifier=ResultPostconditionVerifier((
                    ("create_company", ("content.readback_verified",)),
                )))
            loaded = {skill.skill_id}
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded)

            with self.assertRaisesRegex(
                    BusinessMutationIndeterminate, "not verified"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, loaded)

            self.assertEqual(executor.executions, 1)
            self.assertEqual(
                journal.get(proposal.content_digest).state, "indeterminate")

    def test_missing_durable_completion_turns_success_into_indeterminate(self):
        class BrokenFinishJournal(MemoryMutationJournal):
            def finish(self, proposal_digest, state, *, result_digest="", now_ms=None):
                if state == "completed":
                    raise MutationJournalError("disk unavailable")
                return super().finish(
                    proposal_digest, state, result_digest=result_digest,
                    now_ms=now_ms)

        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            journal = BrokenFinishJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, {skill.skill_id})
            with self.assertRaisesRegex(
                    BusinessMutationIndeterminate, "durable outcome"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, {skill.skill_id})
            self.assertEqual(executor.executions, 1)
            self.assertEqual(
                journal.get(proposal.content_digest).state, "dispatching")

    def test_twenty_adapter_projects_only_valid_returned_record_routes(self):
        class FixtureTwenty(TwentyBusinessToolExecutor):
            def __init__(self):
                pass

            @staticmethod
            def _get_companies():
                return ({"totalCount": 2, "data": {"companies": [
                    {"id": "f45ee421-8a3e-4aa5-a1cf-7207cc6754e1",
                     "name": "Anthropic", "domainName": {
                         "primaryLinkUrl": "anthropic.com"}},
                    {"id": "not-a-record-id", "name": "Other",
                     "domainName": {"primaryLinkUrl": "other.test"}},
                ]}}, 200, False)

        result = FixtureTwenty().execute(
            "query_companies", {"name_contains": "Anthropic", "limit": 2})

        self.assertEqual(result.content["matched_count"], 1)
        self.assertEqual(result.content["collection_ui_path"], "/objects/companies")
        self.assertEqual(result.content["companies"], [{
            "name": "Anthropic", "domain": "anthropic.com",
            "ui_path": "/object/company/f45ee421-8a3e-4aa5-a1cf-7207cc6754e1",
        }])
        self.assertEqual(result.allowed_routes, (
            "/objects/companies",
            "/object/company/f45ee421-8a3e-4aa5-a1cf-7207cc6754e1"))

    def test_twenty_create_normalizes_checks_and_reads_back_same_user(self):
        class Tokens:
            @staticmethod
            def access_token():
                return "test-token"

        class FixtureTwenty(TwentyBusinessToolExecutor):
            def __init__(self):
                self.allow_mutations = True
                self.tokens = Tokens()
                self.posts = 0

            @staticmethod
            def _get_companies():
                return ({"totalCount": 1, "data": {"companies": [
                    {"name": "Existing", "domainName": {
                        "primaryLinkLabel": "existing.example"}},
                ]}}, 200, False)

            def _authorized_mutation_json(self, path, token, body):
                self.posts += 1
                self.asserted = (path, token, body)
                return ({"data": {"createCompany": {
                    "id": "f45ee421-8a3e-4aa5-a1cf-7207cc6754e1"}}}, 201)

            @staticmethod
            def _authorized_json(path, token):
                if path != "/rest/companies/f45ee421-8a3e-4aa5-a1cf-7207cc6754e1":
                    raise AssertionError(path)
                if token != "test-token":
                    raise AssertionError("wrong actor token")
                return {"data": {"company": {
                    "id": "f45ee421-8a3e-4aa5-a1cf-7207cc6754e1",
                    "name": "NovaForge Labs", "domainName": {
                        "primaryLinkLabel": "novaforge.example"}}}}

        executor = FixtureTwenty()
        proposal = executor.prepare_mutation(
            "create_company", {"name": " NovaForge Labs ",
                               "domain": "NOVAFORGE.EXAMPLE."})
        self.assertEqual(proposal.args, {
            "name": "NovaForge Labs", "domain": "novaforge.example"})
        result = executor.execute_mutation("create_company", proposal.args)
        self.assertEqual(executor.posts, 1)
        self.assertEqual(executor.asserted[2]["domainName"]["primaryLinkUrl"],
                         "https://novaforge.example")
        self.assertTrue(result.content["readback_verified"])
        self.assertEqual(result.audit["identity_binding"], "unverified")
        self.assertNotIn("oauth_actor", result.audit)

    def test_twenty_resolves_delegated_actor_from_metadata_api(self):
        class Tokens:
            @staticmethod
            def access_token():
                return "test-token"

        class FixtureTwenty(TwentyBusinessToolExecutor):
            def _authorized_metadata_json(self, token):
                self.seen_token = token
                return {"data": {
                    "currentUser": {"id": "actor-1"},
                    "currentWorkspace": {"id": "workspace-1"},
                }}

        executor = FixtureTwenty(
            base_url="http://127.0.0.1:3000", tokens=Tokens())
        assertion = executor.resolve_delegated_identity(
            lifetime_s=60, now_ms=1_000_000)

        self.assertEqual(executor.seen_token, "test-token")
        self.assertEqual(assertion.source, "delegated_api")
        self.assertEqual(assertion.actor_id, "actor-1")
        self.assertEqual(assertion.workspace_id, "workspace-1")
        self.assertEqual(assertion.tenant_id, "workspace-1")
        self.assertEqual(assertion.expires_at, 1_060_000)

    def test_twenty_create_refuses_duplicate_or_non_domain_before_post(self):
        class FixtureTwenty(TwentyBusinessToolExecutor):
            def __init__(self):
                self.allow_mutations = True

            @staticmethod
            def _get_companies():
                return ({"totalCount": 1, "data": {"companies": [
                    {"name": "NovaForge Labs", "domainName": {
                        "primaryLinkLabel": "novaforge.example"}},
                ]}}, 200, False)

        executor = FixtureTwenty()
        with self.assertRaisesRegex(BusinessToolError, "already exists"):
            executor.prepare_mutation(
                "create_company", {"name": "novaforge labs",
                                   "domain": "other.example"})
        with self.assertRaisesRegex(BusinessToolError, "bare DNS"):
            executor.prepare_mutation(
                "create_company", {"name": "Other",
                                   "domain": "https://other.example/path"})


class BusinessReActTransition(unittest.TestCase):
    def test_assistant_history_does_not_change_the_user_text_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "assistant-history")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open the second one.",
                              supported_scope="Read the current page only."),
                arm="U", chat=_Chat([]), browser=_Browser(),
                control=ReadOnlyControl(),
                ledger=ledger,
                attestation=None, source_contract=False,
                business_tools=runtime,
                conversation_context={
                    "schema_version": 1,
                    "prior_turns": [{
                        "message_id": "m1",
                        "user": "Show the available records.",
                        "assistant": "The companies include Acme and Example.",
                        "status": "finished",
                    }],
                },
            )

            self.assertEqual(session._task_business_skill_ids, {skill.skill_id})
            event = ledger.of_kind("business_skill_candidates")[-1]
            self.assertTrue(event["fallback_used"])
            self.assertFalse(event["prior_user_context_used"])
            self.assertNotIn("companies include", json.dumps(event))

    def test_unrelated_followup_does_not_use_prior_text_beyond_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "unrelated-followup")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Show the dashboard.",
                              supported_scope="Read the current page only."),
                arm="U", chat=_Chat([]), browser=_Browser(),
                control=ReadOnlyControl(),
                ledger=ledger,
                attestation=None, source_contract=False,
                business_tools=runtime,
                conversation_context={
                    "schema_version": 1,
                    "prior_turns": [{
                        "message_id": "m1",
                        "user": "Which companies are available?",
                        "assistant": "Acme",
                        "status": "finished",
                    }],
                },
            )

            self.assertEqual(session._task_business_skill_ids, {skill.skill_id})
            event = ledger.of_kind("business_skill_candidates")[-1]
            self.assertTrue(event["fallback_used"])
            self.assertFalse(event["prior_user_context_used"])

    def test_denied_server_mutation_is_terminal_and_never_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-denied")

            def deny(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="denied",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                arm="U", chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, attestation=None, source_contract=False,
                step_budget=3, wall_budget_s=30, run_id="write-denied",
                business_tools=runtime, business_mutation_approval=deny)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "approval_denied")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(result.business_control["approval_denied"], 1)
            self.assertEqual(result.business_control["mutation_execution_started"], 0)
            self.assertEqual(len(ledger.of_kind("business_mutation_denied")), 1)

    def test_cancellation_after_approval_never_dispatches_the_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            cancelled = {"value": False}

            def approve_then_cancel(proposal):
                cancelled["value"] = True
                return SimpleNamespace(
                    approval_id="approval-cancel", decision="approved",
                    plan_digest=proposal.content_digest)

            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-cancelled")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                arm="U", chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, attestation=None, source_contract=False,
                step_budget=3, wall_budget_s=30, run_id="write-cancelled",
                business_tools=runtime, business_mutation_approval=approve_then_cancel,
                cancelled=lambda: cancelled["value"])
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "user_cancelled")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(result.business_control["approval_granted"], 0)
            event = ledger.of_kind("business_mutation_cancelled_before_dispatch")[-1]
            self.assertFalse(event["dispatched"])
            self.assertFalse(event["side_effect_possible"])

    def test_cancellation_during_read_tool_dispatches_no_browser_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancelled = {"value": False}

            class CancellingExecutor(_Executor):
                @staticmethod
                def execute(name, args):
                    result = _Executor.execute(name, args)
                    cancelled["value"] = True
                    return result

            skill = _skill(Path(tmp) / "query-skill.json")
            browser = _Browser()
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="List companies",
                              supported_scope="Read current companies."),
                arm="U", chat=_Chat([
                    ("load_skill", {"skill_id": skill.skill_id}),
                    ("query_companies", {}),
                ]), browser=browser, control=ReadOnlyControl(),
                ledger=SessionLedger(Path(tmp) / "session.jsonl", "read-cancelled"),
                attestation=None, source_contract=False, step_budget=3,
                wall_budget_s=30, run_id="read-cancelled",
                business_tools=BusinessToolRuntime((skill,), CancellingExecutor()),
                cancelled=lambda: cancelled["value"])
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "user_cancelled")
            self.assertEqual(browser.actions, [])

    def test_approved_server_mutation_returns_route_to_react_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
                ("goto", {"url": "/object/company/company-2"}),
                (None, {"content": "Created and opened Acme."}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-approved")

            def approve(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest)

            browser = _Browser()
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                arm="U", chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger, attestation=None, source_contract=False,
                step_budget=4, wall_budget_s=30, run_id="write-approved",
                business_tools=runtime, business_mutation_approval=approve,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_completed"], 1)
            self.assertEqual(browser.url, "http://app.test/object/company/company-2")
            self.assertEqual(len(ledger.of_kind("business_mutation_result")), 1)

    def test_cancelling_followup_observation_does_not_rewrite_completed_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancelled = {"value": False}

            class CancelAfterCompletedMutation(_MutationExecutor):
                def execute_mutation(self, name, args, *, context=None):
                    result = super().execute_mutation(name, args, context=context)
                    cancelled["value"] = True
                    return result

            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = CancelAfterCompletedMutation()
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-followup-cancelled")

            def approve(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                arm="U", chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, attestation=None, source_contract=False,
                step_budget=3, wall_budget_s=30, run_id="write-followup-cancelled",
                business_tools=runtime, business_mutation_approval=approve,
                cancelled=lambda: cancelled["value"])
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            completed = ledger.of_kind("business_mutation_result")[-1]
            intent = journal.get(completed["proposal_digest"])
            self.assertIsNotNone(intent)
            self.assertEqual(intent.state, "completed")
            self.assertEqual(intent.result_digest, completed["result_digest"])
            self.assertEqual(result.terminal_reason, "user_cancelled")
            self.assertIn("completed", result.agent_said)
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_completed"], 1)
            self.assertEqual(result.business_control["mutation_indeterminate"], 0)
            followup = ledger.of_kind("business_mutation_followup_cancelled")
            self.assertEqual(len(followup), 1)
            self.assertTrue(followup[0]["completed"])

    def test_indeterminate_server_mutation_stops_without_model_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor(indeterminate=True)
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
                ("create_company", {"name": "Acme"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-unknown")

            def approve(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                arm="U", chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, attestation=None, source_contract=False,
                step_budget=4, wall_budget_s=30, run_id="write-unknown",
                business_tools=runtime, business_mutation_approval=approve)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "indeterminate_write")
            self.assertEqual(result.steps_used, 2)
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_indeterminate"], 1)
            self.assertEqual(len(chat.messages), 2,
                             "the model must not receive a chance to retry")
            self.assertEqual(len(ledger.of_kind("business_mutation_indeterminate")), 1)

    def test_load_query_and_finish_use_server_facts_without_page_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _ProjectedExecutor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("finish", {"done": True, "why": "Acme is the current company."}),
            ])
            browser = _Browser()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "business-react")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Which companies are in this workspace?",
                              supported_scope="Read current business data only."),
                arm="U", chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger, attestation=None, source_contract=False,
                step_budget=4, wall_budget_s=30, run_id="business-react",
                business_tools=runtime,
                plain_business_answer_min_page_actions=0)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertTrue(result.business_answer_projected)
            self.assertEqual(result.steps_used, 3)
            self.assertIn("load_skill", chat.tool_names[0])
            self.assertNotIn("query_companies", chat.tool_names[0])
            self.assertIn("wait_for", chat.tool_names[0])
            self.assertIn("query_companies", chat.tool_names[1])
            self.assertEqual(browser.actions, [])
            self.assertEqual(ledger.of_kind("action_receipt"), [])
            facts = [message["content"] for turn in chat.messages for message in turn
                     if message.get("role") == "tool"]
            self.assertTrue(any("BUSINESS SKILL LOADED" in text for text in facts))
            self.assertTrue(any('"name":"Acme"' in text for text in facts))
            self.assertEqual(len(ledger.of_kind("business_skill_loaded")), 1)
            self.assertEqual(ledger.of_kind("business_answer")[0]["terminal_tool"], "finish")
            candidates = ledger.of_kind("business_skill_candidates")
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["matcher_version"],
                             "business-skill-trigger-v2")
            self.assertFalse(candidates[0]["fallback_used"])
            self.assertEqual(candidates[0]["candidate_ids"], [skill.skill_id])
            self.assertNotIn("Which companies", json.dumps(candidates[0]))
            rows = ledger.of_kind("business_tool_result")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["audit"]["oauth_actor"], "delegated_user")
            self.assertNotIn("Acme", json.dumps(rows[0]),
                             "the tool audit stores a digest and projection, not raw rows")

    def test_plain_answer_after_successful_read_tool_is_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _ProjectedExecutor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                (None, {"content": "Acme (acme.test)"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "plain-business-answer")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Which companies are in this workspace?",
                              supported_scope="Read current business data only."),
                arm="U", chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, attestation=None, source_contract=False,
                step_budget=3, wall_budget_s=30, run_id="plain-business-answer",
                business_tools=runtime,
                plain_business_answer_min_page_actions=0)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.agent_said, "Acme (acme.test)")
            self.assertTrue(result.business_answer_projected)
            self.assertEqual(session.latest_business_tool_result.content["companies"], [{
                "name": "Acme", "domain": "acme.test",
                "ui_path": "/object/company/company-1",
            }])
            self.assertNotIn("latest_business_tool_result", result.to_json(),
                             "bounded data stays in server memory unless the product renders it")
            self.assertEqual(result.steps_used, 3)
            self.assertEqual(result.spend["provider_calls"], 0)
            self.assertEqual(len(ledger.of_kind("business_answer")), 1)
            self.assertEqual(len(ledger.of_kind("no_tool_call")), 0)

    def test_read_failure_records_its_machine_code_for_external_scoring(self):
        class UnavailableExecutor(_Executor):
            @staticmethod
            def execute(*_args, **_kwargs):
                raise BusinessToolUnavailableError("private upstream detail")

        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "typed-read-failure")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="List companies",
                              supported_scope="Read current companies."),
                arm="U", chat=_Chat([
                    ("load_skill", {"skill_id": skill.skill_id}),
                    ("query_companies", {}),
                    ("finish", {"done": False, "why": "The data source failed."}),
                ]), browser=_Browser(), control=ReadOnlyControl(), ledger=ledger,
                attestation=None, source_contract=False, step_budget=4,
                wall_budget_s=30, run_id="typed-read-failure",
                business_tools=BusinessToolRuntime((skill,), UnavailableExecutor()))
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_refused")
            rejected = ledger.of_kind("business_tool_rejected")
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["error_code"], "business_unavailable")

    def test_exact_tool_result_route_can_open_a_record_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _ProjectedExecutor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("goto", {"url": "/object/company/company-1"}),
                (None, {"content": "Opened Acme."}),
            ])
            browser = _Browser()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "business-route")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open Acme from companies.",
                              supported_scope="Read and open one company record."),
                arm="U", chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger, attestation=None, source_contract=False,
                step_budget=4, wall_budget_s=30, run_id="business-route",
                business_tools=runtime,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.agent_said, "Opened Acme.")
            self.assertEqual(result.page_actions, 1)
            self.assertFalse(result.business_answer_projected)
            self.assertEqual(browser.actions, ["goto"])
            self.assertEqual(browser.url, "http://app.test/object/company/company-1")
            opened = ledger.of_kind("business_route_opened")
            self.assertEqual(len(opened), 1)
            self.assertNotIn("company-1", json.dumps(opened[0]))

    def test_unreturned_business_route_is_not_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("goto", {"url": "/object/company/invented"}),
                ("finish", {"done": False, "why": "No verified route."}),
            ])
            browser = _Browser()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "route-refusal")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open the requested item from companies.",
                              supported_scope="Read and open one company record."),
                arm="U", chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger, attestation=None, source_contract=False,
                step_budget=4, wall_budget_s=30, run_id="route-refusal",
                business_tools=runtime,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_refused")
            self.assertEqual(browser.actions, [])
            self.assertEqual(len(ledger.of_kind("page_route_rejected")), 1)
            self.assertEqual(len(ledger.of_kind("business_route_opened")), 0)

    def test_plain_answer_waits_for_required_page_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                (None, {"content": "Opened Acme."}),
                ("goto", {"url": "/object/company/company-1"}),
                (None, {"content": "Opened Acme."}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "route-before-answer")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open Acme from companies.",
                              supported_scope="Read and open one company record."),
                arm="U", chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, attestation=None, source_contract=False,
                step_budget=5, wall_budget_s=30, run_id="route-before-answer",
                business_tools=runtime,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.page_actions, 1)
            self.assertEqual(len(ledger.of_kind("no_tool_call")), 1)
            self.assertEqual(len(ledger.of_kind("business_answer")), 1)


if __name__ == "__main__":
    unittest.main()
