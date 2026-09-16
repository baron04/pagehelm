"""Serial attempt driver for P7.4 batches.

For every scheduled slot the driver writes ``attempt_started`` before anything else, checks the
fixture against its qualified baseline, gives WOA a fresh verified-empty state and a new
provider token, computes expected truth in a separate evaluator process, runs the arm's
real-Chromium runner, captures the guard, judges in a separate evaluator process, classifies
the three result axes, resets when the task or the observed state requires it, and closes the
attempt with cost and metrics derived from the gateway's own ledger records. Nothing is
retried: an attempt that qualifies for replacement under the closed policy gets exactly one
replacement, immediately, on the same slot.

The runner receives only ``RunnerInput``. The evaluator specification stays in the driver and
reaches the evaluator process and the approval oracle, never the runner.
"""
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

from . import fixture, ledger, outcomes, runner_pageagent, runner_woa, trace
from .gateway import Backend, GatewayCompletionClient, ProviderGateway
from .oracle import ApprovalOracle
from .population import digest_json
from .projection import actor_projection
from .rpc import OdooActorClient
from .stack import REPOSITORY, Stack, StackError
from .tasks import EvaluatorInput, RunnerInput


#: Evidence of a fault that cut the run short or left out part of what the evaluator reads: the
#: provider's typed records, the browser or control channel lost while the product ran, and the
#: final page or fixture guard that could not be read. A judgement stands beside any other fault.
OBSERVATION_FAULT_EVIDENCE = frozenset({
    "gateway_response_record", "gateway_transport_record", "gateway_accounting_record",
    "browser_exit", "control_channel_error", "final_page_error", "guard_capture_error"})


@dataclass(frozen=True)
class DriverConfig:
    stack: Stack
    ledger_path: Path
    evidence_root: Path
    recovery_dir: Path
    actor_key_files: dict[str, Path]
    woa: runner_woa.WoaRunnerConfig
    pageagent: runner_pageagent.PageAgentRunnerConfig
    contract: PageAgentProviderContract
    fixture_digest: str
    capability_digests: dict[str, str]
    provider_config_digest: str
    evaluator_timeout_s: int = 300


@dataclass
class AttemptPlan:
    slot: dict[str, Any]
    runner_input: RunnerInput
    evaluator_input: EvaluatorInput
    policy: Callable[[dict[str, Any], int], dict[str, Any]] | None = None
    provider_faults: dict[int, str] | None = None
    provider_models: dict[int, str] | None = None
    #: Qualification-only apparatus fault injection: evaluator_exit, browser_crash,
    #: baseline_drift, identity_before_start.
    inject: frozenset[str] = field(default_factory=frozenset)
    replacement_of: str | None = None


