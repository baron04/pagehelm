"""The observation algebra's own rules.

What is tested here is the difference the six repaired defects turned on: a source that was
read as empty and a source that could not be read are different facts, they survive a round
trip through evidence as different facts, and a consumer cannot take the second for the first
by forgetting to look.
"""
from __future__ import annotations

import unittest

from evals.odoo.judging import capture


class CaptureTests(unittest.TestCase):
    def test_a_source_outside_the_closed_set_is_refused(self):
        for build in (lambda: capture.Present(1, "guesswork", {}),
                      lambda: capture.Unavailable("why", "guesswork", {})):
            with self.assertRaises(ValueError):
                build()

    def test_an_empty_read_and_an_unread_source_are_different_facts(self):
        empty = capture.Present([], "agent_audit", {"attempt_id": "a-1"})
        unread = capture.Unavailable("StackError", "agent_audit", {"attempt_id": "a-1"})
        self.assertEqual((empty.available, unread.available), (True, False))
        self.assertEqual(empty.require("audit"), [])
        with self.assertRaises(capture.Unobserved) as raised:
            unread.require("audit")
        self.assertIn("StackError", str(raised.exception))
        # Both answer the same questions, so a consumer branches on the answer, never on type.
        for name in ("available", "complete", "source", "scope", "require"):
            self.assertTrue(hasattr(empty, name) and hasattr(unread, name), name)
        # Neither offers to stand in for the value with a default.
        for holder in (empty, unread):
            self.assertFalse([name for name in dir(holder) if "default" in name or
                              name in {"value_or", "get"}])

    def test_a_round_trip_keeps_the_two_apart(self):
        for original in (capture.Present([], "fixture_guard", {"attempt_id": "a-1"}),
                         capture.Present({"path": "/odoo"}, "final_page", {}, complete=False),
                         capture.Unavailable("TimeoutError", "final_page", {"turn": 2})):
            self.assertEqual(capture.of_record(capture.as_record(original)), original)

    def test_a_record_of_another_shape_is_refused_rather_than_filled_in(self):
        whole = capture.as_record(capture.Present(1, "evaluator_output", {}))
        for broken in ({**whole, "source": "guesswork"}, {**whole, "status": "maybe"},
                       {key: value for key, value in whole.items() if key != "complete"},
                       {**whole, "complete": "yes"}, {**whole, "scope": None},
                       {**whole, "extra": 1}, {"status": "unavailable", "source": "final_page",
                                               "scope": {}, "reason": ""},
                       1, None, {"value": 1}):
            with self.assertRaises(ValueError):
                capture.of_record(broken)

    def test_read_captures_the_listed_failures_and_the_completeness_of_the_answer(self):
        def fails():
            raise LookupError("nothing there")

        caught = capture.read("agent_state", {"attempt_id": "a-1"}, fails, errors=(LookupError,))
        self.assertEqual((caught.available, caught.reason), (False, "LookupError"))
        with self.assertRaises(LookupError):
            capture.read("agent_state", {}, fails, errors=(TimeoutError,))
        partial = capture.read("agent_audit", {}, lambda: [1, 2], complete=lambda value: False)
        self.assertEqual((partial.available, partial.complete, partial.value), (True, False, [1, 2]))
        self.assertTrue(capture.read("agent_audit", {}, lambda: []).complete)


if __name__ == "__main__":
    unittest.main()
