"""The Odoo assistant may click only reviewed read-only controls, and nothing else."""
from __future__ import annotations

import unittest

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
                        _el("e5", "checkbox"), _el("e6", "td", "S00007"),
                        _el("e7", "button", "Activate", name="button_immediate_install")):
            with self.subTest(element=element.name or element.role):
                self.assertTrue(self.decide("/odoo/apps", element))

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

    def test_an_unresolved_action_is_not_reviewed(self):
        unresolved = OdooReviewedClickPolicy.from_review(load_review(), {})
        element = _el("e1", "button", "List")
        self.assertTrue(unresolved.decide(_obs("/odoo/action-286", element), element))


if __name__ == "__main__":
    unittest.main()
