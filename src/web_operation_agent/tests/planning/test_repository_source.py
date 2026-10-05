from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.planning.repository_source import (
    CombinedSourceLookup, RepositorySourceLookup,
)
from web_operation_agent.planning.source_facts import SourceHit
from web_operation_agent.planning.source_history import GitSourceHistory, HistoryUnavailable
from web_operation_agent.planning.sourcetree import SourceTree


PYTHON = "def authorize_invoice(user):\n    raise PermissionError('invoice approval denied')\n"
FILES = {
    "src/access.py": PYTHON,
    "src/panel.js": "export function openAuditPanel() { return 'audit drawer'; }\n",
    "src/menu.xml": "<menuitem name=\"Reconciliation Queue\" action=\"review_action\"/>\n",
    "src/policy.yaml": "retention:\n  archive_after_days: 45\n",
    "src/report.sql": "select order_id from cancelled_orders where fraud_hold = true;\n",
    "src/guide.md": "# Escalation\nThe support captain owns emergency escalation.\n",
    "src/service.toml": "[limits]\nmaximum_batch = 125\n",
    "tests/leak.py": "secret_test_only_marker = True\n",
    "data/demo.xml": "<record name=\"secret_demo_marker\"/>\n",
}


def _tree(root: Path, *, release: str = "build:one") -> SourceTree:
    return SourceTree({
        "repository_id": "example/application", "release": release,
        "allow_globs": ["src/**", "CODEOWNERS"],
        "deny_globs": ["**/tests/**", "tests/**", "data/**", "**/.env*"],
        "max_bytes_per_read": 100_000,
    }, root)


def _write(root: Path, files: dict[str, str] = FILES) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


