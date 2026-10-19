"""References in an answer are filled from the read itself; the text around them is read by the program."""
from dataclasses import replace
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import unittest

from web_operation_agent.answer.evidence import Origin
from web_operation_agent.execution.answer_references import ReferenceTable, TurnReferences, table_from
from web_operation_agent.execution.read_summary import (RelationValue, SummaryColumn, SummaryRead, SummaryRow,
                                                        SummarySpec, compute_summary)

_ZONE = timezone(timedelta(hours=8))


def contract(**changes):
    columns = (SummaryColumn("id", "relation"), SummaryColumn("customerId", "relation", display_name="客户"),
               SummaryColumn("auditStatus", "number", display_name="审批状态", labels=(("20", "审核通过"),)),
               SummaryColumn("totalPrice", "number", "元", display_name="合同金额"),
               SummaryColumn("taxPrice", "number", display_name="税额", unverified_unit=True),
               SummaryColumn("orderDate", "datetime", display_name="下单日期"),
               SummaryColumn("signed", "boolean"))
    row = (RelationValue("998386", "998386"), RelationValue("73", "合成规模·杭州科技有限公司073"), Decimal(20),
           Decimal("9743"), Decimal("101.4"), datetime(2026, 6, 15, 10, 0, tzinfo=_ZONE), True)
    other = (RelationValue("998387", "998387"), RelationValue("74", "Other"), Decimal(0),
             Decimal("1"), None, datetime(2026, 6, 16, 10, 0, tzinfo=_ZONE), False)
    rows = changes.pop("rows", (row,))
    source = SummaryRead(Origin("typed", "synthetic:contracts", "now", "{}"), "actor", columns,
                         tuple(SummaryRow(str(i), values) for i, values in enumerate(rows)),
                         len(rows), len(rows), changes.pop("complete", True))
    spec = SummarySpec.from_dict(changes.pop("spec", {"columns": [c.name for c in columns]}))
    return compute_summary(source, spec), (row, other)


class ProseValueTests(unittest.TestCase):
    def test_cells_read_as_the_page_shows_them_without_changing_a_value(self):
        refs = TurnReferences()
        result, _rows = contract()
        refs.add(table_from(result), result)
        composed = refs.compose("客户{r1.customerId}，{r1.auditStatus}，金额{r1.totalPrice}，税额{r1.taxPrice}，"
                                "下单{r1.orderDate}，已签{r1.signed}", said=("合同 HT001 的情况",))
        self.assertEqual(composed.problems, ())
        # A relation states its identity, so a 编号 column never reads as a bare name.
        self.assertEqual(composed.text, "客户合成规模·杭州科技有限公司073（73），审核通过，金额9743元，税额101.4（单位未核验），"
                                        "下单2026-06-15 10:00:00，已签是")

    def test_an_unknown_code_says_so_and_an_empty_cell_says_empty(self):
        refs = TurnReferences()
        _result, (row, _other) = contract()
        result, _ = contract(rows=(row[:2] + (Decimal(30),) + row[3:4] + (None,) + row[5:],))
        refs.add(table_from(result), result)
        self.assertEqual(refs.compose("{r1.auditStatus}；{r1.taxPrice}", said=()).text, "30（字典中无此代码）；空")

    def test_several_rows_need_a_row_number_and_an_incomplete_read_offers_only_its_count(self):
        refs = TurnReferences()
        _result, rows = contract()
        result, _ = contract(rows=rows)
        refs.add(table_from(result), result)
        self.assertIn("写成 {r1#行号.customerId}", refs.compose("{r1.customerId}").problems[0])
        self.assertEqual(refs.compose("{r1#2.customerId}").text, "Other（74）")
        partial, _ = contract(rows=rows, complete=False)
        refs.add(table_from(partial), partial)
        self.assertEqual(refs.compose("共 {r2.total} 条").text, "共 2 条")
        self.assertTrue(refs.compose("{r2.表}").problems)
        self.assertTrue(refs.compose("{r2#1.customerId}").problems)

    def test_each_count_is_offered_with_what_it_means(self):
        # The recorded Q092 follow-up cited a filter's 0 hits as the rows read, and the
        # first row of an unordered read as the largest amount.
        refs = TurnReferences()
        _result, rows = contract()
        filtered, _ = contract(rows=rows, spec={"columns": ["customerId", "totalPrice"],
                                                "where": [{"column": "totalPrice", "op": ">", "value": 1000000}]})
        refs.add(table_from(filtered), filtered)
        told = refs.describe("r1")
        self.assertIn("{r1.total}=0，满足这次读取条件的记录数", told)
        self.assertIn("{r1.scanned}=2，这次实际读到的行数", told)
        self.assertIn("合同金额 (元) 大于 1000000", told)
        self.assertEqual(refs.compose("读了 {r1.scanned} 行，命中 {r1.total} 行").text, "读了 2 行，命中 0 行")
        plain, _ = contract(rows=rows)
        refs.add(table_from(plain), plain)
        self.assertIn("行号只是读取顺序，不代表大小或先后", refs.describe("r2"))
        ordered, _ = contract(rows=rows, spec={"columns": ["totalPrice"],
                                               "order_by": [{"column": "totalPrice", "direction": "desc"}]})
        refs.add(table_from(ordered), ordered)
        self.assertIn("按 合同金额 降序", refs.describe("r3"))
        problem = refs.compose("最大的是 18921.5 元", said=()).problems[0]
        self.assertIn("不要换成意思不同的引用", problem)

    def test_a_table_reference_delivers_the_whole_bounded_result(self):
        refs = TurnReferences()
        _result, rows = contract()
        result, _ = contract(rows=rows, spec={"columns": ["customerId", "totalPrice"],
                                              "order_by": [{"column": "totalPrice", "direction": "desc"}]})
        refs.add(table_from(result), result)
        composed = refs.compose("按金额排序：\n\n{r1.表}", said=("按金额排序",), user_text="按金额排序")
        self.assertEqual(composed.problems, ())
        self.assertIn("| 合成规模·杭州科技有限公司073 (73) | 9743 |", composed.text)
        self.assertEqual(refs.tables(), ("r1",))


