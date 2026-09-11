"""A journal must be able to shed history without releasing an unresolved write.

`MemoryMutationJournal` never evicted anything, so 10 000 abandoned proposals refused every
later write; the SQLite journal had no cleanup at all (review finding P2-f). A sweep must
also be unable to undo the one guarantee the journal exists for, so both implementations are
checked against the same expectations here.
"""
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.mutation import (MemoryMutationJournal, MutationJournalError,
                                          SQLiteMutationJournal)


DIGEST = "0" * 64
CONTEXT = "1" * 64
KEY = "2" * 64
HOUR = 60 * 60 * 1_000


def _digest(index: int) -> str:
    return f"{index:064x}"


class JournalRetentionContract:
    def make_journal(self):
        raise NotImplementedError

    def test_an_expired_unclaimed_proposal_is_dropped(self):
        journal = self.make_journal()
        journal.register(DIGEST, CONTEXT, expires_at=2 * HOUR, now_ms=HOUR)

        report = journal.sweep(now_ms=3 * HOUR)

        self.assertEqual(report["expired_prepared"], 1)
        self.assertEqual(report["remaining"], 0)
        self.assertIsNone(journal.get(DIGEST))

    def test_a_live_proposal_survives(self):
        journal = self.make_journal()
        journal.register(DIGEST, CONTEXT, expires_at=9 * HOUR, now_ms=HOUR)

        self.assertEqual(journal.sweep(now_ms=2 * HOUR)["remaining"], 1)
        self.assertIsNotNone(journal.get(DIGEST))

    def test_a_dispatching_row_is_never_dropped_however_old(self):
        """It may still have a request in flight upstream; dropping it frees its key."""
        journal = self.make_journal()
        journal.register(DIGEST, CONTEXT, expires_at=2 * HOUR, now_ms=HOUR,
                         business_key=KEY)
        journal.claim(DIGEST, CONTEXT, now_ms=HOUR)

        report = journal.sweep(now_ms=10_000 * HOUR, retain_resolved_ms=0)

        self.assertEqual(report["unresolved_kept"], 1)
        self.assertEqual(report["remaining"], 1)
        self.assertEqual(journal.get(DIGEST).state, "dispatching")

    def test_an_indeterminate_row_is_never_dropped_and_keeps_blocking_its_key(self):
        journal = self.make_journal()
        journal.register(DIGEST, CONTEXT, expires_at=2 * HOUR, now_ms=HOUR,
                         business_key=KEY)
        journal.claim(DIGEST, CONTEXT, now_ms=HOUR)
        journal.finish(DIGEST, "indeterminate", now_ms=HOUR)

        journal.sweep(now_ms=10_000 * HOUR, retain_resolved_ms=0)

        self.assertEqual(journal.get(DIGEST).state, "indeterminate")
        other = _digest(7)
        journal.register(other, CONTEXT, expires_at=10_001 * HOUR, now_ms=10_000 * HOUR,
                         business_key=KEY)
        with self.assertRaisesRegex(MutationJournalError, "in-flight or unresolved"):
            journal.claim(other, CONTEXT, now_ms=10_000 * HOUR)

    def test_a_resolved_row_is_kept_until_its_retention_elapses(self):
        journal = self.make_journal()
        journal.register(DIGEST, CONTEXT, expires_at=2 * HOUR, now_ms=HOUR)
        journal.claim(DIGEST, CONTEXT, now_ms=HOUR)
        journal.finish(DIGEST, "completed", now_ms=HOUR)

        self.assertEqual(journal.sweep(now_ms=2 * HOUR, retain_resolved_ms=HOUR * 24)["remaining"], 1)
        self.assertEqual(journal.sweep(now_ms=100 * HOUR, retain_resolved_ms=HOUR * 24)["resolved"], 1)
        self.assertIsNone(journal.get(DIGEST))

    def test_a_negative_retention_is_refused(self):
        with self.assertRaisesRegex(MutationJournalError, "non-negative"):
            self.make_journal().sweep(retain_resolved_ms=-1)


class MemoryJournalRetentionTests(JournalRetentionContract, unittest.TestCase):
    def make_journal(self):
        return MemoryMutationJournal()

    def test_capacity_is_reclaimed_from_expired_proposals_instead_of_refusing_writes(self):
        journal = MemoryMutationJournal(max_entries=4)
        for index in range(4):
            journal.register(_digest(index), CONTEXT, expires_at=2 * HOUR, now_ms=HOUR)

        journal.register(_digest(9), CONTEXT, expires_at=6 * HOUR, now_ms=5 * HOUR)

        self.assertIsNotNone(journal.get(_digest(9)))
        self.assertIsNone(journal.get(_digest(0)))

    def test_capacity_still_refuses_when_every_row_is_unresolved(self):
        journal = MemoryMutationJournal(max_entries=2)
        for index in range(2):
            journal.register(_digest(index), CONTEXT, expires_at=2 * HOUR, now_ms=HOUR)
            journal.claim(_digest(index), CONTEXT, now_ms=HOUR)

        with self.assertRaisesRegex(MutationJournalError, "capacity is exhausted"):
            journal.register(_digest(9), CONTEXT, expires_at=6 * HOUR, now_ms=5 * HOUR)


class SQLiteJournalRetentionTests(JournalRetentionContract, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def make_journal(self):
        return SQLiteMutationJournal(Path(self.directory.name) / "journal.sqlite3")


if __name__ == "__main__":
    unittest.main()
