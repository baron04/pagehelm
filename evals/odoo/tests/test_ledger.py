import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals.odoo import ledger, outcomes
from evals.odoo.outcomes import ApparatusEvent, Evaluation, classify

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
EMPTY_AGENT = {"active_sessions": 0, "unresolved_mutations": 0, "prepared_proposals": 0,
               "conversation_rows": 0}
CLEAN_AGENT = {"start": EMPTY_AGENT, "end": {**EMPTY_AGENT, "conversation_rows": 1},
               "open_proposals": 0, "closed_proposals": 0}
CANDIDATE = {"commit": "c" * 40, "agent_image_id": "sha256:" + "d" * 64,
             "agent_image_revision": "c" * 40}


def slots(count=4, arm="woa_product"):
    return [{"slot_id": f"s{index:03d}", "arm": arm, "task_id": f"task-{index:03d}",
             "replicate": "primary"} for index in range(count)]


def metrics(attempt_id, wall_ms=1200):
    return {"attempt_id": attempt_id, "automatic_wall_ms": wall_ms, "approval_wait_ms": 0,
            "first_feedback_ms": 80, "cancel_feedback_ms": None, "page_actions": 0,
            "invalid_page_actions": 0, "business_calls": 1, "repeated_observations": 0,
            "user_turns": 1}


def header(slot_list, *, previous=None, protocol=DIGEST, purpose="development", seed="0" * 32,
           **changes):
    value = {"batch_id": "batch-001", "protocol_id": "p74-odoo-v1", "protocol_digest": protocol,
             "schedule_digest": ledger.schedule_digest(slot_list), "purpose": purpose,
             "provider_config_digest": DIGEST, "slots": slot_list, "previous_batch": previous,
             "task_set_seal_digest": DIGEST,
             "fixture": {"manifest_digest": DIGEST, "baseline_guard_digest": OTHER},
             "capability_digests": {"pageagent_browser_only": DIGEST, "woa_product": DIGEST},
             "candidate": CANDIDATE, "schedule_seed": seed, "provider_binding_digest": DIGEST,
             "ledger_contract_digest": ledger.LEDGER_CONTRACT_DIGEST}
    return {**value, **changes}


