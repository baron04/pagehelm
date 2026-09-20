"""Two customers of the same name can be told apart and opened (P7.5 CR-23).

The acceptance data holds two customers called "P75 Twin Traders", one in Denver and one in
Austin. A name search matches both, so the adapter authorised neither record's route, and the
assistant asked which one to open. Answering "Austin 那条" produced the same question again:
the only filter a customer read offered was the name, and no name singles either of them out.
"""
from __future__ import annotations

import unittest

from web_operation_agent.business.runtime import BusinessToolRequestError
from web_operation_agent_odoo.tests.test_adapter import _customers, _FakeOdoo

TWINS = [("P75 Twin Traders", "twin.denver@p75.example.test"),
         ("P75 Twin Traders", "twin.austin@p75.example.test"),
         ("Azure Interior", "azure@example.test")]


class ARecordIsReachableByItsOwnId(unittest.TestCase):
    def setUp(self):
        self.executor = _FakeOdoo({"res.partner": _customers(TWINS)})

    def test_a_name_that_matches_both_opens_neither(self):
        result = self.executor.execute("query_customers", {"name_contains": "Twin Traders"})
        self.assertEqual(result.content["matched_count"], 2)
        self.assertEqual(result.navigation_target, "")
        self.assertEqual(result.allowed_routes, ("/odoo/customers",))
        self.assertEqual([option.label for option in result.selection_options],
                         ["P75 Twin Traders — twin.denver@p75.example.test — Paris",
                          "P75 Twin Traders — twin.austin@p75.example.test — Paris"])

    def test_the_chosen_record_is_read_by_its_id_and_opens(self):
        chosen = self.executor.execute(
            "query_customers", {"name_contains": "Twin Traders"}).content["customers"][1]
        record_id = int(chosen["ui_path"].rsplit("/", 1)[-1])
        result = self.executor.execute("query_customers", {"record_id": record_id})
        self.assertEqual(result.content["matched_count"], 1)
        self.assertEqual(result.content["customers"][0]["email"],
                         "twin.austin@p75.example.test")
        self.assertEqual(result.navigation_target, chosen["ui_path"])
        self.assertIn(chosen["ui_path"], result.allowed_routes)

    def test_the_id_is_checked(self):
        for value in (0, -1, "11", True):
            with self.subTest(value=value), self.assertRaises(BusinessToolRequestError):
                self.executor.execute("query_customers", {"record_id": value})

    def test_every_read_offers_it(self):
        from web_operation_agent_odoo.adapter import QUERY_SCHEMAS
        for name, schema in QUERY_SCHEMAS.items():
            with self.subTest(tool=name):
                self.assertIn(
                    "record_id", schema["function"]["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
