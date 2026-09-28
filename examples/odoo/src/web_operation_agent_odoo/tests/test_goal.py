"""The Odoo goal port over a scripted JSON-RPC client: index and resolution, no model."""
from __future__ import annotations

from dataclasses import replace
import unittest

from web_operation_agent.goal.spec import Deliverable, GoalSpec
from web_operation_agent_odoo.goal import OdooGoalPort
from web_operation_agent_odoo.translations import OdooTranslations, parse_po

_CATALOG = {
    "descriptor_digest": "d1",
    "models": [
        {"model": "sale.order", "label": "Sales Order", "operations": ["read"], "transient": False},
        {"model": "res.partner", "label": "Contact", "operations": ["create", "read"],
         "transient": False},
        {"model": "product.tag", "label": "Product Tag", "operations": ["read"], "transient": False},
        {"model": "sale.order.line", "label": "Sales Order Line", "operations": ["read"],
         "transient": False},
        {"model": "mail.compose.message", "label": "Compose", "operations": ["read"],
         "transient": True},
    ],
    "actions": [
        {"action_id": 331, "xml_id": "sale.action_orders", "name": "Sales Orders",
         "route": "orders", "model": "sale.order", "readable": True,
         "view_modes": ["list", "kanban", "form"], "breadcrumbs": ["Sales", "Orders", "Orders"],
         "domain": "[('state', 'not in', ('draft', 'sent', 'cancel'))]", "context": "{}"},
        {"action_id": 332, "xml_id": "sale.action_quotations", "name": "Quotations",
         "route": "sales", "model": "sale.order", "readable": True,
         "view_modes": ["list", "kanban", "form"],
         "breadcrumbs": ["Sales", "Orders", "Quotations"], "domain": "",
         "context": "{'search_default_my_quotation': 1}"},
        {"action_id": 301, "xml_id": "account.res_partner_action_customer", "name": "Customers",
         "route": "customers", "model": "res.partner", "readable": True,
         "view_modes": ["kanban", "list", "form"],
         "breadcrumbs": ["Sales", "Orders", "Customers"], "domain": "", "context": "{}"},
        {"action_id": 173, "xml_id": "product.product_tag_action", "name": "Product Tags",
         "route": "action-product.product_tag_action", "model": "product.tag",
         "readable": True, "view_modes": ["list", "form"], "breadcrumbs": [],
         "domain": "", "context": "{}"},
        {"action_id": 105, "xml_id": "mail.action_email_compose_message_wizard",
         "name": "Compose Email", "route": "action-mail.x", "model": "mail.compose.message",
         "readable": True, "view_modes": ["form"], "breadcrumbs": [], "domain": "",
         "context": "{}"},
        {"action_id": 66, "xml_id": "base.act_view_currency_rates", "name": "Rates",
         "route": "action-base.rates", "model": "res.currency.rate", "readable": True,
         "view_modes": ["list"], "breadcrumbs": [],
         "domain": "[('currency_id','=', active_id)]", "context": "{}"},
    ],
}

_FIELDS = {
    "sale.order": {
        "name": {"string": "Order Reference", "type": "char"},
        "partner_id": {"string": "Customer", "type": "many2one", "relation": "res.partner"},
        "amount_total": {"string": "Total", "type": "monetary"},
        "currency_id": {"string": "Currency", "type": "many2one", "relation": "res.currency"},
        "state": {"string": "Status", "type": "selection",
                  "selection": [["draft", "Quotation"], ["sent", "Quotation Sent"],
                                ["sale", "Sales Order"], ["cancel", "Cancelled"]]},
        "user_id": {"string": "Salesperson", "type": "many2one", "relation": "res.users"},
        "message_ids": {"string": "Messages", "type": "one2many"},
        "payment_term_id": {"string": "Payment Terms", "type": "many2one",
                            "relation": "account.payment.term"},
        "date_order": {"string": "Order Date", "type": "datetime", "searchable": True},
        "order_line": {"string": "Order Lines", "type": "one2many", "relation": "sale.order.line"},
        "product_variant_count": {"string": "# Product Variants", "type": "integer",
                                  "searchable": False},
    },
    "sale.order.line": {
        "order_id": {"string": "Order Reference", "type": "many2one", "relation": "sale.order"},
        "product_id": {"string": "Product", "type": "many2one", "relation": "product.product"},
        "product_uom_qty": {"string": "Quantity", "type": "float"},
        "price_subtotal": {"string": "Subtotal", "type": "monetary"},
        "currency_id": {"string": "Currency", "type": "many2one", "relation": "res.currency"},
        "sequence": {"string": "Sequence", "type": "integer"},
    },
    "res.partner": {
        "name": {"string": "Name", "type": "char"},
        "comment": {"string": "Notes", "type": "html"},
        "email": {"string": "Email", "type": "char"},
        "parent_id": {"string": "Related Company", "type": "many2one", "relation": "res.partner"},
        "function": {"string": "Job Position", "type": "char"},
    },
    "product.tag": {"name": {"string": "Name", "type": "char"}},
    "account.payment.term": {"name": {"string": "Name", "type": "char"}},
}

_VIEWS = {
    "sale.order": {
        "list": '<list><field name="name"/><field name="partner_id"/><field name="amount_total"/>'
                '<field name="state"/><field name="message_ids"/></list>',
        "form": '<form><header><button name="action_confirm" type="object" string="Confirm"/>'
                '<button name="action_quotation_send" string="Send by Email" type="object"/>'
                '</header><field name="user_id"/><field name="payment_term_id"/>'
                '<notebook><page string="Order Lines"><field name="order_line"><list>'
                '<field name="sequence"/><field name="product_id"/><field name="product_uom_qty"/>'
                '<field name="price_subtotal"/></list></field></page>'
                '<page string="Other Info"><field name="date_order"/></page></notebook>'
                '</form>',
        "search": '<search><filter string="My Orders" name="my" domain="[(\'user_id\', \'=\', uid)]"/>'
                  '<filter string="Quotations" name="draft" domain="[(\'state\', \'in\', (\'draft\', \'sent\'))]"/>'
                  '<filter string="Late" name="late" domain="[(\'date_order\', \'&lt;\', context_today().strftime(\'%Y-%m-%d\'))]"/>'
                  '<filter string="Salesperson" name="salesperson" context="{\'group_by\': \'user_id\'}"/>'
                  '</search>'},
    "sale.order.line": {"list": "<list/>", "form": "<form/>", "search": "<search/>"},
    "res.partner": {
        "list": '<list><field name="name"/><field name="email"/><field name="parent_id"/>'
                '<field name="function"/></list>',
        "form": '<form><notebook><page string="Contacts &amp; Addresses"/>'
                '<page string="Internal Notes"><field name="comment"/></page></notebook></form>',
        "search": '<search/>'},
    "product.tag": {"list": '<list><field name="name"/></list>', "form": "<form/>",
                    "search": "<search/>"},
    "account.payment.term": {"list": "<list/>", "form": "<form/>", "search": "<search/>"},
}


