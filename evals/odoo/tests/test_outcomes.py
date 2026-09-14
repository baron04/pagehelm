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
        for terminal in ("model_call_budget", "context_length_exceeded", "provider_deadline",
                         "user_cancelled", "invalid_tool_arguments", "policy_refused",
                         "business_rule_refused", "permission_denied"):
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
                    outcomes.validate_combination(result.as_record(), "NOT_REQUIRED")

    def test_whitelisted_code_with_foreign_evidence_is_contract_drift(self):
        result = classify(product_started=True, dispatch_state="none", runner_terminal=None,
                          apparatus_events=[ApparatusEvent("provider_http_5xx",
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
            self._base(apparatus_code="provider_http_5xx"),
            self._base(apparatus_integrity="INVALID", apparatus_code="made_up"),
            self._base(unclassified=True),
            self._base(product_outcome="FAIL", apparatus_integrity="INVALID",
                       apparatus_code=outcomes.CONTRACT_DRIFT, unclassified=False),
            self._base(product_started=False),
            self._base(product_outcome="NOT_EVALUABLE", apparatus_integrity="INVALID",
                       apparatus_code="provider_http_5xx", dispatch_state="dispatched"),
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
