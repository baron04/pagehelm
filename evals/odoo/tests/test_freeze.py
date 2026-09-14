import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from evals.odoo import freeze, protocol
from evals.odoo.revision import REPOSITORY

TASKSETS = REPOSITORY / "evals/odoo/tasksets"
IMAGE = "sha256:" + "5" * 64
HELDOUT_IDS = [f"h{index:03d}" for index in range(1, 31)]
ELIGIBLE = ["h001", "h002", "h003", "h004", "h005", "h026", "h027"]


def seals():
    return (json.loads((TASKSETS / "p74-odoo-development-v1.seal.json").read_text()),
            json.loads((TASKSETS / "p74-odoo-heldout-v1.seal.json").read_text()),
            json.loads((TASKSETS / "p74-odoo-split-v1.json").read_text()))


def review(verdict="ACCEPT", **changes):
    development, heldout, _ = seals()
    arguments = {"reviewer": "Independent Reviewer", "reviewed_at": "2026-09-15T08:00:00Z",
                 "verdict": verdict, "development_seal": development, "heldout_seal": heldout}
    return freeze.family_review_record(**{**arguments, **changes})


def first(**review_changes):
    development, heldout, split = seals()
    return freeze.first_freeze_record(development_seal=development, heldout_seal=heldout,
                                      split_verification=split,
                                      family_review=review(**review_changes))


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=p74", "-c",
                           "user.email=p74@example.test", *args],
                          check=True, capture_output=True).stdout.decode().strip()


class CandidateRepository:
    """A throwaway repository holding a copy of every file a second freeze reads from a commit."""

    def __init__(self, test):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        paths = {path for paths in protocol.APPARATUS_MODULES.values() for path in paths}
        for relative in paths | set(freeze.MACHINE_PROTOCOL_FILES):
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY / relative, self.root / relative)
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "candidate")
        self.commit = git(self.root, "rev-parse", "HEAD")

    def candidate(self, revision=None):
        return {"commit": self.commit, "agent_image_id": IMAGE,
                "agent_image_revision": revision or self.commit}


class FirstFreezeTests(unittest.TestCase):
    def test_only_an_accepted_review_of_these_seals_opens_the_first_freeze(self):
        development, heldout, split = seals()
        record = first()
        self.assertEqual(protocol.document(first_freeze=record)["stage"], "first_frozen")
        self.assertEqual(record["family_review_digest"], review()["review_digest"])
        self.assertEqual(freeze.verify_first_freeze(
            record, development_seal=development, heldout_seal=heldout,
            split_verification=split, family_review=review()), record)
        with self.assertRaises(freeze.FreezeError):
            first(verdict="REVISE", findings=["two held-out families repeat development ones"])
        with self.assertRaises(freeze.FreezeError):
            freeze.first_freeze_record(development_seal=development, heldout_seal=heldout,
                                       split_verification=split,
                                       family_review={**review(), "reviewer": "Someone Else"})
        with self.assertRaises(freeze.FreezeError):
            freeze.first_freeze_record(development_seal=heldout, heldout_seal=development,
                                       split_verification=split, family_review=review())
        for changes in ({"reviewed_at": "yesterday"}, {"reviewer": ""}, {"verdict": "OK"}):
            with self.subTest(changes=changes), self.assertRaises(freeze.FreezeError):
                review(**changes)

    def test_a_changed_first_freeze_is_not_a_freeze(self):
        with self.assertRaises(freeze.FreezeError):
            protocol.document(first_freeze={**first(), "normal_tasks_per_split": 30})
        with self.assertRaises(freeze.FreezeError):
            protocol.document(first_freeze={"kind": "p74_first_freeze"})


