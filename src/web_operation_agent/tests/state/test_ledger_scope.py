"""Every audit record must say which session, turn and tenant it belongs to.

`append` stamped only a run id, so an operator reading a ledger shared by several sessions
could not scope an incident without correlating against another store (review answer Q2).
The fields are injected once at the factory rather than threaded through the 77 call sites,
because a call site that can forget them will.
"""
import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.state.ledger import SessionLedger


BASE = {"session_id": "s-1", "turn_id": "m-7", "tenant_digest": "a" * 64}


class EveryRecordCarriesItsScope(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "audit.jsonl"

    def tearDown(self):
        self.directory.cleanup()

    def _written(self):
        return [json.loads(line) for line in
                self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_the_base_fields_reach_the_file_not_only_the_memory_copy(self):
        ledger = SessionLedger(self.path, "run-1", base=BASE)

        ledger.append("turn_identity_bound")
        ledger.append("observation", step=1)

        for record in self._written():
            self.assertEqual({key: record[key] for key in BASE}, BASE)
            self.assertEqual(record["run_id"], "run-1")

    def test_an_event_field_wins_over_the_base_so_a_caller_can_be_specific(self):
        ledger = SessionLedger(self.path, "run-1", base=BASE)

        record = ledger.append("business_tool_result", turn_id="m-8")

        self.assertEqual(record["turn_id"], "m-8")
        self.assertEqual(record["session_id"], "s-1")

    def test_a_ledger_without_a_scope_still_works_and_adds_nothing(self):
        ledger = SessionLedger(self.path, "run-1")

        record = ledger.append("observation")

        self.assertEqual(set(record), {"seq", "t", "run_id", "event"})

    def test_empty_and_wrongly_typed_scope_values_are_dropped(self):
        ledger = SessionLedger(self.path, "run-1", base={
            "session_id": "", "turn_id": None, "tenant_digest": "a" * 64, 7: "x"})

        record = ledger.append("observation")

        self.assertEqual(record["tenant_digest"], "a" * 64)
        self.assertNotIn("session_id", record)
        self.assertNotIn("turn_id", record)

    def test_the_summary_reports_the_same_scope(self):
        ledger = SessionLedger(self.path, "run-1", base=BASE)
        ledger.append("observation")

        summary = ledger.summary()

        self.assertEqual({key: summary[key] for key in BASE}, BASE)

    def test_redaction_still_applies_to_event_fields(self):
        ledger = SessionLedger(self.path, "run-1", base=BASE)

        record = ledger.append("observation", url="https://app/x?jsessionid=abc123")

        self.assertNotIn("abc123", json.dumps(record))


if __name__ == "__main__":
    unittest.main()