class _Client:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.domains: list[list] = []

    @staticmethod
    def time_zone():
        return "Asia/Shanghai"

    def call(self, model, method, args, kwargs=None):
        self.calls.append((model, method))
        kwargs = kwargs or {}
        if method in ("search_count", "read_group", "search_read"):
            self.domains.append(args[0])
        if method == "catalog":
            return _CATALOG
        if method == "fields_get":
            return _FIELDS[model]
        if method == "get_views":
            views = {kind: {"arch": _VIEWS[model][kind]} for _id, kind in args[0]}
            if kwargs.get("options", {}).get("action_id") == 332:
                views["search"] = {"arch": '<search><filter string="My Quotations" name="my_quotation"'
                                           ' domain="[(\'user_id\', \'=\', uid)]"/></search>'}
            return {"views": views}
        if method == "name_search":
            query = args[0]
            table = {"sale.order": [[6, "S00006"], [7, "S00007"]],
                     "res.partner": [[11, "Gemini Furniture"], [21, "Gemini Furniture, Ed"]],
                     "account.payment.term": [[6, "End of Following Month"], [7, "Immediate Payment"]],
                     "product.tag": []}
            return [row for row in table.get(model, []) if query.casefold() in row[1].casefold()]
        if method == "read":
            if model == "sale.order":
                return [{"id": 6, "partner_id": [11, "Gemini Furniture"],
                         "amount_total": 2947.5, "currency_id": [1, "USD"], "state": "sale",
                         "payment_term_id": [6, "End of Following Month"], "order_line": [13]}]
            if model == "sale.order.line":
                return [{"id": 13, "display_name": "S00006 - Desk"}]
            return [{"id": 11, "comment": "<p>VIP &amp; <b>friendly</b></p>", "email": False}]
        if method == "search_count":
            domain = args[0]
            if ["display_name", "ilike", "Joel"] in domain:
                return 0
            if any(clause == ["id", "=", 6] for clause in domain if isinstance(clause, list)):
                return 1 if ("state", "not in", ("draft", "sent", "cancel")) in [
                    tuple(c) if isinstance(c, list) else c for c in domain] else 1
            return 13
        if method == "read_group":
            if args[2]:
                return [{"partner_id": [11, "Gemini Furniture"], "__count": 13},
                        {"partner_id": [16, "Lumber Inc"], "__count": 1}]
            return [{str(args[1][0]).split(":")[0]: 92750.0}]
        if method == "search_read":
            if model == "sale.order.line":
                return [{"id": 13, "display_name": "S00006 - Desk", "product_id": [12, "Desk"],
                         "product_uom_qty": 1.0, "price_subtotal": 750.0, "currency_id": [1, "USD"]}]
            return [{"id": 6, "display_name": "S00006", "currency_id": [1, "USD"],
                     "amount_total": 2947.5, "date_order": "2026-08-12 13:50:00"}]
        raise AssertionError(f"unexpected {model}.{method}")


#: What the application's own zh_CN catalogue says for a few of the fixture's labels.
_TRANSLATIONS = OdooTranslations(
    labels={"Sales Orders": ("销售订单",), "Sales Order Line": ("订单行",),
            "Customer": ("客户",), "Quotation Sent": ("报价已发送",)},
    english={"销售订单": ("Sales Orders",), "订单行": ("Sales Order Line",),
             "客户": ("Customer",), "报价已发送": ("Quotation Sent",)})


