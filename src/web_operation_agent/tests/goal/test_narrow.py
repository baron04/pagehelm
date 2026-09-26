"""Narrowing: hard references by shape, ranked entity summaries, the neighbourhood."""
from __future__ import annotations

import unittest

from web_operation_agent.goal.index import (ApplicationIndex, IndexEntity, IndexField, IndexPage,
                                             IndexRule)
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent.goal.narrow import (KnownReference, correct_records, locate, neighbourhood,
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

    def test_an_entity_the_request_names_joins_with_a_few_rules_and_no_neighbours(self):
        def rule(n, kind, about, subject=""):
            return IndexRule(id=f"r{n}", kind=kind, about=about, text=f"{about} {kind} {n}",
                             subject=subject)
        invoice = IndexEntity(key="invoice", label="Journal Entry", aliases=("发票",),
                              fields=(IndexField("partner", "Partner", "link", relation="partner"),),
                              rules=tuple(rule(n, "editability", f"Field {n}") for n in range(30))
                              + (rule(40, "visibility", "Request Cancel", "button_request_cancel"),))
        payment = IndexEntity(key="payment", label="Payments", aliases=("付款",),
                              rules=(rule(50, "visibility", "Request Cancel", "button_request_cancel"),))
        order = IndexEntity(key="order", label="Sales Order",
                            rules=(rule(60, "editability", "Order Lines"),
                                   rule(61, "editability", "Order Lines"),
                                   rule(62, "refusal", "action_confirm", "action_confirm"),
                                   rule(63, "meaning", "Expiration")))
        line = IndexEntity(key="line", label="Sales Order Line", aliases=("订单行",), lines_of="Sales Order")
        partner = IndexEntity(key="partner", label="Contact")
        index = ApplicationIndex(entities=(invoice, payment, order, line, partner),
                                 pages=(IndexPage(key=f"p{n}", label=f"Invoices {n}",
                                                  breadcrumb=f"Invoicing / {n}", entity="invoice")
                                        for n in range(5)))
        # The first step chose Payments; the request says 发票 (Journal Entry's alias)
        # and names the control: Journal Entry joins with its best rules, capped, and
        # without its own neighbour (Contact); its pages are a few, not all.
        near = neighbourhood(index, ("payment",), request="发票上的 Request Cancel 什么时候才出现？")
        self.assertEqual([e.key for e in near.entities], ["invoice", "payment"])
        joined = near.entity("invoice")
        self.assertEqual(joined.rules[0].about, "Request Cancel")
        self.assertLessEqual(len(joined.rules), 24)
        self.assertEqual(len(near.pages), 3)
        # The owner of chosen lines keeps its refusals and what its form says about
        # editing them, one entry per control or field; meanings stay out.
        near = neighbourhood(index, ("line",), request="订单行什么时候整块都不能编辑？")
        self.assertEqual({r.id for r in near.entity("order").rules}, {"r62", "r60"})
        # A one-word control label ("Expiration") never names an entity.
        near = neighbourhood(index, ("payment",), request="Expiration of what?")
        self.assertEqual([e.key for e in near.entities], ["payment"])

    def test_a_composed_record_number_is_replaced_by_the_located_one(self):
        known = (KnownReference(text="S00009", entity="order", name="S00009"),)
        spec = GoalSpec.from_json({"deliverables": [{"kind": "fact", "entity": "order", "record": "S00096",
                                                     "fields": ["Order Date"]}]})
        fixed, corrections = correct_records(spec, "S00009 的订单日期是哪一天？", known)
        self.assertEqual(fixed.deliverables[0].record, "S00009")
        self.assertEqual(corrections, (("S00096", "S00009"),))
        # A record the request wrote, a description, or no located reference: untouched.
        for record, request, refs in (("S00009", "S00009 的日期", known),
                                      ("the larger quotation", "S00009 的日期", known),
                                      ("S00096", "S00009 的日期", ())):
            spec = GoalSpec.from_json({"deliverables": [{"kind": "fact", "entity": "order", "record": record}]})
            self.assertEqual(correct_records(spec, request, refs)[1], ())
        # Two located records it could be: nothing is guessed.
        two = known + (KnownReference(text="S00010", entity="order", name="S00010"),)
        spec = GoalSpec.from_json({"deliverables": [{"kind": "fact", "entity": "order", "record": "S00096"}]})
        self.assertEqual(correct_records(spec, "S00009 和 S00010", two)[1], ())
        # A code copied from the values offered this turn was not composed: it stands,
        # even where the request names only the customer.
        partner = (KnownReference(text="Azure", entity="partner", name="Azure Interior"),)
        spec = GoalSpec.from_json({"deliverables": [{"kind": "handoff", "entity": "invoice",
                                                     "record": "RINV/2026/00002"}]})
        self.assertEqual(correct_records(spec, "Azure 那张两万的退款", partner,
                                         ("RINV/2026/00002",))[1], ())
        self.assertEqual(correct_records(spec, "Azure 那张两万的退款", partner)[1], ())

    def test_a_data_question_carries_no_rules(self):
        rules = (IndexRule(id="r1", kind="visibility", about="Request Cancel", text="t"),)
        index = ApplicationIndex(entities=(
            IndexEntity(key="invoice", label="Journal Entry", rules=rules),
            IndexEntity(key="payment", label="Payments", rules=rules)))
        near = neighbourhood(index, ("invoice",), request="Request Cancel", rules=False)
        self.assertEqual([e.key for e in near.entities], ["invoice"])
        self.assertEqual(near.entity("invoice").rules, ())
        self.assertNotIn("RULES", near.render())

    def test_the_catalogue_lists_one_entry_per_control_and_kind(self):
        rules = tuple(IndexRule(id=f"r{n}", kind="editability", about="Currency",
                                text=f"readonly when state {n}") for n in range(4))
        index = ApplicationIndex(entities=(IndexEntity(key="invoice", label="Journal Entry", rules=rules),))
        near = neighbourhood(index, ("invoice",), request="currency")
        self.assertEqual([r.id for r in near.entity("invoice").rules], ["r0"])


if __name__ == "__main__":
    unittest.main()
