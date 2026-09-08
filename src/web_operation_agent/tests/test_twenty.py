"""Twenty task acceptance routing stays deterministic and write-safe."""
from __future__ import annotations

import contextlib
import tempfile
import unittest
from importlib import resources
from pathlib import Path

from web_operation_agent.twenty import (TwentyApplicationAdapter,
                                        TwentyBusinessToolExecutor,
                                        classify_twenty_company_task,
                                        normalize_twenty_domain)


STATE = {"projection": {"companies": [
    {"id": "company-1", "name": "Anthropic", "domain": "https://anthropic.com",
     "deleted_at": None},
    {"id": "company-2", "name": "Acme", "domain": "https://acme.example",
     "deleted_at": None},
    {"id": "company-3", "name": "Acme Labs", "domain": "https://labs.example",
     "deleted_at": None},
    {"id": "company-4", "name": "Deleted Co", "domain": "https://deleted.example",
     "deleted_at": "2026-09-08"},
]}}


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


class TwentyTaskRoutingTests(unittest.TestCase):
    def test_query_is_read_intent(self):
        self.assertEqual(
            classify_twenty_company_task("当前有哪些公司？请列出名称和域名。", STATE).kind,
            "query")

    def test_open_binds_to_current_full_name(self):
        intent = classify_twenty_company_task("打开 Acme Labs 公司详情", STATE)
        self.assertEqual(intent.kind, "open")
        self.assertEqual(intent.company["id"], "company-3")

    def test_deleted_company_cannot_be_opened(self):
        with self.assertRaisesRegex(ValueError, "当前存在"):
            classify_twenty_company_task("打开 Deleted Co 公司详情", STATE)

    def test_chinese_create_requires_and_extracts_exact_fields(self):
        intent = classify_twenty_company_task(
            "请创建公司“NovaForge Demo”，域名 novaforge-demo.example，然后打开详情。",
            STATE)
        self.assertEqual(
            (intent.kind, intent.requested_name, intent.requested_domain),
            ("create", "NovaForge Demo", "novaforge-demo.example"))

    def test_english_create_requires_and_extracts_exact_fields(self):
        intent = classify_twenty_company_task(
            "Create a company 'Example Labs' with domain example-labs.test", STATE)
        self.assertEqual(
            (intent.kind, intent.requested_name, intent.requested_domain),
            ("create", "Example Labs", "example-labs.test"))

    def test_create_without_domain_is_rejected_before_agent_dispatch(self):
        with self.assertRaisesRegex(ValueError, "公司名称和裸域名"):
            classify_twenty_company_task("创建公司 Missing Domain", STATE)

    def test_domain_normalization_matches_api_and_database_shapes(self):
        self.assertEqual(normalize_twenty_domain("HTTPS://Example.COM/"), "example.com")


if __name__ == "__main__":
    unittest.main()
