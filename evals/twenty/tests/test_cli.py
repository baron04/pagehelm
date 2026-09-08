from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evals.twenty.evaluator import digest_json, make_state


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = {
    "container": "twenty-test-db-1",
    "database": "default",
    "db_user": "postgres",
    "workspace_schema": "workspace_test",
}


class TwentyCliTests(unittest.TestCase):
    def test_evaluate_runs_as_a_file_only_subprocess(self):
        guards = {
            "people": {"row_count": 0, "digest": digest_json([])},
            "opportunities": {"row_count": 0, "digest": digest_json([])},
        }
        before = make_state(
            fixture=FIXTURE, companies=[], guards=guards,
            captured_at="2026-09-08T00:00:00+00:00", role="baseline")
        after = make_state(
            fixture=FIXTURE, companies=[], guards=guards,
            captured_at="2026-09-08T00:01:00+00:00", role="observation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = {
                "task.json": {"artifact_version": 1, "kind": "query"},
                "before.json": before,
                "after.json": after,
                "evidence.json": {
                    "artifact_version": 1, "execution_status": "completed",
                    "complete": True, "companies": [],
                },
            }
            for name, value in artifacts.items():
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            output = root / "result.json"
            process = subprocess.run(
                [sys.executable, "-m", "evals.twenty", "evaluate",
                 "--task", str(root / "task.json"),
                 "--before", str(root / "before.json"),
                 "--after", str(root / "after.json"),
                 "--evidence", str(root / "evidence.json"),
                 "--output", str(output)],
                cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["status"], "PASS")
            self.assertEqual(json.loads(output.read_text())["status"], "PASS")

    def test_recovery_result_cannot_overwrite_the_recovery_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = root / "recovery.json"
            process = subprocess.run(
                [sys.executable, "-m", "evals.twenty", "restore",
                 "--container", "twenty-test-db-1", "--database", "default",
                 "--db-user", "postgres", "--workspace-schema", "workspace_test",
                 "--expected-digest", "sha256:" + "0" * 64,
                 "--snapshot", str(root / "snapshot.dump"),
                 "--record", str(record), "--output", str(record)],
                cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stdout)["status"], "INVALID")
            self.assertFalse(record.exists())


if __name__ == "__main__":
    unittest.main()
