import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals.odoo import gateway, ledger, outcomes
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


def header(slot_list, *, protocol=DIGEST, purpose="development", seed="0" * 32, **changes):
    value = {"batch_id": "batch-001", "protocol_id": "p74-odoo-v2", "protocol_digest": protocol,
             "schedule_digest": ledger.schedule_digest(slot_list), "purpose": purpose,
             "provider_config_digest": DIGEST, "slots": slot_list,
             "task_set_seal_digest": DIGEST,
             "fixture": {"manifest_digest": DIGEST, "baseline_guard_digest": OTHER},
             "capability_digests": {"pageagent_browser_only": DIGEST, "woa_product": DIGEST},
             "candidate": CANDIDATE, "schedule_seed": seed, "provider_binding_digest": DIGEST}
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
        # A response after the outcome would make the costed calls disagree.
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
        # The call the dead driver dispatched settles as unresolved, after the records the
        # recovery wrote from what was persisted before it.
        ledger.append(harness.path, "model_response_unresolved", {
            "attempt_id": attempt, "request_seq": 1, "reason": "driver_process_exit"})
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
    def test_an_attempt_must_seal_for_the_most_serious_reason_it_names(self):
        """The ledger sees every reason the attempt names, not only the one it chose, so the
        batch's reason does not follow whichever fault the driver recorded first."""
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        outcome = classify(product_started=True, dispatch_state="none",
                           runner_terminal="completed",
                           apparatus_events=[ApparatusEvent("accounting_failure",
                                                            "gateway_accounting_record")],
                           evaluation=Evaluation("PASS", decisive=True),
                           request_contract_violation=True).as_record()
        self.assertEqual(outcome["seal_reason"], "request_contract_violation")
        for broken in ({**outcome, "seal_reason": "accounting_failure"},
                       {**outcome, "seal_reasons": ["operator_stop", "made_up"]},
                       {**outcome, "seal_reasons": []}):
            with self.assertRaises(ledger.LedgerError):
                ledger.append(harness.path, "attempt_outcome",
                              {"attempt_id": attempt, **broken, "evaluation_digest": None,
                               "truth_digest": None})
        # Both reasons reach the batch, and it seals for the more serious one.
        summary = harness.finish(attempt, classify(
            product_started=True, dispatch_state="none", runner_terminal="completed",
            apparatus_events=[ApparatusEvent("accounting_failure", "gateway_accounting_record")],
            evaluation=Evaluation("PASS", decisive=True), request_contract_violation=True))
        self.assertEqual(summary["pending_seal"], "request_contract_violation")
        self.assertEqual(summary["seal_reasons"],
                         ["request_contract_violation", "accounting_failure"])

    def test_a_call_whose_cost_is_unknown_holds_the_batch_until_the_bill_accounts_for_it(self):
        """The cost of a paid call is unknown until the bill says, and a batch that owes one
        makes no more: a call left unresolved in the fifth attempt stops the other fifty-five,
        not only a check made once before the batch."""
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        request = {"attempt_id": attempt, "request_seq": 1, "body_digest": DIGEST,
                   "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
                   "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                                  "tools": 0, "options": {}}}
        ledger.append(harness.path, "model_request_dispatched", request)
        summary = ledger.append(harness.path, "model_response_unresolved", {
            "attempt_id": attempt, "request_seq": 1, "reason": "driver_process_exit"})
        self.assertEqual(summary["cost_unknown"], [f"{attempt}#1"])
        # The attempt still closes: what it already did is recorded, and the reset still runs.
        with self.assertRaisesRegex(ledger.LedgerError, "holds for a call whose cost is unknown"):
            ledger.append(harness.path, "model_request_dispatched", {**request, "request_seq": 2})
        harness.finish(attempt, PASS, calls=1, unavailable=1)
        for kind, body in (("attempt_started", None), ("batch_completed", {})):
            with self.assertRaisesRegex(ledger.LedgerError, "cost is unknown"):
                if kind == "attempt_started":
                    harness.start("s001")
                else:
                    ledger.append(harness.path, "batch_completed", body)
        # Nor may the operator stop a batch that owes a bill: nobody could resume it to pay.
        with self.assertRaisesRegex(ledger.LedgerError, "before stopping the batch"):
            ledger.append(harness.path, "batch_sealed", {"reason": "operator_stop"})
        # The reconciliation covers the calls it names, and only calls this batch owes.
        applied = {"covered_calls": [f"{attempt}#1"], "reconciliation_digest": DIGEST,
                   "coverage_digest": DIGEST}
        for refused in ({**applied, "covered_calls": []},
                        {**applied, "covered_calls": [f"{attempt}#2"]},
                        {**applied, "covered_calls": [f"{attempt}#1", f"{attempt}#1"]}):
            with self.assertRaises(ledger.LedgerError):
                ledger.append(harness.path, "billing_reconciliation_applied", refused)
        summary = ledger.append(harness.path, "billing_reconciliation_applied", applied)
        self.assertEqual(summary["cost_unknown"], [])
        # The same batch goes on; it was never ended.
        harness.finish(harness.start("s001"), PASS)
        self.assertEqual(ledger.append(harness.path, "batch_completed", {})["status"],
                         "COMPLETE")

    def test_a_batch_that_holds_still_seals_for_a_fault_it_met(self):
        """A safety event does not wait on a bill."""
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        ledger.append(harness.path, "model_request_dispatched", {
            "attempt_id": attempt, "request_seq": 1, "body_digest": DIGEST,
            "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
            "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                           "tools": 0, "options": {}}})
        ledger.append(harness.path, "model_response_unresolved", {
            "attempt_id": attempt, "request_seq": 1, "reason": "provider_deadline_elapsed"})
        unsafe = classify(product_started=True, dispatch_state="acknowledged",
                          runner_terminal="completed",
                          evaluation=Evaluation("FAIL", decisive=True,
                                                safety_events=("unauthorized_dispatch",)))
        harness.finish(attempt, unsafe, calls=1, unavailable=1)
        summary = ledger.append(harness.path, "batch_sealed",
                                {"reason": "critical_safety_event"})
        self.assertEqual(summary["seal_reason"], "critical_safety_event")
        # The debt is still recorded, and the paid entry still reads it.
        self.assertEqual(summary["cost_unknown"], [f"{attempt}#1"])

    def test_a_seal_the_batch_was_told_to_take_joins_the_reasons_it_met(self):
        """A reason given to `batch_sealed` rather than met by an attempt -- an operator's stop
        -- joins `seal_reasons`, so the summary never names a seal its own facts do not hold."""
        harness = LedgerHarness(self, slots(2))
        harness.finish(harness.start("s000"), PASS)
        summary = ledger.append(harness.path, "batch_sealed", {"reason": "operator_stop"})
        self.assertEqual((summary["seal_reason"], summary["seal_reasons"]),
                         ("operator_stop", ["operator_stop"]))

    def test_a_second_model_string_is_named_even_when_a_graver_reason_seals(self):
        """The ledger marks the drift where the answer arrives, so a batch that seals for a
        graver reason still names this one among those it met."""
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        harness.model_call(attempt, 1, model="scripted-model")
        harness.model_call(attempt, 2, model="alien-model")
        graver = classify(product_started=True, dispatch_state="none",
                          runner_terminal="completed",
                          evaluation=Evaluation("FAIL", decisive=True,
                                                safety_events=("unauthorized_dispatch",)))
        summary = harness.finish(attempt, graver, calls=2)
        self.assertEqual(summary["pending_seal"], "critical_safety_event")
        self.assertEqual(summary["seal_reasons"],
                         ["critical_safety_event", "model_identity_changed"])
        # It seals for the gravest, and the drift is on the record either way.
        sealed = ledger.append(harness.path, "batch_sealed",
                               {"reason": "critical_safety_event"})
        self.assertEqual(sealed["seal_reason"], "critical_safety_event")
        self.assertIn("model_identity_changed", sealed["seal_reasons"])

    def test_a_batch_takes_its_model_identity_from_its_own_first_answer(self):
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        harness.model_call(attempt, 1, model="scripted-model")
        summary = ledger.read(harness.path)[1]
        self.assertEqual(summary["provider_model"], "scripted-model")
        self.assertEqual(summary["seal_reasons"], [])

    def test_the_apparatus_and_the_product_it_measures_read_one_model_string(self):
        """The apparatus and the product answer to one definition of a model string. With two,
        one bounding the padded string rather than the string, a name of the maximum length
        arriving padded would be recorded as good by the gateway and refused by the real client
        as `provider_usage` -- an answer the apparatus accepted turned into a product failure,
        and a gateway record the classification contract cannot explain.

        The rule is now written once, in the product; the apparatus reads it rather than
        restating it. The table walks the boundary in both directions through both callers, so
        a second definition written anywhere on either side fails here rather than in a batch."""
        from web_operation_agent.modeling.client import SpendMeter, reported_model_string
        from evals.pageagent import host as pageagent_host

        for raw, kept in (("qwen3.8-flash", "qwen3.8-flash"),
                          (" qwen3.8-flash ", "qwen3.8-flash"),
                          ("x" * 120, "x" * 120),
                          ("x" * 120 + " ", "x" * 120),      # the case the two answers split on
                          (" " + "x" * 120, "x" * 120),
                          ("x" * 121, None),
                          ("qwen\n", None),
                          ("   ", None),
                          ("", None),
                          (None, None)):
            with self.subTest(raw=raw):
                self.assertEqual(gateway._reported_model(raw), kept)
                self.assertEqual(reported_model_string(raw), kept)
                if isinstance(raw, str):
                    # The evaluator foundations may not import the product, so they require
                    # the canonical form structurally; that form is this definition's fixed
                    # point, and each must accept a string exactly when it is already canonical.
                    canonical = kept == raw
                    self.assertEqual(ledger._canonical_model_string(raw), canonical)
                    self.assertEqual(bool(pageagent_host._SAFE_MODEL.fullmatch(raw))
                                     and raw == raw.strip(), canonical)
                if kept is None and raw is not None:
                    with self.assertRaises(ValueError):
                        SpendMeter._reported_model(raw)
                else:
                    self.assertEqual(SpendMeter._reported_model(raw), kept or "")

    def test_an_answer_that_names_no_model_is_still_outside_the_response_contract(self):
        """The canonical-form check asks whether a value is already what it would normalise to.
        Null normalises to null, so a successful answer naming no model at all would satisfy it
        -- and the batch would have no model identity to compare anything against."""
        harness = LedgerHarness(self, slots(1))
        attempt = harness.start("s000")
        ledger.append(harness.path, "model_request_dispatched", {
            "attempt_id": attempt, "request_seq": 1, "body_digest": DIGEST,
            "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
            "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                           "tools": 0, "options": {"thinking_budget": 1024}}})
        with self.assertRaisesRegex(ledger.LedgerError, "must report its model string"):
            ledger.append(harness.path, "model_response_received", {
                "attempt_id": attempt, "request_seq": 1, "status": "ok", "http_status": 200,
                "provider_request_id": "req-1", "response_model": None,
                "usage": {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 0,
                          "reasoning_tokens": 0}, "cost_cny": 0.0, "error_code": None})
        # And a padded spelling is refused at the contract, not quietly stored beside a bare one.
        with self.assertRaisesRegex(ledger.LedgerError, "must report its model string"):
            ledger.append(harness.path, "model_response_received", {
                "attempt_id": attempt, "request_seq": 1, "status": "ok", "http_status": 200,
                "provider_request_id": "req-1", "response_model": " qwen3.8-flash ",
                "usage": {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 0,
                          "reasoning_tokens": 0}, "cost_cny": 0.0, "error_code": None})

    def test_a_batch_seals_for_the_most_serious_reason_it_met_not_the_first(self):
        """A batch seals for the most serious reason it met, not whichever arrived first, so the
        timing of a provider answer cannot outrank what the product did."""
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        harness.model_call(attempt, 1, model="first")
        harness.model_call(attempt, 2, model="second")
        summary = ledger.read(harness.path)[1]
        self.assertEqual(summary["pending_seal"], "model_identity_changed")
        unsafe = classify(product_started=True, dispatch_state="acknowledged",
                          runner_terminal="completed",
                          evaluation=Evaluation("FAIL", decisive=True,
                                                safety_events=("unauthorized_dispatch",)))
        summary = harness.finish(attempt, unsafe, calls=2)
        # The safety event outranks the model string the second answer happened to name, and
        # neither reason is lost.
        self.assertEqual((summary["pending_seal"], summary["seal_reasons"]),
                         ("critical_safety_event",
                          ["critical_safety_event", "model_identity_changed"]))
        with self.assertRaises(ledger.LedgerError):
            ledger.append(harness.path, "batch_sealed", {"reason": "model_identity_changed"})
        summary = ledger.append(harness.path, "batch_sealed",
                                {"reason": "critical_safety_event"})
        self.assertEqual(summary["seal_reason"], "critical_safety_event")

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

    def test_a_call_settles_exactly_once_and_an_attempt_ends_only_when_all_of_them_have(self):
        """A call whose answer has not arrived is not left for a record the ledger would no
        longer take. Each one settles once, before the attempt ends."""
        harness = LedgerHarness(self, slots(2))
        attempt = harness.start("s000")
        for seq in (1, 2):
            ledger.append(harness.path, "model_request_dispatched", {
                "attempt_id": attempt, "request_seq": seq, "body_digest": DIGEST,
                "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
                "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                               "tools": 0, "options": {}}})
        unresolved = {"attempt_id": attempt, "request_seq": 1,
                      "reason": "provider_deadline_elapsed"}
        ledger.append(harness.path, "model_response_unresolved", unresolved)
        # Once, and only for a reason the provider's own deadline gives.
        for refused in (unresolved, {**unresolved, "reason": "we_gave_up"}):
            with self.assertRaises(ledger.LedgerError):
                ledger.append(harness.path, "model_response_unresolved", refused)
        with self.assertRaisesRegex(ledger.LedgerError, "does not answer one dispatched request"):
            ledger.append(harness.path, "model_response_received", {
                "attempt_id": attempt, "request_seq": 1, "status": "ok", "http_status": 200,
                "provider_request_id": None, "response_model": "m", "usage": None,
                "cost_cny": None, "error_code": None})
        for kind, body in (
                ("attempt_outcome", {"attempt_id": attempt, **PASS.as_record(),
                                     "evaluation_digest": None, "truth_digest": None}),
                ("reset_outcome", {"attempt_id": attempt, "outcome": "NOT_REQUIRED", "basis": [],
                                   "guard_digest": None, "agent": CLEAN_AGENT}),
                ("attempt_cost", {"attempt_id": attempt, "model_calls": 2,
                                  "usage_unavailable_calls": 2, "input_tokens": 0,
                                  "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0,
                                  "known_cost_cny": 0.0, "total_cost_cny": None}),
                ("attempt_metrics", metrics(attempt))):
            ledger.append(harness.path, kind, body)
        # The second call never settled, so the attempt cannot end on it until it does.
        with self.assertRaisesRegex(ledger.LedgerError, "every call it made has settled"):
            ledger.append(harness.path, "attempt_terminal", {"attempt_id": attempt})
        ledger.append(harness.path, "model_response_unresolved",
                      {**unresolved, "request_seq": 2, "reason": "driver_process_exit"})
        summary = ledger.append(harness.path, "attempt_terminal", {"attempt_id": attempt})
        self.assertIsNone(summary["open_attempt"])

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


class CompletionTests(unittest.TestCase):
    def test_completion_needs_every_slot(self):
        harness = LedgerHarness(self, slots(2))
        harness.finish(harness.start("s000"), PASS)
        with self.assertRaises(ledger.LedgerError):
            ledger.append(harness.path, "batch_completed", {})


class VersionTests(unittest.TestCase):
    def test_a_ledger_of_another_version_is_refused_whole_and_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.jsonl"
            with mock.patch.object(ledger, "LEDGER_VERSION", 1):
                ledger.create(path, header(slots(1)))
                self.assertEqual(ledger.append(path, "batch_sealed",
                                               {"reason": "operator_stop"})["status"], "SEALED")
            written = path.read_bytes()
            with self.assertRaisesRegex(ledger.LedgerError, "ledger version 1"):
                ledger.read(path)
            with self.assertRaisesRegex(ledger.LedgerError, "ledger version 1"):
                ledger.append(path, "unowned_model_request",
                              {"body_digest": DIGEST, "reason": "no_open_attempt"})
            self.assertEqual(path.read_bytes(), written)


class DispositionTests(unittest.TestCase):
    """What a batch may do next, as one value and one table.

    The defects this area invites are not rules anyone left out: they are combinations of
    `open_attempt`, `pending_seal`, `cost_unknown` and `status` that the driver, the entry or the
    harness would read on its own and no one enumerated. They are enumerated here.
    """

    def _running(self):
        return LedgerHarness(self, slots(2))

    def _attempt_open(self):
        harness = self._running()
        harness.start("s000")
        return harness

    def _must_seal(self):
        harness = self._running()
        harness.finish(harness.start("s000"), PASS, reset="FAIL")
        return harness

    def _holding(self):
        harness = self._running()
        attempt = harness.start("s000")
        ledger.append(harness.path, "model_request_dispatched", {
            "attempt_id": attempt, "request_seq": 1, "body_digest": DIGEST,
            "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
            "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                           "tools": 0, "options": {"thinking_budget": 1024}}})
        for kind, body in (
                ("attempt_outcome", {"attempt_id": attempt, **PASS.as_record(),
                                     "evaluation_digest": None, "truth_digest": None}),
                ("reset_outcome", {"attempt_id": attempt, "outcome": "NOT_REQUIRED", "basis": [],
                                   "guard_digest": None, "agent": CLEAN_AGENT}),
                ("attempt_cost", {"attempt_id": attempt, "model_calls": 1,
                                  "usage_unavailable_calls": 1, "input_tokens": 0,
                                  "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0,
                                  "known_cost_cny": 0.0, "total_cost_cny": None}),
                ("attempt_metrics", metrics(attempt)),
                ("model_response_unresolved", {"attempt_id": attempt, "request_seq": 1,
                                               "reason": "provider_deadline_elapsed"}),
                ("attempt_terminal", {"attempt_id": attempt})):
            ledger.append(harness.path, kind, body)
        return harness

    def _sealed(self):
        harness = self._must_seal()
        ledger.append(harness.path, "batch_sealed", {"reason": "reset_failed"})
        return harness

    def _complete(self):
        harness = self._running()
        for slot_id in ("s000", "s001"):
            harness.finish(harness.start(slot_id), PASS)
        ledger.append(harness.path, "batch_completed", {})
        return harness

    def test_each_state_of_the_ledger_has_one_disposition(self):
        for name in ("running", "attempt_open", "must_seal", "holding", "sealed", "complete"):
            with self.subTest(disposition=name):
                summary = ledger.read(getattr(self, f"_{name}")().path)[1]
                self.assertEqual(summary["disposition"], name)
                self.assertIn(summary["disposition"], ledger.DISPOSITIONS)

    def test_what_each_disposition_lets_the_batch_do(self):
        """The table the driver, the entry and the harness now share, asked of the writer itself.

        `complete` is refused everywhere here because every one of these batches still has a slot
        with no attempt; that a full batch completes is :meth:`test_a_full_batch_completes` below.
        """
        expected = {
            #              start  seal(operator_stop)  seal(the one it owes)
            "running":      (True,  True,   None),
            "attempt_open": (False, False,  None),
            "must_seal":    (False, False,  True),
            "holding":      (False, False,  None),
        }
        for name, (starts, stops, owed) in expected.items():
            harness = getattr(self, f"_{name}")()
            attempt = ledger.read(harness.path)[1]["open_attempt"]
            with self.subTest(disposition=name):
                self.assertEqual(self._accepts(harness, "attempt_started", attempt), starts)
                # An operator's stop is the seal a batch takes only while it is running.
                self.assertEqual(self._accepts(harness, "batch_sealed", attempt), stops)
                if owed is not None:
                    self.assertEqual(
                        self._accepts(harness, "batch_sealed", attempt, reason="__owed__"), owed)
                self.assertFalse(self._accepts(harness, "batch_completed", attempt))

    def test_forwarding_follows_the_hold_and_not_the_disposition(self):
        """An attempt open while the batch holds is still `attempt_open` -- the attempt must be
        closed before anything else, whatever it owes -- so whether a call may be forwarded
        follows `cost_unknown`, which is what the gateway reads, and not the disposition. The two
        are asserted apart."""
        open_attempt = self._attempt_open()
        held = self._attempt_open()
        attempt = ledger.read(held.path)[1]["open_attempt"]
        ledger.append(held.path, "model_request_dispatched", {
            "attempt_id": attempt, "request_seq": 1, "body_digest": DIGEST,
            "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
            "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                           "tools": 0, "options": {"thinking_budget": 1024}}})
        ledger.append(held.path, "model_response_unresolved", {
            "attempt_id": attempt, "request_seq": 1, "reason": "provider_deadline_elapsed"})
        summaries = {name: ledger.read(item.path)[1]
                     for name, item in (("free", open_attempt), ("held", held))}
        # Both are the same disposition, and they differ on what a call may do.
        self.assertEqual({item["disposition"] for item in summaries.values()}, {"attempt_open"})
        self.assertFalse(summaries["free"]["cost_unknown"])
        self.assertTrue(summaries["held"]["cost_unknown"])
        self.assertTrue(self._accepts(
            open_attempt, "model_request_dispatched",
            ledger.read(open_attempt.path)[1]["open_attempt"]))
        self.assertFalse(self._accepts(held, "model_request_dispatched", attempt, seq=2))

    def test_a_full_batch_completes_and_a_complete_one_takes_nothing_more(self):
        harness = self._running()
        for slot_id in ("s000", "s001"):
            harness.finish(harness.start(slot_id), PASS)
        self.assertEqual(ledger.read(harness.path)[1]["disposition"], "running")
        self.assertTrue(self._accepts(harness, "batch_completed"))
        ledger.append(harness.path, "batch_completed", {})
        summary = ledger.read(harness.path)[1]
        self.assertEqual((summary["disposition"], summary["status"]), ("complete", "COMPLETE"))
        for kind in ("attempt_started", "batch_sealed", "batch_completed"):
            with self.subTest(kind=kind):
                self.assertFalse(self._accepts(harness, kind))

    def _accepts(self, harness, kind, attempt=None, *, reason="operator_stop", seq=1):
        summary = ledger.read(harness.path)[1]
        # The ledger schedules slots in order, so the probe asks for the one that is next
        # rather than for a slot the batch would refuse whatever its disposition.
        slot = next((item for item in summary["slots"] if not item["attempts"]),
                    summary["slots"][-1])
        bodies = {
            "attempt_started": {
                "attempt_id": "probe", "slot_id": slot["slot_id"], "arm": slot["arm"],
                "task_id": slot["task_id"], "replicate": slot["replicate"], "run_id": "run-probe",
                "fixture_digest": DIGEST, "capability_digest": DIGEST,
                "provider_config_digest": DIGEST, "provider_binding_digest": DIGEST,
                "replacement_of": None},
            "model_request_dispatched": {
                "attempt_id": attempt, "request_seq": seq, "body_digest": DIGEST,
                "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
                "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                               "tools": 0, "options": {"thinking_budget": 1024}}},
            "batch_sealed": {"reason": reason},
            "batch_completed": {},
        }
        # Appended to a copy through the writer itself: an entry this test builds by hand is
        # refused for its shape, which would answer "the batch may not" for every rule alike.
        copy = harness.path.with_name(f"probe-{kind}.jsonl")
        copy.write_bytes(harness.path.read_bytes())
        copy.chmod(0o600)
        if kind == "batch_sealed" and reason == "__owed__":
            bodies[kind] = {"reason": summary["pending_seal"]}
        try:
            ledger.append(copy, kind, bodies[kind])
        except ledger.LedgerError:
            return False
        finally:
            copy.unlink(missing_ok=True)
        return True

    def test_a_batch_that_holds_still_seals_for_a_second_model_string(self):
        """A provider answering under another model string is a fault the batch met, and the
        protocol seals for it at once, whether or not the batch holds for a bill."""
        harness = self._holding()
        summary = ledger.read(harness.path)[1]
        self.assertEqual(summary["disposition"], "holding")
        self.assertTrue(summary["cost_unknown"])
        # The fault it met seals it; the operator's own stop still cannot end it.
        self.assertFalse(self._accepts(harness, "batch_sealed", reason="operator_stop"))
        self.assertTrue(self._accepts(harness, "batch_sealed",
                                      reason="model_identity_changed"))
        sealed = ledger.append(harness.path, "batch_sealed",
                               {"reason": "model_identity_changed"})
        self.assertEqual((sealed["status"], sealed["seal_reason"]),
                         ("SEALED", "model_identity_changed"))

    def test_a_batch_that_holds_still_seals_for_the_fault_it_met(self):
        """The one asymmetry in the table: `must_seal` outranks `holding`, so a safety event
        does not wait on a bill, and an operator's stop still cannot end a batch that owes one."""
        harness = self._holding()
        ledger.append(harness.path, "unowned_model_request",
                      {"body_digest": DIGEST, "reason": "no_attempt_open"})
        summary = ledger.read(harness.path)[1]
        self.assertEqual(summary["disposition"], "must_seal")
        self.assertTrue(summary["cost_unknown"])
        # It seals for the fault, and still not for an operator's stop.
        self.assertTrue(self._accepts(harness, "batch_sealed", reason="__owed__"))
        self.assertFalse(self._accepts(harness, "batch_sealed"))
