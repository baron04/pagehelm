"""PostgreSQL fixture capture and explicit snapshot recovery for Twenty.

All commands execute in a separately provisioned local synthetic-data container.  The
module never reads product credentials and never exposes database command output in errors.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .evaluator import (ARTIFACT_VERSION, EvaluationInputError, digest_json,
                        make_state, validate_fixture, validate_state)


class FixtureUnavailable(RuntimeError):
    pass


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class CommandRunner(Protocol):
    def run(self, argv: list[str], *, input_bytes: bytes | None = None,
            timeout_s: int) -> bytes: ...


class SubprocessRunner:
    def run(self, argv: list[str], *, input_bytes: bytes | None = None,
            timeout_s: int) -> bytes:
        try:
            process = subprocess.run(argv, input=input_bytes, capture_output=True,
                                     timeout=timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FixtureUnavailable("fixture command did not complete") from exc
        if process.returncode != 0:
            raise FixtureUnavailable("fixture command failed; output withheld")
        return process.stdout


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: dict) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                    allow_nan=False) + "\n").encode("utf-8"),
    )


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationInputError("JSON artifact is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise EvaluationInputError("JSON artifact must be an object")
    return value


def qualify_fixture(fixture: "TwentyPostgresFixture") -> dict:
    """Check that a fixture is stable and can produce a schema reset snapshot."""
    before = fixture.capture(role="baseline")
    dump = fixture.dump()
    after = fixture.capture(role="observation")
    if before["digest"] != after["digest"]:
        raise EvaluationInputError("fixture changed while qualification was running")
    projection = before["projection"]
    return {
        "artifact_version": ARTIFACT_VERSION,
        "kind": "twenty_fixture_qualification",
        "status": "READY",
        "fixture": validate_fixture(fixture.identity),
        "baseline_digest": before["digest"],
        "counts": {
            "companies": len(projection["companies"]),
            "people": projection["guards"]["people"]["row_count"],
            "opportunities": projection["guards"]["opportunities"]["row_count"],
        },
        "snapshot": {
            "bytes": len(dump),
            "sha256": "sha256:" + hashlib.sha256(dump).hexdigest(),
        },
        "qualified_at": utc_now(),
    }


@dataclass(frozen=True)
class TwentyPostgresFixture:
    container: str
    database: str
    db_user: str
    workspace_schema: str
    runner: CommandRunner = field(default_factory=SubprocessRunner)

    def __post_init__(self) -> None:
        validate_fixture(self.identity)

    @property
    def identity(self) -> dict[str, str]:
        return {
            "container": self.container,
            "database": self.database,
            "db_user": self.db_user,
            "workspace_schema": self.workspace_schema,
        }

    def _postgres(self, program: str, *args: str, input_bytes: bytes | None = None,
                  timeout_s: int) -> bytes:
        return self.runner.run(
            ["docker", "exec", "-i", self.container, program,
             "-U", self.db_user, "-d", self.database, *args],
            input_bytes=input_bytes, timeout_s=timeout_s,
        )

    def capture(self, *, role: str) -> dict:
        schema = f'"{self.workspace_schema}"'
        sql = f'''\
SELECT json_build_object(
  'section', 'companies',
  'rows', COALESCE(json_agg(row_to_json(c) ORDER BY c.id), '[]'::json)
)
FROM (
  SELECT id::text, name, "domainNamePrimaryLinkUrl" AS domain,
         "createdAt"::text AS created_at, "updatedAt"::text AS updated_at,
         "deletedAt"::text AS deleted_at
  FROM {schema}.company
) AS c;
SELECT json_build_object(
  'section', 'people',
  'rows', COALESCE(jsonb_agg(to_jsonb(p) ORDER BY p.id::text), '[]'::jsonb)
)
FROM {schema}.person AS p;
SELECT json_build_object(
  'section', 'opportunities',
  'rows', COALESCE(jsonb_agg(to_jsonb(o) ORDER BY o.id::text), '[]'::jsonb)
)
FROM {schema}.opportunity AS o;
'''
        raw = self._postgres(
            "psql", "--no-psqlrc", "--tuples-only", "--no-align",
            "--set", "ON_ERROR_STOP=1", input_bytes=sql.encode("utf-8"), timeout_s=45,
        )
        try:
            decoded = raw.decode("utf-8")
            rows = [json.loads(line) for line in decoded.splitlines() if line.strip()]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FixtureUnavailable("fixture returned invalid JSON") from exc
        sections: dict[str, list] = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"section", "rows"} \
                    or row.get("section") in sections or not isinstance(row.get("rows"), list):
                raise FixtureUnavailable("fixture returned an invalid business projection")
            sections[row["section"]] = row["rows"]
        if set(sections) != {"companies", "people", "opportunities"}:
            raise FixtureUnavailable("fixture business projection is incomplete")
        guards = {
            name: {"row_count": len(sections[name]), "digest": digest_json(sections[name])}
            for name in ("people", "opportunities")
        }
        return make_state(
            fixture=self.identity, companies=sections["companies"], guards=guards,
            captured_at=utc_now(), role=role,
        )

    def dump(self) -> bytes:
        dump = self._postgres(
            "pg_dump", "--format=custom", f"--schema={self.workspace_schema}",
            "--no-owner", "--no-privileges",
            timeout_s=180,
        )
        if not dump.startswith(b"PGDMP"):
            raise FixtureUnavailable("fixture snapshot is not a PostgreSQL custom dump")
        return dump

    def restore(self, dump: bytes) -> None:
        if not dump.startswith(b"PGDMP"):
            raise EvaluationInputError("snapshot is not a PostgreSQL custom dump")
        self._postgres(
            "pg_restore", "--clean", "--if-exists",
            f"--schema={self.workspace_schema}", "--no-owner", "--no-privileges",
            "--exit-on-error", input_bytes=dump, timeout_s=300,
        )


def _record_digest(record: dict) -> str:
    return digest_json({key: value for key, value in record.items() if key != "record_digest"})


def _sealed_record(value: dict) -> dict:
    record = dict(value)
    record["record_digest"] = _record_digest(record)
    return record


def _validate_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise EvaluationInputError(f"{field_name} is invalid")
    return value


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise EvaluationInputError("recovery record updated_at is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvaluationInputError("recovery record updated_at is invalid") from exc
    if parsed.tzinfo is None:
        raise EvaluationInputError("recovery record updated_at must include a timezone")
    return value


def _validated_ready_baseline(value: dict, fixture: TwentyPostgresFixture,
                              expected_digest: str) -> dict:
    normalized = validate_state(value, expected_role="baseline")
    if normalized["status"] != "READY":
        raise EvaluationInputError("a ready baseline artifact is required")
    if normalized["fixture"] != validate_fixture(fixture.identity):
        raise EvaluationInputError("baseline identifies a different fixture")
    if normalized["digest"] != expected_digest:
        raise EvaluationInputError("explicit expected digest does not match the baseline")
    return value


def prepare_snapshot(*, fixture: TwentyPostgresFixture, baseline: dict,
                     expected_digest: str, snapshot_path: Path,
                     record_path: Path) -> dict:
    """Create a recovery dump only while the fixture still matches the baseline."""
    _validated_ready_baseline(baseline, fixture, expected_digest)
    if snapshot_path.resolve() == record_path.resolve():
        raise EvaluationInputError("snapshot and recovery record must be separate files")
    if snapshot_path.exists() or record_path.exists():
        raise EvaluationInputError("snapshot and recovery record paths must be new")
    before = fixture.capture(role="observation")
    if before["digest"] != expected_digest:
        raise EvaluationInputError("fixture no longer matches the expected baseline")
    dump = fixture.dump()
    after = fixture.capture(role="observation")
    if after["digest"] != expected_digest:
        raise EvaluationInputError("fixture changed while the snapshot was created")
    atomic_write_bytes(snapshot_path, dump)
    record = _sealed_record({
        "artifact_version": ARTIFACT_VERSION,
        "kind": "twenty_recovery",
        "status": "READY",
        "fixture": validate_fixture(fixture.identity),
        "baseline_digest": expected_digest,
        "snapshot": {
            "sha256": "sha256:" + hashlib.sha256(dump).hexdigest(),
            "bytes": len(dump),
        },
        "updated_at": utc_now(),
    })
    atomic_write_json(record_path, record)
    return record


def _validated_record(value: dict, fixture: TwentyPostgresFixture,
                      expected_digest: str, dump: bytes) -> dict:
    status = value.get("status")
    expected_fields = {"artifact_version", "kind", "status", "fixture",
                       "baseline_digest", "snapshot", "updated_at", "record_digest"}
    if status in {"RESTORED", "FAILED"}:
        expected_fields.add("observed_digest")
    if set(value) != expected_fields:
        raise EvaluationInputError("recovery record has unexpected fields")
    if value.get("artifact_version") != ARTIFACT_VERSION \
            or value.get("kind") != "twenty_recovery" \
            or status not in {"READY", "RESTORING", "RESTORED", "FAILED", "INDETERMINATE"}:
        raise EvaluationInputError("recovery record header is invalid")
    _validate_timestamp(value.get("updated_at"))
    _validate_digest(value.get("record_digest"), "recovery record digest")
    if value.get("record_digest") != _record_digest(value):
        raise EvaluationInputError("recovery record digest is invalid")
    if value.get("fixture") != validate_fixture(fixture.identity):
        raise EvaluationInputError("recovery record identifies a different fixture")
    baseline_digest = _validate_digest(
        value.get("baseline_digest"), "recovery baseline digest")
    if baseline_digest != expected_digest:
        raise EvaluationInputError("explicit expected digest does not match the recovery record")
    snapshot = value.get("snapshot")
    actual_sha = "sha256:" + hashlib.sha256(dump).hexdigest()
    if not isinstance(snapshot, dict) or set(snapshot) != {"sha256", "bytes"}:
        raise EvaluationInputError("recovery snapshot metadata is invalid")
    snapshot_sha = _validate_digest(snapshot.get("sha256"), "recovery snapshot digest")
    snapshot_bytes = snapshot.get("bytes")
    if type(snapshot_bytes) is not int or snapshot_bytes < 5:
        raise EvaluationInputError("recovery snapshot byte count is invalid")
    if snapshot_sha != actual_sha or snapshot_bytes != len(dump):
        raise EvaluationInputError("explicit snapshot does not match the recovery record")
    if status in {"RESTORED", "FAILED"}:
        _validate_digest(value.get("observed_digest"), "recovery observed digest")
    return value


def restore_snapshot(*, fixture: TwentyPostgresFixture, expected_digest: str,
                     snapshot_path: Path, record_path: Path) -> dict:
    """Restore once, recording uncertainty atomically before the database write."""
    if snapshot_path.resolve() == record_path.resolve():
        raise EvaluationInputError("snapshot and recovery record must be separate files")
    try:
        dump = snapshot_path.read_bytes()
    except OSError as exc:
        raise EvaluationInputError("explicit snapshot is unavailable") from exc
    record = _validated_record(load_json(record_path), fixture, expected_digest, dump)
    if record["status"] in {"RESTORING", "INDETERMINATE"}:
        return {"artifact_version": ARTIFACT_VERSION, "kind": "twenty_restore",
                "status": "INDETERMINATE", "reason": "previous_restore_uncertain"}
    if record["status"] == "FAILED":
        return {"artifact_version": ARTIFACT_VERSION, "kind": "twenty_restore",
                "status": "FAIL", "reason": "previous_restore_failed",
                "expected_digest": expected_digest,
                "observed_digest": record["observed_digest"]}
    if record["status"] == "RESTORED":
        raise EvaluationInputError("recovery record has already been consumed")
    active = _sealed_record({**record, "status": "RESTORING", "updated_at": utc_now()})
    atomic_write_json(record_path, active)
    try:
        fixture.restore(dump)
        actual = fixture.capture(role="observation")
    except (FixtureUnavailable, EvaluationInputError):
        failed = _sealed_record({**active, "status": "INDETERMINATE",
                                 "updated_at": utc_now()})
        atomic_write_json(record_path, failed)
        return {"artifact_version": ARTIFACT_VERSION, "kind": "twenty_restore",
                "status": "INDETERMINATE", "reason": "restore_outcome_uncertain"}
    passed = actual["digest"] == expected_digest
    final_status = "RESTORED" if passed else "FAILED"
    final = _sealed_record({**active, "status": final_status, "updated_at": utc_now(),
                            "observed_digest": actual["digest"]})
    atomic_write_json(record_path, final)
    return {
        "artifact_version": ARTIFACT_VERSION,
        "kind": "twenty_restore",
        "status": "PASS" if passed else "FAIL",
        "expected_digest": expected_digest,
        "observed_digest": actual["digest"],
    }
