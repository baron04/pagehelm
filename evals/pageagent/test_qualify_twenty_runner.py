from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.pageagent.qualify_twenty_runner import qualify
from evals.twenty.evaluator import make_state


_DIGEST = "sha256:" + "a" * 64
_COMPANIES = [{
    "id": "f45ee421-8a3e-4aa5-a1cf-7207cc6754e1",
    "name": "Synthetic Acme",
    "domain": "acme.test",
    "created_at": "2026-09-09T00:00:00+00:00",
    "updated_at": "2026-09-09T00:00:00+00:00",
    "deleted_at": None,
}]


def _state(role: str) -> dict:
    return make_state(
        fixture={
            "container": "fixture-db", "database": "fixture",
            "db_user": "postgres", "workspace_schema": "workspace_fixture",
        },
        companies=_COMPANIES,
        guards={
            "people": {"row_count": 0, "digest": _DIGEST},
            "opportunities": {"row_count": 0, "digest": _DIGEST},
        },
        captured_at="2026-09-09T00:00:00+00:00", role=role)


class _Fixture:
    def capture(self, *, role: str) -> dict:
        return _state(role)


class LiveQualificationContractTests(unittest.TestCase):
    def test_query_and_open_use_real_evaluator_with_schema_fixed_output(self):
        taskset = json.loads((
            Path(__file__).resolve().parents[1] / "twenty" / "tasksets"
            / "twenty-companies-development-v1.json").read_text(encoding="utf-8"))
        calls = []

        def fake_run_case(*, case, client, run_id, **_kwargs):
            calls.append((case["evaluator_task"]["kind"], len(client.responses)))
            task = case["evaluator_task"]
            if task["kind"] == "query":
                evidence = {
                    "artifact_version": 1, "execution_status": "completed",
                    "complete": True,
                    "companies": [{"name": row["name"], "domain": row["domain"]}
                                  for row in _COMPANIES],
                }
            else:
                target = _COMPANIES[0]
                evidence = {
                    "artifact_version": 1, "execution_status": "completed",
                    "page": {
                        "path": f"/object/company/{target['id']}",
                        "name": target["name"], "domain": target["domain"],
                    },
                }
            return {
                "status": "completed", "evidence": evidence,
                "session_binding_unchanged": True,
                "run_digest": "sha256:" + ("1" if task["kind"] == "query" else "2") * 64,
                "trial": {"accounting": {
                    "usage_unavailable_calls": 0, "total_cost_cny": 0.0,
                }},
            }

        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "pageagent.js"
            bundle.write_bytes(b"qualified bundle")
            with patch(
                    "evals.pageagent.qualify_twenty_runner.run_case",
                    side_effect=fake_run_case):
                result = qualify(
                    fixture=_Fixture(), adapter=object(),
                    storage_state_path=Path(directory) / "state.json",
                    bundle_path=bundle, development_taskset=taskset)

        self.assertEqual(result["status"], "READY")
        self.assertEqual(calls, [("query", 2), ("open", 3)])
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(set(result["case_checks"]), {"query", "open"})
        rendered = json.dumps(result)
        self.assertNotIn("Synthetic Acme", rendered)
        self.assertNotIn("acme.test", rendered)


if __name__ == "__main__":
    unittest.main()
