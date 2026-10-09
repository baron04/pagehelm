"""Durable session ownership: leases, fences, message dedup and the event outbox.

Recovery preserves history, never execution authority. Accepted old messages are not
replayed; the host must authenticate again and issue a new browser capability.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class DurableStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionOwner:
    session_id: str
    scope: str
    owner: str
    fence: int
    #: Messages the previous owner had accepted or started and never finished.
    interrupted: int = 0


class SQLiteSessionStore:
    def __init__(self, path: str | Path, *, lease_ms: int = 30_000):
        if isinstance(lease_ms, bool) or not isinstance(lease_ms, int) or not 1000 <= lease_ms <= 300_000:
            raise ValueError("lease_ms must be between 1000 and 300000")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("session store must be a regular file")
        finally:
            os.close(fd)
        os.chmod(self.path, 0o600)
        self.lease_ms = lease_ms
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    sid TEXT PRIMARY KEY, scope TEXT NOT NULL, owner TEXT NOT NULL,
                    fence INTEGER NOT NULL, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    sid TEXT NOT NULL, mid TEXT NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL, updated INTEGER NOT NULL,
                    PRIMARY KEY(sid, mid));
                CREATE TABLE IF NOT EXISTS outbox (
                    sid TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
                    created INTEGER NOT NULL, PRIMARY KEY(sid, sequence));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def now():
        return int(time.time() * 1000)

    def acquire(self, sid: str, scope: str) -> SessionOwner:
        now = self.now()
        owner = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sessions WHERE sid = ?", (sid,)).fetchone()
            if row and (row["scope"] != scope or row["expires"] > now):
                raise DurableStateError("session scope mismatch or live owner exists")
            if row is None and db.execute("SELECT count(*) FROM sessions").fetchone()[0] >= 10000:
                raise DurableStateError("durable session capacity exhausted; prune inactive history")
            fence = row["fence"] + 1 if row else 1
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?)",
                       (sid, scope, owner, fence, now + self.lease_ms))
            # Neither queued nor started messages are safe to replay after a crash.
            interrupted = db.execute(
                "UPDATE messages SET state='interrupted', updated=? "
                "WHERE sid=? AND state IN ('accepted','running')", (now, sid)).rowcount
            db.commit()
        return SessionOwner(sid, scope, owner, fence, interrupted)

    def _check(self, db, owner):
        row = db.execute("SELECT * FROM sessions WHERE sid=?", (owner.session_id,)).fetchone()
        if (row is None or row["owner"] != owner.owner or row["scope"] != owner.scope
                or row["fence"] != owner.fence or row["expires"] <= self.now()):
            raise DurableStateError("session owner lease expired or was fenced")

    def renew(self, owner):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check(db, owner)
            db.execute("UPDATE sessions SET expires=? WHERE sid=?",
                       (self.now() + self.lease_ms, owner.session_id))
            db.commit()

    def release(self, owner):
        with self.connect() as db:
            db.execute("UPDATE sessions SET expires=? WHERE sid=? AND owner=? AND fence=?",
                       (self.now(), owner.session_id, owner.owner, owner.fence))

    def message(self, owner, mid, digest):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check(db, owner)
            row = db.execute("SELECT digest FROM messages WHERE sid=? AND mid=?",
                             (owner.session_id, mid)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise DurableStateError("message id content conflicts with durable history")
                db.commit()
                return False
            count = db.execute("SELECT count(*) FROM messages WHERE sid=?",
                               (owner.session_id,)).fetchone()[0]
            if count >= 10000:
                raise DurableStateError("durable message capacity exhausted")
            db.execute("INSERT INTO messages VALUES (?, ?, ?, 'accepted', ?)",
                       (owner.session_id, mid, digest, self.now()))
            db.commit()
            return True

    def transition(self, owner, mid, state):
        if state not in {"running", "completed"}:
            raise ValueError("invalid durable message state")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check(db, owner)
            db.execute("UPDATE messages SET state=?, updated=? WHERE sid=? AND mid=?",
                       (state, self.now(), owner.session_id, mid))
            db.commit()

    def append(self, owner, event):
        # Action envelopes, approvals and observations are deliberately never replayable.
        payload = json.dumps(event, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > 256 * 1024:
            raise DurableStateError("durable event exceeds limit")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check(db, owner)
            seq = db.execute("SELECT coalesce(max(sequence),0)+1 FROM outbox WHERE sid=?",
                             (owner.session_id,)).fetchone()[0]
            if seq > 100000:
                raise DurableStateError("durable event capacity exhausted")
            db.execute("INSERT INTO outbox VALUES (?, ?, ?, ?)",
                       (owner.session_id, seq, payload, self.now()))
            db.commit()
        return seq

    def history(self, owner, *, before: int | None = None, limit: int = 100):
        """Read one ascending page using the durable sequence, under the current owner."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("history limit must be an integer from 1 to 500")
        if before is not None and (isinstance(before, bool) or not isinstance(before, int)
                                   or before < 1):
            raise ValueError("history cursor must be a positive integer")
        with self.connect() as db:
            self._check(db, owner)
            condition = " AND sequence < ?" if before is not None else ""
            params = ((owner.session_id, before, limit) if before is not None
                      else (owner.session_id, limit))
            rows = db.execute(
                "SELECT payload, created, sequence FROM outbox WHERE sid=?" + condition
                + " ORDER BY sequence DESC LIMIT ?", params).fetchall()
        return [{**json.loads(row[0]), "at": int(row[1]), "sequence": int(row[2])}
                for row in reversed(rows)]

    def prune(self, *, older_than_ms: int) -> int:
        """Remove entire inactive sessions; retain tombstones for every live session."""
        if older_than_ms >= self.now() - 86_400_000:
            raise ValueError("retention cutoff must be at least one day old")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            sids = [row[0] for row in db.execute(
                "SELECT sid FROM sessions WHERE expires < ? AND sid NOT IN "
                "(SELECT sid FROM messages WHERE updated >= ?)",
                (older_than_ms, older_than_ms))]
            for sid in sids:
                db.execute("DELETE FROM outbox WHERE sid=?", (sid,))
                db.execute("DELETE FROM messages WHERE sid=?", (sid,))
                db.execute("DELETE FROM sessions WHERE sid=?", (sid,))
            db.commit()
            return len(sids)


def principal_scope(principal, origins) -> str:
    fields = [principal.application_id, principal.tenant_id, principal.workspace_id,
              principal.actor_id, sorted(origins)]
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
