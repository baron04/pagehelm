"""A batch cost stop must bound the batch, not each process's share of it.

`SpendMeter` kept its batch columns in memory, so two meters accounting to one ledger each
spent the stop line in full -- twice the money the operator authorised (review finding
P2-d). The batch column is now recomputed from the ledger inside the same exclusive lock
that serialises the write.
"""
import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.model import BudgetExhausted, SpendMeter


USAGE = {"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000}
PRICE = {"input_per_m": 1.0, "output_per_m": 1.0, "cache_read_per_m": 0.0,
         "currency": "CNY"}


class BatchSpendIsCountedAcrossProcesses(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.ledger = Path(self.directory.name) / "spend.jsonl"

    def tearDown(self):
        self.directory.cleanup()

    def _meter(self, stop=None, batch_id="shared-batch"):
        # The per-run token ceiling is a separate guard; this suite is about the batch stop,
        # so each call uses a fresh run id and the ceiling is lifted above the fixture.
        meter = SpendMeter(ledger=self.ledger, batch_id=batch_id,
                           batch_cost_stop=stop, run_input_token_abort=10_000_000)
        meter.price = PRICE
        return meter

    def _record(self, meter, run_id):
        return meter.record(provider="p", model="m", run_id=run_id, usage=USAGE)

    def test_a_second_meter_sees_the_first_meter_s_spend(self):
        first, second = self._meter(stop=2.5), self._meter(stop=2.5)

        self._record(first, "a")
        row = self._record(second, "b")

        self.assertEqual(row["batch_cost_cny"], 2.0)

    def test_the_stop_line_bounds_the_batch_not_each_process(self):
        first, second = self._meter(stop=2.5), self._meter(stop=2.5)
        self._record(first, "a")
        self._record(second, "b")

        with self.assertRaises(BudgetExhausted) as raised:
            self._record(first, "c")

        self.assertEqual(raised.exception.kind, "batch_cost_stop")
        total = sum(row["call_cost_cny"] for row in self._rows()
                    if row["accounting_status"] == "reported")
        self.assertEqual(total, 3.0)
        self.assertLess(total, 2 * 2.5)

    def test_another_batch_in_the_same_ledger_is_not_counted(self):
        other = self._meter(stop=9.0, batch_id="other-batch")
        self._record(other, "x")

        row = self._record(self._meter(stop=9.0), "a")

        self.assertEqual(row["batch_cost_cny"], 1.0)

    def test_a_dispatched_call_with_no_usage_reports_the_batch_spend(self):
        meter = self._meter(stop=9.0)
        self._record(meter, "a")

        meter.record_unmetered(provider="p", model="m", run_id="b",
                               request_id="a" * 32, error_code="provider_cancelled")

        rows = self._rows()
        self.assertEqual(rows[-1]["accounting_status"], "usage_unavailable")
        self.assertEqual(rows[-1]["batch_cost_cny"], 1.0)

    def test_without_a_stop_line_the_ledger_is_still_append_only_and_intact(self):
        meter = self._meter()
        for index in range(3):
            self._record(meter, f"run-{index}")

        rows = self._rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["calls"] for row in rows], [1, 2, 3])

    def _rows(self):
        return [json.loads(line) for line in
                self.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()
