"""The recovery protocol of the Odoo live fixture, without a database.

The `docker exec` boundary is faked here so the protocol itself can be tested: what is
recorded, in which order, and what a half-finished restore looks like to the next caller.
Whether `pg_restore` works is the live gate's business; whether an interrupted restore can
be silently retried is this file's, and that is the property nobody discovers by hand.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent_odoo.demo.fixture import (BUSINESS_TABLES, FixtureMismatch,
                                                   FixtureUnavailable,
                                                   OdooPostgresFixture, load_json,
                                                   prepare_snapshot, restore_snapshot)
from web_operation_agent_odoo.demo.qualify import (SELF_WRITTEN_TABLES,
                                                   classify_fixture_drift)


class FakePostgres:
    """A container whose tables hold row counts and one content digest each."""

    def __init__(self, *, digest: str = "aaaa"):
        self.state = {table: (1, digest) for table in BUSINESS_TABLES}
        self.archive = b"PGDMP" + b"\x00" * 16
        self.calls: list[str] = []
        self.restores = 0
        self.restore_raises: Exception | None = None
        self.on_capture = None

    def run(self, argv, *, input_bytes=None, timeout_s):
        program = argv[4]
        self.calls.append(program)
        if program == "psql":
            sql = argv[-1]
            if "nextval" in sql:
                return b"1\n"
            if self.on_capture is not None:
                self.on_capture()
            return "".join(f"{table} {rows} {digest}\n"
                           for table, (rows, digest) in sorted(self.state.items())
                           ).encode("utf-8")
        if program == "pg_dump":
            return self.archive
        if program == "pg_restore":
            self.restores += 1
            if self.restore_raises is not None:
                raise self.restore_raises
            assert input_bytes == self.archive
            self.state = {table: (1, "aaaa") for table in BUSINESS_TABLES}
            return b""
        raise AssertionError(program)


def _fixture(postgres: FakePostgres) -> OdooPostgresFixture:
    return OdooPostgresFixture(container="odoo-db", database="woa", db_user="odoo",
                               runner=postgres)


class FixtureIdentity(unittest.TestCase):
    def test_only_simple_names_are_accepted(self):
        for bad in ("woa; drop database", "", "x" * 200, "a b"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "simple container/db/user"):
                    OdooPostgresFixture(container="c", database=bad, db_user="odoo")


class CapturingTheFixture(unittest.TestCase):
    def test_every_business_table_is_projected(self):
        postgres = FakePostgres()
        state = _fixture(postgres).capture()
        self.assertEqual(sorted(state["tables"]), sorted(BUSINESS_TABLES))
        self.assertTrue(state["digest"].startswith("sha256:"))

    def test_a_missing_table_is_unavailable_not_a_smaller_projection(self):
        postgres = FakePostgres()
        del postgres.state["sale_order"]
        with self.assertRaisesRegex(FixtureUnavailable, "incomplete"):
            _fixture(postgres).capture()

    def test_a_changed_row_changes_the_digest(self):
        postgres = FakePostgres()
        before = _fixture(postgres).capture()["digest"]
        postgres.state["sale_order"] = (1, "bbbb")
        self.assertNotEqual(_fixture(postgres).capture()["digest"], before)


class TakingASnapshot(unittest.TestCase):
    def test_a_fixture_that_moves_during_the_dump_is_refused(self):
        postgres = FakePostgres()
        seen = {"count": 0}

        def drift():
            seen["count"] += 1
            if seen["count"] == 2:          # the re-read after pg_dump
                postgres.state["res_partner"] = (2, "cccc")

        postgres.on_capture = drift
        with self.assertRaisesRegex(FixtureUnavailable, "changed while its snapshot"):
            _fixture(postgres).dump()

    def test_the_record_carries_the_baseline_and_the_archive_identity(self):
        postgres = FakePostgres()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = prepare_snapshot(fixture=_fixture(postgres),
                                      snapshot_path=root / "snap.pgdump",
                                      record_path=root / "record.json")
        self.assertEqual(record["status"], "READY")
        self.assertEqual(record["snapshot"]["bytes"], len(postgres.archive))
        self.assertEqual(sorted(record["baseline_tables"]), sorted(BUSINESS_TABLES))


class RestoringTheFixture(unittest.TestCase):
    def _prepared(self, postgres, root):
        prepare_snapshot(fixture=_fixture(postgres), snapshot_path=root / "snap.pgdump",
                         record_path=root / "record.json")
        return {"fixture": _fixture(postgres), "snapshot_path": root / "snap.pgdump",
                "record_path": root / "record.json"}

    def test_a_written_fixture_comes_back_to_its_baseline(self):
        postgres = FakePostgres()
        with tempfile.TemporaryDirectory() as tmp:
            arguments = self._prepared(postgres, Path(tmp))
            postgres.state["res_partner"] = (2, "written")
            outcome = restore_snapshot(**arguments)
        self.assertEqual(outcome["status"], "PASS")
        self.assertEqual(outcome["changed_tables"], [])
        self.assertEqual(postgres.restores, 1)

    def test_the_server_is_told_the_database_changed_under_it(self):
        """A restored database the running Odoo has cached over is a flaky gate, not a pass."""
        postgres = FakePostgres()
        with tempfile.TemporaryDirectory() as tmp:
            arguments = self._prepared(postgres, Path(tmp))
            postgres.calls.clear()
            restore_snapshot(**arguments)
        after_restore = postgres.calls[postgres.calls.index("pg_restore"):]
        self.assertIn("psql", after_restore,
                      "nothing invalidated the registry after the restore")

    def test_a_restore_that_does_not_reach_the_baseline_fails_loudly(self):
        postgres = FakePostgres()
        with tempfile.TemporaryDirectory() as tmp:
            arguments = self._prepared(postgres, Path(tmp))
            postgres.state = {table: (1, "aaaa") for table in BUSINESS_TABLES}
            postgres.state["sale_order"] = (9, "not-the-baseline")
            original = postgres.run

            def restore_without_effect(argv, *, input_bytes=None, timeout_s):
                if argv[4] == "pg_restore":
                    postgres.calls.append("pg_restore")
                    postgres.restores += 1
                    return b""
                return original(argv, input_bytes=input_bytes, timeout_s=timeout_s)

            postgres.run = restore_without_effect
            outcome = restore_snapshot(**arguments)
            record = load_json(Path(tmp) / "record.json")
        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["changed_tables"], ["sale_order"])
        self.assertEqual(record["status"], "FAILED")

    def test_an_interrupted_restore_is_indeterminate_and_stays_that_way(self):
        """The uncertainty is written before the database write, so a crash is visible."""
        postgres = FakePostgres()
        postgres.restore_raises = FixtureUnavailable("pg_restore died")
        with tempfile.TemporaryDirectory() as tmp:
            arguments = self._prepared(postgres, Path(tmp))
            first = restore_snapshot(**arguments)
            record = load_json(Path(tmp) / "record.json")
            postgres.restore_raises = None
            second = restore_snapshot(**arguments)
        self.assertEqual(first["status"], "INDETERMINATE")
        self.assertEqual(record["status"], "INDETERMINATE")
        self.assertEqual(second["status"], "INDETERMINATE",
                         "an uncertain restore must not be retried on its own")
        self.assertEqual(postgres.restores, 1)

    def test_a_record_for_another_fixture_is_refused(self):
        postgres = FakePostgres()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arguments = self._prepared(postgres, root)
            arguments["fixture"] = OdooPostgresFixture(
                container="odoo-db", database="other", db_user="odoo", runner=postgres)
            with self.assertRaisesRegex(FixtureMismatch, "different fixture"):
                restore_snapshot(**arguments)

    def test_an_edited_record_is_refused(self):
        postgres = FakePostgres()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arguments = self._prepared(postgres, root)
            record = load_json(root / "record.json")
            record["baseline_digest"] = "sha256:" + "0" * 64
            (root / "record.json").write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(FixtureMismatch, "record digest is invalid"):
                restore_snapshot(**arguments)

    def test_a_snapshot_that_is_not_its_record_is_refused(self):
        postgres = FakePostgres()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arguments = self._prepared(postgres, root)
            (root / "snap.pgdump").write_bytes(b"PGDMP" + b"\xff" * 32)
            with self.assertRaisesRegex(FixtureMismatch, "does not match its recovery record"):
                restore_snapshot(**arguments)


if __name__ == "__main__":
    unittest.main()


class ClassifyingDriftBeforeTheGateRuns(unittest.TestCase):
    """Who moved the library decides whether the gate may proceed.

    A *running* Odoo writes one row to `mail_message` about twenty seconds after a restore
    and nothing else for at least ten minutes -- measured, not assumed. Blocking on that
    means the gate can only ever run once per restore, which is how it came to be reported
    BLOCKED on a library nobody had written to. Blocking on anything else is the point of
    the check, and these tests are what keeps the difference from eroding.
    """

    @staticmethod
    def _record(tables: dict) -> dict:
        return {"baseline_digest": "sha256:base", "baseline_tables": tables}

    @staticmethod
    def _captured(tables: dict) -> dict:
        return {"digest": "sha256:now", "tables": tables}

    def _baseline(self) -> dict:
        return {table: {"rows": 1, "digest": "aaaa"} for table in BUSINESS_TABLES}

    def test_a_library_on_its_baseline_has_nothing_to_attribute(self):
        tables = self._baseline()
        drifted, attributable = classify_fixture_drift(
            self._captured(tables), self._record(tables))
        self.assertEqual(drifted, [])
        self.assertFalse(attributable)

    def test_the_application_writing_to_itself_is_attributable(self):
        tables = self._baseline()
        moved = dict(tables, mail_message={"rows": 2, "digest": "bbbb"})
        drifted, attributable = classify_fixture_drift(
            self._captured(moved), self._record(tables))
        self.assertEqual(drifted, ["mail_message"])
        self.assertTrue(attributable)

    def test_a_business_table_that_moved_is_never_attributable(self):
        tables = self._baseline()
        for table in sorted(set(BUSINESS_TABLES) - SELF_WRITTEN_TABLES):
            with self.subTest(table=table):
                moved = dict(tables)
                moved[table] = {"rows": 2, "digest": "bbbb"}
                drifted, attributable = classify_fixture_drift(
                    self._captured(moved), self._record(tables))
                self.assertEqual(drifted, [table])
                self.assertFalse(attributable)

    def test_self_written_drift_alongside_a_real_write_still_stops_the_gate(self):
        """The set is a subset test, not a filter: one foreign table poisons the batch."""
        tables = self._baseline()
        moved = dict(tables,
                     mail_message={"rows": 2, "digest": "bbbb"},
                     sale_order={"rows": 2, "digest": "cccc"})
        drifted, attributable = classify_fixture_drift(
            self._captured(moved), self._record(tables))
        self.assertEqual(drifted, ["mail_message", "sale_order"])
        self.assertFalse(attributable)

    def test_no_recorded_per_table_baseline_means_no_attribution(self):
        """An older record cannot be reasoned about, so it is never waved through."""
        drifted, attributable = classify_fixture_drift(
            self._captured(self._baseline()), {"baseline_digest": "sha256:base"})
        self.assertEqual(drifted, [])
        self.assertFalse(attributable)

    def test_every_self_written_table_is_one_the_projection_actually_covers(self):
        self.assertTrue(SELF_WRITTEN_TABLES)
        self.assertTrue(SELF_WRITTEN_TABLES <= set(BUSINESS_TABLES))
