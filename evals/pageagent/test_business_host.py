from __future__ import annotations

import json
import threading
import time
import unittest

from evals.pageagent.business_host import (PageAgentBusinessHostError,
                                           PageAgentBusinessProxy)
from web_operation_agent.business.runtime import (BusinessMutationSpec, BusinessSkill,
                                          BusinessToolContractError,
                                          BusinessToolIdentityError,
                                          BusinessToolRequestError,
                                          BusinessToolResult, BusinessToolRuntime,
                                          BusinessToolUnavailableError)
from web_operation_agent.state.identity import OperationContext, PrincipalContext


READ_SCHEMA = {"type": "function", "function": {
    "name": "query_companies", "description": "Read current companies.",
    "parameters": {"type": "object", "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        "additionalProperties": False}}}
WRITE_SCHEMA = {"type": "function", "function": {
    "name": "create_company", "description": "Create one company.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string"}}, "required": ["name"],
        "additionalProperties": False}}}


def _skill(*, write: bool = False) -> BusinessSkill:
    name = "create_company" if write else "query_companies"
    value = {
        "schema_version": 1,
        "skill_id": "fixture.companies-create" if write else "fixture.companies-query",
        "title": "Create companies" if write else "Query companies",
        "intent": "Use the current company capability.",
        "triggers": ["companies"],
        "instructions": [f"Call {name}."],
        "tool_names": [name],
        "provenance": ["src:fixture@v1:companies.json#companies"],
        "read_only": not write,
    }
    return BusinessSkill.from_bytes(
        json.dumps(value).encode("utf-8"), artifact_uri=f"memory://{name}.json")


class _ReadExecutor:
    calls = 0

    @staticmethod
    def tool_schemas():
        return (READ_SCHEMA,)

    @staticmethod
    def tool_effects():
        return {"query_companies": "read"}

    def execute(self, name, args, *, context=None):
        self.calls += 1
        if name != "query_companies" or args != {"limit": 20}:
            raise AssertionError("unexpected tool call")
        return BusinessToolResult(
            content={"companies": [{"name": "Synthetic Acme", "domain": "acme.test",
                                      "ui_path": "/objects/companies/acme"}]},
            audit={"returned_count": 1},
            allowed_routes=("/objects/companies/acme",),
        )


class _WriteExecutor:
    def __init__(self):
        self.dispatches = 0

    @staticmethod
    def tool_schemas():
        return (WRITE_SCHEMA,)

    @staticmethod
    def tool_effects():
        return {"create_company": "write"}

    @staticmethod
    def execute(*_args, **_kwargs):
        raise AssertionError("write executed")

    @staticmethod
    def prepare_mutation(name, args, **_kwargs):
        if name != "create_company" or set(args) != {"name"}:
            raise AssertionError("unexpected mutation proposal")
        return BusinessMutationSpec(
            args=dict(args), effect="Create one company", targets=(args["name"],))

    def execute_mutation(self, name, args, **_kwargs):
        if name != "create_company" or set(args) != {"name"}:
            raise AssertionError("unexpected mutation dispatch")
        self.dispatches += 1
        route = "/objects/companies/created"
        return BusinessToolResult(
            content={"created": True, "readback_verified": True,
                     "company": {"name": args["name"], "domain": "",
                                 "ui_path": route}},
            audit={"records_created": 1}, allowed_routes=(route,))


def _context(*, write: bool = False) -> OperationContext:
    now = int(time.time() * 1000)
    principal = PrincipalContext(
        schema_version=1, application_id="fixture", tenant_id="tenant",
        workspace_id="workspace", actor_id="actor", delegation_id="delegation",
        issued_at=now, expires_at=now + 60_000,
        scopes=(("companies:write",) if write else ("companies:read",)))
    return OperationContext.create(
        principal, session_id="session", turn_id="turn", operation_id="operation",
        deadline_ms=now + 30_000, now_ms=now)


def _runtime(*, write: bool = False):
    skill = _skill(write=write)
    runtime = BusinessToolRuntime(
        (skill,), _WriteExecutor() if write else _ReadExecutor(), application_id="fixture",
        required_scopes={
            "create_company" if write else "query_companies":
                "companies:write" if write else "companies:read"})
    return runtime, skill