class LiteralTextTests(unittest.TestCase):
    def setUp(self):
        self.refs = TurnReferences()
        self.refs.add(ReferenceTable((("name", "名称"), ("city", "城市")),
                                     (("合成规模·成都渠道客户118", "成都"), ("合成规模·深圳渠道客户099", "深圳")), 2, True))

    def test_numbers_must_come_from_the_person_or_a_reference(self):
        # The recorded Q121 answer listed a city's numbers the person never gave.
        composed = self.refs.compose("没有这个客户；重庆的编号只到 118、106", said=("有没有叫“合成远景·重庆渠道客户099”的",))
        self.assertIn("118", composed.problems[0])
        self.assertNotIn("099", composed.problems[0])
        self.assertEqual(self.refs.compose("名称里的099来自原话", said=("合成远景·重庆渠道客户099",)).problems, ())

    def test_a_copied_name_is_a_reference_and_short_words_are_free(self):
        self.assertFalse(any("写成引用" in p for p in self.refs.compose("名称以合成规模·深圳渠道客户开头").problems))
        problems = self.refs.compose("最接近的是 合成规模·深圳渠道客户099", said=("找客户",)).problems
        self.assertTrue(any("写成引用" in p for p in problems))
        self.assertEqual(self.refs.compose("在成都还是深圳？", said=()).problems, ())

    def test_dates_count_only_as_whole_dates_and_list_markers_are_not_values(self):
        said = ("2026-10-19", "2026年10月19日", "10月19日")
        self.assertEqual(self.refs.compose("截至2026-10-19：\n1. {r1#1.name}\n2. {r1#2.name}", said=said).problems, ())
        self.assertTrue(self.refs.compose("截至2026-10-19，共10条", said=said).problems)

    def test_the_persons_wan_and_yi_are_the_same_numbers(self):
        # The recorded Q092 follow-up said 100万 and the answer restated it as 1000000.
        said = ("金额超过100万的合同", "1.5亿以上")
        for text in ("超过1000000的合同", "超过1,000,000.00的合同", "超过 150000000 元"):
            self.assertEqual(self.refs.compose(text, said=said).problems, (), text)
        self.assertTrue(self.refs.compose("超过1000001的合同", said=said).problems)
        self.assertTrue(self.refs.compose("编号 000099", said=("编号 0099",)).problems)
        self.assertEqual(TurnReferences.withhold("超过1000000的有 3 条", said=said), "超过1000000的有 （未核实） 条")

    def test_a_malformed_reference_is_named(self):
        self.assertTrue(any("引用格式不对" in p for p in self.refs.compose("{ r1.name }").problems))
        self.assertTrue(any("没有列" in p for p in self.refs.compose("{r1#1.phone}").problems))

    def test_the_fallback_is_the_reads_and_without_reads_numbers_are_withheld(self):
        self.assertIn("合成规模·成都渠道客户118", self.refs.fallback())
        self.assertEqual(TurnReferences.withhold("1. 列表共 123 条\n2. 第 7 页", said=("翻到第7页",)),
                         "1. 列表共 （未核实） 条\n2. 第 7 页")

    def test_references_are_bounded_and_numbered_in_reading_order(self):
        refs = TurnReferences()
        for _ in range(14):
            refs.add(ReferenceTable((("n", "n"),), (("x",),), 1, True))
        self.assertNotIn("r1", refs)
        self.assertIn("r14", refs)
        with self.assertRaises(ValueError):
            ReferenceTable((("n", "n"), ("n", "m")), (), 0, True)


if __name__ == "__main__":
    unittest.main()
