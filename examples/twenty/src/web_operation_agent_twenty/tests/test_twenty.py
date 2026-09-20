"""Twenty application adapter and business tools stay bounded and write-safe."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import uuid
from importlib import resources
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from web_operation_agent.business.runtime import (BusinessMutationIndeterminate,
                                          BusinessSkill,
                                          BusinessToolAuthenticationError,
                                          BusinessToolError,
                                          BusinessToolPermissionError,
                                          BusinessToolRuntime,
                                          BusinessToolUnavailableError)
from web_operation_agent.identity import IdentityAssertion, PrincipalError
from web_operation_agent.business.mutation import (MemoryMutationJournal,
                                          SQLiteMutationJournal)
from web_operation_agent_twenty.adapter import (MUTATION_SPECS, READ_SPECS,
                                        TwentyApplicationAdapter,
                                        TwentyBusinessToolExecutor,
                                        TwentyOAuthTokenStore,
                                        TwentyPageSessionIdentityResolver,
                                        normalize_twenty_domain)
from web_operation_agent.tests.adapter_contract import assert_adapter_contract


def _adapter(**kwargs) -> TwentyApplicationAdapter:
    return TwentyApplicationAdapter(
        base_url="http://127.0.0.1:3000",
        token_path="tokens.json",
        registration_path="registration.json",
        **kwargs,
    )


class TwentyToolSpecificationTests(unittest.TestCase):
    def test_schema_authority_and_execution_come_from_one_catalog(self):
        executor = TwentyBusinessToolExecutor(
            base_url="http://127.0.0.1:3000", tokens=object(), allow_mutations=True)
        offered = {schema["function"]["name"] for schema in executor.tool_schemas()}
        for spec in (*READ_SPECS, *MUTATION_SPECS):
            with self.subTest(tool=spec.name):
                self.assertIn(spec.name, offered)
                self.assertEqual(spec.schema["function"]["name"], spec.name)
                self.assertTrue(callable(getattr(executor, spec.execute_method)))
                self.assertTrue(spec.scope.endswith(":read") or spec.scope.endswith(":write"))


class TwentyPackagedSkillTests(unittest.TestCase):
    def test_default_adapter_loads_query_skill_outside_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            adapter = _adapter()
            bound = assert_adapter_contract(self, adapter)

        self.assertEqual(
            adapter.business_tools.tool_names,
            frozenset({"query_companies", "query_people", "query_opportunities"}))
        skills = adapter.business_tools.relevant("Which companies are available?")
        self.assertEqual([skill.skill_id for skill in skills],
                         ["twenty.crm.companies-query"])
        self.assertEqual(
            [skill.skill_id for skill in adapter.business_tools.relevant(
                "列出当前所有联系人", limit=3)],
            ["twenty.crm.people-query"],
        )
        self.assertEqual(
            [skill.skill_id for skill in adapter.business_tools.relevant(
                "Open the API Integration Deal opportunity", limit=3)],
            ["twenty.crm.opportunities-query"],
        )
        self.assertTrue(any(
            "Companies list" in instruction and "/objects/companies" in instruction
            for instruction in skills[0].instructions
        ))
        self.assertEqual(
            skills[0].artifact_uri,
            "package://web_operation_agent_twenty/skills/companies-query.json",
        )
        self.assertEqual(adapter.descriptor.application_id, "twenty")
        self.assertIs(bound.business_tools, adapter.business_tools)
        self.assertEqual(adapter.descriptor.navigation_mode, "spa")
        self.assertEqual(
            adapter.descriptor.catalog_sha256,
            adapter.business_tools.catalog_sha256,
        )

    def test_mutation_skill_requires_an_explicit_capability(self):
        with self.assertRaisesRegex(ValueError, "allow_mutations=True"):
            _adapter(mutation_skill_path="external-create-skill.json")
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            _adapter(allow_mutations="yes")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            TwentyBusinessToolExecutor(
                base_url="http://127.0.0.1:3000", tokens=object(),
                allow_mutations=1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "explicit mutation journal"):
            _adapter(allow_mutations=True)

        adapter = _adapter(
            allow_mutations=True, mutation_journal=MemoryMutationJournal())
        self.assertEqual(
            adapter.business_tools.tool_names,
            frozenset({"query_companies", "query_people", "query_opportunities",
                       "create_company"}),
        )
        create_prompts = (
            "创建公司 Acme，域名 acme.example",
            "创建名为 tom 的公司，域名 tom.com",
            "新增一个公司 Acme",
            "把 Acme 加进 CRM",
            "create a company Acme",
            "add a new company Acme",
            "Register Acme Corp as a customer",
        )
        for prompt in create_prompts:
            with self.subTest(prompt=prompt):
                relevant = adapter.business_tools.relevant(prompt, limit=3)
                self.assertEqual(
                    [skill.skill_id for skill in relevant],
                    ["twenty.crm.companies-create"],
                )
        fallback_prompts = (
            "Onboard Acme Corp (acme.com)",
            "把 Acme 录入系统",
            "Please set up a new account for Acme",
        )
        for prompt in fallback_prompts:
            with self.subTest(prompt=prompt):
                selection = adapter.business_tools.select(
                    prompt, allow_mutations=True)
                self.assertTrue(selection.fallback_used)
                self.assertEqual(
                    list(selection.candidate_ids),
                    ["twenty.crm.companies-create",
                     "twenty.crm.opportunities-query", "twenty.crm.people-query"],
                )
        person_request = adapter.business_tools.select(
            "Create a person named Bob at Acme company",
            allow_mutations=True)
        self.assertNotIn(
            "twenty.crm.companies-create", person_request.candidate_ids,
            "a company name inside a person request must not expose company creation",
        )
        self.assertEqual(
            adapter.business_tools.relevant("check my account settings", limit=3), ())
        card = adapter.business_tools.relevant(create_prompts[0])[0].candidate_card()
        self.assertFalse(card["read_only"])
        self.assertEqual(card["tools"], ["query_companies", "create_company"])
        self.assertTrue(all(
            skill.artifact_uri.startswith(
                "package://web_operation_agent_twenty/skills/")
            for skill in relevant))

    def test_explicit_filesystem_skill_paths_remain_supported(self):
        packaged = resources.files("web_operation_agent_twenty").joinpath("skills")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = root / "companies-query.json"
            create = root / "companies-create.json"
            query.write_bytes(packaged.joinpath("companies-query.json").read_bytes())
            create.write_bytes(packaged.joinpath("companies-create.json").read_bytes())
            adapter = _adapter(
                skill_path=query,
                mutation_skill_path=create,
                allow_mutations=True,
                mutation_journal=MemoryMutationJournal(),
            )

        self.assertEqual(
            adapter.business_tools.tool_names,
            frozenset({"query_companies", "query_people", "query_opportunities",
                       "create_company"}),
        )
        relevant = adapter.business_tools.relevant("create company", limit=3)
        self.assertTrue(all(not skill.artifact_uri.startswith("package://")
                            for skill in relevant))

    def test_adapter_binds_only_matching_page_and_oauth_identity(self):
        adapter = _adapter(
            allow_mutations=True, mutation_journal=MemoryMutationJournal())
        page = IdentityAssertion(
            1, "page_session", "twenty", "workspace-1", "workspace-1",
            "actor-1", 1_000_000, 1_060_000)
        delegated = IdentityAssertion(
            1, "delegated_api", "twenty", "workspace-1", "workspace-1",
            "actor-1", 1_000_000, 1_060_000)
        adapter._executor.resolve_delegated_identity = Mock(return_value=delegated)

        principal = adapter.bind_principal(
            page, delegation_id="grant-1", now_ms=1_000_001)
        self.assertEqual(
            principal.scopes, ("companies:read", "companies:write",
                               "opportunities:read", "people:read"))
        adapter._executor.resolve_delegated_identity = Mock(return_value=IdentityAssertion(
            1, "delegated_api", "twenty", "workspace-1", "workspace-1",
            "actor-2", 1_000_000, 1_060_000))
        with self.assertRaisesRegex(PrincipalError, "different principals"):
            adapter.bind_principal(
                page, delegation_id="grant-2", now_ms=1_000_001)


class TwentyNormalizationTests(unittest.TestCase):
    def test_domain_normalization_matches_api_and_database_shapes(self):
        self.assertEqual(normalize_twenty_domain("HTTPS://Example.COM/"), "example.com")

    def test_metadata_identity_uses_the_bounded_graphql_endpoint(self):
        class Tokens:
            @staticmethod
            def access_token():
                return "test-token"

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                self.close()

        response = Response(json.dumps({"data": {
            "currentUser": {"id": "actor-1"},
            "currentWorkspace": {"id": "workspace-1"},
        }}).encode())
        executor = TwentyBusinessToolExecutor(
            base_url="http://127.0.0.1:3000", tokens=Tokens())
        with patch("web_operation_agent_twenty.adapter.urlopen", return_value=response) as opened:
            identity = executor.resolve_delegated_identity(now_ms=1_000_000)

        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:3000/metadata")
        self.assertEqual(request.get_method(), "POST")
        body = json.loads(request.data)
        self.assertIn("currentUser", body["query"])
        self.assertIn("currentWorkspace", body["query"])
        self.assertNotIn("companies", body["query"])
        self.assertEqual(identity.actor_id, "actor-1")

    def test_metadata_identity_refreshes_after_graphql_unauthenticated(self):
        class Tokens:
            refreshed = []

            @staticmethod
            def access_token():
                return "expired-token"

            @classmethod
            def refresh(cls, base_url, timeout_s, *, rejected_access_token=None):
                cls.refreshed.append((base_url, timeout_s, rejected_access_token))
                return "fresh-token"

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                self.close()

        expired = Response(json.dumps({"errors": [{
            "message": "Token has expired.",
            "extensions": {"code": "UNAUTHENTICATED"},
        }]}).encode())
        success = Response(json.dumps({"data": {
            "currentUser": {"id": "actor-1"},
            "currentWorkspace": {"id": "workspace-1"},
        }}).encode())
        executor = TwentyBusinessToolExecutor(
            base_url="http://127.0.0.1:3000", tokens=Tokens())
        with patch("web_operation_agent_twenty.adapter.urlopen",
                   side_effect=[expired, success]) as opened:
            identity = executor.resolve_delegated_identity(now_ms=1_000_000)

        self.assertEqual(identity.actor_id, "actor-1")
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(
            Tokens.refreshed,
            [("http://127.0.0.1:3000", 15, "expired-token")],
        )
        second_request = opened.call_args_list[1].args[0]
        self.assertEqual(second_request.headers["Authorization"], "Bearer fresh-token")


class TwentyPageSessionIdentityTests(unittest.TestCase):
    @staticmethod
    def _write_state(path: Path, *, cookies: list[dict]) -> None:
        path.write_text(json.dumps({
            "cookies": cookies,
            "origins": [{"origin": "http://127.0.0.1:3000", "localStorage": [{
                "name": "currentUserState", "value": "untrusted and ignored",
            }]}],
        }), encoding="utf-8")
        path.chmod(0o600)

    def test_http_only_cookie_resolves_identity_without_sending_other_state(self):
        response_body = json.dumps({"data": {
            "currentUser": {"id": "actor-1"},
            "currentWorkspace": {"id": "workspace-1"},
        }}).encode()

        class Response:
            status = 200

            @staticmethod
            def read(_limit):
                return response_body

        class Connection:
            instance = None

            def __init__(self, host, port, timeout):
                self.host, self.port, self.timeout = host, port, timeout
                self.request_args = None
                self.closed = False
                Connection.instance = self

            def request(self, *args, **kwargs):
                self.request_args = (args, kwargs)

            @staticmethod
            def getresponse():
                return Response()

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "storage.json"
            self._write_state(state, cookies=[
                {"name": "analyticsCookie", "value": "tracking", "domain": "127.0.0.1",
                 "path": "/", "httpOnly": False, "secure": False, "expires": 2_000},
                {"name": "twenty-session", "value": "session-value",
                 "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                 "secure": False, "expires": 1_020},
            ])
            resolver = TwentyPageSessionIdentityResolver(
                base_url="http://127.0.0.1:3000", storage_state_path=state)
            file_binding = resolver.binding_digest(now_ms=1_000_000)
            memory_binding = TwentyPageSessionIdentityResolver(
                base_url="http://127.0.0.1:3000",
                storage_state=json.loads(state.read_text()),
            ).binding_digest(now_ms=1_000_000)
            with patch("web_operation_agent_twenty.adapter.HTTPConnection", Connection):
                identity = resolver.resolve(lifetime_s=60, now_ms=1_000_000)

        connection = Connection.instance
        self.assertEqual((connection.host, connection.port), ("127.0.0.1", 3000))
        self.assertTrue(connection.closed)
        args, kwargs = connection.request_args
        self.assertEqual(args[:2], ("POST", "/metadata"))
        self.assertEqual(kwargs["headers"]["Cookie"], "twenty-session=session-value")
        self.assertEqual(kwargs["headers"]["Origin"], "http://127.0.0.1:3000")
        self.assertNotIn("tracking", str(connection.request_args))
        self.assertNotIn("currentUserState", str(connection.request_args))
        self.assertEqual(file_binding, memory_binding)
        self.assertNotIn("session-value", file_binding)
        self.assertEqual(len(file_binding), 64)
        self.assertEqual(identity.source, "page_session")
        self.assertEqual(identity.actor_id, "actor-1")
        self.assertEqual(identity.workspace_id, "workspace-1")
        self.assertEqual(identity.expires_at, 1_020_000)

    def test_missing_expired_mismatched_or_duplicate_session_cookie_fails_closed(self):
        cases = [
            [],
            [{"name": "twenty-session", "value": "x", "domain": "127.0.0.1",
              "path": "/", "httpOnly": True, "secure": False, "expires": 999}],
            [{"name": "twenty-session", "value": "x", "domain": "other.test",
              "path": "/", "httpOnly": True, "secure": False, "expires": 2_000}],
            [
                {"name": "twenty-session", "value": "x", "domain": "127.0.0.1",
                 "path": "/", "httpOnly": True, "secure": False, "expires": 2_000},
                {"name": "twenty-session", "value": "y", "domain": "127.0.0.1",
                 "path": "/", "httpOnly": True, "secure": False, "expires": 2_000},
            ],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "storage.json"
            for cookies in cases:
                with self.subTest(cookies=len(cookies)):
                    self._write_state(state, cookies=cookies)
                    resolver = TwentyPageSessionIdentityResolver(
                        base_url="http://127.0.0.1:3000", storage_state_path=state)
                    with self.assertRaisesRegex(BusinessToolError, "one live session cookie"):
                        resolver.resolve(now_ms=1_000_000)

    def test_cookie_shape_and_storage_size_are_bounded_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "storage.json"
            self._write_state(state, cookies=[{
                "name": "twenty-session", "value": "bad;cookie",
                "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                "secure": False, "expires": 2_000,
            }])
            resolver = TwentyPageSessionIdentityResolver(
                base_url="http://127.0.0.1:3000", storage_state_path=state)
            with self.assertRaisesRegex(BusinessToolError, "cookie is invalid"):
                resolver.resolve(now_ms=1_000_000)

            state.write_bytes(b" " * (2 * 1024 * 1024 + 1))
            with self.assertRaisesRegex(BusinessToolError, "exceeds 2 MiB"):
                resolver.resolve(now_ms=1_000_000)

            self._write_state(state, cookies=[{
                "name": "twenty-session", "value": "valid-cookie",
                "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                "secure": False, "expires": 2_000,
            }])
            state.chmod(0o644)
            with self.assertRaisesRegex(BusinessToolError, "group/world"):
                resolver.resolve(now_ms=1_000_000)


class TwentyPaginationTests(unittest.TestCase):
    @staticmethod
    def _rows(start: int, count: int) -> list[dict]:
        return [{
            "id": str(uuid.UUID(int=index + 1)),
            "name": f"Company {index:03d}",
            "domainName": {"primaryLinkLabel": f"company-{index}.example"},
        } for index in range(start, start + count)]

    def test_people_and_opportunity_queries_project_bounded_routes_and_answers(self):
        class RelatedTwenty(TwentyBusinessToolExecutor):
            def __init__(self):
                super().__init__(base_url="http://127.0.0.1:3000", tokens=object())

            def _get_collection(self, resource):
                rows = ({
                    "people": [{"id": "7a93d1e5-3f74-4945-8a65-d7f996083f72",
                                "name": {"firstName": "Ivan", "lastName": "Zhao"},
                                "emails": {"primaryEmail": "zhao@example.com"}}],
                    "opportunities": [{
                        "id": "2beb07b0-340c-41d7-be33-5aa91757f329",
                        "name": "API Integration Deal", "stage": "SCREENING"}],
                })[resource]
                return ({"totalCount": 1, "data": {resource: rows},
                         "_woaPagination": {"complete": True, "records_scanned": 1}},
                        200, False)

        executor = RelatedTwenty()
        people = executor.execute("query_people", {"name_contains": "zhao"})
        self.assertEqual(people.content["people"][0]["email"], "zhao@example.com")
        self.assertEqual(people.allowed_routes,
                         ("/objects/people",
                          "/object/person/7a93d1e5-3f74-4945-8a65-d7f996083f72"))
        self.assertEqual(people.query_answer.collection, "people")
        opportunities = executor.execute("query_opportunities", {"limit": 1})
        self.assertEqual(opportunities.content["opportunities"][0]["stage"], "SCREENING")
        self.assertEqual(opportunities.allowed_routes,
                         ("/objects/opportunities",
                          "/object/opportunity/2beb07b0-340c-41d7-be33-5aa91757f329"))
        self.assertTrue(opportunities.query_answer.source_complete)

    def test_company_query_scans_every_cursor_page_before_answering(self):
        first = self._rows(0, 100)
        second = self._rows(100, 50)

        class Tokens:
            @staticmethod
            def access_token():
                return "token"

        class FixtureTwenty(TwentyBusinessToolExecutor):
            def __init__(self):
                super().__init__(
                    base_url="http://127.0.0.1:3000", tokens=Tokens())
                self.paths = []

            def _authorized_json(self, path, token):
                self.paths.append(path)
                if len(self.paths) == 1:
                    return {"totalCount": 150, "data": {"companies": first},
                            "pageInfo": {"hasNextPage": True,
                                         "endCursor": "cursor/one+="}}
                if len(self.paths) == 2:
                    return {"totalCount": 150, "data": {"companies": second},
                            "pageInfo": {"hasNextPage": False,
                                         "endCursor": "cursor/two+="}}
                raise AssertionError("unexpected page")

        executor = FixtureTwenty()
        result = executor.execute("query_companies", {"limit": 50})

        self.assertEqual(executor.paths, [
            "/rest/companies?limit=100",
            "/rest/companies?limit=100&starting_after=cursor%2Fone%2B%3D",
        ])
        self.assertEqual(result.content["source_total"], 150)
        self.assertEqual(result.content["matched_count"], 150)
        self.assertEqual(len(result.content["companies"]), 50)
        self.assertTrue(result.content["completeness"]["source_complete"])
        self.assertEqual(result.content["completeness"]["pages_scanned"], 2)
        self.assertEqual(result.content["completeness"]["incomplete_reason"], "")
        self.assertTrue(result.content["completeness"]["result_truncated"])
        projection = result.query_answer.to_json()
        self.assertEqual(len(projection["records"]), 50)
        self.assertEqual(projection["records"][0], {
            "name": "Company 000", "domain": "company-0.example"})
        self.assertTrue(projection["completeness"]["source_complete"])
        self.assertTrue(projection["completeness"]["result_truncated"])
        self.assertEqual(projection["completeness"]["source_total"], 150)
        self.assertNotIn("ui_path", projection["records"][0])

    def test_nonadvancing_cursor_returns_an_explicit_partial_result(self):
        pages = [self._rows(0, 2), self._rows(2, 2)]

        class Tokens:
            @staticmethod
            def access_token():
                return "token"

        class FixtureTwenty(TwentyBusinessToolExecutor):
            def __init__(self):
                super().__init__(
                    base_url="http://127.0.0.1:3000", tokens=Tokens())
                self.index = 0

            def _authorized_json(self, _path, _token):
                rows = pages[self.index]
                self.index += 1
                return {"totalCount": 5, "data": {"companies": rows},
                        "pageInfo": {"hasNextPage": True,
                                     "endCursor": "same-cursor"}}

        with patch("web_operation_agent_twenty.adapter._COMPANY_PAGE_SIZE", 2):
            result = FixtureTwenty().execute("query_companies", {"limit": 5})

        completeness = result.content["completeness"]
        self.assertFalse(completeness["source_complete"])
        self.assertTrue(completeness["result_truncated"])
        self.assertEqual(completeness["incomplete_reason"], "cursor_did_not_advance")
        self.assertEqual(completeness["records_scanned"], 4)
        self.assertFalse(result.query_answer.complete)
        self.assertEqual(
            result.query_answer.incomplete_reason, "cursor_did_not_advance")

    def test_page_cap_is_partial_and_duplicate_preflight_fails_closed(self):
        class Tokens:
            @staticmethod
            def access_token():
                return "token"

        class FixtureTwenty(TwentyBusinessToolExecutor):
            def __init__(self):
                super().__init__(
                    base_url="http://127.0.0.1:3000", tokens=Tokens(),
                    allow_mutations=True)
                self.page = 0

            def _authorized_json(self, _path, _token):
                rows = TwentyPaginationTests._rows(self.page * 2, 2)
                self.page += 1
                return {"totalCount": 6, "data": {"companies": rows},
                        "pageInfo": {"hasNextPage": True,
                                     "endCursor": f"cursor-{self.page}"}}

        executor = FixtureTwenty()
        with patch("web_operation_agent_twenty.adapter._COMPANY_PAGE_SIZE", 2), \
                patch("web_operation_agent_twenty.adapter._MAX_COMPANY_PAGES", 2):
            result = executor.execute("query_companies", {"limit": 5})
            self.assertEqual(
                result.content["completeness"]["incomplete_reason"],
                "page_limit_reached")
            with self.assertRaisesRegex(BusinessToolError, "incomplete"):
                executor.prepare_mutation(
                    "create_company", {"name": "New", "domain": "new.example"})


class TwentyMutationFaultTests(unittest.TestCase):
    def test_lost_ack_after_commit_is_indeterminate_and_never_dispatched_twice(self):
        record_id = str(uuid.uuid4())

        class Tokens:
            @staticmethod
            def access_token():
                return "token"

        class LostAcknowledgementTwenty(TwentyBusinessToolExecutor):
            def __init__(self):
                super().__init__(
                    base_url="http://127.0.0.1:3000", tokens=Tokens(),
                    allow_mutations=True)
                self.rows = []
                self.posts = 0

            def _get_companies(self):
                return ({"totalCount": len(self.rows),
                         "data": {"companies": list(self.rows)},
                         "_woaPagination": {"complete": True}}, 200, False)

            def _authorized_mutation_json(self, _path, _token, body):
                self.posts += 1
                self.rows.append({"id": record_id, "name": body["name"],
                                  "domainName": body["domainName"]})
                raise BusinessMutationIndeterminate(
                    "Twenty mutation acknowledgement was unavailable; outcome is unknown")

        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteMutationJournal(Path(directory) / "mutations.sqlite3")
            executor = LostAcknowledgementTwenty()
            skill_root = resources.files("web_operation_agent_twenty").joinpath("skills")
            skills = tuple(BusinessSkill.load(skill_root.joinpath(name)) for name in (
                "companies-create.json", "people-query.json", "opportunities-query.json"))
            runtime = BusinessToolRuntime(
                skills, executor, mutation_journal=journal)
            loaded = {skill.skill_id for skill in skills}
            proposal = runtime.prepare_mutation(
                "create_company",
                {"name": "Fault Probe", "domain": "fault-probe.example"}, loaded)

            with self.assertRaises(BusinessMutationIndeterminate):
                runtime.execute_mutation(proposal, proposal.content_digest, loaded)
            intent = journal.get(proposal.content_digest)
            self.assertIsNotNone(intent)
            self.assertEqual(intent.state, "indeterminate")
            self.assertEqual(executor.posts, 1)
            self.assertEqual(len(executor.rows), 1)

            with self.assertRaisesRegex(BusinessToolError, "already indeterminate"):
                runtime.execute_mutation(proposal, proposal.content_digest, loaded)
            self.assertEqual(executor.posts, 1)
            self.assertEqual(len(executor.rows), 1)


class TwentyPermissionRefusalTests(unittest.TestCase):
    """HTTP 403 is Twenty's permissions refusing a working token -- not an outage."""

    class _Tokens:
        @staticmethod
        def access_token():
            return "token"

    @staticmethod
    def _refused(code: int) -> HTTPError:
        return HTTPError("http://127.0.0.1:3000/rest/companies", code, "refused", {},
                         io.BytesIO(b"{}"))

    def _executor(self, **kwargs) -> TwentyBusinessToolExecutor:
        return TwentyBusinessToolExecutor(
            base_url="http://127.0.0.1:3000", tokens=self._Tokens(), **kwargs)

    def test_a_refused_read_is_a_permission_result_and_other_failures_are_unchanged(self):
        executor = self._executor()
        for code, expected in ((403, BusinessToolPermissionError),
                               (401, BusinessToolAuthenticationError),
                               (500, BusinessToolUnavailableError)):
            with self.subTest(code), patch("web_operation_agent_twenty.adapter.urlopen",
                                           side_effect=self._refused(code)):
                with self.assertRaises(expected) as raised:
                    executor._authorized_json("/rest/companies", "token")
                if code == 403:
                    self.assertEqual(raised.exception.layer, "application")
                else:
                    self.assertNotIsInstance(raised.exception, BusinessToolPermissionError)

    def test_a_refused_mutation_is_definite_and_other_client_errors_are_unchanged(self):
        executor = self._executor(allow_mutations=True)
        with patch("web_operation_agent_twenty.adapter.urlopen",
                   side_effect=self._refused(403)):
            with self.assertRaises(BusinessToolPermissionError):
                executor._authorized_mutation_json("/rest/companies", "token", {})
        with patch("web_operation_agent_twenty.adapter.urlopen",
                   side_effect=self._refused(422)):
            with self.assertRaises(BusinessToolError) as raised:
                executor._authorized_mutation_json("/rest/companies", "token", {})
            self.assertNotIsInstance(raised.exception, BusinessToolPermissionError)
        with patch("web_operation_agent_twenty.adapter.urlopen",
                   side_effect=self._refused(502)):
            with self.assertRaises(BusinessMutationIndeterminate):
                executor._authorized_mutation_json("/rest/companies", "token", {})

    def test_a_permission_refused_readback_after_a_create_stays_indeterminate(self):
        """The record exists; being refused the readback cannot make that "nothing changed"."""
        executor = self._executor(allow_mutations=True)
        record_id = str(uuid.uuid4())

        class Created(io.BytesIO):
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                self.close()

        created = Created(json.dumps({"data": {"createCompany": {"id": record_id}}}).encode())
        with patch.object(executor, "_assert_no_duplicate"), \
                patch("web_operation_agent_twenty.adapter.urlopen",
                      side_effect=[created, self._refused(403)]):
            with self.assertRaises(BusinessMutationIndeterminate) as raised:
                executor.execute_mutation(
                    "create_company", {"name": "Acme", "domain": "acme.example"})
        self.assertEqual(raised.exception.phase, "readback")
        self.assertEqual(raised.exception.record_id, record_id)


