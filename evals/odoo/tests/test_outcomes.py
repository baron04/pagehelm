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
                    # The ending a runner reports beside each fault: none where it lost sight of
                    # the product, the generic provider failure beside a provider record, and an
                    # ordinary ending otherwise.
                    terminal = (None if evidence in outcomes.TERMINAL_LOSS_EVIDENCE
                                else "provider_error" if evidence in outcomes.PROVIDER_EVIDENCE
                                else "completed")
                    result = classify(
                        product_started=True, dispatch_state="none", runner_terminal=terminal,
                        apparatus_events=[ApparatusEvent(code, evidence)],
                        provider_record_in_turn=True)
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
                          runner_terminal="provider_error", provider_record_in_turn=True,
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


class FaultEvidenceTests(unittest.TestCase):
    """The report's items 3 and 4: facts established before a fault survive it, and a fault
    unrelated to the provider never explains an ending nobody mapped."""

    def test_only_a_provider_record_explains_an_unmapped_terminal(self):
        for unrelated in (ApparatusEvent("evaluator_exception", "evaluator_process_exit"),
                          ApparatusEvent("browser_or_control_channel_crash",
                                         "control_channel_error"),
                          ApparatusEvent("fixture_baseline_unstable", "fixture_capture")):
            with self.subTest(code=unrelated.code):
                result = classify(product_started=True, dispatch_state="none",
                                  runner_terminal="terminal_nobody_mapped",
                                  apparatus_events=[unrelated],
                                  evaluation=Evaluation("FAIL", decisive=False))
                self.assertEqual((result.product_outcome, result.unclassified,
                                  result.seal_reason),
                                 ("FAIL", True, "classification_contract_drift"))
                self.assertFalse(outcomes.replacement_eligible(result.as_record(),
                                                               "NOT_REQUIRED"))
        explained = classify(product_started=True, dispatch_state="none",
                             runner_terminal="provider_error", provider_record_in_turn=True,
                             apparatus_events=[event("provider_rate_limited")])
        self.assertEqual((explained.product_outcome, explained.unclassified),
                         ("NOT_EVALUABLE", False))

    def test_a_provider_fault_or_contract_violation_explains_only_the_generic_provider_error(self):
        facts = {"provider_fault": ({"apparatus_events": [event("provider_rate_limited")]},
                                    "apparatus:provider_rate_limited/gateway_response_record"),
                 "request_contract_violation": ({"request_contract_violation": True},
                                                outcomes.REQUEST_CONTRACT_VIOLATION)}
        for label, (kwargs, kept) in facts.items():
            with self.subTest(label=label):
                result = classify(product_started=True, dispatch_state="none",
                                  runner_terminal="terminal_nobody_mapped", **kwargs)
                self.assertEqual((result.product_outcome, result.apparatus_code,
                                  result.unclassified, result.seal_reason),
                                 ("FAIL", outcomes.CONTRACT_DRIFT, True,
                                  "classification_contract_drift"))
                # Both facts stay: the unmapped ending and the fault or violation beside it.
                self.assertEqual(result.basis, ("unlisted_terminal:terminal_nobody_mapped", kept))
                self.assertFalse(outcomes.replacement_eligible(result.as_record(),
                                                               "NOT_REQUIRED"))
                outcomes.validate_combination(result.as_record(), "NOT_REQUIRED")
        # A bare engine status is no provider ending, and the generic ending needs its record.
        for terminal, kwargs in (("error", {"apparatus_events": [event("provider_rate_limited")]}),
                                 ("provider_error", {})):
            with self.subTest(terminal=terminal, kwargs=kwargs):
                self.assertTrue(classify(product_started=True, dispatch_state="none",
                                         runner_terminal=terminal, **kwargs).unclassified)

    def test_a_started_product_with_no_ending_is_drift_unless_the_runner_lost_sight_of_it(self):
        # The report on 0114a25: a missing ending was accepted as a valid result.
        for label, events in {"nothing": [], "provider_fault": [event("provider_rate_limited")],
                              "evaluator": [event("evaluator_exception")]}.items():
            with self.subTest(label=label):
                result = classify(product_started=True, dispatch_state="none",
                                  runner_terminal=None, apparatus_events=events,
                                  evaluation=Evaluation("PASS", decisive=True),
                                  gateway_terminal="provider_deadline")
                self.assertEqual((result.product_outcome, result.unclassified,
                                  result.seal_reason),
                                 ("FAIL", True, "classification_contract_drift"))
                self.assertEqual(result.basis[0], "missing_terminal")
                self.assertEqual(result.basis[-1], "gateway_terminal:provider_deadline")
        for evidence in sorted(outcomes.TERMINAL_LOSS_EVIDENCE):
            with self.subTest(evidence=evidence):
                lost = classify(product_started=True, dispatch_state="none", runner_terminal=None,
                                apparatus_events=[ApparatusEvent(
                                    "browser_or_control_channel_crash", evidence)])
                self.assertEqual((lost.product_outcome, lost.unclassified),
                                 ("NOT_EVALUABLE", False))

    def test_nothing_that_failed_after_every_turn_ended_explains_a_missing_ending(self):
        # The reviews of 8574cb0 and 582e11c: a failed logout, screenshot or browser close after
        # the run showed nothing about how the product ended, yet explained a missing ending.
        for evidence in ("final_page_error", "guard_capture_error", "teardown_error",
                         "logout_failed"):
            with self.subTest(evidence=evidence):
                fault = ApparatusEvent("browser_or_control_channel_crash", evidence)
                self.assertNotIn(evidence, outcomes.TERMINAL_LOSS_EVIDENCE)
                missing = classify(product_started=True, dispatch_state="none",
                                   runner_terminal=None, apparatus_events=[fault])
                self.assertEqual((missing.product_outcome, missing.unclassified,
                                  missing.seal_reason),
                                 ("FAIL", True, "classification_contract_drift"))
                self.assertFalse(outcomes.replacement_eligible(missing.as_record(),
                                                               "NOT_REQUIRED"))
                # Beside an ending the product did report, it stays an ordinary fault: a judgement
                # that stands is kept and never replaced, and none at all is replaceable.
                judged = classify(product_started=True, dispatch_state="none",
                                  runner_terminal="completed", apparatus_events=[fault],
                                  evaluation=Evaluation("PASS", decisive=True))
                self.assertEqual((judged.product_outcome, judged.apparatus_integrity,
                                  judged.apparatus_code),
                                 ("PASS", "INVALID", "browser_or_control_channel_crash"))
                self.assertFalse(outcomes.replacement_eligible(judged.as_record(),
                                                               "NOT_REQUIRED"))
                outcomes.validate_combination(judged.as_record(), "NOT_REQUIRED")
                ended = classify(product_started=True, dispatch_state="none",
                                 runner_terminal="completed", apparatus_events=[fault])
                self.assertEqual((ended.product_outcome, ended.unclassified),
                                 ("NOT_EVALUABLE", False))
                self.assertTrue(outcomes.replacement_eligible(ended.as_record(), "NOT_REQUIRED"))

    def test_a_turn_that_did_not_stop_keeps_its_result_and_is_never_replaced(self):
        # The review of 48cf127: a turn the runner could not stop was scored as a clean product
        # result, though the product may still have been running.
        not_stopped = ApparatusEvent("turn_not_stopped", "stop_not_confirmed")
        self.assertNotIn("stop_not_confirmed", outcomes.TERMINAL_LOSS_EVIDENCE)
        self.assertNotIn("stop_not_confirmed", outcomes.REPLACEMENT_EVIDENCE)
        result = classify(product_started=True, dispatch_state="none",
                          runner_terminal="wall_clock_budget", apparatus_events=[not_stopped],
                          evaluation=Evaluation("FAIL", decisive=True))
        self.assertEqual((result.product_outcome, result.apparatus_integrity,
                          result.apparatus_code),
                         ("FAIL", "INVALID", "turn_not_stopped"))
        self.assertFalse(outcomes.replacement_eligible(result.as_record(), "PASS"))
        outcomes.validate_combination(result.as_record(), "PASS")
        # It shows nothing about an ending the product never reported.
        missing = classify(product_started=True, dispatch_state="none", runner_terminal=None,
                           apparatus_events=[not_stopped])
        self.assertEqual((missing.product_outcome, missing.unclassified), ("FAIL", True))

    def test_a_provider_record_explains_the_generic_failure_only_within_its_turn(self):
        # The review of 582e11c (R4): a record from an earlier turn explained the last turn's
        # generic provider failure.
        rate_limited = [event("provider_rate_limited")]
        outside = classify(product_started=True, dispatch_state="none",
                           runner_terminal="provider_error", apparatus_events=rate_limited)
        self.assertEqual((outside.unclassified, outside.basis),
                         (True, ("unlisted_terminal:provider_error",
                                 "apparatus:provider_rate_limited/gateway_response_record")))
        within = classify(product_started=True, dispatch_state="none",
                          runner_terminal="provider_error", apparatus_events=rate_limited,
                          provider_record_in_turn=True)
        self.assertEqual((within.product_outcome, within.unclassified), ("NOT_EVALUABLE", False))
        # Saying a record is in the turn explains nothing without the record.
        self.assertTrue(classify(product_started=True, dispatch_state="none",
                                 runner_terminal="provider_error",
                                 provider_record_in_turn=True).unclassified)

    def test_every_turn_before_the_last_needs_a_closed_ending(self):
        # The review of 582e11c (R1, R2): only the last turn's ending was ever checked.
        facts = outcomes.gateway_facts(
            [gateway_response(2, "http_error", "provider_rate_limited"),
             gateway_response(4, "http_error", "provider_context_length")],
            [gateway_refusal(6, "request_contract")])
        self.assertEqual((facts.terminals, facts.explaining_seqs),
                         (((4, "context_length_exceeded"),), (2, 6)))
        self.assertTrue(outcomes.explained_within(facts, 1))
        self.assertFalse(outcomes.explained_within(facts, 2, 5))
        self.assertFalse(outcomes.explained_within(facts, 6))
        self.assertFalse(outcomes.explained_within(facts, None))
        turns = [("completed", 0, 1), ("provider_error", 1, 2), ("provider_error", 2, 4),
                 ("provider_error", 4, 6), ("provider_error", 6, 7), ("provider_error", 2, None),
                 ("control_plane_error", 7, 8), (None, 8, 9)]
        self.assertEqual(outcomes.unclosed_earlier_turns(turns, facts), (
            "unexplained_turn_ending:4:provider_error",
            "unexplained_turn_ending:5:provider_error",
            "unlisted_turn_ending:6:control_plane_error", "missing_turn_ending:7"))
        result = classify(product_started=True, dispatch_state="none",
                          runner_terminal="completed",
                          evaluation=Evaluation("PASS", decisive=True),
                          unclosed_turns=("unlisted_turn_ending:0:control_plane_error",))
        self.assertEqual((result.product_outcome, result.unclassified, result.basis),
                         ("FAIL", True, ("unlisted_turn_ending:0:control_plane_error",)))

    def test_an_unmapped_ending_keeps_the_gateway_ending_it_was_not_replaced_by(self):
        result = classify(product_started=True, dispatch_state="none",
                          runner_terminal="terminal_nobody_mapped",
                          evaluation=Evaluation("PASS", decisive=True),
                          gateway_terminal="context_length_exceeded")
        self.assertEqual(result.basis, ("unlisted_terminal:terminal_nobody_mapped",
                                        "gateway_terminal:context_length_exceeded"))
        self.assertTrue(result.unclassified)

    def test_a_confirmed_write_is_not_erased_by_a_later_fault(self):
        result = classify(product_started=True, dispatch_state="acknowledged",
                          runner_terminal="provider_error", provider_record_in_turn=True,
                          apparatus_events=[event("provider_connection_lost")])
        self.assertEqual((result.product_outcome, result.basis),
                         ("INDETERMINATE", ("write_before_apparatus_fault",)))
        self.assertFalse(outcomes.replacement_eligible(result.as_record(), "PASS"))
        outcomes.validate_combination(result.as_record(), "PASS")
        with self.assertRaises(outcomes.OutcomeContractError):
            outcomes.validate_combination({**result.as_record(),
                                           "product_outcome": "NOT_EVALUABLE"}, "PASS")

    def test_a_decided_failure_before_a_fault_is_kept_and_never_replaced(self):
        result = classify(product_started=True, dispatch_state="none",
                          runner_terminal="provider_error", provider_record_in_turn=True,
                          apparatus_events=[event("provider_rate_limited")],
                          evaluation=Evaluation("FAIL", decisive=True,
                                                reasons=("clarification:answer_facts",)))
        self.assertEqual((result.product_outcome, result.apparatus_integrity),
                         ("FAIL", "INVALID"))
        self.assertFalse(outcomes.replacement_eligible(result.as_record(), "NOT_REQUIRED"))

    def test_gateway_facts_name_the_first_faulty_request(self):
        facts = outcomes.gateway_facts(
            [gateway_response(1, "ok"),
             gateway_response(2, "transport_error", "provider_deadline"),
             gateway_response(4, "http_error", "provider_rate_limited"),
             gateway_response(5, "http_error", "provider_unavailable")],
            [gateway_refusal(3, "model_call_budget")])
        self.assertEqual(facts.first_event_seq, 4)
        # The ending keeps the number of the request that named it: the later refusal.
        self.assertEqual((facts.terminal, facts.terminal_seq), ("model_call_budget", 3))
        self.assertIsNone(outcomes.gateway_facts(
            [gateway_response(1, "http_error", "provider_deadline")], []).first_event_seq)


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
                                  runner_terminal="provider_error", provider_record_in_turn=True,
                                  apparatus_events=facts.events)
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
                    provider_record_in_turn=True,
                    apparatus_events=[ApparatusEvent("browser_or_control_channel_crash",
                                                     "browser_exit"), event(code)])
                self.assertEqual((result.apparatus_code, result.seal_reason), (code, seal))
                self.assertFalse(outcomes.replacement_eligible(result.as_record(), "NOT_REQUIRED"))
                outcomes.validate_combination(result.as_record(), "NOT_REQUIRED")
        limited = classify(product_started=True, dispatch_state="none",
                           runner_terminal="provider_error", provider_record_in_turn=True,
                           apparatus_events=[event("provider_rate_limited")])
        self.assertIsNone(limited.seal_reason)
        self.assertTrue(outcomes.replacement_eligible(limited.as_record(), "NOT_REQUIRED"))

    def test_a_request_outside_the_frozen_contract_is_a_product_failure_that_seals(self):
        clean = classify(product_started=True, dispatch_state="none",
                         runner_terminal="provider_error", provider_record_in_turn=True,
                         evaluation=Evaluation("PASS", decisive=True),
                         request_contract_violation=True)
        self.assertEqual((clean.product_outcome, clean.apparatus_integrity, clean.unclassified,
                          clean.seal_reason),
                         ("FAIL", "VALID", False, outcomes.REQUEST_CONTRACT_VIOLATION))
        outcomes.validate_combination(clean.as_record(), "NOT_REQUIRED")
        faulted = classify(product_started=True, dispatch_state="dispatched",
                           runner_terminal="provider_error", provider_record_in_turn=True,
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
