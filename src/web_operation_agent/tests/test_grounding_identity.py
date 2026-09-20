"""A plan step is judged only when it is applicable, attributable and attempted.

The deterministic cases distinguish arriving from being allowed, repeated observation from
distinct attempts, alternative entry candidates from sequential steps, and one application's
request from another application's request with the same name.
"""
import unittest

from web_operation_agent.grounding import (NOT_APPLICABLE, REFUTED, SATISFIED, WRITE_NONE,
                                           WRITE_REFUSED, PlanState)
from web_operation_agent.tests.route_fixture import (FIXTURE_ROUTE_PORT,
                                                     fixture_route_identity as route_of)
from web_operation_agent.schemas import (SCHEMA_VERSIONS, ActionReceipt, ElementRef,
                                         OperationContract, PageObservation)


def _plan_state(value):
    return PlanState.of(value, route_identity=route_of)


def contract(steps=(), candidates=(), mutation=None) -> OperationContract:
    return OperationContract.from_json({
        "schema_version": SCHEMA_VERSIONS["OperationContract"], "task_ref": "T",
        "summary": "s", "entry_candidates": list(candidates), "steps": list(steps),
        "mutation": mutation, "citations": ["a.xml:1-2"]})


def page(url="http://app/accounting/do/main", title="Accounting Manager",
         landmarks=(), messages=()) -> PageObservation:
    return PageObservation(schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=1, url=url,
                           title=title, landmarks=list(landmarks) or [title],
                           elements=[ElementRef("e1", "link", "Payments", "a", 1)],
                           messages=list(messages))


def receipt(action="click", ok=True, url_after="http://app/x") -> ActionReceipt:
    return ActionReceipt(schema_version=SCHEMA_VERSIONS["ActionReceipt"], command_id="c1",
                         action=action, args_echo={"ref": "e1"}, ok=ok,
                         url_before="http://app/before", url_after=url_after, navigated=True)


STEP_A = {"intent": "open the payments list", "webapp": "accounting",
          "request": "findPayments", "landmark": "Find Payment", "citations": ["a.xml:1-2"]}
STEP_B = {"intent": "open the applications tab", "webapp": "accounting",
          "request": "editPaymentApplications", "landmark": "Applied Payments",
          "citations": ["a.xml:1-2"]}


class ArrivingIsNotBeingAllowed(unittest.TestCase):
    """A named route that refuses the operation refutes rather than satisfies the step."""

    def _observe(self):
        st = _plan_state(contract([STEP_A, STEP_B]))
        return st, st.observe(page(url="http://app/accounting/do/findPayments",
                                   title="Access denied",
                                   landmarks=["Access denied"]), receipt(), WRITE_NONE)

    def test_the_step_is_not_satisfied(self):
        _, event = self._observe()
        self.assertNotEqual(event["verdict"], SATISFIED)

    def test_a_denial_on_the_step_s_own_route_contradicts_it(self):
        """The error is about this step's request, so it is evidence about this step."""
        _, event = self._observe()
        self.assertEqual(event["verdict"], REFUTED)

    def test_the_cursor_does_not_advance_past_it_as_though_it_worked(self):
        st, _ = self._observe()
        self.assertNotEqual(st.counts()[SATISFIED], 1)


class CompoundRouteIdentity(unittest.TestCase):
    def test_the_full_multi_segment_request_is_preserved(self):
        self.assertEqual(route_of(
            "http://app/workeffort/do/WorkEffortPartyAssigns/Edit?workEffortId=42"),
            ("workeffort", "workeffortpartyassigns/edit"))

    def test_a_trailing_slash_is_not_part_of_the_request(self):
        self.assertEqual(route_of("/beacon/do/LampSchedule/create/"),
                         ("beacon", "lampschedule/create"))


class TheSameObservationIsOnePieceOfEvidence(unittest.TestCase):
    """Repeatedly observing one unrelated refusal cannot refute unattempted steps."""

    def test_observing_twice_does_not_refute_two_steps(self):
        st = _plan_state(contract([STEP_A, STEP_B], mutation={
            "webapp": "accounting", "request": "createPaymentApplication",
            "citations": ["a.xml:1-2"]}))
        p = page(url="http://app/accounting/do/somewhereElse", title="Action refused")
        st.observe(p, receipt(action="observe"), WRITE_NONE)
        st.observe(p, receipt(action="observe"), WRITE_NONE)
        self.assertLessEqual(st.counts()[REFUTED], 1)
        self.assertFalse(st.withdrawn, "one page cannot condemn a plan twice over")

    def test_an_observe_attempts_nothing_so_it_refutes_nothing(self):
        """`observe` re-reads the page. It cannot be the attempt that proves a step wrong."""
        st = _plan_state(contract([STEP_A]))
        event = st.observe(page(title="Action refused"), receipt(action="observe"), WRITE_NONE)
        self.assertEqual(event["verdict"], NOT_APPLICABLE)


