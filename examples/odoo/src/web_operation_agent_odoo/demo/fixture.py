"""Snapshot and restore the Odoo fixture a live gate runs against.

A live gate that writes is only worth running on a database you can put back. Without that,
the first run that creates a customer or confirms a quotation changes what every later run
measures, and the drift is invisible: the gate still passes, against a different library
each time. G2's spike left exactly that behind -- a confirmed S00001, a filled picking and
a leftover partner -- and every conclusion after it rested on a moving baseline.

Three operations, in the order a gate uses them:

* ``capture`` reads a content digest per business table straight from Postgres, not through
  the adapter. Reading it through the adapter would only see what the three read tools
  expose, and the spike's damage was mostly *outside* that: a stock picking and a mail
  thread nobody queries. The digest covers whole rows, so a changed ``write_date`` is a
  changed fixture.
* ``dump`` takes a ``pg_dump`` custom archive, refusing to record one while the fixture is
  moving under it.
* ``restore`` puts the archive back and then bumps Odoo's own registry and cache signalling
  sequences. Without that last step the database is correct and the running server still
  serves what it had cached, which is the failure mode that looks like a flaky gate.

Deliberately not covered: the filestore under ``/var/lib/odoo``. Attachments written during
a run survive a restore as orphaned files. They are unreachable once the rows that named
them are gone, so they cannot change a later measurement, but they do accumulate; a gate
that starts exercising attachments needs this extended, not assumed.

The design follows ``evals/twenty/fixture.py`` on purpose -- same recovery-record protocol,
same "record the uncertainty before the write" ordering -- so that one habit covers both
applications.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


ARTIFACT_VERSION = 1

#: The tables whose whole rows are digested. Chosen as "what a business workflow can
#: touch", which is wider than "what a tool can read": confirming a quotation writes a
#: picking, a move, a mail message and an activity, and a fixture check that only looked at
#: `sale_order` would call that run read-only.
BUSINESS_TABLES = (
    "res_partner",
    "res_company",
    "res_users",
    "sale_order",
    "sale_order_line",
    "product_template",
    "product_product",
    "account_move",
    "account_move_line",
    "stock_picking",
    "stock_move",
    "stock_move_line",
    "mail_message",
    "mail_activity",
    "mail_followers",
    "ir_attachment",
)

#: Tables a *running* Odoo writes to on its own, with nothing else happening.
#:
#: Measured rather than assumed: after a restore of an idle server the digest holds for
#: about twenty seconds, then one row appears in `mail_message` and nothing moves again for
#: at least ten minutes. Sampled once a minute for ten minutes, no other table of the
#: sixteen ever differed.
#:
#: It lives here rather than with the gate because it qualifies `capture()`: it is the set a
#: caller must exclude when asking "has anything *else* changed". It relaxes only the check
#: that a library is on its baseline before a run. It does not relax what a run is measured
#: against -- the post-run comparison still uses the whole digest, and no gate case writes to
#: `mail_message` alone, so a run that wrote and failed to clean up is still caught by the
#: tables it could not avoid.
SELF_WRITTEN_TABLES = frozenset({"mail_message"})

#: Sequences Odoo polls to decide whether its in-process registry and caches are stale.
#: Bumping them after a restore is what makes the running server agree with the database.
_SIGNALLING = (
    "base_registry_signaling",
    "base_cache_signaling_default",
    "base_cache_signaling_assets",
    "base_cache_signaling_templates",
    "base_cache_signaling_routing",
    "base_cache_signaling_groups",
)

_NAME = re.compile(r"\A[A-Za-z0-9_.-]{1,120}\Z")


class FixtureUnavailable(RuntimeError):
    """The fixture could not be read or written; never reported as a passing gate."""


class FixtureMismatch(RuntimeError):
    """The fixture is not the one this artifact describes."""


class CommandRunner(Protocol):
    def run(self, argv: list[str], *, input_bytes: bytes | None = None,
            timeout_s: int) -> bytes: ...


class SubprocessRunner:
    def run(self, argv: list[str], *, input_bytes: bytes | None = None,
            timeout_s: int) -> bytes:
        try:
            done = subprocess.run(argv, input=input_bytes, capture_output=True,
                                  timeout=timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FixtureUnavailable(f"{argv[0]} did not complete") from exc
        if done.returncode != 0:
            # The application's own rows may appear in a Postgres error, so only the
            # command and its exit status cross this boundary.
            raise FixtureUnavailable(
                f"{' '.join(argv[:4])} exited {done.returncode}")
        return done.stdout


def digest_json(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    with open(temporary, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: dict) -> None:
    atomic_write_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         indent=2) + "\n").encode("utf-8"))


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureMismatch(f"{path.name} is unreadable") from exc
    if not isinstance(value, dict):
        raise FixtureMismatch(f"{path.name} is not a JSON object")
    return value


@dataclass(frozen=True)
class OdooPostgresFixture:
    """One local Odoo database, reachable through its container's Postgres client."""

    container: str
    database: str
    db_user: str
    runner: CommandRunner = field(default_factory=SubprocessRunner)

    def __post_init__(self) -> None:
        for name in (self.container, self.database, self.db_user):
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ValueError("fixture identity must be simple container/db/user names")

    @property
    def identity(self) -> dict[str, str]:
        return {"container": self.container, "database": self.database,
                "db_user": self.db_user}

    def _psql(self, sql: str, *, database: str = "", timeout_s: int = 120) -> str:
        return self.runner.run(
            ["docker", "exec", "-i", self.container, "psql", "-U", self.db_user,
             "-d", database or self.database, "-v", "ON_ERROR_STOP=1", "-tAc", sql],
            timeout_s=timeout_s).decode("utf-8", "replace")

    # -- reading ------------------------------------------------------------------------
    def present_tables(self) -> tuple[str, ...]:
        """Which of the tracked tables this deployment actually has.

        `BUSINESS_TABLES` covers any Odoo this integration might be pointed at, and no
        deployment has all of them: the reference fixture installs sales and accounting and
        not warehouse operations, so the three `stock_*` tables do not exist. Asking the
        catalogue rather than assuming is what lets one tracked list serve both, and the
        answer is recorded in the state so a module appearing or disappearing between a
        baseline and an observation is drift rather than a silent change of subject.
        """
        wanted = ", ".join(f"'{table}'" for table in BUSINESS_TABLES)
        raw = self._psql(
            "select table_name from information_schema.tables where table_schema = 'public' "
            f"and table_name in ({wanted}) order by table_name")
        return tuple(line.strip() for line in raw.splitlines() if line.strip())

    def capture(self) -> dict:
        """A per-table row count and whole-row digest, plus one digest over all of them."""
        present = self.present_tables()
        if not present:
            raise FixtureUnavailable(
                f"{self.database} has none of the tracked business tables; is it the right "
                "database, and are its modules installed?")
        selects = " union all ".join(
            f"select '{table}' as t, count(*)::text as n, "
            f"md5(coalesce(string_agg(x::text, '|' order by x.id), '')) as d from {table} x"
            for table in present)
        raw = self._psql(f"select t || ' ' || n || ' ' || d from ({selects}) rows order by t")
        tables: dict[str, dict] = {}
        for line in raw.splitlines():
            if not line.strip():
                continue
            name, count, content = line.split(" ", 2)
            tables[name] = {"rows": int(count), "digest": content}
        missing = sorted(set(present) - set(tables))
        if missing:
            raise FixtureUnavailable(f"fixture projection is incomplete: {missing}")
        return {
            "artifact_version": ARTIFACT_VERSION,
            "kind": "woa_odoo_fixture_state",
            "fixture": self.identity,
            "tables": tables,
            # Part of the digest, so a `stock_picking` that appears because someone installed
            # a module moves the baseline instead of quietly joining it.
            "digest": digest_json(tables),
        }

    # -- writing ------------------------------------------------------------------------
    def dump(self) -> bytes:
        """A custom-format archive, refused if the fixture moves while it is being read."""
        before = self.capture()["digest"]
        archive = self.runner.run(
            ["docker", "exec", "-i", self.container, "pg_dump", "-U", self.db_user,
             "-d", self.database, "--format=custom", "--no-owner", "--no-privileges"],
            timeout_s=600)
        if not archive.startswith(b"PGDMP"):
            raise FixtureUnavailable("fixture snapshot is not a PostgreSQL custom dump")
        if self.capture()["digest"] != before:
            raise FixtureUnavailable("the fixture changed while its snapshot was taken")
        return archive

    def restore(self, archive: bytes) -> None:
        """Recreate the database from the archive, then make the running Odoo agree.

        Restoring *into* the live database was the obvious first design and it does not
        work: an Odoo database is ~480 tables, and `pg_restore --clean --single-transaction`
        needs a lock per object, which exhausts `max_locks_per_transaction` on a default
        server -- reported, unhelpfully, as "out of shared memory". Dropping the whole
        database and recreating it needs no such transaction, and has the better property
        anyway: there is no state from the previous run that a `--clean` could miss.

        The cost is that Odoo's connections must go first, so the database is briefly
        closed to new ones. If anything between closing and dropping fails, the guard
        reopens it rather than leaving a server that cannot reach its own database.
        """
        if not archive.startswith(b"PGDMP"):
            raise FixtureMismatch("snapshot is not a PostgreSQL custom dump")
        self._psql(f'alter database "{self.database}" with allow_connections false',
                   database="postgres")
        try:
            self._psql("select pg_terminate_backend(pid) from pg_stat_activity "
                       f"where datname = '{self.database}' and pid <> pg_backend_pid()",
                       database="postgres")
            self._psql(f'drop database "{self.database}"', database="postgres")
        except (FixtureUnavailable, FixtureMismatch):
            self._psql(f'alter database "{self.database}" with allow_connections true',
                       database="postgres")
            raise
        self._psql(f'create database "{self.database}" owner "{self.db_user}"',
                   database="postgres")
        self.runner.run(
            ["docker", "exec", "-i", self.container, "pg_restore", "-U", self.db_user,
             "-d", self.database, "--no-owner", "--no-privileges", "--exit-on-error"],
            input_bytes=archive, timeout_s=900)
        self.signal_reload()

    def signal_reload(self) -> None:
        """Invalidate the server's registry and caches without restarting it.

        A restored database that the server does not know about is worse than no restore:
        the rows are right and the answers are stale, which reads as an intermittent gate
        rather than as the wiring mistake it is.
        """
        self._psql("; ".join(f"select nextval('{name}')" for name in _SIGNALLING))


