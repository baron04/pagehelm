"""The formal entry points, against temporary repositories and a local fake provider only."""
import dataclasses
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from evals.odoo import canary, escrow, fixture, formal, freeze, harness, ledger, protocol
from evals.odoo.revision import REPOSITORY, RevisionError, committed_once
from evals.odoo.tests.test_freeze import CandidateRepository, external, first, git, review
from evals.odoo.tests.test_ledger import CANDIDATE, DIGEST, NEW_CANDIDATE, OTHER, header, slots
from evals.odoo.tests.test_escrow import separate_domains
from evals.odoo.tests.test_upstream import FakeProvider, completion

ESCROWED = {
    "heldout/p74-odoo-heldout-v1.json": b'{"set_id": "synthetic-heldout", "tasks": []}\n',
    "seeds/development.seed": b"00112233445566778899aabbccddeeff\n",
    "seeds/heldout.seed": b"ffeeddccbbaa99887766554433221100\n",
}
USAGE = {"prompt_tokens": 20, "completion_tokens": 2}
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "p74", "GIT_AUTHOR_EMAIL": "p74@example.test",
                "GIT_COMMITTER_NAME": "p74", "GIT_COMMITTER_EMAIL": "p74@example.test"}
CALL_FIELDS = {"provider_request_id", "status", "http_status", "response_model", "usage",
               "cost_cny", "error_code"}


def answer(model="qwen3.8-flash"):
    return (200, json.dumps(completion(model=model, usage=USAGE)).encode(), 0)


def repository(root):
    root.mkdir(parents=True)
    git(root, "init", "-q")
    (root / "README").write_text("synthetic\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial")
    return root


def apparatus_repository(root):
    """A checkout holding a committed copy of every file an external review binds."""
    root.mkdir(parents=True)
    for relative in freeze.reviewed_files():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / relative, root / relative)
    (root / "README").write_text("synthetic\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial")
    return root


def recovery(private, status="READY"):
    """A sealed fixture recovery record in the state ``status``; no fixture stands behind it."""
    record = fixture._seal({
        "artifact_version": 1, "kind": "p74_fixture_recovery", "status": status,
        "fixture_id": fixture.FIXTURE_ID, "project": "woa-p74-odoo-v1", "database": "woa_trial",
        "database_archive": {"sha256": DIGEST, "bytes": 1},
        "filestore_archive": {"sha256": DIGEST, "bytes": 1},
        "baseline_guard": {"digest": OTHER}, "baseline_digest": OTHER,
        "actor_projection_digest": DIGEST})
    (private / "recovery").mkdir(parents=True, exist_ok=True)
    (private / "recovery" / "recovery.json").write_text(json.dumps(record))


