"""Twenty application adapter and business tools stay bounded and write-safe."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from importlib import resources
from pathlib import Path
from unittest.mock import Mock, patch

from web_operation_agent.identity import IdentityAssertion, PrincipalError
from web_operation_agent.twenty import (TwentyApplicationAdapter,
                                        TwentyBusinessToolExecutor,
                                        normalize_twenty_domain)


def _adapter(**kwargs) -> TwentyApplicationAdapter:
    return TwentyApplicationAdapter(
        base_url="http://127.0.0.1:3000",
        token_path="tokens.json",
        registration_path="registration.json",
        **kwargs,
    )


class TwentyPackagedSkillTests(unittest.TestCase):
    def test_default_adapter_loads_query_skill_outside_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            adapter = _adapter()

        self.assertEqual(
            adapter.business_tools.tool_names, frozenset({"query_companies"}))
        skills = adapter.business_tools.relevant("Which companies are available?")
        self.assertEqual([skill.skill_id for skill in skills],
                         ["twenty.crm.companies-query"])
        self.assertEqual(
            skills[0].artifact_uri,
            "package://web_operation_agent/skills/twenty/companies-query.json",
        )
        self.assertEqual(adapter.descriptor.application_id, "twenty")
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

        adapter = _adapter(allow_mutations=True)
        self.assertEqual(
            adapter.business_tools.tool_names,
            frozenset({"query_companies", "create_company"}),
        )
        relevant = adapter.business_tools.relevant("create company", limit=3)
        self.assertIn("twenty.crm.companies-create",
                      {skill.skill_id for skill in relevant})
        self.assertTrue(all(
            skill.artifact_uri.startswith(
                "package://web_operation_agent/skills/twenty/")
            for skill in relevant))

    def test_explicit_filesystem_skill_paths_remain_supported(self):
        packaged = resources.files("web_operation_agent").joinpath("skills", "twenty")
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
            )

        self.assertEqual(
            adapter.business_tools.tool_names,
            frozenset({"query_companies", "create_company"}),
        )
        relevant = adapter.business_tools.relevant("create company", limit=3)
        self.assertTrue(all(not skill.artifact_uri.startswith("package://")
                            for skill in relevant))

    def test_adapter_binds_only_matching_page_and_oauth_identity(self):
        adapter = _adapter(allow_mutations=True)
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
            principal.scopes, ("companies:read", "companies:write"))
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
        with patch("web_operation_agent.twenty.urlopen", return_value=response) as opened:
            identity = executor.resolve_delegated_identity(now_ms=1_000_000)

        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:3000/metadata")
        self.assertEqual(request.get_method(), "POST")
        body = json.loads(request.data)
        self.assertIn("currentUser", body["query"])
        self.assertIn("currentWorkspace", body["query"])
        self.assertNotIn("companies", body["query"])
        self.assertEqual(identity.actor_id, "actor-1")


if __name__ == "__main__":
    unittest.main()
