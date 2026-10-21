"""Narrowing: hard references by shape, ranked entity summaries, the neighbourhood."""
from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.index import (ApplicationIndex, IndexEntity, IndexField, IndexPage,
                                             IndexRule)
from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent.goal.resolve.resolver import Resolver
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent.goal.narrow import (KnownReference, candidate_entities, correct_records, locate, neighbourhood,
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

    def test_one_context_dependent_list_refusal_does_not_abort_reference_location(self):
        index = ApplicationIndex(entities=(IndexEntity("requires-parent", "Child"),
                                           IndexEntity("normal", "Normal")),
                                 pages=(IndexPage("/child", "Child", "Child", "requires-parent"),
                                        IndexPage("/normal", "Normal", "Normal", "normal")))

        class Data:
            def index(self, *, context=None):
                return index

        class Resolver:
            def __init__(self):
                self.asked = []

            def view(self, _entity):
                return SimpleNamespace(fields={})

            def _find_record(self, entity, _text):
                self.asked.append(entity)
                if entity == "requires-parent":
                    raise DataRefused("parent required")
                return None

        resolver = Resolver()
        port = DataGoalPort(Data())
        with patch.object(port, "_resolver", return_value=resolver):
            self.assertEqual(port.locate_reference("ABC123"), ())
            self.assertEqual(resolver.asked, ["requires-parent", "normal"])
            self.assertEqual(port.locate_names(("Alice",), entities=("requires-parent", "normal")), ())

    def test_context_dependent_related_list_does_not_hide_an_absent_record(self):
        index = ApplicationIndex(entities=(IndexEntity("customer", "Customer"),
                                           IndexEntity("statistics", "Statistics")))
        resolver = Resolver(None, index, None)
        link = SimpleNamespace(kind="link", relation="customer")
        view = SimpleNamespace(listed=True, shown=("customerId",),
                               fields={"customerId": link})
        with patch.object(resolver, "view", return_value=view), patch.object(
                resolver, "_find_record", side_effect=DataRefused("department required")):
            self.assertIsNone(resolver._rehome("customer", "Missing customer"))


class RankingTests(unittest.TestCase):
    def test_named_entities_first_then_mentioned_then_the_rest_nothing_dropped(self):
        index = _index()
        known = (KnownReference("S00006", "order", "S00006"),)
        ranked = ranked_entities(index, "S00006 的客户是谁，产品有哪些", known)
        self.assertEqual([e.key for e in ranked][:3], ["order", "partner", "product"])
        self.assertEqual(len(ranked), len(index.entities))
        text = summaries(index, "S00006 的客户是谁", known)
        self.assertTrue(text.startswith("ENTITIES"))
        self.assertIn("order: Sales Order [销售订单, 报价单] | named in request: S00006 | pages: Sales / Quotations", text)
        self.assertIn("line: Sales Order Line [订单行] (lines of Sales Order)", text)
        self.assertIn("KNOWN REFERENCES", text)
        self.assertLess(len(text), 1200)

    def test_scope_keeps_distinct_paths_controls_and_links_without_page_identifiers(self):
        index = _index()
        order = replace(index.entities[0], actions=("Confirm", "Send by Email"))
        pages = tuple(IndexPage(key=f"technical-page-{n}", label=path.split(" / ")[-1],
                                breadcrumb=path, entity="order")
                      for n, path in enumerate(("Sales / Quotations",) * 8
                                               + ("Sales / Orders",)))
        index = replace(index, entities=(order,) + index.entities[1:], pages=pages)
        text = summaries(index, "Confirm S00006 and count orders", ())
        self.assertIn("pages: Sales / Quotations; Sales / Orders", text)
        self.assertIn("controls: Confirm, Send by Email", text)
        self.assertIn("[销售订单, 报价单]", text)
        self.assertIn("(lines of Sales Order)", text)
        self.assertNotIn("technical-page-", text)
        # The next step retains exact page identities, including duplicate paths.
        detail = neighbourhood(index, ("order",), rules=False).render()
        for page in pages:
            self.assertIn(page.key, detail)
        explicit = summaries(index, "Open technical-page-8", ())
        self.assertIn("technical-page-8 Sales / Orders", explicit)

    def test_large_index_retrieves_named_pages_fields_references_and_conventions(self):
        from web_operation_agent.goal.index import IndexConvention
        filler = tuple(IndexEntity(key=f"misc/item-{n}", label=f"Generic Item {n}")
                       for n in range(300))
        target = IndexEntity(key="trade/order", label="交易订单",
                             fields=(IndexField("amount", "实付金额", "money"),))
        named = IndexEntity(key="crm/contract", label="合同")
        index = ApplicationIndex(
            entities=filler + (target, named),
            pages=(IndexPage(key="/mall/order", label="商城订单", breadcrumb="商城 / 订单",
                             entity="trade/order"),),
            conventions=(IndexConvention(terms=("成交",), meaning="已成交合同",
                                         entity="crm/contract"),))
        known = (KnownReference("C-100", "crm/contract", "C-100"),)
        selected = candidate_entities(index, "商城订单的实付金额和成交合同", known)
        self.assertIn("trade/order", [e.key for e in selected])
        self.assertIn("crm/contract", [e.key for e in selected])
        rendered = summaries(index, "商城订单的实付金额和成交合同", known)
        self.assertLessEqual(len(rendered), 12_500)
        self.assertIn("trade/order: 交易订单", rendered)
        self.assertIn("crm/contract: 合同", rendered)
        self.assertIn("OTHER ENTITIES", rendered)
        self.assertNotIn("misc/item-299: Generic Item 299 |", rendered)

    def test_the_neighbourhood_keeps_links_lines_and_owners_and_their_pages(self):
        index = _index()
        near = neighbourhood(index, ("line",))
        self.assertEqual({e.key for e in near.entities}, {"line", "order", "product"})
        self.assertEqual({p.key for p in near.pages}, {"p1"})
        wide = neighbourhood(index, ("order",))
        self.assertEqual({e.key for e in wide.entities}, {"order", "partner", "line"})
        self.assertNotIn("tag", [e.key for e in wide.entities])
        self.assertEqual(neighbourhood(index, ("nope",)).entities, ())

    def test_records_pointing_at_the_chosen_one_join_when_the_request_names_them_in_part(self):
        index = _index()
        # "Journal Entry" links to the contact; a request naming a word of its label
        # brings it in, one that does not leaves it out.
        self.assertIn("invoice", {e.key for e in neighbourhood(
            index, ("partner",), request="the journal of Acme").entities})
        self.assertNotIn("invoice", {e.key for e in neighbourhood(
            index, ("partner",), request="Acme's phone").entities})
        self.assertIn("invoice", {e.key for e in neighbourhood(
            replace(index, entities=tuple(replace(e, label="记账凭证") if e.key == "invoice" else e
                                          for e in index.entities)),
            ("partner",), request="客户的凭证有哪些").entities})

    def test_a_field_no_page_shows_is_listed_only_when_the_request_names_it(self):
        product = IndexEntity(key="product", label="Product", aliases=("产品",),
                              fields=(IndexField("price", "Sales Price", "money"),),
                              held=(IndexField("weight", "Weight", "number", aliases=("重量",)),
                                    IndexField("volume", "Volume", "number", aliases=("体积",))))
        index = ApplicationIndex(entities=(product,), pages=(), source_digest="d")
        # The full index never lists them.
        self.assertNotIn("Weight", index.render())
        # Only asked for: an ordinary neighbourhood carries none.
        self.assertEqual(neighbourhood(index, ("product",),
                                       request="重量超过 1 公斤的产品是哪几个？").entity("product").held, ())
        named = neighbourhood(index, ("product",), request="重量超过 1 公斤的产品是哪几个？", held=True)
        self.assertEqual([f.key for f in named.entity("product").held], ["weight"])
        self.assertIn("held, not shown on its pages: Weight[重量](number)", named.render())
        # A technical name written as a word names the field too.
        self.assertEqual([f.key for f in neighbourhood(index, ("product",), held=True,
                          request="Product 的 volume 是多少").entity("product").held], ["volume"])
        # A request naming none of them carries none.
        plain = neighbourhood(index, ("product",), request="产品有几个？", held=True)
        self.assertEqual(plain.entity("product").held, ())
        self.assertNotIn("held", plain.render())

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

    def test_explicit_code_adjacent_to_chinese_is_never_replaced_by_another_target(self):
        known = (KnownReference(text="DOC-1001", entity="invoice", name="DOC-1001"),)
        spec = GoalSpec.from_json({"deliverables": [
            {"kind":"fact","entity":"invoice","record":"DOC-9999","fields":["Amount"]},
            {"kind":"fact","entity":"invoice","record":"DOC-1001","fields":["Amount"]}]})
        for request in ("告诉我DOC-9999的金额和DOC-1001的金额", "查询doc-9999的金额；以及DOC-1001"):
            fixed, corrections = correct_records(spec, request, known)
            self.assertEqual(fixed, spec)
            self.assertEqual(corrections, ())
        # An embedded identifier or prefix of a different code is not the exact target.
        for request in ("查询DOC-99990，另查DOC-1001", "prefixDOC-9999的金额，另查DOC-1001"):
            self.assertEqual(correct_records(spec, request, known)[1], (("DOC-9999","DOC-1001"),))

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


class NameLocationTests(unittest.TestCase):
    def test_names_go_to_the_port_that_locates_names_and_fall_back_to_references(self):
        from web_operation_agent.goal.narrow import locate_names

        class _Names:
            def locate_names(self, names, *, entities=(), context=None):
                assert entities == ("order",)
                return tuple((n, "partner", n + " Ltd") for n in names if n == "Acme")

        found = locate_names(_Names(), ("Acme", "Nobody"), entities=("order",))
        self.assertEqual(found, (KnownReference("Acme", "partner", "Acme Ltd"),))

        class _References:
            def locate_reference(self, text, *, context=None):
                return (("order", text),) if text == "S00004" else ()

        self.assertEqual(locate_names(_References(), ("S00004",)),
                         (KnownReference("S00004", "order", "S00004"),))
class ConventionTests(unittest.TestCase):
    """An organisation's meaning of its words reaches the model only when the request
    says one of them."""

    def setUp(self):
        from web_operation_agent.goal.index import IndexConvention
        self.orders = IndexConvention(terms=("订单", "单子"), meaning="order in every Status, quotations included")
        self.quotes = IndexConvention(terms=("报价单",), meaning="order with Status in Quotation")
        self.index = ApplicationIndex(
            entities=(IndexEntity(key="order", label="Order"),),
            pages=(IndexPage(key="p1", label="Orders", breadcrumb="Sales / Orders", entity="order"),),
            conventions=(self.orders, self.quotes))

    def test_a_convention_has_terms_and_one_short_meaning(self):
        from web_operation_agent.goal.index import IndexConvention
        with self.assertRaises(ValueError):
            IndexConvention(terms=(" ",), meaning="x")
        with self.assertRaises(ValueError):
            IndexConvention(terms=("订单",), meaning="x" * 301)
        self.assertEqual(IndexConvention(terms=(" 订单 ", "订单"), meaning=" a  b ").terms, ("订单",))

    def test_only_the_conventions_the_request_says_are_shown(self):
        focused = neighbourhood(self.index, ("order",), request="Joel 的订单有几张？")
        self.assertEqual(focused.conventions, (self.orders,))
        rendered = focused.render()
        self.assertIn("CONVENTIONS of this organisation", rendered)
        self.assertIn("订单 / 单子: order in every Status, quotations included", rendered)
        self.assertNotIn("报价单", rendered)
        scope = summaries(self.index, "Acme 的报价单", ())
        self.assertIn("报价单: order with Status in Quotation", scope)
        self.assertNotIn("CONVENTIONS", summaries(self.index, "S00006 的客户是谁？", ()))


if __name__ == "__main__":
    unittest.main()
