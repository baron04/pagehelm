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

    def test_a_record_is_committed_once_and_as_a_regular_file(self):
        (self.root / "record.json").write_text("{}\n")
        git(self.root, "add", "record.json")
        git(self.root, "commit", "-q", "-m", "record")
        self.assertEqual(revision.committed_once(self.root, "record.json"),
                         git(self.root, "rev-parse", "HEAD"))
        # The review of 582e11c: a link's target changes while git sees no change to the link.
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "record.json"
        target.write_text("{}\n")
        (self.root / "link.json").symlink_to(target)
        git(self.root, "add", "link.json")
        git(self.root, "commit", "-q", "-m", "link")
        with self.assertRaisesRegex(revision.RevisionError, "regular file"):
            revision.committed_once(self.root, "link.json")

    def test_the_main_line_leaves_out_what_merges_brought_in(self):
        base = git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        git(self.root, "checkout", "-q", "-b", "side")
        (self.root / "records").mkdir()
        (self.root / "records" / "side.json").write_text("{}\n")
        git(self.root, "add", "records")
        git(self.root, "commit", "-q", "-m", "side")
        side = git(self.root, "rev-parse", "HEAD")
        git(self.root, "checkout", "-q", base)
        (self.root / "apparatus.py").write_text("VERSION = 2\n")
        git(self.root, "commit", "-q", "-am", "main")
        git(self.root, "merge", "-q", "--no-edit", "side")
        main_line = revision.first_parent_commits(self.root)
        self.assertIn(self.first, main_line)
        self.assertNotIn(side, main_line)
        # A directory's history is read whatever letters its path is spelled in.
        blob = git(self.root, "hash-object", "-w", str(self.root / "records" / "side.json"))
        git(self.root, "update-index", "--add", "--cacheinfo", f"100644,{blob},Records/x.json")
        git(self.root, "commit", "-q", "-m", "other letters")
        self.assertEqual(sorted(path for _, _, path in revision.path_history(self.root, "records")),
                         ["Records/x.json", "records/side.json"])

    def test_commits_are_full_lowercase_shas(self):
        for value in ("eeea4a0", "A" * 40, None, 7):
            with self.subTest(value=value), self.assertRaises(revision.RevisionError):
                revision.require_commit(value)

    def test_ancestry_and_file_content_are_compared_by_commit(self):
        (self.root / "apparatus.py").write_text("VERSION = 2\n")
        git(self.root, "commit", "-q", "-am", "second")
        second = git(self.root, "rev-parse", "HEAD")
        self.assertTrue(revision.is_ancestor(self.root, self.first, second))
        self.assertTrue(revision.is_ancestor(self.root, second, second))
        self.assertFalse(revision.is_ancestor(self.root, second, self.first))
        with self.assertRaises(revision.RevisionError):
            revision.is_ancestor(self.root, "0" * 40, second)
        paths = ["apparatus.py", "missing.py"]
        current = revision.content_blob_ids(self.root, paths)
        self.assertEqual(revision.blob_ids_at(self.root, second, paths), current)
        self.assertIsNone(current["missing.py"])
        self.assertNotEqual(revision.blob_ids_at(self.root, self.first, paths)["apparatus.py"],
                            current["apparatus.py"])


if __name__ == "__main__":
    unittest.main()
