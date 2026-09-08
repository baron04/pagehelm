"""Twenty application adapter and business tools stay bounded and write-safe."""
from __future__ import annotations

import contextlib
import tempfile
import unittest
from importlib import resources
from pathlib import Path

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


class TwentyNormalizationTests(unittest.TestCase):
    def test_domain_normalization_matches_api_and_database_shapes(self):
        self.assertEqual(normalize_twenty_domain("HTTPS://Example.COM/"), "example.com")


if __name__ == "__main__":
    unittest.main()
