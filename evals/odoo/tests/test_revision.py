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

    def test_files_are_read_from_the_commit_not_the_working_tree(self):
        (self.root / "apparatus.py").write_text("VERSION = 99\n")
        self.assertEqual(revision.file_at(self.root, self.first, "apparatus.py"),
                         b"VERSION = 1\n")
        with self.assertRaises(revision.RevisionError):
            revision.file_at(self.root, self.first, "missing.py")
        self.assertFalse(revision.commit_exists(self.root, "0" * 40))
        with self.assertRaises(revision.RevisionError):
            revision.file_at(self.root, "0" * 40, "apparatus.py")

    def test_commits_are_full_lowercase_shas(self):
        for value in ("eeea4a0", "A" * 40, None, 7):
            with self.subTest(value=value), self.assertRaises(revision.RevisionError):
                revision.require_commit(value)


if __name__ == "__main__":
    unittest.main()
