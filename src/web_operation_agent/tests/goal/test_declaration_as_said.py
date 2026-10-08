"""Spellings of a declaration that mean something it has a proper place for."""
from types import SimpleNamespace
import unittest

from web_operation_agent.goal.executor import (_as_said, _coverage_gaps, _moved_from_named_kind,
                                              _repairable)
from web_operation_agent.goal.coverage import cleared_fields
from web_operation_agent.goal.index import ApplicationIndex, IndexConvention, IndexEntity, IndexField
from web_operation_agent.goal.ports import Resolution, Resolved
from web_operation_agent.goal.spec import GoalSpec

INDEX = ApplicationIndex(entities=(
    IndexEntity(key="crm/customer", label="客户", fields=(
        IndexField(key="telephone", label="电话", kind="text"),
        IndexField(key="remark", label="备注", kind="text", aliases=("说明",)),
        IndexField(key="email", label="邮箱", kind="text"))),
    IndexEntity(key="crm/business", label="商机", fields=(
        IndexField(key="totalPrice", label="商机总金额", kind="money"),)),
    IndexEntity(key="erp/sale-order", label="销售订单"),
    IndexEntity(key="trade/order", label="交易订单"),
    IndexEntity(key="system/user", label="用户")),
    conventions=(IndexConvention(terms=("订单",), meaning="销售订单", entity="erp/sale-order"),))


def spec(*deliverables, write=False):
    return GoalSpec.from_json({"write_intent": write, "deliverables": list(deliverables)})


class AsSaid(unittest.TestCase):
    def one(self, deliverable, text, write=False):
        out, changed = _as_said(spec(deliverable, write=write), text, INDEX)
        return out.deliverables[0], changed

    def test_an_ordered_list_asked_as_the_highest_is_the_extreme_with_its_ties(self):
        listed = {"kind": "list", "entity": "crm/business", "fields": ["商机名称"], "sort": "商机总金额",
                  "descending": True}
        d, changed = self.one(listed, "金额最高的商机是哪几笔")
        self.assertEqual((changed, d.sort, d.metric.op, d.metric.field), (("extreme",), "", "max", "商机总金额"))
        self.assertEqual(d.kind, "list")
        d, _ = self.one({**listed, "descending": False}, "金额最低的商机")
        self.assertEqual(d.metric.op, "min")
        # Asked as a first so-many, ordered the other way round, limited, or not asked as an extreme.
        for other, text in ((listed, "金额最高的前3个商机"), ({**listed, "descending": False}, "金额最高的商机"),
                            ({**listed, "limit": 5}, "金额最高的商机"), (listed, "把商机按金额从大到小排一下")):
            self.assertEqual(self.one(other, text)[1], ())

    def test_a_new_record_declared_with_conditions_starts_with_them_as_values(self):
        created = {"kind": "handoff", "entity": "crm/customer", "action": "create",
                   "metric": {"op": "count", "filters": [{"field": "邮箱", "values": ["a@x.invalid"]}]}}
        d, changed = self.one(created, "新增客户，邮箱a@x.invalid", write=True)
        self.assertEqual((changed, d.field_values, d.metric), (("starting values",), (("邮箱", "a@x.invalid"),), None))
        ranged = {**created, "metric": {"op": "count", "filters": [{"field": "邮箱", "values": ["a", "b"]}]}}
        self.assertEqual(self.one(ranged, "新增客户", write=True)[1], ())
        existing = {**created, "action": "", "record": "晨岚机电"}
        self.assertEqual(self.one(existing, "改一下晨岚机电", write=True)[1], ())

    def test_a_field_the_request_clears_is_that_field_with_an_empty_value(self):
        for text in ("清空晨岚机电的电话", "把它的电话清空", "clear Ada's 电话"):
            with self.subTest(text):
                d, changed = self.one({"kind": "handoff", "entity": "crm/customer", "record": "晨岚机电",
                                       "fields": ["电话"]}, text, write=True)
                self.assertEqual((changed, d.field_values, d.fields), (("cleared",), (("电话", ""),), ()))
        # A field merely lacking a value is still asked for; so is one the request says not to clear.
        for text in ("给晨岚机电补个电话", "不要清空晨岚机电的电话"):
            self.assertEqual(self.one({"kind": "handoff", "entity": "crm/customer", "record": "晨岚机电",
                                       "fields": ["电话"]}, text, write=True)[1], ())
        self.assertEqual(cleared_fields("清空晨岚机电的说明", INDEX.entity("crm/customer")), {"说明"})


class Repairs(unittest.TestCase):
    def state(self, text):
        return SimpleNamespace(full_index=INDEX, index=INDEX, message=SimpleNamespace(text=text))

    def moved(self, text, before, after):
        return _moved_from_named_kind(
            self.state(text), spec({"kind": "fact", "entity": before, "record": "X", "fields": ["a"]}),
            spec({"kind": "fact", "entity": after, "record": "X", "fields": ["a"]}))

    def test_a_number_found_in_another_kind_the_same_word_names_may_be_answered_from_it(self):
        self.assertFalse(self.moved("订单SC20261003005实付了多少钱", "erp/sale-order", "trade/order"))
        # The person's word names the first kind and nothing of the second: still held to it.
        self.assertTrue(self.moved("这个客户陈牧的电话", "crm/customer", "system/user"))

    def test_a_refused_read_is_said_to_the_declarer_and_a_denied_one_is_not(self):
        declared = spec({"kind": "fact", "entity": "wms/inventory", "fields": ["库存数量"]})
        refused = Resolution(deliverables=(Resolved(kind="fact", problem="refused", read_failure="refused",
                                                    refused="统计维度不能为空"),))
        problems = _repairable(declared, refused)
        self.assertEqual(len(problems), 1)
        self.assertIn("refused to read wms/inventory", problems[0])
        self.assertIn("统计维度不能为空", problems[0])
        denied = Resolution(deliverables=(Resolved(kind="fact", problem="x", read_failure="refused",
                                                   denied="read"),))
        self.assertEqual(_repairable(declared, denied), ())

    def test_a_named_records_tab_delivers_a_list_asked_of_that_entity(self):
        parts = [{"kind": "list", "entity": "crm/customer", "about": "操作记录"}]
        tab = spec({"kind": "page", "entity": "crm/customer", "record": "星泊物流", "section": "操作日志"})
        self.assertEqual(_coverage_gaps(tab, parts, "在页面上查看星泊物流的客户操作记录", "2026-10-03"), ())
        plain = spec({"kind": "page", "entity": "crm/customer", "record": "星泊物流"})
        self.assertEqual(len(_coverage_gaps(plain, parts, "在页面上查看星泊物流的客户操作记录", "2026-10-03")), 1)


if __name__ == "__main__":
    unittest.main()
