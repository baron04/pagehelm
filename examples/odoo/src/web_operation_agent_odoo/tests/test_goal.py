"""The Odoo goal port over a scripted JSON-RPC client: index and resolution, no model."""
from __future__ import annotations

import unittest

from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent_odoo.goal import OdooGoalPort

_CATALOG = {
    "descriptor_digest": "d1",
    "models": [
        {"model": "sale.order", "label": "Sales Order", "operations": ["read"], "transient": False},
        {"model": "res.partner", "label": "Contact", "operations": ["read"], "transient": False},
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
        "list": '<list><field name="name"/><field name="email"/></list>',
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


class OdooGoalPortTests(unittest.TestCase):
    def setUp(self):
        self.client = _Client()
        self.port = OdooGoalPort(self.client)
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
        self.assertIs(self.port.index(), self.index)

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



class OdooGoalPortConditionTests(unittest.TestCase):
    def setUp(self):
        self.client = _Client()
        self.port = OdooGoalPort(self.client)
        self.index = self.port.index()

    def _resolve(self, deliverable):
        return self.port.resolve(GoalSpec.from_json({"deliverables": [deliverable]}))

    def _last_domain(self):
        return self.client.domains[-1]

    def test_line_entities_come_from_the_record_form_with_the_link_first(self):
        keys = [e.key for e in self.index.entities]
        self.assertEqual(keys[keys.index("sale.order") + 1], "sale.order.line")
        line = self.index.entity("sale.order.line")
        self.assertEqual([f.label for f in line.fields],
                         ["Order Reference", "Product", "Subtotal", "Quantity"])
        self.assertIn("订单行", line.aliases)

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
