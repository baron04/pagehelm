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
        from web_operation_agent.tests.support.identity import principal
        from web_operation_agent.business.operations import reconcile
        from web_operation_agent.composition.ports import MutationReconciliation
        import time
        p = principal(issued_at=int(time.time() * 1000),
                      expires_at=int(time.time() * 1000) + 60_000)
        identity = {k: v for k, v in p.audit_projection().items()
                    if k in {'tenant_digest', 'workspace_digest', 'actor_digest'}}
        adapter = SimpleNamespace(
            descriptor=SimpleNamespace(application_id='sample'),
            ports=SimpleNamespace(identity=Mock(), reconciliation=Mock()))
        adapter.ports.reconciliation.reconcile.return_value = MutationReconciliation(
            'unknown', True, 'current state is not proof of the earlier outcome')
        with tempfile.TemporaryDirectory() as tmp:
            journal = SQLiteMutationJournal(Path(tmp) / 'journal.db')
            journal.register('a' * 64, 'b' * 64, expires_at=2000, now_ms=1000,
                             reconciliation={'application_id': 'sample', 'tool': 'create_company',
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

    def test_reconcile_forwards_post_dispatch_metadata_to_adapter_evidence(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import time
        from web_operation_agent.business.operations import online_reconcile, reconcile, unresolved_report
        from web_operation_agent.composition.ports import MutationReconciliation
        from web_operation_agent.tests.support.identity import principal

        now = int(time.time() * 1000)
        p = principal(issued_at=now, expires_at=now + 60_000)
        identity = {k: v for k, v in p.audit_projection().items()
                    if k in {"tenant_digest", "workspace_digest", "actor_digest"}}
        adapter = SimpleNamespace(
            descriptor=SimpleNamespace(application_id="sample"),
            ports=SimpleNamespace(identity=Mock(), reconciliation=Mock()),
        )
        adapter.ports.reconciliation.reconcile.return_value = MutationReconciliation(
            "unknown", True, "partial effect requires human verification")
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "journal.db"
            journal = SQLiteMutationJournal(journal_path)
            digest = "e" * 64
            journal.register(
                digest, "b" * 64, expires_at=now + 60_000, now_ms=now,
                reconciliation={"application_id": "sample", "tool": "update_record",
                                "args": {"entity": "crm/customer"}, "identity": identity})
            journal.claim(digest, "b" * 64, now_ms=now + 1)
            journal.finish(
                digest, "indeterminate", now_ms=now + 2,
                reconciliation={
                    "id": 77,
                    "unverified_effect": "unrelated_field_preservation",
                    "effects": [{"effect": "record", "verified": True, "id": 77}],
                },
            )
            reconcile(journal, digest, application=adapter, principal=p)
            _tool, _args = adapter.ports.reconciliation.reconcile.call_args.args
            evidence = adapter.ports.reconciliation.reconcile.call_args.kwargs["evidence"]
            self.assertEqual(evidence["id"], 77)
            self.assertEqual(evidence["unverified_effect"], "unrelated_field_preservation")
            self.assertEqual(evidence["effects"], [{"effect": "record", "verified": True, "id": 77}])
            self.assertIn("result_digest", evidence)

            online = online_reconcile(journal, digest, application=adapter, principal=p, now_ms=now + 3)
            self.assertEqual(online["outcome"], "unknown")
            self.assertFalse(online["settled"])
            self.assertEqual(unresolved_report(journal.get(digest), journal_path=journal_path)["record_id"], "77")

    def test_online_reconcile_handles_completed_failed_unknown_and_active_lease(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import time
        from web_operation_agent.business.operations import online_reconcile
        from web_operation_agent.composition.ports import MutationReconciliation
        from web_operation_agent.tests.support.identity import principal

        now = int(time.time() * 1000)
        p = principal(issued_at=now, expires_at=now + 60_000)
        identity = {k: v for k, v in p.audit_projection().items()
                    if k in {"tenant_digest", "workspace_digest", "actor_digest"}}

        def make_adapter(outcome: str, requires_human: bool, reason: str):
            adapter = SimpleNamespace(
                descriptor=SimpleNamespace(application_id="sample"),
                ports=SimpleNamespace(identity=Mock(), reconciliation=Mock()),
            )
            adapter.ports.reconciliation.reconcile.return_value = MutationReconciliation(
                outcome, requires_human, reason)
            return adapter

        with tempfile.TemporaryDirectory() as tmp:
            journal = SQLiteMutationJournal(Path(tmp) / "journal.db")
            for idx, (outcome, req_human, reason) in enumerate((
                ("completed", False, "record exists"),
                ("failed", False, "record absent"),
                ("unknown", True, "ambiguous"),
            )):
                digest = f"{idx}" * 64
                journal.register(
                    digest, "b" * 64, expires_at=5_000, now_ms=1_000,
                    reconciliation={"application_id": "sample", "tool": "create_company",
                                    "args": {"name": f"Co-{idx}"}, "identity": identity})
                journal.claim(digest, "b" * 64, now_ms=1_001, dispatch_lease_expires_at=2_000)
                journal.finish(digest, "indeterminate", now_ms=1_002)
                result = online_reconcile(
                    journal, digest, application=make_adapter(outcome, req_human, reason),
                    principal=p, now_ms=1_003)
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["dispatches"], 0)
                if outcome in {"completed", "failed"} and not req_human:
                    self.assertTrue(result["settled"])
                    self.assertEqual(journal.get(digest).state, outcome)
                else:
                    self.assertFalse(result["settled"])
                    self.assertEqual(journal.get(digest).state, "indeterminate")

            # Actively dispatching with unexpired lease is never overridden.
            live = "9" * 64
            journal.register(
                live, "b" * 64, expires_at=5_000, now_ms=1_000,
                reconciliation={"application_id": "sample", "tool": "create_company",
                                "args": {"name": "Live"}, "identity": identity})
            journal.claim(live, "b" * 64, now_ms=1_001, dispatch_lease_expires_at=5_000)
            active_res = online_reconcile(
                journal, live, application=make_adapter("completed", False, "exists"),
                principal=p, now_ms=2_000)
            self.assertFalse(active_res["settled"])
            self.assertEqual(journal.get(live).state, "dispatching")

            # Once dispatch lease expires, online CAS succeeds.
            expired_res = online_reconcile(
                journal, live, application=make_adapter("completed", False, "exists"),
                principal=p, now_ms=5_000)
            self.assertTrue(expired_res["settled"])
            self.assertEqual(journal.get(live).state, "completed")

    def test_lease_expired_cas_allows_cli_and_concurrent_race_wins_once(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.db"
            journal = SQLiteMutationJournal(path)
            journal.register("a" * 64, "b" * 64, expires_at=10_000, now_ms=1_000)
            journal.claim("a" * 64, "b" * 64, now_ms=1_001, dispatch_lease_expires_at=2_000)
            # Before lease expiry, writer_stopped=False is refused.
            with self.assertRaises(MutationJournalError):
                journal.resolve(
                    "a" * 64, expected_updated_at=1_001, outcome="completed",
                    operator_digest="d" * 64, evidence_digest="e" * 64,
                    writer_stopped=False, now_ms=1_500, dry_run=False)

            barrier = threading.Barrier(12)
            outcomes = []
            lock = threading.Lock()

            def race():
                barrier.wait()
                try:
                    journal.resolve(
                        "a" * 64, expected_updated_at=1_001, outcome="completed",
                        operator_digest="d" * 64, evidence_digest="e" * 64,
                        writer_stopped=False, now_ms=2_500, dry_run=False)
                    tag = "resolved"
                except MutationJournalError:
                    tag = "refused"
                with lock:
                    outcomes.append(tag)

            workers = [threading.Thread(target=race) for _ in range(12)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(5)
            self.assertEqual(outcomes.count("resolved"), 1)
            self.assertEqual(outcomes.count("refused"), 11)
            self.assertEqual(journal.get("a" * 64).state, "completed")

            # CLI --lease-expired --apply works on an expired lease row.
            journal.register("f" * 64, "b" * 64, expires_at=10_000, now_ms=1_000)
            journal.claim("f" * 64, "b" * 64, now_ms=1_001, dispatch_lease_expires_at=2_000)
            evidence = Path(tmp) / "evidence.txt"
            evidence.write_text("Readback verified")
            main(["--journal", str(path), "resolve", "f" * 64,
                  "--expected-updated-at", "1001", "--outcome", "failed",
                  "--operator", "ops", "--evidence", str(evidence),
                  "--lease-expired", "--apply"])
            self.assertEqual(journal.get("f" * 64).state, "failed")
