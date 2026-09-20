"""Execution transaction for one Odoo evaluation attempt."""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evals.pageagent.host import PageAgentProviderContract

from evals.odoo.domain import outcomes
from evals.odoo.execution import runner_pageagent, runner_woa, trace
from evals.odoo.execution.attempt_results import (
    agent_assertions,
    attempt_cost as _cost,
    final_page as _final_page,
    final_turn_requests,
    scored_terminal,
    trace_counts as _trace_counts,
    turn_bounds,
    turns_before_fault,
    with_residue as _with_residue,
)
from evals.odoo.fixture import fixture
from evals.odoo.judging import capture
from evals.odoo.reporting import ledger
from evals.odoo.provider.gateway import Backend, GatewayCompletionClient, ProviderGateway
from evals.odoo.execution.oracle import ApprovalOracle
from evals.odoo.domain.population import digest_json
from evals.odoo.fixture.projection import actor_projection
from evals.odoo.fixture.rpc import OdooActorClient
from evals.odoo.fixture.stack import REPOSITORY, Stack, StackError
from evals.odoo.domain.tasks import EvaluatorInput, RunnerInput


from evals.odoo.execution.driver import RUN_CUT_EVIDENCE, _PreStart

def run_attempt(self, plan: AttemptPlan) -> dict[str, Any]:
    _, status = ledger.read(self.config.ledger_path)
    attempt_id = f"attempt-{len(status['attempts']) + 1:03d}"
    run_id = "run-" + secrets.token_hex(8)
    arm = plan.slot["arm"]
    evidence = self.config.evidence_root / attempt_id
    self._append("attempt_started", {
        "attempt_id": attempt_id, "slot_id": plan.slot["slot_id"], "arm": arm,
        "task_id": plan.slot["task_id"], "replicate": plan.slot["replicate"],
        "run_id": run_id, "fixture_digest": self.config.fixture_digest,
        "capability_digest": self.config.capability_digests[arm],
        "provider_config_digest": self.config.provider_config_digest,
        "provider_binding_digest": self.gateway.binding_digest(),
        "replacement_of": plan.replacement_of})
    events: list[outcomes.ApparatusEvent] = []
    product_started = False
    # The record of a run that has not happened, in the shape its own runner returns: what
    # the driver reads of an attempt that never started and of one that ended is read alike.
    runner = runner_woa if arm == "woa_product" else runner_pageagent
    artifact: dict[str, Any] = runner.empty_artifact(plan.runner_input)
    projected = trace.project_woa([]) if arm == "woa_product" else trace.project_pageagent(None)
    truth = judged = None
    guard_changes: list[str] = []
    agent_start: dict[str, Any] | None = None
    token = "p74" + secrets.token_urlsafe(32)
    #: What every observation of this attempt belongs to. The evaluator refuses one that
    #: names another attempt, run or arm.
    scope = {"attempt_id": attempt_id, "run_id": run_id, "arm": arm}
    evaluator_job = {"evaluator_input": asdict(plan.evaluator_input),
                     "scope": dict(scope),
                     "rpc_url": self.config.stack.rpc_url,
                     "actor_key_files": {k: str(v) for k, v in
                                         self.config.actor_key_files.items()}}
    self._write(evidence, "evaluator-job.json", evaluator_job)
    opened = False
    settled: dict[int, int] = {}
    checkpoints: list[dict[str, Any]] = []
    try:
        before = self.guard.capture()
        if "baseline_drift" in plan.inject:
            before = {**before, "digest": "sha256:" + "f" * 64}
        if before["digest"] != self.baseline_guard["digest"]:
            raise _PreStart("fixture_baseline_unstable", "fixture_capture")
        if plan.policy is not None:
            self.backend.install(attempt_id, plan.policy, faults=plan.provider_faults,
                                 model_overrides=plan.provider_models)
        self.gateway.open_attempt(attempt_id, token)
        opened = True
        if arm == "woa_product":
            try:
                agent_start = self.config.stack.fresh_agent(token)
            except StackError as exc:
                raise _PreStart("browser_or_control_channel_crash",
                                "control_channel_error") from exc
            # Written before the product starts, so recovering a dead driver still has it.
            self._write(evidence, "agent-start.json", agent_start)
        truth_read = self._evaluator("truth", evidence)
        if not truth_read.available:
            raise _PreStart("evaluator_exception", "evaluator_process_exit")
        truth = truth_read.value
        if "identity_before_start" in plan.inject:
            raise _PreStart("credential_or_identity_before_start", "pre_start_check")

        def checkpoint(value: dict[str, Any]) -> None:
            if value.get("settled"):
                # How many model requests the attempt had made when this turn ended: a turn
                # whose count stays below the first faulty request ended before the fault.
                requests, _, refusals = self._gateway_records(attempt_id)
                settled[int(value["turn"])] = len(requests) + len(refusals)
            if "record" in value:
                # The turn the runner just read, written down before the run goes on and bound
                # into the ledger by its digest, so a driver killed later still has it.
                turn = int(value["turn"])
                name = f"turn-{turn}"
                digest = self._write(evidence, f"{name}.json", {
                    "turn": value["record"], "settled": bool(value.get("settled")),
                    "requests_at_settle": settled.get(turn),
                    "expected_turns": len(plan.runner_input.messages)})
                self._append("attempt_checkpoint", {
                    "attempt_id": attempt_id, "product_started": True,
                    "dispatch_state": "none", "evidence": {name: digest}})
                if "kill_after_turn_settled" in plan.inject and value.get("settled"):
                    # Qualification-only: the driver dies once a turn it read is written
                    # down, so recovery can be shown to judge what that turn showed.
                    os.kill(os.getpid(), signal.SIGKILL)
                return
            if value.get("settled"):
                return
            checkpoints.append(value)
            self._append("attempt_checkpoint", {
                "attempt_id": attempt_id, "product_started": True,
                "dispatch_state": "none", "evidence": {}})
            if "kill_after_product_start" in plan.inject:
                # Qualification-only: a driver process that dies mid-attempt, exactly as a
                # crash would, so recovery can be shown not to erase the attempt.
                os.kill(os.getpid(), signal.SIGKILL)

        if arm == "woa_product":
            oracle = (ApprovalOracle.from_spec(plan.evaluator_input.approval_oracle)
                      if plan.evaluator_input.approval_oracle else None)
            decided = [0]
            attributable = [True]

            def approval_hook(card: dict[str, Any]) -> dict[str, Any]:
                read = capture.read("agent_audit", scope, self.config.stack.export_audit,
                                    errors=(StackError,))
                if not read.available:
                    # Nobody could read what the product proposed, so nothing can be matched
                    # against the card. An unseen proposal is never approved -- and the
                    # proposals this card would have consumed stay unconsumed, so no later
                    # card in this attempt can be told apart from them either.
                    attributable[0] = False
                    return {"decision": "deny", "reasons": ["proposals_unobserved"],
                            "safety_events": []}
                proposals = trace.project_woa(read.value)["proposals"]
                pending = proposals[decided[0]:]
                decided[0] = len(proposals)
                if oracle is None:
                    return {"decision": "deny", "reasons": ["no_write_expected"],
                            "safety_events": []}
                return oracle.decide(visible_effect=card["effect"],
                                     visible_targets=card["targets"],
                                     server_proposals=pending,
                                     attributable=attributable[0])

            artifact = runner_woa.run(plan.runner_input, self.config.woa,
                                      approval_hook=approval_hook, checkpoint=checkpoint,
                                      playwright=self.playwright)
        else:
            client = GatewayCompletionClient(base_url=self.gateway.base_url, token=token,
                                             model=self.config.contract.model,
                                             model_options=self.gateway.budgets.model_options)
            artifact = runner_pageagent.run(plan.runner_input, self.config.pageagent,
                                            client=client, contract=self.config.contract,
                                            run_id=run_id, playwright=self.playwright)
        product_started = artifact["product_started"]
    except _PreStart as exc:
        events.append(outcomes.ApparatusEvent(exc.code, exc.evidence))
    except runner_woa.RunnerPreStartError as exc:
        events.append(outcomes.ApparatusEvent(exc.code, exc.evidence))
    finally:
        if opened:
            self.gateway.close_attempt(attempt_id)
    if "browser_crash" in plan.inject and product_started:
        artifact["browser_exit"] = "injected_crash"
    if "extra_side_effect" in plan.inject and product_started:
        # Qualification-only: a write nobody asked for lands during a read task; the
        # guard and the evaluator must both notice it.
        OdooActorClient(base_url=self.config.stack.rpc_url,
                        api_key_file=self.config.actor_key_files["writer"]).execute(
            "res.partner", "create", [{"name": "P74 Unrequested Side Effect",
                                       "customer_rank": 1}])
    if artifact["browser_exit"]:
        events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                              "browser_exit"))
    elif artifact["control_channel_error"]:
        events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                              "control_channel_error"))
    elif artifact["final_page_error"]:
        # Every turn had ended when the final page could not be read: what the evaluator reads
        # from that page is missing, but nothing shows that the product's ending was lost.
        events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                              "final_page_error"))
    elif artifact["teardown_error"]:
        events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                              "teardown_error"))
    elif product_started and not artifact["logged_out"]:
        # Logging out through the Odoo menu failed after the run: the channel is suspect, but
        # nothing shows that the product's ending was lost.
        events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                              "logout_failed"))
    requests, responses, refusals = self._gateway_records(attempt_id)
    # What the provider did is read from the gateway's typed records, the same for every
    # arm, never from how the runner reported it.
    facts = outcomes.gateway_facts(responses, refusals)
    events.extend(facts.events)
    audit: list[dict[str, Any]] = []
    agent_end: dict[str, Any] | None = None
    audit_read: capture.Capture = capture.Unavailable("product_not_started", "agent_audit",
                                                      scope)
    if arm == "woa_product" and product_started:
        audit_read = capture.read("agent_audit", scope, self.config.stack.export_audit,
                                  errors=(StackError,))
        state_read = capture.read("agent_state", scope, self.config.stack.agent_state,
                                  errors=(StackError,))
        if not audit_read.available or not state_read.available:
            events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                  "control_channel_error"))
        if state_read.available:
            agent_end = state_read.value
            self._write(evidence, "agent-end.json", agent_end)
        audit = audit_read.value if audit_read.available else []
        projected = trace.project_woa(audit)
        if not audit_read.available:
            # Nothing was read of what the product dispatched or counted, which is not the
            # same as having read that it dispatched nothing and did no work.
            projected = {**projected, **trace.UNREAD_TRACE}
    elif arm != "woa_product":
        projected = trace.project_pageagent(artifact["bridge"],
                                            host_errors=artifact["host_errors"],
                                            document_replaced=artifact["document_replaced"])
    recorded_turns = artifact["turns"]
    # The turns the product was sent: those the runner recorded, and one it had sent when the
    # run broke off before recording it.
    sent = (max(len(recorded_turns),
                len({int(value["turn"]) for value in checkpoints if "turn" in value}))
            if product_started else 0)
    if arm == "woa_product" and not audit_read.available and product_started:
        # Nobody read how any turn ended. That is not an audit reporting turns without
        # endings: there are no endings to check, and the fault that lost them is the
        # attempt's evidence.
        final_ending, unclosed = None, ()
    elif arm == "woa_product":
        # The audit records one ending per turn, in order. Every turn before the last must have
        # a closed ending of its own; the last turn's ending is the one scored.
        endings = [item["terminal"] for item in projected["turns"]]
        final_ending = endings[sent - 1] if 0 < sent <= len(endings) else None
        unclosed = outcomes.unclosed_earlier_turns(
            [(endings[index] if index < len(endings) else None, *turn_bounds(settled, index))
             for index in range(sent - 1)], facts)
        if len(endings) > sent:
            unclosed += (f"unexpected_endings:{len(endings)}/{sent}",)
    else:
        final_ending, unclosed = projected["terminal"], ()
    # A provider record explains the last turn's generic failure only if it is numbered after
    # the turn before it ended; a gateway ending replaces it only if its own request is the one
    # the turn ended on.
    in_turn = sent > 0 and outcomes.explained_within(facts, turn_bounds(settled, sent - 1)[0])
    stopped = (bool(recorded_turns) and len(recorded_turns) == sent
               and bool(recorded_turns[-1].get("timed_out")))
    # A turn the runner asked to stop and that did not stop leaves the product running: the
    # attempt is not replaced, and the fixture is restored before the next one starts.
    not_stopped = stopped and recorded_turns[-1].get("stopped") is False
    if not_stopped:
        events.append(outcomes.ApparatusEvent("turn_not_stopped", "stop_not_confirmed"))
    # Only the product's own arm has a budget it can exhaust; the browser-only arm reports
    # its limits as its own ending.
    budget = artifact["budget_exhausted"] if arm == "woa_product" else None
    terminal = scored_terminal(final_ending, facts, budget,
                               final_turn_requests(settled, sent), stopped=stopped)
    gateway_ending = (f"{facts.terminal}@{facts.terminal_seq}" if facts.terminal is not None
                      else None)
    guard_read: capture.Capture = capture.Unavailable("product_not_started", "fixture_guard",
                                                      scope)
    page_read = _final_page(artifact, scope)
    # Each arm's trace is projected from that arm's own record -- WOA's audit, the
    # browser-only arm's in-page bridge -- so it carries the source it was read from and
    # whether that source showed the record whole.
    if arm == "woa_product":
        # An audit that was exported is whole only if it holds an ending for every turn the
        # attempt sent: a truncated export answers, and answering is not being whole.
        projection = (capture.Present(projected, "agent_audit", scope,
                                      complete=len(projected["turns"]) >= sent)
                      if audit_read.available else audit_read)
    else:
        projection = capture.Present(projected, "runner_artifact", scope,
                                     complete=artifact["bridge"] is not None)
    if product_started:
        guard_read = capture.read(
            "fixture_guard", scope,
            lambda: fixture.differences(self.baseline_guard, self.guard.capture()),
            errors=(StackError,))
        if not guard_read.available:
            events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                  "guard_capture_error"))
        guard_changes = guard_read.value if guard_read.available else []
        expected = len(plan.runner_input.messages)
        observation = {
            "arm": arm, "expected_turns": expected,
            "turns": capture.as_record(capture.Present(
                recorded_turns, "runner_artifact", scope,
                complete=len(recorded_turns) == expected)),
            "final": capture.as_record(page_read),
            "trace": capture.as_record(projection),
            # Only the product has an approval channel; the browser-only arm has none, so
            # its empty list is what that arm is, not a record the runner failed to write.
            "approvals": capture.as_record(capture.Present(
                artifact["approvals"] if arm == "woa_product" else [],
                "runner_artifact", scope)),
            "guard_changes": capture.as_record(guard_read)}
        self._write(evidence, "observation.json", observation)
        judgement = self._evaluator("judge", evidence,
                                    inject_exit="evaluator_exit" in plan.inject)
        judged = judgement.value if judgement.available else None
        if judged is None:
            events.append(outcomes.ApparatusEvent("evaluator_exception",
                                                  "evaluator_process_exit"))
    artifact_digest = self._write(evidence, "runner-artifact.json", artifact)
    self._write(evidence, "trace.json", projected)
    if audit:
        self._write(evidence, "agent-audit.json", audit)
    # What the apparatus lost, and what it cut. These are not the same fault: a component
    # nobody could read takes away the failures that rest on it and no others, while a fault
    # that cut the run leaves nothing attributable to the product after it. A fault once the
    # observation was complete -- the screenshot, logging out, closing the browser -- takes
    # away neither.
    lost = {name for name, read in (("final", page_read), ("guard_changes", guard_read),
                                    ("trace", projection)) if not read.available}
    run_cut = (bool(artifact["browser_exit"] or artifact["control_channel_error"])
               or any(event.evidence in RUN_CUT_EVIDENCE for event in facts.events))
    evaluation = None
    if judged is not None:
        # A failure the evaluator read from a turn that ended before the first apparatus
        # fault was already decided; the fault cannot turn it into a missing observation.
        earlier = turns_before_fault(settled, facts.first_event_seq)
        observed = [failure for failure in judged["failures"]
                    if not set(failure["on"]) & lost]
        before_cut = [failure for failure in observed
                      if failure["turn"] is not None and failure["turn"] < earlier]
        if judged["status"] == "PASS":
            # A pass rests on every component its task reads: one of them lost would have
            # failed the judgement instead. Only a cut run takes it away.
            decisive = bool(judged["decisive"]) and not run_cut
        else:
            decisive = bool(judged["decisive"]) and bool(
                judged["safety_events"] or before_cut or (observed and not run_cut))
        evaluation = outcomes.Evaluation(
            status=judged["status"], decisive=decisive,
            reasons=tuple(judged["reasons"]), safety_events=tuple(judged["safety_events"]),
            extraction_ambiguous=bool(judged["extraction_ambiguous"]))
    try:
        classification = outcomes.classify(
            product_started=product_started, dispatch_state=projected["dispatch_state"],
            runner_terminal=terminal if product_started else None,
            apparatus_events=events, evaluation=evaluation,
            request_contract_violation=facts.request_contract_violation,
            gateway_terminal=gateway_ending, provider_record_in_turn=in_turn,
            unclosed_turns=unclosed)
    except outcomes.OutcomeContractError:
        # The fallback keeps the judgement's safety events, which the protocol counts whatever
        # the other axes say, so a branch nothing reaches today would not lose a fact if it
        # ever did.
        classification = outcomes.classify(
            product_started=product_started, dispatch_state=projected["dispatch_state"],
            runner_terminal="classification_input_missing", apparatus_events=events,
            evaluation=(outcomes.Evaluation(
                "FAIL", decisive=False, reasons=evaluation.reasons,
                safety_events=evaluation.safety_events,
                extraction_ambiguous=evaluation.extraction_ambiguous)
                if evaluation is not None else None),
            request_contract_violation=facts.request_contract_violation,
            gateway_terminal=gateway_ending, unclosed_turns=unclosed)
    self._append("attempt_outcome", {
        "attempt_id": attempt_id, **classification.as_record(),
        "evaluation_digest": digest_json(judged) if judged else None,
        "truth_digest": digest_json(truth) if truth else None})
    agent = (agent_assertions(agent_start, agent_end, audit,
                              product_started=product_started,
                              audit_observed=audit_read.available)
             if arm == "woa_product" else None)
    reset = self._reset(plan, guard_changes, agent,
                        forced=(not_stopped or not guard_read.available
                                or (arm == "woa_product" and not audit_read.available)))
    self._append("reset_outcome", {"attempt_id": attempt_id, **reset})
    self._append("attempt_cost", _cost(attempt_id, requests, responses))
    turns = artifact["turns"]
    cancel = [turn["cancel_feedback_ms"] for turn in turns
              if turn["cancel_feedback_ms"] is not None]
    self._append("attempt_metrics", {
        "attempt_id": attempt_id,
        "automatic_wall_ms": sum(turn["automatic_ms"] for turn in turns),
        "approval_wait_ms": sum(turn["approval_wait_ms"] for turn in turns),
        "first_feedback_ms": (turns[0]["first_feedback_ms"] if turns else None),
        "cancel_feedback_ms": max(cancel) if cancel else None,
        **_trace_counts(projected, whole=projection.available and projection.complete),
        "user_turns": len(plan.runner_input.messages)})
    self._append("attempt_terminal", {"attempt_id": attempt_id})
    _, summary = ledger.read(self.config.ledger_path)
    summary["last_attempt_evidence_digest"] = artifact_digest
    return summary

