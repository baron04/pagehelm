"""The core resolver's words: periods, loose name matching and field labels."""
from __future__ import annotations

import unittest
from datetime import date

from web_operation_agent.goal.data import EntitySchema, FieldSchema, Row, all_of, matches
from web_operation_agent.goal.resolve.phrases import Phrases
from web_operation_agent.goal.resolve.view import EntityView
from web_operation_agent.goal.resolve.words import period, worded


class WordTests(unittest.TestCase):
    def test_periods_are_half_open_spans_around_today(self):
        today = date(2026, 9, 28)
        self.assertEqual(period("last month", today), (date(2026, 8, 1), date(2026, 9, 1)))
        self.assertEqual(period("上季度", today), (date(2026, 4, 1), date(2026, 7, 1)))
        self.assertEqual(period("2026-09", today), (date(2026, 9, 1), date(2026, 10, 1)))
        self.assertEqual(period("最近7天", today), (date(2026, 9, 21), date(2026, 9, 29)))
        self.assertEqual(period("overdue", today), (None, today))
        self.assertIsNone(period("soon", today))

    def test_a_name_matches_by_its_words_in_any_order(self):
        self.assertEqual(worded(["acoustic screens"]), [["acoustic", "screen"]])
        self.assertEqual(worded(["Black Office Chair", "Desk"]),
                         [["Black", "Office", "Chair"], ["Desk"]])
        # Words only a person would type in another script have nothing to match.
        self.assertEqual(worded(["白色隔音屏"]), [])


class FieldLabelTests(unittest.TestCase):
    def setUp(self):
        self.view = EntityView(EntitySchema(key="order", label="Order", fields=(
            FieldSchema("date_order", "Order Date", "datetime", aliases=("订单日期",)),
            FieldSchema("validity_date", "Expiration", "date"),
            FieldSchema("activity_ids", "Activities", "links", aliases=("活动",)),
            FieldSchema("campaign_id", "Campaign", "link", relation="campaign", aliases=("活动",)),
            FieldSchema("invoice_date", "Invoice/Bill Date", "date"),
            FieldSchema("partner_id", "Partner", "link", relation="partner"),
            FieldSchema("partner_name", "Invoice Partner Display Name", "text"),
        )), labels_for=lambda text: ("Campaign", "Activities") if text == "活动" else ())

    def test_labels_match_whole_words_and_field_keys(self):
        self.assertEqual(self.view.field_by_label("Order Date"), "date_order")
        self.assertEqual(self.view.field_by_label("Validity Date"), "validity_date")
        self.assertEqual(self.view.field_by_label("Invoice Date"), "invoice_date")
        self.assertIsNone(self.view.field_by_label("ID"))

    def test_a_word_in_the_persons_language_follows_the_applications_own_translations(self):
        # Two fields translate to 活动; the application's catalogue says which first.
        self.assertEqual(self.view.field_by_label("活动"), "campaign_id")
        self.assertEqual(self.view.field_by_label("订单日期"), "date_order")

    def test_a_translation_that_is_an_exact_label_wins_over_a_looser_first_one(self):
        view = EntityView(EntitySchema(key="line", label="Line", fields=(
            FieldSchema("qty_delivered_method", "Method to update delivered qty", "choice"),
            FieldSchema("product_uom_qty", "Quantity", "number"),
        )), labels_for=lambda text: ("Qty", "Number", "Quantity") if text == "数量" else ())
        self.assertEqual(view.field_by_label("数量"), "product_uom_qty")

    def test_words_without_spaces_match_the_label_that_holds_them(self):
        view = EntityView(EntitySchema(key="order", label="销售订单", fields=(
            FieldSchema("status", "销售状态", "choice"),
            FieldSchema("outStatus", "出库状态说明", "choice"),
            FieldSchema("remark", "备注", "text"),
        )))
        self.assertEqual(view.field_by_label("状态"), "status")
        self.assertIsNone(view.field_by_label("客户"))

    def test_a_link_is_found_inside_a_longer_label(self):
        self.assertEqual(self.view.link_by_label("Invoice Partner Display Name"), "partner_id")


class PhraseTests(unittest.TestCase):
    def test_a_scope_is_described_in_the_persons_language(self):
        zh, en = Phrases("zh"), Phrases()
        self.assertEqual(zh.during("下单时间", "", "last month"), "下单时间在上个月")
        self.assertEqual(zh.compare("合同金额", ">", ["1000"]), "合同金额大于 1000")
        self.assertEqual(zh.compare("Type", "", ["Customer Invoice", "Vendor Bill"]),
                         "Type 为 Customer Invoice 或 Vendor Bill")
        self.assertEqual(zh.any_status("Sales Order", 3), "Sales Order（不限状态）：3")
        self.assertEqual(zh.ordered("合同金额", True), "按合同金额降序")
        # In English the words are what they were.
        self.assertEqual(en.during("Order Date", "", "last month"), "Order Date in last month")
        self.assertEqual(en.compare("Status", "", ["Posted"]), "Status = Posted")
        self.assertEqual(en.filled("Partner", False), "Partner not set")


class SpanPhraseTests(unittest.TestCase):
    def test_two_ends_are_a_span_not_alternatives(self):
        zh = Phrases("zh")
        self.assertEqual(zh.compare("价格", "between", ("3000", "6000")), "价格介于 3000 至 6000")
        self.assertEqual(zh.during("下次联系时间", "", "2026-10-01..2026-10-10"),
                         "下次联系时间在 2026-10-01 至 2026-10-10")
        self.assertEqual(Phrases("en").compare("Price", "between", ("3000", "6000")),
                         "Price between 3000 / 6000")


class FixedDayTests(unittest.TestCase):
    def test_a_fixed_day_is_the_one_periods_count_from(self):
        from web_operation_agent.goal.resolve.resolver import Resolver
        resolver = Resolver(None, None, None, today=date(2026, 9, 28))
        self.assertEqual(resolver.today(), date(2026, 9, 28))

    def test_date_exclusion_keeps_known_dates_outside_the_half_open_period(self):
        from web_operation_agent.goal.resolve.resolver import Resolver
        resolver = Resolver(None, None, None, today=date(2026, 9, 28))
        view = EntityView(EntitySchema(key="item", label="Item", fields=(
            FieldSchema("day", "Date", "date"),)))
        cases = (
            ("2026-09-12", ("2026-09-11", "2026-09-13"), ("2026-09-12",)),
            ("2026-09", ("2026-08-31", "2026-10-01"), ("2026-09-01", "2026-09-30")),
            ("last quarter", ("2026-03-31", "2026-07-01"), ("2026-04-01", "2026-06-30")),
            ("overdue", ("2026-09-28", "2026-09-29"), ("2026-09-27",)),
        )
        for period_name, outside, inside in cases:
            clauses, text = resolver._condition(view, "Date", (period_name,), op="!=")
            self.assertEqual(text, f"Date != {period_name}")
            zh = Resolver(None, None, None, today=date(2026, 9, 28))
            zh.say = Phrases("zh")
            _, caption = zh._condition(view, "Date", (period_name,), op="!=")
            self.assertIn("不在", caption)
            cond = all_of(*clauses)
            for value in outside:
                self.assertTrue(matches(cond, Row("1", "Item", {"day": value})), (period_name, value))
            for value in (*inside, None, ""):
                self.assertFalse(matches(cond, Row("1", "Item", {"day": value})), (period_name, value))


if __name__ == "__main__":
    unittest.main()
