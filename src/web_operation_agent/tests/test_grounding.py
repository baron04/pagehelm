"""Behavioral boundaries for the plan state machine.

The cases distinguish a page that is simply on the way, a step that cannot yet be judged, and
a page that positively contradicts the plan.
"""
import unittest

from web_operation_agent.grounding import (NOT_APPLICABLE, REFUTED, SATISFIED, WRITE_NONE,
                                           WRITE_PERMITTED, WRITE_REFUSED, PlanState)
from web_operation_agent.schemas import (SCHEMA_VERSIONS, ActionReceipt, ElementRef,
                                         OperationContract, PageObservation)


def contract(steps, mutation=None) -> OperationContract:
    return OperationContract.from_json({
        "schema_version": SCHEMA_VERSIONS["OperationContract"], "task_ref": "T",
        "summary": "s", "steps": steps, "mutation": mutation, "citations": ["a.xml:1-2"]})


def page(url="http://app/accounting/control/main", title="Accounting Manager",
         landmarks=("Accounting Manager",), messages=()) -> PageObservation:
    return PageObservation(schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=1,
                           url=url, title=title, landmarks=list(landmarks),
                           elements=[ElementRef("e1", "link", "Payments", "a", 1)],
                           messages=list(messages))


def receipt(ok=True, navigated=True, url_after="http://app/x") -> ActionReceipt:
    return ActionReceipt(schema_version=SCHEMA_VERSIONS["ActionReceipt"], command_id="c1",
                         action="click", args_echo={"ref": "e1"}, ok=ok,
                         url_before="http://app/before", url_after=url_after,
                         navigated=navigated)


STEPS = [
    {"intent": "open the payments list", "webapp": "accounting", "request": "findPayments",
     "landmark": "Find Payment", "citations": ["a.xml:1-2"]},
    {"intent": "open the applications tab", "webapp": "accounting",
     "request": "editPaymentApplications", "landmark": "Applied Payments",
     "citations": ["a.xml:1-2"]},
]
MUTATION = {"webapp": "accounting", "request": "createPaymentApplication",
            "citations": ["a.xml:1-2"]}


class OnTheWayIsNotAContradiction(unittest.TestCase):
    def test_the_applications_own_home_page_is_not_a_conflict(self):
        """An intermediate application page provides no evidence against a later step."""
        st = PlanState.of(contract(STEPS))
        event = st.observe(page(), receipt(), WRITE_NONE)
        self.assertEqual(event["verdict"], NOT_APPLICABLE)
        self.assertEqual(st.refuted_steps, [])
        self.assertFalse(st.withdrawn)
        self.assertEqual(st.standing_notes, [])

    def test_an_unjudgeable_page_leaves_the_cursor_where_it_was(self):
        st = PlanState.of(contract(STEPS))
        for _ in range(5):
            st.observe(page(), receipt(), WRITE_NONE)
        self.assertEqual(st.current.step.intent, "open the payments list")
        self.assertEqual(st.counts()[REFUTED], 0)

    def test_absence_of_a_landmark_alone_never_withdraws_a_plan(self):
        st = PlanState.of(contract(STEPS, MUTATION))
        for _ in range(20):
            st.observe(page(), receipt(), WRITE_NONE)
        self.assertFalse(st.withdrawn, "a plan must not be discarded for want of evidence")


class Confirmation(unittest.TestCase):
    def test_the_landmark_confirms_the_step(self):
        st = PlanState.of(contract([{**STEPS[0], "request": ""}, STEPS[1]]))
        event = st.observe(page(title="Find Payment", landmarks=["Find Payment"]),
                           receipt(), WRITE_NONE)
        self.assertEqual(event["verdict"], SATISFIED)
        self.assertEqual(st.current.step.intent, "open the applications tab")

    def test_arriving_at_the_named_request_confirms_it_without_the_landmark(self):
        """The request is the fact; the landmark is a guess about how it will look."""
        st = PlanState.of(contract(STEPS))
        event = st.observe(page(url="http://app/accounting/control/findPayments",
                                title="Something Else", landmarks=["Nothing Familiar"]),
                           receipt(), WRITE_NONE)
        self.assertEqual(event["verdict"], SATISFIED)


