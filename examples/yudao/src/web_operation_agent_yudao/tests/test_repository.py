"""Raw code lookup policy and native production wiring, with synthetic source only."""
from pathlib import Path
import os
import tempfile
import unittest

from web_operation_agent_yudao.repository import repository_source


class RepositoryTests(unittest.TestCase):
    def test_cli_relative_source_root_is_resolved_before_allowlist_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "ui/docs/owner.md"
            path.parent.mkdir(parents=True)
            path.write_text("维护团队：合成团队\n")
            source = repository_source(Path(os.path.relpath(root)), "synthetic-1")
            self.assertTrue(source.lookup.search("维护团队"))

    def test_allowlist_covers_code_and_documentation_without_a_compiled_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            permitted = {
                "ui/src/views/mes/index.vue": "<!-- MES 前端维护团队：合成团队 -->\n",
                "ui/src/views/crm/customer/detail/index.vue": '<el-tab-pane label="回款" />\n',
                "server/yudao-module-demo/src/main/java/example/DemoService.java":
                    'throw new IllegalStateException("synthetic limit");\n',
                "ui/.github/CODEOWNERS": "/src/views/mes/ @synthetic-team\n",
            }
            denied = ["ui/.env.local", "ui/src/data/customer.ts", "ui/src/uploads/private.ts",
                      "ui/src/tests/expected.ts", "ui/src/credentials/live.ts",
                      "server/yudao-module-demo/target/src/main/java/Generated.java"]
            for path, text in {**permitted, **dict.fromkeys(denied, "PRIVATE_MARKER")}.items():
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(text)
            source = repository_source(root, "synthetic-1")
            self.assertIsNone(source.source_map)
            self.assertFalse(source.source_contract)
            self.assertEqual(set(permitted), set(source.lookup._tree.files()))
            self.assertEqual((), source.lookup.search("PRIVATE_MARKER"))
            hits = source.lookup.search("MES 前端维护团队")
            self.assertTrue(any("合成团队" in h.evidence.excerpt for h in hits))
            self.assertTrue(all(source.lookup.verify(h.evidence) for h in hits))
            self.assertTrue(source.history_unavailable_reason)

    def test_source_is_optional_but_an_explicit_bad_configuration_fails(self):
        self.assertFalse(repository_source(None, "").runtime_source)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                repository_source(Path(tmp), "")
            with self.assertRaises(ValueError):
                repository_source(Path(tmp) / "missing", "synthetic-1")


if __name__ == "__main__":
    unittest.main()
