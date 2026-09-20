from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.business.mutation import SQLiteMutationJournal, MutationJournalError
from web_operation_agent.business.operations import main


class MutationOpsTests(unittest.TestCase):
    def test_dry_run_and_cas_resolution_leave_audit_and_never_rearm(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = SQLiteMutationJournal(Path(tmp) / "journal.db")
            journal.register("a" * 64, "b" * 64, expires_at=2000, now_ms=1000,
                             business_key="c" * 64, reconciliation={"tool": "create"})
            journal.claim("a" * 64, "b" * 64, now_ms=1001)
            args = dict(expected_updated_at=1001, outcome="completed",
                        operator_digest="d" * 64, evidence_digest="e" * 64,
                        writer_stopped=True)
            self.assertTrue(journal.resolve("a" * 64, **args)["dry_run"])
            self.assertEqual(journal.get("a" * 64).state, "dispatching")
            with self.assertRaises(MutationJournalError):
                journal.resolve("a" * 64, **{**args, "expected_updated_at": 999}, dry_run=False)
            with self.assertRaises(MutationJournalError):
                journal.resolve("a" * 64, **{**args, "writer_stopped": False}, dry_run=False)
            journal.resolve("a" * 64, **args, dry_run=False)
            self.assertEqual(journal.get("a" * 64).state, "completed")
            with journal._connect() as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM mutation_resolutions").fetchone()[0], 1)
            with self.assertRaises(MutationJournalError):
                journal.claim("a" * 64, "b" * 64, now_ms=1002)
            with self.assertRaises(MutationJournalError):
                journal.resolve("a" * 64, **args, dry_run=False)
            self.assertEqual(json.loads(journal.get("a" * 64).reconciliation_json),
                             {"tool": "create"})

    def test_operator_command_is_dry_run_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.db"
            journal = SQLiteMutationJournal(path)
            journal.register("a" * 64, "b" * 64, expires_at=2000, now_ms=1000)
            journal.claim("a" * 64, "b" * 64, now_ms=1001)
            evidence = Path(tmp) / "evidence.txt"
            evidence.write_text("Synthetic application acknowledgement and stopped writer")
            main(["--journal", str(path), "resolve", "a" * 64,
                  "--expected-updated-at", "1001", "--outcome", "completed",
                  "--operator", "test-operator", "--evidence", str(evidence),
                  "--writer-stopped"])
            self.assertEqual(journal.get("a" * 64).state, "dispatching")

    def test_read_only_reconcile_checks_original_identity_before_adapter(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from web_operation_agent.tests.test_identity import principal
        from web_operation_agent.business.operations import reconcile
        from web_operation_agent.composition.ports import MutationReconciliation
        import time
        p = principal(issued_at=int(time.time() * 1000),
                      expires_at=int(time.time() * 1000) + 60_000)
        identity = {k: v for k, v in p.audit_projection().items()
                    if k in {'tenant_digest', 'workspace_digest', 'actor_digest'}}
        adapter = SimpleNamespace(
            descriptor=SimpleNamespace(application_id='twenty'),
            ports=SimpleNamespace(identity=Mock(), reconciliation=Mock()))
        adapter.ports.reconciliation.reconcile.return_value = MutationReconciliation(
            'unknown', True, 'current state is not proof of the earlier outcome')
        with tempfile.TemporaryDirectory() as tmp:
            journal = SQLiteMutationJournal(Path(tmp) / 'journal.db')
            journal.register('a' * 64, 'b' * 64, expires_at=2000, now_ms=1000,
                             reconciliation={'application_id': 'twenty', 'tool': 'create_company',
                                             'args': {'name': 'Synthetic'}, 'identity': identity})
            journal.claim('a' * 64, 'b' * 64, now_ms=1001)
            before = journal.get('a' * 64)
            result = reconcile(journal, 'a' * 64, application=adapter, principal=p)
            self.assertEqual(result['outcome'], 'unknown')
            self.assertEqual(journal.get('a' * 64), before)
            from dataclasses import replace
            with self.assertRaises(MutationJournalError):
                reconcile(journal, 'a' * 64, application=adapter,
                          principal=replace(p, actor_id='other-actor'))
            self.assertEqual(adapter.ports.reconciliation.reconcile.call_count, 1)