# -- the recovery record ------------------------------------------------------------------
#
# Same protocol as the Twenty fixture: the uncertainty is written down *before* the database
# write, so a restore that dies half way is INDETERMINATE on the next read rather than
# silently retried.


def _record_digest(record: dict) -> str:
    return digest_json({key: value for key, value in record.items()
                        if key != "record_digest"})


def _sealed(value: dict) -> dict:
    record = dict(value)
    record["record_digest"] = _record_digest(record)
    return record


def prepare_snapshot(*, fixture: OdooPostgresFixture, snapshot_path: Path,
                     record_path: Path) -> dict:
    """Record the fixture as it is now, and seal what it took to do so."""
    if snapshot_path.resolve() == record_path.resolve():
        raise FixtureMismatch("snapshot and recovery record must be separate files")
    state = fixture.capture()
    archive = fixture.dump()
    atomic_write_bytes(snapshot_path, archive)
    record = _sealed({
        "artifact_version": ARTIFACT_VERSION,
        "kind": "woa_odoo_recovery",
        "status": "READY",
        "fixture": fixture.identity,
        "baseline_digest": state["digest"],
        "baseline_tables": state["tables"],
        "snapshot": {"sha256": "sha256:" + hashlib.sha256(archive).hexdigest(),
                     "bytes": len(archive)},
        "updated_at": utc_now(),
    })
    atomic_write_json(record_path, record)
    return record