class Driver:
    def __init__(self, config: DriverConfig, *, gateway: ProviderGateway,
                 backend: Backend, playwright: Any):
        self.config = config
        self.gateway = gateway
        self.backend = backend
        self.playwright = playwright
        self.guard = fixture.FixtureGuard(config.stack)
        record = json.loads((config.recovery_dir / "recovery.json").read_text())
        self.baseline_guard = record["baseline_guard"]

    # -- helpers -----------------------------------------------------------------------------
    def _append(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        return ledger.append(self.config.ledger_path, kind, body)

    def _write(self, directory: Path, name: str, value: Any) -> str:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1,
                          default=str).encode("utf-8")
        path = directory / name
        path.write_bytes(data)
        os.chmod(path, 0o600)
        return digest_json(value)

    def _projection(self) -> dict[str, Any]:
        return actor_projection(self.config.stack.rpc_url, self.config.actor_key_files)

    def _evaluator(self, command: str, directory: Path, *, inject_exit: bool = False
                   ) -> dict[str, Any] | None:
        argv = [sys.executable, "-m", "evals.odoo.evaluator", command,
                "--job", str(directory / "evaluator-job.json"),
                "--output", str(directory / f"evaluator-{command}.json")]
        if command == "judge":
            argv += ["--truth", str(directory / "evaluator-truth.json"),
                     "--observation", str(directory / "observation.json")]
        if inject_exit:
            argv = [sys.executable, "-c", "raise SystemExit(70)"]
        # PYTHONPATH carries only the checkout root. The virtual environment can still import the
        # product, so what keeps the evaluator apart is its own refusal to write a result once a
        # product, adapter or engine module was loaded.
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        env["PYTHONPATH"] = str(REPOSITORY)
        try:
            completed = subprocess.run(argv, cwd=REPOSITORY, env=env, capture_output=True,
                                       timeout=self.config.evaluator_timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            (directory / f"evaluator-{command}.stderr").write_bytes(completed.stderr[-4000:])
            return None
        return json.loads((directory / f"evaluator-{command}.json").read_text())

    def _gateway_records(self, attempt_id: str) -> tuple[list[dict], list[dict], list[dict]]:
        entries, _ = ledger.read(self.config.ledger_path)
        requests = [entry["body"] for entry in entries
                    if entry["kind"] == "model_request_dispatched"
                    and entry["body"]["attempt_id"] == attempt_id]
        responses = [entry["body"] for entry in entries
                     if entry["kind"] == "model_response_received"
                     and entry["body"]["attempt_id"] == attempt_id]
        refusals = [entry["body"] for entry in entries
                    if entry["kind"] == "model_request_refused"
                    and entry["body"]["attempt_id"] == attempt_id]
        return requests, responses, refusals

    # -- one attempt ---------------------------------------------------------------------------
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
        artifact: dict[str, Any] = {"turns": [], "final": None, "approvals": []}
        projected = trace.project_woa([]) if arm == "woa_product" else trace.project_pageagent(None)
        truth = judged = None
        guard_changes: list[str] = []
        agent_start: dict[str, Any] | None = None
        token = "p74" + secrets.token_urlsafe(32)
        evaluator_job = {"evaluator_input": asdict(plan.evaluator_input),
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
            truth = self._evaluator("truth", evidence)
            if truth is None:
                raise _PreStart("evaluator_exception", "evaluator_process_exit")
            if "identity_before_start" in plan.inject:
                raise _PreStart("credential_or_identity_before_start", "pre_start_check")

            def checkpoint(value: dict[str, Any]) -> None:
                if value.get("settled"):
                    # How many model requests the attempt had made when this turn ended: a turn
                    # whose count stays below the first faulty request ended before the fault.
                    requests, _, refusals = self._gateway_records(attempt_id)
                    settled[int(value["turn"])] = len(requests) + len(refusals)
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

                def approval_hook(card: dict[str, Any]) -> dict[str, Any]:
                    proposals = trace.project_woa(self.config.stack.export_audit())["proposals"]
                    pending = proposals[decided[0]:]
                    decided[0] = len(proposals)
                    if oracle is None:
                        return {"decision": "deny", "reasons": ["no_write_expected"],
                                "safety_events": []}
                    return oracle.decide(visible_effect=card["effect"],
                                         visible_targets=card["targets"],
                                         server_proposals=pending)

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
            product_started = bool(artifact.get("product_started"))
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
        if artifact.get("browser_exit"):
            events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                  "browser_exit"))
        elif artifact.get("control_channel_error"):
            events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                  "control_channel_error"))
        elif artifact.get("final_page_error"):
            # Every turn had ended when the final page could not be read: what the evaluator reads
            # from that page is missing, but nothing shows that the product's ending was lost.
            events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                  "final_page_error"))
        elif artifact.get("teardown_error"):
            events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                  "teardown_error"))
        elif product_started and not artifact.get("logged_out"):
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
        if arm == "woa_product" and product_started:
            audit_available = False
            try:
                audit = self.config.stack.export_audit()
                agent_end = self.config.stack.agent_state()
                audit_available = True
            except StackError:
                events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                      "control_channel_error"))
            else:
                self._write(evidence, "agent-end.json", agent_end)
            projected = trace.project_woa(audit)
            if not audit_available:
                # Nothing was read of what the product dispatched, which is not the same as
                # having read that it dispatched nothing.
                projected = {**projected, "dispatch_state": "unknown", "audit_readable": False}
        elif arm != "woa_product":
            projected = trace.project_pageagent(artifact.get("bridge"),
                                                host_errors=artifact.get("host_errors") or [],
                                                document_replaced=bool(
                                                    artifact.get("document_replaced")))
        recorded_turns = artifact.get("turns") or []
        # The turns the product was sent: those the runner recorded, and one it had sent when the
        # run broke off before recording it.
        sent = (max(len(recorded_turns),
                    len({int(value["turn"]) for value in checkpoints if "turn" in value}))
                if product_started else 0)
        if arm == "woa_product":
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
        terminal = scored_terminal(final_ending, facts, artifact.get("budget_exhausted"),
                                   final_turn_requests(settled, sent), stopped=stopped)
        gateway_ending = (f"{facts.terminal}@{facts.terminal_seq}" if facts.terminal is not None
                          else None)
        guard_unreadable = False
        if product_started:
            try:
                guard_after = self.guard.capture()
                guard_changes = fixture.differences(self.baseline_guard, guard_after)
            except StackError:
                guard_unreadable = True
                events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                      "guard_capture_error"))
            observation = {"arm": arm, "turns": artifact.get("turns") or [],
                           "expected_turns": len(plan.runner_input.messages),
                           "final": artifact.get("final"), "trace": projected,
                           "approvals": artifact.get("approvals") or [],
                           "guard_changes": guard_changes}
            self._write(evidence, "observation.json", observation)
            judged = self._evaluator("judge", evidence,
                                     inject_exit="evaluator_exit" in plan.inject)
            if judged is None:
                events.append(outcomes.ApparatusEvent("evaluator_exception",
                                                      "evaluator_process_exit"))
        artifact_digest = self._write(evidence, "runner-artifact.json", artifact)
        self._write(evidence, "trace.json", projected)
        if audit:
            self._write(evidence, "agent-audit.json", audit)
        # Faults that cut the run or what the evaluator reads of it. A fault after the observation
        # was complete -- the screenshot, logging out, closing the browser -- leaves the judgement
        # standing, whatever it decided.
        observation_faults = any(event.evidence in OBSERVATION_FAULT_EVIDENCE for event in events)
        evaluation = None
        if judged is not None:
            # A failure the evaluator read from a turn that ended before the first apparatus
            # fault was already decided; the fault cannot turn it into a missing observation.
            earlier = turns_before_fault(settled, facts.first_event_seq)
            decided_before_fault = (bool(judged["decisive"]) and judged["status"] == "FAIL"
                                    and any(turn < earlier
                                            for turn in judged.get("failed_turns") or []))
            evaluation = outcomes.Evaluation(
                status=judged["status"],
                decisive=(bool(judged["decisive"]) and not observation_faults
                          or decided_before_fault or bool(judged["safety_events"])),
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
            classification = outcomes.classify(
                product_started=product_started, dispatch_state=projected["dispatch_state"],
                runner_terminal="classification_input_missing", apparatus_events=events,
                evaluation=outcomes.Evaluation("FAIL", decisive=False),
                request_contract_violation=facts.request_contract_violation,
                gateway_terminal=gateway_ending, unclosed_turns=unclosed)
        self._append("attempt_outcome", {
            "attempt_id": attempt_id, **classification.as_record(),
            "evaluation_digest": digest_json(judged) if judged else None,
            "truth_digest": digest_json(truth) if truth else None})
        agent = (agent_assertions(agent_start, agent_end, audit, product_started=product_started)
                 if arm == "woa_product" else None)
        reset = self._reset(plan, guard_changes, agent,
                            forced=not_stopped or guard_unreadable)
        self._append("reset_outcome", {"attempt_id": attempt_id, **reset})
        self._append("attempt_cost", _cost(attempt_id, requests, responses))
        turns = artifact.get("turns") or []
        cancel = [turn["cancel_feedback_ms"] for turn in turns
                  if turn.get("cancel_feedback_ms") is not None]
        self._append("attempt_metrics", {
            "attempt_id": attempt_id,
            "automatic_wall_ms": sum(int(turn.get("automatic_ms") or 0) for turn in turns),
            "approval_wait_ms": sum(int(turn.get("approval_wait_ms") or 0) for turn in turns),
            "first_feedback_ms": (turns[0].get("first_feedback_ms") if turns else None),
            "cancel_feedback_ms": max(cancel) if cancel else None,
            "page_actions": projected["page_actions"],
            "invalid_page_actions": projected["invalid_page_actions"],
            "business_calls": projected["business_calls"],
            "repeated_observations": projected["repeated_observations"],
            "user_turns": len(plan.runner_input.messages)})
        self._append("attempt_terminal", {"attempt_id": attempt_id})
        _, summary = ledger.read(self.config.ledger_path)
        summary["last_attempt_evidence_digest"] = artifact_digest
        return summary

    def _reset(self, plan: AttemptPlan, guard_changes: list[str],
               agent: tuple[dict[str, Any], list[str]] | None, *,
               forced: bool = False) -> dict[str, Any]:
        """Put the fixture back where the task, the observed state or ``forced`` requires it.

        ``forced`` covers what the attempt could not observe: a turn that did not stop, and a
        guard the apparatus could not read. Neither may be recorded as needing no reset.
        """
        record, residue = agent if agent is not None else (None, [])
        required = forced or plan.evaluator_input.reset_profile != "none" or bool(guard_changes)
        if not required:
            return {"outcome": _with_residue("NOT_REQUIRED", residue), "basis": list(residue),
                    "guard_digest": None, "agent": record}
        result = fixture.restore(self.config.stack, self.guard, self.config.recovery_dir,
                                 actor_projection=self._projection)
        return {"outcome": _with_residue(result["status"], residue),
                "basis": [*residue, *(result.get("basis") or [])],
                "guard_digest": result.get("guard_digest"), "agent": record}

    def recover_open_attempt(self) -> dict[str, Any] | None:
        """Close an attempt a previous driver process started and never finished.

        The attempt is scored from what was persisted -- its checkpoints, the gateway's request
        records and, for WOA, the Agent's own audit -- then reset conservatively and closed as
        ``interrupted_attempt`` with the process exit as its independent evidence. It is never
        silently rerun: at most a policy replacement may follow it.
        """
        entries, status = ledger.read(self.config.ledger_path)
        attempt_id = status["open_attempt"]
        if attempt_id is None:
            return None
        records = [entry for entry in entries if entry["body"].get("attempt_id") == attempt_id]
        started = next(entry["body"] for entry in records if entry["kind"] == "attempt_started")
        present = {entry["kind"] for entry in records}
        product_started = any(entry["kind"] == "attempt_checkpoint"
                              and entry["body"]["product_started"] for entry in records)
        requests, responses, refusals = self._gateway_records(attempt_id)
        facts = outcomes.gateway_facts(responses, refusals)
        projected = trace.project_woa([])
        audit: list[dict[str, Any]] = []
        audit_available = False
        agent = None
        if started["arm"] == "woa_product":
            start_path = self.config.evidence_root / attempt_id / "agent-start.json"
            agent_start = json.loads(start_path.read_text()) if start_path.exists() else None
            agent_end = None
            if product_started:
                try:
                    # The dead driver never logged out. Ending its session first means nothing
                    # can still act for it while its audit, Agent state and fixture are read.
                    agent_end = self.config.stack.end_orphaned_sessions()
                    audit = self.config.stack.export_audit()
                    audit_available = True
                    projected = trace.project_woa(audit)
                except StackError:
                    pass
            agent = agent_assertions(agent_start, agent_end, audit,
                                     product_started=product_started)
        dispatch = (projected["dispatch_state"] if audit_available or not product_started
                    else "unknown")
        classification = outcomes.classify(
            product_started=product_started, dispatch_state=dispatch, runner_terminal=None,
            apparatus_events=[outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                      "runner_process_exit"), *facts.events],
            request_contract_violation=facts.request_contract_violation)
        if "attempt_outcome" not in present:
            self._append("attempt_outcome", {
                "attempt_id": attempt_id, **classification.as_record(),
                "evaluation_digest": None, "truth_digest": None})
        if "reset_outcome" not in present:
            record, residue = agent if agent is not None else (None, [])
            changes = fixture.differences(self.baseline_guard, self.guard.capture())
            if changes:
                result = fixture.restore(self.config.stack, self.guard,
                                         self.config.recovery_dir,
                                         actor_projection=self._projection)
                outcome = result["status"]
                basis = ["interrupted_attempt_changed_fixture", *result.get("basis", [])]
                guard_digest = result.get("guard_digest")
            else:
                outcome, basis, guard_digest = "NOT_REQUIRED", [], None
            self._append("reset_outcome", {
                "attempt_id": attempt_id, "outcome": _with_residue(outcome, residue),
                "basis": [*residue, *basis], "guard_digest": guard_digest, "agent": record})
        if "attempt_cost" not in present:
            self._append("attempt_cost", _cost(attempt_id, requests, responses))
        if "attempt_metrics" not in present:
            # The dead driver never took the attempt's times: they are unknown, never zero, so no
            # latency or confirmation-wait sample counts them.
            self._append("attempt_metrics", {
                "attempt_id": attempt_id, "automatic_wall_ms": None, "approval_wait_ms": None,
                "first_feedback_ms": None, "cancel_feedback_ms": None,
                "page_actions": projected["page_actions"],
                "invalid_page_actions": projected["invalid_page_actions"],
                "business_calls": projected["business_calls"],
                "repeated_observations": projected["repeated_observations"],
                "user_turns": 0})
        entries, status = ledger.read(self.config.ledger_path)
        # The ledger recomputes eligibility from the outcome it holds, so this reads that same
        # record: a classification recomputed here can differ -- the audit may show more, or no
        # longer be readable -- and the two answers must not disagree.
        recorded = next(entry["body"] for entry in reversed(entries)
                        if entry["kind"] == "attempt_outcome"
                        and entry["body"]["attempt_id"] == attempt_id)
        closing = next(item for item in status["attempts"] if item["attempt_id"] == attempt_id)
        eligible = outcomes.replacement_eligible(recorded, closing["reset_outcome"])
        return ledger.append(self.config.ledger_path, "interrupted_attempt", {
            "attempt_id": attempt_id, "evidence_kinds": ["runner_process_exit"],
            "replacement_eligible": eligible})

    def run_slot(self, plan: AttemptPlan, *, replacement: AttemptPlan | None = None
                 ) -> dict[str, Any]:
        """One scheduled slot: the original attempt, then at most one policy replacement."""
        _, status = ledger.read(self.config.ledger_path)
        if status["status"] != "OPEN":
            return status
        if status["pending_seal"]:
            # Something outside any attempt -- an unowned model request -- already requires
            # the batch to stop; no further attempt may start.
            return self._seal(status["pending_seal"])
        summary = self.run_attempt(plan)
        last = summary["attempts"][-1]
        if summary["pending_seal"]:
            return self._seal(summary["pending_seal"])
        if last["replacement_eligible"]:
            follow = replacement or AttemptPlan(
                slot=plan.slot, runner_input=plan.runner_input,
                evaluator_input=plan.evaluator_input, policy=plan.policy)
            follow.replacement_of = last["attempt_id"]
            summary = self.run_attempt(follow)
            if summary["pending_seal"]:
                return self._seal(summary["pending_seal"])
        return summary

    def _seal(self, reason: str) -> dict[str, Any]:
        """Seal the batch once every call it made has been recorded: a sealed ledger takes no
        answer, so a call still being answered would have nowhere left to be written."""
        self.gateway.drain()
        _, status = ledger.read(self.config.ledger_path)
        return ledger.append(self.config.ledger_path, "batch_sealed",
                             {"reason": status["pending_seal"] or reason})


