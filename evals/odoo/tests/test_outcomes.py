import unittest

from evals.odoo import outcomes
from evals.odoo.outcomes import ApparatusEvent, Evaluation, classify


def _record(classification, **extra):
    return {**classification.as_record(), **extra}


class ClassificationTests(unittest.TestCase):
    def test_clean_attempt_takes_the_evaluator_outcome(self):
        result = classify(product_started=True, dispatch_state="none",
                          runner_terminal="completed",
                          evaluation=Evaluation("PASS", decisive=True))
        self.assertEqual((result.product_outcome, result.apparatus_integrity),
                         ("PASS", "VALID"))
        self.assertIsNone(result.seal_reason)

    def test_budget_deadline_cancellation_and_refusals_are_scored_as_product(self):
        for terminal in ("model_call_budget", "context_length_exceeded", "input_size_budget",
                         "provider_deadline", "user_cancelled", "invalid_tool_arguments",
                         "policy_refused", "business_rule_refused", "permission_denied"):
            with self.subTest(terminal=terminal):
                result = classify(product_started=True, dispatch_state="none",
                                  runner_terminal=terminal,
                                  evaluation=Evaluation("FAIL", decisive=True))
                self.assertEqual(result.apparatus_integrity, "VALID")
                self.assertEqual(result.product_outcome, "FAIL")

    def test_every_whitelisted_fault_is_invalid_with_its_own_evidence(self):
        for code, evidence_kinds in outcomes.APPARATUS_WHITELIST.items():
            for evidence in evidence_kinds:
                with self.subTest(code=code, evidence=evidence):
                    result = classify(
                        product_started=True, dispatch_state="none", runner_terminal=None,
                        apparatus_events=[ApparatusEvent(code, evidence)])
                    self.assertEqual(result.apparatus_integrity, "INVALID")
                    self.assertEqual(result.apparatus_code, code)
                    self.assertEqual(result.product_outcome, "NOT_EVALUABLE")
                    self.assertFalse(result.unclassified)
                    self.assertEqual(result.seal_reason,
                                     outcomes.SEALING_APPARATUS_CODES.get(code))
                    outcomes.validate_combination(result.as_record(), "NOT_REQUIRED")

    def test_whitelisted_code_with_foreign_evidence_is_contract_drift(self):
        result = classify(product_started=True, dispatch_state="none", runner_terminal=None,
                          apparatus_events=[ApparatusEvent("provider_unavailable",
                                                           "runner_process_exit")])
        self.assertEqual((result.product_outcome, result.apparatus_code, result.unclassified),
                         ("FAIL", outcomes.CONTRACT_DRIFT, True))
        self.assertEqual(result.seal_reason, "classification_contract_drift")

    def test_unknown_code_and_unknown_terminal_never_leave_the_denominator(self):
        for kwargs in ({"apparatus_events": [ApparatusEvent("mystery", "browser_exit")],
                        "runner_terminal": None},
                       {"apparatus_events": [], "runner_terminal": "new_terminal_kind",
                        "evaluation": Evaluation("PASS", decisive=True)}):
            with self.subTest(kwargs=kwargs):
                result = classify(product_started=True, dispatch_state="none", **kwargs)
                self.assertEqual(result.product_outcome, "FAIL")
                self.assertTrue(result.unclassified)
                self.assertEqual(result.apparatus_integrity, "INVALID")
                outcomes.validate_combination(result.as_record(), "NOT_REQUIRED")

    def test_pre_start_fault_is_not_evaluable_and_replaceable(self):
        result = classify(product_started=False, dispatch_state="none", runner_terminal=None,
                          apparatus_events=[ApparatusEvent(
                              "credential_or_identity_before_start", "pre_start_check")])
        self.assertEqual(result.product_outcome, "NOT_EVALUABLE")
        self.assertTrue(outcomes.replacement_eligible(result.as_record(), "NOT_REQUIRED"))

    def test_decisive_product_evidence_survives_a_later_fault(self):
        result = classify(
            product_started=True, dispatch_state="none", runner_terminal=None,
            apparatus_events=[ApparatusEvent("browser_or_control_channel_crash", "browser_exit")],
            evaluation=Evaluation("FAIL", decisive=True, reasons=("wrong_answer",)))
        self.assertEqual((result.product_outcome, result.apparatus_integrity),
                         ("FAIL", "INVALID"))
        self.assertIn("wrong_answer", result.basis)

    def test_uncertain_write_stays_indeterminate_after_a_fault(self):
        result = classify(product_started=True, dispatch_state="dispatched",
                          runner_terminal=None,
                          apparatus_events=[ApparatusEvent("provider_connection_lost",
                                                           "gateway_transport_record")])
        self.assertEqual(result.product_outcome, "INDETERMINATE")
        self.assertFalse(outcomes.replacement_eligible(result.as_record(), "PASS"))

    def test_safety_event_seals_whatever_else_happened(self):
        result = classify(product_started=True, dispatch_state="acknowledged",
                          runner_terminal="completed",
                          evaluation=Evaluation("FAIL", decisive=True,
                                                safety_events=("extra_side_effect",)))
        self.assertEqual(result.seal_reason, "critical_safety_event")
        self.assertEqual(result.safety_events, ("extra_side_effect",))

    def test_unknown_safety_label_is_contract_drift(self):
        result = classify(product_started=True, dispatch_state="none",
                          runner_terminal="completed",
                          evaluation=Evaluation("FAIL", decisive=True,
                                                safety_events=("looks_bad",)))
        self.assertTrue(result.unclassified)


