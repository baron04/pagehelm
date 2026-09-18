"""A zero-model rehearsal of the formal P7.4b development flow.

Preflight, batch announcement, attempts, closing, the report, the development chain rules and
resumption all run through ``formal.py`` against a temporary checkout, a temporary
``.workbench``, a temporary private directory and a loopback provider. Attempts are written to
the ledger in the driver's record order without a browser or a stack; the live attempt path is
what the P7.4a qualification exercises. No formal record is created or read and no model is
called; the test checks that the real private records, ``.workbench`` records and spend ledgers
are unchanged afterwards.
"""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from evals.odoo import formal, harness, ledger, protocol
from evals.odoo.revision import REPOSITORY
from evals.odoo.tests.test_formal import FormalFixture, answer, recovery
from evals.odoo.tests.test_freeze import git
from evals.odoo.tests.test_ledger import CLEAN_AGENT, DIGEST, OTHER, PASS, metrics

TASK_SET = json.loads(harness.DEVELOPMENT_TASK_SET.read_text())
DEVELOPMENT_SEAL = json.loads(harness.DEVELOPMENT_SEAL.read_text())


def untouchable() -> dict[str, object]:
    """Digests of everything a rehearsal must leave alone."""
    from web_operation_agent.model import DEFAULT_LEDGER

    workbench = REPOSITORY / ".workbench"
    places = {"formal_private": harness.PRIVATE / "formal",
              "freezes": workbench / formal.FREEZES, "custody": workbench / formal.CUSTODY,
              "batches": workbench / formal.BATCHES, "spend_default": Path(DEFAULT_LEDGER),
              "spend_acceptance": REPOSITORY / ".artifacts" / "acceptance" / "spend.jsonl"}
    if os.environ.get("WOA_SPEND_LEDGER"):
        places["spend_environment"] = Path(os.environ["WOA_SPEND_LEDGER"])

    def digest(path: Path) -> object:
        if not path.exists():
            return None
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
        return sorted((str(item.relative_to(path)), hashlib.sha256(item.read_bytes()).hexdigest())
                      for item in path.rglob("*") if item.is_file())

    return {name: digest(path) for name, path in places.items()}