@unittest.skipIf(shutil.which("openssl") is None, "openssl is not installed")
class FormalFixture(unittest.TestCase):
    def setUp(self):
        identity = mock.patch.dict(os.environ, GIT_IDENTITY)
        identity.start()
        self.addCleanup(identity.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workbench = repository(self.root / "workbench")
        self.checkout = apparatus_repository(self.root / "checkout")
        self.private = self.root / "private"
        for name, data in ESCROWED.items():
            (self.private / name).parent.mkdir(parents=True, exist_ok=True)
            (self.private / name).write_bytes(data)
        self.paths = formal.FormalPaths(repository=self.checkout, private_dir=self.private,
                                        workbench=self.workbench,
                                        spend_ledger=self.root / "spend" / "model-spend.jsonl")

    def provider(self, *responses):
        provider = FakeProvider(self, responses)
        self.key = self.root / "evaluation.key"
        self.key.write_text("sk-synthetic-evaluation-key\n")
        os.chmod(self.key, 0o600)
        self.paths.provider_config.parent.mkdir(parents=True, exist_ok=True)
        self.paths.provider_config.write_text(json.dumps(
            {"base_url": provider.base_url, "key_file": str(self.key)}))
        return provider

    def custody(self):
        archive, secret = self.root / "vault" / "p74.escrow", self.root / "keys" / "p74.secret"
        escrow.create(self.private, archive, secret)
        record = escrow.custody_record(private_dir=self.private, archive=archive, secret=secret,
                                       probe=separate_domains)
        return self.record(formal.CUSTODY, record)

    def record(self, relative, value):
        path = self.workbench / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        git(self.workbench, "add", "--", relative)
        git(self.workbench, "commit", "-q", "-m", f"record {path.name}")
        return git(self.workbench, "rev-parse", "HEAD")

    def freeze(self):
        """An accepted external review of this checkout, the family review and the first freeze
        binding both, each committed once; returns the commit that recorded the first freeze."""
        self.external_review = external(reviewed_commit=git(self.checkout, "rev-parse", "HEAD"))
        self.record(formal.external_review_path(self.external_review), self.external_review)
        self.record(f"{formal.FREEZES}/family-review.json", review())
        self.first_freeze = first(external_review=self.external_review)
        return self.record(f"{formal.FREEZES}/first-freeze.json", self.first_freeze)

    def preflight(self, at="2026-09-15T08:00:00Z"):
        return formal.run_preflight(self.paths, confirm_paid_calls=True, clock=lambda: at)


class PreflightTests(FormalFixture):
    def assertRefused(self, provider, pattern):
        with self.assertRaisesRegex(formal.FormalError, pattern):
            self.preflight()
        self.assertEqual(provider.requests, [])
        self.assertFalse(self.paths.spend().exists())

    def test_no_canary_goes_out_beside_a_running_development_process(self):
        """The review of f85c1c1: the preflight read the chain once and never took the lock a
        development batch holds, so five canaries could go out beside a running batch and a cost
        that batch left unknown partway through reached none of them."""
        provider = self.provider(*[answer()] * 5)
        self.freeze()
        self.custody()
        with formal.development_lock(self.paths):
            self.assertTrue(formal.development_running(self.paths))
            self.assertRefused(provider, "another development process")
        # The lock is the only thing that was in the way.
        self.assertEqual(self.preflight()["status"], "READY")

    def test_no_canary_goes_out_while_an_attempt_of_the_newest_batch_is_open(self):
        """The review of 8c4025d: a driver killed mid-attempt leaves a dispatched call that is
        neither an accounting error nor yet unresolved -- the recovery writes it down -- so the
        reconciliation gate saw no debt and five paid canaries went out before it existed."""
        provider = self.provider(*[answer()] * 5)
        self.freeze()
        self.custody()
        path = self.paths.ledgers / "p74b-development-001.jsonl"
        ledger.create(path, header(slots(1), batch_id="p74b-development-001"))
        ledger.append(path, "attempt_started", {
            "attempt_id": "attempt-001", "slot_id": "s000", "arm": "woa_product",
            "task_id": "task-000", "replicate": "primary", "run_id": "run-001",
            "fixture_digest": DIGEST, "capability_digest": DIGEST,
            "provider_config_digest": DIGEST, "provider_binding_digest": DIGEST,
            "replacement_of": None})
        # The call the dead driver dispatched is on the ledger; nothing has settled it yet, so
        # `unaccounted_calls` sees no debt at all.
        ledger.append(path, "model_request_dispatched", {
            "attempt_id": "attempt-001", "request_seq": 1, "body_digest": DIGEST,
            "requested_model": "qwen3.8-flash", "max_output_tokens": 2048,
            "parameters": {"temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto",
                           "tools": 0, "options": {"thinking_budget": 1024}}})
        self.assertEqual(formal.unaccounted_calls(self.paths), {})
        self.assertEqual(formal.status(self.paths)["checks"]["billing_reconciliation"], "READY")
        self.assertRefused(provider, "still open")

    def test_a_preflight_that_wrote_no_record_still_owes_the_calls_it_sent(self):
        """The review of 96f399c: a preflight wrote what it sent only once its last canary had
        answered, and the debt gate derived the preflight half of the bill from those records
        alone. A process killed -- or a disk filled -- between the first paid call and that
        write left calls nobody could see, and the next paid call went out as if nothing were
        owed. A batch never has that window: it writes each dispatch down before the call
        leaves."""
        journal = self.paths.preflights / "p74b-preflight-20260101T000000Z.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal.write_text(
            '{"call_index": 1, "kind": "dispatched"}\n'
            '{"call_index": 1, "kind": "settled", "provider_request_id": "a", "status": "ok"}\n'
            '{"call_index": 2, "kind": "dispatched"}\n', encoding="utf-8")
        # The second call left and never settled: nobody knows what it cost, so the bill says.
        self.assertEqual(formal.unaccounted_calls(self.paths),
                         {"p74b-preflight-20260101T000000Z": [
                             "p74b-preflight-20260101T000000Z#2"]})
        self.assertTrue(formal.status(self.paths)["checks"][
            "billing_reconciliation"].startswith("refused"))
        # Once the record beside it exists, the record answers and the journal is not read
        # twice: this one settled every call it made.
        (self.paths.preflights / "p74b-preflight-20260101T000000Z.json").write_text(
            json.dumps({"spend_batch_id": "p74b-preflight-20260101T000000Z",
                        "calls": [{"provider_request_id": "a", "status": "ok"},
                                  {"provider_request_id": "b", "status": "ok"}]}),
            encoding="utf-8")
        self.assertEqual(formal.unaccounted_calls(self.paths), {})

    def test_nothing_is_sent_until_every_prerequisite_holds(self):
        provider = self.provider(*[answer()] * 5)
        with self.assertRaisesRegex(formal.FormalError, "confirm-paid-calls"):
            formal.run_preflight(self.paths, confirm_paid_calls=False)
        self.assertRefused(provider, "family-review")
        self.freeze()
        freeze_file = self.workbench / formal.FREEZES / "first-freeze.json"
        freeze_file.write_text(freeze_file.read_text().replace('"kind"', '"kind" ', 1))
        self.assertRefused(provider, "first-freeze")
        git(self.workbench, "checkout", "--", f"{formal.FREEZES}/first-freeze.json")
        self.assertRefused(provider, "escrow-custody")
        self.custody()
        seed = self.private / "seeds" / "heldout.seed"
        seed.write_bytes(b"changed after the escrow\n")
        self.assertRefused(provider, "custody does not verify")
        seed.write_bytes(ESCROWED["seeds/heldout.seed"])
        (self.checkout / "notes.txt").write_text("uncommitted\n")
        self.assertRefused(provider, "clean checkout")
        (self.checkout / "notes.txt").unlink()
        if os.geteuid() != 0:
            ledger_dir = self.paths.spend().parent
            ledger_dir.mkdir(parents=True)
            os.chmod(ledger_dir, 0o500)
            self.assertRefused(provider, "spend ledger")
            os.chmod(ledger_dir, 0o700)
        os.chmod(self.key, 0o644)
        self.assertRefused(provider, "evaluation key")
        os.chmod(self.key, 0o600)
        self.assertEqual(self.preflight()["status"], "READY")
        self.assertEqual(len(provider.requests), 5)

    def test_a_preflight_sends_five_contract_canaries_and_keeps_only_content_free_facts(self):
        provider = self.provider(*[answer()] * 5)
        self.freeze()
        self.custody()
        record = self.preflight()
        self.assertEqual((record["status"], record["spend_batch_id"],
                          record["usage_unavailable_calls"]),
                         ("READY", "p74b-preflight-20260915T080000Z", 0))
        expected = canary.canary_request(harness.protocol_budgets())
        self.assertEqual([item["body"] for item in provider.requests], [expected] * 5)
        rows = [json.loads(line) for line in self.paths.spend().read_text().splitlines()]
        self.assertEqual([row["logical_run_id"] for row in rows],
                         ["p74b-preflight-20260915T080000Z"] * 5)
        stored_path = self.paths.preflights / "p74b-preflight-20260915T080000Z.json"
        self.assertEqual(os.stat(stored_path).st_mode & 0o777, 0o600)
        stored = json.loads(stored_path.read_text())
        self.assertEqual(stored, record)
        self.assertTrue(all(set(call) == CALL_FIELDS for call in stored["calls"]))
        self.assertNotIn(canary.CANARY_PROMPT, json.dumps(stored))
        self.assertNotIn("sk-synthetic-evaluation-key", json.dumps(stored))
        self.assertEqual(stored["provider_binding"]["requested_model"], "qwen3.8-flash")
        self.assertEqual(formal.verified_preflight(self.paths, self.first_freeze), record)

    def test_a_changed_provider_endpoint_or_key_needs_a_new_preflight(self):
        self.provider(*[answer()] * 5)
        self.freeze()
        self.custody()
        record = self.preflight()
        settings = json.loads(self.paths.provider_config.read_text())
        other_key = self.root / "other.key"
        other_key.write_text("sk-another-evaluation-key\n")
        os.chmod(other_key, 0o600)
        other_provider = FakeProvider(self, ())
        variants = {
            "endpoint_and_key": {"base_url": other_provider.base_url, "key_file": str(other_key)},
            "key_only": {**settings, "key_file": str(other_key)},
            "endpoint_only": {**settings, "base_url": other_provider.base_url},
        }
        for label, value in variants.items():
            with self.subTest(label=label):
                self.paths.provider_config.write_text(json.dumps(value))
                with self.assertRaisesRegex(formal.FormalError, "run the preflight again"):
                    formal.verified_preflight(self.paths, self.first_freeze)
        self.paths.provider_config.write_text(json.dumps(
            {**settings, "base_url": settings["base_url"].replace("http://", "HTTP://") + "/"}))
        self.assertEqual(formal.verified_preflight(self.paths, self.first_freeze), record)
        self.assertEqual(other_provider.requests, [])

    def test_only_the_newest_preflight_counts_and_it_must_have_passed_unchanged(self):
        self.provider(*[answer()] * 4, answer("qwen3.8-flash-drifted"), *[answer()] * 5)
        self.freeze()
        self.custody()
        failed = self.preflight()
        self.assertEqual(failed["status"], "FAILED")
        self.assertFalse(failed["canary"]["checks"]["model_string_5_of_5"])
        with self.assertRaisesRegex(formal.FormalError, "failed"):
            formal.verified_preflight(self.paths, self.first_freeze)
        ready = self.preflight("2026-09-15T09:00:00Z")
        self.assertEqual(formal.verified_preflight(self.paths, self.first_freeze), ready)
        with self.assertRaisesRegex(formal.FormalError, "another first freeze"):
            formal.verified_preflight(self.paths, first(external_review=self.external_review,
                                                        reviewed_at="2026-09-16T08:00:00Z"))
        stored = self.paths.preflights / f"{ready['spend_batch_id']}.json"
        stored.write_text(json.dumps({**ready, "calls": []}))
        with self.assertRaisesRegex(formal.FormalError, "changed"):
            formal.verified_preflight(self.paths, self.first_freeze)

    def test_a_canary_the_spend_ledger_cannot_record_stops_the_preflight_at_once(self):
        from web_operation_agent.model import SpendMeter

        provider = self.provider(*[answer()] * 5)
        self.freeze()
        self.custody()
        original, written = SpendMeter._append_record, []

        def append(meter, rec, **options):
            written.append(rec)
            if len(written) == 2:
                raise OSError("the spend ledger's disk is full")
            return original(meter, rec, **options)

        with mock.patch.object(SpendMeter, "_append_record", append):
            record = self.preflight()
        self.assertEqual((record["status"], record["canary"]["stopped"], len(provider.requests)),
                         ("FAILED", "accounting_failure", 2))
        self.assertEqual([call["status"] for call in record["calls"]], ["ok", "accounting_error"])


