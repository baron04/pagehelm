"""Stated-value extraction and tracing over plain text; no model, no browser."""
import unittest

from web_operation_agent_yudao.reads.answer_numbers import answer_numbers, untraced


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
        # A stated value matches only the same sign: -500 is not provenance for +500.
        self.assertEqual(untraced("退货 -500 元", ("500",)), ("-500",))
        self.assertEqual(untraced("金额 500 元", ("-500",)), ("500",))
        self.assertEqual(untraced("退货 -500 元", ("支出 -500 元",)), ())
        self.assertEqual(untraced("金额 500 元", ("应收 500 元",)), ())

    def test_dates_numeric_ids_and_addresses_in_the_answer_are_not_reported(self):
        self.assertEqual(
            untraced("2026-10-17 创建，编号 99991，详情 /crm/customer/detail/77", ("total=1",)), ())

    def test_mixed_identifiers_preserve_leading_zeros_and_case_without_quantity_normalization(self):
        sources = ('{"no":"PO202610000085","supplierName":"示例供应商"}',)
        self.assertEqual(untraced("PO202610000085的供应商是示例供应商", sources), ())
        for changed in ("PO2026100085", "po202610000085", "PO2026100000850"):
            with self.subTest(changed=changed):
                self.assertEqual(untraced(f"{changed}的供应商是示例供应商", sources), (changed,))

    def test_codes_in_two_entities_require_whole_ascii_tokens_and_exact_separators(self):
        sources = ('{"orderNo":"PO-000017","sku":"SKU_0042-A"}',)
        self.assertEqual(untraced("订单PO-000017，产品SKU_0042-A", sources), ())
        for changed in ("PO-00001", "XPO-000017", "PO_000017", "SKU_42-A", "SKU_0042"):
            with self.subTest(changed=changed):
                self.assertEqual(untraced(changed, sources), (changed,))

    def test_person_supplied_codes_are_literal_sources_and_urls_are_not_stated_codes(self):
        self.assertEqual(untraced("已查询AB-0002，见https://test.invalid/AB-0003及/orders/CD0004", ("查询AB-0002",)), ())
        self.assertEqual(untraced("型号L30合计2,440.80元", ("型号L30，总价2440.8",)), ())

    def test_iso_timestamps_are_not_identifiers(self):
        for stamp in ("2026-10-19T10:00:00", "2026-10-19T10:00:00.123Z", "2026-10-19T10:00:00+08:00"):
            with self.subTest(stamp=stamp):
                self.assertEqual(untraced(stamp, ()), ())

    def test_terminal_period_does_not_hide_a_changed_code_or_split_an_embedded_period(self):
        self.assertEqual(untraced("PO0002.", ("PO000002",)), ("PO0002",))
        self.assertEqual(untraced("SKU.0002.", ("SKU.0002",)), ())
        self.assertEqual(untraced("SKU.0002", ("SKU.00020",)), ("SKU.0002",))

    def test_an_answer_without_numbers_passes_without_sources(self):
        self.assertEqual(untraced("已打开订单页。", ()), ())

    def test_chinese_enumeration_markers_are_not_stated_values(self):
        self.assertEqual(untraced("结果如下：\n1、已完成\n2）待处理", ("total=0",)), ())
        self.assertEqual(untraced("结果：1.甲 2.乙", ("total=0",)), ())
        # A dotted decimal is not a marker: 3.14 stays a stated value.
        self.assertEqual(untraced("结果 3.14", ("total=0",)), ("3.14",))

    def test_a_number_after_an_english_id_word_is_not_a_labeled_identifier(self):
        # "paid" ends in "id" but is not the label "id": 12345 stays a value.
        self.assertEqual(untraced("paid 12345", ("total=0",)), ("12345",))
        self.assertEqual(untraced("id：12345", ("total=0",)), ())
        self.assertEqual(untraced("ID=12345", ("total=0",)), ())

    def test_the_chinese_minus_signs_like_the_ascii_one(self):
        self.assertEqual(untraced("余额负500元", ("balance=-500",)), ())
        self.assertEqual(untraced("余额负 500 元", ("balance=-500",)), ())
        self.assertEqual(untraced("负500元", ("balance=500",)), ("-500",))


if __name__ == "__main__":
    unittest.main()
