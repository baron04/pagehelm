import copy
import tempfile
import unittest
from pathlib import Path

from evals.odoo import ledger, report
from evals.odoo.tests.test_ledger import header, slots


class ReportLimitationTests(unittest.TestCase):
    def build(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempts.jsonl"
            ledger.create(path, header(slots(1)))
            summary = ledger.read(path)[1]
        return report.build_report(
            batches=[summary], tasks={"task-000": {"class": "normal", "stratum": "s",
                                                   "read_only": True}})

    def test_every_report_states_the_provider_limitations(self):
        value = self.build()
        self.assertEqual(value["limitations"], list(report.LIMITATIONS))
        self.assertIn("provider_alias_is_not_a_model_snapshot", value["limitations"])
        self.assertEqual(report.verify_report(value)["status"], "READY")

    def test_a_report_that_drops_or_changes_a_limitation_is_refused(self):
        value = self.build()
        for mutate in (lambda item: item["limitations"].pop(),
                       lambda item: item["limitations"].__setitem__(0, "alias_verified"),
                       lambda item: item.pop("limitations")):
            candidate = copy.deepcopy(value)
            mutate(candidate)
            with self.subTest(candidate=candidate.get("limitations")):
                with self.assertRaises(ValueError):
                    report.verify_report(candidate)


if __name__ == "__main__":
    unittest.main()
