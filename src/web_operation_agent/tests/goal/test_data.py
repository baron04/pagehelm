"""Conditions over the data port: combination and in-memory evaluation."""
from __future__ import annotations

import unittest
from datetime import date, datetime

from web_operation_agent.goal.data import (EVERYTHING, All, Any, Compare, LinkValue, Native, Not,
                                            NotEvaluable, Row, all_of, any_of, as_bool, is_native,
                                            matches, parts)


def _row(**values) -> Row:
    return Row(id="1", display="S00001", values=values)


class CombinationTests(unittest.TestCase):
    def test_all_of_flattens_and_drops_everything(self):
        a, b = Compare("state", "eq", "sale"), Compare("total", "gt", 10)
        self.assertEqual(all_of(EVERYTHING, a, None, all_of(b)), All((a, b)))
        self.assertEqual(all_of(a), a)
        self.assertEqual(all_of(), EVERYTHING)
        self.assertEqual(parts(All((a, b))), (a, b))

    def test_any_of_with_everything_is_everything(self):
        a = Compare("state", "eq", "sale")
        self.assertEqual(any_of(a, EVERYTHING), EVERYTHING)
        self.assertEqual(any_of(a, any_of(Compare("state", "eq", "done"))),
                         Any((a, Compare("state", "eq", "done"))))

    def test_native_is_seen_through_combinations(self):
        self.assertTrue(is_native(Not(all_of(Compare("a", "set"), Native([["x", "=", 1]])))))
        self.assertFalse(is_native(all_of(Compare("a", "set"))))

    def test_unknown_operator_is_refused(self):
        with self.assertRaises(ValueError):
            Compare("a", "ilike", "x")


class MatchTests(unittest.TestCase):
    def test_numbers_text_and_emptiness(self):
        row = _row(total="1,250.50", name="Office Chair", note=None, qty=3)
        self.assertTrue(matches(Compare("total", "gt", 1000), row))
        self.assertTrue(matches(Compare("qty", "le", 3), row))
        self.assertTrue(matches(Compare("name", "eq", "office chair"), row))
        self.assertTrue(matches(Compare("name", "contains", "CHAIR"), row))
        self.assertTrue(matches(Compare("name", "starts", "off"), row))
        self.assertTrue(matches(Compare("note", "unset"), row))
        self.assertFalse(matches(Compare("note", "gt", 1), row))
        self.assertTrue(matches(Compare("note", "ne", "x"), row))

    def test_a_yes_no_field_compares_by_its_value_and_by_the_words_for_it(self):
        # `false` is a value the application holds, not an empty field; and a word that is
        # not a yes/no word matches neither side rather than turning into `true`.
        paid, unpaid = _row(payStatus=True), _row(payStatus=False)
        self.assertTrue(matches(Compare("payStatus", "eq", False), unpaid))
        self.assertFalse(matches(Compare("payStatus", "eq", False), paid))
        for word in ("no", "false", "否", 0):
            self.assertTrue(matches(Compare("payStatus", "eq", word), unpaid), word)
        for word in ("yes", "true", "是", 1):
            self.assertTrue(matches(Compare("payStatus", "eq", word), paid), word)
        for word in ("未支付", "已发货", None, 2):
            self.assertFalse(matches(Compare("payStatus", "eq", word), unpaid), word)
            self.assertFalse(matches(Compare("payStatus", "eq", word), paid), word)
        # The field holds a value either way, so it is set, and only "not equal" holds
        # for a value it does not carry.
        self.assertTrue(matches(Compare("payStatus", "set"), unpaid))
        self.assertFalse(matches(Compare("payStatus", "unset"), unpaid))
        self.assertTrue(matches(Compare("payStatus", "ne", True), unpaid))
        self.assertEqual([as_bool(v) for v in (True, "no", "是", 0, "未支付", None, 2)],
                         [True, False, True, False, None, None, None])

    def test_links_compare_by_id_or_by_name(self):
        row = _row(customer=LinkValue("7", "Gemini Furniture"))
        self.assertTrue(matches(Compare("customer", "eq", "7"), row))
        self.assertTrue(matches(Compare("customer", "in", ("3", 7)), row))
        self.assertTrue(matches(Compare("customer", "contains", "gemini"), row))
        self.assertFalse(matches(Compare("customer", "not_in", ("7",)), row))

    def test_dates_and_datetimes(self):
        row = _row(day=date(2026, 9, 12), at="2026-09-12T08:30:00")
        self.assertTrue(matches(Compare("day", "ge", date(2026, 9, 1)), row))
        self.assertTrue(matches(Compare("day", "lt", "2026-10-01"), row))
        self.assertTrue(matches(Compare("at", "lt", datetime(2026, 9, 13)), row))

    def test_boolean_structure(self):
        row = _row(state="sale", total=5)
        cond = all_of(any_of(Compare("state", "eq", "draft"), Compare("state", "eq", "sale")),
                      Not(Compare("total", "gt", 10)))
        self.assertTrue(matches(cond, row))
        self.assertTrue(matches(EVERYTHING, row))

    def test_native_cannot_be_evaluated(self):
        with self.assertRaises(NotEvaluable):
            matches(Native([["state", "=", "sale"]], "Sales Orders"), _row(state="sale"))


if __name__ == "__main__":
    unittest.main()