class BusinessProxyTests(unittest.TestCase):
    def _proxy(self, *, cancelled=None, max_calls=2):
        runtime, skill = _runtime()
        return PageAgentBusinessProxy(
            run_token="opaque-business-token", runtime=runtime,
            loaded_skill_ids=(skill.skill_id,), context=_context(),
            expected_catalog_sha256=runtime.catalog_sha256,
            max_business_calls=max_calls, max_route_calls=1, cancelled=cancelled)

    def test_configuration_comes_from_the_exact_runtime_skill_and_schema(self):
        proxy = self._proxy()
        self.assertIn("APP SKILLS", proxy.instructions)
        self.assertIn("query_companies", proxy.instructions)
        self.assertEqual(proxy.tool_specs[0]["name"], "query_companies")
        self.assertEqual(proxy.tool_specs[0]["input_schema"]["required"], [])
        self.assertRegex(proxy.skill_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(proxy.tool_catalog_digest, r"^sha256:[0-9a-f]{64}$")

    def test_read_call_is_principal_bound_and_routes_are_retained(self):
        proxy = self._proxy()
        result = proxy.handle({
            "run_token": "opaque-business-token", "name": "query_companies",
            "args": {"limit": 20},
        })
        self.assertEqual(result["companies"][0]["name"], "Synthetic Acme")
        self.assertEqual(proxy.allowed_routes, frozenset({"/objects/companies/acme"}))
        permit = proxy.handle_route({
            "run_token": "opaque-business-token",
            "route": "/objects/companies/acme",
        })
        self.assertEqual(permit, {
            "allowed": True, "route": "/objects/companies/acme"})
        report = proxy.report()
        self.assertEqual(report["business_attempts"], 1)
        self.assertEqual(report["business_responses"], 1)
        self.assertEqual(report["route_attempts"], 1)
        self.assertEqual(report["route_permits"], 1)
        self.assertNotIn("Synthetic Acme", json.dumps(report))

    def test_typed_tool_failures_cross_the_browser_boundary_as_content_free_codes(self):
        cases = (
            (BusinessToolRequestError("secret request detail"), "business_request"),
            (BusinessToolIdentityError("secret identity detail"), "business_identity"),
            (BusinessToolUnavailableError("secret outage detail"), "business_unavailable"),
            (BusinessToolContractError("secret payload detail"), "business_contract"),
        )
        for failure, expected in cases:
            with self.subTest(code=expected):
                class FailingExecutor(_ReadExecutor):
                    def execute(self, *_args, **_kwargs):
                        raise failure

                skill = _skill()
                runtime = BusinessToolRuntime(
                    (skill,), FailingExecutor(), application_id="fixture",
                    required_scopes={"query_companies": "companies:read"})
                proxy = PageAgentBusinessProxy(
                    run_token="opaque-business-token", runtime=runtime,
                    loaded_skill_ids=(skill.skill_id,), context=_context(),
                    expected_catalog_sha256=runtime.catalog_sha256,
                    max_business_calls=1, max_route_calls=0)
                with self.assertRaises(PageAgentBusinessHostError) as raised:
                    proxy.handle({
                        "run_token": "opaque-business-token",
                        "name": "query_companies", "args": {"limit": 20}})
                self.assertEqual(raised.exception.code, expected)
                self.assertNotIn("secret", str(raised.exception))
                self.assertEqual(proxy.report()["last_error_code"], expected)

    def test_route_requires_a_fresh_tool_disclosure_and_has_its_own_budget(self):
        proxy = self._proxy()
        route_call = {
            "run_token": "opaque-business-token",
            "route": "/objects/companies/acme",
        }
        with self.assertRaisesRegex(PageAgentBusinessHostError, "route_refused"):
            proxy.handle_route(route_call)
        proxy.handle({
            "run_token": "opaque-business-token", "name": "query_companies",
            "args": {"limit": 20},
        })
        with self.assertRaisesRegex(PageAgentBusinessHostError, "route_call_budget"):
            proxy.handle_route(route_call)

    def test_wrong_token_call_budget_and_cancellation_fail_closed(self):
        proxy = self._proxy(max_calls=1)
        with self.assertRaisesRegex(PageAgentBusinessHostError, "tool_contract"):
            proxy.handle({"run_token": "wrong-business-token", "name": "query_companies",
                          "args": {"limit": 20}})
        call = {"run_token": "opaque-business-token", "name": "query_companies",
                "args": {"limit": 20}}
        proxy.handle(call)
        with self.assertRaisesRegex(PageAgentBusinessHostError, "business_call_budget"):
            proxy.handle(call)

        cancelled = threading.Event()
        cancelled.set()
        proxy = self._proxy(cancelled=cancelled)
        with self.assertRaisesRegex(PageAgentBusinessHostError, "cancelled"):
            proxy.handle(call)

    def test_catalog_mismatch_and_write_skill_are_rejected_at_construction(self):
        runtime, skill = _runtime()
        with self.assertRaisesRegex(ValueError, "catalog"):
            PageAgentBusinessProxy(
                run_token="opaque-business-token", runtime=runtime,
                loaded_skill_ids=(skill.skill_id,), context=_context(),
                expected_catalog_sha256="0" * 64, max_business_calls=1,
                max_route_calls=1)
        write_runtime, write_skill = _runtime(write=True)
        with self.assertRaisesRegex(ValueError, "read-only"):
            PageAgentBusinessProxy(
                run_token="opaque-business-token", runtime=write_runtime,
                loaded_skill_ids=(write_skill.skill_id,), context=_context(),
                expected_catalog_sha256=write_runtime.catalog_sha256,
                max_business_calls=1, max_route_calls=1)

    def test_mutation_requires_one_digest_bound_decision_and_cannot_replay(self):
        runtime, skill = _runtime(write=True)
        proxy = PageAgentBusinessProxy(
            run_token="opaque-business-token", runtime=runtime,
            loaded_skill_ids=(skill.skill_id,), context=_context(write=True),
            expected_catalog_sha256=runtime.catalog_sha256,
            max_business_calls=2, max_route_calls=1, allow_mutations=True)
        proposal = proxy.handle({
            "run_token": "opaque-business-token", "name": "create_company",
            "args": {"name": "Synthetic Acme"},
        })["__woa_mutation_approval__"]
        self.assertEqual(proposal["effect"], "Create one company")
        self.assertEqual(proposal["targets"], ["Synthetic Acme"])
        result = proxy.handle_approval({
            "run_token": "opaque-business-token",
            "proposal_digest": proposal["proposal_digest"],
            "decision": "approved",
        })
        self.assertTrue(result["created"])
        self.assertEqual(runtime._executor.dispatches, 1)
        with self.assertRaisesRegex(PageAgentBusinessHostError, "approval_replay"):
            proxy.handle_approval({
                "run_token": "opaque-business-token",
                "proposal_digest": proposal["proposal_digest"],
                "decision": "approved",
            })
        self.assertEqual(runtime._executor.dispatches, 1)
        report = proxy.report()
        self.assertEqual(report["approvals_requested"], 1)
        self.assertEqual(report["approvals_approved"], 1)
        self.assertEqual(report["mutation_dispatches"], 1)

    def test_denied_mutation_is_consumed_without_dispatch(self):
        runtime, skill = _runtime(write=True)
        proxy = PageAgentBusinessProxy(
            run_token="opaque-business-token", runtime=runtime,
            loaded_skill_ids=(skill.skill_id,), context=_context(write=True),
            expected_catalog_sha256=runtime.catalog_sha256,
            max_business_calls=1, max_route_calls=0, allow_mutations=True)
        proposal = proxy.handle({
            "run_token": "opaque-business-token", "name": "create_company",
            "args": {"name": "Synthetic Acme"},
        })["__woa_mutation_approval__"]
        with self.assertRaisesRegex(PageAgentBusinessHostError, "approval_denied"):
            proxy.handle_approval({
                "run_token": "opaque-business-token",
                "proposal_digest": proposal["proposal_digest"],
                "decision": "denied",
            })
        self.assertEqual(runtime._executor.dispatches, 0)
        self.assertEqual(proxy.report()["approvals_denied"], 1)


if __name__ == "__main__":
    unittest.main()