class LedgerHarness:
    def __init__(self, test, slot_list, **header_changes):
        self.directory = tempfile.TemporaryDirectory()
        test.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "attempts.jsonl"
        self.slots = {slot["slot_id"]: slot for slot in slot_list}
        ledger.create(self.path, header(slot_list, **header_changes))
        self.counter = 0

    def start(self, slot_id, *, replacement_of=None, **changes):
        self.counter += 1
        attempt_id = f"attempt-{self.counter:03d}"
        slot = self.slots[slot_id]
        ledger.append(self.path, "attempt_started", {
            "attempt_id": attempt_id, "slot_id": slot_id, "arm": slot["arm"],
            "task_id": slot["task_id"], "replicate": slot["replicate"],
            "run_id": f"run-{self.counter:03d}", "fixture_digest": DIGEST,
            "capability_digest": DIGEST, "provider_config_digest": DIGEST,
            "provider_binding_digest": DIGEST, "replacement_of": replacement_of, **changes})
        return attempt_id

    def model_call(self, attempt_id, seq, *, model="scripted-model", usage=True, status="ok"):
        ledger.append(self.path, "model_request_dispatched", {
            "attempt_id": attempt_id, "request_seq": seq, "body_digest": DIGEST,
            "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
            "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                           "tools": 0, "options": {"thinking_budget": 1024}}})
        ledger.append(self.path, "model_response_received", {
            "attempt_id": attempt_id, "request_seq": seq, "status": status,
            "http_status": 200 if status == "ok" else 503, "provider_request_id": f"req-{seq}",
            "response_model": model if status == "ok" else None,
            "usage": ({"input_tokens": 10, "output_tokens": 2, "cached_tokens": 0,
                       "reasoning_tokens": 0} if usage else None),
            "cost_cny": 0.0 if usage else None, "error_code": None})

    def finish(self, attempt_id, classification, *, reset="NOT_REQUIRED", calls=0,
               unavailable=0, terminal="attempt_terminal", evidence=None, agent=CLEAN_AGENT):
        ledger.append(self.path, "attempt_outcome", {
            "attempt_id": attempt_id, **classification.as_record(),
            "evaluation_digest": None, "truth_digest": None})
        ledger.append(self.path, "reset_outcome", {
            "attempt_id": attempt_id, "outcome": reset, "basis": [], "guard_digest": None,
            "agent": agent})
        ledger.append(self.path, "attempt_cost", {
            "attempt_id": attempt_id, "model_calls": calls,
            "usage_unavailable_calls": unavailable,
            "input_tokens": 10 * (calls - unavailable), "output_tokens": 2 * (calls - unavailable),
            "cached_tokens": 0, "reasoning_tokens": 0, "known_cost_cny": 0.0,
            "total_cost_cny": None if unavailable else 0.0})
        ledger.append(self.path, "attempt_metrics", metrics(attempt_id))
        if terminal == "attempt_terminal":
            return ledger.append(self.path, terminal, {"attempt_id": attempt_id})
        return ledger.append(self.path, terminal, {
            "attempt_id": attempt_id, "evidence_kinds": evidence or [],
            "replacement_eligible": outcomes.replacement_eligible(
                classification.as_record(), reset) and bool(evidence)})


PASS = classify(product_started=True, dispatch_state="none", runner_terminal="completed",
                evaluation=Evaluation("PASS", decisive=True))
OUTAGE = classify(product_started=True, dispatch_state="none", runner_terminal="provider_error",
                  apparatus_events=[ApparatusEvent("provider_unavailable",
                                                   "gateway_response_record")],
                  provider_record_in_turn=True)


class LifecycleTests(unittest.TestCase):
    def test_complete_batch_and_status(self):
        harness = LedgerHarness(self, slots(2))
        for slot_id in ("s000", "s001"):
            attempt = harness.start(slot_id)
            harness.model_call(attempt, 1)
            harness.finish(attempt, PASS, calls=1)
        summary = ledger.append(harness.path, "batch_completed", {})
        self.assertEqual(summary["status"], "COMPLETE")
        self.assertEqual(summary["provider_model"], "scripted-model")
        self.assertEqual(os.stat(harness.path).st_mode & 0o777, 0o600)

    def test_one_open_attempt_and_schedule_order(self):
        harness = LedgerHarness(self, slots(3))
        with self.assertRaises(ledger.LedgerError):
            harness.start("s001")
        harness.start("s000")
        with self.assertRaises(ledger.LedgerError):
            harness.start("s001")

    def test_records_must_follow_the_attempt_order(self):
        harness = LedgerHarness(self, slots(1))
        attempt = harness.start("s000")
        with self.assertRaises(ledger.LedgerError):
            ledger.append(harness.path, "attempt_terminal", {"attempt_id": attempt})
        with self.assertRaises(ledger.LedgerError):
            ledger.append(harness.path, "reset_outcome", {
                "attempt_id": attempt, "outcome": "PASS", "basis": [], "guard_digest": None,
                "agent": CLEAN_AGENT})

    def test_response_needs_its_request_and_cost_must_match(self):
        harness = LedgerHarness(self, slots(1))
        attempt = harness.start("s000")
        with self.assertRaises(ledger.LedgerError):
            ledger.append(harness.path, "model_response_received", {
                "attempt_id": attempt, "request_seq": 1, "status": "ok", "http_status": 200,
                "provider_request_id": None, "response_model": "m", "usage": None,
                "cost_cny": None, "error_code": None})
        harness.model_call(attempt, 1, usage=False)
        with self.assertRaises(ledger.LedgerError):
            harness.finish(attempt, PASS, calls=1, unavailable=0)

    def test_a_response_after_the_outcome_is_refused_and_only_an_interruption_lacks_times(self):
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        ledger.append(harness.path, "model_request_dispatched", {
            "attempt_id": attempt, "request_seq": 1, "body_digest": DIGEST,
            "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
            "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                           "tools": 0, "options": {}}})
        ledger.append(harness.path, "attempt_outcome", {
            "attempt_id": attempt, **PASS.as_record(), "evaluation_digest": None,
            "truth_digest": None})
        # The review of 582e11c: a response after the outcome made the costed calls disagree.
        with self.assertRaisesRegex(ledger.LedgerError, "precede the attempt's outcome"):
            ledger.append(harness.path, "model_response_received", {
                "attempt_id": attempt, "request_seq": 1, "status": "ok", "http_status": 200,
                "provider_request_id": None, "response_model": "m", "usage": None,
                "cost_cny": None, "error_code": None})
        ledger.append(harness.path, "reset_outcome", {
            "attempt_id": attempt, "outcome": "NOT_REQUIRED", "basis": [], "guard_digest": None,
            "agent": CLEAN_AGENT})
        ledger.append(harness.path, "attempt_cost", {
            "attempt_id": attempt, "model_calls": 1, "usage_unavailable_calls": 1,
            "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0,
            "known_cost_cny": 0.0, "total_cost_cny": None})
        ledger.append(harness.path, "attempt_metrics", {
            **metrics(attempt), "automatic_wall_ms": None, "approval_wait_ms": None})
        # Times a driver never took are unknown: an attempt that finished normally has them.
        with self.assertRaisesRegex(ledger.LedgerError, "automatic and confirmation times"):
            ledger.append(harness.path, "attempt_terminal", {"attempt_id": attempt})
        summary = ledger.append(harness.path, "interrupted_attempt", {
            "attempt_id": attempt, "evidence_kinds": ["runner_process_exit"],
            "replacement_eligible": False})
        self.assertIsNone(summary["attempts"][0]["metrics"]["automatic_wall_ms"])

    def test_dispatched_request_parameters_are_validated(self):
        harness = LedgerHarness(self, slots(1))
        attempt = harness.start("s000")
        valid = {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto", "tools": 0,
                 "options": {"thinking_budget": 1024}}
        body = {"attempt_id": attempt, "request_seq": 1, "body_digest": DIGEST,
                "requested_model": "qwen3.8-flash", "max_output_tokens": 2048}
        cases = {
            "zero_cap": ({**valid, "max_tokens": 0}, {"max_output_tokens": 0}),
            "cap_disagrees": ({**valid, "max_tokens": 1024}, {}),
            "missing_field": ({key: value for key, value in valid.items() if key != "tools"}, {}),
            "nested_option": ({**valid, "options": {"thinking": {"type": "enabled"}}}, {}),
            "object_tool_choice": ({**valid, "tool_choice": {"type": "function"}}, {}),
        }
        for label, (parameters, override) in cases.items():
            with self.subTest(label=label), self.assertRaises(ledger.LedgerError):
                ledger.append(harness.path, "model_request_dispatched",
                              {**body, **override, "parameters": parameters})
        ledger.append(harness.path, "model_request_dispatched", {**body, "parameters": valid})

    def test_unknown_usage_cannot_be_reported_as_zero_cost(self):
        harness = LedgerHarness(self, slots(1))
        attempt = harness.start("s000")
        harness.model_call(attempt, 1, usage=False)
        summary = harness.finish(attempt, PASS, calls=1, unavailable=1)
        self.assertIsNone(summary["attempts"][0]["cost"]["total_cost_cny"])

    def test_tampering_is_detected(self):
        harness = LedgerHarness(self, slots(1))
        harness.start("s000")
        lines = harness.path.read_text().splitlines()
        entry = json.loads(lines[1])
        entry["body"]["task_id"] = "task-999"
        lines[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        harness.path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(ledger.LedgerError):
            ledger.read(harness.path)

    def test_backwards_timestamp_is_refused_on_replay(self):
        harness = LedgerHarness(self, slots(1))
        entries, _ = ledger.read(harness.path)
        late = ledger._new_entry("batch_sealed", {"reason": "operator_stop"}, 1,
                                 entries[0]["digest"], "2000-01-01T00:00:00.000Z")
        with self.assertRaises(ledger.LedgerError):
            ledger.replay([*entries, late])


class BindingTests(unittest.TestCase):
    def test_the_header_binds_seal_fixture_capabilities_candidate_and_seed(self):
        slot_list = slots(1)
        without_candidate = {key: value for key, value in header(slot_list).items()
                             if key != "candidate"}
        cases = {
            "missing_candidate": without_candidate,
            "short_commit": header(slot_list, candidate={**CANDIDATE, "commit": "eeea4a0",
                                                         "agent_image_revision": "eeea4a0"}),
            "image_of_another_commit": header(slot_list, candidate={
                **CANDIDATE, "agent_image_revision": "f" * 40}),
            "no_capability_for_a_scheduled_arm": header(slot_list, capability_digests={
                "pageagent_browser_only": DIGEST}),
            "formal_batch_without_a_seed": header(slot_list, seed=None),
            "qualification_with_a_seed": header(slot_list, purpose="apparatus_qualification"),
            "malformed_fixture": header(slot_list, fixture={"manifest_digest": "manifest",
                                                            "baseline_guard_digest": OTHER}),
            "malformed_seal": header(slot_list, task_set_seal_digest="seal"),
            "malformed_provider_binding": header(slot_list, provider_binding_digest="binding"),
        }
        with tempfile.TemporaryDirectory() as directory:
            for label, value in cases.items():
                with self.subTest(label=label), self.assertRaises(ledger.LedgerError):
                    ledger.create(Path(directory) / f"{label}.jsonl", value)
            summary = ledger.create(Path(directory) / "qualification.jsonl",
                                    header(slot_list, purpose="apparatus_qualification",
                                           seed=None))
        self.assertEqual(summary["batch"]["candidate"], CANDIDATE)
        self.assertEqual(summary["batch"]["fixture"]["manifest_digest"], DIGEST)

    def test_an_attempt_must_use_the_fixture_and_capability_its_batch_binds(self):
        harness = LedgerHarness(self, slots(2))
        for field in ("fixture_digest", "capability_digest", "provider_config_digest",
                      "provider_binding_digest"):
            with self.subTest(field=field), self.assertRaises(ledger.LedgerError):
                harness.start("s000", **{field: OTHER})
        attempt = harness.start("s000")
        harness.finish(attempt, PASS)


class AgentStateTests(unittest.TestCase):
    def open_outcome(self, harness, slot_id):
        attempt = harness.start(slot_id)
        ledger.append(harness.path, "attempt_outcome", {
            "attempt_id": attempt, **PASS.as_record(), "evaluation_digest": None,
            "truth_digest": None})
        return {"attempt_id": attempt, "basis": [], "guard_digest": None}

    def test_residue_cannot_accompany_a_passing_reset(self):
        declined = {**CLEAN_AGENT, "end": {**EMPTY_AGENT, "prepared_proposals": 1},
                    "closed_proposals": 1}
        harness = LedgerHarness(self, slots(2))
        harness.finish(harness.start("s000"), PASS, agent=declined)
        reset = self.open_outcome(harness, "s001")
        undecided = {**declined, "open_proposals": 1, "closed_proposals": 0}
        for outcome in ("PASS", "NOT_REQUIRED"):
            with self.assertRaises(ledger.LedgerError):
                ledger.append(harness.path, "reset_outcome",
                              {**reset, "outcome": outcome, "agent": undecided})
        summary = ledger.append(harness.path, "reset_outcome",
                                {**reset, "outcome": "FAIL", "agent": undecided})
        self.assertEqual(summary["attempts"][-1]["reset_outcome"], "FAIL")

    def test_assertions_must_be_present_and_consistent(self):
        harness = LedgerHarness(self, slots(1))
        reset = {**self.open_outcome(harness, "s000"), "outcome": "PASS"}
        unread = {**CLEAN_AGENT, "end": None}
        cases = {
            "missing": None,
            "no_start": {**CLEAN_AGENT, "start": None},
            "uncounted_proposal": {**CLEAN_AGENT, "end": {**EMPTY_AGENT, "prepared_proposals": 2},
                                   "closed_proposals": 1},
            "inherited_state": {**CLEAN_AGENT, "start": {**EMPTY_AGENT, "conversation_rows": 1}},
            "unread_end": unread,
        }
        for label, agent in cases.items():
            with self.subTest(label=label), self.assertRaises(ledger.LedgerError):
                ledger.append(harness.path, "reset_outcome", {**reset, "agent": agent})
        ledger.append(harness.path, "reset_outcome",
                      {**reset, "outcome": "INDETERMINATE", "agent": unread})

    def test_only_woa_attempts_carry_agent_state(self):
        harness = LedgerHarness(self, slots(2, arm="pageagent_browser_only"))
        harness.finish(harness.start("s000"), PASS, agent=None)
        with self.assertRaises(ledger.LedgerError):
            harness.finish(harness.start("s001"), PASS)


class MandatorySealTests(unittest.TestCase):
    def test_model_string_change_forces_a_seal(self):
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        harness.model_call(attempt, 1, model="first")
        harness.model_call(attempt, 2, model="second")
        harness.finish(attempt, PASS, calls=2)
        with self.assertRaises(ledger.LedgerError):
            harness.start("s001")
        with self.assertRaises(ledger.LedgerError):
            ledger.append(harness.path, "batch_sealed", {"reason": "operator_stop"})
        summary = ledger.append(harness.path, "batch_sealed", {"reason": "model_identity_changed"})
        self.assertEqual(summary["status"], "SEALED")

    def test_reset_failure_and_unowned_request_force_seals(self):
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        harness.finish(attempt, PASS, reset="FAIL")
        self.assertEqual(ledger.read(harness.path)[1]["pending_seal"], "reset_failed")
        other = LedgerHarness(self, slots(2))
        ledger.append(other.path, "unowned_model_request",
                      {"body_digest": DIGEST, "reason": "no_open_attempt"})
        with self.assertRaises(ledger.LedgerError):
            other.start("s000")

    def test_a_late_answer_is_recorded_and_seals_only_when_it_could_not_be_accounted(self):
        harness = LedgerHarness(self, slots(2))
        late = {"attempt_id": "attempt-001", "request_seq": 1, "status": "http_error",
                "provider_request_id": "req-1", "error_code": "provider_unavailable"}
        summary = ledger.append(harness.path, "late_model_response", late)
        self.assertIsNone(summary["pending_seal"])
        with self.assertRaises(ledger.LedgerError):
            ledger.append(harness.path, "late_model_response", {**late, "status": "later"})
        summary = ledger.append(harness.path, "late_model_response",
                                {**late, "status": "accounting_error", "error_code":
                                 "accounting_failure"})
        self.assertEqual(summary["pending_seal"], "accounting_failure")

    def test_unclassified_result_forces_a_seal(self):
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        drift = classify(product_started=True, dispatch_state="none",
                         runner_terminal="brand_new_terminal",
                         evaluation=Evaluation("PASS", decisive=True))
        harness.finish(attempt, drift)
        with self.assertRaises(ledger.LedgerError):
            ledger.append(harness.path, "batch_completed", {})
        ledger.append(harness.path, "batch_sealed", {"reason": "classification_contract_drift"})


class ReplacementTests(unittest.TestCase):
    def test_eligible_original_is_replaced_once_immediately(self):
        harness = LedgerHarness(self, slots(4))
        original = harness.start("s000")
        harness.finish(original, OUTAGE)
        replacement = harness.start("s000", replacement_of=original)
        harness.finish(replacement, PASS)
        with self.assertRaises(ledger.LedgerError):
            harness.start("s000", replacement_of=replacement)
        summary = ledger.read(harness.path)[1]
        self.assertEqual(summary["replacements"], {"woa_product": 1})
        self.assertEqual(summary["slots"][0]["attempts"], [original, replacement])

    def test_ineligible_original_cannot_be_replaced(self):
        harness = LedgerHarness(self, slots(2))
        original = harness.start("s000")
        harness.finish(original, classify(
            product_started=True, dispatch_state="none", runner_terminal="completed",
            evaluation=Evaluation("FAIL", decisive=True)))
        with self.assertRaises(ledger.LedgerError):
            harness.start("s000", replacement_of=original)

    def test_cap_exhaustion_forces_a_seal(self):
        harness = LedgerHarness(self, slots(4))  # cap: ceil(4/10) = 1 per arm
        first = harness.start("s000")
        harness.finish(first, OUTAGE)
        harness.finish(harness.start("s000", replacement_of=first), PASS)
        second = harness.start("s001")
        summary = harness.finish(second, OUTAGE)
        self.assertEqual(summary["pending_seal"], "replacement_cap_exceeded")
        with self.assertRaises(ledger.LedgerError):
            harness.start("s001", replacement_of=second)

    def test_interruption_without_independent_evidence_is_not_replaceable(self):
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        with self.assertRaises(ledger.LedgerError):
            ledger.append(harness.path, "attempt_outcome", {
                "attempt_id": attempt, **OUTAGE.as_record(), "evaluation_digest": None,
                "truth_digest": None})
            ledger.append(harness.path, "reset_outcome", {
                "attempt_id": attempt, "outcome": "NOT_REQUIRED", "basis": [],
                "guard_digest": None, "agent": CLEAN_AGENT})
            ledger.append(harness.path, "attempt_cost", {
                "attempt_id": attempt, "model_calls": 0, "usage_unavailable_calls": 0,
                "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                "reasoning_tokens": 0, "known_cost_cny": 0.0, "total_cost_cny": 0.0})
            ledger.append(harness.path, "attempt_metrics", metrics(attempt))
            ledger.append(harness.path, "interrupted_attempt", {
                "attempt_id": attempt, "evidence_kinds": [], "replacement_eligible": True})
        # Everything before the interruption record was accepted; only that record was refused.
        self.assertIsNotNone(ledger.read(harness.path)[1]["attempts"][0]["metrics"])


class ProviderRecordTests(unittest.TestCase):
    def response(self, harness, attempt, seq, **changes):
        ledger.append(harness.path, "model_request_dispatched", {
            "attempt_id": attempt, "request_seq": seq, "body_digest": DIGEST,
            "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
            "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                           "tools": 0, "options": {"thinking_budget": 1024}}})
        return ledger.append(harness.path, "model_response_received", {
            "attempt_id": attempt, "request_seq": seq, "status": "ok", "http_status": 200,
            "provider_request_id": f"req-{seq}", "response_model": "first", "usage": None,
            "cost_cny": None, "error_code": None, **changes})

    def test_typed_failures_are_recorded_and_any_model_string_they_name_still_counts(self):
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        self.response(harness, attempt, 1, status="contract_error",
                      error_code="provider_response_contract")
        summary = self.response(harness, attempt, 2, status="accounting_error",
                                error_code="accounting_failure", response_model="second")
        self.assertEqual((summary["provider_model"], summary["pending_seal"]),
                         ("first", "model_identity_changed"))
        ledger.append(harness.path, "model_request_refused",
                      {"attempt_id": attempt, "request_seq": 3, "reason": "accounting_failure"})
        with self.assertRaises(ledger.LedgerError):
            self.response(harness, attempt, 4, status="teapot", response_model=None)

    def test_a_refused_credential_an_unaccounted_call_and_a_contract_violation_seal(self):
        for code, evidence in (("provider_authentication_failed", "gateway_response_record"),
                               ("accounting_failure", "gateway_accounting_record")):
            with self.subTest(code=code):
                harness = LedgerHarness(self, slots(2))
                fault = classify(product_started=True, dispatch_state="none",
                                 runner_terminal="provider_error", provider_record_in_turn=True,
                                 apparatus_events=[ApparatusEvent(code, evidence)])
                summary = harness.finish(harness.start("s000"), fault)
                self.assertEqual(summary["pending_seal"], code)
                self.assertFalse(summary["attempts"][0]["replacement_eligible"])
        harness = LedgerHarness(self, slots(2))
        violation = classify(product_started=True, dispatch_state="none",
                             runner_terminal="provider_error", request_contract_violation=True,
                             provider_record_in_turn=True)
        summary = harness.finish(harness.start("s000"), violation)
        self.assertEqual((summary["pending_seal"], summary["attempts"][0]["product_outcome"]),
                         ("request_contract_violation", "FAIL"))


NEW_CANDIDATE = {"commit": "e" * 40, "agent_image_id": "sha256:" + "f" * 64,
                 "agent_image_revision": "e" * 40}


def link(*, purpose="development", protocol=DIGEST, candidate=CANDIDATE,
         reason="batch_completed", record=None):
    return {"batch_id": "batch-000", "purpose": purpose, "protocol_digest": protocol,
            "candidate_commit": candidate["commit"], "chain_head": OTHER,
            "termination_reason": reason, "continuation_record": record}


class BatchChainTests(unittest.TestCase):
    def outcome(self, **header_changes):
        with tempfile.TemporaryDirectory() as directory:
            try:
                ledger.create(Path(directory) / "batch.jsonl", header(slots(1), **header_changes))
            except ledger.LedgerError:
                return "refused"
            return "allowed"

    def test_a_new_development_candidate_follows_any_finished_batch(self):
        for reason in ("batch_completed", "critical_safety_event", "reset_failed", "operator_stop",
                       "model_identity_changed"):
            with self.subTest(reason=reason):
                self.assertEqual(self.outcome(previous=link(reason=reason),
                                              candidate=NEW_CANDIDATE), "allowed")

    def test_the_same_development_candidate_continues_only_after_an_exogenous_stop(self):
        expected = {"batch_completed": "refused", "critical_safety_event": "refused",
                    "operator_stop": "refused", "request_contract_violation": "refused",
                    "model_identity_changed": "allowed", "replacement_cap_exceeded": "allowed",
                    "provider_authentication_failed": "allowed", "accounting_failure": "allowed"}
        for reason, outcome in expected.items():
            with self.subTest(reason=reason):
                record = OTHER if reason == "accounting_failure" else None
                self.assertEqual(self.outcome(previous=link(reason=reason, record=record)),
                                 outcome)
        self.assertEqual(self.outcome(previous=link(), protocol=OTHER), "refused")

    def test_an_unaccounted_call_is_followed_only_with_its_bill_reconciliation(self):
        for candidate in (CANDIDATE, NEW_CANDIDATE):
            with self.subTest(candidate=candidate["commit"][:1]):
                self.assertEqual(self.outcome(previous=link(reason="accounting_failure"),
                                              candidate=candidate), "refused")
                self.assertEqual(self.outcome(previous=link(reason="accounting_failure",
                                                            record=OTHER), candidate=candidate),
                                 "allowed")
        # No other ending carries a continuation record.
        self.assertEqual(self.outcome(previous=link(reason="model_identity_changed",
                                                    record=OTHER)), "refused")

    def test_heldout_batches_keep_the_strict_protocol_rule(self):
        heldout = {"purpose": "heldout", "candidate": NEW_CANDIDATE}
        self.assertEqual(self.outcome(previous=link(purpose="heldout"), **heldout), "refused")
        self.assertEqual(self.outcome(previous=link(purpose="heldout"), protocol=OTHER, **heldout),
                         "allowed")
        for reason in ("model_identity_changed", "replacement_cap_exceeded"):
            with self.subTest(reason=reason):
                self.assertEqual(self.outcome(previous=link(purpose="heldout", reason=reason),
                                              **heldout), "allowed")

    def test_a_heldout_restart_after_a_refused_credential_or_unaccounted_call_needs_its_audit(self):
        heldout = {"purpose": "heldout", "candidate": NEW_CANDIDATE}
        for reason in ("provider_authentication_failed", "accounting_failure"):
            with self.subTest(reason=reason):
                self.assertEqual(self.outcome(previous=link(purpose="heldout", reason=reason),
                                              **heldout), "refused")
                self.assertEqual(self.outcome(previous=link(purpose="heldout", reason=reason,
                                                            record=OTHER), **heldout), "allowed")
        # A new protocol version needs no audit after a refused credential, and a qualification
        # batch starts again after any exogenous stop without one.
        self.assertEqual(self.outcome(previous=link(purpose="heldout",
                                                    reason="provider_authentication_failed"),
                                      protocol=OTHER, **heldout), "allowed")
        qualification = {"purpose": "apparatus_qualification", "seed": None,
                         "candidate": NEW_CANDIDATE}
        self.assertEqual(self.outcome(previous=link(purpose="apparatus_qualification",
                                                    reason="provider_authentication_failed"),
                                      **qualification), "allowed")

    def test_a_link_names_its_purpose_candidate_and_ending(self):
        partial = {key: value for key, value in link().items() if key != "candidate_commit"}
        for label, previous in {"other_purpose": link(purpose="heldout"), "no_candidate": partial,
                                "unknown_ending": link(reason="lunch_break")}.items():
            with self.subTest(label=label):
                self.assertEqual(self.outcome(previous=previous, candidate=NEW_CANDIDATE),
                                 "refused")

    def test_completion_needs_every_slot(self):
        harness = LedgerHarness(self, slots(2))
        harness.finish(harness.start("s000"), PASS)
        with self.assertRaises(ledger.LedgerError):
            ledger.append(harness.path, "batch_completed", {})


class LedgerContractTests(unittest.TestCase):
    def test_a_ledger_written_under_another_contract_is_refused_whole_and_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.jsonl"
            older = "sha256:" + "1" * 64
            with mock.patch.object(ledger, "LEDGER_CONTRACT_DIGEST", older):
                ledger.create(path, header(slots(1), ledger_contract_digest=older))
                self.assertEqual(ledger.append(path, "batch_sealed",
                                               {"reason": "operator_stop"})["status"], "SEALED")
            written = path.read_bytes()
            with self.assertRaisesRegex(ledger.LedgerContractError, "another ledger contract"):
                ledger.read(path)
            with self.assertRaises(ledger.LedgerContractError):
                ledger.append(path, "unowned_model_request",
                              {"body_digest": DIGEST, "reason": "no_open_attempt"})
            self.assertEqual(path.read_bytes(), written)
            unnamed = {key: value for key, value in header(slots(1)).items()
                       if key != "ledger_contract_digest"}
            with self.assertRaises(ledger.LedgerContractError):
                ledger.create(Path(directory) / "unnamed.jsonl", unnamed)
            self.assertFalse((Path(directory) / "unnamed.jsonl").exists())

    def test_the_contract_digest_follows_the_files_that_define_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ledger.CONTRACT_FILES:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ledger._REPOSITORY / relative, root / relative)
            self.assertEqual(ledger.contract_digest(root), ledger.LEDGER_CONTRACT_DIGEST)
            outcomes_file = root / "evals/odoo/outcomes.py"
            outcomes_file.write_bytes(outcomes_file.read_bytes() + b"\n")
            self.assertNotEqual(ledger.contract_digest(root), ledger.LEDGER_CONTRACT_DIGEST)


REFUSED_CREDENTIAL = classify(product_started=True, dispatch_state="none",
                              runner_terminal="provider_error", provider_record_in_turn=True,
                              apparatus_events=[ApparatusEvent("provider_authentication_failed",
                                                               "gateway_response_record")])


class ExposureTests(unittest.TestCase):
    def test_a_credential_refused_before_any_answer_exposes_nothing(self):
        harness = LedgerHarness(self, slots(2), purpose="heldout")
        harness.finish(harness.start("s000"), REFUSED_CREDENTIAL)
        ledger.append(harness.path, "batch_sealed", {"reason": "provider_authentication_failed"})
        self.assertEqual(ledger.exposure(ledger.read(harness.path)[0]), [])

    def test_a_batch_that_answered_was_judged_or_wrote_state_exposes_it(self):
        harness = LedgerHarness(self, slots(2), purpose="heldout")
        first = harness.start("s000")
        harness.model_call(first, 1)
        harness.finish(first, PASS, reset="PASS", calls=1)
        harness.finish(harness.start("s001"), REFUSED_CREDENTIAL)
        self.assertEqual(ledger.exposure(ledger.read(harness.path)[0]),
                         ["judged_result:attempt-001", "model_answer:attempt-001",
                          "state_write:attempt-001"])


if __name__ == "__main__":
    unittest.main()