class _PreStart(Exception):
    def __init__(self, code: str, evidence: str):
        super().__init__(code)
        self.code = code
        self.evidence = evidence


def agent_assertions(start: dict[str, Any] | None, end: dict[str, Any] | None,
                     audit: list[dict[str, Any]], *, product_started: bool
                     ) -> tuple[dict[str, Any], list[str]]:
    """The content-free Agent-state assertions of one WOA attempt and the residue they show.

    ``start`` is the verified-empty state the attempt began from and ``end`` the state once
    the attempt logged out. Sessions and unresolved mutations are residue outright. A prepared
    journal reservation is residue unless the audit closed its proposal without dispatch: the
    product keeps a declined proposal prepared until it expires. A started attempt whose end
    state could not be read is recorded as unasserted, never as clean.
    """
    closed = trace.closed_proposals(audit)
    prepared = set(end["prepared_proposals"]) if end is not None else set()
    record = {"start": _agent_counts(start), "end": _agent_counts(end),
              "open_proposals": len(prepared - closed),
              "closed_proposals": len(prepared & closed)}
    residue = []
    if start is not None and any(record["start"].values()):
        residue.append("agent_residue:inherited_state")
    if end is None:
        if product_started:
            residue.append("agent_state_unreadable")
        return record, residue
    residue += [f"agent_residue:{key}" for key in ("active_sessions", "unresolved_mutations")
                if end[key]]
    if record["open_proposals"]:
        residue.append("agent_residue:open_proposals")
    return record, residue


