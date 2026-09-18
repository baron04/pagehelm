import subprocess
import tempfile
import unittest
from pathlib import Path

from evals.odoo import revision


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=p74", "-c",
                           "user.email=p74@example.test", *args],
                          check=True, capture_output=True).stdout.decode().strip()


class RevisionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        git(self.root, "init", "-q")
        (self.root / ".gitignore").write_text("private/\n")
        (self.root / "apparatus.py").write_text("VERSION = 1\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "first")
        self.first = git(self.root, "rev-parse", "HEAD")

    def test_clean_head_ignores_ignored_paths_but_not_untracked_files(self):
        (self.root / "private").mkdir()
        (self.root / "private" / "secret.txt").write_text("not tracked, ignored\n")
        self.assertEqual(revision.require_clean_head(self.root, commit=self.first)["commit"],
                         self.first)
        (self.root / "notes.txt").write_text("untracked\n")
        with self.assertRaises(revision.RevisionError):
            revision.require_clean_head(self.root)

    def test_modified_or_moved_head_is_refused(self):
        (self.root / "apparatus.py").write_text("VERSION = 2\n")
        self.assertFalse(revision.repository_state(self.root)["clean"])
        git(self.root, "commit", "-q", "-am", "second")
        with self.assertRaises(revision.RevisionError):
            revision.require_clean_head(self.root, commit=self.first)

    def test_commits_are_full_lowercase_shas(self):
        for value in ("eeea4a0", "A" * 40, None, 7):
            with self.subTest(value=value), self.assertRaises(revision.RevisionError):
                revision.require_commit(value)

    def test_ancestry_and_changed_paths_are_compared_by_commit(self):
        (self.root / "apparatus.py").write_text("VERSION = 2\n")
        (self.root / "evals").mkdir()
        (self.root / "evals" / "driver.py").write_text("DRIVER = 1\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "second")
        second = git(self.root, "rev-parse", "HEAD")
        self.assertTrue(revision.is_ancestor(self.root, self.first, second))
        self.assertTrue(revision.is_ancestor(self.root, second, second))
        self.assertFalse(revision.is_ancestor(self.root, second, self.first))
        with self.assertRaises(revision.RevisionError):
            revision.is_ancestor(self.root, "0" * 40, second)
        self.assertEqual(revision.changed_between(self.root, self.first, second, "evals/"),
                         ["evals/driver.py"])
        self.assertEqual(revision.changed_between(self.root, second, second, "evals/"), [])
        with self.assertRaises(revision.RevisionError):
            revision.changed_between(self.root, "0" * 40, second, "evals/")


if __name__ == "__main__":
    unittest.main()