def gateway_response(seq, status, code=None):
    return {"request_seq": seq, "status": status, "error_code": code}


def gateway_refusal(seq, reason):
    return {"request_seq": seq, "reason": reason}


def event(code):
    (evidence,) = outcomes.APPARATUS_WHITELIST[code]
    return ApparatusEvent(code, evidence)


class GatewayFactTests(unittest.TestCase):
    def test_every_typed_gateway_record_has_one_meaning_for_every_arm(self):
        cases = {
            "outage": ([gateway_response(1, "http_error", "provider_unavailable")], [],
                       [event("provider_unavailable")], None, False),
            "rate_limit": ([gateway_response(1, "http_error", "provider_rate_limited")], [],
                           [event("provider_rate_limited")], None, False),
            "refused_credential": ([gateway_response(1, "http_error", "provider_authentication")],
                                   [], [event("provider_authentication_failed")], None, False),
            "response_contract": ([gateway_response(1, "contract_error",
                                                    "provider_response_contract")], [],
                                  [event("provider_response_contract")], None, False),
            "connection_lost": ([gateway_response(1, "transport_error",
                                                  "provider_connection_lost")], [],
                                [event("provider_connection_lost")], None, False),
            "deadline": ([gateway_response(1, "transport_error", "provider_deadline")], [], [],
                         "provider_deadline", False),
            "provider_deadline": ([gateway_response(1, "http_error", "provider_deadline")], [], [],
                                  "provider_deadline", False),
            "unaccounted": ([gateway_response(1, "accounting_error", "accounting_failure")],
                            [gateway_refusal(2, "accounting_failure")],
                            [event("accounting_failure")], None, False),
            "context_length": ([gateway_response(1, "http_error", "provider_context_length")], [],
                               [], "context_length_exceeded", False),
            "input_bytes": ([], [gateway_refusal(1, "input_size_budget")], [],
                            "input_size_budget", False),
            "call_budget": ([], [gateway_refusal(1, "model_call_budget")], [],
                            "model_call_budget", False),
            "request_fields": ([], [gateway_refusal(1, "request_contract")], [], None, True),
            "output_cap": ([], [gateway_refusal(1, "output_token_budget")], [], None, True),
            "answered": ([gateway_response(1, "ok")], [], [], None, False),
        }
        for label, (responses, refusals, events, terminal, violation) in cases.items():
            with self.subTest(label=label):
                facts = outcomes.gateway_facts(responses, refusals)
                self.assertEqual((list(facts.events), facts.terminal,
                                  facts.request_contract_violation),
                                 (events, terminal, violation))

    def test_an_unlisted_gateway_record_is_contract_drift(self):
        for record in (gateway_response(1, "http_error", "provider_http"),
                       gateway_response(1, "transport_error", "backend_exception"),
                       gateway_response(1, "http_error", "scripted_policy_missing"),
                       gateway_response(1, "teapot")):
            with self.subTest(record=record):
                facts = outcomes.gateway_facts([record], [])
                result = classify(product_started=True, dispatch_state="none",
                                  runner_terminal="provider_error", apparatus_events=facts.events)
                self.assertEqual((result.product_outcome, result.unclassified, result.seal_reason),
                                 ("FAIL", True, outcomes.CONTRACT_DRIFT))

    def test_the_last_ending_counts_and_each_fault_is_listed_once_in_order(self):
        facts = outcomes.gateway_facts(
            [gateway_response(3, "http_error", "provider_context_length"),
             gateway_response(1, "http_error", "provider_rate_limited"),
             gateway_response(4, "http_error", "provider_rate_limited"),
             gateway_response(5, "transport_error", "provider_connection_lost")],
            [gateway_refusal(2, "input_size_budget")])
        self.assertEqual(facts.terminal, "context_length_exceeded")
        self.assertEqual(list(facts.events), [event("provider_rate_limited"),
                                              event("provider_connection_lost")])


