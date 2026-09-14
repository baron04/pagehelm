"""The formal entry points, against temporary repositories and a local fake provider only."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from evals.odoo import canary, escrow, formal, harness, ledger, protocol
from evals.odoo.revision import RevisionError, committed_once
from evals.odoo.tests.test_freeze import first, git, review
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
        self.checkout = repository(self.root / "checkout")
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
        self.record(f"{formal.FREEZES}/family-review.json", review())
        return self.record(f"{formal.FREEZES}/first-freeze.json", first())

    def preflight(self, at="2026-09-15T08:00:00Z"):
        return formal.run_preflight(self.paths, confirm_paid_calls=True, clock=lambda: at)


class PreflightTests(FormalFixture):
    def assertRefused(self, provider, pattern):
        with self.assertRaisesRegex(formal.FormalError, pattern):
            self.preflight()
        self.assertEqual(provider.requests, [])
        self.assertFalse(self.paths.spend().exists())

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
        self.assertEqual(formal.verified_preflight(self.paths, first()), record)

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
                    formal.verified_preflight(self.paths, first())
        self.paths.provider_config.write_text(json.dumps(
            {**settings, "base_url": settings["base_url"].replace("http://", "HTTP://") + "/"}))
        self.assertEqual(formal.verified_preflight(self.paths, first()), record)
        self.assertEqual(other_provider.requests, [])

    def test_only_the_newest_preflight_counts_and_it_must_have_passed_unchanged(self):
        self.provider(*[answer()] * 4, answer("qwen3.8-flash-drifted"), *[answer()] * 5)
        self.freeze()
        self.custody()
        failed = self.preflight()
        self.assertEqual(failed["status"], "FAILED")
        self.assertFalse(failed["canary"]["checks"]["model_string_5_of_5"])
        with self.assertRaisesRegex(formal.FormalError, "failed"):
            formal.verified_preflight(self.paths, first())
        ready = self.preflight("2026-09-15T09:00:00Z")
        self.assertEqual(formal.verified_preflight(self.paths, first()), ready)
        with self.assertRaisesRegex(formal.FormalError, "another first freeze"):
            formal.verified_preflight(self.paths, first(reviewed_at="2026-09-16T08:00:00Z"))
        stored = self.paths.preflights / f"{ready['spend_batch_id']}.json"
        stored.write_text(json.dumps({**ready, "calls": []}))
        with self.assertRaisesRegex(formal.FormalError, "changed"):
            formal.verified_preflight(self.paths, first())


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
                     "binding_digest": OTHER}
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


class StatusTests(FormalFixture):
    def test_status_names_what_is_missing_without_sending_anything(self):
        provider = self.provider()
        value = formal.status(self.paths)
        self.assertEqual((value["preflight_can_start"], value["development_can_start"]),
                         (False, False))
        self.assertEqual((value["checks"]["clean_checkout"],
                          value["checks"]["provider_configuration"]), ("READY", "READY"))
        for name in ("first_freeze", "escrow_custody", "preflight"):
            self.assertTrue(value["checks"][name].startswith("refused"), name)
        self.freeze()
        self.custody()
        value = formal.status(self.paths)
        self.assertEqual((value["preflight_can_start"], value["development_can_start"]),
                         (True, False))
        self.assertEqual(provider.requests, [])
        self.assertFalse(self.paths.spend().exists())


if __name__ == "__main__":
    unittest.main()
