import unittest

from evals.odoo import manifest
from evals.odoo.population import digest_json

CANDIDATE = {"commit": "c" * 40, "agent_image_id": "sha256:" + "d" * 64,
             "agent_image_revision": "c" * 40}
GUARD = "sha256:" + "a" * 64


def recorded(**changes):
    body = {"artifact_version": 1, "kind": "p74_fixture_manifest", "database": "woa_trial",
            "candidate": CANDIDATE, "baseline": {"guard_digest": GUARD}, **changes}
    return {**body, "manifest_digest": digest_json(body)}


class VerifyManifestTests(unittest.TestCase):
    def test_a_current_manifest_yields_the_fixture_a_batch_binds(self):
        record = manifest.verify_manifest(recorded(), candidate=CANDIDATE,
                                          baseline_guard_digest=GUARD)
        self.assertEqual(manifest.batch_fixture(record),
                         {"manifest_digest": record["manifest_digest"],
                          "baseline_guard_digest": GUARD})

    def test_a_changed_stale_or_foreign_manifest_is_refused(self):
        rebuilt = {**CANDIDATE, "commit": "e" * 40, "agent_image_revision": "e" * 40}
        cases = {
            "changed_after_recording": ({**recorded(), "database": "another"}, GUARD),
            "taken_before_candidates_were_recorded": (recorded(candidate=None), GUARD),
            "taken_with_another_candidate": (recorded(candidate=rebuilt), GUARD),
            "another_baseline": (recorded(), "sha256:" + "b" * 64),
            "wrong_kind": (recorded(kind="p74_fixture"), GUARD),
        }
        for label, (record, guard) in cases.items():
            with self.subTest(label=label), self.assertRaises(manifest.ManifestError):
                manifest.verify_manifest(record, candidate=CANDIDATE, baseline_guard_digest=guard)


if __name__ == "__main__":
    unittest.main()