class LanguageNeutralRepositoryLookupTests(unittest.TestCase):
    def test_explicit_filename_outranks_incidental_directory_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, {"src/constraints.md": "End date must follow signing date.\n",
                          "src/noise/constraints_date.py": "# signing date constraint " * 40})
            tree = _tree(root)
            hit = RepositorySourceLookup(tree).search("constraints.md signing date", limit=1)[0]
            self.assertEqual(hit.evidence.path, "src/constraints.md")
            self.assertTrue(tree.verify(hit.evidence))

    def test_lexical_symbol_hop_finds_usage_without_a_framework_compiler(self):
        for suffix, implementation in (
            ("py", "def delete_archive(row):\n    if row.locked:\n        raise RuntimeError(ARCHIVE_DELETE_BLOCKED)\n"),
            ("java", "void deleteArchive(Row row) {\n  if (row.locked) throw error(ARCHIVE_DELETE_BLOCKED);\n}\n")):
            with self.subTest(language=suffix), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write(root, {"src/messages.py": "ARCHIVE_DELETE_BLOCKED = 'archive deletion locked'\n",
                    f"src/service.{suffix}": implementation,
                    "src/doc.md": "archive deletion locked " * 30,
                    "src/help.md": "archive deletion locked " * 20})
                tree = _tree(root)
                hits = RepositorySourceLookup(tree).search("archive deletion locked", limit=2)
                self.assertIn(f"src/service.{suffix}", [hit.evidence.path for hit in hits])
                self.assertTrue(all(tree.verify(hit.evidence) for hit in hits))

    def test_exact_identifier_finds_body_usage_not_only_filename_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, {"src/messages.py": "ARCHIVE_DELETE_BLOCKED = 'delete blocked'\n",
                "src/rules.py": "if locked: raise RuntimeError(ARCHIVE_DELETE_BLOCKED)\n",
                "src/doc.md": "archive delete blocked " * 20})
            hits = RepositorySourceLookup(_tree(root)).search("ARCHIVE_DELETE_BLOCKED", limit=2)
            self.assertEqual({"src/messages.py", "src/rules.py"}, {hit.evidence.path for hit in hits})

    def test_symbol_hop_admits_helpers_without_original_query_words(self):
        for suffix, implementation in (
            ("py", "def validateCeiling(value, bound):\n    return value <= bound\n"),
            ("java", "boolean validateCeiling(int value, int bound) { return value <= bound; }\n")):
            with self.subTest(language=suffix), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write(root, {"src/caller.md": "采购金额超限: validateCeiling(value, bound)\n",
                              f"src/rules.{suffix}": implementation,
                              "src/guide.md": "采购金额超限 " * 20,
                              "src/other.md": "采购金额超限 " * 10,
                              "tests/hidden.py": implementation})
                tree = _tree(root)
                hits = RepositorySourceLookup(tree).search("采购金额超限", limit=4)
                self.assertIn(f"src/rules.{suffix}", [h.evidence.path for h in hits])
                self.assertTrue(all(tree.verify(h.evidence) for h in hits))
                self.assertFalse(any(h.evidence.path.startswith("tests/") for h in hits))
                explicit = RepositorySourceLookup(tree).search("src/caller.md 采购金额超限", limit=1)
                self.assertEqual(explicit[0].evidence.path, "src/caller.md")

    def test_snapshot_search_survives_root_removal_but_live_verification_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root)
            lookup = RepositorySourceLookup(_tree(root))
            hit = lookup.search("invoice approval denied", limit=1)[0]
        self.assertEqual(hit, lookup.search("invoice approval denied", limit=1)[0])
        self.assertFalse(lookup.verify(hit.evidence))

    def test_explicit_relative_path_refines_a_search_without_granting_a_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, {
                "src/order/page.js": "// order approve status permission\n",
                "src/other/page.js": "// approve approve status status permission permission\n"})
            tree = _tree(root)
            hit = RepositorySourceLookup(tree).search(
                "order/page.js approve status permission", limit=1)[0]
            self.assertEqual("src/order/page.js", hit.evidence.path)
            self.assertTrue(tree.verify(hit.evidence))

    def test_matched_error_keeps_the_neighbouring_guard_in_verified_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = "\n".join(["# filler"] * 9 + [
                "if approved:", "    raise PermissionError('archived row deletion denied')"]
                + ["# filler"] * 9) + "\n"
            _write(root, {"src/rules.py": body})
            tree = _tree(root)
            lookup = RepositorySourceLookup(tree, line_limit=8, overlap=0)
            hit = lookup.search("archived row deletion denied", limit=1)[0]
            self.assertIn("if approved:", hit.text)
            self.assertTrue(tree.verify(hit.evidence))
            (root / "src/rules.py").write_text(body.replace("approved", "rejected"))
            self.assertFalse(lookup.verify(hit.evidence))
            self.assertEqual(hit, lookup.search("archived row deletion denied", limit=1)[0])

    def test_component_and_server_source_need_no_framework_compiler(self):
        files = {
            "src/CustomerDetails.vue": '<el-tab-pane label="回款" />\n',
            "src/ProjectService.java": 'throw new IllegalStateException("项目已归档");\n',
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, files)
            tree = _tree(root)
            lookup = RepositorySourceLookup(tree)
            for query, path in (("客户 回款", "src/CustomerDetails.vue"),
                                ("项目已归档", "src/ProjectService.java")):
                with self.subTest(path=path):
                    hit = lookup.search(query, limit=1)[0]
                    self.assertEqual(path, hit.evidence.path)
                    self.assertTrue(tree.verify(hit.evidence))

    def test_seven_source_formats_are_ranked_and_cited(self):
        queries = {
            "invoice approval denied": "src/access.py",
            "open audit panel drawer": "src/panel.js",
            "reconciliation queue review action": "src/menu.xml",
            "archive retention 45 days": "src/policy.yaml",
            "cancelled orders fraud hold": "src/report.sql",
            "support captain emergency escalation": "src/guide.md",
            "maximum batch 125": "src/service.toml",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root)
            tree = _tree(root)
            lookup = RepositorySourceLookup(tree, line_limit=8, overlap=2)
            self.assertEqual(7, lookup.stats["files"])
            for query, path in queries.items():
                with self.subTest(query=query):
                    hits = lookup.search(query, limit=5)
                    self.assertTrue(hits)
                    self.assertEqual(path, hits[0].evidence.path)
                    self.assertTrue(tree.verify(hits[0].evidence))
                    self.assertEqual("implementation", hits[0].fact_type)
                    self.assertTrue(hits[0].verbatim)

    def test_scope_and_release_drift_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root)
            tree = _tree(root)
            lookup = RepositorySourceLookup(tree, line_limit=8, overlap=2)
            self.assertFalse(lookup.search("secret test only marker"))
            self.assertFalse(lookup.search("secret demo marker"))
            hit = lookup.search("invoice approval denied")[0]
            (root / "src/access.py").write_text(PYTHON.replace("denied", "blocked"),
                                                 encoding="utf-8")
            self.assertFalse(lookup.verify(hit.evidence))
            self.assertFalse(_tree(root, release="build:two").verify(hit.evidence))

    def test_adapter_path_filter_can_narrow_an_allowlisted_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root)
            lookup = RepositorySourceLookup(
                _tree(root), line_limit=8, overlap=2,
                path_filter=lambda path: path.endswith(".xml"))
            self.assertEqual(1, lookup.stats["files"])
            self.assertTrue(lookup.search("reconciliation queue"))
            self.assertFalse(lookup.search("invoice approval denied"))
            digest = lookup.surface_digest
            (root / "src/access.py").write_text("unrelated = True\n", encoding="utf-8")
            narrowed = RepositorySourceLookup(
                _tree(root), line_limit=8, overlap=2,
                path_filter=lambda path: path.endswith(".xml"))
            self.assertEqual(digest, narrowed.surface_digest)
            (root / "src/menu.xml").write_text("<menuitem name=\"Changed\"/>\n",
                                                encoding="utf-8")
            changed = RepositorySourceLookup(
                _tree(root), line_limit=8, overlap=2,
                path_filter=lambda path: path.endswith(".xml"))
            self.assertNotEqual(digest, changed.surface_digest)

    def test_non_utf8_source_is_skipped_instead_of_receiving_a_lossy_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root)
            (root / "src/binary.py").write_bytes(b"name = '\xff'\n")
            lookup = RepositorySourceLookup(_tree(root), line_limit=8, overlap=2)
            self.assertEqual(1, lookup.stats["skipped_non_utf8"])
            self.assertFalse(lookup.search("binary"))

    def test_combined_lookup_keeps_structured_and_raw_results_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root)
            raw = RepositorySourceLookup(_tree(root), line_limit=8, overlap=2)
            raw_hit = raw.search("invoice approval denied", limit=1)[0]
            compiled_ref = raw.search("open audit panel drawer", limit=1)[0].evidence

            class Fixed:
                release = "build:one"

                def search(self, query, *, limit=8, route=None):
                    del query, limit, route
                    return (SourceHit(compiled_ref, "compiled permission rule"),)

            hits = CombinedSourceLookup(Fixed(), raw).search(
                "invoice approval denied", limit=4)
            self.assertGreaterEqual(len(hits), 2)
            self.assertEqual("compiled permission rule", hits[0].text)
            self.assertTrue(hits[1].verbatim)