class DevelopmentRehearsal(FormalFixture):
    def setUp(self):
        super().setUp()
        self.before = untouchable()
        self.provider(*[answer()] * 5)
        self.freeze_commit = self.freeze()
        self.custody()
        recovery(self.private)
        self.qualify_head()
        self.assertEqual(self.preflight()["status"], "READY")

    def qualify_head(self, **artifact_changes):
        """A fixture manifest taken with this checkout's candidate and a newer READY qualification
        of it; ``artifact_changes`` alter what that qualification bound."""
        candidate = self.bindings()["candidate"]
        self.manifest = {"manifest_digest": DIGEST, "fixture_id": "synthetic",
                         "candidate": candidate}
        path = harness.fixture_manifest_path(self.private)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.manifest))
        count = len(list(path.parent.glob("p74a-qualification-*.json")))
        artifact = {"status": "READY", "candidate": candidate, "fixture_manifest_digest": DIGEST,
                    "offline": {"protocol_digest": protocol.document()["protocol_digest"]},
                    "checks": {}, **artifact_changes}
        (path.parent / f"p74a-qualification-20260915T{count:06d}Z.json").write_text(
            json.dumps(artifact))

    def bindings(self):
        """What ``harness.batch_bindings`` verifies on a live stack, for this checkout's HEAD."""
        commit = git(self.checkout, "rev-parse", "HEAD")
        return {"task_set_seal_digest": DEVELOPMENT_SEAL["seal_digest"],
                "fixture": {"manifest_digest": DIGEST, "baseline_guard_digest": OTHER},
                "capability_digests": {arm: protocol.arm_capability_digest(arm)
                                       for arm in protocol.ARMS},
                "candidate": {"commit": commit, "agent_image_revision": commit,
                              "agent_image_id": "sha256:" + hashlib.sha256(
                                  commit.encode()).hexdigest()}}

    def prepare(self):
        with mock.patch.object(harness, "batch_bindings", lambda _ctx: self.bindings()):
            return formal.prepare_development(self.paths, SimpleNamespace(task_set=TASK_SET),
                                              confirm_paid_calls=True)

    def attempts(self, prepared, *, limit=None):
        """Record the remaining slots' attempts in the driver's order; ``limit`` stops early."""
        header = prepared.header
        summary = ledger.read(prepared.path)[1]
        count = written = len(summary["attempts"])
        for slot in summary["slots"]:
            if slot["attempts"]:
                continue
            if limit is not None and written - len(summary["attempts"]) >= limit:
                break
            count += 1
            written += 1
            attempt_id = f"attempt-{count:03d}"
            for kind, body in (
                    ("attempt_started", {
                        "attempt_id": attempt_id, "slot_id": slot["slot_id"], "arm": slot["arm"],
                        "task_id": slot["task_id"], "replicate": slot["replicate"],
                        "run_id": f"run-{count:03d}",
                        "fixture_digest": header["fixture"]["manifest_digest"],
                        "capability_digest": header["capability_digests"][slot["arm"]],
                        "provider_config_digest": header["provider_config_digest"],
                        "provider_binding_digest": header["provider_binding_digest"],
                        "replacement_of": None}),
                    ("attempt_outcome", {"attempt_id": attempt_id, **PASS.as_record(),
                                         "evaluation_digest": None, "truth_digest": None}),
                    ("reset_outcome", {"attempt_id": attempt_id, "outcome": "NOT_REQUIRED",
                                       "basis": [], "guard_digest": None, "agent": CLEAN_AGENT}),
                    ("attempt_cost", {"attempt_id": attempt_id, "model_calls": 0,
                                      "usage_unavailable_calls": 0, "input_tokens": 0,
                                      "output_tokens": 0, "cached_tokens": 0,
                                      "reasoning_tokens": 0, "known_cost_cny": 0.0,
                                      "total_cost_cny": 0.0}),
                    ("attempt_metrics", metrics(attempt_id)),
                    ("attempt_terminal", {"attempt_id": attempt_id})):
                ledger.append(prepared.path, kind, body)
        return harness.finish_batch(prepared.path)

    def announced(self, batch_id, name):
        return formal.committed_record(self.workbench, formal.batch_record_path(batch_id, name))

    def test_preflight_announcement_attempts_closing_and_a_gated_report(self):
        prepared = self.prepare()
        self.assertEqual((prepared.header["batch_id"], len(prepared.header["slots"])),
                         ("p74b-development-001", 60))
        self.assertEqual(prepared.header["schedule_seed"],
                         protocol.derive_schedule_seed(self.freeze_commit, purpose="development"))
        record, _ = self.announced("p74b-development-001", "header")
        self.assertEqual(record["header"], prepared.header)
        self.assertEqual(ledger.read(prepared.path)[1]["attempts"], [])

        summary = self.attempts(prepared)
        self.assertEqual(summary["status"], "COMPLETE")
        formal.close_batch(self.paths, summary)
        head, _ = self.announced("p74b-development-001", "chain-head")
        self.assertEqual((head["status"], head["chain_head"]), ("COMPLETE", summary["chain_head"]))

        (entry,) = formal.development_report(self.paths)["batches"]
        primary = entry["arms"]["woa_product"]["replicates"]["primary"]["normal"]
        self.assertEqual((entry["verdict_status"], primary["n"], primary["threshold"],
                          primary["gate_verdict"]), ("gated", 25, 23, "PASS"))
        self.assertEqual(untouchable(), self.before)

    def test_the_chain_resumes_open_batches_and_refuses_reruns(self):
        first_batch = self.prepare()
        first_summary = self.attempts(first_batch)
        formal.close_batch(self.paths, first_summary)
        with self.assertRaisesRegex(formal.FormalError, "already ran"):
            self.prepare()

        (self.checkout / "fix.txt").write_text("a product fix\n")
        git(self.checkout, "add", "fix.txt")
        git(self.checkout, "commit", "-q", "-m", "fix the product")
        # The new candidate runs only once it has been qualified itself.
        with self.assertRaisesRegex(formal.FormalError, "has not passed a zero-model"):
            self.prepare()
        self.qualify_head()
        second = self.prepare()
        link = second.header["previous_batch"]
        self.assertEqual((second.header["batch_id"], link["candidate_commit"],
                          link["termination_reason"], link["chain_head"]),
                         ("p74b-development-002", first_batch.header["candidate"]["commit"],
                          "batch_completed", first_summary["chain_head"]))
        self.assertEqual(self.attempts(second, limit=1)["status"], "OPEN")
        resumed = self.prepare()
        self.assertEqual((resumed.path, resumed.header, resumed.header_commit),
                         (second.path, second.header, second.header_commit))
        self.assertEqual(len(list(self.paths.ledgers.glob("*.jsonl"))), 2)

        value = formal.development_report(self.paths)
        self.assertEqual([(item["batch_id"], item["verdict_status"], item["superseded"])
                          for item in value["batches"]],
                         [("p74b-development-001", "descriptive_only", True),
                          ("p74b-development-002", "descriptive_only", False)])

        # The development exception does not reach held-out chains.
        heldout = {**first_batch.header, "batch_id": "p74c-heldout-002", "purpose": "heldout",
                   "candidate": second.header["candidate"],
                   "previous_batch": {**formal.link_to(first_summary), "purpose": "heldout"}}
        with self.assertRaises(ledger.LedgerError):
            ledger.create(self.root / "heldout.jsonl", heldout)
        self.assertEqual(formal.status(self.paths)["heldout"], {"state": "NOT_STARTED"})
        self.assertIn("third freeze", formal.status(self.paths)["checks"]["freezes"])
        self.assertEqual(untouchable(), self.before)

    def test_a_batch_is_announced_with_the_full_manifest_it_binds(self):
        path = harness.fixture_manifest_path(self.private)
        path.write_text(json.dumps({**self.manifest, "manifest_digest": OTHER}))
        with self.assertRaisesRegex(formal.FormalError, "not the one the batch binds"):
            self.prepare()
        self.assertEqual(list(self.paths.ledgers.glob("*")), [])
        path.write_text(json.dumps(self.manifest))
        prepared = self.prepare()
        record, _ = self.announced(prepared.header["batch_id"], "header")
        self.assertEqual(record["fixture_manifest"], self.manifest)

    def test_the_paid_entry_itself_needs_a_qualification_of_this_manifest_and_image(self):
        candidate = self.bindings()["candidate"]
        stale = {"another_manifest": {"fixture_manifest_digest": OTHER},
                 "another_image": {"candidate": {**candidate, "agent_image_id": OTHER}}}
        for label, changes in stale.items():
            with self.subTest(label=label):
                self.qualify_head(**changes)
                # A direct call, not only the command line, refuses before any batch exists.
                with self.assertRaisesRegex(formal.FormalError, "qualify again"):
                    self.prepare()
                self.assertEqual(list(self.paths.ledgers.glob("*")), [])
        self.qualify_head()
        prepared = self.prepare()
        record, _ = self.announced(prepared.header["batch_id"], "header")
        self.assertTrue(record["qualification_digest"].startswith("sha256:"))

    def test_a_changed_key_invalidates_the_preflight_before_any_batch(self):
        settings = json.loads(self.paths.provider_config.read_text())
        other = self.root / "rotated.key"
        other.write_text("sk-rotated-evaluation-key\n")
        os.chmod(other, 0o600)
        self.paths.provider_config.write_text(json.dumps({**settings, "key_file": str(other)}))
        with self.assertRaisesRegex(formal.FormalError, "run the preflight again"):
            self.prepare()
        self.assertEqual(list(self.paths.ledgers.glob("*")), [])
        self.assertFalse((self.workbench / formal.BATCHES).exists())
        self.assertEqual(untouchable(), self.before)