def _agent_counts(state: dict[str, Any] | None) -> dict[str, int] | None:
    if state is None:
        return None
    return {"active_sessions": int(state["active_sessions"]),
            "unresolved_mutations": int(state["unresolved_mutations"]),
            "prepared_proposals": len(state["prepared_proposals"]),
            "conversation_rows": int(state["conversation_rows"])}


def turns_before_fault(settled: dict[int, int], first_fault_seq: int | None) -> int:
    """How many leading turns ended before the attempt's first faulty gateway record.

    ``settled`` maps each turn that ended to the number of model requests made by then. Without a
    gateway fault every ended turn counts: a browser or control-channel fault never cuts a turn
    that had already ended, because an unfinished turn is not reported as ended.
    """
    count = 0
    while count in settled and (first_fault_seq is None or settled[count] < first_fault_seq):
        count += 1
    return count


def turn_bounds(settled: dict[int, int], index: int) -> tuple[int | None, int | None]:
    """The request numbers turn ``index`` made, as ``(after, through)``.

    ``settled`` maps each turn that ended to the number of requests made by then. A turn made the
    requests numbered above what the turn before it had made -- none before the first turn --
    through what it had made when it ended. A bound whose count was not recorded is ``None``.
    """
    return (0 if index == 0 else settled.get(index - 1)), settled.get(index)


