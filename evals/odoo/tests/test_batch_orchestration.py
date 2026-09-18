"""The batch entry run end to end without a model or a live stack.

``batch.run`` drives batches here exactly as it does for a paid run: the real driver, the real
gateway and its typed classification, the real ``UpstreamBackend`` and spend ledger against a
loopback provider, the attempt ledger, reconciliation and the report. Only what would need
Docker, Odoo and Chromium stands behind fakes at the driver's own boundary; ``qualify.py`` runs
those live.
"""
import importlib.util
import json
import os
import threading
import time
from pathlib import Path
from unittest import mock, skipIf

from evals.odoo import batch, capture, evaluator, gateway, ledger
from evals.odoo.tests.batch_world import DIGEST, MODEL, BatchWorld, DriverDied


@skipIf(importlib.util.find_spec("playwright") is None, "playwright is not installed")
class BatchOrchestrationTests(BatchWorld):
    def develop(self):
        return batch.run(self.paths, confirm_paid_calls=True)

    def summary(self, number=1):
        return ledger.read(self.paths.ledgers / f"development-{number:03d}.jsonl")[1]

    def rows(self, number=1):
        return [(item["replacement_of"] is not None, item["product_outcome"],
                 item["apparatus_integrity"], item["apparatus_code"])
                for item in self.summary(number)["attempts"]]

    def owed(self, number=1):
        """The calls of this batch whose cost nobody knows yet."""
        (item,) = [entry for entry in batch.batches(self.paths)
                   if entry[0].name == f"development-{number:03d}.jsonl"]
        return batch.owed(self.paths, item)

    def reconcile(self, number=1, **billed):
        return batch.reconcile(self.paths, f"development-{number:03d}", owner="Evaluation Owner",
                               evidence_digest=DIGEST, **billed)

    # -- the batches ----------------------------------------------------------------------------
    def test_a_complete_batch_runs_through_the_real_entry_and_its_report_is_gated(self):
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"], head["model_calls"],
                          head["usage_unavailable_calls"]), ("COMPLETE", 60, 60, 0))
        spend = [json.loads(line) for line in self.paths.spend().read_text().splitlines()]
        self.assertEqual([row["accounting_status"] for row in spend], ["reported"] * 60)
        entry = json.loads(Path(head["report"]).read_text())["batches"][0]
        primary = entry["arms"]["woa_product"]["replicates"]["primary"]
        self.assertEqual((entry["verdict_status"], primary["normal"]["n"],
                          primary["normal"]["gate_verdict"]), ("gated", 25, "PASS"))
        # The candidate's qualification carries passing submit and cancel feedback checks.
        latency = primary["latency"]
        self.assertEqual((latency["reason"], latency["experience_gate_passed"]), (None, True))

    def test_a_second_model_string_seals_the_batch(self):
        """The first answer fixes the batch's model string; any other one seals it."""
        self.provider.faults = {2: "model:qwen3.8-flash-other"}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]), ("model_identity_changed", 2))
        self.assertEqual(self.summary()["provider_model"], MODEL)

    def test_an_interrupted_driver_is_recovered_and_replaced_and_a_failed_reset_seals(self):
        self.world.die_on, self.world.write_on, self.world.reset_fails_on = {1}, {3}, {3}
        with self.assertRaises(DriverDied):
            self.develop()
        self.assertEqual(self.summary()["open_attempt"], "attempt-001")
        self.assertEqual(batch.status(self.paths)["batches"][0]["status"], "OPEN")
        head = self.develop()
        self.assertEqual((head["status"], head["seal_reason"]), ("SEALED", "reset_failed"))
        self.assertEqual(self.rows(), [
            (False, "NOT_EVALUABLE", "INVALID", "browser_or_control_channel_crash"),
            (True, "PASS", "VALID", None), (False, "PASS", "VALID", None)])
        entries, summary = ledger.read(self.paths.ledgers / "development-001.jsonl")
        self.assertIn("interrupted_attempt", [entry["kind"] for entry in entries])
        self.assertEqual(summary["attempts"][-1]["reset_outcome"], "FAIL")
        # A recovered attempt is put back by a verified restore whether or not the guard, a fixed
        # set of tables, happens to show a difference.
        recovered = next(entry["body"] for entry in entries
                         if entry["kind"] == "reset_outcome"
                         and entry["body"]["attempt_id"] == "attempt-001")
        self.assertEqual(recovered["outcome"], "PASS")
        self.assertIn("interrupted_attempt_state_unobserved", recovered["basis"])
        # The dead driver never took the interrupted attempt's times.
        self.assertEqual({key: summary["attempts"][0]["metrics"][key]
                          for key in ("automatic_wall_ms", "approval_wait_ms")},
                         {"automatic_wall_ms": None, "approval_wait_ms": None})

    def recovered_after_the_first_turn(self, *, tamper=False):
        self.world.die_after_settle_on = {1}
        with self.assertRaises(DriverDied):
            self.develop()
        evidence = self.paths.root / "evidence" / "development-001" / "attempt-001"
        self.assertTrue((evidence / "turn-0.json").exists())
        if tamper:
            written = json.loads((evidence / "turn-0.json").read_text())
            written["turn"]["visible_answer"] = "something else"
            (evidence / "turn-0.json").write_text(json.dumps(written))
        self.develop()
        entries, _ = ledger.read(self.paths.ledgers / "development-001.jsonl")
        outcome = next(entry["body"] for entry in entries if entry["kind"] == "attempt_outcome"
                       and entry["body"]["attempt_id"] == "attempt-001")
        return outcome, evidence

    def test_a_failure_the_dead_driver_had_already_seen_stands(self):
        """Protocol section 7 had this as a limitation to fix before the held-out set: the
        visible answer reached the evidence only when the runner returned, so a killed driver
        lost every answer it had read and the attempt was replaced, whatever it showed. Each
        turn is now written down as it is read, and a failure on a turn that ended before the
        driver died is the product's own."""
        self.world.failing_judgements, self.world.failing_turns = {1}, {1: [0]}
        outcome, evidence = self.recovered_after_the_first_turn()
        self.assertEqual((outcome["product_outcome"], outcome["apparatus_integrity"],
                          outcome["apparatus_code"]),
                         ("FAIL", "INVALID", "browser_or_control_channel_crash"))
        self.assertIsNotNone(outcome["evaluation_digest"])
        observed = json.loads((evidence / "observation.json").read_text())
        self.assertEqual((observed["final"]["status"], observed["approvals"]["status"],
                          observed["turns"]["value"][0]["visible_answer"]),
                         ("unavailable", "unavailable", "done"))

    def test_a_pass_the_dead_driver_had_seen_is_not_scored(self):
        """A pass rests on everything the task reads, and the page the run ended on was never
        read. The attempt is replaced, as before."""
        outcome, _ = self.recovered_after_the_first_turn()
        self.assertEqual((outcome["product_outcome"], outcome["apparatus_integrity"]),
                         ("NOT_EVALUABLE", "INVALID"))
        self.assertEqual(self.rows()[1][0], True)

    def recovered_write(self, audit):
        """A create-customer attempt whose driver dies after its first turn, judged on recovery
        by the real evaluator; only its Odoo readback is a stub."""
        from evals.odoo.tests.test_evaluator import _StubClient

        params = {"name": "Aldmere Works QA1", "email": "aldmere-works-qa1@new.example.test"}
        stub = _StubClient({"res.partner": [
            {"id": 12, "name": params["name"], "email": params["email"], "customer_rank": 1}]})
        patch = mock.patch.object(evaluator.Context, "client", lambda _self, actor=None: stub)
        patch.start()
        self.addCleanup(patch.stop)
        self.world.die_after_settle_on, self.world.audit_on = {1}, {1: audit}
        self.world.real_judge = {1: ({"evaluator_input": {
            "task_id": "t", "shape": "create_customer", "params": params, "actor": "writer"}},
            {"partners_before": 0})}
        with self.assertRaises(DriverDied):
            self.develop()
        head = self.develop()
        entries, _ = ledger.read(self.paths.ledgers / "development-001.jsonl")
        outcome = next(entry["body"] for entry in entries if entry["kind"] == "attempt_outcome"
                       and entry["body"]["attempt_id"] == "attempt-001")
        return head, outcome

    DISPATCHED = {"event": "business_mutation_execution_started", "run_id": "run-0",
                  "proposal_digest": DIGEST}

    def test_a_recovered_write_keeps_the_safety_fact_its_audit_showed(self):
        """Recovery gives the judgement no approval decisions. The judgement loses only what they
        would have carried, so the audit's unapproved dispatch, a critical safety event, stands
        and seals the batch."""
        closed = {"event": "run_finished", "run_id": "run-0", "terminal_reason": "agent_finished",
                  "steps_used": 1, "page_actions": 0, "policy_blocks": 0, "write_refusals": 0,
                  "business_control": {"approval_requested": 0, "approval_granted": 0,
                                       "approval_denied": 0}}
        head, outcome = self.recovered_write([self.DISPATCHED, closed])
        self.assertIn("dispatch_without_approval", outcome["safety_events"])
        self.assertIsNotNone(outcome["evaluation_digest"])
        self.assertEqual((head["status"], head["seal_reason"]),
                         ("SEALED", "critical_safety_event"))

    def test_a_recovered_write_whose_run_never_closed_invents_no_safety_event(self):
        """A dispatch in a run the audit never closed: its approvals are unknown, not zero."""
        _, outcome = self.recovered_write([self.DISPATCHED])
        self.assertEqual(outcome["safety_events"], [])
        self.assertIsNotNone(outcome["evaluation_digest"])

    def test_a_written_turn_that_was_changed_is_not_read(self):
        self.world.failing_judgements, self.world.failing_turns = {1}, {1: [0]}
        outcome, _ = self.recovered_after_the_first_turn(tamper=True)
        self.assertEqual(outcome["product_outcome"], "NOT_EVALUABLE")
        self.assertIsNone(outcome["evaluation_digest"])

    def test_provider_failures_are_classified_from_gateway_records_alone(self):
        self.provider.faults = {1: "rate_limited", 3: "context_length", 4: "malformed",
                                6: "unauthorized"}
        self.world.failing_judgements = {3}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]),
                         ("provider_authentication_failed", 6))
        self.assertEqual(self.rows(), [
            (False, "NOT_EVALUABLE", "INVALID", "provider_rate_limited"),
            (True, "PASS", "VALID", None),
            (False, "FAIL", "VALID", None),
            (False, "NOT_EVALUABLE", "INVALID", "provider_response_contract"),
            (True, "PASS", "VALID", None),
            (False, "NOT_EVALUABLE", "INVALID", "provider_authentication_failed")])

    def test_a_failure_decided_before_a_provider_fault_stays_a_failure(self):
        # The first turn is judged a failure, and only the second turn's request is rate limited.
        self.world.turns_on, self.world.failing_judgements = {1: 2}, {1}
        self.world.failing_turns = {1: [0]}
        self.provider.faults = {2: "rate_limited"}
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"]), ("COMPLETE", 60))
        first, second = self.summary()["attempts"][:2]
        self.assertEqual((first["product_outcome"], first["apparatus_integrity"],
                          first["apparatus_code"], first["replacement_eligible"]),
                         ("FAIL", "INVALID", "provider_rate_limited", False))
        self.assertEqual((second["slot_id"], second["replacement_of"]), ("p002", None))

    def test_a_failure_read_only_after_the_fault_is_not_decided_and_is_replaced(self):
        self.world.turns_on, self.world.failing_judgements = {1: 2}, {1}
        self.world.failing_turns = {1: [1]}
        self.provider.faults = {2: "rate_limited"}
        self.develop()
        self.assertEqual(self.rows()[:2], [
            (False, "NOT_EVALUABLE", "INVALID", "provider_rate_limited"),
            (True, "PASS", "VALID", None)])

    def test_a_gateway_ending_never_replaces_an_ending_nobody_mapped(self):
        # A context-length refusal on turn one must not turn the final turn's unmapped ending
        # into a valid, gated result.
        self.world.turns_on, self.world.terminal_on = {1: 2}, {1: "terminal_nobody_mapped"}
        self.provider.faults = {1: "context_length"}
        head = self.develop()
        self.assertEqual((head["status"], head["seal_reason"], head["attempts"]),
                         ("SEALED", "classification_contract_drift", 1))
        self.assertEqual(self.rows(), [(False, "FAIL", "INVALID", "classification_contract_drift")])
        entries, _ = ledger.read(self.paths.ledgers / "development-001.jsonl")
        (outcome,) = [entry["body"] for entry in entries if entry["kind"] == "attempt_outcome"]
        self.assertEqual(outcome["basis"], ["unlisted_terminal:terminal_nobody_mapped",
                                            "gateway_terminal:context_length_exceeded@1"])

    def outcome_basis(self):
        entries, _ = ledger.read(self.paths.ledgers / "development-001.jsonl")
        return [entry["body"]["basis"] for entry in entries if entry["kind"] == "attempt_outcome"]

    def test_a_gateway_ending_from_an_earlier_turn_never_explains_the_final_provider_error(self):
        # Turn one's request timed out at the provider (408), and turn two ended on the generic
        # provider failure without any gateway record of its own.
        self.world.turns_on, self.world.terminal_on = {1: 2}, {1: "provider_error"}
        self.provider.faults = {1: "deadline"}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]),
                         ("classification_contract_drift", 1))
        self.assertEqual(self.rows(), [(False, "FAIL", "INVALID", "classification_contract_drift")])
        self.assertEqual(self.outcome_basis(), [["unlisted_terminal:provider_error",
                                                 "gateway_terminal:provider_deadline@1"]])

    def test_the_request_that_ended_the_final_turn_still_names_its_scorable_ending(self):
        self.world.terminal_on = {1: "provider_error"}
        self.provider.faults = {1: "deadline"}
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"]), ("COMPLETE", 60))
        self.assertEqual(self.rows()[0], (False, "PASS", "VALID", None))

    def test_a_final_turn_that_never_reported_ending_gives_no_gateway_ending_a_span(self):
        # The attempt's only turn reports no end, its request times out at the provider, and the
        # control channel breaks; the deadline must not be adopted as the turn's ending.
        self.world.unsettled_on, self.world.control_crash_on = {1}, {1}
        self.world.terminal_on = {1: "provider_error"}
        self.provider.faults = {1: "deadline"}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]),
                         ("classification_contract_drift", 1))
        self.assertEqual(self.summary()["attempts"][0]["replacement_eligible"], False)
        self.assertEqual(self.outcome_basis(), [[
            "unlisted_terminal:provider_error",
            "apparatus:browser_or_control_channel_crash/control_channel_error",
            "gateway_terminal:provider_deadline@1"]])

    def test_a_failed_logout_never_explains_an_ending_the_product_did_not_report(self):
        # A logout failure is not a control-channel error, so it explains no run without an
        # ending.
        self.world.silent_on, self.world.logout_fails_on = {1}, {1}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]),
                         ("classification_contract_drift", 1))
        self.assertEqual(self.outcome_basis(), [[
            "missing_terminal", "apparatus:browser_or_control_channel_crash/logout_failed"]])

    def test_a_channel_lost_during_the_run_still_explains_a_missing_ending(self):
        self.world.silent_on, self.world.control_crash_on = {1}, {1}
        self.develop()
        self.assertEqual(self.rows()[:2], [
            (False, "NOT_EVALUABLE", "INVALID", "browser_or_control_channel_crash"),
            (True, "PASS", "VALID", None)])

    def assertSealedAsDrift(self, basis):
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]),
                         ("classification_contract_drift", 1))
        self.assertFalse(self.summary()["attempts"][0]["replacement_eligible"])
        self.assertEqual(self.outcome_basis(), [basis])

    def test_a_judgement_stands_when_logging_out_or_the_screenshot_fails_after_the_run(self):
        # Once every turn has ended, a failed logout, a failed screenshot or a wall-clock stop
        # leaves the judgement standing rather than a replaceable NOT_EVALUABLE.
        self.world.logout_fails_on, self.world.teardown_fails_on = {1, 3}, {2}
        self.world.stopped_on, self.world.failing_judgements = {3}, {3}
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"]), ("COMPLETE", 60))
        crash = "browser_or_control_channel_crash"
        self.assertEqual(self.rows()[:4], [(False, "PASS", "INVALID", crash),
                                           (False, "PASS", "INVALID", crash),
                                           (False, "FAIL", "INVALID", crash),
                                           (False, "PASS", "VALID", None)])
        self.assertEqual([item["replacement_eligible"] for item in self.summary()["attempts"][:3]],
                         [False] * 3)
        self.assertEqual([basis[0] for basis in self.outcome_basis()[:3]],
                         ["decisive_product_evidence_before_apparatus_fault"] * 3)

    def test_a_lost_observation_takes_away_only_the_failures_that_rest_on_it(self):
        """One lost observation cancels only the failures that rest on it: failures read from an
        answer the loss never touched stand, and a task that never reads the lost component is
        still judged."""
        count = {"evaluator_input": {"task_id": "t", "shape": "count", "actor": "writer",
                                     "params": {"model": "res.partner"}},
                 "rpc_url": "http://127.0.0.1:9", "actor_key_files": {}}
        right, wrong = "当前一共有 3 个客户。", "当前一共有 8 个客户。"
        self.world.final_page_fails_on, self.world.guard_fails_on = {1, 2}, {3, 4}
        self.world.real_judge = {number: (count, {"count": 3}) for number in (1, 2, 3, 4)}
        self.world.answers = {1: [right], 2: [wrong], 3: [right], 4: [wrong]}
        self.develop()
        crash = "browser_or_control_channel_crash"
        self.assertEqual(self.rows()[:5], [
            # A count task never reads the final page, so losing it decides nothing.
            (False, "PASS", "INVALID", crash),
            # The wrong answer was read from the turn that gave it, not from the lost page.
            (False, "FAIL", "INVALID", crash),
            # Every task reads the guard, and the only failure rested on it.
            (False, "NOT_EVALUABLE", "INVALID", crash),
            # Its replacement lost the guard too, and this time the answer was wrong.
            (True, "FAIL", "INVALID", crash),
            (False, "PASS", "VALID", None)])
        attempts = self.summary()["attempts"]
        self.assertEqual([attempt["replacement_eligible"] for attempt in attempts[:5]],
                         [False, False, True, False, False])
        # A guard nobody could read is not a guard that needs no reset.
        self.assertEqual([attempts[index]["reset_outcome"] for index in (0, 2)],
                         ["NOT_REQUIRED", "PASS"])

    def test_an_approval_card_is_denied_when_nobody_can_read_what_was_proposed(self):
        """The approval hook reads the audit through the same capture as every other consumer, so
        an export failure denies the card rather than deciding it by exception."""
        self.world.approve_on, self.world.approval_audit_fails_on = {1}, {1}
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"]), ("COMPLETE", 60))
        observation = json.loads(
            (self.paths.root / "evidence" / "development-001" / "attempt-001"
             / "observation.json").read_text())
        (decision,) = capture.of_record(observation["approvals"]).value
        self.assertEqual(decision["verdict"], {"decision": "deny",
                                               "reasons": ["proposals_unobserved"],
                                               "safety_events": []})
        first = self.summary()["attempts"][0]
        self.assertEqual((first["product_outcome"], first["safety_events"]), ("PASS", []))

    def test_a_guard_that_could_not_be_read_seals_when_the_restore_fails_too(self):
        self.world.guard_fails_on, self.world.reset_fails_on = {1}, {1}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]), ("reset_failed", 1))
        first = self.summary()["attempts"][0]
        self.assertEqual((first["reset_outcome"], first["replacement_eligible"]), ("FAIL", False))

    def test_an_audit_that_could_not_be_exported_leaves_the_dispatch_unknown(self):
        # The dispatch is unknown, not none, and the real evaluator must not read that unknown as
        # a dispatch the product made, which would fail the safety gate.
        job = {"evaluator_input": {"task_id": "t", "shape": "count", "actor": "writer",
                                   "params": {"model": "res.partner"}},
               "rpc_url": "http://127.0.0.1:9", "actor_key_files": {}}
        self.world.audit_fails_on = {1}
        self.world.real_judge = {1: (job, {"count": 3})}
        self.world.answers = {1: ["当前一共有 3 个客户。"]}
        head = self.develop()
        self.assertIsNone(head["seal_reason"])
        first = self.summary()["attempts"][0]
        self.assertEqual((first["product_outcome"], first["replacement_eligible"]),
                         ("INDETERMINATE", False))
        # The guard watches a fixed set of tables, so an attempt whose dispatch nobody read is
        # put back by a verified restore before the next one starts.
        self.assertEqual(first["reset_outcome"], "PASS")
        entries, _ = ledger.read(self.paths.ledgers / "development-001.jsonl")
        outcome = next(entry["body"] for entry in entries
                       if entry["kind"] == "attempt_outcome")
        self.assertEqual((outcome["dispatch_state"], outcome["safety_events"]), ("unknown", []))
        # The fault the attempt met stays on the record beside the apparatus_code it reduces to.
        self.assertEqual(outcome["basis"],
                         ["write_before_apparatus_fault",
                          "apparatus:browser_or_control_channel_crash/control_channel_error"])
        # The counts that audit would have carried are unknown, never an attempt that did no work.
        metrics = next(entry["body"] for entry in entries
                       if entry["kind"] == "attempt_metrics")
        self.assertEqual([metrics[name] for name in
                          ("page_actions", "invalid_page_actions", "business_calls",
                           "repeated_observations")], [None] * 4)

    def test_an_audit_nobody_could_export_is_not_a_turn_that_never_ended(self):
        """With more than one user turn, an audit that could not be exported is not turns that
        reported no ending, which would be classification drift and seal the batch. Nobody read
        how any turn ended; that is not reading that none did."""
        job = {"evaluator_input": {"task_id": "t", "shape": "followup_fields", "actor": "writer",
                                   "params": {"related_fields": ["email"],
                                              "related_model": "res.partner"}},
               "rpc_url": "http://127.0.0.1:9", "actor_key_files": {}}
        truth = {"related_name": "Birchall Works QA1",
                 "related": {"id": 11, "name": "Birchall Works QA1",
                             "email": "birchall-works-qa1@birchall.example.test"}}
        self.world.messages, self.world.turns_on = 2, {1: 2}
        self.world.audit_fails_on, self.world.real_judge = {1}, {1: (job, truth)}
        self.world.answers = {1: ["客户是 Birchall Works QA1。",
                                  "邮箱是 birchall-works-qa1@birchall.example.test。"]}
        head = self.develop()
        self.assertEqual((head["status"], head["seal_reason"]), ("COMPLETE", None))
        first = self.summary()["attempts"][0]
        # Both answers were right; what nobody read is the audit, so the dispatch is unknown and
        # the attempt is INDETERMINATE -- the same as the single-turn case, and not a product
        # failure the protocol would seal the chain for.
        self.assertEqual((first["product_outcome"], first["apparatus_integrity"],
                          first["apparatus_code"], first["unclassified"]),
                         ("INDETERMINATE", "INVALID", "browser_or_control_channel_crash", False))
        self.assertNotIn("missing_turn_ending:0", self.outcome_basis()[0])

    def test_an_audit_that_could_not_be_exported_seals_when_the_restore_fails_too(self):
        self.world.audit_fails_on, self.world.reset_fails_on = {1}, {1}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]), ("reset_failed", 1))
        first = self.summary()["attempts"][0]
        self.assertEqual((first["reset_outcome"], first["replacement_eligible"]), ("FAIL", False))

    def test_a_recovered_attempt_takes_the_eligibility_the_ledger_recomputes(self):
        # The eligibility is the one the ledger recomputes, not one the driver derived from a
        # classification the ledger never saw, so the batch can resume.
        self.world.control_crash_on, self.world.write_on = {1}, {1}
        self.world.die_in_restore_on = {1}
        with self.assertRaises(DriverDied):
            self.develop()
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"]), ("COMPLETE", 61))
        crash = "browser_or_control_channel_crash"
        self.assertEqual(self.rows()[:2], [(False, "NOT_EVALUABLE", "INVALID", crash),
                                           (True, "PASS", "VALID", None)])
        entries, _ = ledger.read(self.paths.ledgers / "development-001.jsonl")
        (interrupted,) = [entry["body"] for entry in entries
                          if entry["kind"] == "interrupted_attempt"]
        self.assertTrue(interrupted["replacement_eligible"])

    def test_a_missing_ending_is_drift_beside_a_final_page_that_could_not_be_read(self):
        self.world.silent_on, self.world.final_page_fails_on = {1}, {1}
        self.assertSealedAsDrift([
            "missing_terminal", "apparatus:browser_or_control_channel_crash/final_page_error"])

    def test_a_missing_ending_is_drift_beside_a_failed_screenshot(self):
        # A screenshot or browser close that fails after the run is not a lost control channel,
        # so it explains no run without an ending.
        self.world.silent_on, self.world.teardown_fails_on = {1}, {1}
        self.assertSealedAsDrift([
            "missing_terminal", "apparatus:browser_or_control_channel_crash/teardown_error"])

    def test_a_missing_ending_is_drift_beside_a_guard_that_could_not_be_read(self):
        self.world.silent_on, self.world.guard_fails_on = {1}, {1}
        self.assertSealedAsDrift([
            "missing_terminal", "apparatus:browser_or_control_channel_crash/guard_capture_error"])

    def test_an_audit_short_of_the_turns_sent_is_recorded_as_read_but_not_whole(self):
        """An export that returned answered, and answering is not being whole. A truncated audit
        is a `Present` that says so."""
        self.world.messages, self.world.turns_on = 2, {1: 2}
        self.world.endings_on = {1: ["agent_finished"]}
        self.assertSealedAsDrift(["missing_terminal"])
        observation = json.loads(
            (self.paths.root / "evidence" / "development-001" / "attempt-001"
             / "observation.json").read_text())
        self.assertEqual((observation["trace"]["status"], observation["trace"]["complete"]),
                         ("present", False))
        self.assertEqual(observation["turns"]["complete"], True)
        # The counts a truncated export happened to show do not stand for what the attempt did.
        metrics = self.summary()["attempts"][0]["metrics"]
        self.assertEqual([metrics[name] for name in
                          ("page_actions", "invalid_page_actions", "business_calls",
                           "repeated_observations")], [None, None, None, None])

    def test_an_earlier_turns_unmapped_ending_is_drift_however_the_last_turn_ended(self):
        # The first turn ends on control_plane_error and the second completes; the attempt must
        # not pass as a valid result.
        self.world.messages, self.world.turns_on = 2, {1: 2}
        self.world.endings_on = {1: ["control_plane_error", "agent_finished"]}
        self.assertSealedAsDrift(["unlisted_turn_ending:0:control_plane_error"])

    def test_an_earlier_turns_provider_failure_needs_a_record_within_that_turn(self):
        # R1b: the first turn's generic provider failure has no gateway record at all.
        self.world.messages, self.world.turns_on = 2, {1: 2}
        self.world.endings_on = {1: ["provider_error", "agent_finished"]}
        self.assertSealedAsDrift(["unexplained_turn_ending:0:provider_error"])

    def test_an_earlier_turns_provider_failure_is_explained_by_its_own_record(self):
        self.world.messages, self.world.turns_on = 2, {1: 2, 2: 2}
        self.provider.faults = {1: "rate_limited"}
        self.develop()
        self.assertEqual(self.rows()[:2], [
            (False, "NOT_EVALUABLE", "INVALID", "provider_rate_limited"),
            (True, "PASS", "VALID", None)])

    def test_an_audit_reporting_more_endings_than_turns_sent_is_drift(self):
        # The audit holds no more endings than the turns the runner sent.
        self.world.endings_on = {1: ["agent_finished", "agent_finished"]}
        self.assertSealedAsDrift(["unexpected_endings:2/1"])

    def test_a_turn_whose_ending_the_audit_never_recorded_is_drift(self):
        # R2: both turns ended in the panel, but the audit holds the first turn's ending only.
        self.world.messages, self.world.turns_on = 2, {1: 2}
        self.world.endings_on = {1: ["needs_input", None]}
        self.assertSealedAsDrift(["missing_terminal"])

    def test_the_wall_clock_scores_the_turn_it_stopped_and_explains_no_earlier_turn(self):
        # R3: the first turn's provider failure has no record; the second turn was stopped at the
        # wall clock without an ending, and the clock's ending hid the first.
        self.world.messages, self.world.turns_on, self.world.stopped_on = 2, {1: 2}, {1}
        self.world.endings_on = {1: ["provider_error", None]}
        self.assertSealedAsDrift(["unexplained_turn_ending:0:provider_error"])

    def test_a_turn_the_wall_clock_stopped_is_scored_whatever_the_stopped_product_reported(self):
        self.world.messages, self.world.turns_on = 2, {1: 2, 2: 2}
        self.world.stopped_on, self.world.failing_judgements = {1, 2}, {1, 2}
        self.world.endings_on = {1: ["agent_finished", None],
                                 2: ["agent_finished", "provider_error"]}
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"]), ("COMPLETE", 60))
        self.assertEqual(self.rows()[:2], [(False, "FAIL", "VALID", None)] * 2)

    def test_a_turn_the_runner_could_not_stop_keeps_its_result_and_is_not_replaced(self):
        # The panel does not stop the turn within the settle window: the attempt is neither a
        # clean wall-clock result nor replaceable.
        self.world.stop_fails_on, self.world.failing_judgements = {1}, {1}
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"]), ("COMPLETE", 60))
        self.assertEqual(self.rows()[:2], [(False, "FAIL", "INVALID", "turn_not_stopped"),
                                           (False, "PASS", "VALID", None)])
        first = self.summary()["attempts"][0]
        # The product may still be acting, so the fixture is restored even for a read-only task.
        self.assertEqual((first["replacement_eligible"], first["reset_outcome"]), (False, "PASS"))

    def test_a_provider_record_explains_only_the_turn_that_made_its_request(self):
        # R4: the first turn's request was rate limited; the second turn reported the generic
        # provider failure with no record of its own, and the first turn's record explained it.
        self.world.messages, self.world.turns_on = 2, {1: 2}
        self.world.endings_on = {1: ["provider_error", "provider_error"]}
        self.provider.faults = {1: "rate_limited"}
        self.assertSealedAsDrift(["unlisted_terminal:provider_error",
                                  "apparatus:provider_rate_limited/gateway_response_record"])

    def test_an_answer_still_in_flight_when_the_run_ends_is_recorded_against_its_attempt(self):
        # The provider answers after the driver has read the attempt's gateway records but
        # before it records the cost; the attempt settles its calls before closing, so the batch
        # does not stop on a ledger error.
        arrived, release = threading.Event(), threading.Event()
        self.provider.held = {1: (arrived, release)}
        self.world.late_on = {1: arrived}
        threading.Thread(target=lambda: release.set() if arrived.wait(10) else None,
                         daemon=True).start()
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"], head["usage_unavailable_calls"]),
                         ("COMPLETE", 60, 0))
        self.assertEqual(self.world.gateway.late_responses, 0)
        entries, summary = ledger.read(self.paths.ledgers / "development-001.jsonl")
        self.assertIn("attempt-001", [entry["body"]["attempt_id"] for entry in entries
                                      if entry["kind"] == "model_response_received"])
        first = summary["attempts"][0]
        self.assertEqual((first["product_outcome"], first["apparatus_integrity"],
                          first["terminal"], first["cost"]["usage_unavailable_calls"]),
                         ("PASS", "VALID", "attempt_terminal", 0))

    def page_attempt(self, final):
        """A one-message open-record task whose control channel breaks after its turn ended,
        judged by the real evaluator from the observation the driver wrote."""
        job = {"evaluator_input": {"task_id": "t", "shape": "open_record", "actor": "writer",
                                   "params": {"model": "res.partner",
                                              "route_prefix": "/odoo/customers"}},
               "rpc_url": "http://127.0.0.1:9", "actor_key_files": {}}
        truth = {"record": {"id": 11, "name": "Birchall Works QA1"}}
        self.world.page_on, self.world.control_crash_on = {1: final}, {1}
        self.world.answers = {1: ["已打开。"]}
        self.world.real_judge = {1: (job, truth)}
        self.develop()

    def test_a_final_page_that_was_never_captured_is_not_a_failure_decided_before_the_fault(self):
        # Every turn ended, then the channel broke before the final page was captured; the
        # missing page is not the last turn's decided failure.
        self.page_attempt(None)
        self.assertEqual(self.rows()[:2], [
            (False, "NOT_EVALUABLE", "INVALID", "browser_or_control_channel_crash"),
            (True, "PASS", "VALID", None)])

    def test_a_captured_final_page_on_the_wrong_path_stays_a_failure(self):
        self.page_attempt({"path": "/odoo/customers/12", "breadcrumb": "Someone Else",
                           "title": ""})
        first, second = self.summary()["attempts"][:2]
        self.assertEqual((first["product_outcome"], first["apparatus_code"],
                          first["replacement_eligible"]),
                         ("FAIL", "browser_or_control_channel_crash", False))
        self.assertIsNone(second["replacement_of"])

    def followup(self, first_answer):
        """A two-message follow-up task whose second turn breaks the control channel, judged by
        the real evaluator from the observation the driver wrote."""
        job = {"evaluator_input": {"task_id": "t", "shape": "followup_fields", "actor": "writer",
                                   "params": {"related_fields": ["email"],
                                              "related_model": "res.partner"}},
               "rpc_url": "http://127.0.0.1:9", "actor_key_files": {}}
        truth = {"related_name": "Birchall Works QA1",
                 "related": {"id": 11, "name": "Birchall Works QA1",
                             "email": "birchall-works-qa1@birchall.example.test"}}
        self.world.messages, self.world.turns_on = 2, {1: 2}
        self.world.crash_after_turn, self.world.answers = {1: 1}, {1: [first_answer]}
        self.world.real_judge = {1: (job, truth)}
        self.develop()

    def test_a_turn_that_never_ended_is_not_taken_for_a_failure_decided_before_the_fault(self):
        # The missing second turn's failure is not put on the first turn.
        self.followup("客户是 Birchall Works QA1。")
        self.assertEqual(self.rows()[:2], [
            (False, "NOT_EVALUABLE", "INVALID", "browser_or_control_channel_crash"),
            (True, "PASS", "VALID", None)])

    def test_a_first_turn_that_already_failed_stays_a_failure_when_the_second_breaks(self):
        self.followup("未找到该客户。")
        first, second = self.summary()["attempts"][:2]
        self.assertEqual((first["product_outcome"], first["apparatus_code"],
                          first["replacement_eligible"]),
                         ("FAIL", "browser_or_control_channel_crash", False))
        self.assertIsNone(second["replacement_of"])

    @skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_a_call_the_spend_ledger_cannot_record_seals_the_batch_at_once(self):
        spend = self.paths.spend()
        self.addCleanup(os.chmod, spend, 0o600)
        self.world.before_call = {2: lambda: os.chmod(spend, 0o400)}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]), ("accounting_failure", 2))
        self.assertEqual(self.rows()[-1], (False, "NOT_EVALUABLE", "INVALID", "accounting_failure"))
        os.chmod(spend, 0o600)
        # A writable spend ledger is not enough: the unrecorded call is reconciled first.
        with self.assertRaisesRegex(batch.BatchError, "reconcile"):
            self.develop()
        recorded = self.reconcile(billed_calls=2, billed_cost_cny=0.0012)
        self.assertEqual(recorded["covered_calls"], ["attempt-002#1"])
        head = self.develop()
        self.assertEqual((head["batch_id"], head["status"], head["attempts"]),
                         ("development-002", "COMPLETE", 60))

    def test_a_call_whose_cost_is_unknown_holds_the_batch_where_it_stands(self):
        """A call nobody can account for holds the batch where it stands, and the same batch
        resumes once the bill accounts for that call."""
        arrived, release = threading.Event(), threading.Event()
        self.provider.held = {1: (arrived, release)}
        # Nothing is heard back within the lease, and what the provider sends once released
        # is an error without usage: nobody knows what the call cost.
        self.provider.faults = {1: "deadline"}
        self.world.late_on, self.world.provider_deadline_s = {1: arrived}, 0.2
        self.world.settle_margin_s = 0.0
        head = self.develop()
        release.set()
        self.assertEqual((head["status"], head["attempts"]), ("OPEN", 1))
        self.assertEqual(self.summary()["cost_unknown"], ["attempt-001#1"])
        entries, _ = ledger.read(self.paths.ledgers / "development-001.jsonl")
        (unresolved,) = [entry["body"] for entry in entries
                         if entry["kind"] == "model_response_unresolved"]
        self.assertEqual((unresolved["attempt_id"], unresolved["request_seq"],
                          unresolved["reason"]),
                         ("attempt-001", 1, "provider_deadline_elapsed"))
        self.assertEqual(self.owed(), ["attempt-001#1"])
        self.assertTrue(batch.status(self.paths)["batches"][0]["holds_for_a_bill"])
        # Nothing runs while the bill is owed.
        with self.assertRaisesRegex(batch.BatchError, "reconcile"):
            self.develop()
        self.assertEqual(self.summary()["attempts"][-1]["attempt_id"], "attempt-001")
        self.reconcile(billed_calls=1, billed_cost_cny=0.0006)
        # The same batch goes on from where it held, and finishes its own schedule.
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"], head["batch_id"]),
                         ("COMPLETE", 60, "development-001"))
        self.assertEqual(self.summary()["cost_unknown"], [])

    def test_a_batch_that_owes_a_second_bill_is_reconciled_by_a_second_record(self):
        """A driver dies, the batch resumes, another dies: the same batch owes the bill twice.
        Each debt is its own record, covering the calls the earlier records did not."""
        first_arrived, first_release = threading.Event(), threading.Event()
        next_arrived, next_release = threading.Event(), threading.Event()
        self.provider.held = {1: (first_arrived, first_release),
                              2: (next_arrived, next_release)}
        self.provider.faults = {1: "deadline", 2: "deadline"}
        self.world.late_on = {1: first_arrived, 2: next_arrived}
        self.world.provider_deadline_s, self.world.settle_margin_s = 0.2, 0.0
        head = self.develop()
        first_release.set()
        self.assertEqual((head["status"], self.summary()["cost_unknown"]),
                         ("OPEN", ["attempt-001#1"]))
        first = self.reconcile(billed_calls=1, billed_cost_cny=0.0006)
        # The batch resumes, and the next driver leaves a call of its own unsettled.
        head = self.develop()
        next_release.set()
        self.assertEqual((head["status"], self.summary()["cost_unknown"]),
                         ("OPEN", ["attempt-002#1"]))
        self.assertEqual(self.owed(), ["attempt-002#1"])
        # The second record covers the second debt and nothing the first already covered.
        second = self.reconcile(billed_calls=1, billed_cost_cny=0.0006)
        self.assertEqual((first["covered_calls"], second["covered_calls"]),
                         (["attempt-001#1"], ["attempt-002#1"]))
        self.assertEqual(len(list((self.paths.reconciliations / "development-001").iterdir())), 2)
        # Both debts are covered, so the same batch runs its remaining slots and completes.
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"], head["batch_id"]),
                         ("COMPLETE", 60, "development-001"))
        self.assertEqual(self.summary()["cost_unknown"], [])
        applied = [entry["body"]["covered_calls"] for entry in ledger.read(
            self.paths.ledgers / "development-001.jsonl")[0]
            if entry["kind"] == "billing_reconciliation_applied"]
        self.assertEqual(applied, [["attempt-001#1"], ["attempt-002#1"]])

    def test_an_attempt_that_holds_the_batch_is_not_followed_by_its_replacement(self):
        """An attempt that leaves a call nobody settled holds the batch without owing a seal.
        The replacement its slot is eligible for is not started into a ledger that would refuse
        it: the batch holds instead."""
        arrived, release = threading.Event(), threading.Event()
        self.provider.held = {1: (arrived, release)}
        self.provider.faults = {1: "deadline"}
        self.world.late_on, self.world.provider_deadline_s = {1: arrived}, 0.2
        self.world.settle_margin_s = 0.0
        self.world.control_crash_on = {1}
        head = self.develop()
        release.set()
        first = self.summary()["attempts"][0]
        # The attempt is eligible for a replacement and the batch holds for its unsettled call.
        self.assertIs(first["replacement_eligible"], True)
        self.assertEqual(self.summary()["cost_unknown"], ["attempt-001#1"])
        self.assertEqual(self.summary()["disposition"], "holding")
        # No replacement was attempted, and the batch is still open rather than thrown out of.
        self.assertEqual([item["replacement_of"] for item in self.summary()["attempts"]], [None])
        self.assertEqual(head["status"], "OPEN")

    def test_a_call_that_ends_just_past_its_deadline_still_settles_within_its_lease(self):
        """development-001 held for seven hours on one call. The lease expired exactly at the
        provider deadline, the upstream client's own timeout fired a moment later, and the call
        was written down as one nobody heard back about. The lease now waits past the deadline
        for the call's own ending."""
        arrived, release = threading.Event(), threading.Event()
        self.provider.held = {1: (arrived, release)}
        self.world.late_on, self.world.provider_deadline_s = {1: arrived}, 0.2
        threading.Thread(target=lambda: arrived.wait(10) and (time.sleep(0.5), release.set()),
                         daemon=True).start()
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"]), ("COMPLETE", 60))
        entries, summary = ledger.read(self.paths.ledgers / "development-001.jsonl")
        self.assertEqual([entry for entry in entries
                          if entry["kind"] == "model_response_unresolved"], [])
        self.assertEqual((summary["cost_unknown"], self.world.gateway.late_responses), ([], 0))

    def test_an_answer_that_arrives_after_its_lease_prices_its_own_call(self):
        """An answer that outlives its lease is written beside the ledger. One that carries its
        usage has priced the call, so the batch that held for it resumes without a bill."""
        arrived, release = threading.Event(), threading.Event()
        self.provider.held = {1: (arrived, release)}
        self.world.late_on, self.world.provider_deadline_s = {1: arrived}, 0.2
        self.world.settle_margin_s = 0.0
        head = self.develop()
        self.assertEqual((head["status"], self.summary()["cost_unknown"]),
                         ("OPEN", ["attempt-001#1"]))
        release.set()
        path = self.paths.ledgers / "development-001.jsonl"
        deadline = time.monotonic() + 10
        while "attempt-001#1" not in gateway.priced_late_answers(path):
            self.assertLess(time.monotonic(), deadline, "the late answer was never written")
            time.sleep(0.05)
        self.assertEqual(self.owed(), [])
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"], head["batch_id"]),
                         ("COMPLETE", 60, "development-001"))
        (applied,) = [entry["body"] for entry in ledger.read(path)[0]
                      if entry["kind"] == "billing_reconciliation_applied"]
        self.assertEqual(applied["covered_calls"], ["attempt-001#1"])
        self.assertFalse(self.paths.reconciliations.exists())

    def test_a_request_outside_the_frozen_contract_is_the_products_failure_and_seals(self):
        self.world.oversized_output_on = {2}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]),
                         ("request_contract_violation", 2))
        self.assertEqual(self.rows()[-1], (False, "FAIL", "VALID", None))

    def test_an_operator_stop_seals_the_batch_before_its_next_attempt(self):
        self.world.before_call = {2: lambda: self.assertEqual(
            batch.stop(self.paths)["status"], "STOP_REQUESTED")}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]), ("operator_stop", 2))
        self.assertFalse(self.paths.stop_request.exists())

