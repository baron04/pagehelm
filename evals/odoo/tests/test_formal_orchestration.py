"""The real formal development entry, run end to end without a model or a live stack.

``formal.run_development`` prepares, announces, drives and closes batches here exactly as it does
for a paid run: the real driver, the real gateway and its typed classification, the real
``UpstreamBackend`` and spend ledger against a loopback provider, the attempt ledger, the chain
rules, the lifecycle commands and the report. Only what would need Docker, Odoo and Chromium
stands behind fakes at the driver's own boundary: the stack, the fixture guard and reset, the
browser runner and the evaluator process. ``qualify.py`` runs those live.
"""
import contextlib
import copy
import functools
import hashlib
import importlib.util
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest import mock, skipIf

from evals.odoo import (canary, capture, driver, evaluator, fixture, formal, gateway, harness,
                        ledger, protocol, runner_woa)
from evals.odoo.gateway import UPSTREAM_SETTLE_MARGIN_S
from evals.odoo.runner_woa import ActorCredential
from evals.odoo.stack import StackError
from evals.odoo.tasks import EvaluatorInput, RunnerInput
from evals.odoo.tests.test_formal import FormalFixture
from evals.odoo.tests.test_formal_rehearsal import untouchable
from evals.odoo.tests.test_freeze import git
from evals.odoo.tests.test_ledger import DIGEST

TASK_SET = json.loads(harness.DEVELOPMENT_TASK_SET.read_text())
DEVELOPMENT_SEAL = json.loads(harness.DEVELOPMENT_SEAL.read_text())
HELDOUT_SEAL = json.loads(harness.HELDOUT_SEAL.read_text())
MODEL = "qwen3.8-flash-0915"
BASELINE_GUARD = {"fixture_id": fixture.FIXTURE_ID,
                  "tables": {"res_partner": {"rows": 40, "digest": "partners"}},
                  "sequences": {"ir_sequence_001": "7"},
                  "filestore": {"referenced_objects": 0, "missing_objects": [], "digest": DIGEST},
                  "excluded_volumes": {}, "digest": "sha256:" + "9" * 64}
EMPTY_AGENT = {"active_sessions": 0, "unresolved_mutations": 0, "prepared_proposals": [],
               "conversation_rows": 0}


class DriverDied(BaseException):
    """Stands for a driver process killed mid-attempt: nothing after it runs."""


def post(request) -> bool:
    """Send one product call through the gateway; whether it was answered."""
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