def final_turn_requests(settled: dict[int, int], turns: int) -> tuple[int, int] | None:
    """The request numbers the last of ``turns`` turns made, as ``(after, through)``.

    Without the count at its own end, or at the end of the turn before it, the span cannot be
    proven and is ``None``: every request of the attempt is not a turn's span.
    """
    if turns <= 0:
        return None
    after, through = turn_bounds(settled, turns - 1)
    return None if after is None or through is None else (after, through)


def scored_terminal(runner_terminal: str | None, facts: outcomes.GatewayFacts,
                    budget_exhausted: str | None = None,
                    final_turn: tuple[int, int] | None = None, *,
                    stopped: bool = False) -> str | None:
    """The ending an attempt is scored under.

    An exhausted call budget ends an attempt whose product reported a typed ending, and so does a
    wall clock the runner enforced. Where the runner stopped the last turn at that clock
    (``stopped``), the stopped product's generic provider failure or missing ending is the clock's
    too; a turn that ended by itself keeps its own. A scorable ending the gateway recorded -- an
    exhausted call budget, an input refused as too long, a context-length refusal or a provider
    deadline -- replaces the generic provider failure only where its record is provably the
    request that ended the last turn: numbered within ``final_turn`` and the last request of it.
    Anywhere else, and for an ending nobody mapped, the runner's ending is kept, and the
    classification sees it as drift.
    """
    generic = runner_terminal in outcomes.GENERIC_PROVIDER_TERMINALS
    if facts.terminal == "model_call_budget" and runner_terminal in outcomes.PRODUCT_TERMINALS:
        return facts.terminal
    if budget_exhausted is not None and (
            runner_terminal in outcomes.PRODUCT_TERMINALS
            or stopped and (generic or runner_terminal is None)):
        return "wall_clock_budget"
    if generic and final_turn is not None:
        for seq, terminal in facts.terminals:
            if final_turn[0] < seq == final_turn[1]:
                return terminal
    return runner_terminal


