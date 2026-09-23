"""The Odoo assistant may click only reviewed read-only controls, and nothing else."""
from __future__ import annotations

import unittest

from web_operation_agent.business.errors import BusinessToolPermissionError
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent_odoo.click_policy import OdooReviewedClickPolicy, load_review

ORIGIN = "http://odoo.test"


def _obs(path: str, *elements: ElementRef) -> PageObservation:
    return PageObservation(schema_version=1, epoch=1, url=ORIGIN + path, title="Odoo",
                           landmarks=[], elements=list(elements))


def _el(ref, role, label="", **attrs) -> ElementRef:
    return ElementRef(ref, role, label, "button" if role == "button" else "a", 1, attrs)


class ReviewedClicks(unittest.TestCase):
    def setUp(self):
        self.policy = OdooReviewedClickPolicy.from_review(
            load_review(), {"account.action_payment_term_form": 286})

    def decide(self, path, element):
        return self.policy.decide(_obs(path, element), element)

    def test_reviewed_controls_on_a_reviewed_page_are_allowed(self):
        for element in (_el("e1", "button", "Kanban"), _el("e2", "button", "Next"),
                        _el("e3", "button", "Remove"), _el("e4", "button", "Configuration"),
                        _el("e5", "menuitem", "Payment Terms", href="/odoo/action-286"),
                        _el("e6", "link", "S00007", href="/odoo/sales/7")):
            with self.subTest(element=element.name):
                self.assertEqual(self.decide("/odoo/sales", element), "")
        self.assertEqual(self.decide("/odoo/action-286", _el("e1", "button", "List")), "")
        self.assertEqual(self.decide(
            "/odoo/action-account.action_payment_term_form", _el("e1", "button", "List")), "")

    def test_writers_and_unnamed_controls_are_refused_even_on_a_reviewed_page(self):
        for element in (_el("e1", "button", "New"), _el("e2", "button", "Actions"),
                        _el("e3", "button", "Upload"), _el("e4", "button"),
                        _el("e5", "checkbox"), _el("e6", "td", "S00007", name="name"),
                        _el("e7", "button", "Activate", name="button_immediate_install")):
            with self.subTest(element=element.name or element.role):
                self.assertTrue(self.decide("/odoo/apps", element))

    def test_rows_open_only_where_the_review_says_so(self):
        cell = _el("e1", "td", "S00007", name="name")
        card = _el("e2", "article", "Acme Corporation")
        self.assertEqual(self.decide("/odoo/sales", cell), "")
        self.assertEqual(self.decide("/odoo/customers", card), "")
        # The checkbox and handle cells carry no field name; they are not the row.
        self.assertTrue(self.decide("/odoo/sales", _el("e3", "td")))
        # Apps and configuration lists (editable lists start inline editing) keep rows shut.
        self.assertTrue(self.decide("/odoo/apps", card))
        self.assertTrue(self.decide("/odoo/action-286", _el("e4", "td", "30 Days",
                                                              name="name")))

    def test_a_record_of_a_reviewed_collection_allows_only_its_pager_and_menus(self):
        for element in (_el("e1", "button", "Next"), _el("e2", "button", "Previous"),
                        _el("e3", "button", "Orders"), _el("e4", "tab", "Other Info",
                                                               href="#")):
            with self.subTest(element=element.name):
                self.assertEqual(self.decide("/odoo/sales/1", element), "")
        for element in (_el("e1", "button", "Confirm", name="action_confirm"),
                        _el("e2", "button", "Send by Email"), _el("e3", "button", "Cancel"),
                        _el("e4", "button", "Delete", name="delete"),
                        _el("e5", "button", "Remove"), _el("e6", "button", "Kanban"),
                        _el("e7", "td", "Office Lamp", name="product_id"),
                        _el("e8", "button", "Add a product", href="#")):
            with self.subTest(element=element.name):
                self.assertIn("on a record", self.decide("/odoo/sales/1", element))
        self.assertIn("not been reviewed", self.decide("/odoo/sales/new",
                                                        _el("e1", "button", "Next")))

    def test_an_action_s_own_url_path_is_the_same_reviewed_page(self):
        policy = OdooReviewedClickPolicy.from_review(
            load_review(), {"account.action_move_in_invoice_type": 264},
            {"account.action_move_in_invoice_type": "bills"})
        cell = _el("e1", "td", "BILL/2026/0001", name="name")
        for path in ("/odoo/bills", "/odoo/action-264",
                     "/odoo/action-account.action_move_in_invoice_type"):
            with self.subTest(path=path):
                self.assertEqual(policy.decide(_obs(path, cell), cell), "")
        self.assertTrue(policy.destination_allowed("/odoo/bills/12"))
        # A path the database reports that is not a plain segment is ignored.
        odd = OdooReviewedClickPolicy.from_review(
            load_review(), {"account.action_move_in_invoice_type": 264},
            {"account.action_move_in_invoice_type": "../settings"})
        self.assertFalse(odd.reviewed("/odoo/../settings"))

    def test_an_unreviewed_page_refuses_its_controls_but_not_tabs_or_the_home_menu(self):
        self.assertIn("not been reviewed", self.decide("/odoo/discuss",
                                                        _el("e1", "button", "Kanban")))
        self.assertIn("not been reviewed", self.decide("/odoo/products/new",
                                                        _el("e1", "button", "Next")))
        self.assertEqual(self.decide("/odoo/products/new", _el("e2", "tab", "Sales",
                                                                 href="#")), "")
        self.assertEqual(self.decide("/odoo/discuss", _el("e0", "button", "Home Menu")), "")
        # The application switcher's entries are links: reviewed destinations only.
        self.assertEqual(self.decide("/odoo/discuss", _el("e3", "menuitem", "Sales",
                                                           href="/odoo/sales")), "")
        self.assertIn("nobody reviewed", self.decide(
            "/odoo/discuss", _el("e4", "menuitem", "Settings", href="/odoo/settings")))

    def test_links_leave_only_for_reviewed_destinations_in_this_window(self):
        refused = (
            _el("e1", "link", "New", href="/odoo/sales/new"),
            _el("e2", "link", "Other action", href="/odoo/action-999"),
            _el("e3", "button", "Learn More", href="https://www.odoo.com/app", target="_blank"),
            _el("e4", "link", "Report", href="/odoo/sales/7", target="_blank"),
            _el("e5", "link", "Script", href="javascript:void(0)"),
        )
        for element in refused:
            with self.subTest(element=element.name):
                self.assertTrue(self.decide("/odoo/sales", element))

    def test_a_reviewed_record_opens_its_related_records_and_no_further(self):
        # Found live: "open order S00007, then open its customer" reached the order and was
        # refused the customer link. Odoo writes a many2one link as /odoo/<model>/<id>.
        customer = _el("e19", "link", "Gemini Furniture", href="/odoo/res.partner/11")
        term = _el("e20", "link", "End of Following Month",
                   href="/odoo/account.payment.term/6")
        self.assertEqual(self.decide("/odoo/sales/7", customer), "")
        self.assertEqual(self.decide("/odoo/sales/7", term), "")
        refused = (
            # From a page nobody reviewed, or from the related record itself: no chain.
            ("/odoo/discuss", customer),
            ("/odoo/res.partner/11", _el("e3", "link", "Parent", href="/odoo/res.partner/9")),
            # From a reviewed list the rule does not apply; its rows are how records open.
            ("/odoo/sales", customer),
            # Not a record link: a new record, a model path with no dot, a nested path.
            ("/odoo/sales/7", _el("e4", "link", "New", href="/odoo/res.partner/new")),
            ("/odoo/sales/7", _el("e5", "link", "Other", href="/odoo/settings/1")),
            ("/odoo/sales/7", _el("e6", "link", "Deep", href="/odoo/res.partner/11/edit")),
            ("/odoo/sales/7", _el("e7", "link", "Window", href="/odoo/res.partner/11",
                                  target="_blank")),
        )
        for page, element in refused:
            with self.subTest(page=page, element=element.name):
                self.assertTrue(self.decide(page, element))
        # Only clicks: opening it by address is not what the review cleared.
        self.assertFalse(self.policy.allows_destination("/odoo/res.partner/11"))

    def test_a_reviewed_page_opens_by_address_with_nothing_but_its_view(self):
        for url in ("/odoo/sales", "/odoo/sales?view_type=kanban", "/odoo/sales/7",
                    "/odoo/action-286"):
            with self.subTest(url=url):
                self.assertTrue(self.policy.allows_destination(url))
        for url in ("/odoo/sales?debug=1", "/odoo/sales?view_type=kanban&debug=1",
                    "/odoo/settings", "/odoo/sales/new", "/odoo/sales-7", "//evil/x",
                    "http://odoo.test/odoo/sales", "/odoo/sales#x", "/odoo/sales?view_type=x"):
            with self.subTest(url=url):
                self.assertFalse(self.policy.allows_destination(url))

    def test_an_unresolved_action_is_not_reviewed(self):
        unresolved = OdooReviewedClickPolicy.from_review(load_review(), {})
        element = _el("e1", "button", "List")
        self.assertTrue(unresolved.decide(_obs("/odoo/action-286", element), element))


