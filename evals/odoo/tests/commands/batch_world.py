"""A batch run end to end without a model, Docker, Odoo or Chromium.

The real driver, gateway, provider backend, spend ledger, attempt ledger and report run here.
Only what needs a live stack stands behind fakes at the driver's own boundary: the stack, the
fixture guard and reset, the browser runner and the evaluator process. The provider is an
OpenAI-compatible server on loopback, so every call really crosses the backend that accounts
for it.
"""
from __future__ import annotations

import contextlib
import copy
import functools
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest import mock

from evals.odoo.domain import protocol
from evals.odoo.execution import driver, harness, runner_woa
from evals.odoo.fixture import fixture
from evals.odoo.judging import capture, evaluator
from evals.odoo.provider import gateway
from evals.odoo.provider.gateway import UPSTREAM_SETTLE_MARGIN_S
from evals.odoo.execution.runner_woa import ActorCredential
from evals.odoo.fixture.stack import StackError
from evals.odoo.domain.tasks import EvaluatorInput, RunnerInput

TASK_SET = json.loads(harness.DEVELOPMENT_TASK_SET.read_text())
DEVELOPMENT_SEAL = json.loads(harness.DEVELOPMENT_SEAL.read_text())
#: The held-out seal is public; the held-out tasks are not, and no test reads them. Held-out runs
#: here reuse the development tasks under the held-out seal's digest.
HELDOUT_SEAL = json.loads(harness.HELDOUT_SEAL.read_text())
DIGEST = "sha256:" + "0" * 64
MODEL = "qwen3.8-flash"
BASELINE_GUARD = {"fixture_id": fixture.FIXTURE_ID,
                  "tables": {"res_partner": {"rows": 40, "digest": "partners"}},
                  "sequences": {"ir_sequence_001": "7"},
                  "filestore": {"referenced_objects": 0, "missing_objects": [], "digest": DIGEST},
                  "excluded_volumes": {}, "digest": "sha256:" + "9" * 64}
EMPTY_AGENT = {"active_sessions": 0, "unresolved_mutations": 0, "prepared_proposals": [],
               "conversation_rows": 0}
_GIT_IDENTITY = {"GIT_AUTHOR_NAME": "batch-world", "GIT_AUTHOR_EMAIL": "batch@world.test",
                 "GIT_COMMITTER_NAME": "batch-world", "GIT_COMMITTER_EMAIL": "batch@world.test"}


class DriverDied(BaseException):
    """Stands for a driver process killed mid-attempt: nothing after it runs."""


def post(request) -> bool:
    """Send one product call through the gateway; whether it was answered."""
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def git(repository: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **_GIT_IDENTITY}).stdout.strip()


class Provider:
    """An OpenAI-compatible provider on loopback that answers each product call, with the fault
    scheduled for its number."""

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
        with self.lock:
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
                          "content": "done"}}],
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


class BatchWorld(unittest.TestCase):
    """A clean checkout, a qualified candidate built from it, a configured provider, and the
    driver's boundary faked. ``self.paths`` is what ``evals.odoo.batch`` runs against."""

    def setUp(self):
        from evals.odoo import batch

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q")
        (self.checkout / "README").write_text("candidate\n")
        git(self.checkout, "add", "README")
        git(self.checkout, "commit", "-q", "-m", "candidate")
        self.commit = git(self.checkout, "rev-parse", "HEAD")
        self.private = self.root / "private"
        self.paths = batch.BatchPaths(repository=self.checkout, private_dir=self.private,
                                      spend_ledger=self.root / "spend" / "model-spend.jsonl")
        (self.root / "spend").mkdir()
        self.world = world = World()
        self.stack = Stack(world)
        self.provider = Provider(self)
        key = self.root / "evaluation.key"
        key.write_text("sk-synthetic-evaluation-key\n")
        os.chmod(key, 0o600)
        self.paths.provider_config.parent.mkdir(parents=True, exist_ok=True)
        self.paths.provider_config.write_text(json.dumps(
            {"base_url": self.provider.base_url, "key_file": str(key)}))
        self.qualify(self.candidate())
        # The fixture's recovery record: its baseline, and that resets may run on it.
        record = fixture._seal({
            "artifact_version": 1, "kind": "p74_fixture_recovery", "status": "READY",
            "fixture_id": fixture.FIXTURE_ID, "project": "woa-p74-odoo-v1",
            "database": "woa_trial", "database_archive": {"sha256": DIGEST, "bytes": 1},
            "filestore_archive": {"sha256": DIGEST, "bytes": 1},
            "baseline_guard": BASELINE_GUARD, "baseline_digest": BASELINE_GUARD["digest"],
            "actor_projection_digest": DIGEST})
        (self.private / "recovery").mkdir(parents=True, exist_ok=True)
        (self.private / "recovery" / "recovery.json").write_text(json.dumps(record))
        # The manifest of the fixture the faked stack stands for; the driver binds its digest.
        manifest = harness.fixture_manifest_path(self.private)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"manifest_digest": DIGEST}))
        # A held-out run reads the development tasks under the held-out seal, and schedules WOA
        # only: the browser-only arm needs a real browser this world does not have.
        def split(paths, population):
            seal = HELDOUT_SEAL if population == "heldout" else DEVELOPMENT_SEAL
            return TASK_SET, seal
        for patch in (mock.patch.object(batch, "task_set", split),
                      mock.patch.object(protocol, "pageagent_eligibility", lambda _tasks: [])):
            patch.start()
            self.addCleanup(patch.stop)
        self.bundle = self.root / "pageagent-baseline.js"
        self.bundle.write_text("// never loaded here\n")

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

    # -- the candidate and its qualification ------------------------------------------------
    def candidate(self, commit: str | None = None) -> dict[str, str]:
        commit = commit or self.commit
        return {"commit": commit, "agent_image_revision": commit,
                "agent_image_id": "sha256:" + hashlib.sha256(commit.encode()).hexdigest()}

    def qualify(self, candidate: dict[str, str], *, status: str = "READY",
                manifest: str = DIGEST, name: str = "p74a-qualification-20260918T000000Z.json"):
        target = self.private / "qualification" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "status": status, "candidate": candidate, "fixture_manifest_digest": manifest,
            "checks": {"submit_feedback_within_300ms": True,
                       "cancel_feedback_within_300ms": True}}))

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
        seal = (HELDOUT_SEAL if getattr(ctx, "population_name", "") == "heldout"
                else DEVELOPMENT_SEAL)
        return {"task_set_seal_digest": seal["seal_digest"],
                "fixture": {"manifest_digest": DIGEST,
                            "baseline_guard_digest": BASELINE_GUARD["digest"]},
                "capability_digests": {arm: protocol.arm_capability_digest(arm)
                                       for arm in protocol.ARMS},
                "candidate": self.candidate(git(self.checkout, "rev-parse", "HEAD"))}

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
                    **protocol.PROVIDER["request_parameters"]["model_options"],
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
