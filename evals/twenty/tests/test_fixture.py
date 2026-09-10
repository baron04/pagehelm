from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from evals.twenty.evaluator import EvaluationInputError, digest_json
from evals.twenty.fixture import (FixtureUnavailable, TwentyPostgresFixture,
                                  load_json, prepare_snapshot, qualify_fixture,
                                  restore_snapshot)


FIXTURE_ARGS = {
    "container": "twenty-test-db-1",
    "database": "default",
    "db_user": "postgres",
    "workspace_schema": "workspace_test",
}
COMPANY = {
    "id": "7e172546-a20e-4c7a-861c-a6478a944d37",
    "name": "Anthropic",
    "domain": "anthropic.com",
    "created_at": "2026-09-08T01:00:00+00:00",
    "updated_at": "2026-09-08T01:00:00+00:00",
    "deleted_at": None,
}
PERSON = {
    "id": "7a93d1e5-3f74-4945-8a65-d7f996083f72", "name": "Ivan Zhao",
    "email": "zhao@example.com", "company_id": COMPANY["id"],
    "created_at": "2026-09-08T01:00:00+00:00",
    "updated_at": "2026-09-08T01:00:00+00:00", "deleted_at": None,
}
OPPORTUNITY = {
    "id": "2beb07b0-340c-41d7-be33-5aa91757f329", "name": "API Deal",
    "stage": "SCREENING", "amount_micros": "75000000000", "currency": "USD",
    "company_id": COMPANY["id"], "created_at": "2026-09-08T01:00:00+00:00",
    "updated_at": "2026-09-08T01:00:00+00:00", "deleted_at": None,
}


def capture_output() -> bytes:
    return ("\n".join(json.dumps(row, sort_keys=True) for row in (
        {"section": "companies", "rows": [COMPANY]},
        {"section": "people", "rows": [{"id": "person-1", "name": "Synthetic"}]},
        {"section": "opportunities", "rows": []},
    )) + "\n").encode()


class FakeRunner:
    def __init__(self, outputs: list[bytes | Exception]):
        self.outputs = list(outputs)
        self.calls: list[tuple[list[str], bytes | None, int]] = []

    def run(self, argv: list[str], *, input_bytes: bytes | None, timeout_s: int) -> bytes:
        self.calls.append((argv, input_bytes, timeout_s))
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