class DevelopmentTests(FormalFixture):
    def bindings(self, record):
        return {"task_set_seal_digest": record["development_seal_digest"],
                "fixture": {"manifest_digest": DIGEST, "baseline_guard_digest": OTHER},
                "capability_digests": {arm: protocol.arm_capability_digest(arm)
                                       for arm in protocol.ARMS},
                "candidate": CANDIDATE}

    def test_the_header_derives_its_schedule_from_the_first_freeze_commit(self):
        record = first()
        ids = formal.task_ids(json.loads(harness.DEVELOPMENT_TASK_SET.read_text()))
        arguments = {"batch_id": "p74b-development-001", "first_freeze": record,
                     "bindings": self.bindings(record), "task_ids": ids, "previous": None,
                     "binding_digest": OTHER, "preflight_model": "qwen3.8-flash"}
        value = formal.development_header(first_freeze_commit="e" * 40, **arguments)
        self.assertEqual(value["schedule_seed"],
                         protocol.derive_schedule_seed("e" * 40, purpose="development"))
        self.assertEqual((len(value["slots"]), {slot["arm"] for slot in value["slots"]}),
                         (60, {"woa_product"}))
        summary = ledger.create(self.root / "development.jsonl", value)
        self.assertEqual(summary["batch"]["candidate"], CANDIDATE)
        plan = formal.development_plan(first_freeze=record, first_freeze_commit="e" * 40,
                                       header=value, task_ids=ids, binding_digest=OTHER)
        self.assertEqual((plan["schedule_digest"], plan["candidate_commit"],
                          plan["fixture_manifest_digest"], plan["protocol_digest"],
                          plan["provider_binding_digest"]),
                         (value["schedule_digest"], CANDIDATE["commit"], DIGEST,
                          value["protocol_digest"], value["provider_binding_digest"]))
        other = formal.development_header(first_freeze_commit="f" * 40, **arguments)
        self.assertNotEqual(other["schedule_digest"], value["schedule_digest"])

    def test_no_batch_starts_before_a_passing_preflight_and_the_stack_is_untouched(self):
        provider = self.provider()
        self.freeze()
        self.custody()

        class Untouchable:
            def __getattr__(self, name):
                raise AssertionError("the stack was used before the preflight was verified")

        ctx = SimpleNamespace(stack=Untouchable(), task_set={"tasks": []})
        with self.assertRaisesRegex(formal.FormalError, "no provider preflight"):
            formal.prepare_development(self.paths, ctx, confirm_paid_calls=True)
        self.assertEqual(list(self.paths.ledgers.glob("*")), [])
        self.assertEqual(provider.requests, [])

    def test_development_ledgers_must_form_one_chain(self):
        path = self.paths.ledgers / "p74b-development-001.jsonl"
        ledger.create(path, header(slots(1), batch_id="p74b-development-001"))
        sealed = ledger.append(path, "batch_sealed", {"reason": "operator_stop"})
        ledger.create(self.paths.ledgers / "p74b-development-002.jsonl",
                      header(slots(1), batch_id="p74b-development-002", candidate=NEW_CANDIDATE,
                             previous=formal.link_to(sealed)))
        self.assertEqual([item[2]["batch"]["batch_id"]
                          for item in formal.development_chain(self.paths)],
                         ["p74b-development-001", "p74b-development-002"])
        ledger.create(self.paths.ledgers / "p74b-development-003.jsonl",
                      header(slots(1), batch_id="p74b-development-003"))
        with self.assertRaisesRegex(formal.FormalError, "chain"):
            formal.development_chain(self.paths)

    def test_a_candidate_runs_once_and_never_returns(self):
        first_path = self.paths.ledgers / "p74b-development-001.jsonl"
        ledger.create(first_path, header(slots(1), batch_id="p74b-development-001"))
        exogenous = ledger.append(first_path, "batch_sealed", {"reason": "model_identity_changed"})
        chain = formal.development_chain(self.paths)
        formal.check_candidate_may_run(chain, CANDIDATE["commit"])
        formal.check_candidate_may_run(chain, NEW_CANDIDATE["commit"])
        second_path = self.paths.ledgers / "p74b-development-002.jsonl"
        ledger.create(second_path, header(slots(1), batch_id="p74b-development-002",
                                          candidate=NEW_CANDIDATE,
                                          previous=formal.link_to(exogenous)))
        ledger.append(second_path, "batch_sealed", {"reason": "operator_stop"})
        chain = formal.development_chain(self.paths)
        with self.assertRaisesRegex(formal.FormalError, "new candidate"):
            formal.check_candidate_may_run(chain, NEW_CANDIDATE["commit"])
        with self.assertRaisesRegex(formal.FormalError, "never returns"):
            formal.check_candidate_may_run(chain, CANDIDATE["commit"])
        formal.check_candidate_may_run(chain, "d" * 40)

    def test_a_batch_sealed_on_a_refused_credential_needs_a_later_preflight(self):
        self.provider(*[answer()] * 10)
        self.freeze()
        self.custody()
        self.preflight("2000-01-01T00:00:00Z")
        path = self.paths.ledgers / "p74b-development-001.jsonl"
        ledger.create(path, header(slots(1), batch_id="p74b-development-001"))
        ledger.append(path, "batch_sealed", {"reason": "provider_authentication_failed"})
        with self.assertRaisesRegex(formal.FormalError, "refused the evaluation credential"):
            formal.preflight_for_next_batch(self.paths, self.first_freeze)
        self.assertIn("refused the evaluation credential",
                      formal.status(self.paths)["checks"]["preflight"])
        later = datetime.now(timezone.utc) + timedelta(minutes=1)
        self.preflight(later.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(
            formal.preflight_for_next_batch(self.paths, self.first_freeze)["status"], "READY")

    def test_a_batch_record_is_committed_once_and_other_staged_work_is_left_alone(self):
        relative = f"{formal.BATCHES}/p74b-development-001/header.json"
        announced = {"header": {"batch_id": "p74b-development-001"}}
        commit = formal.commit_record(self.workbench, relative, announced, "announce")
        self.assertEqual(committed_once(self.workbench, relative), commit)
        self.assertEqual(formal.commit_record(self.workbench, relative, announced, "announce"),
                         commit)
        with self.assertRaisesRegex(formal.FormalError, "something else"):
            formal.commit_record(self.workbench, relative, {"header": {}}, "announce")
        (self.workbench / "notes.md").write_text("draft\n")
        git(self.workbench, "add", "notes.md")
        formal.commit_record(self.workbench, f"{formal.BATCHES}/p74b-development-001/"
                             "chain-head.json", {"status": "COMPLETE"}, "close")
        self.assertEqual(git(self.workbench, "diff", "--cached", "--name-only"), "notes.md")
        (self.workbench / relative).write_text("{}\n")
        with self.assertRaises(RevisionError):
            committed_once(self.workbench, relative)


class QualificationFeedbackTests(FormalFixture):
    def test_the_newest_ready_qualification_of_the_candidate_supplies_both_feedback_checks(self):
        root = self.private / "qualification"
        root.mkdir(parents=True)

        def artifact(stamp, **changes):
            record = {"status": "READY", "candidate": {"commit": CANDIDATE["commit"]},
                      "checks": {"submit_feedback_within_300ms": True,
                                 "cancel_feedback_within_300ms": True}, **changes}
            (root / f"p74a-qualification-{stamp}.json").write_text(json.dumps(record))

        self.assertIsNone(formal.qualification_feedback(self.paths, CANDIDATE["commit"]))
        artifact("20260901T000000Z")
        artifact("20260902T000000Z", checks={"submit_feedback_within_300ms": True})
        artifact("20260903T000000Z", status="FAILED")
        artifact("20260904T000000Z", candidate={"commit": NEW_CANDIDATE["commit"]})
        value = formal.qualification_feedback(self.paths, CANDIDATE["commit"])
        self.assertEqual((value["submit_within_300ms"], value["cancel_within_300ms"]),
                         (True, False))
        self.assertTrue(value["qualification_digest"].startswith("sha256:"))


class StatusTests(FormalFixture):
    def test_status_names_what_is_missing_without_sending_anything(self):
        provider = self.provider()
        value = formal.status(self.paths)
        self.assertEqual((value["preflight_can_start"], value["development_can_start"]),
                         (False, False))
        self.assertEqual((value["checks"]["clean_checkout"],
                          value["checks"]["provider_configuration"]), ("READY", "READY"))
        for name in ("first_freeze", "escrow_custody", "preflight", "qualification",
                     "fixture_recovery"):
            self.assertTrue(value["checks"][name].startswith("refused"), name)
        self.assertEqual((value["checks"]["batch_records"], value["batches"],
                          value["development_running"]), ("READY", [], False))
        self.freeze()
        self.custody()
        value = formal.status(self.paths)
        self.assertEqual((value["preflight_can_start"], value["development_can_start"]),
                         (True, False))
        self.assertTrue(value["runtime_binding_digest"].startswith("sha256:"))
        self.assertEqual(provider.requests, [])
        self.assertFalse(self.paths.spend().exists())


class LifecycleTests(FormalFixture):
    def open_batch(self, slot_count=2, **changes):
        path = self.paths.ledgers / "p74b-development-001.jsonl"
        ledger.create(path, header(slots(slot_count), batch_id="p74b-development-001",
                                   **changes))
        return path

    def test_one_development_process_holds_the_ledgers_at_a_time(self):
        self.assertFalse(formal.development_running(self.paths))
        with formal.development_lock(self.paths):
            self.assertTrue(formal.development_running(self.paths))
            with self.assertRaisesRegex(formal.FormalError, "another development process"):
                with formal.development_lock(self.paths):
                    pass
        self.assertFalse(formal.development_running(self.paths))

    def test_close_commits_every_missing_chain_head_and_status_names_the_gap(self):
        summary = ledger.append(self.open_batch(), "batch_sealed", {"reason": "operator_stop"})
        self.assertEqual([(row["status"], row["chain_head_committed"])
                          for row in formal.batch_records(self.paths)], [("SEALED", False)])
        self.assertIn("run close", formal.status(self.paths)["checks"]["batch_records"])
        (closed,) = formal.close_finished_batches(self.paths)
        self.assertEqual(closed["chain_head"], summary["chain_head"])
        self.assertEqual(formal.status(self.paths)["checks"]["batch_records"], "READY")
        self.assertEqual(formal.close_finished_batches(self.paths), [closed])
        with formal.development_lock(self.paths), self.assertRaisesRegex(
                formal.FormalError, "development process is running"):
            formal.close_finished_batches(self.paths)

    def test_stop_asks_a_running_process_and_otherwise_seals_the_open_batch_at_once(self):
        self.open_batch()
        with formal.development_lock(self.paths):
            self.assertEqual(formal.stop_development(self.paths),
                             {"status": "STOP_REQUESTED", "batch_id": "p74b-development-001"})
            self.assertTrue(formal.stop_requested(self.paths.stop_request,
                                                  "p74b-development-001"))
            self.assertFalse(formal.stop_requested(self.paths.stop_request,
                                                   "p74b-development-002"))
            self.assertEqual(formal.status(self.paths)["stop_requested_for"],
                             "p74b-development-001")
        value = formal.stop_development(self.paths)
        self.assertEqual((value["status"], value["seal_reason"]), ("SEALED", "operator_stop"))
        self.assertFalse(self.paths.stop_request.exists())
        record, _ = formal.committed_record(
            self.workbench, formal.batch_record_path("p74b-development-001", "chain-head"))
        self.assertEqual(record["seal_reason"], "operator_stop")
        with self.assertRaisesRegex(formal.FormalError, "no development or held-out batch"):
            formal.stop_development(self.paths)

    def test_a_batch_that_owes_a_seal_is_stopped_for_that_seal(self):
        ledger.append(self.open_batch(), "unowned_model_request",
                      {"body_digest": DIGEST, "reason": "no_open_attempt"})
        self.assertEqual(formal.stop_development(self.paths)["seal_reason"],
                         "unowned_model_request")

    def test_the_fixture_must_be_ready_and_rearm_asks_first_and_waits_for_an_open_attempt(self):
        self.assertIn("cannot be read", formal.status(self.paths)["checks"]["fixture_recovery"])
        recovery(self.private, status="FAILED")
        self.assertIn("rearm-fixture", formal.status(self.paths)["checks"]["fixture_recovery"])
        with self.assertRaisesRegex(formal.FormalError, "--confirm-rearm"):
            formal.rearm_fixture(self.paths, confirm_rearm=False)
        ledger.append(self.open_batch(slot_count=1), "attempt_started", {
            "attempt_id": "attempt-001", "slot_id": "s000", "arm": "woa_product",
            "task_id": "task-000", "replicate": "primary", "run_id": "run-001",
            "fixture_digest": DIGEST, "capability_digest": DIGEST,
            "provider_config_digest": DIGEST, "provider_binding_digest": DIGEST,
            "replacement_of": None})
        with self.assertRaisesRegex(formal.FormalError, "interrupted attempt"):
            formal.rearm_fixture(self.paths, confirm_rearm=True)
        recovery(self.private)
        self.assertEqual(formal.status(self.paths)["checks"]["fixture_recovery"], "READY")
        self.assertEqual(formal.rearm_fixture(self.paths, confirm_rearm=True),
                         {"status": "READY", "restored": False})

    def test_status_checks_that_head_qualified_with_this_manifest_image_and_protocol(self):
        head = git(self.checkout, "rev-parse", "HEAD")
        candidate = {"commit": head, "agent_image_id": DIGEST, "agent_image_revision": head}
        root = self.private / "qualification"
        root.mkdir(parents=True)
        (root / "fixture-manifest.json").write_text(json.dumps(
            {"manifest_digest": DIGEST, "candidate": candidate}))
        self.assertIn("has not passed", formal.status(self.paths)["checks"]["qualification"])
        digest = protocol.document()["protocol_digest"]

        def artifact(stamp, **changes):
            record = {"status": "READY", "candidate": candidate,
                      "fixture_manifest_digest": DIGEST,
                      "offline": {"protocol_digest": digest}, **changes}
            (root / f"p74a-qualification-{stamp}.json").write_text(json.dumps(record))

        def check():
            return formal.status(self.paths)["checks"]["qualification"]

        artifact("20260901T000000Z")
        artifact("20260902T000000Z", candidate={**candidate, "commit": NEW_CANDIDATE["commit"]})
        self.assertEqual(check(), "READY")
        # A manifest taken or an image built after the qualification leaves it behind.
        artifact("20260903T000000Z", fixture_manifest_digest=OTHER)
        self.assertIn("another fixture manifest", check())
        # The manifest no longer names a candidate, so the image is checked where the running
        # stack is: the paid entry passes what it verified there, and status cannot.
        artifact("20260904T000000Z")
        self.assertEqual(check(), "READY")
        with self.assertRaisesRegex(formal.FormalError, "another Agent image"):
            formal.qualification_for_head(
                self.paths, candidate={**candidate, "agent_image_id": OTHER})
        artifact("20260905T000000Z", offline={"protocol_digest": DIGEST})
        self.assertIn("another machine protocol", check())
        artifact("20260906T000000Z", status="FAILED")
        self.assertIn("did not pass", check())

    def test_freeze_commands_record_each_freeze_once_after_verifying_it(self):
        with self.assertRaisesRegex(formal.FormalError, "only an ACCEPT"):
            formal.record_family_review(self.paths, reviewer="Independent Reviewer",
                                        reviewed_at="2026-09-15T08:00:00Z", verdict="REVISE",
                                        findings=["two families repeat"])
        with self.assertRaisesRegex(formal.FormalError, "family-review"):
            formal.record_first_freeze(self.paths)
        recorded = formal.record_family_review(self.paths, reviewer="Independent Reviewer",
                                               reviewed_at="2026-09-15T08:00:00Z",
                                               verdict="ACCEPT", findings=[])
        self.assertEqual(recorded["review_digest"], review()["review_digest"])
        # An accepted family review does not stand in for the external review.
        with self.assertRaisesRegex(formal.FormalError, "no committed external review"):
            formal.record_first_freeze(self.paths)
        conclusions = external(reviewed_commit=git(self.checkout, "rev-parse", "HEAD"))
        review_file = self.root / "external-review.json"
        review_file.write_text(json.dumps({key: conclusions[key] for key in (
            "reviewer", "reviewed_at", "reviewed_commit", "items", "findings")}))
        reviewed = formal.record_external_review(self.paths, review_file=review_file)
        self.assertEqual((reviewed["verdict"], reviewed["review_digest"]),
                         ("ACCEPT", conclusions["review_digest"]))
        taken = formal.record_first_freeze(self.paths)
        self.assertEqual(taken["first_freeze_digest"],
                         first(external_review=conclusions)["first_freeze_digest"])
        self.assertEqual(formal.record_first_freeze(self.paths)["commit"], taken["commit"])
        self.assertEqual(formal.verify_freezes(self.paths)["stage"], "first_frozen")
        with self.assertRaisesRegex(formal.FormalError, "no formal development batch"):
            formal.record_second_freeze(self.paths)
        with self.assertRaisesRegex(formal.FormalError, "second-freeze"):
            formal.record_third_freeze(self.paths)

    def test_the_second_freeze_needs_a_gated_screening_safety_and_experience_pass(self):
        self.freeze()

        def value(verdict_status="gated", gate="PASS", robust=True, experience=True,
                  safety=False):
            primary = {"normal": {"gate_verdict": gate, "robust": robust},
                       "latency": {"experience_gate_passed": experience}}
            return {"batches": [{"verdict_status": verdict_status,
                                 "arms": {"woa_product": {"replicates": {"primary": primary}}}}],
                    "safety": {"safety_gate_failed": safety}}

        refusals = {"descriptive": (value(verdict_status="descriptive_only"), "not gated"),
                    "screening": (value(gate="FAIL"), "screening gate"),
                    "not_robust": (value(robust=False), "--accept-not-robust"),
                    "experience": (value(experience=None), "experience gate"),
                    "safety": (value(safety=True), "safety gate")}
        for label, (report_value, pattern) in refusals.items():
            with self.subTest(label=label), mock.patch.object(
                    formal, "development_report", return_value=report_value):
                with self.assertRaisesRegex(formal.FormalError, pattern):
                    formal.record_second_freeze(self.paths)
        candidate = CandidateRepository(self)
        paths = dataclasses.replace(self.paths, repository=candidate.root)
        path = paths.ledgers / "p74b-development-001.jsonl"
        ledger.create(path, header(slots(1), batch_id="p74b-development-001",
                                   candidate=candidate.candidate()))
        summary = ledger.append(path, "batch_sealed", {"reason": "operator_stop"})
        with mock.patch.object(formal, "development_report", return_value=value(robust=False)):
            formal.record_second_freeze(paths, accept_not_robust=True)
        record, _ = formal.committed_record(self.workbench,
                                            f"{formal.FREEZES}/second-freeze.json")
        self.assertEqual((record["candidate"], record["development_chain_head"]),
                         (candidate.candidate(), summary["chain_head"]))
        self.assertEqual(formal.verify_freezes(paths)["stage"], "second_frozen")


class ChainVersionTests(FormalFixture):
    OLDER_CONTRACT = "sha256:" + "1" * 64

    def finished_chain(self, preflight):
        """Two sealed development batches, each announced with its manifest and closed."""
        task_id = json.loads(harness.DEVELOPMENT_TASK_SET.read_text())["tasks"][0]["task_id"]
        schedule = [{"slot_id": "p001", "arm": "woa_product", "task_id": task_id,
                     "replicate": "primary"}]
        manifest = {"manifest_digest": DIGEST, "fixture_id": "synthetic"}
        previous = None
        for number, candidate in ((1, CANDIDATE), (2, NEW_CANDIDATE)):
            batch_id = f"p74b-development-{number:03d}"
            value = header(schedule, batch_id=batch_id, candidate=candidate, previous=previous,
                           ledger_contract_digest=ledger.LEDGER_CONTRACT_DIGEST)
            path = self.paths.ledgers / f"{batch_id}.jsonl"
            ledger.create(path, value)
            formal._announce(self.paths, value, {"fixture_manifest": manifest,
                                                 "preflight_digest": preflight["preflight_digest"]})
            summary = ledger.append(path, "batch_sealed", {"reason": "operator_stop"})
            formal.close_batch(self.paths, summary)
            previous = formal.link_to(summary)

    def test_an_archived_chain_recomputes_under_its_reader_and_no_later_reader_reads_it(self):
        self.provider(*[answer()] * 5)
        self.freeze()
        self.custody()
        preflight = self.preflight()
        with mock.patch.object(ledger, "LEDGER_CONTRACT_DIGEST", self.OLDER_CONTRACT):
            self.finished_chain(preflight)
            archived = formal.archive_development_chain(self.paths)
            verified = formal.verify_chain_archive(self.paths, recompute=True)
        self.assertEqual((verified["archive_digest"], verified["recomputed"]),
                         (archived["archive_digest"], True))
        record, _ = formal.committed_record(self.workbench, formal.chain_archive_path())
        self.assertEqual((record["ledger_contract_digest"], list(record["fixture_manifests"]),
                          [item["batch_id"] for item in record["batches"]]),
                         (self.OLDER_CONTRACT, [DIGEST],
                          ["p74b-development-001", "p74b-development-002"]))
        old_ledgers = self.paths.ledgers
        written = {path.name: path.read_bytes() for path in old_ledgers.glob("*.jsonl")}
        # A later reader refuses the chain rather than read it by rules it was not written under.
        with self.assertRaisesRegex(formal.FormalError, "another ledger contract"):
            formal.development_chain(self.paths)
        self.assertIn("another ledger contract",
                      formal.status(self.paths)["checks"]["batch_records"])
        with self.assertRaisesRegex(formal.FormalError, "reader that wrote"):
            formal.verify_chain_archive(self.paths, recompute=True)
        # A new protocol version opens its own chain and still verifies the archive it inherits.
        with mock.patch.object(protocol, "PROTOCOL_ID", "p74-odoo-v2"):
            self.assertEqual(formal.development_chain(self.paths), [])
            inherited = formal.verify_chain_archive(self.paths, protocol_id="p74-odoo-v1")
            self.assertEqual(inherited["archive_digest"], archived["archive_digest"])
            self.paths.ledgers.mkdir(parents=True)
            name = sorted(written)[0]
            (self.paths.ledgers / name).write_bytes(written[name])
            os.chmod(self.paths.ledgers / name, 0o600)
            with self.assertRaisesRegex(formal.FormalError, "another ledger contract"):
                formal.development_chain(self.paths)
        self.assertEqual({path.name: path.read_bytes() for path in old_ledgers.glob("*.jsonl")},
                         written)


class ReconciliationTests(FormalFixture):
    def stopped_preflight(self):
        """A preflight whose second canary the spend ledger could not record."""
        from web_operation_agent.model import SpendMeter

        original, written = SpendMeter._append_record, []

        def append(meter, rec, **options):
            written.append(rec)
            if len(written) == 2:
                raise OSError("the spend ledger's disk is full")
            return original(meter, rec, **options)

        with mock.patch.object(SpendMeter, "_append_record", append):
            return self.preflight()

    def test_no_paid_call_follows_an_unaccounted_call_until_the_bill_is_reconciled(self):
        provider = self.provider(*[answer()] * 7)
        self.freeze()
        self.custody()
        stopped = self.stopped_preflight()
        self.assertEqual(stopped["canary"]["stopped"], "accounting_failure")
        self.assertEqual(formal.unaccounted_calls(self.paths), {stopped["spend_batch_id"]: [
            stopped["calls"][1]["provider_request_id"]]})
        self.assertIn("reconcile-billing",
                      formal.status(self.paths)["checks"]["billing_reconciliation"])
        # A spend ledger that can be written again is not enough.
        self.assertEqual(formal.status(self.paths)["checks"]["spend_ledger"], "READY")
        with self.assertRaisesRegex(formal.FormalError, "reconciliation"):
            self.preflight("2026-09-15T09:00:00Z")
        self.assertEqual(len(provider.requests), 2)
        arguments = {"owner": "Evaluation Owner", "reconciled_at": "2026-09-15T08:30:00Z",
                     "billed_calls": 2, "billed_cost_cny": 0.0004, "evidence_digest": DIGEST,
                     "notes": ["the bill lists both canaries"]}
        with self.assertRaisesRegex(formal.FormalError, "no unaccounted call"):
            formal.record_billing_reconciliation(self.paths, spend_batch_id="p74b-other",
                                                 **arguments)
        for label, changes in {"owner": {"owner": ""}, "time": {"reconciled_at": "today"},
                               "calls": {"billed_calls": -1},
                               "evidence": {"evidence_digest": "a bill"}}.items():
            with self.subTest(label=label), self.assertRaises(formal.FormalError):
                formal.record_billing_reconciliation(
                    self.paths, spend_batch_id=stopped["spend_batch_id"],
                    **{**arguments, **changes})
        recorded = formal.record_billing_reconciliation(
            self.paths, spend_batch_id=stopped["spend_batch_id"], **arguments)
        batch = stopped["spend_batch_id"]
        # The check answers with what covers the batch: its debts and the records that account
        # for them, so a second debt later changes the answer.
        covered = formal.check_reconciliations(self.paths)
        self.assertEqual(set(covered), {batch})
        self.assertTrue(covered[batch].startswith("sha256:"))
        claims = formal.reconciliation_claims(self.paths, batch)
        self.assertEqual([record["reconciliation_digest"] for record in claims.values()],
                         [recorded["reconciliation_digest"]])
        self.assertEqual(recorded["covered_calls"], [stopped["calls"][1]["provider_request_id"]])
        self.assertEqual(formal.status(self.paths)["checks"]["billing_reconciliation"], "READY")
        self.assertEqual(self.preflight("2026-09-15T09:00:00Z")["status"], "READY")
        # Nothing is left to reconcile, and the same record is not written twice.
        with self.assertRaisesRegex(formal.FormalError, "already reconciled"):
            formal.record_billing_reconciliation(self.paths, spend_batch_id=batch, **arguments)
        # A reconciliation changed after it was committed no longer counts.
        target = self.workbench / recorded["record"]
        target.write_text(target.read_text().replace("Evaluation Owner", "Someone Else"))
        with self.assertRaises(formal.FormalError):
            formal.check_reconciliations(self.paths)


    def test_a_reconciliation_of_calls_no_spend_batch_left_unaccounted_is_refused(self):
        """A batch that owes nothing has no reconciliation to write. A record standing against
        one covers calls nobody made, and the check that reads only debted batches would never
        have read it."""
        self.assertEqual(formal.unaccounted_calls(self.paths), {})
        self.assertEqual(formal.check_reconciliations(self.paths), {})
        record = formal.reconciliation_record(
            spend_batch_id="p74b-development-009", request_ids=["attempt-001#1"],
            owner="Evaluation Owner", reconciled_at="2026-09-15T08:30:00Z", billed_calls=1,
            billed_cost_cny=0.0002, evidence_digest=DIGEST, notes=["a bill nobody owed"])
        formal.commit_record(
            self.workbench,
            f"{formal.RECONCILIATIONS}/p74b-development-009/"
            f"{formal._claim_name('p74b-development-009', ['attempt-001#1'])}.json",
            record, "a reconciliation of calls nobody made")
        with self.assertRaisesRegex(formal.FormalError, "never left unaccounted"):
            formal.check_reconciliations(self.paths)

    def test_a_reconciliation_beside_the_spend_batches_is_refused(self):
        """Each batch's reconciliations live in its own directory, so a file left beside them --
        the shape the one-file-per-batch layout wrote -- is a record nothing reads."""
        record = formal.reconciliation_record(
            spend_batch_id="p74b-development-009", request_ids=["attempt-001#1"],
            owner="Evaluation Owner", reconciled_at="2026-09-15T08:30:00Z", billed_calls=1,
            billed_cost_cny=0.0002, evidence_digest=DIGEST, notes=["the earlier layout"])
        formal.commit_record(self.workbench,
                             f"{formal.RECONCILIATIONS}/p74b-development-009.json", record,
                             "a reconciliation outside any spend batch")
        with self.assertRaisesRegex(formal.FormalError, "outside any spend batch"):
            formal.check_reconciliations(self.paths)


#: The time the review tests record at; a review may not name a later one.
NOW = "2026-09-16T00:00:00Z"


class ExternalReviewTests(FormalFixture):
    def setUp(self):
        super().setUp()
        # Reviews are recorded by commits dated at NOW, so no review time lies after its commit.
        dated = mock.patch.dict(os.environ, {"GIT_AUTHOR_DATE": NOW.replace("Z", "+00:00"),
                                             "GIT_COMMITTER_DATE": NOW.replace("Z", "+00:00")})
        dated.start()
        self.addCleanup(dated.stop)

    def review_file(self, record):
        path = self.root / f"review-{record['review_digest'][7:19]}.json"
        path.write_text(json.dumps({key: record[key] for key in (
            "reviewer", "reviewed_at", "reviewed_commit", "items", "findings")}))
        return path

    def record_review(self, value, *, now=NOW):
        return formal.record_external_review(self.paths, review_file=self.review_file(value),
                                             clock=lambda: now)

    def gate(self):
        return formal.status(self.paths)["checks"]["external_review"]

    def test_only_the_newest_review_of_the_running_apparatus_opens_paid_calls(self):
        provider = self.provider(*[answer()] * 5)
        self.freeze()
        self.custody()
        self.assertEqual(self.gate(), "READY")
        head = git(self.checkout, "rev-parse", "HEAD")
        revised = external({"real_product_entry": "REVISE"}, reviewed_commit=head,
                           reviewed_at="2026-09-15T09:00:00Z")
        recorded = self.record_review(revised)
        self.assertEqual(recorded["verdict"], "REVISE")
        # The first freeze still verifies, but the newest review of this apparatus decides.
        self.assertEqual(formal.verified_first_freeze(self.paths)[0], self.first_freeze)
        self.assertIn("is REVISE", self.gate())
        with self.assertRaisesRegex(formal.FormalError, "is REVISE"):
            self.preflight()
        self.assertEqual(provider.requests, [])
        accepted = external(reviewed_commit=head, reviewed_at="2026-09-15T10:00:00Z")
        self.record_review(accepted)
        record = self.preflight()
        self.assertEqual((record["status"], record["external_review_digest"]),
                         ("READY", accepted["review_digest"]))

    def test_a_review_binds_its_commit_and_every_reviewed_file(self):
        head = git(self.checkout, "rev-parse", "HEAD")
        with self.assertRaisesRegex(formal.FormalError, "reviewed commit"):
            self.record_review(external(reviewed_commit="d" * 40))
        # A later commit outside the reviewed files leaves the review standing ...
        (self.checkout / "README").write_text("a later product change\n")
        git(self.checkout, "commit", "-q", "-am", "change the readme")
        accepted = external(reviewed_commit=head)
        self.record_review(accepted)
        self.assertEqual(formal.verified_external_review(self.paths)[0], accepted)
        # ... and one that changes an apparatus file needs a new review.
        evaluator = self.checkout / "evals/odoo/evaluator.py"
        evaluator.write_bytes(evaluator.read_bytes() + b"# changed after the review\n")
        git(self.checkout, "commit", "-q", "-am", "change the evaluator")
        with self.assertRaisesRegex(formal.FormalError, "changed after the reviewed commit"):
            formal.verified_external_review(self.paths)
        with self.assertRaisesRegex(formal.FormalError, "changed after the reviewed commit"):
            self.record_review(external(reviewed_commit=head, reviewed_at="2026-09-15T11:00:00Z"))

    def test_the_custody_check_and_the_qualification_are_bound_as_well(self):
        head = git(self.checkout, "rev-parse", "HEAD")
        self.record_review(external(reviewed_commit=head))
        for relative in freeze.REVIEW_BOUND_FILES:
            with self.subTest(relative=relative):
                path = self.checkout / relative
                original = path.read_bytes()
                path.write_bytes(original + b"# changed after the review\n")
                git(self.checkout, "commit", "-q", "-am", f"change {relative}")
                with self.assertRaisesRegex(formal.FormalError,
                                            "changed after the reviewed commit"):
                    formal.verified_external_review(self.paths)
                path.write_bytes(original)
                git(self.checkout, "commit", "-q", "-am", f"restore {relative}")
                self.assertEqual(self.gate(), "READY")

    def test_the_review_recorded_last_decides_whatever_time_it_names(self):
        head = git(self.checkout, "rev-parse", "HEAD")
        # A block reviewed at 09:00 but recorded after the acceptance decides.
        self.record_review(external(reviewed_commit=head, reviewed_at="2026-09-15T10:00:00Z",
                                    reviewer="Reviewer A"))
        blocked = external({"apparatus_whitelist_closed": "BLOCK"}, reviewed_commit=head,
                           reviewed_at="2026-09-15T09:00:00Z", reviewer="Reviewer B")
        self.assertEqual(self.record_review(blocked)["verdict"], "BLOCK")
        self.assertIn("is BLOCK", self.gate())
        accepted = external(reviewed_commit=head, reviewed_at="2026-09-15T08:00:00Z",
                            reviewer="Reviewer C")
        self.record_review(accepted)
        self.assertEqual(formal.verified_external_review(self.paths)[0], accepted)
        # A time the recording has not reached is refused, and so is any other spelling of a time.
        with self.assertRaisesRegex(formal.FormalError, "later than now"):
            self.record_review(external(reviewed_commit=head, reviewed_at="2026-09-16T00:00:01Z"))
        for spelling in ("2026-09-15T12:00:00.500Z", "20260915T120000Z"):
            with self.subTest(spelling=spelling), self.assertRaises(freeze.FreezeError):
                external(reviewed_commit=head, reviewed_at=spelling)

    def test_a_review_dated_after_the_commit_that_recorded_it_is_refused(self):
        head = git(self.checkout, "rev-parse", "HEAD")
        future = external(reviewed_commit=head, reviewed_at="2099-01-01T00:00:00Z")
        self.record(formal.external_review_path(future), future)
        self.assertIn("later than the commit that recorded it", self.gate())

    def test_a_review_merged_away_on_a_side_branch_stops_paid_calls(self):
        # The report on 0114a25: a merge keeping one side's tree hid a block recorded on the other.
        head = git(self.checkout, "rev-parse", "HEAD")
        self.record_review(external(reviewed_commit=head, reviewed_at="2026-09-15T08:00:00Z"))
        base = git(self.workbench, "rev-parse", "--abbrev-ref", "HEAD")
        git(self.workbench, "checkout", "-q", "-b", "side")
        self.record_review(external({"real_product_entry": "BLOCK"}, reviewed_commit=head,
                                    reviewed_at="2026-09-15T09:00:00Z"))
        git(self.workbench, "checkout", "-q", base)
        git(self.workbench, "merge", "-q", "-s", "ours", "side", "-m", "keep the acceptance only")
        self.assertIn("merged into .workbench", self.gate())
        with self.assertRaises(formal.FormalError):
            formal.verified_external_review(self.paths)

    def test_a_review_recorded_on_a_merged_branch_is_refused_whichever_way_it_was_merged(self):
        # The review of 582e11c (E4): an acceptance recorded on a side branch, merged into a main
        # line that had recorded a revision later, came last in the history and decided.
        head = git(self.checkout, "rev-parse", "HEAD")
        base = git(self.workbench, "rev-parse", "--abbrev-ref", "HEAD")
        git(self.workbench, "checkout", "-q", "-b", "side")
        self.record_review(external(reviewed_commit=head, reviewed_at="2026-09-15T08:00:00Z"))
        git(self.workbench, "checkout", "-q", base)
        self.record_review(external({"real_product_entry": "REVISE"}, reviewed_commit=head,
                                    reviewed_at="2026-09-15T09:00:00Z"))
        git(self.workbench, "merge", "-q", "--no-edit", "side")
        self.assertIn("merged into .workbench", self.gate())
        git(self.workbench, "reset", "-q", "--hard", "HEAD~1")
        git(self.workbench, "checkout", "-q", "side")
        git(self.workbench, "merge", "-q", "--no-edit", base)
        self.assertIn("merged into .workbench", self.gate())

    def test_a_review_recorded_as_a_symbolic_link_is_refused(self):
        # E3: a link to a file outside .workbench, which changes while git sees no change at all.
        head = git(self.checkout, "rev-parse", "HEAD")
        revised = external({"real_product_entry": "REVISE"}, reviewed_commit=head,
                           reviewed_at="2026-09-15T08:00:00Z")
        outside = self.root / "outside-review.json"
        outside.write_text(json.dumps(revised))
        relative = formal.external_review_path(revised)
        (self.workbench / relative).parent.mkdir(parents=True, exist_ok=True)
        (self.workbench / relative).symlink_to(outside)
        git(self.workbench, "add", "--", relative)
        git(self.workbench, "commit", "-q", "-m", "record a review as a link")
        self.assertIn("regular file", self.gate())
        outside.write_text(json.dumps(external(reviewed_commit=head,
                                               reviewed_at="2026-09-15T08:00:00Z")))
        self.assertIn("regular file", self.gate())

    def test_a_recorded_review_replayed_under_another_name_is_refused(self):
        # E2: the bytes of an earlier acceptance, committed again under a new name after a block.
        head = git(self.checkout, "rev-parse", "HEAD")
        accepted = self.record_review(external(reviewed_commit=head,
                                               reviewed_at="2026-09-15T08:00:00Z"))
        self.record_review(external({"real_product_entry": "BLOCK"}, reviewed_commit=head,
                                    reviewed_at="2026-09-15T09:00:00Z"))
        replay = f"{formal.REVIEWS}/external-review-20260915T100000Z-000000000000.json"
        (self.workbench / replay).write_bytes((self.workbench / accepted["record"]).read_bytes())
        git(self.workbench, "add", "--", replay)
        git(self.workbench, "commit", "-q", "-m", "replay the acceptance")
        self.assertIn("not named by the time and digest", self.gate())

    def test_a_review_recorded_under_a_directory_spelled_in_other_letters_is_refused(self):
        head = git(self.checkout, "rev-parse", "HEAD")
        self.record_review(external(reviewed_commit=head, reviewed_at="2026-09-15T08:00:00Z"))
        blocked = external({"real_product_entry": "BLOCK"}, reviewed_commit=head,
                           reviewed_at="2026-09-15T09:00:00Z")
        source = self.root / "blocked-review.json"
        source.write_text(json.dumps(blocked))
        blob = git(self.workbench, "hash-object", "-w", str(source))
        spelled = formal.external_review_path(blocked).replace("/reviews/", "/Reviews/")
        git(self.workbench, "update-index", "--add", "--cacheinfo", f"100644,{blob},{spelled}")
        git(self.workbench, "commit", "-q", "-m", "record a block under another spelling")
        self.assertIn("misplaced", self.gate())

    def test_a_review_that_names_a_key_twice_is_refused(self):
        head = git(self.checkout, "rev-parse", "HEAD")
        accepted = external(reviewed_commit=head, reviewed_at="2026-09-15T08:00:00Z")
        # A reader sees the item revised; a parser that keeps the last key sees it accepted.
        doubled = json.dumps(accepted).replace(
            '"items": {', '"items": {"real_product_entry": {"evidence": ["revised"], '
                          '"verdict": "REVISE"}, ', 1)
        self.assertEqual(json.loads(doubled), accepted)
        review_file = self.root / "doubled-review.json"
        review_file.write_text(doubled)
        with self.assertRaisesRegex(formal.FormalError, "cannot be read"):
            formal.record_external_review(self.paths, review_file=review_file, clock=lambda: NOW)
        relative = formal.external_review_path(accepted)
        (self.workbench / relative).parent.mkdir(parents=True, exist_ok=True)
        (self.workbench / relative).write_text(doubled)
        git(self.workbench, "add", "--", relative)
        git(self.workbench, "commit", "-q", "-m", "record a review that names an item twice")
        self.assertIn("names a key twice", self.gate())

    def test_two_reviews_recorded_by_one_commit_are_refused(self):
        head = git(self.checkout, "rev-parse", "HEAD")
        for name, value in (("external-review-20260915T080000Z-a.json",
                             external(reviewed_commit=head, reviewed_at="2026-09-15T08:00:00Z")),
                            ("external-review-20260915T090000Z-b.json",
                             external({"real_product_entry": "BLOCK"}, reviewed_commit=head,
                                      reviewed_at="2026-09-15T09:00:00Z"))):
            target = self.workbench / formal.REVIEWS / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(value))
        git(self.workbench, "add", "--", formal.REVIEWS)
        git(self.workbench, "commit", "-q", "-m", "record two reviews at once")
        self.assertIn("more than one external review", self.gate())

    def test_a_removed_changed_or_unrecorded_review_file_stops_paid_calls(self):
        head = git(self.checkout, "rev-parse", "HEAD")
        self.record_review(external(reviewed_commit=head, reviewed_at="2026-09-15T08:00:00Z"))
        blocked = self.record_review(external({"real_product_entry": "BLOCK"},
                                              reviewed_commit=head,
                                              reviewed_at="2026-09-15T09:00:00Z"))
        self.assertIn("is BLOCK", self.gate())
        target = self.workbench / blocked["record"]
        kept = target.read_bytes()
        # A review file nobody recorded is refused rather than read or skipped.
        stray = self.workbench / formal.REVIEWS / "external-review-20260915T100000Z.json"
        stray.write_bytes(kept)
        self.assertIn("never recorded", self.gate())
        stray.unlink()
        # Removing the block from the working tree does not reopen the
        # acceptance before it.
        target.unlink()
        self.assertTrue(self.gate().startswith("refused"))
        self.assertNotIn("is BLOCK", self.gate())
        target.write_bytes(kept)
        self.assertIn("is BLOCK", self.gate())
        # Neither does removing it by a commit, which is refused for good.
        git(self.workbench, "rm", "-q", "--", blocked["record"])
        git(self.workbench, "commit", "-q", "-m", "remove the block")
        self.assertIn("changed, removed", self.gate())
        with self.assertRaisesRegex(formal.FormalError, "changed, removed"):
            self.record_review(external(reviewed_commit=head, reviewed_at="2026-09-15T10:00:00Z"))

    def test_a_blocking_finding_keeps_a_review_of_accepted_items_from_accepting(self):
        head = git(self.checkout, "rev-parse", "HEAD")
        blocking = external(reviewed_commit=head, reviewed_at="2026-09-15T08:00:00Z", findings=[
            {"blocking": True, "text": "the review gate orders reviews by a written time"}])
        self.assertEqual(self.record_review(blocking)["verdict"], "REVISE")
        self.assertIn("is REVISE", self.gate())
        noted = external(reviewed_commit=head, reviewed_at="2026-09-15T09:00:00Z", findings=[
            {"blocking": False, "text": "a note that blocks nothing"}])
        self.assertEqual(self.record_review(noted)["verdict"], "ACCEPT")
        self.assertEqual(self.gate(), "READY")