class Refutation(unittest.TestCase):
    def test_a_refused_write_on_the_step_the_plan_named_refutes_it(self):
        st = PlanState.of(contract(STEPS, MUTATION))
        st.observe(page(url="http://app/accounting/control/findPayments"), receipt(), WRITE_NONE)
        st.observe(page(url="http://app/accounting/control/editPaymentApplications"),
                   receipt(), WRITE_NONE)
        event = st.observe(page(url="http://app/accounting/control/createPaymentApplication"),
                           receipt(), WRITE_REFUSED,
                           refused_requests=["accounting/createPaymentApplication"])
        self.assertEqual(event["verdict"], REFUTED)
        self.assertTrue(st.standing_notes)

    def test_a_page_that_says_no_refutes_the_step_it_was_supposed_to_satisfy(self):
        st = PlanState.of(contract(STEPS))
        event = st.observe(page(url="http://app/accounting/control/findPayments",
                                title="Action refused", landmarks=["Action refused"]),
                           receipt(), WRITE_NONE)
        self.assertEqual(event["verdict"], REFUTED)
        self.assertIn("refusal", event["why"])

    def test_a_refusal_on_a_failed_action_is_not_attributed_to_the_step(self):
        """If the action itself failed, the page it left behind proves nothing about the plan."""
        st = PlanState.of(contract(STEPS))
        event = st.observe(page(title="Action refused", landmarks=["Action refused"]),
                           receipt(ok=False), WRITE_NONE)
        self.assertEqual(event["verdict"], NOT_APPLICABLE)


class Withdrawal(unittest.TestCase):
    def test_a_refused_write_endpoint_withdraws_the_plan(self):
        st = PlanState.of(contract(STEPS, MUTATION))
        event = st.refute_mutation("the application refused it")
        self.assertEqual(event["verdict"], REFUTED)
        self.assertTrue(st.withdrawn)
        self.assertIn("write endpoint", st.withdrawn_because)

    def test_one_refused_write_is_enough_to_withdraw(self):
        """A refusal is a fact from the enforcement point, not an inference from a page.

        Waiting for an identical refusal adds no evidence and risks repeating the same write.
        """
        st = PlanState.of(contract(STEPS, MUTATION))
        st.observe(page(url="http://app/accounting/control/editInvoiceApplications"),
                   receipt(), WRITE_REFUSED,
                   refused_requests=["accounting/createPaymentApplication"])
        self.assertTrue(st.withdrawn)
        self.assertIn("refused", st.withdrawn_because)

    def test_two_contradicted_steps_withdraw_the_plan(self):
        st = PlanState.of(contract(STEPS, MUTATION))
        st.observe(page(url="http://app/accounting/control/findPayments",
                        title="Action refused"), receipt(), WRITE_NONE)
        self.assertFalse(st.withdrawn, "one wrong step is survivable")
        st.observe(page(url="http://app/accounting/control/editPaymentApplications",
                        title="Action refused"), receipt(), WRITE_NONE)
        self.assertTrue(st.withdrawn)
        self.assertIn("2 of its steps", st.withdrawn_because)

    def test_a_withdrawn_plan_stops_judging(self):
        st = PlanState.of(contract(STEPS, MUTATION))
        st.refute_mutation("refused")
        event = st.observe(page(title="Find Payment"), receipt(), WRITE_NONE)
        self.assertEqual(event["verdict"], "no_active_step")

    def test_withdrawal_is_announced_once_and_says_what_to_do(self):
        st = PlanState.of(contract(STEPS, MUTATION))
        st.refute_mutation("refused")
        st.refute_mutation("refused again")
        withdrawals = [n for n in st.standing_notes if "withdrawn" in n]
        self.assertEqual(len(withdrawals), 1)
        self.assertIn("work from what the pages actually offer", withdrawals[0])

    def test_a_refusal_it_cannot_place_does_not_blame_an_unrelated_step(self):
        """An AJAX refusal without request identity cannot be assigned to the current step."""
        st = PlanState.of(contract(STEPS, MUTATION))
        event = st.observe(page(url="http://app/accounting/control/editInvoiceApplications"),
                           receipt(), WRITE_REFUSED)
        self.assertEqual(event["verdict"], NOT_APPLICABLE)
        self.assertFalse(st.withdrawn)
        self.assertEqual(event["step"], "(unattributed)")
        self.assertEqual(st.counts()[REFUTED], 0, "no step may be marked wrong on a guess")
        self.assertEqual(st.unattributed_refusals, 1)
        self.assertIn("identity does not establish", st.standing_notes[0])

    def test_a_page_text_refutation_alone_still_needs_two(self):
        """An inference from page text is weaker evidence than a refusal, and one wrong
        intermediate step does not condemn a plan that may still be right about the rest."""
        st = PlanState.of(contract(STEPS, MUTATION))
        st.observe(page(url="http://app/accounting/control/findPayments",
                        title="Action refused"), receipt(), WRITE_NONE)
        self.assertFalse(st.withdrawn)
        st.observe(page(url="http://app/accounting/control/editPaymentApplications",
                        title="Action refused"), receipt(), WRITE_NONE)
        self.assertTrue(st.withdrawn)

    def test_a_permitted_write_never_refutes_anything(self):
        st = PlanState.of(contract(STEPS, MUTATION))
        for _ in range(3):
            st.observe(page(), receipt(), WRITE_PERMITTED)
        self.assertFalse(st.withdrawn)
        self.assertEqual(st.counts()[REFUTED], 0)


if __name__ == "__main__":
    unittest.main()