def _catalog(actions):
    """A runtime descriptor catalog payload carrying exactly `actions`."""
    from web_operation_agent_odoo.server.descriptors import _catalog_digest

    models = [{"model": "account.payment.term", "label": "Payment Terms",
               "operations": ["read"], "transient": False}]
    rows = [{"action_id": action_id, "xml_id": xml_id, "name": xml_id, "route": route,
             "model": "account.payment.term", "readable": True, "view_modes": ["list"],
             "breadcrumbs": [], "domain": "", "context": ""}
            for xml_id, action_id, route in actions]
    return {"schema_version": 1, "odoo_release": "18.0", "language": "en_US",
            "company_id": 1, "module_digest": "sha256:" + "0" * 64,
            "descriptor_digest": _catalog_digest(models, rows, [], []),
            "models": models, "actions": rows, "omitted_models": [], "omitted_actions": []}


class ReviewedActionResolution(unittest.TestCase):
    """How a reviewed XML ID becomes this database's action number, for whoever is asking."""

    def test_an_ordinary_user_still_gets_the_pages_their_menus_reach(self):
        # Reading ir.model.data needs Settings rights, which a salesperson does not have.
        adapter = SkillRoutesOnOdoo._adapter(
            self, ["/odoo/sales"], registry_readable=False,
            in_menus=(("account.action_payment_term_form", 286, "payment-terms"),))
        policy = adapter.click_policy()
        self.assertTrue(policy.reviewed("/odoo/action-286"))
        self.assertTrue(policy.reviewed("/odoo/payment-terms"))
        self.assertFalse(policy.reviewed("/odoo/action-300"))

    def test_a_page_with_no_menu_entry_is_resolved_for_whoever_may_read_the_registry(self):
        adapter = SkillRoutesOnOdoo._adapter(self, ["/odoo/sales"], in_menus=())
        policy = adapter.click_policy()
        self.assertTrue(policy.reviewed("/odoo/action-286"))

    def test_neither_source_leaves_the_policy_claiming_pages_it_could_not_confirm(self):
        adapter = SkillRoutesOnOdoo._adapter(
            self, ["/odoo/sales"], registry_readable=False, in_menus=())
        policy = adapter.click_policy()
        self.assertFalse(policy.reviewed("/odoo/action-286"))
        self.assertTrue(policy.reviewed("/odoo/sales"))


