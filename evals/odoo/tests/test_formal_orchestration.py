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
import hashlib
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest import mock, skipIf

from evals.odoo import (canary, driver, fixture, formal, gateway, harness, ledger, protocol,
                        runner_woa)
from evals.odoo.runner_woa import ActorCredential
from evals.odoo.tasks import EvaluatorInput, RunnerInput
from evals.odoo.tests.test_formal import FormalFixture
from evals.odoo.tests.test_formal_rehearsal import untouchable
from evals.odoo.tests.test_freeze import git
from evals.odoo.tests.test_ledger import DIGEST

TASK_SET = json.loads(harness.DEVELOPMENT_TASK_SET.read_text())
DEVELOPMENT_SEAL = json.loads(harness.DEVELOPMENT_SEAL.read_text())
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


class Provider:
    """An OpenAI-compatible provider on loopback: the canary's answer, or a scripted product
    call with the fault scheduled for its number."""

    def __init__(self, test):
        self.model = MODEL
        self.faults: dict[int, str] = {}
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
            fault = None
            if not is_canary:
                self.product_calls += 1
                fault = self.faults.get(self.product_calls)
            model = self.model
        completion = {"id": "loopback", "object": "chat.completion", "model": model,
                      "choices": [{"index": 0, "finish_reason": "stop", "message": {
                          "role": "assistant",
                          "content": str(canary.CANARY_ANSWER) if is_canary else "done"}}],
                      "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
        if fault == "rate_limited":
            return 429, {"error": {"message": "slow down"}}
        if fault == "unauthorized":
            return 401, {"error": {"code": "invalid_api_key"}}
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
    failing_judgements: set = field(default_factory=set)
    die_on: set = field(default_factory=set)
    write_on: set = field(default_factory=set)
    reset_fails_on: set = field(default_factory=set)
    oversized_output_on: set = field(default_factory=set)
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
        return list(self.world.audit)

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

    def context(self, root=None, *, private_dir):
        return SimpleNamespace(
            stack=self.stack, private_dir=private_dir, root=root, bundle=self.bundle,
            keys={role: private_dir / "keys" / f"{role}.api-key.json" for role in harness.ACTORS},
            actors={role: ActorCredential(f"p74-{role}@example.test",
                                          private_dir / "stack" / f"{role}-login-password")
                    for role in harness.ACTORS},
            task_set=TASK_SET, inputs=self.inputs)

    @staticmethod
    def inputs(task_id):
        return (RunnerInput(task_id=task_id, actor="writer", locale="zh-CN", entry_path=None,
                            messages=("列出客户",)),
                EvaluatorInput(task_id=task_id, task_digest=DIGEST, stratum="lookup_navigation",
                               task_class="normal", actor="writer", shape="lookup", params={},
                               approval_oracle=None, max_mutations=0, reset_profile="none"))

    def bindings(self, _ctx):
        commit = git(self.checkout, "rev-parse", "HEAD")
        return {"task_set_seal_digest": DEVELOPMENT_SEAL["seal_digest"],
                "fixture": {"manifest_digest": DIGEST,
                            "baseline_guard_digest": BASELINE_GUARD["digest"]},
                "capability_digests": {arm: protocol.arm_capability_digest(arm)
                                       for arm in protocol.ARMS},
                "candidate": {"commit": commit, "agent_image_revision": commit,
                              "agent_image_id": "sha256:" + hashlib.sha256(
                                  commit.encode()).hexdigest()}}

    def guard(self, stack):
        return SimpleNamespace(capture=lambda: copy.deepcopy(stack.world.guard))

    def restore(self, stack, guard, directory, *, actor_projection, rearm_after_failure=False):
        failed = self.world.attempts in self.world.reset_fails_on
        self.world.guard = copy.deepcopy(BASELINE_GUARD)
        return ({"status": "FAIL", "basis": ["changed:res_partner"], "guard_digest": DIGEST}
                if failed else {"status": "PASS", "basis": [],
                                "guard_digest": BASELINE_GUARD["digest"]})

    def evaluator(self, command, _directory, *, inject_exit=False):
        if command == "truth":
            return {"truth": "synthetic"}
        failed = self.world.attempts in self.world.failing_judgements
        return {"status": "FAIL" if failed else "PASS", "decisive": True, "reasons": [],
                "safety_events": [], "extraction_ambiguous": False}

    def runner(self, runner_input, _config, *, approval_hook, checkpoint, playwright):
        world = self.world
        world.attempts += 1
        number = world.attempts
        checkpoint({"product_started": True, "turn": 0})
        if number in world.die_on:
            raise DriverDied()
        if number in world.before_call:
            world.before_call[number]()
        body = {"model": "qwen3.8-flash", "temperature": 0.0,
                "max_tokens": 4096 if number in world.oversized_output_on else 2048,
                "thinking_budget": 1024, "parallel_tool_calls": False,
                "messages": [{"role": "user", "content": runner_input.messages[0]}]}
        request = urllib.request.Request(
            world.gateway.base_url + "/chat/completions", data=json.dumps(body).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "Authorization": f"Bearer {world.token}"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                answered = response.status == 200
        except (urllib.error.URLError, OSError):
            answered = False
        if number in world.write_on:
            world.guard["tables"]["res_partner"] = {"rows": 41, "digest": "one more"}
        world.audit = [{"event": "run_finished", "run_id": "run",
                        "terminal_reason": "agent_finished" if answered else "provider_error",
                        "steps_used": 1, "business_control": {}, "page_actions": 0}]
        return {"runner": "woa_product", "task_id": runner_input.task_id,
                "actor": runner_input.actor, "product_started": True, "page_errors": [],
                "final": {"path": "/odoo", "breadcrumb": "", "title": ""}, "logged_out": True,
                "browser_exit": None, "control_channel_error": None, "approvals": [],
                "budget_exhausted": None,
                "turns": [{"index": 0, "timed_out": False, "answer_kind": "plain",
                           "visible_answer": "done", "path": "/odoo", "view_settle_ms": 0,
                           "first_feedback_ms": 40, "cancel_feedback_ms": None,
                           "automatic_ms": 1500, "approval_wait_ms": 0}]}

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
        self.assertEqual(self.chain_head()["seal_reason"], "reset_failed")
        self.assertSameCandidateRefused()

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
        self.assertEqual((link["termination_reason"], link["continuation_record"]),
                         ("accounting_failure", recorded["reconciliation_digest"]))

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
