"""Times and days are the signed-in user's, not the server's (P7.5 CR-18).

Odoo stores datetimes in UTC and shows them in each user's own zone. The assistant reported
the stored value, so an order the person sees at 04:00 on the 15th was answered as 20:00 on
the 14th, and a filter "from the 15th" left it out. Between midnight and 08:00 in Shanghai,
every "today" answer was yesterday's.
"""
from __future__ import annotations

import unittest

from web_operation_agent_odoo.adapter import _in_zone, _utc_boundary
from web_operation_agent_odoo.tests.test_adapter import BASE, _FakeOdoo

#: 2026-09-14 20:00 UTC is 2026-09-15 04:00 in Shanghai: a different day for the reader.
ORDERS = [{"id": 1, "name": "S00001", "partner_id": [3, "Acme"], "state": "sale",
           "amount_total": 100.0, "currency_id": [1, "USD"],
           "date_order": "2026-09-14 20:00:00", "invoice_status": "no"}]


def _domain(executor) -> list:
    """The domain the read actually asked Odoo for, from whichever call it made first."""
    for model, method, args, _kwargs in executor.calls:
        if model == "sale.order" and method in ("search_count", "search_read"):
            return args[0]
    raise AssertionError("the read made no scoped call")


class TheReaderSeesTheirOwnClock(unittest.TestCase):
    def test_a_stored_utc_datetime_is_reported_in_the_users_zone(self):
        executor = _FakeOdoo({"sale.order": ORDERS}, tz="Asia/Shanghai")
        result = executor.execute("query_sale_orders", {"limit": 10})

        self.assertEqual(result.content["sale_orders"][0]["date_order"],
                         "2026-09-15 04:00:00")

    def test_without_a_zone_the_stored_value_stands(self):
        """Odoo itself shows UTC to a user with no zone, so the answer agrees with it."""
        executor = _FakeOdoo({"sale.order": ORDERS})
        result = executor.execute("query_sale_orders", {"limit": 10})

        self.assertEqual(result.content["sale_orders"][0]["date_order"],
                         "2026-09-14 20:00:00")


class ADayBoundaryIsTheReadersMidnight(unittest.TestCase):
    def test_a_from_date_starts_at_local_midnight(self):
        executor = _FakeOdoo({"sale.order": ORDERS}, tz="Asia/Shanghai")
        executor.execute("query_sale_orders", {"date_from": "2026-09-15", "limit": 10})

        self.assertIn(["date_order", ">=", "2026-09-14 16:00:00"], _domain(executor))

    def test_the_order_that_is_today_for_the_reader_is_returned(self):
        executor = _FakeOdoo({"sale.order": ORDERS}, tz="Asia/Shanghai")
        result = executor.execute("query_sale_orders", {"date_from": "2026-09-15", "limit": 10})

        self.assertEqual(result.content["matched_count"], 1)

    def test_the_same_question_without_a_zone_uses_utc_midnight(self):
        executor = _FakeOdoo({"sale.order": ORDERS})
        executor.execute("query_sale_orders", {"date_from": "2026-09-15", "limit": 10})

        self.assertIn(["date_order", ">=", "2026-09-15 00:00:00"], _domain(executor))

    def test_a_refused_argument_reaches_the_application_not_at_all(self):
        executor = _FakeOdoo({"sale.order": ORDERS}, tz="Asia/Shanghai")
        with self.assertRaises(Exception):
            executor.execute("query_sale_orders",
                             {"date_from": "2026-09-15", "date_until": "2026-09-15"})
        self.assertEqual(executor.calls, [])


class TheZoneIsReadOnceAndSurvivesABadOne(unittest.TestCase):
    def test_the_zone_is_asked_for_once(self):
        executor = _FakeOdoo({"sale.order": ORDERS}, tz="Asia/Shanghai")
        executor.execute("query_sale_orders", {"limit": 10})
        executor.execute("query_sale_orders", {"limit": 10})

        lookups = [call for call in executor.calls
                   if call[0] == "res.users" and call[3].get("fields") == ["tz"]]
        self.assertEqual(len(lookups), 1)

    def test_a_zone_this_machine_does_not_know_is_ignored(self):
        executor = _FakeOdoo({"sale.order": ORDERS}, tz="Mars/Olympus")
        result = executor.execute("query_sale_orders", {"limit": 10})

        self.assertEqual(result.content["sale_orders"][0]["date_order"],
                         "2026-09-14 20:00:00")
        self.assertEqual(executor.time_zone(), "")


class TheHelpersAreExact(unittest.TestCase):
    def test_conversion_and_boundaries(self):
        from datetime import date

        self.assertEqual(_in_zone("2026-09-14 20:00:00", "Asia/Shanghai"),
                         "2026-09-15 04:00:00")
        self.assertEqual(_in_zone("2026-09-14 20:00:00", "America/New_York"),
                         "2026-09-14 16:00:00")
        self.assertEqual(_utc_boundary(date(2026, 9, 15), "Asia/Shanghai"),
                         "2026-09-14 16:00:00")
        self.assertEqual(_utc_boundary(date(2026, 9, 15), "America/New_York"),
                         "2026-09-15 04:00:00")
        self.assertEqual(_in_zone("not a time", "Asia/Shanghai"), "not a time")
        self.assertEqual(BASE.startswith("http"), True)


if __name__ == "__main__":
    unittest.main()