class CandidatesAreAlternatives(unittest.TestCase):
    """Reaching any entry candidate satisfies entry regardless of declaration order."""

    CANDIDATES = [
        {"intent": "enter via the payment lookup", "webapp": "accounting",
         "request": "findPayments", "landmark": "Find Payment", "citations": ["a.xml:1-2"]},
        {"intent": "enter via the invoice lookup", "webapp": "accounting",
         "request": "findInvoices", "landmark": "Find Invoice", "citations": ["a.xml:1-2"]},
    ]

    def test_denied_second_candidate_does_not_discard_the_first(self):
        st = _plan_state(contract([STEP_B], candidates=self.CANDIDATES))
        event = st.observe(page(url="http://app/accounting/do/findInvoices",
                                title="Access denied"), receipt(), WRITE_NONE)
        self.assertEqual(event["verdict"], REFUTED)
        self.assertFalse(st.entry_reached)
        self.assertEqual([c.status for c in st.candidates], ["pending", REFUTED])

    def test_matching_landmark_on_another_named_route_is_not_arrival(self):
        st = _plan_state(contract([STEP_A]))
        event = st.observe(page(url="http://app/beta/do/findPayments",
                                title="Find Payment"), receipt(), WRITE_NONE)
        self.assertEqual(event["verdict"], NOT_APPLICABLE)


class RefusalIdentity(unittest.TestCase):
    def test_another_webapps_refusal_never_refutes_mutation_even_after_steps_end(self):
        for steps in ([], [STEP_A]):
            st = _plan_state(contract(steps, mutation={"webapp": "alpha", "request": "edit"}))
            event = st.observe(page(url="http://app/beta/do/edit"),
                               receipt(url_after="http://app/alpha/do/edit"),
                               WRITE_REFUSED, refused_requests=["beta/edit"])
            self.assertEqual(event["verdict"], NOT_APPLICABLE)
            self.assertFalse(st.withdrawn)

    def test_the_request_identity_can_refute_even_when_ajax_leaves_the_url_unchanged(self):
        st = _plan_state(contract([STEP_A], mutation={"webapp": "alpha", "request": "edit"}))
        event = st.observe(page(), receipt(), WRITE_REFUSED, refused_requests=["alpha/edit"])
        self.assertEqual(event["verdict"], REFUTED)
        self.assertTrue(st.withdrawn)


class SuccessfulCandidates(unittest.TestCase):
    CANDIDATES = CandidatesAreAlternatives.CANDIDATES

    def test_reaching_the_second_candidate_satisfies_the_entry(self):
        st = _plan_state(contract([STEP_B], candidates=self.CANDIDATES))
        event = st.observe(page(url="http://app/accounting/do/findInvoices",
                                title="Find Invoice"), receipt(), WRITE_NONE)
        self.assertEqual(event["verdict"], SATISFIED)

    def test_the_unused_candidate_is_not_left_pending_or_wrong(self):
        st = _plan_state(contract([STEP_B], candidates=self.CANDIDATES))
        st.observe(page(url="http://app/accounting/do/findInvoices", title="Find Invoice"),
                   receipt(), WRITE_NONE)
        self.assertEqual(st.counts()[REFUTED], 0, "not taking a route does not disprove it")
        self.assertIsNotNone(st.current)
        self.assertEqual(st.current.step.intent, STEP_B["intent"],
                         "the ordered steps begin once any candidate has been reached")

    def test_a_candidate_that_is_never_reached_does_not_block_the_steps(self):
        st = _plan_state(contract([STEP_B], candidates=self.CANDIDATES))
        st.observe(page(url="http://app/accounting/do/findPayments", title="Find Payment"),
                   receipt(), WRITE_NONE)
        self.assertEqual(st.current.step.intent, STEP_B["intent"])


def _key(idx, app):
    """The citation key the index actually built for one application's controller."""
    return next(k for k in idx.requests if k.startswith(f"{app}/controller.xml"))


class WebappIdentityInCitations(unittest.TestCase):
    """A citation for one application's `edit` request cannot support another's."""

    @staticmethod
    def _map():
        from web_operation_agent.schemas import (SCHEMA_VERSIONS, EvidenceRef, SourceMap,
                                                 SourceRelation)

        def rel(subject, obj, path, lines):
            ref = EvidenceRef(repository_id="r", release="rel", path=path,
                              start_line=lines[0], end_line=lines[1], sha256="0" * 64)
            return SourceRelation(kind="request_handler", subject=subject, object=obj,
                                  evidence=(ref,))
        return SourceMap(schema_version=SCHEMA_VERSIONS["SourceMap"], attestation_id="x",
                         repository_id="r", release="rel",
                         relations=[rel("alpha/edit", "alpha.EditSvc", "alpha/controller.xml",
                                        (10, 12)),
                                    rel("beta/edit", "beta.EditSvc", "beta/controller.xml",
                                        (20, 22))])

    def test_its_own_applications_evidence_supports_it(self):
        from web_operation_agent.planning.contract import SupportIndex, _segment_from_template
        idx = SupportIndex(self._map(),
                           segment=_segment_from_template(FIXTURE_ROUTE_PORT.contract_route_template))
        self.assertTrue(idx.supports([_key(idx, "beta")], "edit", "beta"))

    def test_a_citation_about_another_application_does_not_support_the_step(self):
        """Request identity includes the application when names overlap."""
        from web_operation_agent.planning.contract import SupportIndex, _segment_from_template
        idx = SupportIndex(self._map(),
                           segment=_segment_from_template(FIXTURE_ROUTE_PORT.contract_route_template))
        self.assertFalse(idx.supports([_key(idx, "alpha")], "edit", "beta"),
                         "alpha's evidence is not evidence about beta's request")


if __name__ == "__main__":
    unittest.main()