class RehearsalQualificationTests(FormalFixture):
    def test_a_rehearsal_qualification_counts_only_while_paid_effects_are_kept_apart(self):
        head = git(self.checkout, "rev-parse", "HEAD")
        candidate = {"commit": head, "agent_image_id": DIGEST, "agent_image_revision": head}
        manifest = harness.fixture_manifest_path(self.private)
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"manifest_digest": DIGEST, "candidate": candidate}))
        records = self.root / "rehearsal" / "qualification"
        records.mkdir(parents=True)
        (records / "p74a-qualification-20260915T000000Z.json").write_text(json.dumps({
            "status": "READY", "candidate": candidate, "fixture_manifest_digest": DIGEST,
            "offline": {"protocol_digest": protocol.document()["protocol_digest"]}}))
        self.provider()
        shared = dataclasses.replace(self.paths, rehearsal_qualifications=records)
        with self.assertRaisesRegex(formal.FormalError, "its own formal root.*loopback provider"):
            formal.qualification_for_head(shared)
        self.assertIn("kept apart", formal.status(shared)["checks"]["qualification"])
        apart = dataclasses.replace(shared, formal_root=self.root / "rehearsal" / "formal",
                                    provider_config_file=self.paths.provider_config)
        self.assertEqual(formal.qualification_for_head(apart)["artifact"],
                         "p74a-qualification-20260915T000000Z.json")
        apart.provider_config.write_text(json.dumps(
            {"base_url": "https://provider.example.test/v1", "key_file": str(self.key)}))
        with self.assertRaisesRegex(formal.FormalError, "loopback provider"):
            formal.qualification_for_head(apart)
        # Without a rehearsal directory the private records decide, and none qualified HEAD.
        self.assertIn("has not passed", formal.status(self.paths)["checks"]["qualification"])


if __name__ == "__main__":
    unittest.main()
