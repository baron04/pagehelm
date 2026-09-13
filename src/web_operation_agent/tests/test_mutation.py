from __future__ import annotations

import os
import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path

from web_operation_agent.mutation import (MemoryMutationJournal,
                                          MutationClaimError,
                                          MutationJournalDurability,
                                          MutationJournalError,
                                          SQLiteMutationJournal)


PROPOSAL = "a" * 64
CONTEXT = "b" * 64
RESULT = "c" * 64


def _competing_claim(path, proposal, ready, start, output):
    journal = SQLiteMutationJournal(path)
    ready.put(True)
    start.wait(10)
    try:
        journal.claim(proposal, CONTEXT, now_ms=1_001)
        output.put("claimed")
    except MutationJournalError:
        output.put("refused")


class MutationJournalContract:
    def make_journal(self):
        raise NotImplementedError

    def test_state_machine_is_consume_before_dispatch_and_single_use(self):
        journal = self.make_journal()
        journal.register(PROPOSAL, CONTEXT, expires_at=2_000, now_ms=1_000)
        self.assertEqual(journal.get(PROPOSAL).state, "prepared")
        journal.claim(PROPOSAL, CONTEXT, now_ms=1_001)
        self.assertEqual(journal.get(PROPOSAL).state, "dispatching")
        with self.assertRaisesRegex(MutationJournalError, "already dispatching"):
            journal.claim(PROPOSAL, CONTEXT, now_ms=1_002)
        journal.finish(PROPOSAL, "completed", result_digest=RESULT, now_ms=1_003)
        intent = journal.get(PROPOSAL)
        self.assertEqual(intent.state, "completed")
        self.assertEqual(intent.result_digest, RESULT)
        with self.assertRaisesRegex(MutationJournalError, "already completed"):
            journal.claim(PROPOSAL, CONTEXT, now_ms=1_004)

    def test_distinct_proposals_block_until_definite_finish(self):
        journal = self.make_journal()
        for proposal in (PROPOSAL, RESULT):
            journal.register(proposal, CONTEXT, expires_at=2_000, now_ms=1_000,
                             business_key="d" * 64)
        journal.claim(PROPOSAL, CONTEXT, now_ms=1_001)
        with self.assertRaisesRegex(MutationJournalError, "business key"):
            journal.claim(RESULT, CONTEXT, now_ms=1_002)
        journal.finish(PROPOSAL, "completed", now_ms=1_003)
        journal.claim(RESULT, CONTEXT, now_ms=1_004)
        self.assertEqual(journal.get(RESULT).state, "dispatching")

    def test_indeterminate_key_never_expires_into_permission(self):
        journal = self.make_journal()
        journal.register(PROPOSAL, CONTEXT, expires_at=2_000, now_ms=1_000,
                         business_key="d" * 64)
        journal.claim(PROPOSAL, CONTEXT, now_ms=1_001)
        journal.finish(PROPOSAL, "indeterminate", now_ms=1_002)
        journal.register(RESULT, CONTEXT, expires_at=4_000, now_ms=3_000,
                         business_key="d" * 64)
        with self.assertRaisesRegex(MutationJournalError, "business key"):
            journal.claim(RESULT, CONTEXT, now_ms=3_001)
        journal.register("e" * 64, CONTEXT, expires_at=4_000, now_ms=3_000,
                         business_key="f" * 64)
        journal.claim("e" * 64, CONTEXT, now_ms=3_001)

    def test_legacy_unresolved_intent_blocks_new_keyed_dispatch(self):
        journal = self.make_journal()
        journal.register(PROPOSAL, CONTEXT, expires_at=2_000, now_ms=1_000)
        journal.claim(PROPOSAL, CONTEXT, now_ms=1_001)
        journal.register(RESULT, CONTEXT, expires_at=2_000, now_ms=1_000,
                         business_key="d" * 64)
        with self.assertRaisesRegex(MutationJournalError, "business key"):
            journal.claim(RESULT, CONTEXT, now_ms=1_002)

    def test_context_mismatch_and_expiry_fail_before_claim(self):
        journal = self.make_journal()
        journal.register(PROPOSAL, CONTEXT, expires_at=2_000, now_ms=1_000)
        with self.assertRaisesRegex(MutationJournalError, "another context"):
            journal.claim(PROPOSAL, "d" * 64, now_ms=1_001)
        with self.assertRaisesRegex(MutationJournalError, "expired"):
            journal.claim(PROPOSAL, CONTEXT, now_ms=2_000)
        self.assertEqual(journal.get(PROPOSAL).state, "prepared")

    def test_claim_refusals_carry_stable_machine_reasons(self):
        journal = self.make_journal()
        journal.register(PROPOSAL, CONTEXT, expires_at=2_000, now_ms=1_000,
                         business_key="d" * 64)
        with self.assertRaises(MutationClaimError) as mismatch:
            journal.claim(PROPOSAL, "e" * 64, now_ms=1_001)
        self.assertEqual(mismatch.exception.reason, "context_mismatch")
        journal.claim(PROPOSAL, CONTEXT, now_ms=1_001)
        with self.assertRaises(MutationClaimError) as consumed:
            journal.claim(PROPOSAL, CONTEXT, now_ms=1_002)
        self.assertEqual(consumed.exception.reason, "already_consumed")
        self.assertEqual(consumed.exception.state, "dispatching")


