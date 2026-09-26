"""The Odoo goal port over a scripted JSON-RPC client: index and resolution, no model."""
from __future__ import annotations

import unittest

from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent_odoo.goal import OdooGoalPort

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
        self.assertEqual(self.index.page("p301").operations, ("create",))
        self.assertEqual(self.index.page("p331").operations, ())
        self.assertIs(self.port.index(), self.index)

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

    def test_a_view_the_page_lacks_is_reported_on_the_plan(self):
        result = self._resolve({"kind": "page", "page": {"key": "p173", "view": "pivot"}})
        plan = result.deliverables[0].page
        self.assertEqual((plan.view, plan.missing_view), ("", "pivot"))
        self.assertTrue(plan.url.startswith("/odoo/action-"))

    def test_a_handoff_value_becomes_what_to_enter_only_when_certain(self):
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
