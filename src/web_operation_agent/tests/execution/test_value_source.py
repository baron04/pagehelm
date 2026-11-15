"""A value the assistant typed must come from the person, a chosen candidate or a receipt."""
import unittest

from web_operation_agent.execution.value_source import (
    Entry, Receipt, SourceLedger, amounts_in, check_entries, cn_number, moments_in)


def verdict(entry, words, receipts=()):
    ledger = SourceLedger([entry], list(receipts))
    return check_entries(ledger, words)[0]


class ValueSourceTests(unittest.TestCase):
    def test_value_in_the_words_passes(self):
        got = verdict(Entry("name", "王小明"), ["给客户新增联系人王小明"])
        self.assertEqual((got.ok, got.source), (True, "user_text"))

    def test_spacing_and_punctuation_are_ignored(self):
        self.assertTrue(verdict(Entry("name", "Acme 科技"), ["客户叫 Acme科技。"]).ok)

    def test_spoken_date_and_time_match_the_form_value(self):
        words = ["把拜访安排在11月12号下午两点"]
        self.assertTrue(verdict(Entry("time", "2026-11-12 14:00:00", kind="datetime"), words).ok)
        self.assertFalse(verdict(Entry("time", "2026-11-12 10:00:00", kind="datetime"), words).ok)
        self.assertFalse(verdict(Entry("time", "2026-11-13 14:00:00", kind="datetime"), words).ok)

    def test_a_date_alone_does_not_license_a_time_of_day(self):
        self.assertFalse(verdict(Entry("time", "2026-11-12 09:30:00"), ["11月12号"]).ok)
        self.assertTrue(verdict(Entry("day", "2026-11-12"), ["11月12号"]).ok)

    def test_amounts_by_meaning(self):
        self.assertTrue(verdict(Entry("amount", "20000"), ["金额两万元"]).ok)
        self.assertTrue(verdict(Entry("amount", "3500"), ["报价3500"]).ok)
        self.assertTrue(verdict(Entry("amount", "12000"), ["1.2万"]).ok)
        self.assertFalse(verdict(Entry("amount", "9999"), ["金额两万元"]).ok)

    def test_a_chosen_candidate_passes_and_an_invented_value_does_not(self):
        self.assertEqual(verdict(Entry("customerId", "3", label="Acme", candidate=True), ["选客户"]).source,
                         "candidate")
        got = verdict(Entry("name", "李雷"), ["新增联系人"])
        self.assertEqual((got.ok, got.reason), (False, "not_from_the_person"))

    def test_receipt_for_the_same_field_only(self):
        receipts = [Receipt("start", "2026-11-13 09:00:00")]
        self.assertTrue(verdict(Entry("start", "2026-11-13 09:00:00"), ["明天上午开始"], receipts).ok)
        self.assertFalse(verdict(Entry("end", "2026-11-13 09:00:00"), ["明天上午开始"], receipts).ok)

    def test_last_entry_for_a_field_wins(self):
        ledger = SourceLedger()
        ledger.add(Entry("name", "编的名字"))
        ledger.add(Entry("name", "王小明"))
        self.assertTrue(all(v.ok for v in check_entries(ledger, ["联系人王小明"])))

    def test_chinese_numbers_and_clock(self):
        self.assertEqual([cn_number(t) for t in ("十二", "两百", "二千四百", "三万五")],
                         [12, 200, 2400, 35000])
        self.assertIn((11, 12, 14, 0), moments_in("11月12号下午两点"))
        self.assertIn((None, None, 9, 30), moments_in("上午九点半"))
        self.assertIn(20000, {int(v) for v in amounts_in("两万")})


class IntervalSourceTests(unittest.TestCase):
    def test_a_time_interval_inherits_its_preceding_period(self):
        from web_operation_agent.execution.value_source import Entry, user_supplied
        words = ["11月12号下午两点到三点"]
        self.assertTrue(user_supplied(Entry("开始", "2026-11-12 14:00:00"), words))
        self.assertTrue(user_supplied(Entry("结束", "2026-11-12 15:00:00"), words))
        self.assertFalse(user_supplied(Entry("结束", "2026-11-12 03:00:00"), words))
        self.assertFalse(user_supplied(Entry("开始", "2026-11-12 14:00:00"), ["11月12号下午"]))


if __name__ == "__main__":
    unittest.main()
