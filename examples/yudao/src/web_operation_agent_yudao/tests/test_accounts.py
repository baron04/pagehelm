"""One private name-to-account map: selected by name, never guessed at, written atomically."""
import argparse
import json
from pathlib import Path
import stat
import tempfile
import unittest

from web_operation_agent_yudao.accounts import (ACCOUNTS_FILE, add_account_arguments,
                                                load_account, read_accounts, save_account)
from web_operation_agent_yudao.client import YudaoAccount

ROOT = Path(__file__).resolve().parents[5]
ADMIN = {"username": "admin", "password": "synthetic-admin", "tenant": "1"}
EXPLORER = {"username": "explorer", "password": "synthetic-explorer", "tenant": "2"}


class AccountsFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "accounts.json"
        self.path.write_text(json.dumps({"admin": ADMIN, "explorer": EXPLORER}))

    def test_the_named_entry_is_selected(self):
        self.assertEqual(load_account("explorer", self.path), EXPLORER)
        self.assertEqual(YudaoAccount.load("explorer", self.path),
                         YudaoAccount("explorer", "synthetic-explorer", "2"))

    def test_a_single_account_object_is_refused_not_guessed_at(self):
        self.path.write_text(json.dumps(ADMIN))
        for name in ("admin", "username"):
            with self.assertRaises(ValueError):
                load_account(name, self.path)

    def test_an_unknown_name_lists_names_without_passwords(self):
        with self.assertRaises(LookupError) as raised:
            load_account("evaluator", self.path)
        self.assertIn("['admin', 'explorer']", str(raised.exception))
        self.assertNotIn("synthetic-", str(raised.exception))

    def test_save_keeps_other_entries_private_and_never_overwrites(self):
        entry = {"username": "new", "password": "synthetic-new", "tenant": "1"}
        save_account("new", entry, self.path)
        self.assertEqual(read_accounts(self.path), {"admin": ADMIN, "explorer": EXPLORER, "new": entry})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["accounts.json"])
        before = self.path.read_bytes()
        for name, value in (("admin", entry), ("incomplete", {"username": "x"})):
            with self.assertRaises(ValueError):
                save_account(name, value, self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_tools_default_to_the_checkout_private_file_git_ignores(self):
        parser = argparse.ArgumentParser()
        add_account_arguments(parser)
        self.assertEqual(parser.parse_args(["--account", "admin"]).accounts, ACCOUNTS_FILE)
        self.assertEqual(ACCOUNTS_FILE, ROOT / ".private/yudao-accounts.json")
        self.assertIn(".private/", (ROOT / ".gitignore").read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
