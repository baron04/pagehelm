from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.state.store import SQLiteSessionStore
from web_operation_agent.business.mutation import SQLiteMutationJournal
from web_operation_agent.operations import (
    OperationalStateError,
    create_state_backup,
    inspect_mutation_journal,
    main,
    restore_state_backup,
    verify_state_backup,
)


class OperationalBackupTests(unittest.TestCase):
    def _state(self, root: Path) -> tuple[Path, Path]:
        sessions = root / "live" / "sessions.sqlite3"
        mutations = root / "live" / "mutations.sqlite3"
        store = SQLiteSessionStore(sessions)
        owner = store.acquire("session-one", "scope-one")
        store.message(owner, "message-one", "digest-one")
        store.release(owner)
        journal = SQLiteMutationJournal(mutations)
        journal.register("a" * 64, "b" * 64, expires_at=9_999_999_999_999,
                         business_key="c" * 64)
        journal.claim("a" * 64, "b" * 64)
        return sessions, mutations

    def test_online_backup_and_isolated_restore_keep_unresolved_state_without_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions, mutations = self._state(root)
            bundle = root / "backup"
            manifest = create_state_backup({
                "session-store": sessions,
                "mutation-journal": mutations,
            }, bundle, now_ms=1234)
            self.assertEqual(manifest["created_at_ms"], 1234)
            self.assertFalse(manifest["execution_authority_included"])
            self.assertNotIn(str(root), (bundle / "manifest.json").read_text())
            self.assertEqual(verify_state_backup(bundle), manifest)

            result = restore_state_backup(
                bundle, root / "restored", writer_isolated=True)
            self.assertFalse(result["execution_authority_restored"])
            self.assertTrue(result["requires_reconciliation"])
            self.assertEqual(result["mutation_journal"]["unresolved"], 1)
            restored_store = SQLiteSessionStore(root / "restored/session-store.sqlite3")
            with restored_store.connect() as database:
                self.assertEqual(database.execute(
                    "SELECT state FROM messages WHERE mid='message-one'"
                ).fetchone()[0], "accepted")
            with sqlite3.connect(root / "restored/mutation-journal.sqlite3") as database:
                self.assertEqual(database.execute(
                    "SELECT state FROM mutation_intents"
                ).fetchone()[0], "dispatching")
            self.assertEqual(os.stat(root / "restored/session-store.sqlite3").st_mode & 0o777,
                             0o600)

    def test_restore_refuses_live_destination_and_missing_writer_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions, mutations = self._state(root)
            bundle = root / "backup"
            create_state_backup({"sessions": sessions, "mutations": mutations}, bundle)
            with self.assertRaisesRegex(OperationalStateError, "isolated"):
                restore_state_backup(bundle, root / "restored", writer_isolated=False)
            (root / "occupied").mkdir()
            with self.assertRaisesRegex(OperationalStateError, "already exists"):
                restore_state_backup(bundle, root / "occupied", writer_isolated=True)

    def test_live_mutation_inspection_is_read_only_and_counts_unresolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            _sessions, mutations = self._state(Path(temporary))
            self.assertEqual(inspect_mutation_journal(mutations), {
                "present": True, "unresolved": 1})
            with sqlite3.connect(mutations) as database:
                self.assertEqual(database.execute(
                    "SELECT state FROM mutation_intents").fetchone()[0], "dispatching")

    def test_tampering_symlinks_and_undeclared_files_fail_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions, mutations = self._state(root)
            for variant in ("digest", "extra", "symlink"):
                bundle = root / f"backup-{variant}"
                create_state_backup({"sessions": sessions, "mutations": mutations}, bundle)
                if variant == "digest":
                    with (bundle / "sessions.sqlite3").open("ab") as target:
                        target.write(b"changed")
                elif variant == "extra":
                    (bundle / "secret.env").write_text("do not accept")
                else:
                    target = bundle / "sessions.sqlite3"
                    target.unlink()
                    target.symlink_to(sessions)
                with self.assertRaises(OperationalStateError):
                    verify_state_backup(bundle)

    def test_failed_backup_leaves_no_destination_or_partial_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions, mutations = self._state(root)
            destination = root / "backup"
            with patch("web_operation_agent.operations._online_copy",
                       side_effect=OperationalStateError("injected")):
                with self.assertRaisesRegex(OperationalStateError, "injected"):
                    create_state_backup({"sessions": sessions, "mutations": mutations},
                                        destination)
            self.assertFalse(destination.exists())
            self.assertFalse(tuple(root.glob(".backup.partial-*")))

    def test_cli_reports_refusal_and_ready_without_exposing_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions, mutations = self._state(root)
            bundle = root / "backup"
            with patch("builtins.print") as output:
                self.assertEqual(main(["backup", "--sessions", str(sessions),
                                       "--mutations", str(mutations),
                                       "--destination", str(bundle)]), 0)
            message = json.loads(output.call_args.args[0])
            self.assertEqual(message["status"], "READY")
            self.assertNotIn(str(root), output.call_args.args[0])
            with patch("builtins.print") as output:
                self.assertEqual(main(["restore", str(bundle), "--destination",
                                       str(root / "refused")]), 2)
            self.assertEqual(json.loads(output.call_args.args[0])["status"], "REFUSED")


if __name__ == "__main__":
    unittest.main()