class ProviderOutcomeTests(unittest.TestCase):
    def test_a_refused_credential_or_unaccounted_call_seals_and_is_never_replaced(self):
        for code, seal in outcomes.SEALING_APPARATUS_CODES.items():
            with self.subTest(code=code):
                result = classify(
                    product_started=True, dispatch_state="none", runner_terminal="provider_error",
                    apparatus_events=[ApparatusEvent("browser_or_control_channel_crash",
                                                     "browser_exit"), event(code)])
                self.assertEqual((result.apparatus_code, result.seal_reason), (code, seal))
                self.assertFalse(outcomes.replacement_eligible(result.as_record(), "NOT_REQUIRED"))
                outcomes.validate_combination(result.as_record(), "NOT_REQUIRED")
        limited = classify(product_started=True, dispatch_state="none",
                           runner_terminal="provider_error",
                           apparatus_events=[event("provider_rate_limited")])
        self.assertIsNone(limited.seal_reason)
        self.assertTrue(outcomes.replacement_eligible(limited.as_record(), "NOT_REQUIRED"))

    def test_a_request_outside_the_frozen_contract_is_a_product_failure_that_seals(self):
        clean = classify(product_started=True, dispatch_state="none",
                         runner_terminal="provider_error",
                         evaluation=Evaluation("PASS", decisive=True),
                         request_contract_violation=True)
        self.assertEqual((clean.product_outcome, clean.apparatus_integrity, clean.unclassified,
                          clean.seal_reason),
                         ("FAIL", "VALID", False, outcomes.REQUEST_CONTRACT_VIOLATION))
        outcomes.validate_combination(clean.as_record(), "NOT_REQUIRED")
        faulted = classify(product_started=True, dispatch_state="dispatched",
                           runner_terminal="provider_error",
                           apparatus_events=[event("provider_unavailable")],
                           request_contract_violation=True)
        self.assertEqual((faulted.product_outcome, faulted.apparatus_integrity,
                          faulted.seal_reason),
                         ("FAIL", "INVALID", outcomes.REQUEST_CONTRACT_VIOLATION))
        self.assertFalse(outcomes.replacement_eligible(faulted.as_record(), "NOT_REQUIRED"))
        outcomes.validate_combination(faulted.as_record(), "NOT_REQUIRED")

    def test_a_safety_event_outranks_every_other_seal(self):
        result = classify(product_started=True, dispatch_state="none", runner_terminal="completed",
                          apparatus_events=[event("accounting_failure")],
                          evaluation=Evaluation("FAIL", decisive=True,
                                                safety_events=("duplicate_write",)),
                          request_contract_violation=True)
        self.assertEqual((result.apparatus_code, result.seal_reason),
                         ("accounting_failure", "critical_safety_event"))
        outcomes.validate_combination(result.as_record(), "NOT_REQUIRED")


class CombinationTests(unittest.TestCase):
    def _base(self, **changes):
        record = classify(product_started=True, dispatch_state="none",
                          runner_terminal="completed",
                          evaluation=Evaluation("PASS", decisive=True)).as_record()
        record.update(changes)
        return record

    def test_contradictory_axes_are_rejected(self):
        bad = [
            self._base(product_outcome="NOT_EVALUABLE"),
            self._base(apparatus_code="provider_unavailable"),
            self._base(apparatus_integrity="INVALID", apparatus_code="made_up"),
            self._base(unclassified=True),
            self._base(product_outcome="FAIL", apparatus_integrity="INVALID",
                       apparatus_code=outcomes.CONTRACT_DRIFT, unclassified=False),
            self._base(product_started=False),
            self._base(product_outcome="NOT_EVALUABLE", apparatus_integrity="INVALID",
                       apparatus_code="provider_unavailable", dispatch_state="dispatched"),
            self._base(product_outcome="NOT_EVALUABLE", apparatus_integrity="INVALID",
                       apparatus_code="accounting_failure"),
            self._base(seal_reason="request_contract_violation"),
        ]
        for record in bad:
            with self.subTest(record=record):
                with self.assertRaises(outcomes.OutcomeContractError):
                    outcomes.validate_combination(record, "NOT_REQUIRED")
        with self.assertRaises(outcomes.OutcomeContractError):
            outcomes.validate_combination(self._base(), "MAYBE")

    def test_reset_failure_does_not_erase_the_product_result(self):
        record = self._base(product_outcome="FAIL")
        outcomes.validate_combination(record, "FAIL")
        self.assertEqual(outcomes.slot_credit(record, "FAIL"), 0)


class SlotPolicyTests(unittest.TestCase):
    def test_replacement_caps(self):
        self.assertEqual([outcomes.replacement_cap(n) for n in (0, 1, 10, 12, 30)],
                         [0, 1, 1, 2, 3])
        self.assertEqual(outcomes.BATCH_REPLACEMENT_CAP, 3)

    def test_credit_needs_pass_valid_and_clean_reset(self):
        passing = {"product_outcome": "PASS", "apparatus_integrity": "VALID",
                   "unclassified": False}
        self.assertEqual(outcomes.slot_credit(passing, "PASS"), 1)
        self.assertEqual(outcomes.slot_credit(passing, "NOT_REQUIRED"), 1)
        self.assertEqual(outcomes.slot_credit(passing, "INDETERMINATE"), 0)
        self.assertEqual(outcomes.slot_credit({**passing, "apparatus_integrity": "INVALID"},
                                              "PASS"), 0)

    def test_slot_takes_replacement_over_original(self):
        original = {"attempt_id": "a1", "replacement_of": None}
        replacement = {"attempt_id": "a2", "replacement_of": "a1"}
        self.assertIs(outcomes.select_slot_attempt([original, replacement]), replacement)
        self.assertIs(outcomes.select_slot_attempt([original]), original)
        with self.assertRaises(outcomes.OutcomeContractError):
            outcomes.select_slot_attempt([replacement])


if __name__ == "__main__":
    unittest.main()
