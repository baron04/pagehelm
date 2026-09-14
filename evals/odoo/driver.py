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
        # PYTHONPATH carries only the checkout root: an evaluator that tried to import the
        # product would fail to start instead of silently sharing its code.
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
            checkpoints = []

            def checkpoint(value: dict[str, Any]) -> None:
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
        elif artifact.get("control_channel_error") or (product_started
                                                       and not artifact.get("logged_out")):
            events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                  "control_channel_error"))
        requests, responses, refusals = self._gateway_records(attempt_id)
        for response in responses:
            if response["status"] == "http_error" and (response["http_status"] or 0) >= 500:
                events.append(outcomes.ApparatusEvent("provider_http_5xx",
                                                      "gateway_response_record"))
            elif (response["status"] == "transport_error"
                  and response["error_code"] in {"provider_connection_lost",
                                                 "backend_exception"}):
                # A provider deadline is a scorable product result, not an apparatus fault.
                events.append(outcomes.ApparatusEvent("provider_connection_lost",
                                                      "gateway_transport_record"))
        audit: list[dict[str, Any]] = []
        agent_end: dict[str, Any] | None = None
        if arm == "woa_product" and product_started:
            try:
                audit = self.config.stack.export_audit()
                agent_end = self.config.stack.agent_state()
            except StackError:
                events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                      "control_channel_error"))
            else:
                self._write(evidence, "agent-end.json", agent_end)
            projected = trace.project_woa(audit)
        elif arm != "woa_product":
            projected = trace.project_pageagent(artifact.get("bridge"),
                                                host_errors=artifact.get("host_errors") or [],
                                                document_replaced=bool(
                                                    artifact.get("document_replaced")))
        terminal = projected["terminal"]
        if any(item["reason"] == "model_call_budget" for item in refusals):
            terminal = "model_call_budget"
        if product_started:
            try:
                guard_after = self.guard.capture()
                guard_changes = fixture.differences(self.baseline_guard, guard_after)
            except StackError:
                events.append(outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                      "control_channel_error"))
            observation = {"arm": arm, "turns": artifact.get("turns") or [],
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
        complete = bool(artifact.get("turns")) and not any(
            turn.get("timed_out") for turn in artifact.get("turns") or [])
        run_faults = any(event.evidence in {"gateway_response_record",
                                            "gateway_transport_record", "browser_exit",
                                            "control_channel_error"} for event in events)
        evaluation = None
        if judged is not None:
            evaluation = outcomes.Evaluation(
                status=judged["status"],
                decisive=bool(judged["decisive"]) and complete and not run_faults
                or bool(judged["safety_events"]),
                reasons=tuple(judged["reasons"]), safety_events=tuple(judged["safety_events"]),
                extraction_ambiguous=bool(judged["extraction_ambiguous"]))
        try:
            classification = outcomes.classify(
                product_started=product_started, dispatch_state=projected["dispatch_state"],
                runner_terminal=terminal if product_started else None,
                apparatus_events=events, evaluation=evaluation)
        except outcomes.OutcomeContractError:
            classification = outcomes.classify(
                product_started=product_started, dispatch_state=projected["dispatch_state"],
                runner_terminal="classification_input_missing", apparatus_events=events,
                evaluation=outcomes.Evaluation("FAIL", decisive=False))
        self._append("attempt_outcome", {
            "attempt_id": attempt_id, **classification.as_record(),
            "evaluation_digest": digest_json(judged) if judged else None,
            "truth_digest": digest_json(truth) if truth else None})
        agent = (agent_assertions(agent_start, agent_end, audit, product_started=product_started)
                 if arm == "woa_product" else None)
        reset = self._reset(plan, guard_changes, agent)
        self._append("reset_outcome", {"attempt_id": attempt_id, **reset})
        self._append("attempt_cost", _cost(attempt_id, requests, responses))
        turns = artifact.get("turns") or []
        self._append("attempt_metrics", {
            "attempt_id": attempt_id,
            "automatic_wall_ms": sum(int(turn.get("automatic_ms") or 0) for turn in turns),
            "approval_wait_ms": sum(int(turn.get("approval_wait_ms") or 0) for turn in turns),
            "first_feedback_ms": (turns[0].get("first_feedback_ms") if turns else None),
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
               agent: tuple[dict[str, Any], list[str]] | None) -> dict[str, Any]:
        record, residue = agent if agent is not None else (None, [])
        required = plan.evaluator_input.reset_profile != "none" or bool(guard_changes)
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
        requests, responses, _ = self._gateway_records(attempt_id)
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
                                                      "runner_process_exit")])
        if "attempt_outcome" not in present:
            self._append("attempt_outcome", {
                "attempt_id": attempt_id, **classification.as_record(),
                "evaluation_digest": None, "truth_digest": None})
        _, status = ledger.read(self.config.ledger_path)
        outcome_record = next(item for item in status["attempts"]
                              if item["attempt_id"] == attempt_id)
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
            self._append("attempt_metrics", {
                "attempt_id": attempt_id, "automatic_wall_ms": 0, "approval_wait_ms": 0,
                "first_feedback_ms": None, "page_actions": projected["page_actions"],
                "invalid_page_actions": projected["invalid_page_actions"],
                "business_calls": projected["business_calls"],
                "repeated_observations": projected["repeated_observations"],
                "user_turns": 0})
        _, status = ledger.read(self.config.ledger_path)
        closing = next(item for item in status["attempts"] if item["attempt_id"] == attempt_id)
        eligible = outcomes.replacement_eligible(
            {**classification.as_record(), **{
                key: closing[key] for key in ("product_outcome", "apparatus_integrity",
                                              "apparatus_code", "unclassified")}},
            closing["reset_outcome"])
        del outcome_record
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
            return ledger.append(self.config.ledger_path, "batch_sealed",
                                 {"reason": status["pending_seal"]})
        summary = self.run_attempt(plan)
        last = summary["attempts"][-1]
        if summary["pending_seal"]:
            return ledger.append(self.config.ledger_path, "batch_sealed",
                                 {"reason": summary["pending_seal"]})
        if last["replacement_eligible"]:
            follow = replacement or AttemptPlan(
                slot=plan.slot, runner_input=plan.runner_input,
                evaluator_input=plan.evaluator_input, policy=plan.policy)
            follow.replacement_of = last["attempt_id"]
            summary = self.run_attempt(follow)
            if summary["pending_seal"]:
                return ledger.append(self.config.ledger_path, "batch_sealed",
                                     {"reason": summary["pending_seal"]})
        return summary


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
