"""The operator loop that closes an Odoo write whose outcome was lost.

Without this the journal only ever grows: `unresolved()` lists writes nobody can settle,
and an old uncertainty becomes indistinguishable from a new one. What is tested here is the
discipline, not the plumbing -- that resolving needs evidence a person actually gathered,
that it needs an attestation only a person can make, and that nothing in this path can
redispatch the write.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.mutation import SQLiteMutationJournal
from web_operation_agent.mutation_ops import unresolved_report

from web_operation_agent_odoo.demo import settle


DIGEST = "a" * 64


def _journal(root: Path) -> SQLiteMutationJournal:
    """One write that got as far as a readback and then lost its answer."""
    import time
    journal = SQLiteMutationJournal(root / "mutations.sqlite3")
    journal.register(DIGEST, "", expires_at=int(time.time() * 1000) + 600_000,
                     reconciliation={"tool": "confirm_quotation",
                                     "args": {"reference": "S00002"},
                                     "application_id": "odoo", "identity": {}})
    journal.claim(DIGEST, "")
    journal.finish(DIGEST, "indeterminate",
                   reconciliation={"phase": "readback", "record_id": "2"})
    return journal


class WhatAnOperatorSees(unittest.TestCase):
    def test_the_listing_says_which_record_to_go_and_look_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = _journal(root)
            rows = [unresolved_report(item, journal_path=root / "mutations.sqlite3")
                    for item in journal.unresolved()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["state"], "indeterminate")
        self.assertEqual(row["tool"], "confirm_quotation")
        # The two fields that turn "a write may have happened" into a place to look.
        self.assertEqual(row["phase"], "readback")
        self.assertEqual(row["record_id"], "2")
        self.assertIn("--writer-stopped", row["resolve_command"])
        self.assertIn(DIGEST, row["resolve_command"])

    def test_the_command_line_lists_without_needing_an_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _journal(root)
            code = settle.main(["--journal", str(root / "mutations.sqlite3"), "list"])
        self.assertEqual(code, 0)


class WhatResolvingRequires(unittest.TestCase):
    def _resolve(self, root: Path, **overrides):
        arguments = {"journal": str(root / "mutations.sqlite3"), "proposal": DIGEST,
                     "evidence": str(root / "evidence.json"), "outcome": "completed",
                     "operator": "alice", "writer_stopped": True, "apply": True}
        arguments.update(overrides)
        return settle.resolve(type("Args", (), arguments)())

    def test_resolving_without_evidence_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _journal(root)
            with self.assertRaisesRegex(SystemExit, "needs an evidence file"):
                self._resolve(root)

    def test_resolving_without_a_stopped_writer_is_refused(self):
        """A timed-out request may still be executing; nothing here can observe that."""
        from web_operation_agent.mutation import MutationJournalError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _journal(root)
            (root / "evidence.json").write_text(json.dumps({"observed": "nothing"}))
            with self.assertRaisesRegex(MutationJournalError, "stopped writer"):
                self._resolve(root, writer_stopped=False)

    def test_the_default_is_a_dry_run_that_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = _journal(root)
            (root / "evidence.json").write_text(json.dumps({"observed": "nothing"}))
            plan = self._resolve(root, apply=False)
            self.assertTrue(plan["dry_run"])
            self.assertEqual(plan["dispatches"], 0)
            self.assertEqual(len(journal.unresolved()), 1, "a dry run settles nothing")

    def test_applying_settles_it_once_and_records_who_decided(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = _journal(root)
            (root / "evidence.json").write_text(
                json.dumps({"observed": "sale.order S00002 is confirmed"}))
            plan = self._resolve(root)
            self.assertFalse(plan["dry_run"])
            self.assertEqual(plan["to"], "completed")
            self.assertEqual(plan["dispatches"], 0, "settling is not writing")
            self.assertEqual(journal.unresolved(), ())
            # And it cannot be settled twice: the state is no longer unresolved.
            from web_operation_agent.mutation import MutationJournalError
            with self.assertRaises(MutationJournalError):
                self._resolve(root)

    def test_no_path_in_this_module_can_redispatch_the_write(self):
        """The one property that matters most, asserted against the source itself."""
        import inspect as inspect_module
        source = inspect_module.getsource(settle)
        for forbidden in ("execute_mutation", "prepare_mutation", "business_tools.execute"):
            self.assertNotIn(forbidden, source)


class InspectingNeedsTheOriginalActor(unittest.TestCase):
    def test_an_authorised_read_is_refused_without_credentials(self):
        """Reconciliation runs as the actor who wrote, or it does not run."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _journal(root)
            arguments = type("Args", (), {
                "journal": str(root / "mutations.sqlite3"), "proposal": DIGEST,
                "evidence": "", "base_url": "http://127.0.0.1:8069",
                "api_key": "", "storage_state": ""})()
            with self.assertRaisesRegex(SystemExit, "original actor"):
                settle.inspect(arguments)


if __name__ == "__main__":
    unittest.main()
