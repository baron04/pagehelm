"""Complete comparisons must not mistake a truncated sample or mode for an extreme."""
import unittest

from web_operation_agent.goal.data import Row
from web_operation_agent.goal.executor import _render
from web_operation_agent.goal.ports import Resolved
from web_operation_agent.goal.resolve.extrema import resolve_extreme
from web_operation_agent.goal.spec import Metric


class ExtremeTests(unittest.TestCase):
    def resolve(self, amounts, *, operation="max", units=None, after=None):
        rows = tuple(Row(str(i), f"Record {i}", {"amount": v},
                         {"amount": str(v)}, {"amount": (units or [""] * len(amounts))[i]})
                     for i, v in enumerate(amounts))
        return resolve_extreme(operation=operation, label="Amount", key="amount", subject="Records",
                               count=len(rows), after=len(rows) if after is None else after, rows=rows)

    def test_extreme_is_not_frequency_and_keeps_every_tie(self):
        result = self.resolve([0, 0, 0, 8000, 3500, 8000, None, -2])
        self.assertEqual(result.aggregate.extreme, ("max", "Amount", "8000"))
        self.assertEqual(result.aggregate.records, ("Record 3", "Record 5"))
        for zh in (False, True):
            answer = "\n".join(_render(result, None, zh))
            self.assertIn("8000", answer)
            self.assertNotIn("Record 4", answer)
            self.assertNotIn("最多", answer)

    def test_minimum_includes_negative_and_zero_and_excludes_missing(self):
        self.assertEqual(self.resolve([None, "", 0, -2, -2], operation="min").aggregate.records,
                         ("Record 3", "Record 4"))
        self.assertEqual(self.resolve([None, 0], operation="min").aggregate.extreme[2], "0")

    def test_no_known_values_is_not_zero(self):
        result = self.resolve([None, ""])
        self.assertIsNone(result.aggregate.extreme[2])
        self.assertIn("No known numeric values", _render(result, None, False)[0])

    def test_invalid_values_units_and_incomplete_reads_refuse(self):
        for value in (True, float("inf"), float("nan"), "123"):
            with self.subTest(value=value):
                self.assertTrue(self.resolve([value]).problem)
        self.assertTrue(self.resolve([1, 2], units=["USD", "EUR"]).problem)
        self.assertTrue(self.resolve([1, 2], units=["USD", ""]).problem)
        self.assertTrue(self.resolve([1], units=["unknown-currency"]).problem)
        self.assertTrue(self.resolve([1], after=2).problem)
        for rows in ((Row("1", "A", {"amount": 2}),),
                     (Row("1", "A", {"amount": 2}), Row("1", "B", {"amount": 2})),
                     (Row("", "A", {"amount": 2}), Row("2", "B", {"amount": 2})),
                     (Row("1", "A"), Row("2", "B", {"amount": 2}))):
            self.assertTrue(resolve_extreme(operation="max", label="Amount", key="amount",
                                           subject="Records", count=2, after=2, rows=rows).problem)

    def test_model_contract_accepts_min_max(self):
        for op in ("min", "max"):
            self.assertEqual(Metric.from_json({"op": op, "field": "Amount"}).op, op)

    def test_empty_scope_is_reported_without_claiming_a_missing_capability(self):
        result = Resolved(kind="fact", problem="no matches", no_matches="Contracts · name = Synthetic")
        for zh, words in ((True, "没有找到符合"), (False, "No record matches")):
            answer = "\n".join(_render(result, None, zh))
            self.assertIn(words, answer)
            self.assertNotIn("Could not do", answer)


if __name__ == "__main__":
    unittest.main()
