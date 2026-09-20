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


#: Evidence of a fault that cut the run short: the provider's typed records, and the browser or
#: control channel lost while the product ran. After one of these, only a failure the evaluator
#: read from a turn that had already ended is the product's own. A fault that lost one observed
#: component instead -- the final page, the fixture guard, the audit -- is not here: it takes
#: away the failures that rest on that component and leaves the rest of the judgement standing.
RUN_CUT_EVIDENCE = frozenset({
    "gateway_response_record", "gateway_transport_record", "gateway_accounting_record",
    "browser_exit", "control_channel_error"})


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
    #: baseline_drift, identity_before_start, kill_after_product_start, kill_after_turn_settled.
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
                   ) -> capture.Capture:
        argv = [sys.executable, "-m", "evals.odoo.judging.evaluator", command,
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
            scope = {"command": command, "directory": directory.name}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return capture.Unavailable(type(exc).__name__, "evaluator_output",
                                       {"command": command, "directory": directory.name})
        if completed.returncode != 0:
            (directory / f"evaluator-{command}.stderr").write_bytes(completed.stderr[-4000:])
            return capture.Unavailable(f"exit_{completed.returncode}", "evaluator_output", scope)
        return capture.read("evaluator_output", scope,
                            lambda: json.loads(
                                (directory / f"evaluator-{command}.json").read_text()))

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
        from evals.odoo.execution.attempt_execution import run_attempt

        return run_attempt(self, plan)

    def _reset(self, plan: AttemptPlan, guard_changes: list[str],
               agent: tuple[dict[str, Any], list[str]] | None, *,
               forced: bool = False) -> dict[str, Any]:
        """Put the fixture back where the task, the observed state or ``forced`` requires it.

        ``forced`` covers what the attempt could not observe: a turn that did not stop, a guard
        the apparatus could not read, and an audit it could not export. None of them may be
        recorded as needing no reset -- the guard watches a fixed set of tables, so an attempt
        whose dispatch nobody read is put back by a verified restore rather than by that
        whitelist alone.
        """
        record, residue = agent if agent is not None else (None, [])
        required = forced or plan.evaluator_input.reset_profile != "none" or bool(guard_changes)
        if not required:
            return {"outcome": _with_residue("NOT_REQUIRED", residue), "basis": list(residue),
                    "guard_digest": None, "agent": record}
        result = fixture.restore(self.config.stack, self.guard, self.config.recovery_dir,
                                 actor_projection=self._projection)
        return {"outcome": _with_residue(result["status"], residue),
                "basis": [*residue, *result["basis"]],
                "guard_digest": result.get("guard_digest"), "agent": record}

    def _persisted_turns(self, attempt_id: str, records: list[dict[str, Any]]
                         ) -> tuple[list[dict[str, Any]], dict[int, int], int | None]:
        """The turns a dead driver wrote down as its runner read them, in order.

        Each is checked against the digest its checkpoint bound in the ledger; the first one
        missing or changed ends the list, since a later turn read without it would be judged out
        of place. Also returns, per turn that ended, how many model requests had been made by
        then, and how many turns the task has.
        """
        bound: dict[str, str] = {}
        for entry in records:
            if entry["kind"] == "attempt_checkpoint":
                bound.update(entry["body"]["evidence"])
        directory = self.config.evidence_root / attempt_id
        turns: list[dict[str, Any]] = []
        settled: dict[int, int] = {}
        expected = None
        while (name := f"turn-{len(turns)}") in bound:
            try:
                value = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                break
            if digest_json(value) != bound[name]:
                break
            if value["settled"] and value["requests_at_settle"] is not None:
                settled[len(turns)] = int(value["requests_at_settle"])
            turns.append(value["turn"])
            expected = value["expected_turns"]
        return turns, settled, expected

    def _judge_persisted(self, started: dict[str, Any], records: list[dict[str, Any]], *,
                         projected: dict[str, Any], audit_read: capture.Capture,
                         changes_read: capture.Capture, facts: outcomes.GatewayFacts
                         ) -> tuple[outcomes.Evaluation | None, dict[str, Any] | None,
                                    dict[str, Any] | None]:
        """Judge what a dead driver's attempt left: the turns written down as they were read,
        the product's audit and the fixture as it stands.

        The page the run ended on and the approval decisions were never written, so a failure
        resting on either does not count. The run was cut where the process died: a failure on a
        turn that ended before the first fault stands, as it does after any cut, and a pass does
        not. Without a turn or the truth read before the product started there is nothing to
        judge.
        """
        attempt_id, arm = started["attempt_id"], started["arm"]
        turns, settled, expected = self._persisted_turns(attempt_id, records)
        directory = self.config.evidence_root / attempt_id
        try:
            truth = json.loads((directory / "evaluator-truth.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, None, None
        if not turns:
            return None, None, truth
        # The scope the evaluator job was written with: the judgement refuses any other.
        scope = {"attempt_id": attempt_id, "run_id": started["run_id"], "arm": arm}
        trace_read = (capture.Present(projected, "agent_audit", scope, complete=False)
                      if audit_read.available
                      else capture.Unavailable("runner_process_exit", "agent_audit", scope))
        guard_read = (capture.Present(changes_read.value, "fixture_guard", scope)
                      if changes_read.available
                      else capture.Unavailable("guard_capture_error", "fixture_guard", scope))
        self._write(directory, "observation.json", {
            "arm": arm, "expected_turns": expected,
            "turns": capture.as_record(capture.Present(turns, "runner_artifact", scope,
                                                       complete=len(turns) == expected)),
            "final": capture.as_record(capture.Unavailable("runner_process_exit", "final_page",
                                                           scope)),
            "trace": capture.as_record(trace_read),
            "approvals": capture.as_record(capture.Unavailable("runner_process_exit",
                                                               "runner_artifact", scope)),
            "guard_changes": capture.as_record(guard_read)})
        judgement = self._evaluator("judge", directory)
        if not judgement.available:
            return None, None, truth
        judged = judgement.value
        lost = {"final", "approvals"} | {name for name, read in (("trace", trace_read),
                                                                  ("guard_changes", guard_read))
                                          if not read.available}
        earlier = turns_before_fault(settled, facts.first_event_seq)
        before_cut = [failure for failure in judged["failures"]
                      if not set(failure["on"]) & lost
                      and failure["turn"] is not None and failure["turn"] < earlier]
        decisive = (judged["status"] == "FAIL" and bool(judged["decisive"])
                    and bool(judged["safety_events"] or before_cut))
        return (outcomes.Evaluation(
            status=judged["status"], decisive=decisive, reasons=tuple(judged["reasons"]),
            safety_events=tuple(judged["safety_events"]),
            extraction_ambiguous=bool(judged["extraction_ambiguous"])), judged, truth)

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
        scope = {"attempt_id": attempt_id, "arm": started["arm"], "recovered": True}
        audit_read: capture.Capture = capture.Unavailable("product_not_started", "agent_audit",
                                                          scope)
        agent = None
        if started["arm"] == "woa_product":
            start_path = self.config.evidence_root / attempt_id / "agent-start.json"
            agent_start = json.loads(start_path.read_text()) if start_path.exists() else None
            agent_end = None
            if product_started:
                # The dead driver never logged out. Ending its session first means nothing
                # can still act for it while its audit, Agent state and fixture are read.
                state_read = capture.read("agent_state", scope,
                                          self.config.stack.end_orphaned_sessions,
                                          errors=(StackError,))
                agent_end = state_read.value if state_read.available else None
                audit_read = capture.read("agent_audit", scope,
                                          self.config.stack.export_audit, errors=(StackError,))
                if audit_read.available:
                    audit = audit_read.value
                    projected = trace.project_woa(audit)
                else:
                    projected = {**projected, **trace.UNREAD_TRACE}
            agent = agent_assertions(agent_start, agent_end, audit,
                                     product_started=product_started,
                                     audit_observed=audit_read.available)
        dispatch = projected["dispatch_state"]
        # What the fixture shows now, before any restore: it is both what the judgement below
        # reads and what decides the reset's basis.
        changes_read = capture.read(
            "fixture_guard", scope,
            lambda: fixture.differences(self.baseline_guard, self.guard.capture()),
            errors=(StackError,))
        evaluation, judged, truth = None, None, None
        if "attempt_outcome" not in present and product_started:
            evaluation, judged, truth = self._judge_persisted(
                started, records, projected=projected, audit_read=audit_read,
                changes_read=changes_read, facts=facts)
        classification = outcomes.classify(
            product_started=product_started, dispatch_state=dispatch, runner_terminal=None,
            apparatus_events=[outcomes.ApparatusEvent("browser_or_control_channel_crash",
                                                      "runner_process_exit"), *facts.events],
            evaluation=evaluation,
            request_contract_violation=facts.request_contract_violation)
        if "attempt_outcome" not in present:
            self._append("attempt_outcome", {
                "attempt_id": attempt_id, **classification.as_record(),
                "evaluation_digest": digest_json(judged) if judged else None,
                "truth_digest": digest_json(truth) if truth else None})
        if "reset_outcome" not in present:
            record, residue = agent if agent is not None else (None, [])
            # An attempt whose driver died is put back by a verified restore, whatever the guard
            # shows. The guard watches a fixed set of tables, the audit may no longer be
            # readable, and the task's own reset profile is not known here: none of that adds up
            # to an attempt that needed no reset.
            changes = (changes_read.value if changes_read.available
                       else fixture.differences(self.baseline_guard, self.guard.capture()))
            result = fixture.restore(self.config.stack, self.guard, self.config.recovery_dir,
                                     actor_projection=self._projection)
            basis = [("interrupted_attempt_changed_fixture" if changes
                      else "interrupted_attempt_state_unobserved"), *result["basis"]]
            self._append("reset_outcome", {
                "attempt_id": attempt_id,
                "outcome": _with_residue(result["status"], residue),
                "basis": [*residue, *basis], "guard_digest": result.get("guard_digest"),
                "agent": record})
        if "attempt_cost" not in present:
            self._append("attempt_cost", _cost(attempt_id, requests, responses))
        if "attempt_metrics" not in present:
            # The dead driver never took the attempt's times: they are unknown, never zero, so no
            # latency or confirmation-wait sample counts them.
            self._append("attempt_metrics", {
                "attempt_id": attempt_id, "automatic_wall_ms": None, "approval_wait_ms": None,
                "first_feedback_ms": None, "cancel_feedback_ms": None,
                # Whether the dead driver's audit holds every turn cannot be shown: the
                # checkpoints it left do not say how many turns it sent, so there is no count
                # to compare the export against. These are unknown here, exactly as the times
                # this same record leaves unknown.
                **_trace_counts(projected, whole=False),
                "user_turns": 0})
        # The dead driver's calls settle here: a lease it took out and never closed is written
        # down as unresolved, so the attempt ends with every call it made accounted for, and the
        # bill reconciliation names the ones nobody knows the cost of.
        settled = {body["request_seq"] for body in responses}
        settled |= {entry["body"]["request_seq"] for entry in
                    ledger.read(self.config.ledger_path)[0]
                    if entry["kind"] == "model_response_unresolved"
                    and entry["body"]["attempt_id"] == attempt_id}
        for request in requests:
            if request["request_seq"] not in settled:
                self._append("model_response_unresolved", {
                    "attempt_id": attempt_id, "request_seq": request["request_seq"],
                    "reason": "driver_process_exit"})
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
        if status["disposition"] == "must_seal":
            # Something outside any attempt -- an unowned model request -- already requires
            # the batch to stop; no further attempt may start.
            return self._seal(status["pending_seal"])
        if status["disposition"] != "running":
            return status
        summary = self.run_attempt(plan)
        last = summary["attempts"][-1]
        if summary["disposition"] == "must_seal":
            return self._seal(summary["pending_seal"])
        # The attempt just changed what the batch may do, so the disposition is read again
        # rather than branching on `pending_seal` alone: an attempt that leaves a call nobody
        # settled holds the batch without owing a seal, and a replacement started then would go
        # into a ledger that refuses it. The entry that resumes the batch runs the replacement
        # its slot is owed once the bill accounts for that call.
        if summary["disposition"] == "running" and last["replacement_eligible"]:
            follow = replacement or AttemptPlan(
                slot=plan.slot, runner_input=plan.runner_input,
                evaluator_input=plan.evaluator_input, policy=plan.policy)
            follow.replacement_of = last["attempt_id"]
            summary = self.run_attempt(follow)
            if summary["disposition"] == "must_seal":
                return self._seal(summary["pending_seal"])
        return summary

    def _seal(self, reason: str) -> dict[str, Any]:
        """Seal the batch. Every call it made settled when its attempt closed on the gateway, so
        a sealed ledger leaves no answer without somewhere to have been written."""
        _, status = ledger.read(self.config.ledger_path)
        return ledger.append(self.config.ledger_path, "batch_sealed",
                             {"reason": status["pending_seal"] or reason})


class _PreStart(Exception):
    def __init__(self, code: str, evidence: str):
        super().__init__(code)
        self.code = code
        self.evidence = evidence