class GitSourceHistoryTests(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> str:
        run = subprocess.run(("git", "-C", str(root), *args), check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return run.stdout.decode().strip()

    def _repository(self, root: Path) -> tuple[str, str]:
        self._git(root, "init", "-q")
        self._git(root, "config", "user.name", "Fixture Author")
        self._git(root, "config", "user.email", "fixture@example.invalid")
        _write(root, {"src/access.py": PYTHON,
                      "CODEOWNERS": "src/*.py @security-team\n"})
        self._git(root, "add", "src/access.py", "CODEOWNERS")
        self._git(root, "commit", "-q", "-m", "declare invoice authorization")
        deployed = self._git(root, "rev-parse", "HEAD")
        (root / "src/access.py").write_text(
            PYTHON + "\ndef retry_invoice():\n    return 'manual review'\n", encoding="utf-8")
        self._git(root, "add", "src/access.py")
        self._git(root, "commit", "-q", "-m", "add manual invoice retry")
        target = self._git(root, "rev-parse", "HEAD")
        self._git(root, "checkout", "-q", "--detach", deployed)
        return deployed, target

    def test_owners_blame_commits_and_changes_are_deployment_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deployed, target = self._repository(root)
            history = GitSourceHistory(_tree(root), deployed)
            self.assertEqual(deployed, history.deployed_commit)
            owners = history.search("owners", path="src/access.py")
            self.assertIn("@security-team", owners[0].statement)
            blame = history.search("blame", path="src/access.py", start_line=1, end_line=2)
            self.assertTrue(blame)
            self.assertIn("Fixture Author", blame[0].statement)
            commits = history.search("commits", path="src/access.py")
            self.assertEqual(deployed, commits[0].commit)
            changes = history.search(
                "changes", path="src/access.py", target_commit=target)
            self.assertEqual("src/access.py", changes[0].path)
            self.assertIn("manual review", changes[0].statement)
            for hit in (*owners, *blame, *commits, *changes):
                self.assertEqual("build:one", hit.release)
                self.assertEqual(64, len(hit.digest))

    def test_dirty_allowlisted_bytes_and_missing_git_are_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deployed, _ = self._repository(root)
            (root / "src/access.py").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(HistoryUnavailable, "do not match"):
                GitSourceHistory(_tree(root), deployed)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root)
            history, reason = GitSourceHistory.optional(_tree(root), "HEAD")
            self.assertIsNone(history)
            self.assertTrue(reason)

    def test_history_path_filter_is_enforced_after_the_source_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deployed, _ = self._repository(root)
            history = GitSourceHistory(
                _tree(root), deployed, path_filter=lambda path: path == "CODEOWNERS")
            with self.assertRaisesRegex(ValueError, "outside"):
                history.search("commits", path="src/access.py")


if __name__ == "__main__":
    unittest.main()
