"""The strongest evidence about an uncertain write must survive to the operator.

`twenty.py` raised four indeterminate outcomes with text only, so the record identifier the
upstream acknowledgement had already returned was discarded: the journal kept a digest, the
ledger event carried no digest at all, and settling a row meant scanning the collection
(review finding F5d).
"""
import json
import unittest

from web_operation_agent.business import BusinessMutationIndeterminate
from web_operation_agent.mutation import MemoryMutationJournal, merged_reconciliation


DIGEST = "0" * 64
CONTEXT = "1" * 64
RECORD = "99999999-9999-4999-8999-999999999999"


class TheExceptionCarriesBoundedPostDispatchFacts(unittest.TestCase):
    def test_a_readback_failure_names_the_record_the_write_produced(self):
        error = BusinessMutationIndeterminate(
            "readback failed", phase="readback", record_id=RECORD)

        self.assertEqual(error.reconciliation, {"phase": "readback", "record_id": RECORD})

    def test_an_outcome_with_nothing_learned_carries_nothing(self):
        self.assertEqual(BusinessMutationIndeterminate("unknown").reconciliation, {})

    def test_oversized_or_wrongly_typed_facts_are_dropped_rather_than_carried(self):
        error = BusinessMutationIndeterminate(
            "x", phase="p" * 65, record_id=object())

        self.assertEqual(error.reconciliation, {})


class TheJournalRowKeepsThemForTheOperator(unittest.TestCase):
    def _dispatching(self):
        journal = MemoryMutationJournal()
        journal.register(DIGEST, CONTEXT, expires_at=2, now_ms=1,
                         reconciliation={"tool": "create_company", "name": "Acme"})
        journal.claim(DIGEST, CONTEXT, now_ms=1)
        return journal

    def test_finishing_indeterminate_merges_the_record_id_into_the_intent(self):
        journal = self._dispatching()

        journal.finish(DIGEST, "indeterminate", now_ms=1, reconciliation={
            "phase": "readback", "record_id": RECORD})

        stored = json.loads(journal.get(DIGEST).reconciliation_json)
        self.assertEqual(stored["record_id"], RECORD)
        self.assertEqual(stored["phase"], "readback")
        # What the write intended is still there; the merge adds, it does not replace.
        self.assertEqual(stored["name"], "Acme")

    def test_finishing_without_new_facts_leaves_the_registration_untouched(self):
        journal = self._dispatching()

        journal.finish(DIGEST, "indeterminate", now_ms=1)

        self.assertEqual(json.loads(journal.get(DIGEST).reconciliation_json),
                         {"tool": "create_company", "name": "Acme"})

    def test_a_corrupt_or_absent_registration_does_not_lose_the_new_facts(self):
        self.assertEqual(json.loads(merged_reconciliation("not json", {"record_id": RECORD})),
                         {"record_id": RECORD})
        self.assertEqual(json.loads(merged_reconciliation("", {"record_id": RECORD})),
                         {"record_id": RECORD})


if __name__ == "__main__":
    unittest.main()
