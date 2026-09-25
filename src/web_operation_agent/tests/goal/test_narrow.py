"""Narrowing: hard references by shape, ranked entity summaries, the neighbourhood."""
from __future__ import annotations

import unittest

from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexField, IndexPage
from web_operation_agent.goal.narrow import (KnownReference, locate, neighbourhood,
                                              ranked_entities, references, summaries)


def _index() -> ApplicationIndex:
    return ApplicationIndex(
        entities=(
            IndexEntity(key="order", label="Sales Order", aliases=("销售订单", "报价单"),
                        fields=(IndexField("partner", "Customer", "link", relation="partner"),
                                IndexField("total", "Total", "money"))),
            IndexEntity(key="line", label="Sales Order Line", aliases=("订单行",),
                        lines_of="Sales Order",
                        fields=(IndexField("product", "Product", "link", relation="product"),)),
            IndexEntity(key="partner", label="Contact", aliases=("客户",)),
            IndexEntity(key="product", label="Product", aliases=("产品",)),
            IndexEntity(key="invoice", label="Journal Entry", aliases=("发票",),
                        fields=(IndexField("partner", "Partner", "link", relation="partner"),)),
            IndexEntity(key="tag", label="Product Tag"),
        ),
        pages=(IndexPage(key="p1", label="Quotations", breadcrumb="Sales / Quotations", entity="order"),
               IndexPage(key="p2", label="Customers", breadcrumb="Sales / Customers", entity="partner"),
               IndexPage(key="p3", label="Invoices", breadcrumb="Invoicing / Invoices", entity="invoice"),
               IndexPage(key="p4", label="Tags", breadcrumb="Config / Tags", entity="tag")),
        source_digest="d")


class _Port:
    def __init__(self, table):
        self.table = table
        self.asked: list[str] = []

    def locate_reference(self, text, *, context=None):
        self.asked.append(text)
        return self.table.get(text, ())


class ReferencesTests(unittest.TestCase):
    def test_codes_names_amounts_and_dates_are_cut_by_shape(self):
        self.assertEqual(references("INV/2026/00002 现在是什么状态？"), ("INV/2026/00002",))
        self.assertEqual(references("发给 Joel Willis 的那份报价，我要确认下来。"), ("Joel Willis",))
        self.assertEqual(references("Acme 那份 1740 美元的草稿报价"), ("1740", "Acme"))
        self.assertEqual(references("2026-08-22 的订单和 S00011"), ("S00011", "2026-08-22"))
        self.assertEqual(references("哪些客户在美国？"), ())

    def test_a_reference_inside_a_code_is_not_reported_twice(self):
        self.assertEqual(references("去年的 BILL/2025/06/0001，付款参考改成 PO-2025-0617"),
                         ("BILL/2025/06/0001", "PO-2025-0617"))


class LocateTests(unittest.TestCase):
    def test_the_adapter_is_asked_for_each_reference_and_answers_settle_entities(self):
        port = _Port({"S00006": (("order", "S00006"),), "Joel Willis": (("partner", "Joel Willis"),)})
        known = locate(port, "S00006 的客户是不是 Joel Willis？")
        self.assertEqual([(k.text, k.entity, k.name) for k in known],
                         [("S00006", "order", "S00006"), ("Joel Willis", "partner", "Joel Willis")])
        self.assertEqual(port.asked, ["S00006", "Joel Willis"])

    def test_a_port_without_the_method_contributes_nothing(self):
        self.assertEqual(locate(object(), "S00006"), ())


class RankingTests(unittest.TestCase):
    def test_named_entities_first_then_mentioned_then_the_rest_nothing_dropped(self):
        index = _index()
        known = (KnownReference("S00006", "order", "S00006"),)
        ranked = ranked_entities(index, "S00006 的客户是谁，产品有哪些", known)
        self.assertEqual([e.key for e in ranked][:3], ["order", "partner", "product"])
        self.assertEqual(len(ranked), len(index.entities))
        text = summaries(index, "S00006 的客户是谁", known)
        self.assertTrue(text.startswith("ENTITIES"))
        self.assertIn("order: Sales Order [销售订单, 报价单] | named in request: S00006 | pages: p1 Sales / Quotations", text)
        self.assertIn("line: Sales Order Line [订单行] (lines of Sales Order)", text)
        self.assertIn("KNOWN REFERENCES", text)
        self.assertLess(len(text), 1200)

    def test_the_neighbourhood_keeps_links_lines_and_owners_and_their_pages(self):
        index = _index()
        near = neighbourhood(index, ("line",))
        self.assertEqual({e.key for e in near.entities}, {"line", "order", "product"})
        self.assertEqual({p.key for p in near.pages}, {"p1"})
        wide = neighbourhood(index, ("order",))
        self.assertEqual({e.key for e in wide.entities}, {"order", "partner", "line"})
        self.assertNotIn("tag", [e.key for e in wide.entities])
        self.assertEqual(neighbourhood(index, ("nope",)).entities, ())


if __name__ == "__main__":
    unittest.main()
