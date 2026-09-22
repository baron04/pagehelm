"""The Odoo source-form gate must derive a selection from cited bytes."""
from __future__ import annotations

import unittest

from web_operation_agent_odoo.demo.source_form_qualify import _selected_value


class SourceFormQualificationTests(unittest.TestCase):
    def test_derives_native_value_only_from_cited_selection(self):
        result = ("source#1 [addons/sale/models/product_template.py:40-52] "
                  "selection=[('order', 'Ordered quantities'), "
                  "('delivery', 'Delivered quantities')]")
        self.assertEqual(_selected_value(result), "delivery")

    def test_refuses_uncited_or_ambiguous_selection(self):
        for result in (
                "selection=[('delivery', 'Delivered quantities')]",
                "source#1 selection=[('order', 'Ordered quantities')]",
                "source#1 selection=[('delivery', 'Delivered quantities'), "
                "('ship', 'Delivered quantities')]"):
            with self.subTest(result=result), self.assertRaises(ValueError):
                _selected_value(result)


if __name__ == "__main__":
    unittest.main()
