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

from . import capture, fixture, ledger, outcomes, runner_pageagent, runner_woa, trace
from .gateway import Backend, GatewayCompletionClient, ProviderGateway
from .oracle import ApprovalOracle
from .population import digest_json
from .projection import actor_projection
from .rpc import OdooActorClient
from .stack import REPOSITORY, Stack, StackError
from .tasks import EvaluatorInput, RunnerInput


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


def agent_assertions(start: dict[str, Any] | None, end: dict[str, Any] | None,
                     audit: list[dict[str, Any]], *, product_started: bool,
                     audit_observed: bool = True) -> tuple[dict[str, Any], list[str]]:
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
        # An audit nobody could export is not an empty one: read as one, every proposal the
        # product prepared would count as still open, and a reset that can only be undecided
        # would read as a failure. A proposal nobody read the closure of is unasserted, which is
        # what the reset says.
        residue.append("agent_residue:open_proposals" if audit_observed
                       else "agent_audit_unreadable")
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


def _final_page(artifact: dict[str, Any], scope: dict[str, Any]) -> capture.Capture:
    """The page the run left behind, or why it was never read.

    A page the runner could not read is not a page showing nothing: the evaluator must not put a
    failure on a turn whose page nobody saw.
    """
    final = artifact["final"]
    if isinstance(final, dict):
        return capture.Present(final, "final_page", scope)
    for field in ("final_page_error", "control_channel_error"):
        fault = artifact[field]
        if fault:
            return capture.Unavailable(str(fault["type"]), "final_page", scope)
    if artifact["browser_exit"]:
        return capture.Unavailable(str(artifact["browser_exit"]), "final_page", scope)
    return capture.Unavailable("not_captured", "final_page", scope)


def _trace_counts(projected: dict[str, Any], *, whole: bool) -> dict[str, int | None]:
    """The counts the projected trace supports. A trace nobody could read supports none of
    them, and neither does one read in part: a truncated export would count what it happened to
    show as what the attempt did. They are recorded as unknown, never as an attempt that did no
    work.
    """
    return {name: (projected[name] if whole else None) for name in
            ("page_actions", "invalid_page_actions", "business_calls", "repeated_observations")}


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


#: Residue markers that say the apparatus could not read what it would have asserted. A reset
#: held up by one of these alone is undecided; residue the apparatus did observe is a failure.
UNASSERTED_RESIDUE = frozenset({"agent_state_unreadable", "agent_audit_unreadable"})


def _with_residue(outcome: str, residue: list[str]) -> str:
    """A reset that would otherwise pass cannot while Agent residue, or no assertion, remains."""
    if outcome not in {"PASS", "NOT_REQUIRED"} or not residue:
        return outcome
    return "FAIL" if set(residue) - UNASSERTED_RESIDUE else "INDETERMINATE"


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
