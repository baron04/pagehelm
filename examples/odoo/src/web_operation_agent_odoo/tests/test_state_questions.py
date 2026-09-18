"""A question about a record's state, or a request to check one, is not a request to change it
(P7.5 CR-13).

The same class as CR-06, on the other side of the verb. "S00010 开票了吗？" asks whether the
order has been invoiced, and "开过票吗" whether it ever was; both contain the invoice Skill's
trigger "开票". "确认" also means "check": "请确认报价单 S00007 是否已发送" and "确认一下报价单
S00007 的金额是多少" ask to verify a fact about the quotation, not to confirm it. Each was taken
for an explicit write request, and the session refuses every read-only answer to one.
"""
from __future__ import annotations

import unittest

from web_operation_agent.business import trigger_requests_action


class AStateQuestionIsNotARequest(unittest.TestCase):
    def test_an_aspect_marker_after_the_verb_asks_about_what_happened(self):
        for trigger, task in (("开票", "S00010 开票了吗？"), ("开票", "S00010 开过票吗？"),
                              ("开票", "S00010 开票了没有？"), ("开票", "这张订单开过票没有"),
                              ("确认报价单", "S00007 这张确认报价单了吗？"),
                              ("创建客户", "P75 Foo 创建过客户吗？")):
            with self.subTest(task=task):
                self.assertFalse(trigger_requests_action(trigger, task))

    def test_checking_a_fact_about_the_record_is_not_changing_it(self):
        for task in ("请确认报价单 S00007 是否已发送", "帮我确认一下报价单 S00007 的金额是多少？",
                     "确认报价单 S00007 的客户是谁", "确认一下报价单 S00007 的状态是什么"):
            with self.subTest(task=task):
                self.assertFalse(trigger_requests_action("确认报价单", task))

    def test_the_requests_stay_requests(self):
        for trigger, task in (("确认报价单", "确认报价单 S00007"),
                              ("确认报价单", "能帮我确认报价单 S00007 吗？"),
                              ("确认报价单", "帮我确认一下报价单 S00007"),
                              ("确认报价单", "确认报价单 S00007，然后告诉我它的金额是多少"),
                              ("开票", "给销售订单 S00010 开票"),
                              ("开票", "S00010 开票吧"),
                              ("创建客户", "创建客户 P75 Foo，邮箱 foo@example.test")):
            with self.subTest(task=task):
                self.assertTrue(trigger_requests_action(trigger, task))


if __name__ == "__main__":
    unittest.main()