class MemoryMutationJournalTests(MutationJournalContract, unittest.TestCase):
    def make_journal(self):
        return MemoryMutationJournal()

    def test_declares_volatile_restart_behavior(self):
        self.assertIs(
            self.make_journal().durability, MutationJournalDurability.VOLATILE)

    def test_capacity_exhaustion_fails_closed(self):
        journal = MemoryMutationJournal(max_entries=1)
        journal.register(PROPOSAL, CONTEXT, expires_at=2_000, now_ms=1_000)
        with self.assertRaisesRegex(MutationJournalError, "capacity"):
            journal.register("d" * 64, CONTEXT, expires_at=2_000, now_ms=1_000)


class SQLiteMutationJournalTests(MutationJournalContract, unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "mutation.sqlite3"

    def tearDown(self):
        self.temporary.cleanup()

    def make_journal(self):
        return SQLiteMutationJournal(self.path)

    def test_declares_durable_restart_behavior(self):
        self.assertIs(
            self.make_journal().durability, MutationJournalDurability.DURABLE)

    def test_dispatching_state_survives_reopen_and_prevents_retry(self):
        first = self.make_journal()
        first.register(PROPOSAL, CONTEXT, expires_at=2_000, now_ms=1_000)
        first.claim(PROPOSAL, CONTEXT, now_ms=1_001)

        reopened = self.make_journal()
        self.assertEqual(reopened.get(PROPOSAL).state, "dispatching")
        with self.assertRaisesRegex(MutationJournalError, "already dispatching"):
            reopened.claim(PROPOSAL, CONTEXT, now_ms=1_002)
        reopened.finish(PROPOSAL, "indeterminate", now_ms=1_003)
        self.assertEqual(self.make_journal().get(PROPOSAL).state, "indeterminate")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_separate_processes_cannot_claim_the_same_business_key(self):
        journal = self.make_journal()
        for proposal in (PROPOSAL, RESULT):
            journal.register(proposal, CONTEXT, expires_at=2_000, now_ms=1_000,
                             business_key="d" * 64)
        ctx = multiprocessing.get_context("spawn")
        ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        workers = [ctx.Process(target=_competing_claim,
                   args=(self.path, proposal, ready, start, output))
                   for proposal in (PROPOSAL, RESULT)]
        try:
            for worker in workers:
                worker.start()
            for _ in workers:
                ready.get(timeout=10)
            start.set()
            self.assertEqual(sorted(output.get(timeout=10) for _ in workers),
                             ["claimed", "refused"])
            for worker in workers:
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
        reopened = self.make_journal()
        self.assertEqual(sum(reopened.get(p).state == "dispatching"
                             for p in (PROPOSAL, RESULT)), 1)

    def test_invalid_timeout_and_symbolic_link_are_rejected(self):
        for value in (True, 0, float("inf"), 121):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                SQLiteMutationJournal(self.path, timeout_s=value)

        target = Path(self.temporary.name) / "target.sqlite3"
        target.touch()
        symbolic_link = Path(self.temporary.name) / "linked.sqlite3"
        symbolic_link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            SQLiteMutationJournal(symbolic_link)

    def test_concurrent_claim_has_exactly_one_winner(self):
        journal = self.make_journal()
        journal.register(PROPOSAL, CONTEXT, expires_at=20_000, now_ms=1_000)
        barrier = threading.Barrier(12)
        outcomes = []
        lock = threading.Lock()

        def claim():
            barrier.wait()
            try:
                journal.claim(PROPOSAL, CONTEXT, now_ms=1_001)
                value = "claimed"
            except MutationJournalError:
                value = "refused"
            with lock:
                outcomes.append(value)

        workers = [threading.Thread(target=claim) for _ in range(12)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(outcomes.count("claimed"), 1)
        self.assertEqual(outcomes.count("refused"), 11)
        self.assertEqual(journal.get(PROPOSAL).state, "dispatching")


if __name__ == "__main__":
    unittest.main()
