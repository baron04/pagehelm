"""A REST/plain-HTML adapter qualifies exact proposal-to-form conversion."""
from dataclasses import replace
import json
import unittest

from testapps.contacts.adapter import ContactsMutationPrefill, MinimalContactsAdapter
from testapps.contacts.tests.test_generic_forms import _context
from web_operation_agent.business.tools.contracts import BusinessMutationProposal
from web_operation_agent.execution.model_loop_contracts import PageActionOutcome, PageProcedureContext
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation


ORIGIN = "http://127.0.0.1:1"
SCOPE = "form:contact-create"
INSTANCE = "contact-form-one"


def _page(epoch, route, values=None):
    if route == "/contacts":
        elements = [ElementRef("e1", "link", "New contact", "a", epoch,
                               {"href": "/contacts/new"})]
    else:
        values = values or {"Name": "", "Email": ""}
        elements = [ElementRef(f"e{index}", "textbox", label, "input", epoch,
                               {"field": label.lower(), "form_scope": SCOPE,
                                "form_instance": INSTANCE, "value": values[label]})
                    for index, label in enumerate(("Name", "Email"), 1)]
        elements.append(ElementRef("e3", "button", "Save contact", "button", epoch,
                                   {"form_scope": SCOPE, "form_instance": INSTANCE}))
    return PageObservation(1, epoch, ORIGIN + route, "Contacts", [], elements,
                           "synthetic contacts", render_state="ready", page_route=route)


class _Page:
    def __init__(self):
        self.current = _page(1, "/contacts")
        self.actions = []

    def observation(self):
        return self.current

    def checkpoint(self):
        pass

    def stopped(self):
        return False

    def action_observations(self):
        return (self.current,)

    def execute(self, action, arguments, *, navigating=False):
        before = self.current
        if action == "click":
            assert before.page_route == "/contacts" and arguments == {"ref": "e1"}
            self.current = _page(before.epoch + 1, "/contacts/new")
            echo = {**arguments, "epoch": before.epoch}
        elif action == "fill":
            assert before.page_route == "/contacts/new"
            labels = {"e1": "Name", "e2": "Email"}
            shown = {e.attrs["field"].title(): e.attrs["value"] for e in before.elements
                     if e.role == "textbox"}
            shown[labels[arguments["ref"]]] = arguments["value"]
            self.current = _page(before.epoch + 1, "/contacts/new", shown)
            echo = {**arguments, "epoch": before.epoch}
        elif action == "handoff_form":
            assert before.page_route == "/contacts/new"
            self.current = _page(before.epoch + 1, "/contacts/new",
                {e.attrs["field"].title(): e.attrs["value"] for e in before.elements
                 if e.role == "textbox"})
            echo = arguments
        else:
            raise AssertionError(action)
        self.actions.append(action)
        receipt = ActionReceipt(1, f"command-{len(self.actions)}", action, echo, True,
                                observation=self.current, url_before=before.url,
                                url_after=self.current.url, write_guard_held=True)
        return PageActionOutcome("completed", "acknowledged",
                                 receipt_json=json.dumps(receipt.to_json()))

    def handoff(self, fields, submit):
        return self.execute("handoff_form", {"fields": fields, "submit": submit})


class ContactsMutationPrefillTests(unittest.TestCase):
    def setUp(self):
        self.adapter = MinimalContactsAdapter(ORIGIN, api_token="synthetic-delegation")
        self.port = ContactsMutationPrefill(self.adapter)
        self.operation = _context()
        self.proposal = BusinessMutationProposal(
            "create_contact", {"name": "Synthetic Ada", "email": "ada@example.test"},
            "Create exactly one contact", ("Synthetic Ada",), "1" * 32,
            operation_context_digest=self.operation.binding_digest)

    def test_exact_offer_uses_reviewed_form_and_manual_handoff(self):
        offer = self.port.offer(self.proposal, context=self.operation,
                                observation=_page(1, "/contacts"))
        self.assertEqual((offer.target, offer.fields), ("/contacts/new",
            (("name", "Synthetic Ada"), ("email", "ada@example.test"))))
        page = _Page()
        events = []
        context = PageProcedureContext(self.operation, page, lambda _: None,
                                       lambda *args, **kw: events.append((args, kw)),
                                       lambda _: None)
        result = self.port.handoff(offer, self.proposal, context=context)
        self.assertEqual((result.state, result.marked, page.actions),
                         ("completed", "Save contact",
                          ["click", "fill", "fill", "handoff_form"]))
        self.assertEqual([e.attrs.get("value") for e in page.current.elements[:2]],
                         ["Synthetic Ada", "ada@example.test"])
        self.assertTrue(any(args == ("page_handoff_ready",) for args, _ in events))

    def test_wrong_offer_is_rejected_before_any_page_action(self):
        offer = self.port.offer(self.proposal, context=self.operation,
                                observation=_page(1, "/contacts"))
        wrong = replace(offer, target="/contacts/another")
        page = _Page()
        context = PageProcedureContext(self.operation, page, lambda _: None,
                                       lambda *a, **k: None, lambda _: None)
        result = self.port.handoff(wrong, self.proposal, context=context)
        self.assertEqual((result.state, page.actions), ("unverified", []))

    def test_unreviewed_page_does_not_offer_and_changed_page_does_not_navigate(self):
        self.assertIsNone(self.port.offer(self.proposal, context=self.operation,
                                          observation=_page(1, "/contacts/new")))
        offer = self.port.offer(self.proposal, context=self.operation,
                                observation=_page(1, "/contacts"))
        page = _Page()
        page.current = _page(2, "/contacts/new")
        context = PageProcedureContext(self.operation, page, lambda _: None,
                                       lambda *a, **k: None, lambda _: None)
        result = self.port.handoff(offer, self.proposal, context=context)
        self.assertEqual((result.state, page.actions), ("unverified", []))


if __name__ == "__main__":
    unittest.main()