class TwentyOAuthRefreshTests(unittest.TestCase):
    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()

    def test_concurrent_rejected_token_causes_one_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tokens = root / "tokens.json"
            registration = root / "registration.json"
            tokens.write_text(json.dumps({
                "access_token": "expired", "refresh_token": "refresh"}))
            registration.write_text(json.dumps({"client_id": "client"}))
            store = TwentyOAuthTokenStore(tokens, registration)
            calls = 0
            calls_lock = threading.Lock()

            def open_refresh(_request, timeout):
                nonlocal calls
                self.assertEqual(timeout, 3)
                with calls_lock:
                    calls += 1
                time.sleep(0.02)
                return self.Response(json.dumps({
                    "access_token": "new-token",
                    "refresh_token": "new-refresh",
                }).encode())

            results: list[str] = []
            errors: list[BaseException] = []

            def worker():
                try:
                    results.append(store.refresh(
                        "http://127.0.0.1:3000", 3,
                        rejected_access_token="expired"))
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            with patch("web_operation_agent_twenty.adapter.urlopen", side_effect=open_refresh):
                threads = [threading.Thread(target=worker) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(calls, 1)
            self.assertEqual(results, ["new-token"] * 8)
            self.assertEqual(store.access_token(), "new-token")
            self.assertEqual(tokens.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