def _validated_record(value: dict, fixture: OdooPostgresFixture, archive: bytes) -> dict:
    if value.get("artifact_version") != ARTIFACT_VERSION \
            or value.get("kind") != "woa_odoo_recovery":
        raise FixtureMismatch("recovery record header is invalid")
    if value.get("record_digest") != _record_digest(value):
        raise FixtureMismatch("recovery record digest is invalid")
    if value.get("fixture") != fixture.identity:
        raise FixtureMismatch("recovery record identifies a different fixture")
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("bytes") != len(archive) \
            or snapshot.get("sha256") != "sha256:" + hashlib.sha256(archive).hexdigest():
        raise FixtureMismatch("the snapshot does not match its recovery record")
    return value


def restore_snapshot(*, fixture: OdooPostgresFixture, snapshot_path: Path,
                     record_path: Path) -> dict:
    """Restore once, recording the uncertainty before the database write, never after.

    Unlike the Twenty recovery record this one is not consumed: a gate that writes restores
    after *every* run, so a successful restore returns the record to READY. What is not
    reusable is an uncertain one -- once a restore has failed to report an outcome, the
    only honest answer to "what is in this database?" is "nobody knows", and a person has
    to look before the file is usable again.
    """
    try:
        archive = snapshot_path.read_bytes()
    except OSError as exc:
        raise FixtureMismatch("the snapshot file is unavailable") from exc
    record = _validated_record(load_json(record_path), fixture, archive)
    if record["status"] in {"RESTORING", "INDETERMINATE"}:
        return {"kind": "woa_odoo_restore", "status": "INDETERMINATE",
                "reason": "a previous restore did not report an outcome"}
    if record["status"] == "FAILED":
        return {"kind": "woa_odoo_restore", "status": "FAIL",
                "reason": "a previous restore left the fixture off its baseline",
                "baseline_digest": record["baseline_digest"],
                "observed_digest": record.get("observed_digest", "")}
    active = _sealed({**record, "status": "RESTORING", "updated_at": utc_now()})
    atomic_write_json(record_path, active)
    try:
        fixture.restore(archive)
        observed = fixture.capture()
    except (FixtureUnavailable, FixtureMismatch):
        atomic_write_json(record_path,
                          _sealed({**active, "status": "INDETERMINATE",
                                   "updated_at": utc_now()}))
        return {"kind": "woa_odoo_restore", "status": "INDETERMINATE",
                "reason": "the restore outcome is uncertain"}
    changed = sorted(name for name, row in observed["tables"].items()
                     if record["baseline_tables"].get(name) != row)
    # The same rule the pre-run check uses. A restore that put every business table back is a
    # successful restore, and the running server writes one `mail_message` row every twenty
    # seconds or so -- comparing the whole digest here made success depend on whether the
    # verifying read happened to land before that write, which is a gate that fails at
    # random by design. The exact digest is still recorded, so nothing is hidden; what
    # changes is only which difference counts as a failed restore.
    unexpected = [name for name in changed if name not in SELF_WRITTEN_TABLES]
    passed = not unexpected
    atomic_write_json(record_path, _sealed({
        **{key: value for key, value in record.items() if key != "record_digest"},
        "status": "READY" if passed else "FAILED",
        "observed_digest": observed["digest"],
        "updated_at": utc_now(),
    }))
    return {
        "kind": "woa_odoo_restore",
        "status": "PASS" if passed else "FAIL",
        "baseline_digest": record["baseline_digest"],
        "observed_digest": observed["digest"],
        "changed_tables": changed,
        # Named separately from `changed_tables`: a reviewer wants to see that the log grew
        # *and* that nothing else did, not a single boolean that merged the two.
        "unexpected_tables": unexpected,
    }