def _with_residue(outcome: str, residue: list[str]) -> str:
    """A reset that would otherwise pass cannot while Agent residue, or no assertion, remains."""
    if outcome not in {"PASS", "NOT_REQUIRED"} or not residue:
        return outcome
    return "INDETERMINATE" if "agent_state_unreadable" in residue else "FAIL"


def _cost(attempt_id: str, requests: list[dict], responses: list[dict]) -> dict[str, Any]:
    by_seq = {item["request_seq"]: item for item in responses}
    unavailable = sum(1 for item in requests
                      if by_seq.get(item["request_seq"]) is None
                      or by_seq[item["request_seq"]]["usage"] is None
                      or by_seq[item["request_seq"]]["cost_cny"] is None)
    metered = [item for item in responses if item["usage"] is not None]
    known = round(sum(item["cost_cny"] for item in responses
                      if item["cost_cny"] is not None), 6)
    return {
        "attempt_id": attempt_id, "model_calls": len(requests),
        "usage_unavailable_calls": unavailable,
        "input_tokens": sum(item["usage"]["input_tokens"] for item in metered),
        "output_tokens": sum(item["usage"]["output_tokens"] for item in metered),
        "cached_tokens": sum(item["usage"]["cached_tokens"] for item in metered),
        "reasoning_tokens": sum(item["usage"]["reasoning_tokens"] for item in metered),
        "known_cost_cny": known, "total_cost_cny": None if unavailable else known,
    }
