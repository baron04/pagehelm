"""A holdout cannot combine different code, dates or fixture contents."""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.odoo.goals import binding
from evals.odoo.goals.verify_v4_pages import candidate_paths
from evals.odoo.goals.qualification import verify_qualification
from evals.odoo.goals.run_goals import load_tasks


class GoalBindingTests(unittest.TestCase):
    def test_docker_desktop_bind_mount_resolves_to_host_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory)
            desktop_path = "/host_mnt" + str(host)
            self.assertEqual(binding.host_mount_path(desktop_path), host)

    def test_reference_date_comes_from_isolated_application_clock(self):
        with patch.object(binding, "_output", return_value="2026-09-24") as output:
            self.assertEqual(binding.current_reference_date("isolated-app"), "2026-09-24")
        output.assert_called_once_with(
            ["docker", "exec", "isolated-app", "env", "TZ=Asia/Shanghai",
             "date", "+%Y-%m-%d"])

    def test_empty_or_dirty_checkout_is_not_a_candidate(self):
        with patch.object(binding, "_output", side_effect=[" M src/changed.py"]):
            with self.assertRaisesRegex(RuntimeError, "not clean"):
                binding.candidate_commit(Path("/candidate"))
        with patch.object(binding, "_output", side_effect=["", "abc123"]):
            self.assertEqual(binding.candidate_commit(Path("/candidate")), "abc123")

    def test_wrong_date_fails_before_fixture_or_model_is_touched(self):
        with patch.object(binding, "current_reference_date", return_value="2026-09-25"), \
             patch.object(binding, "fixture_signature") as fixture:
            with self.assertRaisesRegex(RuntimeError, "requires application date"):
                binding.make_binding(root=Path("/candidate"), task_digest="abc",
                                     reference_date="2026-09-24", db_container="db",
                                     database="odoo", app_container="app", source_release="release")
            fixture.assert_not_called()

    def test_runtime_must_import_from_the_candidate(self):
        root = Path(__file__).resolve().parents[3]
        binding.assert_runtime_checkout(root)
        with self.assertRaisesRegex(RuntimeError, "not imported"):
            binding.assert_runtime_checkout(Path("/unrelated-checkout"))

    def test_candidate_change_is_rejected_between_attempts(self):
        with patch.object(binding, "assert_runtime_checkout"), \
             patch.object(binding, "candidate_commit", return_value="new"):
            with self.assertRaisesRegex(RuntimeError, "candidate commit changed"):
                binding.recheck_checkout({"candidate_commit": "old"}, Path("/candidate"))

    def test_resume_rejects_a_different_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "attempts").mkdir()
            binding.bind_output(output, {"candidate_commit": "first"})
            self.assertEqual(json.loads((output / "run-binding.json").read_text()),
                             {"candidate_commit": "first"})
            binding.bind_output(output, {"candidate_commit": "first"})
            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                binding.bind_output(output, {"candidate_commit": "second"})

    def test_unbound_prior_attempts_are_not_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "attempts").mkdir()
            (output / "attempts" / "r1-one.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "unbound prior attempts"):
                binding.bind_output(output, {"candidate_commit": "first"})

    def test_recheck_rejects_fixture_drift(self):
        saved = {"reference_date": "2026-09-24", "fixture_signature": "old",
                 "application_container": "same"}
        with patch.object(binding, "current_reference_date", return_value="2026-09-24"), \
             patch.object(binding, "fixture_signature", return_value="new"):
            with self.assertRaisesRegex(RuntimeError, "database changed"):
                binding.recheck(saved, app_container="app", db_container="db", database="odoo")

    def test_recheck_rejects_a_changed_addon_mount(self):
        saved = {"reference_date": "2026-09-24", "fixture_signature": "same",
                 "application_container": "same", "addon_digest": "old"}
        with patch.object(binding, "current_reference_date", return_value="2026-09-24"), \
             patch.object(binding, "fixture_signature", return_value="same"), \
             patch.object(binding, "addon_digest", return_value="new"), \
             patch.object(binding, "_output", return_value="same"):
            with self.assertRaisesRegex(RuntimeError, "deployed addon changed"):
                binding.recheck(saved, app_container="app", db_container="db", database="odoo")

    def test_sealed_page_paths_expand_without_changing_oracles(self):
        self.assertEqual(candidate_paths(r"^/odoo/(customers|res\.partner)/15$"),
                         ["/odoo/customers/15", "/odoo/res.partner/15"])
        self.assertEqual(candidate_paths(r"^/odoo/sales$"), ["/odoo/sales"])

    def test_qualification_requires_all_sealed_oracles_and_matching_binding(self):
        tasks = load_tasks("v4")["tasks"]
        groups = {
            "answer": [t for t in tasks if t["category"] == "answer"],
            "page": [t for t in tasks if t["category"] in ("open", "handoff")],
            "limitation": [t for t in tasks if t["category"] == "impossible"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = {}
            for kind, group in groups.items():
                artifact = root / f"{kind}.json"
                artifact.write_text(json.dumps([{"id": t["id"], "ok": True} for t in group]))
                reports[kind] = {"path": str(artifact),
                                 "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
            qualification = root / "qualification.json"
            qualification.write_text(json.dumps({"schema_version": 1,
                                                 "binding": {"candidate_commit": "one"},
                                                 "reports": reports}))
            verify_qualification(qualification, binding={"candidate_commit": "one"},
                                 tasks=tasks)
            with self.assertRaisesRegex(RuntimeError, "different candidate"):
                verify_qualification(qualification, binding={"candidate_commit": "two"},
                                     tasks=tasks)
            (root / "answer.json").write_text("[]")
            with self.assertRaisesRegex(RuntimeError, "report changed"):
                verify_qualification(qualification, binding={"candidate_commit": "one"},
                                     tasks=tasks)


if __name__ == "__main__":
    unittest.main()