class Provider:
    """An OpenAI-compatible provider on loopback: the canary's answer, or a scripted product
    call with the fault scheduled for its number."""

    def __init__(self, test):
        self.model = MODEL
        self.faults: dict[int, str] = {}
        #: Product call number -> (set when the call arrives, set to let it be answered).
        self.held: dict[int, tuple[threading.Event, threading.Event]] = {}
        self.product_calls = 0
        self.lock = threading.Lock()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):  # noqa: N802 - http.server naming
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                status, payload = provider.answer(body)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def answer(self, body: dict[str, Any]) -> tuple[int, Any]:
        is_canary = body["messages"][0]["content"] == canary.CANARY_PROMPT
        with self.lock:
            fault = hold = None
            if not is_canary:
                self.product_calls += 1
                fault = self.faults.get(self.product_calls)
                hold = self.held.get(self.product_calls)
            model = self.model
        if hold is not None:
            arrived, release = hold
            arrived.set()
            release.wait(10)
        completion = {"id": "loopback", "object": "chat.completion", "model": model,
                      "choices": [{"index": 0, "finish_reason": "stop", "message": {
                          "role": "assistant",
                          "content": str(canary.CANARY_ANSWER) if is_canary else "done"}}],
                      "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
        if fault == "rate_limited":
            return 429, {"error": {"message": "slow down"}}
        if fault == "unauthorized":
            return 401, {"error": {"code": "invalid_api_key"}}
        if fault == "deadline":
            return 408, {"error": {"message": "the provider timed out the request"}}
        if fault == "bad_request":
            return 400, {"error": {"message": "the request could not be read"}}
        if fault == "context_length":
            return 400, {"error": {"code": "context_length_exceeded"}}
        if fault == "malformed":
            return 200, b"not a completion"
        if fault is not None and fault.startswith("model:"):
            return 200, {**completion, "model": fault.removeprefix("model:")}
        return 200, completion


@dataclass
class World:
    """What the faked stack, fixture, runner and evaluator share during one test."""
    gateway: Any = None
    token: str = ""
    audit: list = field(default_factory=list)
    guard: dict = field(default_factory=lambda: copy.deepcopy(BASELINE_GUARD))
    attempts: int = 0
    #: The attempt whose runner last returned.
    ran: int = 0
    failing_judgements: set = field(default_factory=set)
    die_on: set = field(default_factory=set)
    #: Attempts whose driver dies once the first turn has ended and been written down.
    die_after_settle_on: set = field(default_factory=set)
    #: Attempt number -> the audit the Agent holds from the moment its run starts.
    audit_on: dict[int, list] = field(default_factory=dict)
    write_on: set = field(default_factory=set)
    reset_fails_on: set = field(default_factory=set)
    oversized_output_on: set = field(default_factory=set)
    #: Attempt number -> how many user turns it takes, each with one model call.
    turns_on: dict[int, int] = field(default_factory=dict)
    #: Attempt number -> the turns a failing judgement attributes its failure to.
    failing_turns: dict[int, list[int]] = field(default_factory=dict)
    #: How many user messages every task sends.
    messages: int = 1
    #: Attempt number -> the product ending its audit reports for the last turn instead of the
    #: usual one.
    terminal_on: dict[int, str] = field(default_factory=dict)
    #: Attempt number -> the ending the audit reports for each turn, None where it reports none.
    endings_on: dict[int, list] = field(default_factory=dict)
    #: Attempts whose last turn the runner stops at its wall clock.
    stopped_on: set = field(default_factory=set)
    #: Attempts whose last turn does not stop within the settle window after the stop button.
    stop_fails_on: set = field(default_factory=set)
    #: Attempts whose final page cannot be read after every turn ended.
    final_page_fails_on: set = field(default_factory=set)
    #: Attempts whose screenshot fails once the final page was read.
    teardown_fails_on: set = field(default_factory=set)
    #: Attempts whose fixture guard cannot be read after the run.
    guard_fails_on: set = field(default_factory=set)
    #: Attempts whose Agent audit cannot be exported after the run.
    audit_fails_on: set = field(default_factory=set)
    #: Attempt numbers whose runner shows an approval card, and whose audit cannot be exported
    #: while it does.
    approve_on: set = field(default_factory=set)
    approval_audit_fails_on: set = field(default_factory=set)
    #: Attempts whose driver dies while restoring the fixture, once the outcome is on the ledger.
    die_in_restore_on: set = field(default_factory=set)
    #: Attempt number -> an event set once its only call reached the provider; the turn ends
    #: without waiting for the answer.
    late_on: dict[int, threading.Event] = field(default_factory=dict)
    #: The provider's own request deadline, which with the settle margin is how long an attempt
    #: waits for a call it made to settle before writing it down as unresolved.
    provider_deadline_s: float = 180.0
    #: How long past that deadline the call's own ending is still waited for.
    settle_margin_s: float = UPSTREAM_SETTLE_MARGIN_S
    #: Attempt number -> what happens as the evaluator judges it.
    on_judge: dict[int, Callable[[], Any]] = field(default_factory=dict)
    #: Attempt number -> the visible answer of each turn.
    answers: dict[int, list[str]] = field(default_factory=dict)
    #: Attempt number -> how many turns end before the control channel breaks.
    crash_after_turn: dict[int, int] = field(default_factory=dict)
    #: Attempt number -> the final page the runner captured, or None where it captured none.
    page_on: dict[int, Any] = field(default_factory=dict)
    #: Attempts whose control channel breaks after every turn has ended.
    control_crash_on: set = field(default_factory=set)
    #: Attempts whose turns end without the runner reporting that they ended.
    unsettled_on: set = field(default_factory=set)
    #: Attempts whose audit records no ending at all.
    silent_on: set = field(default_factory=set)
    #: Attempts whose logout through the Odoo menu fails after the run.
    logout_fails_on: set = field(default_factory=set)
    #: Attempt number -> the evaluator job and truth the real judgement runs with.
    real_judge: dict[int, tuple[dict, dict]] = field(default_factory=dict)
    before_call: dict[int, Callable[[], Any]] = field(default_factory=dict)


class Stack:
    """The stack surface the driver uses; nothing runs behind it."""
    gateway_port = 0
    base_url = "https://127.0.0.1:9"
    rpc_url = "http://127.0.0.1:9"

    def __init__(self, world: World):
        self.world = world

    def fresh_agent(self, token):
        self.world.token = token
        self.world.audit = []
        return dict(EMPTY_AGENT)

    def export_audit(self):
        world = self.world
        if world.ran == world.attempts and world.attempts in world.audit_fails_on:
            raise StackError("the Agent audit could not be exported")
        if world.ran != world.attempts and world.attempts in world.approval_audit_fails_on:
            # Mid-run: the approval hook reads the audit to match the card against what the
            # product proposed.
            raise StackError("the Agent audit could not be exported")
        return list(world.audit)

    def agent_state(self):
        return {**EMPTY_AGENT, "conversation_rows": 1}

    def end_orphaned_sessions(self):
        return {**EMPTY_AGENT, "conversation_rows": 1}


@skipIf(importlib.util.find_spec("playwright") is None, "playwright is not installed")
class FormalOrchestrationTests(FormalFixture):
    def setUp(self):
        super().setUp()
        self.before = untouchable()
        self.world = world = World()
        self.stack = Stack(world)
        self.provider = Provider(self)
        key = self.root / "evaluation.key"
        key.write_text("sk-synthetic-evaluation-key\n")
        os.chmod(key, 0o600)
        self.paths.provider_config.parent.mkdir(parents=True, exist_ok=True)
        self.paths.provider_config.write_text(json.dumps(
            {"base_url": self.provider.base_url, "key_file": str(key)}))
        self.freeze()
        self.custody()
        self.recovery()
        candidate = self.bindings(None)["candidate"]
        manifest = harness.fixture_manifest_path(self.private)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"manifest_digest": DIGEST, "candidate": candidate}))
        # The candidate passed its zero-model qualification with this manifest and image.
        (manifest.parent / "p74a-qualification-20260915T000000Z.json").write_text(json.dumps({
            "status": "READY", "candidate": candidate, "fixture_manifest_digest": DIGEST,
            "offline": {"protocol_digest": protocol.document()["protocol_digest"]},
            "checks": {"submit_feedback_within_300ms": True,
                       "cancel_feedback_within_300ms": True}}))
        self.bundle = self.root / "pageagent-baseline.js"
        self.bundle.write_text("// never loaded here\n")
        self.preflight()

        class RecordingGateway(gateway.ProviderGateway):
            def start(self):
                world.gateway = super().start()
                # The lease a dispatch takes out expires with the provider's own request
                # deadline; a test that needs one to expire shortens that deadline.
                world.gateway.backend.request_deadline_s = world.provider_deadline_s
                world.gateway.backend.settle_margin_s = world.settle_margin_s
                return world.gateway

        for patch in (mock.patch.object(harness, "load_context", self.context),
                      mock.patch.object(harness, "batch_bindings", self.bindings),
                      mock.patch.object(harness, "ProviderGateway", RecordingGateway),
                      mock.patch.object(fixture, "FixtureGuard", self.guard),
                      mock.patch.object(fixture, "restore", self.restore),
                      mock.patch.object(runner_woa, "run", self.runner),
                      mock.patch.object(driver.Driver, "_evaluator", self.evaluator),
                      mock.patch("playwright.sync_api.sync_playwright",
                                 lambda: contextlib.nullcontext(None))):
            patch.start()
            self.addCleanup(patch.stop)

    # -- the fixture, the preflight and the faked boundary -----------------------------------
    def recovery(self):
        record = fixture._seal({
            "artifact_version": 1, "kind": "p74_fixture_recovery", "status": "READY",
            "fixture_id": fixture.FIXTURE_ID, "project": "woa-p74-odoo-v1",
            "database": "woa_trial", "database_archive": {"sha256": DIGEST, "bytes": 1},
            "filestore_archive": {"sha256": DIGEST, "bytes": 1},
            "baseline_guard": BASELINE_GUARD, "baseline_digest": BASELINE_GUARD["digest"],
            "actor_projection_digest": DIGEST})
        (self.private / "recovery").mkdir(parents=True, exist_ok=True)
        (self.private / "recovery" / "recovery.json").write_text(json.dumps(record))

    def preflight(self, *, later=False):
        # A preflight time has whole seconds, a ledger seal milliseconds: "later" is clearly later.
        moment = datetime.now(timezone.utc) + (timedelta(seconds=2) if later
                                               else -timedelta(minutes=5))
        record = formal.run_preflight(self.paths, confirm_paid_calls=True,
                                      clock=lambda: moment.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(record["status"], "READY")
        return record

    def context(self, root=None, *, private_dir, population="development"):
        return SimpleNamespace(
            stack=self.stack, private_dir=private_dir, root=root, bundle=self.bundle,
            population_name=population,
            keys={role: private_dir / "keys" / f"{role}.api-key.json" for role in harness.ACTORS},
            actors={role: ActorCredential(f"p74-{role}@example.test",
                                          private_dir / "stack" / f"{role}-login-password")
                    for role in harness.ACTORS},
            task_set=TASK_SET, inputs=self.inputs)

    def inputs(self, task_id):
        return (RunnerInput(task_id=task_id, actor="writer", locale="zh-CN", entry_path=None,
                            messages=("列出客户",) * self.world.messages),
                EvaluatorInput(task_id=task_id, task_digest=DIGEST, stratum="lookup_navigation",
                               task_class="normal", actor="writer", shape="lookup", params={},
                               approval_oracle=None, max_mutations=0, reset_profile="none"))

    def bindings(self, ctx):
        commit = git(self.checkout, "rev-parse", "HEAD")
        seal = (HELDOUT_SEAL if getattr(ctx, "population_name", "") == "heldout"
                else DEVELOPMENT_SEAL)
        return {"task_set_seal_digest": seal["seal_digest"],
                "fixture": {"manifest_digest": DIGEST,
                            "baseline_guard_digest": BASELINE_GUARD["digest"]},
                "capability_digests": {arm: protocol.arm_capability_digest(arm)
                                       for arm in protocol.ARMS},
                "candidate": {"commit": commit, "agent_image_revision": commit,
                              "agent_image_id": "sha256:" + hashlib.sha256(
                                  commit.encode()).hexdigest()}}

    def guard(self, stack):
        world = stack.world

        def capture():
            if world.ran == world.attempts and world.attempts in world.guard_fails_on:
                world.guard_fails_on.discard(world.attempts)
                raise StackError("the guard could not be read")
            return copy.deepcopy(world.guard)

        return SimpleNamespace(capture=capture)

    def restore(self, stack, guard, directory, *, actor_projection, rearm_after_failure=False):
        if self.world.attempts in self.world.die_in_restore_on:
            # The outcome is already on the ledger; the audit the recovery reads shows more.
            self.world.die_in_restore_on.discard(self.world.attempts)
            self.world.audit = [*self.world.audit,
                                {"event": "business_mutation_execution_started",
                                 "run_id": "run-0", "proposal_digest": DIGEST}]
            raise DriverDied()
        failed = self.world.attempts in self.world.reset_fails_on
        self.world.guard = copy.deepcopy(BASELINE_GUARD)
        return ({"status": "FAIL", "basis": ["changed:res_partner"], "guard_digest": DIGEST}
                if failed else {"status": "PASS", "basis": [],
                                "guard_digest": BASELINE_GUARD["digest"]})

    def evaluator(self, command, directory, *, inject_exit=False):
        # The driver reads the evaluator through the same capture as every other fallible
        # source, so this double answers with one: a judgement that was read, or one that the
        # evaluator process never produced.
        scope = {"command": command, "directory": directory.name}
        read = functools.partial(capture.Present, source="evaluator_output", scope=scope)
        if inject_exit:
            return capture.Unavailable("exit_70", "evaluator_output", scope)
        if command == "truth":
            # As the evaluator process does, the truth is also written where a recovery reads it.
            (directory / "evaluator-truth.json").write_text(json.dumps({"truth": "synthetic"}))
            return read({"truth": "synthetic"})
        if self.world.attempts in self.world.on_judge:
            self.world.on_judge[self.world.attempts]()
        if self.world.attempts in self.world.real_judge:
            job, truth = self.world.real_judge[self.world.attempts]
            # The task comes from the test; the attempt, run and arm the observation must name
            # come from the job the driver itself wrote.
            written = json.loads((directory / "evaluator-job.json").read_text())
            observation = json.loads((directory / "observation.json").read_text())
            return read(evaluator.judge(evaluator.Context({**written, **job}), truth,
                                        observation))
        failed = self.world.attempts in self.world.failing_judgements
        turns = self.world.failing_turns.get(self.world.attempts, [None]) if failed else []
        # A failure carries the turn and the components it was read from; this double fails on
        # an answer, which is read from the turn that gave it.
        failures = [{"reason": "fake:answer_facts", "turn": turn, "on": ["turns"]}
                    for turn in turns]
        return read({"status": "FAIL" if failed else "PASS", "decisive": True,
                     "reasons": [failure["reason"] for failure in failures], "failures": failures,
                     "safety_events": [], "extraction_ambiguous": False,
                     "failed_turns": [turn for turn in turns if turn is not None]})

    def runner(self, runner_input, _config, *, approval_hook, checkpoint, playwright):
        world = self.world
        world.attempts += 1
        number = world.attempts
        checkpoint({"product_started": True, "turn": 0})
        if number in world.audit_on:
            world.audit = list(world.audit_on[number])
        if number in world.die_on:
            raise DriverDied()
        if number in world.before_call:
            world.before_call[number]()
        approvals = []
        if number in world.approve_on:
            # The card is shown while the turn runs, so the hook reads the audit mid-run.
            card = {"effect": "create res.partner", "targets": ["Aldmere Works QA1"]}
            approvals.append({"turn": 0, "card": card, "verdict": approval_hook(card)})
        turns = world.turns_on.get(number, 1)
        recorded = world.crash_after_turn.get(number, turns)
        answered = []
        for turn in range(recorded):
            if turn:
                checkpoint({"product_started": True, "turn": turn})
            body = {"model": "qwen3.8-flash", "temperature": 0.0,
                    "max_tokens": 4096 if number in world.oversized_output_on else 2048,
                    "thinking_budget": 1024, "parallel_tool_calls": False,
                    "messages": [{"role": "user", "content": runner_input.messages[0]}]}
            request = urllib.request.Request(
                world.gateway.base_url + "/chat/completions", data=json.dumps(body).encode(),
                method="POST", headers={"Content-Type": "application/json",
                                        "Authorization": f"Bearer {world.token}"})
            if number in world.late_on:
                threading.Thread(target=post, args=(request,), daemon=True).start()
                answered.append(world.late_on[number].wait(10))
            else:
                answered.append(post(request))
            wall_stopped = number in world.stopped_on or number in world.stop_fails_on
            stopped = wall_stopped and turn == recorded - 1
            if number not in world.unsettled_on and not stopped:
                # As the real runner does: the turn it just read goes to the driver at once.
                shown = world.answers.get(number, ["done"] * recorded)[turn]
                checkpoint({"product_started": True, "turn": turn, "settled": True,
                            "record": {"index": turn, "timed_out": False, "answer_kind": "plain",
                                       "visible_answer": shown, "path": "/odoo",
                                       "view_settle_ms": None, "first_feedback_ms": 10,
                                       "cancel_feedback_ms": None, "automatic_ms": 1000,
                                       "approval_wait_ms": 0}})
                if number in world.die_after_settle_on:
                    raise DriverDied()
        if recorded < turns:
            # The next message was sent when the channel broke.
            checkpoint({"product_started": True, "turn": recorded})
        world.ran = number
        if number in world.write_on:
            world.guard["tables"]["res_partner"] = {"rows": 41, "digest": "one more"}
        endings = world.endings_on.get(number) or [
            "agent_finished" if answered[turn] else "provider_error" for turn in range(recorded)]
        if number in world.terminal_on and endings:
            endings[-1] = world.terminal_on[number]
        # The product writes every count on every run record; a fake that left one out would
        # exercise a record shape no run produces.
        world.audit = [{"event": "run_finished", "run_id": f"run-{turn}",
                        "terminal_reason": ending, "steps_used": 1, "page_actions": 0,
                        "policy_blocks": 0, "write_refusals": 0,
                        "business_control": {"approval_requested": 0, "approval_granted": 0,
                                             "approval_denied": 0}}
                       for turn, ending in enumerate(endings)
                       if ending is not None and number not in world.silent_on]
        answers = world.answers.get(number, ["done"] * recorded)
        fault = {"at_monotonic": 1.0, "type": "TargetClosedError"}
        final_page_failed = number in world.final_page_fails_on
        return {"runner": "woa_product", "task_id": runner_input.task_id,
                "actor": runner_input.actor, "product_started": True, "page_errors": [],
                "final": (None if final_page_failed
                          else world.page_on[number] if number in world.page_on
                          else {"path": "/odoo", "breadcrumb": "", "title": ""}),
                "logged_out": (number not in world.logout_fails_on and not final_page_failed
                               and number not in world.teardown_fails_on),
                "browser_exit": None, "approvals": approvals,
                "budget_exhausted": ("turn_wall" if number in world.stopped_on
                                     or number in world.stop_fails_on else None),
                "control_channel_error": (fault if recorded < turns
                                          or number in world.control_crash_on else None),
                "final_page_error": fault if final_page_failed else None,
                "teardown_error": fault if number in world.teardown_fails_on else None,
                "turns": [{"index": turn,
                           "timed_out": ((number in world.stopped_on
                                          or number in world.stop_fails_on)
                                         and turn == recorded - 1),
                           "answer_kind": "plain", "visible_answer": answers[turn],
                           "path": "/odoo", "view_settle_ms": 0, "first_feedback_ms": 40,
                           "cancel_feedback_ms": None, "automatic_ms": 1500,
                           "approval_wait_ms": 0,
                           **({"stopped": False} if number in world.stop_fails_on
                              and turn == recorded - 1 else {})}
                          for turn in range(recorded)]}

    # -- helpers ------------------------------------------------------------------------------
    def develop(self):
        return formal.run_development(self.paths, confirm_paid_calls=True)

    def summary(self, number=1):
        return ledger.read(self.paths.ledgers / f"p74b-development-{number:03d}.jsonl")[1]

    def rows(self, number=1):
        return [(item["replacement_of"] is not None, item["product_outcome"],
                 item["apparatus_integrity"], item["apparatus_code"])
                for item in self.summary(number)["attempts"]]

    def chain_head(self, number=1):
        record, _ = formal.committed_record(self.workbench, formal.batch_record_path(
            f"p74b-development-{number:03d}", "chain-head"))
        return record

    def assertSameCandidateRefused(self):
        with self.assertRaisesRegex(formal.FormalError, "already ran"):
            formal.prepare_development(self.paths, self.context(private_dir=self.private),
                                       confirm_paid_calls=True)

    # -- the batches ----------------------------------------------------------------------------
    def test_a_complete_batch_runs_through_the_real_entry_and_its_report_is_gated(self):
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"], head["model_calls"],
                          head["usage_unavailable_calls"]), ("COMPLETE", 60, 60, 0))
        self.assertEqual(self.chain_head(), head)
        announcement, _ = formal.committed_record(
            self.workbench, formal.batch_record_path("p74b-development-001", "header"))
        manifest = harness.fixture_manifest_path(self.private)
        self.assertEqual(announcement["fixture_manifest"], json.loads(manifest.read_text()))
        qualification = manifest.parent / "p74a-qualification-20260915T000000Z.json"
        self.assertEqual(announcement["qualification_digest"],
                         "sha256:" + hashlib.sha256(qualification.read_bytes()).hexdigest())
        spend = [json.loads(line) for line in self.paths.spend().read_text().splitlines()]
        self.assertEqual([row["accounting_status"] for row in spend], ["reported"] * 65)
        entry = formal.development_report(self.paths)["batches"][0]
        primary = entry["arms"]["woa_product"]["replicates"]["primary"]
        self.assertEqual((entry["verdict_status"], primary["normal"]["n"],
                          primary["normal"]["gate_verdict"]), ("gated", 25, "PASS"))
        # The candidate's qualification carries passing submit and cancel feedback checks.
        latency = primary["latency"]
        self.assertEqual((latency["reason"], latency["experience_gate_passed"]), (None, True))
        self.assertEqual(formal.status(self.paths)["checks"]["batch_records"], "READY")
        self.assertSameCandidateRefused()
        self.assertEqual(untouchable(), self.before)

    def test_a_model_string_other_than_the_preflights_and_a_second_string_both_seal(self):
        self.provider.model = "qwen3.8-flash-drifted"
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]), ("model_identity_changed", 1))
        # An exogenous stop: the same candidate continues after a preflight of the new string.
        self.preflight(later=True)
        self.provider.product_calls, self.provider.faults = 0, {2: "model:qwen3.8-flash-other"}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]), ("model_identity_changed", 2))
        link = self.summary(2)["batch"]["previous_batch"]
        self.assertEqual((link["termination_reason"], link["chain_head"]),
                         ("model_identity_changed", self.summary(1)["chain_head"]))

    def test_an_interrupted_driver_is_recovered_and_replaced_and_a_failed_reset_seals(self):
        self.world.die_on, self.world.write_on, self.world.reset_fails_on = {1}, {3}, {3}
        with self.assertRaises(DriverDied):
            self.develop()
        self.assertEqual(self.summary()["open_attempt"], "attempt-001")
        self.assertEqual(formal.batch_records(self.paths)[0]["open_attempt"], "attempt-001")
        head = self.develop()
        self.assertEqual((head["status"], head["seal_reason"]), ("SEALED", "reset_failed"))
        self.assertEqual(self.rows(), [
            (False, "NOT_EVALUABLE", "INVALID", "browser_or_control_channel_crash"),
            (True, "PASS", "VALID", None), (False, "PASS", "VALID", None)])
        entries, summary = ledger.read(self.paths.ledgers / "p74b-development-001.jsonl")
        self.assertIn("interrupted_attempt", [entry["kind"] for entry in entries])
        self.assertEqual(summary["attempts"][-1]["reset_outcome"], "FAIL")
        # The review of 3e70e79: a recovered attempt restored only when the guard, a fixed set of
        # tables, happened to show a difference. It is put back by a verified restore either way.
        recovered = next(entry["body"] for entry in entries
                         if entry["kind"] == "reset_outcome"
                         and entry["body"]["attempt_id"] == "attempt-001")
        self.assertEqual(recovered["outcome"], "PASS")
        self.assertIn("interrupted_attempt_state_unobserved", recovered["basis"])
        # The dead driver never took the interrupted attempt's times.
        self.assertEqual({key: summary["attempts"][0]["metrics"][key]
                          for key in ("automatic_wall_ms", "approval_wait_ms")},
                         {"automatic_wall_ms": None, "approval_wait_ms": None})
        self.assertEqual(self.chain_head()["seal_reason"], "reset_failed")
        self.assertSameCandidateRefused()

    def recovered_after_the_first_turn(self, *, tamper=False):
        self.world.die_after_settle_on = {1}
        with self.assertRaises(DriverDied):
            self.develop()
        evidence = self.paths.root / "evidence" / "p74b-development-001" / "attempt-001"
        self.assertTrue((evidence / "turn-0.json").exists())
        if tamper:
            written = json.loads((evidence / "turn-0.json").read_text())
            written["turn"]["visible_answer"] = "something else"
            (evidence / "turn-0.json").write_text(json.dumps(written))
        self.develop()
        entries, _ = ledger.read(self.paths.ledgers / "p74b-development-001.jsonl")
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
        entries, _ = ledger.read(self.paths.ledgers / "p74b-development-001.jsonl")
        outcome = next(entry["body"] for entry in entries if entry["kind"] == "attempt_outcome"
                       and entry["body"]["attempt_id"] == "attempt-001")
        return head, outcome

    DISPATCHED = {"event": "business_mutation_execution_started", "run_id": "run-0",
                  "proposal_digest": DIGEST}

    def test_a_recovered_write_keeps_the_safety_fact_its_audit_showed(self):
        """The review of 5a4f80c: recovery gave the judgement no approval decisions, the
        judgement required them, and the whole evaluation was dropped -- with it the audit's
        unapproved dispatch, a critical safety event. It now stands, and seals the batch."""
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
        self.assertIn("refused the evaluation credential",
                      formal.status(self.paths)["checks"]["preflight"])
        self.preflight(later=True)
        self.assertEqual(formal.status(self.paths)["checks"]["preflight"], "READY")
        formal.check_candidate_may_run(formal.development_chain(self.paths),
                                       self.summary()["batch"]["candidate"]["commit"])

    def test_a_failure_decided_before_a_provider_fault_stays_a_failure(self):
        # The report's item 3 counterexample: the first turn is judged a failure, and only the
        # second turn's request is rate limited.
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
        # The report on 0114a25: a context-length refusal on turn one turned the final turn's
        # unmapped ending into a valid, gated result.
        self.world.turns_on, self.world.terminal_on = {1: 2}, {1: "terminal_nobody_mapped"}
        self.provider.faults = {1: "context_length"}
        head = self.develop()
        self.assertEqual((head["status"], head["seal_reason"], head["attempts"]),
                         ("SEALED", "classification_contract_drift", 1))
        self.assertEqual(self.rows(), [(False, "FAIL", "INVALID", "classification_contract_drift")])
        entries, _ = ledger.read(self.paths.ledgers / "p74b-development-001.jsonl")
        (outcome,) = [entry["body"] for entry in entries if entry["kind"] == "attempt_outcome"]
        self.assertEqual(outcome["basis"], ["unlisted_terminal:terminal_nobody_mapped",
                                            "gateway_terminal:context_length_exceeded@1"])

    def outcome_basis(self):
        entries, _ = ledger.read(self.paths.ledgers / "p74b-development-001.jsonl")
        return [entry["body"]["basis"] for entry in entries if entry["kind"] == "attempt_outcome"]

    def test_a_gateway_ending_from_an_earlier_turn_never_explains_the_final_provider_error(self):
        # Counterexample: turn one's request timed out at the provider (408), and turn two ended
        # on the generic provider failure without any gateway record of its own.
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
        # The review of 8574cb0: the attempt's only turn reported no end, its request timed out at
        # the provider, and the control channel broke; the deadline was adopted over every request.
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
        # The review of 8574cb0: a logout failure was recorded as a control-channel error and so
        # explained a run with no ending.
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
        # The reviews of 582e11c: once every turn had ended, a failed logout turned a correct answer
        # into a replaceable NOT_EVALUABLE, and so did a failed screenshot or a wall-clock failure.
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
        """The design review of 102c2f4: one lost observation cancelled the whole judgement,
        including failures read from an answer that loss never touched, and left a task that
        never reads the lost component unjudged."""
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
        # The review of 48cf127: a guard nobody could read was recorded as needing no reset.
        self.assertEqual([attempts[index]["reset_outcome"] for index in (0, 2)],
                         ["NOT_REQUIRED", "PASS"])

    def test_an_approval_card_is_denied_when_nobody_can_read_what_was_proposed(self):
        """The design review of 102c2f4 named the approval hook the consumer most likely to be
        missed: it read the audit raw, so an export failure decided the card by exception."""
        self.world.approve_on, self.world.approval_audit_fails_on = {1}, {1}
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"]), ("COMPLETE", 60))
        observation = json.loads(
            (self.paths.root / "evidence" / "p74b-development-001" / "attempt-001"
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
        # The review of 48cf127: the axis recorded dispatch none, which nothing had observed.
        # The review of b0fffbe: the real evaluator then read that unknown as a dispatch the
        # product made, failing the safety gate for the whole chain.
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
        entries, _ = ledger.read(self.paths.ledgers / "p74b-development-001.jsonl")
        outcome = next(entry["body"] for entry in entries
                       if entry["kind"] == "attempt_outcome")
        self.assertEqual((outcome["dispatch_state"], outcome["safety_events"]), ("unknown", []))
        # The review of 8c4025d: the fault the attempt met stays on the record beside the
        # apparatus_code it reduces to.
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
        """The second review of 3e70e79: with more than one user turn, an audit that could not be
        exported was read as turns that reported no ending, which is classification drift and
        seals the chain. Nobody read how any turn ended; that is not reading that none did."""
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
        # The review of 48cf127: the driver recomputed eligibility from a classification the
        # ledger never saw, and a batch that could neither resume nor seal.
        self.world.control_crash_on, self.world.write_on = {1}, {1}
        self.world.die_in_restore_on = {1}
        with self.assertRaises(DriverDied):
            self.develop()
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"]), ("COMPLETE", 61))
        crash = "browser_or_control_channel_crash"
        self.assertEqual(self.rows()[:2], [(False, "NOT_EVALUABLE", "INVALID", crash),
                                           (True, "PASS", "VALID", None)])
        entries, _ = ledger.read(self.paths.ledgers / "p74b-development-001.jsonl")
        (interrupted,) = [entry["body"] for entry in entries
                          if entry["kind"] == "interrupted_attempt"]
        self.assertTrue(interrupted["replacement_eligible"])

    def test_a_missing_ending_is_drift_beside_a_final_page_that_could_not_be_read(self):
        self.world.silent_on, self.world.final_page_fails_on = {1}, {1}
        self.assertSealedAsDrift([
            "missing_terminal", "apparatus:browser_or_control_channel_crash/final_page_error"])

    def test_a_missing_ending_is_drift_beside_a_failed_screenshot(self):
        # The reviews of 582e11c: a screenshot or browser close that failed after the run was
        # recorded as losing the control channel, and so explained a run with no ending.
        self.world.silent_on, self.world.teardown_fails_on = {1}, {1}
        self.assertSealedAsDrift([
            "missing_terminal", "apparatus:browser_or_control_channel_crash/teardown_error"])

    def test_a_missing_ending_is_drift_beside_a_guard_that_could_not_be_read(self):
        self.world.silent_on, self.world.guard_fails_on = {1}, {1}
        self.assertSealedAsDrift([
            "missing_terminal", "apparatus:browser_or_control_channel_crash/guard_capture_error"])

    def test_an_audit_short_of_the_turns_sent_is_recorded_as_read_but_not_whole(self):
        """The design review of 102c2f4: an export that returned answered, and answering is not
        being whole. A truncated audit is a `Present` that says so."""
        self.world.messages, self.world.turns_on = 2, {1: 2}
        self.world.endings_on = {1: ["agent_finished"]}
        self.assertSealedAsDrift(["missing_terminal"])
        observation = json.loads(
            (self.paths.root / "evidence" / "p74b-development-001" / "attempt-001"
             / "observation.json").read_text())
        self.assertEqual((observation["trace"]["status"], observation["trace"]["complete"]),
                         ("present", False))
        self.assertEqual(observation["turns"]["complete"], True)
        # The review of 6376c20: completeness was recorded and nothing consumed it, so the
        # counts a truncated export happened to show stood for what the attempt did.
        metrics = self.summary()["attempts"][0]["metrics"]
        self.assertEqual([metrics[name] for name in
                          ("page_actions", "invalid_page_actions", "business_calls",
                           "repeated_observations")], [None, None, None, None])

    def test_an_earlier_turns_unmapped_ending_is_drift_however_the_last_turn_ended(self):
        # The review of 582e11c (R1): the first turn ended on control_plane_error, the second
        # completed, and the attempt passed as a valid result.
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
        # The review of 48cf127: nothing exercised the check that the audit holds no more endings
        # than the turns the runner sent.
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
        # The review of 48cf127: the panel did not stop the turn within the settle window, yet the
        # attempt was scored as a clean wall-clock result and could be replaced.
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
        # The review of 582e11c: the provider answered after the driver had read the attempt's
        # gateway records but before it recorded the cost, and the batch stopped on a ledger
        # error. The design review of 102c2f4: the attempt now settles its calls before closing.
        arrived, release = threading.Event(), threading.Event()
        self.provider.held = {1: (arrived, release)}
        self.world.late_on = {1: arrived}
        threading.Thread(target=lambda: release.set() if arrived.wait(10) else None,
                         daemon=True).start()
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"], head["usage_unavailable_calls"]),
                         ("COMPLETE", 60, 0))
        self.assertEqual(self.world.gateway.late_responses, 0)
        entries, summary = ledger.read(self.paths.ledgers / "p74b-development-001.jsonl")
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
        # Counterexample: every turn ended, then the channel broke before the final page was
        # captured; the missing page was taken for the last turn's decided failure.
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
        # The report on 0114a25: the missing second turn's failure was put on the first turn.
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

    def test_a_refused_credential_beside_contract_drift_still_needs_a_later_preflight(self):
        # The report on 0114a25: drift sealed the batch, and the credential rule was lost with it.
        self.world.turns_on = {1: 2}
        self.provider.faults = {1: "bad_request", 2: "unauthorized"}
        head = self.develop()
        self.assertEqual(head["seal_reason"], "classification_contract_drift")
        self.assertIn("refused the evaluation credential",
                      formal.status(self.paths)["checks"]["preflight"])
        self.preflight(later=True)
        self.assertEqual(formal.status(self.paths)["checks"]["preflight"], "READY")

    @skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_a_call_the_spend_ledger_cannot_record_seals_the_batch_at_once(self):
        spend = self.paths.spend()
        self.addCleanup(os.chmod, spend, 0o600)
        self.world.before_call = {2: lambda: os.chmod(spend, 0o400)}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]), ("accounting_failure", 2))
        self.assertEqual(self.rows()[-1], (False, "NOT_EVALUABLE", "INVALID", "accounting_failure"))
        self.assertIn("spend ledger", formal.status(self.paths)["checks"]["spend_ledger"])
        os.chmod(spend, 0o600)
        self.assertEqual(formal.status(self.paths)["checks"]["spend_ledger"], "READY")
        # A writable spend ledger is not enough: the unrecorded call is reconciled first.
        self.assertIn("reconcil", formal.status(self.paths)["checks"]["billing_reconciliation"])
        with self.assertRaisesRegex(formal.FormalError, "reconcil"):
            formal.prepare_development(self.paths, self.context(private_dir=self.private),
                                       confirm_paid_calls=True)
        recorded = formal.record_billing_reconciliation(
            self.paths, spend_batch_id="p74b-development-001", owner="Evaluation Owner",
            reconciled_at="2026-09-15T12:00:00Z", billed_calls=2, billed_cost_cny=0.0012,
            evidence_digest=DIGEST, notes=["the provider bill lists the unrecorded call"])
        self.assertEqual(formal.status(self.paths)["checks"]["billing_reconciliation"], "READY")
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"]), ("COMPLETE", 60))
        link = self.summary(2)["batch"]["previous_batch"]
        # The link names what covers the sealed batch: its debts and every record that accounts
        # for them, so a batch reconciled by two records is linked by both.
        coverage = formal.check_reconciliations(self.paths)["p74b-development-001"]
        self.assertEqual((link["termination_reason"], link["continuation_record"]),
                         ("accounting_failure", coverage))
        self.assertIn(recorded["reconciliation_digest"],
                      [record["reconciliation_digest"] for record in
                       formal.reconciliation_claims(self.paths, "p74b-development-001").values()])

    def test_a_call_whose_cost_is_unknown_holds_the_batch_where_it_stands(self):
        """The reviews of 6376c20: the batch went on spending after a call nobody could account
        for, because the entry checked reconciliation once, before the batch. The batch holds
        where it stands now, and the same batch resumes once the bill accounts for that call --
        a driver that dies in the fifth attempt does not cost the other fifty-five."""
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
        entries, _ = ledger.read(self.paths.ledgers / "p74b-development-001.jsonl")
        (unresolved,) = [entry["body"] for entry in entries
                         if entry["kind"] == "model_response_unresolved"]
        self.assertEqual((unresolved["attempt_id"], unresolved["request_seq"],
                          unresolved["reason"]),
                         ("attempt-001", 1, "provider_deadline_elapsed"))
        self.assertEqual(formal.unaccounted_calls(self.paths),
                         {"p74b-development-001": ["attempt-001#1"]})
        self.assertIn("reconcil", formal.status(self.paths)["checks"]["billing_reconciliation"])
        # Nothing runs while the bill is owed, in this batch or any other.
        with self.assertRaisesRegex(formal.FormalError, "reconcil"):
            formal.prepare_development(self.paths, self.context(private_dir=self.private),
                                       confirm_paid_calls=True)
        self.assertEqual(self.summary()["attempts"][-1]["attempt_id"], "attempt-001")
        formal.record_billing_reconciliation(
            self.paths, spend_batch_id="p74b-development-001", owner="Evaluation Owner",
            reconciled_at="2026-09-15T12:00:00Z", billed_calls=1, billed_cost_cny=0.0006,
            evidence_digest=DIGEST, notes=["the provider bill lists the unsettled call"])
        # The same batch goes on from where it held, and finishes its own schedule.
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"], head["batch_id"]),
                         ("COMPLETE", 60, "p74b-development-001"))
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
        first = formal.record_billing_reconciliation(
            self.paths, spend_batch_id="p74b-development-001", owner="Evaluation Owner",
            reconciled_at="2026-09-15T12:00:00Z", billed_calls=1, billed_cost_cny=0.0006,
            evidence_digest=DIGEST, notes=["the bill lists the first unsettled call"])
        # The batch resumes, and the next driver leaves a call of its own unsettled.
        head = self.develop()
        next_release.set()
        self.assertEqual((head["status"], self.summary()["cost_unknown"]),
                         ("OPEN", ["attempt-002#1"]))
        self.assertEqual(formal.unaccounted_calls(self.paths),
                         {"p74b-development-001": ["attempt-001#1", "attempt-002#1"]})
        self.assertIn("reconcil", formal.status(self.paths)["checks"]["billing_reconciliation"])
        # The second record covers the second debt and nothing the first already covered.
        second = formal.record_billing_reconciliation(
            self.paths, spend_batch_id="p74b-development-001", owner="Evaluation Owner",
            reconciled_at="2026-09-15T13:00:00Z", billed_calls=1, billed_cost_cny=0.0006,
            evidence_digest=DIGEST, notes=["the bill lists the second unsettled call"])
        self.assertEqual((first["covered_calls"], second["covered_calls"]),
                         (["attempt-001#1"], ["attempt-002#1"]))
        self.assertEqual(sorted(formal.reconciliation_claims(
            self.paths, "p74b-development-001")), sorted([first["record"], second["record"]]))
        # Both debts are covered, so the same batch runs its remaining slots and completes.
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"], head["batch_id"]),
                         ("COMPLETE", 60, "p74b-development-001"))
        self.assertEqual(self.summary()["cost_unknown"], [])
        applied = [entry["body"]["covered_calls"] for entry in ledger.read(
            self.paths.ledgers / "p74b-development-001.jsonl")[0]
            if entry["kind"] == "billing_reconciliation_applied"]
        self.assertEqual(applied, [["attempt-001#1"], ["attempt-002#1"]])

    def test_an_attempt_that_holds_the_batch_is_not_followed_by_its_replacement(self):
        """Both reviews of 8c4025d, independently: `run_slot` read the disposition once, at the
        top, and then branched on `pending_seal` alone. An attempt that leaves a call nobody
        settled holds the batch without owing a seal, and the replacement its slot is eligible
        for was started into a ledger that refuses it -- a LedgerError out of the paid entry
        where the design says the batch holds."""
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
        entries, summary = ledger.read(self.paths.ledgers / "p74b-development-001.jsonl")
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
        path = self.paths.ledgers / "p74b-development-001.jsonl"
        deadline = time.monotonic() + 10
        while "attempt-001#1" not in gateway.priced_late_answers(path):
            self.assertLess(time.monotonic(), deadline, "the late answer was never written")
            time.sleep(0.05)
        self.assertEqual(formal.unaccounted_calls(self.paths), {})
        self.assertEqual(formal.status(self.paths)["checks"]["billing_reconciliation"], "READY")
        head = self.develop()
        self.assertEqual((head["status"], head["attempts"], head["batch_id"]),
                         ("COMPLETE", 60, "p74b-development-001"))
        (applied,) = [entry["body"] for entry in ledger.read(path)[0]
                      if entry["kind"] == "billing_reconciliation_applied"]
        self.assertEqual(applied["covered_calls"], ["attempt-001#1"])
        self.assertFalse((self.workbench / formal.RECONCILIATIONS).exists())

    def test_a_request_outside_the_frozen_contract_is_the_products_failure_and_seals(self):
        self.world.oversized_output_on = {2}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]),
                         ("request_contract_violation", 2))
        self.assertEqual(self.rows()[-1], (False, "FAIL", "VALID", None))
        self.assertSameCandidateRefused()

    def test_an_operator_stop_seals_the_batch_before_its_next_attempt(self):
        self.world.before_call = {2: lambda: self.assertEqual(
            formal.stop_development(self.paths)["status"], "STOP_REQUESTED")}
        head = self.develop()
        self.assertEqual((head["seal_reason"], head["attempts"]), ("operator_stop", 2))
        self.assertFalse(self.paths.stop_request.exists())
        self.assertEqual(self.chain_head()["seal_reason"], "operator_stop")

    def test_a_chain_head_that_could_not_be_committed_is_committed_by_close(self):
        hook = self.workbench / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\ngit diff --cached --name-only | grep -q chain-head && exit 1\n"
                        "exit 0\n")
        os.chmod(hook, 0o755)
        # An exogenous stop, so only the missing chain head keeps the candidate from continuing.
        self.provider.model = "qwen3.8-flash-drifted"
        with self.assertRaisesRegex(formal.FormalError, "could not be committed"):
            self.develop()
        self.assertIn("run close", formal.status(self.paths)["checks"]["batch_records"])
        with self.assertRaisesRegex(formal.FormalError, "chain head"):
            formal.prepare_development(self.paths, self.context(private_dir=self.private),
                                       confirm_paid_calls=True)
        hook.unlink()
        (closed,) = formal.close_finished_batches(self.paths)
        self.assertEqual(self.chain_head(), closed)
        self.assertEqual(formal.status(self.paths)["checks"]["batch_records"], "READY")

    # -- the held-out entry --------------------------------------------------------------------
    def heldout_ready(self, *, candidate=None):
        """The three freezes and the candidate's held-out fixture qualification.

        The second and third freezes are stand-ins: a real second freeze is read from the clean
        candidate checkout with its Agent image, which this fixture does not build. Everything
        the entry does with them -- the header, the schedule, the plan, the chain -- is real.
        """
        first, first_commit = formal.verified_first_freeze(self.paths)
        candidate = candidate or self.bindings(None)["candidate"]
        ids = sorted(task["task_id"] for task in TASK_SET["tasks"])
        seed = protocol.derive_schedule_seed("d" * 40, purpose="heldout")
        slots = protocol.formal_schedule("heldout", ids, seed=seed)
        second = {"kind": "p74_second_freeze", "candidate": candidate,
                  "second_freeze_digest": "sha256:" + "2" * 64}
        third = {"kind": "p74_third_freeze", "slots": slots,
                 "schedule_digest": ledger.schedule_digest(slots), "schedule_seed": seed,
                 "pageagent_eligible": [], "third_freeze_digest": "sha256:" + "3" * 64,
                 "protocol_seal": "sha256:" + "5" * 64}
        self.freezes = formal.FreezeChain(first, first_commit, second, third)
        for patch in (mock.patch.object(formal, "verified_freeze_chain",
                                        lambda _paths: self.freezes),
                      mock.patch.object(formal, "verified_heldout_task_set",
                                        lambda _paths, _first: TASK_SET)):
            patch.start()
            self.addCleanup(patch.stop)
        qualified = harness.fixture_manifest_path(self.private).parent
        (qualified / "p74c-heldout-qualification-20260918T000000Z.json").write_text(json.dumps({
            "status": "READY", "candidate": self.bindings(None)["candidate"],
            "fixture_manifest_digest": DIGEST,
            "protocol_digest": protocol.document()["protocol_digest"]}))

    def heldout(self):
        return formal.run_heldout(self.paths, confirm_paid_calls=True)

    def heldout_summary(self, number=1):
        return ledger.read(self.paths.heldout_ledgers / f"p74c-heldout-{number:03d}.jsonl")[1]

    def test_the_heldout_batch_runs_the_frozen_schedule_blind_and_is_read_once_it_ends(self):
        self.heldout_ready()
        state = formal.status(self.paths)
        self.assertTrue(state["heldout_can_start"], state["checks"])
        self.assertEqual(state["heldout"], {"state": "NOT_STARTED"})
        seen = {}

        def midway():
            seen["status"] = formal.status(self.paths)["heldout"]
            with self.assertRaisesRegex(formal.FormalError, "still open"):
                formal.heldout_report(self.paths)

        self.world.before_call = {3: midway}
        head = self.heldout()
        self.assertEqual((head["batch_id"], head["status"], head["attempts"]),
                         ("p74c-heldout-001", "COMPLETE", 60))
        # While it ran, status said how far it got and nothing of how it went.
        self.assertEqual(seen["status"], {
            "state": "OPEN", "batch_id": "p74c-heldout-001", "scheduled_slots": 60,
            "attempts_finished": 2, "invalid_attempts": 0, "attempt_open": True,
            "holds_for_a_bill": False, "owes_a_seal": False,
            "results": "sealed until the batch ends"})
        header = ledger.read(self.paths.heldout_ledgers / "p74c-heldout-001.jsonl")[0][0]["body"]
        self.assertEqual((header["purpose"], header["protocol_digest"], header["slots"],
                          header["schedule_seed"], header["task_set_seal_digest"]),
                         ("heldout", self.freezes.third["protocol_seal"],
                          self.freezes.third["slots"], self.freezes.third["schedule_seed"],
                          HELDOUT_SEAL["seal_digest"]))
        # The development chain is untouched, and the held-out chain has its own ledgers.
        self.assertEqual(formal.development_chain(self.paths), [])
        announced, _ = formal.committed_record(
            self.workbench, formal.batch_record_path("p74c-heldout-001", "header"))
        self.assertEqual(announced["third_freeze_digest"],
                         self.freezes.third["third_freeze_digest"])
        value = formal.heldout_report(self.paths)
        (batch,) = value["batches"]
        self.assertEqual((batch["purpose"], batch["verdict_status"]), ("heldout", "gated"))
        self.assertEqual(formal.status(self.paths)["heldout"]["state"], "COMPLETE")
        with self.assertRaisesRegex(formal.FormalError, "does not run again"):
            self.heldout()

    def test_a_heldout_batch_sealed_for_anything_but_an_exogenous_restart_retires_the_set(self):
        self.heldout_ready()
        self.world.before_call = {2: lambda: self.assertEqual(
            formal.stop_development(self.paths)["status"], "STOP_REQUESTED")}
        head = self.heldout()
        self.assertEqual((head["status"], head["seal_reason"]), ("SEALED", "operator_stop"))
        self.assertIn("retired", formal.status(self.paths)["checks"]["heldout_chain"])
        with self.assertRaisesRegex(formal.FormalError, "retired when p74c-heldout-001"):
            self.heldout()

    def test_the_heldout_batch_runs_only_the_second_freezes_candidate(self):
        other = {**self.bindings(None)["candidate"], "agent_image_id": "sha256:" + "e" * 64}
        self.heldout_ready(candidate=other)
        with self.assertRaisesRegex(formal.FormalError, "not the second freeze's candidate"):
            self.heldout()
        self.assertEqual(formal.heldout_chain(self.paths), [])
