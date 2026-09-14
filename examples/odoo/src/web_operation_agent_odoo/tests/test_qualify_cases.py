import unittest

from web_operation_agent_odoo.demo.qualify import CASES, approved_write_ran


class ApprovedWriteRanTests(unittest.TestCase):
    """The post-run fixture expectation follows the cases that ran, not the case table."""

    def test_read_only_run_expects_an_unchanged_fixture(self):
        executed = [{"case": name} for name, case in CASES.items() if not case.get("writes")]
        self.assertTrue(executed)
        self.assertFalse(approved_write_ran(executed))

    def test_unapproved_write_case_expects_an_unchanged_fixture(self):
        refusals = [{"case": name} for name, case in CASES.items()
                    if case.get("writes") and not case.get("approve")]
        self.assertTrue(refusals)
        self.assertFalse(approved_write_ran(refusals))

    def test_an_executed_approved_write_expects_a_changed_fixture(self):
        writes = [{"case": name} for name, case in CASES.items() if case.get("approve")]
        self.assertTrue(writes)
        self.assertTrue(approved_write_ran(writes[:1]))


if __name__ == "__main__":
    unittest.main()
