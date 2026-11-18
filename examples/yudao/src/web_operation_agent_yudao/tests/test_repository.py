"""Raw code lookup policy and native production wiring, with synthetic source only."""
from pathlib import Path
import os
import tempfile
import unittest
import subprocess

from web_operation_agent.planning.source_history import HistoryUnavailable

from web_operation_agent_yudao.rules.repository import repository_source


class RepositoryTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(["git", "-C", str(root),
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
            "-c", "user.name=Synthetic author", "-c", "user.email=synthetic@example.invalid",
            *args], stderr=subprocess.PIPE, text=True).strip()

    def repository(self, root, files):
        root.mkdir(parents=True)
        self.git(root, "init", "-q")
        for name, contents in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        self.git(root, "add", ".")
        self.git(root, "commit", "-qm", "Initial synthetic implementation")
        path = root / next(iter(files))
        path.write_text(path.read_text() + "\n// reviewed validation change\n")
        self.git(root, "add", ".")
        self.git(root, "commit", "-qm", "Add synthetic refund validation")
        return self.git(root, "rev-parse", "HEAD")

    def test_two_real_git_repositories_bind_their_own_commits_and_allowlists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ui = self.repository(root / "ui", {
                "src/refund.ts": "export const refundLimit = 100;\n",
                "CODEOWNERS": "/src/ @synthetic-ui\n", ".env": "PRIVATE_GIT_MARKER\n"})
            server_path = "yudao-module-demo/src/main/java/example/Refund.java"
            server = self.repository(root / "server", {server_path: "class Refund {}\n"})
            source = repository_source(root, "synthetic-git", commits=(("ui", ui), ("server", server)))
            history = source.history_lookup()
            self.assertIsNone(history.deployed_commit)
            owners = history.search("owners", path="ui/src/refund.ts")
            self.assertIn("synthetic-ui", owners[0].statement)
            self.assertEqual(owners[0].deployed_commit, ui)
            blame = history.search("blame", path="server/" + server_path, start_line=1, end_line=1)
            self.assertEqual(blame[0].deployed_commit, server)
            hits = (*history.search("changes", path="ui/src/refund.ts", limit=2,
                        target_commit=self.git(root / "ui", "rev-parse", ui + "^")),
                    *history.search("changes", path="server/" + server_path, limit=2,
                        target_commit=self.git(root / "server", "rev-parse", server + "^")))
            self.assertEqual({h.deployed_commit for h in hits}, {ui, server})
            self.assertTrue(all(h.path.startswith(("ui/", "server/")) for h in hits))
            self.assertFalse(source.history_unavailable_reason)
            for path in ("ui/.env", "ui/../server/.env", "outside/src/refund.ts"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    history.search("commits", path=path)
            with self.assertRaises(ValueError):
                history.search("changes", target_commit=ui)
            with self.assertRaises(HistoryUnavailable):
                repository_source(root, "synthetic-git", commits=(("ui", server),))
            (root / "ui/src/refund.ts").write_text("uncommitted drift")
            with self.assertRaises(HistoryUnavailable):
                repository_source(root, "synthetic-git", commits=(("ui", ui),))

    def test_single_repository_can_bind_a_snapshot_with_both_source_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repository"
            commit = self.repository(root, {"ui/src/refund.ts": "export const maximum = 20;\n"})
            source = repository_source(root, "synthetic-git", commits=(("root", commit),))
            hits = source.history_lookup().search("commits", path="ui/src/refund.ts")
            self.assertEqual(hits[0].commit, commit)
            self.assertEqual(hits[0].path, "ui/src/refund.ts")
            self.assertEqual(source.history_lookup().deployed_commit, commit)

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

    def test_runtime_off_keeps_the_release_validated_and_offers_no_user_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "ui/src/refund.ts"
            path.parent.mkdir(parents=True)
            path.write_text("export const refundLimit = 100;\n")
            source = repository_source(root, "synthetic-1", runtime=False)
            self.assertFalse(source.runtime_source)
            self.assertIsNone(source.lookup)
            self.assertIsNone(source.history_lookup())
            # The deployment's release binding is still checked at startup, and the
            # runtime switch never stands in for the build-time scanners' own root.
            with self.assertRaises(ValueError):
                repository_source(root / "missing", "synthetic-1", runtime=False)
            with self.assertRaises(ValueError):
                repository_source(root, "", runtime=False)


if __name__ == "__main__":
    unittest.main()