class SkillRoutesOnOdoo(unittest.TestCase):
    """A Skill's routes on Odoo must be pages reviewed as read-only on this database."""

    def _adapter(self, routes, *, registry_readable=True, in_menus=()):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from web_operation_agent.business.runtime import BusinessSkill
        from web_operation_agent_odoo.adapter import OdooApplicationAdapter
        from web_operation_agent_odoo.tests.test_adapter import BASE, _key_file

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        skill = BusinessSkill.from_bytes(json.dumps({
            "schema_version": 1, "skill_id": "eval.terms", "title": "Terms",
            "intent": "Open payment terms.", "triggers": ["payment terms"],
            "instructions": ["Open the Payment Terms list."],
            "tool_names": ["query_products"],
            "provenance": ["src:odoo@test:skills/terms.json"], "read_only": True,
            "routes": routes}).encode(), artifact_uri="memory://terms")
        adapter = OdooApplicationAdapter(base_url=BASE, api_key_path=_key_file(Path(tmp.name)),
                                         extra_skills=(skill,))
        def call(model, method, *_args, **_kwargs):
            if model == "woa.runtime.descriptor":
                return _catalog(in_menus)
            if not registry_readable:
                raise BusinessToolPermissionError("ordinary users cannot read the registry")
            if model == "ir.model.data":
                return [{"module": "account", "name": "action_payment_term_form",
                         "res_id": 286},
                        {"module": "account", "name": "not_reviewed", "res_id": 5}]
            return [{"id": 286, "path": False}]

        patcher = mock.patch.object(adapter._executor, "call", side_effect=call)
        patcher.start()
        self.addCleanup(patcher.stop)
        return adapter

    def test_a_reviewed_route_composes(self):
        policy = self._adapter(["/odoo/action-286"]).click_policy()
        self.assertTrue(policy.destination_allowed("/odoo/action-286"))
        # Only the exact reviewed pair resolves, not everything its module and name match.
        self.assertNotIn("5", policy.actions)

    def test_an_unreviewed_action_route_refuses_the_composition(self):
        with self.assertRaisesRegex(ValueError, "not reviewed read-only pages"):
            self._adapter(["/odoo/action-999"]).click_policy()


if __name__ == "__main__":
    unittest.main()
