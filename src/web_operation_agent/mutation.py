"""Atomic mutation intent journals for consume-before-dispatch semantics."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STATES = {"prepared", "dispatching", "completed", "failed", "indeterminate"}
_TERMINAL = {"completed", "failed", "indeterminate"}


class MutationJournalError(RuntimeError):
    """A mutation intent cannot safely transition to the requested state."""


class MutationJournalDurability(str, Enum):
    """Restart behavior a journal implementation explicitly promises."""

    VOLATILE = "volatile"
    DURABLE = "durable"


@dataclass(frozen=True, slots=True)
class MutationIntent:
    proposal_digest: str
    operation_context_digest: str
    state: str
    expires_at: int
    updated_at: int
    result_digest: str = ""
    business_key: str = ""
    reconciliation_json: str = "{}"

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.proposal_digest):
            raise MutationJournalError("proposal digest is invalid")
        if self.operation_context_digest and not _DIGEST.fullmatch(
                self.operation_context_digest):
            raise MutationJournalError("operation context digest is invalid")
        if self.state not in _STATES:
            raise MutationJournalError("mutation intent state is invalid")
        for name in ("expires_at", "updated_at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MutationJournalError(f"{name} must be a non-negative integer")
        if self.business_key and not _DIGEST.fullmatch(self.business_key):
            raise MutationJournalError("business key is invalid")
        if self.result_digest and not _DIGEST.fullmatch(self.result_digest):
            raise MutationJournalError("result digest is invalid")


class MutationJournal(Protocol):
    durability: MutationJournalDurability

    def register(self, proposal_digest: str, operation_context_digest: str, *,
                 expires_at: int, now_ms: int | None = None,
                 business_key: str = "", reconciliation: dict | None = None) -> None: ...

    def claim(self, proposal_digest: str, operation_context_digest: str, *,
              now_ms: int | None = None) -> None: ...

    def finish(self, proposal_digest: str, state: str, *,
               result_digest: str = "", now_ms: int | None = None) -> None: ...

    def get(self, proposal_digest: str) -> MutationIntent | None: ...


def _now(value: int | None) -> int:
    if value is None:
        return int(time.time() * 1000)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MutationJournalError("now_ms must be a non-negative integer")
    return value


def _registration(proposal_digest: str, operation_context_digest: str,
                  expires_at: int, now_ms: int) -> None:
    if not _DIGEST.fullmatch(proposal_digest):
        raise MutationJournalError("proposal digest is invalid")
    if operation_context_digest and not _DIGEST.fullmatch(operation_context_digest):
        raise MutationJournalError("operation context digest is invalid")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int) \
            or expires_at <= now_ms:
        raise MutationJournalError("mutation proposal expiry must be in the future")


class MemoryMutationJournal:
    """Thread-safe bounded journal used when durable storage is not configured."""

    durability = MutationJournalDurability.VOLATILE

    def __init__(self, *, max_entries: int = 10_000):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) \
                or not 1 <= max_entries <= 1_000_000:
            raise ValueError("max_entries must be an integer from 1 to 1000000")
        self.max_entries = max_entries
        self._records: dict[str, MutationIntent] = {}
        self._lock = threading.RLock()

    def register(self, proposal_digest: str, operation_context_digest: str, *,
                 expires_at: int, now_ms: int | None = None,
                 business_key: str = "", reconciliation: dict | None = None) -> None:
        encoded = json.dumps(reconciliation or {}, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 16_384:
            raise MutationJournalError("reconciliation metadata exceeds 16 KiB")
        now = _now(now_ms)
        _registration(proposal_digest, operation_context_digest, expires_at, now)
        if business_key and not _DIGEST.fullmatch(business_key):
            raise MutationJournalError("business key is invalid")
        with self._lock:
            prior = self._records.get(proposal_digest)
            if prior is not None:
                if (prior.operation_context_digest != operation_context_digest
                        or prior.expires_at != expires_at or prior.business_key != business_key
                        or prior.reconciliation_json != encoded):
                    raise MutationJournalError("proposal digest registration conflicts")
                return
            if len(self._records) >= self.max_entries:
                raise MutationJournalError("mutation journal capacity is exhausted")
            self._records[proposal_digest] = MutationIntent(
                proposal_digest, operation_context_digest, "prepared",
                expires_at, now, business_key=business_key, reconciliation_json=encoded)

    def claim(self, proposal_digest: str, operation_context_digest: str, *,
              now_ms: int | None = None) -> None:
        now = _now(now_ms)
        with self._lock:
            intent = self._records.get(proposal_digest)
            if intent is None:
                raise MutationJournalError("mutation proposal is unknown")
            if intent.operation_context_digest != operation_context_digest:
                raise MutationJournalError("mutation proposal belongs to another context")
            if intent.expires_at <= now:
                raise MutationJournalError("mutation proposal has expired")
            if intent.state != "prepared":
                raise MutationJournalError(
                    f"mutation proposal is already {intent.state}")
            if intent.business_key and any(
                    row.business_key in {"", intent.business_key}
                    and row.state in {"dispatching", "indeterminate"}
                    for row in self._records.values()):
                raise MutationJournalError("business key has an in-flight or unresolved mutation")
            self._records[proposal_digest] = replace(
                intent, state="dispatching", updated_at=now)

    def finish(self, proposal_digest: str, state: str, *,
               result_digest: str = "", now_ms: int | None = None) -> None:
        now = _now(now_ms)
        if state not in _TERMINAL:
            raise MutationJournalError("mutation finish state is invalid")
        with self._lock:
            intent = self._records.get(proposal_digest)
            if intent is None or intent.state != "dispatching":
                raise MutationJournalError("only a dispatching mutation can finish")
            self._records[proposal_digest] = replace(
                intent, state=state, updated_at=now, result_digest=result_digest)

    def get(self, proposal_digest: str) -> MutationIntent | None:
        with self._lock:
            return self._records.get(proposal_digest)


class SQLiteMutationJournal:
    """Process-safe durable mutation state machine backed by a private SQLite file."""

    durability = MutationJournalDurability.DURABLE

    def __init__(self, path: str | Path, *, timeout_s: float = 5.0):
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or not 0 < timeout_s <= 120):
            raise ValueError("timeout_s must be in (0, 120]")
        candidate = Path(path).expanduser()
        self.path = candidate if candidate.is_absolute() else Path.cwd() / candidate
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("mutation journal path must not be a symbolic link")
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            file_stat = os.lstat(self.path)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError("mutation journal path must be a regular file")
        else:
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        self.timeout_s = timeout_s
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS mutation_intents (
                    proposal_digest TEXT PRIMARY KEY,
                    operation_context_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'prepared', 'dispatching', 'completed', 'failed', 'indeterminate')),
                    expires_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    result_digest TEXT NOT NULL DEFAULT ''
                )
            """)

            connection.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(mutation_intents)")}
            if "business_key" not in columns:
                connection.execute(
                    "ALTER TABLE mutation_intents ADD COLUMN business_key TEXT NOT NULL DEFAULT ''")
            if "reconciliation_json" not in columns:
                connection.execute("ALTER TABLE mutation_intents ADD COLUMN "
                                   "reconciliation_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute("CREATE TABLE IF NOT EXISTS mutation_resolutions ("
                               "proposal_digest TEXT PRIMARY KEY, expected_updated_at INTEGER NOT NULL, "
                               "outcome TEXT NOT NULL, operator_digest TEXT NOT NULL, "
                               "evidence_digest TEXT NOT NULL, resolved_at INTEGER NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS mutation_business_key "
                               "ON mutation_intents(business_key, state)")
            connection.commit()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(
            self.path, timeout=self.timeout_s, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def register(self, proposal_digest: str, operation_context_digest: str, *,
                 expires_at: int, now_ms: int | None = None,
                 business_key: str = "", reconciliation: dict | None = None) -> None:
        encoded = json.dumps(reconciliation or {}, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 16_384:
            raise MutationJournalError("reconciliation metadata exceeds 16 KiB")
        now = _now(now_ms)
        _registration(proposal_digest, operation_context_digest, expires_at, now)
        if business_key and not _DIGEST.fullmatch(business_key):
            raise MutationJournalError("business key is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT operation_context_digest, expires_at, business_key, reconciliation_json FROM mutation_intents "
                "WHERE proposal_digest = ?", (proposal_digest,)).fetchone()
            if row is not None:
                if (row["operation_context_digest"] != operation_context_digest
                        or row["expires_at"] != expires_at or row["business_key"] != business_key
                        or row["reconciliation_json"] != encoded):
                    raise MutationJournalError("proposal digest registration conflicts")
                connection.commit()
                return
            connection.execute(
                "INSERT INTO mutation_intents "
                "(proposal_digest, operation_context_digest, state, expires_at, updated_at, business_key, reconciliation_json) "
                "VALUES (?, ?, 'prepared', ?, ?, ?, ?)",
                (proposal_digest, operation_context_digest, expires_at, now, business_key, encoded))
            connection.commit()

    def claim(self, proposal_digest: str, operation_context_digest: str, *,
              now_ms: int | None = None) -> None:
        now = _now(now_ms)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                "SELECT * FROM mutation_intents WHERE proposal_digest = ?",
                (proposal_digest,)).fetchone()
            if intent is not None and intent["state"] == "prepared" and intent["business_key"]:
                blocked = connection.execute(
                    "SELECT 1 FROM mutation_intents WHERE (business_key = ? OR business_key = '') "
                    "AND state IN ('dispatching', 'indeterminate') LIMIT 1",
                    (intent["business_key"],)).fetchone()
                if blocked:
                    raise MutationJournalError("business key has an in-flight or unresolved mutation")
            changed = connection.execute(
                "UPDATE mutation_intents SET state = 'dispatching', updated_at = ? "
                "WHERE proposal_digest = ? AND operation_context_digest = ? "
                "AND state = 'prepared' AND expires_at > ?",
                (now, proposal_digest, operation_context_digest, now)).rowcount
            if changed == 1:
                connection.commit()
                return
            row = connection.execute(
                "SELECT * FROM mutation_intents WHERE proposal_digest = ?",
                (proposal_digest,)).fetchone()
            connection.rollback()
        if row is None:
            raise MutationJournalError("mutation proposal is unknown")
        if row["operation_context_digest"] != operation_context_digest:
            raise MutationJournalError("mutation proposal belongs to another context")
        if row["expires_at"] <= now:
            raise MutationJournalError("mutation proposal has expired")
        raise MutationJournalError(f"mutation proposal is already {row['state']}")

    def finish(self, proposal_digest: str, state: str, *,
               result_digest: str = "", now_ms: int | None = None) -> None:
        now = _now(now_ms)
        if state not in _TERMINAL:
            raise MutationJournalError("mutation finish state is invalid")
        if result_digest and not _DIGEST.fullmatch(result_digest):
            raise MutationJournalError("result digest is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE mutation_intents SET state = ?, updated_at = ?, result_digest = ? "
                "WHERE proposal_digest = ? AND state = 'dispatching'",
                (state, now, result_digest, proposal_digest)).rowcount
            if changed != 1:
                connection.rollback()
                raise MutationJournalError("only a dispatching mutation can finish")
            connection.commit()

    def get(self, proposal_digest: str) -> MutationIntent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mutation_intents WHERE proposal_digest = ?",
                (proposal_digest,)).fetchone()
        return (MutationIntent(**dict(row)) if row is not None else None)

    def unresolved(self) -> tuple[MutationIntent, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM mutation_intents WHERE state IN "
                                      "('dispatching', 'indeterminate') ORDER BY updated_at").fetchall()
        return tuple(MutationIntent(**dict(row)) for row in rows)

    def resolve(self, proposal_digest: str, *, expected_updated_at: int,
                outcome: str, operator_digest: str, evidence_digest: str,
                writer_stopped: bool, dry_run: bool = True) -> dict:
        """Operator-only CAS resolution; never retries a write or restores an approval.

        A verified stopped writer is required even for indeterminate records: a timed-out
        upstream request may still complete. Evidence must establish a definitive result.
        """
        if outcome not in {"completed", "failed"} or writer_stopped is not True:
            raise MutationJournalError("resolution requires definite evidence and a stopped writer")
        if not isinstance(dry_run, bool):
            raise MutationJournalError("dry_run must be boolean")
        if not all(_DIGEST.fullmatch(value) for value in (operator_digest, evidence_digest)):
            raise MutationJournalError("resolution requires operator and evidence digests")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM mutation_intents WHERE proposal_digest = ?",
                                     (proposal_digest,)).fetchone()
            if row is None or row["state"] not in {"dispatching", "indeterminate"}:
                raise MutationJournalError("resolution requires an unresolved intent")
            if row["updated_at"] != expected_updated_at:
                raise MutationJournalError("resolution evidence is stale")
            plan = {"proposal_digest": proposal_digest, "from": row["state"],
                    "to": outcome, "dry_run": dry_run, "dispatches": 0}
            if not dry_run:
                now = _now(None)
                connection.execute("INSERT INTO mutation_resolutions VALUES (?, ?, ?, ?, ?, ?)",
                                   (proposal_digest, expected_updated_at, outcome,
                                    operator_digest, evidence_digest, now))
                connection.execute("UPDATE mutation_intents SET state = ?, updated_at = ?, "
                                   "result_digest = ? WHERE proposal_digest = ?",
                                   (outcome, now, evidence_digest, proposal_digest))
            connection.commit()
            return plan

    def database_sha256(self) -> str:
        """Return a diagnostic file digest after checkpointing committed WAL pages."""
        with self._connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(FULL)")
        return hashlib.sha256(self.path.read_bytes()).hexdigest()