class OdooGoalPortTests(unittest.TestCase):
    def setUp(self):
        self.client = _Client()
        self.port = OdooGoalPort(self.client, translations=_TRANSLATIONS)
        self.index = self.port.index()

    def test_the_index_keeps_business_pages_and_drops_technical_and_bound_ones(self):
        keys = [p.key for p in self.index.pages]
        self.assertEqual(keys, ["p301", "p331", "p332", "p173"])
        orders = self.index.page("p331")
        self.assertEqual(orders.breadcrumb, "Sales / Orders / Orders")
        self.assertEqual(orders.groups, ("Salesperson",))
        self.assertIn("Quotations", orders.filters)
        self.assertEqual(self.index.page("p332").filters, ("My Quotations",))
        self.assertIn("销售订单", self.index.page("p331").aliases)
        entity = self.index.entity("sale.order")
        labels = [f.label for f in entity.fields]
        # Links and choices first, then amounts, dates and text: what people ask about.
        self.assertEqual(labels[:5], ["Customer", "Salesperson", "Payment Terms", "Status", "Total"])
        self.assertNotIn("Messages", labels)
        self.assertEqual(entity.sections, ("Order Lines", "Other Info"))
        self.assertEqual(entity.actions, ("Confirm", "Send by Email"))
        self.assertEqual(self.index.entity("product.tag").fields, ())
        self.assertEqual(self.index.page("p301").operations, ("create",))
        self.assertEqual(self.index.page("p331").operations, ())
        self.assertIs(self.port.index(), self.index)

    def test_a_column_labelled_as_another_field_indexes_that_field_too(self):
        from web_operation_agent_odoo.goal.text import _ModelMeta
        fields = {"amount_signed": {"string": "Total Signed", "type": "monetary"},
                  "amount_total": {"string": "Total", "type": "monetary"},
                  "helper": {"string": "Total (Tax inc.)", "type": "monetary"}}
        meta = _ModelMeta("m", "Entries", fields, ["amount_signed"], ["helper"], (), (), [],
                          list_arch='<list><field name="amount_signed" string="Total"/></list>')
        labels = [f.label for f in self.port._index_fields(meta)]
        self.assertEqual(labels, ["Total Signed", "Total", "Total (Tax inc.)"])

    def test_create_handoff_uses_actor_allowed_page_not_proposed_record_lookup(self):
        self.client.calls.clear()
        result = self._resolve({"kind": "handoff", "entity": "res.partner",
                                "record": "Test Co", "page": {"key": "p301"},
                                "action": "create"}, write_intent=True)
        page = result.deliverables[0].page
        self.assertIsNotNone(page)
        self.assertEqual((page.url, page.highlight), ("/odoo/customers", "New"))
        self.assertNotIn(("res.partner", "name_search"), self.client.calls)
        unavailable = self._resolve({"kind": "handoff", "entity": "sale.order",
                                     "page": {"key": "p331"}, "action": "create"},
                                    write_intent=True)
        self.assertIn("not available", unavailable.deliverables[0].problem)

    def _resolve(self, *deliverables, write_intent=False):
        return self.port.resolve(GoalSpec.from_json(
            {"deliverables": list(deliverables), "write_intent": write_intent}))

    def test_a_fact_reads_the_labelled_fields_and_shows_them_as_the_page_would(self):
        result = self._resolve({"kind": "fact", "entity": "sale.order", "record": "S00006",
                                "fields": ["Customer", "Total", "Status", "Weight"]})
        facts = result.deliverables[0].facts
        self.assertEqual([(f.label, f.value) for f in facts],
                         [("Customer", "Gemini Furniture"), ("Total", "2,947.50 USD"),
                          ("Status", "Sales Order")])
        self.assertEqual(result.deliverables[0].problem, "not on this record: Weight")
        self.assertIn(("read", "sale.order"), result.reads)

    def test_a_linked_record_page_follows_the_link_field(self):
        result = self._resolve({"kind": "page", "entity": "sale.order", "record": "S00006",
                                "via": "Customer", "section": "Internal Notes"})
        page = result.deliverables[0].page
        self.assertEqual(page.url, "/odoo/customers/11")
        self.assertEqual(page.section, "Internal Notes")
        self.assertEqual(page.label, "Gemini Furniture · Internal Notes")

    def test_a_record_page_picks_the_action_whose_domain_admits_the_record(self):
        result = self._resolve({"kind": "page", "entity": "sale.order", "record": "S00006",
                                "section": "other info"})
        self.assertEqual(result.deliverables[0].page.url, "/odoo/orders/6")
        self.assertEqual(result.deliverables[0].page.section, "Other Info")

    def test_a_linked_record_without_a_page_uses_the_model_route(self):
        result = self._resolve({"kind": "page", "entity": "sale.order", "record": "S00006",
                                "via": "Payment Terms"})
        self.assertEqual(result.deliverables[0].page.url, "/odoo/account.payment.term/6")
        self.assertEqual(result.deliverables[0].page.label, "End of Following Month")

    def test_ambiguity_and_absence_are_reported_not_guessed(self):
        result = self._resolve({"kind": "fact", "entity": "sale.order", "record": "S0000",
                                "fields": ["Customer"]})
        self.assertIn("ambiguous", result.deliverables[0].problem)
        self.assertEqual(result.deliverables[0].candidates, ("S00006", "S00007"))
        result = self._resolve({"kind": "fact", "entity": "product.tag", "record": "Blue",
                                "fields": ["Name"]})
        self.assertIn("no Product Tag matches", result.deliverables[0].problem)
        result = self._resolve({"kind": "fact", "entity": "res.partner",
                                "record": "Gemini Furniture", "fields": ["Notes", "Email"]})
        self.assertEqual([f.value for f in result.deliverables[0].facts],
                         ["VIP & friendly", "(empty)"])

    def test_the_deployments_conventions_reach_the_index(self):
        from web_operation_agent_odoo.conventions import REFERENCE_CONVENTIONS, load_conventions
        conventions = load_conventions(REFERENCE_CONVENTIONS)
        self.assertTrue(any("订单" in c.terms for c in conventions))
        self.assertEqual(self.port.index().conventions, ())
        self.port.attach_conventions(conventions)
        index = self.port.index()
        self.assertEqual(index.conventions, conventions)
        # The meanings are written in the index's own terms: each names the model it is about.
        import re
        self.assertTrue(all(re.match(r"[a-z_]+\.[a-z_.]+\b", c.meaning) for c in conventions))

    def test_a_malformed_conventions_file_is_refused(self):
        import tempfile
        from pathlib import Path
        from web_operation_agent_odoo.conventions import load_conventions
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "c.json"
            path.write_text('{"schema_version": 2, "conventions": []}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_conventions(path)
            path.write_text('{"schema_version": 1, "conventions": [{"terms": "订单"}]}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_conventions(path)

    def test_a_search_that_names_no_record_of_the_page_asks_for_a_condition_instead(self):
        # "Joel's order": Joel is a customer, not an order's name; the repair says to
        # put the words in a condition on the field that links to the customer.
        result = self._resolve({"kind": "page", "entity": "sale.order",
                                "page": {"key": "p332", "search": "Joel"}})
        resolved = result.deliverables[0]
        self.assertIn("no ", resolved.problem)
        self.assertIsNone(resolved.page)
        self.assertIn("metric.filters on the field that links to it", resolved.repair)

    def test_a_list_page_carries_view_and_facets_and_passes_unknown_facets_through(self):
        result = self._resolve({"kind": "page", "page": {"key": "p332", "view": "kanban",
                                                          "filters_off": ["My Quotations"],
                                                          "group_by": "salesperson"}})
        page = result.deliverables[0].page
        self.assertEqual(page.url, "/odoo/sales?view_type=kanban")
        self.assertEqual(page.expected_path, "/odoo/sales")
        self.assertEqual(page.facets_off, ("My Quotations",))
        self.assertEqual(page.facets_on, ("salesperson",))
        result = self._resolve({"kind": "page", "page": {"key": "p173", "view": "pivot"}})
        self.assertEqual(result.deliverables[0].page.url, "/odoo/action-173")
        self.assertIn("no pivot view", result.deliverables[0].problem)

    def test_an_aggregate_applies_page_scope_and_conditions_and_skips_user_defaults(self):
        result = self._resolve({"kind": "aggregate", "entity": "sale.order",
                                "page": {"key": "p332"},
                                "metric": {"op": "sum", "field": "Total",
                                           "filters": [{"field": "Customer",
                                                        "values": ["Gemini Furniture"]},
                                                       {"field": "Status",
                                                        "values": ["Sales Order"]}]}})
        agg = result.deliverables[0].aggregate
        self.assertEqual(agg.count, 13)
        self.assertEqual(agg.sums, (("Total", "92,750.00 USD"),))
        self.assertEqual(agg.subject,
                         "Quotations · Customer = Gemini Furniture · Status = Sales Order")
        self.assertIn(("search_count", "sale.order"), result.reads)

    def test_a_grouped_count_reports_the_largest_groups(self):
        result = self._resolve({"kind": "aggregate", "entity": "sale.order",
                                "page": {"key": "p331"},
                                "metric": {"op": "count", "group_by": "Customer", "top": 1}})
        self.assertEqual(result.deliverables[0].aggregate.groups[0], ("Gemini Furniture", 13))

    def test_a_handoff_resolves_to_the_record_page_and_the_named_control(self):
        result = self._resolve({"kind": "handoff", "entity": "sale.order", "record": "S00006",
                                "action": "send by email"}, write_intent=True)
        page = result.deliverables[0].page
        self.assertEqual(result.deliverables[0].kind, "handoff")
        self.assertEqual(page.highlight, "Send by Email")
        self.assertEqual(page.url, "/odoo/orders/6")
        self.assertNotIn(("write", "sale.order"), result.reads)
        self.assertTrue(all(method in {"catalog", "fields_get", "get_views", "name_search",
                                       "read", "search_count", "read_group", "search_read"}
                            for _model, method in self.client.calls))


    def test_a_described_record_is_picked_out_by_its_conditions(self):
        result = self._resolve({"kind": "handoff", "entity": "sale.order", "record": "",
                                "action": "Confirm", "page": {"key": "p332"},
                                "metric": {"op": "count",
                                           "filters": [{"field": "Customer", "values": ["Gemini Furniture"]},
                                                       {"field": "Status", "values": ["Quotation Sent"]}]}},
                               write_intent=True)
        page = result.deliverables[0].page
        self.assertEqual((page.url, page.highlight), ("/odoo/orders/6", "Confirm"))
        self.assertTrue(any(["partner_id", "child_of", [11]] in d and ["state", "in", ["sent"]] in d
                            for d in self.client.domains))

    def test_a_named_record_that_does_not_exist_is_never_turned_into_a_creation(self):
        result = self._resolve({"kind": "handoff", "entity": "sale.order",
                                "record": "the quotation sent to Joel", "action": "Confirm"},
                               write_intent=True)
        self.assertIsNone(result.deliverables[0].page)
        self.assertIn("no Sales Order matches", result.deliverables[0].problem)
        created = self._resolve({"kind": "handoff", "entity": "res.partner",
                                 "record": "Joel", "action": "New"}, write_intent=True)
        self.assertEqual(created.deliverables[0].page.highlight, "New")

    def test_a_creation_opens_the_page_declared_for_the_entity(self):
        result = self._resolve({"kind": "handoff", "entity": "res.partner", "action": "New",
                                "page": {"key": "p301"}}, write_intent=True)
        self.assertEqual(result.deliverables[0].page.url, "/odoo/customers")
        widest = self._resolve({"kind": "handoff", "entity": "res.partner", "action": "New"},
                               write_intent=True)
        self.assertEqual(widest.deliverables[0].page.url, "/odoo/customers")
        # An entity this actor may not create: the control is not pointed at.
        refused = self._resolve({"kind": "handoff", "entity": "sale.order", "action": "New"},
                                write_intent=True)
        self.assertIn("not available", refused.deliverables[0].problem)

    def test_the_words_a_request_says_widen_the_page_scope_as_the_deployment_means_them(self):
        from dataclasses import replace as _replace
        from web_operation_agent.goal.index import IndexConvention
        quotes = IndexConvention(terms=("报价",), meaning="sale.order not yet confirmed",
                                 entity="sale.order",
                                 conditions=(("Status", ("Quotation", "Quotation Sent")),))
        every = IndexConvention(terms=("订单",), meaning="sale.order in every status",
                                entity="sale.order")
        count = GoalSpec.from_json({"deliverables": [{
            "kind": "aggregate", "entity": "sale.order", "page": {"key": "p331"},
            "metric": {"op": "count", "filters": [{"field": "Customer", "values": ["Gemini Furniture"]}]}}]})
        # Declared on the confirmed orders' page, the request naming quotations too: the
        # page's own records or the quotations, and the customer either way.
        self.port.resolve(_replace(count, conventions=(quotes,)))
        domain = self.client.domains[-1]
        self.assertEqual(domain[0], "|")
        self.assertIn(["state", "in", ["draft", "sent"]], domain)
        self.assertEqual(domain[-1], ["partner_id", "child_of", [11]])
        # A word meaning every order lifts the page's own scope; the customer stays.
        self.port.resolve(_replace(count, conventions=(every,)))
        self.assertEqual(self.client.domains[-1], [["partner_id", "child_of", [11]]])
        # Declared on the entity alone, the words say which records: the quotations.
        self.port.resolve(_replace(GoalSpec.from_json({"deliverables": [{
            "kind": "aggregate", "entity": "sale.order", "metric": {"op": "count"}}]}),
            conventions=(quotes,)))
        self.assertEqual(self.client.domains[-1], [["state", "in", ["draft", "sent"]]])
        # A convention about another entity leaves this scope alone.
        self.port.resolve(count)
        self.assertNotEqual(self.client.domains[-1][0], "|")
        # With the deployment's words for orders about Status, a word meaning every order
        # widens the page along Status only: a page's other clause stays (every order
        # still to invoice, not every order).
        self.port._conventions = (every, quotes)
        orders = next(a for a in self.port._actions if a["action_id"] == 331)
        orders["domain"] = ("[('state', 'not in', ('draft', 'sent', 'cancel')), "
                            "('invoice_status', '=', 'to invoice')]")
        self.port.resolve(_replace(count, conventions=(every,)))
        self.assertEqual([list(c) for c in self.client.domains[-1]],
                         [["invoice_status", "=", "to invoice"], ["partner_id", "child_of", [11]]])

    def test_a_name_put_on_the_wrong_link_is_found_on_the_one_it_fits(self):
        # "Joel's sent quotation" declared with Joel as the salesperson: nothing fits;
        # with Joel as the customer exactly one does, and that is the record.
        client = self.client
        original = client.call

        def call(model, method, args, kwargs=None):
            if model == "res.users" and method == "name_search":
                return [[5, "Gemini Furniture"]]
            if model == "sale.order" and method == "search_read":
                searched.append(args[0])
                return [] if "user_id" in str(args[0]) else [{"id": 7, "display_name": "S00007"}]
            return original(model, method, args, kwargs)

        searched: list = []
        client.call = call
        result = self._resolve({"kind": "fact", "entity": "sale.order", "fields": ["Total"],
                                "metric": {"op": "count", "filters": [
                                    {"field": "Salesperson", "values": ["Gemini Furniture"]}]}})
        self.assertEqual(result.deliverables[0].problem, "")
        self.assertEqual(searched[0], [["user_id", "in", [5]]])
        self.assertIn([["partner_id", "child_of", [11]]], searched)

    def test_a_company_written_in_a_text_field_is_found_on_its_link(self):
        # "Gemini's marketing manager" declared with Gemini in a text field: nothing fits;
        # on the link to the company, one contact does.
        original = self.client.call
        searched: list = []

        def call(model, method, args, kwargs=None):
            if model == "res.partner" and method == "search_read":
                searched.append(args[0])
                return ([{"id": 31, "display_name": "Gemini Furniture, Ed"}]
                        if "parent_id" in str(args[0]) else [])
            return original(model, method, args, kwargs)

        self.client.call = call
        result = self._resolve({"kind": "fact", "entity": "res.partner", "fields": ["Email"],
                                "metric": {"op": "count", "filters": [
                                    {"field": "Email", "values": ["Gemini Furniture"]},
                                    {"field": "Job Position", "values": ["Marketing Manager"]}]}})
        self.assertEqual(result.deliverables[0].problem, "")
        self.assertTrue(any("parent_id" in str(d) for d in searched))

    def test_the_contacts_of_a_company_written_as_its_name_are_counted_on_the_link(self):
        original = self.client.call

        def call(model, method, args, kwargs=None):
            if model == "res.partner" and method == "search_count":
                return 3 if "parent_id" in str(args[0]) else 0
            return original(model, method, args, kwargs)

        self.client.call = call
        result = self._resolve({"kind": "aggregate", "entity": "res.partner",
                                "metric": {"op": "count", "filters": [
                                    {"field": "Email", "values": ["Gemini Furniture"]}]}})
        agg = result.deliverables[0].aggregate
        self.assertEqual(agg.count, 3)
        self.assertIn("Related Company", agg.subject)

    def test_words_in_another_language_give_way_to_the_other_conditions(self):
        # "三人沙发" cannot match an English name; the price picks the one order out.
        original = self.client.call

        def call(model, method, args, kwargs=None):
            if model == "sale.order" and method == "search_read":
                return [] if "三人沙发" in str(args[0]) else [{"id": 6, "display_name": "S00006"}]
            return original(model, method, args, kwargs)

        self.client.call = call
        result = self._resolve({"kind": "fact", "entity": "sale.order", "fields": ["Total"],
                                "metric": {"op": "count", "filters": [
                                    {"field": "Order Reference", "values": ["三人沙发"], "op": "contains"},
                                    {"field": "Total", "values": ["2947.5"]}]}})
        self.assertEqual(result.deliverables[0].problem, "")
        self.assertEqual(result.deliverables[0].facts[0].subject, "S00006")

    def test_a_named_record_with_conditions_it_does_not_meet_means_those_linked_to_it(self):
        # "Gemini's manager": Gemini is not a manager; its contacts that are, are meant.
        original = self.client.call
        seen: list = []

        def call(model, method, args, kwargs=None):
            if model == "res.partner" and method == "search_count":
                return 0 if ["id", "=", 11] in args[0] else 2
            if model == "res.partner" and method == "search_read":
                seen.append(args[0])
                return [{"id": 31, "display_name": "Gemini Furniture, Ann"},
                        {"id": 32, "display_name": "Gemini Furniture, Bob"}]
            return original(model, method, args, kwargs)

        self.client.call = call
        result = self._resolve({"kind": "fact", "entity": "res.partner", "record": "Gemini Furniture",
                                "fields": ["Email"], "metric": {"op": "count", "filters": [
                                    {"field": "Job Position", "values": ["Manager"]}]}})
        self.assertEqual(result.deliverables[0].candidates,
                         ("Gemini Furniture, Ann", "Gemini Furniture, Bob"))
        # The company's people, not the company itself.
        self.assertIn(["parent_id", "in", [11]], seen[-1])

    def test_search_words_that_repeat_a_condition_add_nothing(self):
        self.client.domains.clear()
        self._resolve({"kind": "list", "entity": "sale.order", "page": {"key": "p331", "search": "Gemini"},
                       "metric": {"op": "count", "filters": [{"field": "Customer", "values": ["Gemini Furniture"]}]}})
        self.assertTrue(self.client.domains)
        self.assertFalse(any("display_name" in str(d) or "ilike" in str(d) for d in self.client.domains))

    def test_an_exact_name_that_longer_names_hold_is_asked_about(self):
        original = self.client.call

        def call(model, method, args, kwargs=None):
            if model == "sale.order" and method == "name_search":
                return [[6, "S00006"], [8, "S00006-B"]] if args[0] == "S00006" else []
            return original(model, method, args, kwargs)

        self.client.call = call
        # A numbered reference is an identifier: the exact one is taken.
        self.assertEqual(self.port._find_record("sale.order", "S00006")[:2], ("sale.order", 6))
        original2 = self.client.call

        def names(model, method, args, kwargs=None):
            if model == "product.tag" and method == "name_search":
                return [[1, "Office Chair"], [2, "Office Chair Black"]]
            return original2(model, method, args, kwargs)

        self.client.call = names
        self.assertEqual(tuple(self.port._find_record("product.tag", "Office Chair")),
                         ("Office Chair", "Office Chair Black"))

    def test_a_name_copied_with_its_code_is_found_by_its_name(self):
        found = self.port._find_record("sale.order", "[X1] S00006")
        self.assertEqual(found[:2], ("sale.order", 6))

    def test_a_delete_this_actor_may_not_perform_is_denied_before_any_record_is_sought(self):
        # The actor may read orders but not delete them: whichever order was meant, the
        # answer is the same, so no record is looked up and no one is asked which.
        before = len(self.client.calls) if hasattr(self.client, "calls") else None
        denied = self._resolve({"kind": "handoff", "entity": "sale.order",
                                "record": "Ready Mat 的草稿报价", "action": "Delete"}, write_intent=True)
        self.assertEqual(denied.deliverables[0].denied, "delete")
        self.assertEqual(denied.deliverables[0].candidates, ())
        self.assertEqual(denied.reads, ())
        if before is not None:
            self.assertEqual(len(self.client.calls), before)
        # Another action on the same record is not affected.
        other = self._resolve({"kind": "handoff", "entity": "sale.order", "record": "S00006",
                               "action": "Confirm"}, write_intent=True)
        self.assertEqual(other.deliverables[0].denied, "")
        # A new record on a model the account may not create is denied as a creation.
        created = self._resolve({"kind": "handoff", "entity": "sale.order", "action": "New",
                                 "value": "S00099"}, write_intent=True)
        self.assertEqual(created.deliverables[0].denied, "create")
        created = self._resolve({"kind": "handoff", "entity": "res.partner", "action": "New",
                                 "value": "Sunrise Trading"}, write_intent=True)
        self.assertEqual(created.deliverables[0].denied, "")

    def test_a_view_the_page_lacks_is_reported_on_the_plan(self):
        result = self._resolve({"kind": "page", "page": {"key": "p173", "view": "pivot"}})
        plan = result.deliverables[0].page
        self.assertEqual((plan.view, plan.missing_view), ("", "pivot"))
        self.assertTrue(plan.url.startswith("/odoo/action-"))

    def test_a_handoff_value_becomes_what_to_enter_only_when_certain(self):
        # An account that may not change orders is told so, whatever the value.
        denied = self._resolve({"kind": "handoff", "entity": "sale.order", "record": "S00006",
                                "action": "Order Date", "value": "2026-10-31"}, write_intent=True)
        self.assertEqual(denied.deliverables[0].denied, "write")
        self.port._operations["sale.order"] = frozenset({"read", "write"})

        def plan(action, value):
            result = self._resolve({"kind": "handoff", "entity": "sale.order", "record": "S00006",
                                    "action": action, "value": value}, write_intent=True)
            return result.deliverables[0]
        dated = plan("Order Date", "2026-10-31").page
        self.assertEqual((dated.fill, dated.fill_kind), ("2026-10-31", "text"))
        self.assertEqual(plan("Order Date", "2026-10").page.fill, "")
        meta = self.port._models["sale.order"]
        self.assertEqual(self.port._fill_for(meta, "state", "Quotation Sent"),
                         ("sent", "choice", "Quotation Sent"))
        self.assertEqual(self.port._fill_for(meta, "state", "Lost"), ("", "", ""))
        linked = plan("Payment Terms", "End of Following Month").page
        self.assertEqual((linked.fill, linked.fill_kind), ("End of Following Month", "link"))
        nobody = plan("Payment Terms", "Nobody")
        self.assertEqual(nobody.page.fill, "")
        self.assertIn("no Payment Terms is named 'Nobody'", nobody.repair)
        self.assertEqual(self.port._fill_for(meta, "amount_total", "1,200.50")[0], "1200.50")
        self.assertEqual(self.port._fill_for(meta, "amount_total", "a lot")[0], "")
        self.assertEqual(plan("Confirm", "").page.fill, "")

    def test_a_reference_is_located_on_the_entities_the_pages_show(self):
        self.assertEqual(self.port.locate_reference("S00006"), (("sale.order", "S00006"),))
        self.assertEqual(self.port.locate_reference("Gemini Furniture"),
                         (("res.partner", "Gemini Furniture"),))
        # A number or a date is not a name; a name found only inside a longer name is
        # not a reference to that record.
        self.assertEqual(self.port.locate_reference("1740"), ())
        self.assertEqual(self.port.locate_reference("2026-08-22"), ())
        self.assertEqual(self.port.locate_reference("Nobody"), ())

    def test_source_rules_become_index_entries_with_citations(self):
        from web_operation_agent.schemas.planning import EvidenceRef, SourceMap, SourceRelation
        from web_operation_agent.schemas.planning import SCHEMA_VERSIONS

        def ref(path, start, end):
            return EvidenceRef(repository_id="odoo", release="bound", path=path,
                               start_line=start, end_line=end, sha256="b" * 64)
        relations = [
            SourceRelation(kind="form_target", subject="sale.view_order_form", object="330",
                           attrs={"model": "sale.order", "label": "Create Invoice",
                                  "target_type": "action", "invisible": "state != 'sale'"},
                           evidence=(ref("addons/sale/views/sale_order_views.xml", 266, 271),)),
            SourceRelation(kind="form_field", subject="sale.view_order_form", object="date_order",
                           attrs={"model": "sale.order", "readonly": "state in ('sale', 'cancel')"},
                           evidence=(ref("addons/sale/views/sale_order_views.xml", 300, 300),)),
            SourceRelation(kind="service_guard", subject="sale.order.action_confirm",
                           object="It is not allowed to confirm an order in the following states: %s",
                           attrs={"condition": "order.state not in ('draft', 'sent')"},
                           evidence=(ref("addons/sale/models/sale_order.py", 1010, 1014),)),
            SourceRelation(kind="service_entity", subject="sale.order", object="validity_date",
                           attrs={"string": "Expiration", "help": "Validity of the order, after it, a customer can no longer accept it."},
                           evidence=(ref("addons/sale/models/sale_order.py", 120, 124),)),
            SourceRelation(kind="menu_link", subject="Orders", object="sales/orders",
                           attrs={}, evidence=(ref("addons/sale/views/sale_menus.xml", 1, 2),)),
            # The window "Create Invoice" opens, its own refusal and a field's meaning,
            # and a button whose method opens a window by xml id, for a group only.
            SourceRelation(kind="request_handler", subject="odoo/action-330", object="sale.advance.payment.inv",
                           attrs={"action_id": "330", "name": "Create invoice(s)", "target": "new",
                                  "event_type": "dialog"},
                           evidence=(ref("addons/sale/wizard/sale_make_invoice_advance_views.xml", 40, 48),)),
            SourceRelation(kind="service_guard", subject="sale.advance.payment.inv._check_amount_is_positive",
                           object="The value of the down payment amount must be positive.",
                           attrs={"condition": "wizard.amount <= 0.0"},
                           evidence=(ref("addons/sale/wizard/sale_make_invoice_advance.py", 125, 130),)),
            SourceRelation(kind="service_entity", subject="sale.advance.payment.inv",
                           object="consolidated_billing",
                           attrs={"string": "Consolidated Billing",
                                  "help": "Create one invoice for all orders related to same customer and same invoicing address"},
                           evidence=(ref("addons/sale/wizard/sale_make_invoice_advance.py", 60, 63),)),
            SourceRelation(kind="form_target", subject="sale.view_order_form", object="action_reverse",
                           attrs={"model": "sale.order", "label": "Credit Note", "target_type": "object",
                                  "invisible": "state != 'posted'", "groups": "account.group_account_invoice"},
                           evidence=(ref("addons/sale/views/sale_order_views.xml", 769, 772),)),
            SourceRelation(kind="service_parameter", subject="sale.order.action_reverse", object="->window",
                           attrs={"direction": "out", "opens": "", "opens_action": "331", "opens_name": ""},
                           evidence=(ref("addons/sale/models/sale_order.py", 5550, 5556),)),
            SourceRelation(kind="request_handler", subject="odoo/action-331", object="account.move.reversal",
                           attrs={"action_id": "331", "name": "Reverse", "target": "new", "event_type": "dialog"},
                           evidence=(ref("addons/account/wizard/account_move_reversal_view.xml", 28, 37),)),
            # A button whose method returns another model's method's action, and the
            # same field condition written in two views of the form.
            SourceRelation(kind="form_target", subject="sale.view_order_form", object="action_pay",
                           attrs={"model": "sale.order", "label": "Pay", "target_type": "object"},
                           evidence=(ref("addons/sale/views/sale_order_views.xml", 700, 701),)),
            SourceRelation(kind="service_parameter", subject="sale.order.action_pay", object="->window",
                           attrs={"direction": "out", "opens_via": "action_open_register"},
                           evidence=(ref("addons/sale/models/sale_order.py", 900, 901),)),
            SourceRelation(kind="service_parameter", subject="sale.order.line.action_open_register",
                           object="->window",
                           attrs={"direction": "out", "opens": "account.payment.register",
                                  "opens_action": "", "opens_name": ""},
                           evidence=(ref("addons/sale/models/sale_order_line.py", 50, 60),)),
            SourceRelation(kind="form_field", subject="sale.view_order_form", object="note",
                           attrs={"model": "sale.order", "readonly": "locked"},
                           evidence=(ref("addons/sale/views/sale_order_views.xml", 420, 420),)),
            SourceRelation(kind="form_field", subject="sale.view_order_form", object="note",
                           attrs={"model": "sale.order", "readonly": "locked"},
                           evidence=(ref("addons/sale/views/sale_order_views.xml", 527, 530),)),
            SourceRelation(kind="service_guard", subject="sale.order.action_confirm",
                           object="the message of _confirmation_error_message",
                           attrs={"condition": "error_msg", "via": "_confirmation_error_message"},
                           evidence=(ref("addons/sale/models/sale_order.py", 1154, 1157),)),
        ]
        source_map = SourceMap(schema_version=SCHEMA_VERSIONS["SourceMap"], attestation_id="a",
                               repository_id="odoo", release="bound", relations=relations)
        self.port.attach_source(source_map, release="18.0-20260909")
        self.assertIn("source", self.port.capabilities())
        entity = self.port.index().entity("sale.order")
        by_kind = {}
        for r in entity.rules:
            by_kind.setdefault(r.kind, r)
        self.assertEqual(set(by_kind), {"visibility", "editability", "refusal", "meaning", "target"})
        self.assertEqual(by_kind["visibility"].about, "Create Invoice")
        self.assertIn("hidden when state != 'sale'", by_kind["visibility"].text)
        self.assertEqual(by_kind["visibility"].citation,
                         "addons/sale/views/sale_order_views.xml:266-271@18.0-20260909 sha256:" + "b" * 64)
        self.assertIn('Field "Order Date" (date_order): readonly when', by_kind["editability"].text)
        self.assertTrue(any("Validity of the order" in r.text for r in entity.rules))
        self.assertNotIn("RULES", self.port.index().render())
        # The button says which window it opens; the window's own refusal and field
        # meaning come along under the button's name, on the button's operation.
        create = by_kind["visibility"]
        self.assertIn("which opens the Create invoice(s) window of sale.advance.payment.inv", create.text)
        self.assertEqual(create.subject, "330")
        texts = {r.text: r for r in entity.rules}
        opened = next(r for r in entity.rules if r.kind == "target" and r.about == "Create Invoice")
        self.assertIn("opens the Create invoice(s) window of sale.advance.payment.inv as a dialog", opened.text)
        self.assertTrue(opened.citation.startswith(
            "addons/sale/wizard/sale_make_invoice_advance_views.xml:40-48@18.0-20260909"))
        positive = next(r for r in entity.rules if "_check_amount_is_positive" in r.about)
        self.assertEqual(positive.kind, "refusal")
        self.assertTrue(positive.about.startswith("Create Invoice › "))
        self.assertEqual(positive.subject, "330")
        consolidated = next(r for r in entity.rules if r.about == "Create Invoice › Consolidated Billing")
        self.assertIn("In the Create invoice(s) window (sale.advance.payment.inv)", consolidated.text)
        self.assertIn("same invoicing address", consolidated.text)
        # A method that opens a window by xml id names the window; a group restriction
        # is a condition; a refusal whose text another method returns says so.
        credit = next(r for r in entity.rules if r.about == "Credit Note" and r.kind == "visibility")
        self.assertIn("calls action_reverse, which opens the Reverse window of account.move.reversal", credit.text)
        self.assertIn("only for members of account.group_account_invoice", credit.text)
        self.assertEqual(credit.subject, "action_reverse")
        declared = [r for r in entity.rules if r.about == "Credit Note" and r.kind == "target"]
        self.assertEqual([r.citation.split("@")[0] for r in declared],
                         ["addons/sale/models/sale_order.py:5550-5556",
                          "addons/account/wizard/account_move_reversal_view.xml:28-37"])
        via = next(r for r in entity.rules if "the message of" in r.text)
        self.assertEqual(via.text, "action_confirm refuses with the message of _confirmation_error_message when error_msg")
        self.assertEqual(via.subject, "_confirmation_error_message")
        pay = next(r for r in entity.rules if r.about == "Pay" and r.kind == "target")
        self.assertIn("calls action_pay, which opens the window of account.payment.register", pay.text)
        notes = [r for r in entity.rules if r.kind == "editability" and "(note)" in r.text]
        self.assertEqual([r.citation.split("@")[0] for r in notes],
                         ["addons/sale/views/sale_order_views.xml:420-420",
                          "addons/sale/views/sale_order_views.xml:527-530"])
        refusals = [r for r in entity.rules if r.about == "action_confirm"]
        self.assertIn('action_confirm refuses with "It is not allowed', refusals[0].text)
        self.assertEqual(refusals[0].subject, "action_confirm")

    def test_a_link_name_matches_by_its_words_in_any_order(self):
        from web_operation_agent_odoo.goal.port import _worded
        self.assertEqual(_worded("product_id", ["acoustic screens"]),
                         ["&", ["product_id", "ilike", "acoustic"], ["product_id", "ilike", "screen"]])
        self.assertEqual(_worded("product_id", ["Black Office Chair", "Desk"]),
                         ["|", "&", "&", ["product_id", "ilike", "Black"],
                          ["product_id", "ilike", "Office"], ["product_id", "ilike", "Chair"],
                          ["product_id", "ilike", "Desk"]])
        # Words only a person would type in another script have nothing to match.
        self.assertEqual(_worded("product_id", ["白色隔音屏"]), [])

    def test_candidate_values_are_the_lines_and_documents_near_what_the_request_names(self):
        self.assertIn("values", self.port.capabilities())
        # A named order: itself and its lines, the line's item by its exact name.
        named = self.port.candidates(entities=("sale.order.line",), known=(("sale.order", "S00006"),))
        self.assertEqual([(c.kind, c.entity, c.display) for c in named],
                         [("record", "sale.order", "S00006"), ("line", "sale.order.line", "Desk")])
        self.assertIn("Quantity 1.0", named[1].detail)
        self.assertEqual(named[1].binds, (("Order Reference", "S00006"), ("Product", "Desk")))
        self.assertTrue(named[1].detail.startswith("S00006: "))
        # A named customer: their documents on the entity asked about, and those lines.
        partner = self.port.candidates(entities=("sale.order.line",),
                                       known=(("res.partner", "Gemini Furniture"),))
        self.assertEqual([(c.kind, c.display) for c in partner], [("record", "S00006"), ("line", "Desk")])
        self.assertIn(["partner_id", "child_of", 11], self.client.domains[-2])
        # A day the request names: the documents dated that day.
        dated = self.port.candidates(entities=("sale.order",), dates=("2026-08-12",))
        self.assertEqual([c.display for c in dated], ["S00006"])
        self.assertIn("Total 2947.5", partner[0].detail)
        self.assertEqual(partner[0].binds, (("Order Reference", "S00006"),))
        # Nothing named, nothing listed: no search across the database by a guess.
        self.assertEqual(self.port.candidates(entities=("sale.order.line",)), ())

    def test_a_flag_that_is_off_reads_as_no(self):
        self.assertEqual(self.port._display(False, {"type": "boolean"}, {}), "No")
        self.assertEqual(self.port._display(True, {"type": "boolean"}, {}), "Yes")
        self.assertEqual(self.port._display(False, {"type": "char"}, {}), "(empty)")

    def test_a_contact_of_a_company_is_located_by_its_own_name(self):
        self.assertIn(("res.partner", "Gemini Furniture, Ed"), self.port.locate_reference("Ed"))

    def test_field_labels_match_by_whole_words_and_field_names(self):
        meta = self.port._models["sale.order"]
        self.assertEqual(meta.field_by_label("Order Date"), "date_order")
        self.assertEqual(meta.field_by_label("Product Variants"), "product_variant_count")
        self.assertEqual(meta.field_by_label("Customer name"), "partner_id")
        self.assertIsNone(meta.field_by_label("Validity Date"))
        self.assertEqual(meta.field_by_label("Date Order"), "date_order")


class OdooGoalPortConditionTests(unittest.TestCase):
    def setUp(self):
        self.client = _Client()
        self.port = OdooGoalPort(self.client, translations=_TRANSLATIONS)
        self.index = self.port.index()

    def _resolve(self, deliverable):
        return self.port.resolve(GoalSpec.from_json({"deliverables": [deliverable]}))

    def _last_domain(self):
        return self.client.domains[-1]

    def test_a_record_a_line_links_to_is_the_lines_own_condition(self):
        # "S00004 订单里 [FURN_8888] Office Lamp 那行": the code's digits do not make the
        # product a document the line belongs to, even where another model finds the
        # name; the product is the line's own.
        found = {("product.product", "[D1] Desk 2"): ("product.product", 12, "[D1] Desk 2"),
                 ("sale.order", "[D1] Desk 2"): ("sale.order", 9, "S00009")}
        self.port._find_record = lambda model, value: found.get((model, value))
        line = Deliverable.from_json({
            "kind": "fact", "entity": "sale.order.line", "fields": ["Quantity"],
            "metric": {"op": "count", "filters": [{"field": "Product", "values": ["[D1] Desk 2"]}]}})
        self.assertEqual(self.port._line_record_from_conditions(line), line)

    def test_fields_no_page_shows_are_held_on_the_entity(self):
        order = self.index.entity("sale.order")
        # In fields_get, on no view: held, not indexed; mail bookkeeping is neither.
        self.assertIn("product_variant_count", [f.key for f in order.held])
        self.assertNotIn("product_variant_count", [f.key for f in order.fields])
        self.assertFalse({f.key for f in order.held} & {f.key for f in order.fields})
        self.assertNotIn("message_ids", [f.key for f in order.held])

    def test_line_entities_come_from_the_record_form_with_the_link_first(self):
        keys = [e.key for e in self.index.entities]
        self.assertEqual(keys[keys.index("sale.order") + 1], "sale.order.line")
        line = self.index.entity("sale.order.line")
        self.assertEqual([f.label for f in line.fields],
                         ["Order Reference", "Product", "Subtotal", "Quantity"])
        self.assertIn("订单行", line.aliases)

    def test_the_applications_own_chinese_names_label_and_resolve_fields_and_choices(self):
        order = self.index.entity("sale.order")
        customer = next(f for f in order.fields if f.label == "Customer")
        self.assertEqual(customer.aliases, ("客户",))
        self.assertIn("Customer[客户]", self.index.render())
        # A condition written with the Chinese names reaches the same field and choice.
        resolved = self._resolve({"kind": "aggregate", "entity": "sale.order",
                                  "page": {"key": "p331"}, "metric": {"op": "count", "filters": [
                                      {"field": "状态", "values": ["报价已发送"]}]}})
        self.assertTrue(resolved.deliverables[0].problem)  # 状态 is not in this catalogue
        resolved = self._resolve({"kind": "aggregate", "entity": "sale.order",
                                  "page": {"key": "p331"}, "metric": {"op": "count", "filters": [
                                      {"field": "Status", "values": ["报价已发送"]}]}})
        self.assertEqual(resolved.deliverables[0].problem, "")
        self.assertIn(["state", "in", ["sent"]], self._last_domain())
        # Without a catalogue there are no Chinese names at all: nothing hand-written.
        bare = OdooGoalPort(_Client()).index()
        self.assertEqual(bare.page("p331").aliases, ())

    def test_a_date_period_becomes_utc_bounds_in_the_users_zone(self):
        self._resolve({"kind": "aggregate", "entity": "sale.order", "page": {"key": "p331"},
                       "metric": {"op": "count",
                                  "filters": [{"field": "Order Date", "values": ["2026-08"]}]}})
        self.assertEqual(self._last_domain()[-2:],
                         [["date_order", ">=", "2026-07-31 16:00:00"],
                          ["date_order", "<", "2026-08-31 16:00:00"]])
        self._resolve({"kind": "aggregate", "entity": "sale.order", "page": {"key": "p331"},
                       "metric": {"op": "count",
                                  "filters": [{"field": "Order Date", "values": ["2026-09-10"],
                                               "op": ">"}]}})
        self.assertEqual(self._last_domain()[-1], ["date_order", ">=", "2026-09-10 16:00:00"])
        result = self._resolve({"kind": "aggregate", "entity": "sale.order", "page": {"key": "p331"},
                                "metric": {"op": "count",
                                           "filters": [{"field": "Order Date", "values": ["someday"]}]}})
        self.assertIn("not a date or period", result.deliverables[0].problem)

    def test_a_field_not_equal_to_nothing_is_a_field_that_is_set(self):
        # "Orders with a customer": the field, no value, not equal -- the field is set.
        resolved = self._resolve({"kind": "aggregate", "entity": "sale.order",
                                  "page": {"key": "p331"}, "metric": {"op": "count", "filters": [
                                      {"field": "Customer", "values": [], "op": "!="}]}})
        self.assertEqual(resolved.deliverables[0].problem, "")
        self.assertIn(["partner_id", "!=", False], self._last_domain())
        # Equal to nothing: the field is not set ("orders with no campaign").
        self._resolve({"kind": "aggregate", "entity": "sale.order", "page": {"key": "p331"},
                       "metric": {"op": "count", "filters": [
                           {"field": "Customer", "values": [], "op": "="}]}})
        self.assertIn(["partner_id", "=", False], self._last_domain())
        # A field named with nothing to compare against, and no comparison, says nothing.
        self._resolve({"kind": "aggregate", "entity": "sale.order", "page": {"key": "p331"},
                       "metric": {"op": "count", "filters": [{"field": "Customer", "values": []}]}})
        self.assertFalse(any(isinstance(c, list) and c[0] == "partner_id"
                             for c in self._last_domain()))

    def test_a_value_several_records_hold_is_asked_about_when_more_than_one_is_in_scope(self):
        terms = [[7, "Immediate Payment"], [8, "Immediate Payment Split"]]
        in_scope = {"rows": terms}
        call = self.client.call

        def call_with_terms(model, method, args, kwargs=None):
            if model == "account.payment.term" and method == "name_search":
                return [row for row in terms if args[0].casefold() in row[1].casefold()]
            if model == "sale.order" and method == "search_read" and \
                    (kwargs or {}).get("fields") == ["payment_term_id"]:
                return [{"id": i, "payment_term_id": row} for i, row in enumerate(in_scope["rows"])]
            return call(model, method, args, kwargs)
        self.client.call = call_with_terms
        declared = {"kind": "aggregate", "entity": "sale.order", "page": {"key": "p331"},
                    "metric": {"op": "count", "filters": [
                        {"field": "Payment Terms", "values": ["Immediate Payment"]}]}}
        # Both orders' terms hold the name: the person is asked which.
        resolved = self._resolve(declared).deliverables[0]
        self.assertEqual(resolved.candidates, ("Immediate Payment", "Immediate Payment Split"))
        # Only one of them is on the orders described: that one is meant.
        in_scope["rows"] = terms[:1]
        resolved = self._resolve(declared).deliverables[0]
        self.assertEqual((resolved.problem, resolved.candidates), ("", ()))
        self.assertIn(["payment_term_id", "=", 7], self._last_domain())
        # Once the person has chosen the name, it is the record of exactly that name.
        in_scope["rows"] = terms
        resolved = self.port.resolve(replace(GoalSpec.from_json({"deliverables": [declared]}),
                                             chosen=("Immediate Payment",))).deliverables[0]
        self.assertEqual((resolved.problem, resolved.candidates), ("", ()))
        # The words said as words ("contains") are every record holding them.
        contains = {**declared, "metric": {"op": "count", "filters": [
            {"field": "Payment Terms", "values": ["Immediate Payment"], "op": "contains"}]}}
        self.assertEqual(self._resolve(contains).deliverables[0].candidates, ())

    def test_a_page_filter_that_names_today_is_written_for_this_turn(self):
        self._resolve({"kind": "aggregate", "entity": "sale.order", "page": {"key": "p331"},
                       "metric": {"op": "count", "page_filters": ["Late"]}})
        clause = self._last_domain()[-1]
        self.assertEqual(list(clause[:2]), ["date_order", "<"])
        self.assertRegex(clause[2], r"^\d{4}-\d{2}-\d{2}$")

    def test_numbers_take_operators_and_unstored_fields_are_refused(self):
        self._resolve({"kind": "aggregate", "entity": "sale.order", "page": {"key": "p331"},
                       "metric": {"op": "count",
                                  "filters": [{"field": "Total", "values": ["500", "1,000"],
                                               "op": "between"}]}})
        self.assertEqual(self._last_domain()[-2:],
                         [["amount_total", ">=", 500.0], ["amount_total", "<=", 1000.0]])
        result = self._resolve({"kind": "aggregate", "entity": "sale.order", "page": {"key": "p331"},
                                "metric": {"op": "count",
                                           "filters": [{"field": "Product Variants", "values": ["1"],
                                                        "op": ">"}]}})
        self.assertIn("cannot be used as a condition", result.deliverables[0].problem)

    def test_an_entity_without_a_page_counts_every_record_of_it(self):
        result = self._resolve({"kind": "aggregate", "entity": "sale.order.line",
                                "metric": {"op": "sum", "field": "Subtotal",
                                           "filters": [{"field": "Order Reference", "values": ["S00006"]}]}})
        agg = result.deliverables[0].aggregate
        self.assertEqual(agg.subject, "Sales Order Line · Order Reference = S00006")
        self.assertEqual(self.client.domains[0], [["order_id", "in", [6]]])
        self.assertEqual(agg.sums, (("Subtotal", "92,750.00 USD"),))

    def test_a_list_shows_columns_and_orders_records(self):
        result = self._resolve({"kind": "list", "entity": "sale.order.line",
                                "fields": ["Product", "Quantity", "Subtotal"],
                                "sort": "Subtotal", "descending": True, "limit": 5,
                                "metric": {"op": "count",
                                           "filters": [{"field": "Order Reference", "values": ["S00006"]}]}})
        agg = result.deliverables[0].aggregate
        self.assertEqual(agg.records,
                         ("S00006 - Desk — Product: Desk; Quantity: 1.00; Subtotal: 750.00 USD",))
        self.assertIn("by Subtotal desc", agg.subject)
        self.assertIsNone(result.deliverables[0].page)

    def test_an_x2many_fact_lists_the_first_names(self):
        result = self._resolve({"kind": "fact", "entity": "sale.order", "record": "S00006",
                                "fields": ["Order Lines"]})
        self.assertEqual(result.deliverables[0].facts[0].value, "1 records: S00006 - Desk")


if __name__ == "__main__":
    unittest.main()