# -- command line -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Take, check or put back the baseline a live gate runs against.

        python -m web_operation_agent_odoo.demo.fixture state    --container ... --database ...
        python -m web_operation_agent_odoo.demo.fixture snapshot --snapshot .private/odoo/baseline.pgdump ...
        python -m web_operation_agent_odoo.demo.fixture restore  --snapshot .private/odoo/baseline.pgdump ...
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="web-operation-agent-odoo-fixture",
        description="Snapshot and restore the local Odoo fixture a live gate runs against.")
    parser.add_argument("command", choices=("state", "snapshot", "restore"))
    parser.add_argument("--container", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--db-user", default="odoo")
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--record", default="")
    arguments = parser.parse_args(argv)

    fixture = OdooPostgresFixture(container=arguments.container,
                                  database=arguments.database,
                                  db_user=arguments.db_user)
    if arguments.command == "state":
        print(json.dumps(fixture.capture(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if not arguments.snapshot or not arguments.record:
        parser.error("snapshot and restore need --snapshot and --record")
    snapshot_path, record_path = Path(arguments.snapshot), Path(arguments.record)
    if arguments.command == "snapshot":
        outcome = prepare_snapshot(fixture=fixture, snapshot_path=snapshot_path,
                                   record_path=record_path)
    else:
        outcome = restore_snapshot(fixture=fixture, snapshot_path=snapshot_path,
                                   record_path=record_path)
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if outcome.get("status") in {"READY", "PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