class SecondFreezeTests(unittest.TestCase):
    def test_the_second_freeze_reads_the_apparatus_from_the_clean_candidate_commit(self):
        repository = CandidateRepository(self)
        record = freeze.second_freeze_record(first_freeze=first(),
                                             candidate=repository.candidate(),
                                             repository=repository.root)
        evaluator = REPOSITORY / "evals/odoo/evaluator.py"
        self.assertEqual(record["implementation_digests"]["evaluator"]["evals/odoo/evaluator.py"],
                         "sha256:" + hashlib.sha256(evaluator.read_bytes()).hexdigest())
        self.assertEqual(set(record["machine_protocol_digests"]),
                         set(freeze.MACHINE_PROTOCOL_FILES))
        self.assertEqual(protocol.document(first_freeze=first(), second_freeze=record)["stage"],
                         "second_frozen")
        # A later commit changes the checkout, not what the freeze bound.
        (repository.root / "evals/odoo/evaluator.py").write_text("# changed after the freeze\n")
        git(repository.root, "commit", "-q", "-am", "later")
        self.assertEqual(freeze.verify_second_freeze(record, first_freeze=first(),
                                                     repository=repository.root), record)
        tampered = {**record, "provider_config_digest": "sha256:" + "0" * 64}
        with self.assertRaises(freeze.FreezeError):
            freeze.verify_second_freeze(tampered, first_freeze=first(),
                                        repository=repository.root)

    def test_a_mislabelled_image_or_an_unclean_checkout_cannot_be_frozen(self):
        repository = CandidateRepository(self)
        with self.assertRaises(freeze.FreezeError):
            freeze.second_freeze_record(first_freeze=first(),
                                        candidate=repository.candidate("b" * 40),
                                        repository=repository.root)
        with self.assertRaises(freeze.FreezeError):
            freeze.second_freeze_record(first_freeze={"kind": "p74_first_freeze"},
                                        candidate=repository.candidate(),
                                        repository=repository.root)
        (repository.root / "notes.txt").write_text("uncommitted\n")
        with self.assertRaises(freeze.FreezeError):
            freeze.second_freeze_record(first_freeze=first(), candidate=repository.candidate(),
                                        repository=repository.root)


class ThirdFreezeTests(unittest.TestCase):
    def setUp(self):
        repository = CandidateRepository(self)
        self.first = first()
        self.second = freeze.second_freeze_record(first_freeze=self.first,
                                                  candidate=repository.candidate(),
                                                  repository=repository.root)

    def third(self, commit="c" * 40, ids=HELDOUT_IDS, eligible=ELIGIBLE):
        return freeze.third_freeze_record(first_freeze=self.first, second_freeze=self.second,
                                          second_freeze_commit=commit, heldout_task_ids=ids,
                                          pageagent_eligible=eligible)

    def test_the_schedule_and_protocol_seal_are_derived_not_chosen(self):
        record = self.third()
        self.assertEqual(record["schedule_seed"],
                         protocol.derive_schedule_seed("c" * 40, purpose="heldout"))
        self.assertEqual(len(record["slots"]), 2 * (len(HELDOUT_IDS) + len(ELIGIBLE)))
        self.assertEqual(protocol.document(first_freeze=self.first, second_freeze=self.second,
                                           third_freeze=record)["stage"], "third_frozen")
        self.assertEqual(freeze.verify_third_freeze(
            record, first_freeze=self.first, second_freeze=self.second,
            heldout_task_ids=HELDOUT_IDS, pageagent_eligible=ELIGIBLE), record)
        with self.assertRaises(freeze.FreezeError):
            freeze.verify_third_freeze(record, first_freeze=self.first,
                                       second_freeze=self.second, heldout_task_ids=HELDOUT_IDS,
                                       pageagent_eligible=ELIGIBLE[:-1])
        with self.assertRaises(freeze.FreezeError):
            self.third(ids=HELDOUT_IDS[:29])
        with self.assertRaises(freeze.FreezeError):
            self.third(eligible=["h099"])

    def test_the_document_refuses_arbitrary_or_unlinked_freezes(self):
        with self.assertRaises(freeze.FreezeError):
            protocol.document(first_freeze=self.first, second_freeze=self.second,
                              third_freeze={"schedule": "anything"})
        with self.assertRaises(freeze.FreezeError):
            protocol.document(second_freeze=self.second)
        with self.assertRaises(freeze.FreezeError):
            protocol.document(first_freeze=first(reviewed_at="2026-09-16T08:00:00Z"),
                              second_freeze=self.second)
        record = self.third()
        forged = dict(record, slots=list(reversed(record["slots"])))
        forged = {**forged, "schedule_digest": protocol.digest_json(forged["slots"])}
        body = {key: value for key, value in forged.items() if key != "third_freeze_digest"}
        forged["third_freeze_digest"] = protocol.digest_json(body)
        with self.assertRaises(freeze.FreezeError):
            freeze.verify_third_freeze(forged, first_freeze=self.first,
                                       second_freeze=self.second, heldout_task_ids=HELDOUT_IDS,
                                       pageagent_eligible=ELIGIBLE)


if __name__ == "__main__":
    unittest.main()
