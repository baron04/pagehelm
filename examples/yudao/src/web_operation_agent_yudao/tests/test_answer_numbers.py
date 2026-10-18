"""Stated-value extraction and tracing over plain text; no model, no browser."""
import unittest

from web_operation_agent_yudao.answer_numbers import answer_numbers, untraced


class ExtractionTests(unittest.TestCase):
    def test_amounts_counts_and_percentages_are_stated_values(self):
        self.assertEqual(answer_numbers("合计 2,440.80 元，共 12 条，税率 13%，库存 24 个"),
                         ["2440.8", "12", "13", "24"])

    def test_dates_times_and_record_numbers_are_not_stated_values(self):
        self.assertEqual(answer_numbers(
            "2026-10-17 14:30 下单，10月17日 8点半 提醒，2026年10月 起，编号 CG2026100004，型号 L30，"
            "订单编号：1024，电话 13800138000"), [])

    def test_addresses_and_urls_do_not_contribute_numbers(self):
        self.assertEqual(answer_numbers(
            "详情见 /crm/customer/detail/1024 与 https://synthetic.test/orders/7?page=2"), [])

    def test_structural_ordinals_and_list_markers_are_not_values(self):
        self.assertEqual(answer_numbers("第 2 页如下：\n1. 甲\n2. 乙\n3. 丙\n第3名是乙"), [])

    def test_ordinal_ranges_exclude_both_positions_but_keep_business_counts_and_bounds(self):
        for separator in ("-", "–", "—", "~", "～", "至", "到"):
            with self.subTest(separator=separator):
                self.assertEqual(answer_numbers(f"第 11 {separator} 20 条，共10条，金额100至200元"),
                                 ["10", "100", "200"])
        self.assertEqual(untraced("第2页显示第11–20条，共10条", ("第2页，每页10条，本页10行，共385条",)), ())

    def test_separators_trailing_zeros_and_signs_normalize_to_one_form(self):
        self.assertEqual(answer_numbers("¥2,440.80 和 +5 与 −3 以及 -0.50"),
                         ["2440.8", "5", "-3", "-0.5"])

    def test_a_value_stated_twice_is_listed_once(self):
        self.assertEqual(answer_numbers("共 12 条；12 条已核对"), ["12"])


class TracingTests(unittest.TestCase):
    def test_values_the_turn_read_are_traced_after_normalization(self):
        self.assertEqual(
            untraced("合计 2440.8 元，共 12 条", ('{"price": 2440.80}', "total=12（符合条件的总数）")), ())

    def test_values_from_the_person_s_own_words_are_traced(self):
        self.assertEqual(untraced("已筛出金额超过 2000 元的订单", ("金额超过 2000 元", "total=3")), ())

    def test_a_fabricated_value_is_reported_in_the_form_the_answer_used(self):
        self.assertEqual(untraced("合计 4,999 元", ("total=12", '{"price": 2440.80}')), ("4,999",))

    def test_signs_unify_before_comparison(self):
        self.assertEqual(untraced("退货 -500 元", ("500",)), ())
        self.assertEqual(untraced("金额 500 元", ("-500",)), ())

    def test_dates_ids_and_addresses_in_the_answer_are_never_reported(self):
        self.assertEqual(
            untraced("2026-10-17 创建，编号 CG99991，详情 /crm/customer/detail/77", ("total=1",)), ())

    def test_an_answer_without_numbers_passes_without_sources(self):
        self.assertEqual(untraced("已打开订单页。", ()), ())


if __name__ == "__main__":
    unittest.main()