class TwentyFixtureTests(unittest.TestCase):
    def test_breadth_capture_projects_all_three_collections_with_selected_fields(self):
        output = ("\n".join(json.dumps(row, sort_keys=True) for row in (
            {"section": "companies", "rows": [COMPANY]},
            {"section": "people", "rows": [PERSON]},
            {"section": "opportunities", "rows": [OPPORTUNITY]},
        )) + "\n").encode()
        runner = FakeRunner([output])
        state = TwentyPostgresFixture(**FIXTURE_ARGS, runner=runner).capture_breadth(
            role="baseline")
        self.assertEqual(state["projection"]["people"], [PERSON])
        self.assertEqual(state["projection"]["opportunities"], [OPPORTUNITY])
        sql = runner.calls[0][1].decode()
        self.assertIn('"emailsPrimaryEmail"', sql)
        self.assertIn('"amountAmountMicros"::text', sql)
        self.assertNotIn("emailsAdditionalEmails", sql)

    def test_qualification_proves_stable_capture_and_reset_dump_without_raw_guards(self):
        dump = b"PGDMP\x00synthetic-dump"
        runner = FakeRunner([capture_output(), dump, capture_output()])
        result = qualify_fixture(TwentyPostgresFixture(**FIXTURE_ARGS, runner=runner))

        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["counts"], {
            "companies": 1, "people": 1, "opportunities": 0,
        })
        self.assertEqual(result["snapshot"]["bytes"], len(dump))
        self.assertNotIn("Synthetic", json.dumps(result))
        self.assertEqual([call[0][4] for call in runner.calls],
                         ["psql", "pg_dump", "psql"])

    def test_qualification_rejects_a_fixture_that_changes_during_dump(self):
        changed = json.loads(capture_output().decode().splitlines()[0])
        changed["rows"][0]["name"] = "Changed"
        changed_output = ("\n".join((
            json.dumps(changed, sort_keys=True),
            json.dumps({"section": "people", "rows": [
                {"id": "person-1", "name": "Synthetic"}],}, sort_keys=True),
            json.dumps({"section": "opportunities", "rows": []}, sort_keys=True),
        )) + "\n").encode()
        runner = FakeRunner([capture_output(), b"PGDMP\x00synthetic-dump", changed_output])

        with self.assertRaisesRegex(EvaluationInputError, "fixture changed"):
            qualify_fixture(TwentyPostgresFixture(**FIXTURE_ARGS, runner=runner))

    def test_capture_uses_explicit_identity_and_persists_only_guard_digests(self):
        runner = FakeRunner([capture_output()])
        fixture = TwentyPostgresFixture(**FIXTURE_ARGS, runner=runner)
        state = fixture.capture(role="baseline")

        argv, sql, _ = runner.calls[0]
        self.assertEqual(argv[:5], ["docker", "exec", "-i", "twenty-test-db-1", "psql"])
        self.assertIn("-U", argv)
        self.assertIn("postgres", argv)
        self.assertIn('FROM "workspace_test".company', sql.decode())
        self.assertNotIn('"deletedAt" IS NULL', sql.decode())
        self.assertEqual(state["status"], "READY")
        self.assertEqual(state["projection"]["companies"], [COMPANY])
        self.assertEqual(state["projection"]["guards"]["people"]["row_count"], 1)
        self.assertNotIn("Synthetic", json.dumps(state))

    def test_snapshot_and_restore_are_bound_to_explicit_digest_and_atomic_record(self):
        dump = b"PGDMP\x00synthetic-dump"
        runner = FakeRunner([capture_output(), dump, capture_output(), b"", capture_output()])
        fixture = TwentyPostgresFixture(**FIXTURE_ARGS, runner=runner)
        baseline = fixture.capture(role="baseline")
        # Put the consumed baseline capture back into the snapshot workflow's sequence.
        runner.outputs.insert(0, capture_output())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "baseline.dump"
            record_path = root / "recovery.json"
            record = prepare_snapshot(
                fixture=fixture, baseline=baseline, expected_digest=baseline["digest"],
                snapshot_path=snapshot, record_path=record_path,
            )
            self.assertEqual(record["status"], "READY")
            self.assertEqual(snapshot.read_bytes(), dump)
            self.assertEqual(os.stat(snapshot).st_mode & 0o777, 0o600)
            self.assertEqual(record["snapshot"]["sha256"],
                             "sha256:" + hashlib.sha256(dump).hexdigest())

            result = restore_snapshot(
                fixture=fixture, expected_digest=baseline["digest"],
                snapshot_path=snapshot, record_path=record_path,
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(load_json(record_path)["status"], "RESTORED")
            programs = [call[0][4] for call in runner.calls]
            self.assertIn("pg_dump", programs)
            self.assertIn("pg_restore", programs)
            dump_argv = next(call[0] for call in runner.calls if call[0][4] == "pg_dump")
            restore_argv = next(
                call[0] for call in runner.calls if call[0][4] == "pg_restore")
            self.assertIn("--schema=workspace_test", dump_argv)
            self.assertIn("--schema=workspace_test", restore_argv)

    def test_snapshot_refuses_a_stale_or_implicit_expectation(self):
        fixture = TwentyPostgresFixture(**FIXTURE_ARGS, runner=FakeRunner([]))
        baseline_runner = FakeRunner([capture_output()])
        baseline = TwentyPostgresFixture(**FIXTURE_ARGS, runner=baseline_runner).capture(
            role="baseline")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(EvaluationInputError):
                prepare_snapshot(
                    fixture=fixture, baseline=baseline,
                    expected_digest="sha256:" + "0" * 64,
                    snapshot_path=root / "state.dump", record_path=root / "record.json",
                )

    def test_restore_failure_is_recorded_as_indeterminate_and_not_retried(self):
        dump = b"PGDMP\x00synthetic-dump"
        setup_runner = FakeRunner([capture_output(), dump, capture_output()])
        fixture = TwentyPostgresFixture(**FIXTURE_ARGS, runner=setup_runner)
        baseline = fixture.capture(role="baseline")
        setup_runner.outputs.insert(0, capture_output())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "state.dump"
            record_path = root / "record.json"
            prepare_snapshot(
                fixture=fixture, baseline=baseline, expected_digest=baseline["digest"],
                snapshot_path=snapshot, record_path=record_path,
            )
            failing_runner = FakeRunner([FixtureUnavailable("failed")])
            failing_fixture = TwentyPostgresFixture(**FIXTURE_ARGS, runner=failing_runner)
            result = restore_snapshot(
                fixture=failing_fixture, expected_digest=baseline["digest"],
                snapshot_path=snapshot, record_path=record_path,
            )
            self.assertEqual(result["status"], "INDETERMINATE")
            self.assertEqual(load_json(record_path)["status"], "INDETERMINATE")
            self.assertEqual(len(failing_runner.calls), 1)
            result = restore_snapshot(
                fixture=failing_fixture, expected_digest=baseline["digest"],
                snapshot_path=snapshot, record_path=record_path,
            )
            self.assertEqual(result["status"], "INDETERMINATE")
            self.assertEqual(len(failing_runner.calls), 1)

    def test_invalid_workspace_and_snapshot_record_are_rejected_before_writing(self):
        for field_name, invalid in (
                ("container", "--privileged"),
                ("database", "default;drop"),
                ("db_user", "postgres user"),
                ("workspace_schema", "workspace_bad;drop")):
            with self.subTest(field_name=field_name):
                with self.assertRaises(EvaluationInputError):
                    TwentyPostgresFixture(**{**FIXTURE_ARGS, field_name: invalid})

        runner = FakeRunner([capture_output()])
        baseline = TwentyPostgresFixture(**FIXTURE_ARGS, runner=runner).capture(role="baseline")
        self.assertEqual(baseline["projection"]["guards"]["opportunities"]["digest"],
                         digest_json([]))

    def test_recovery_record_fields_are_strictly_validated_before_restore(self):
        dump = b"PGDMP\x00synthetic-dump"
        setup_runner = FakeRunner([capture_output(), dump, capture_output()])
        fixture = TwentyPostgresFixture(**FIXTURE_ARGS, runner=setup_runner)
        baseline = fixture.capture(role="baseline")
        setup_runner.outputs.insert(0, capture_output())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "state.dump"
            record_path = root / "record.json"
            original = prepare_snapshot(
                fixture=fixture, baseline=baseline, expected_digest=baseline["digest"],
                snapshot_path=snapshot, record_path=record_path,
            )
            cases = {
                "updated_at": lambda row: row.update(updated_at=7),
                "baseline_digest": lambda row: row.update(baseline_digest="sha256:bad"),
                "snapshot_sha": lambda row: row["snapshot"].update(sha256="sha256:bad"),
                "snapshot_bytes": lambda row: row["snapshot"].update(bytes=True),
            }
            no_io = FakeRunner([])
            validator = TwentyPostgresFixture(**FIXTURE_ARGS, runner=no_io)
            for name, mutate in cases.items():
                with self.subTest(name=name):
                    changed = copy.deepcopy(original)
                    mutate(changed)
                    changed["record_digest"] = digest_json({
                        key: value for key, value in changed.items()
                        if key != "record_digest"
                    })
                    record_path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaises(EvaluationInputError):
                        restore_snapshot(
                            fixture=validator, expected_digest=baseline["digest"],
                            snapshot_path=snapshot, record_path=record_path,
                        )
            self.assertEqual(no_io.calls, [])


if __name__ == "__main__":
    unittest.main()
