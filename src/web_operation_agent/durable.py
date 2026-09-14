"""Private SQLite session ownership, message deduplication and event outbox.

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

from .identity import (PrincipalContext, PrincipalError, PrincipalRevokedError,
                       actor_authority_digest)


class DurableStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionOwner:
    session_id: str
    scope: str
    owner: str
    fence: int


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
            db.execute("UPDATE messages SET state='interrupted', updated=? "
                       "WHERE sid=? AND state IN ('accepted','running')", (now, sid))
            db.commit()
        return SessionOwner(sid, scope, owner, fence)

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

    def history(self, owner):
        with self.connect() as db:
            self._check(db, owner)
            rows = db.execute(
                "SELECT payload, created FROM outbox WHERE sid=? ORDER BY sequence DESC LIMIT 30",
                (owner.session_id,)).fetchall()
        return [{**json.loads(row[0]), "at": int(row[1])} for row in reversed(rows)]

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


class SQLiteProviderQuota:
    """Shared rolling request quota. A reservation is never refunded after dispatch risk."""

    def __init__(self, store: SQLiteSessionStore, *, scope: str,
                 max_requests: int = 60, window_ms: int = 60_000):
        if (not isinstance(scope, str) or not scope or len(scope) > 200
                or isinstance(max_requests, bool) or not isinstance(max_requests, int)
                or not 1 <= max_requests <= 1_000_000
                or isinstance(window_ms, bool) or not isinstance(window_ms, int)
                or not 1000 <= window_ms <= 86_400_000):
            raise ValueError("invalid provider quota configuration")
        self.store = store
        self.scope = hashlib.sha256(scope.encode()).hexdigest()
        self.max_requests, self.window_ms = max_requests, window_ms
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS provider_quota ("
                       "scope TEXT NOT NULL, reservation TEXT PRIMARY KEY, created INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS provider_quota_scope ON provider_quota(scope,created)")
            db.execute("CREATE TABLE IF NOT EXISTS provider_quota_policy ("
                       "scope TEXT PRIMARY KEY, max_requests INTEGER, window_ms INTEGER)")
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO provider_quota_policy VALUES (?, ?, ?)",
                       (self.scope, max_requests, window_ms))
            policy = db.execute("SELECT max_requests, window_ms FROM provider_quota_policy WHERE scope=?",
                                (self.scope,)).fetchone()
            if tuple(policy) != (max_requests, window_ms):
                raise DurableStateError("shared provider quota policy conflicts")
            db.commit()

    def reserve(self) -> bool:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self.store.now()
            db.execute("DELETE FROM provider_quota WHERE scope=? AND created<=?",
                       (self.scope, now - self.window_ms))
            count = db.execute("SELECT count(*) FROM provider_quota WHERE scope=?",
                               (self.scope,)).fetchone()[0]
            if count >= self.max_requests:
                db.commit()
                return False
            db.execute("INSERT INTO provider_quota VALUES (?, ?, ?)",
                       (self.scope, uuid.uuid4().hex, now))
            db.commit()
            return True


class SQLiteExpiringNonceStore:
    """Atomic, process-shared consumption of short-lived signed-event nonces."""

    def __init__(self, store: SQLiteSessionStore, *, namespace: str):
        if not isinstance(store, SQLiteSessionStore):
            raise TypeError("store must be a SQLiteSessionStore")
        if (not isinstance(namespace, str) or not 1 <= len(namespace) <= 64
                or any(not (char.islower() or char.isdigit() or char == "_")
                       for char in namespace)):
            raise ValueError("nonce namespace must be a lowercase reason code")
        self.store, self.namespace = store, namespace
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS expiring_nonces ("
                       "namespace TEXT NOT NULL, nonce TEXT NOT NULL, "
                       "expires INTEGER NOT NULL, PRIMARY KEY(namespace, nonce))")

    def consume(self, nonce: str, *, expires_at: int, now_ms: int) -> bool:
        if (not isinstance(nonce, str) or not nonce or len(nonce) > 200
                or isinstance(expires_at, bool) or not isinstance(expires_at, int)
                or isinstance(now_ms, bool) or not isinstance(now_ms, int)
                or now_ms < 0 or expires_at <= now_ms):
            raise ValueError("expiring nonce is invalid")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM expiring_nonces WHERE expires <= ?", (now_ms,))
            changed = db.execute(
                "INSERT OR IGNORE INTO expiring_nonces VALUES (?, ?, ?)",
                (self.namespace, nonce, expires_at)).rowcount
            db.commit()
        return changed == 1


class SQLitePrincipalRevocations:
    """Process-shared exact and actor-wide revocation watermarks."""

    def __init__(self, store: SQLiteSessionStore):
        if not isinstance(store, SQLiteSessionStore):
            raise TypeError("store must be a SQLiteSessionStore")
        self.store = store
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS principal_revocations ("
                       "principal_digest TEXT PRIMARY KEY, revoked INTEGER NOT NULL, "
                       "reason TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS actor_revocations ("
                       "actor_authority_digest TEXT PRIMARY KEY, revoked_before INTEGER NOT NULL, "
                       "reason TEXT NOT NULL)")

    @staticmethod
    def _reason(reason: str) -> None:
        if (not isinstance(reason, str) or not 1 <= len(reason) <= 64
                or any(not (char.islower() or char.isdigit() or char == "_")
                       for char in reason)):
            raise ValueError("revocation reason must be a lowercase reason code")

    def revoke(self, principal: PrincipalContext, *, reason: str = "session_revoked",
               now_ms: int | None = None) -> bool:
        if not isinstance(principal, PrincipalContext):
            raise TypeError("principal must be a PrincipalContext")
        self._reason(reason)
        revoked = self.store.now() if now_ms is None else now_ms
        if isinstance(revoked, bool) or not isinstance(revoked, int) or revoked < 0:
            raise ValueError("revocation time must be a non-negative integer")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "INSERT OR IGNORE INTO principal_revocations VALUES (?, ?, ?)",
                (principal.binding_digest, revoked, reason)).rowcount
            db.execute(
                "INSERT INTO actor_revocations VALUES (?, ?, ?) "
                "ON CONFLICT(actor_authority_digest) DO UPDATE SET "
                "revoked_before=max(revoked_before, excluded.revoked_before), "
                "reason=CASE WHEN excluded.revoked_before >= revoked_before "
                "THEN excluded.reason ELSE reason END",
                (principal.actor_authority_digest, revoked, reason))
            db.commit()
        return changed == 1

    def revoke_actor(self, *, application_id: str, tenant_id: str, actor_id: str,
                     reason: str = "session_revoked",
                     now_ms: int | None = None) -> None:
        self._reason(reason)
        scope = actor_authority_digest(application_id, tenant_id, actor_id)
        revoked = self.store.now() if now_ms is None else now_ms
        if isinstance(revoked, bool) or not isinstance(revoked, int) or revoked < 0:
            raise ValueError("revocation time must be a non-negative integer")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO actor_revocations VALUES (?, ?, ?) "
                "ON CONFLICT(actor_authority_digest) DO UPDATE SET "
                "revoked_before=max(revoked_before, excluded.revoked_before), "
                "reason=CASE WHEN excluded.revoked_before >= revoked_before "
                "THEN excluded.reason ELSE reason END",
                (scope, revoked, reason))
            db.commit()

    def validate(self, principal: PrincipalContext) -> None:
        if not isinstance(principal, PrincipalContext):
            raise PrincipalError("principal is unavailable")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT 1 FROM principal_revocations WHERE principal_digest=?",
                (principal.binding_digest,)).fetchone()
            actor = db.execute(
                "SELECT revoked_before FROM actor_revocations "
                "WHERE actor_authority_digest=?",
                (principal.actor_authority_digest,)).fetchone()
        if row is not None or (actor is not None
                               and principal.issued_at <= int(actor["revoked_before"])):
            raise PrincipalRevokedError("principal authority has been revoked")

    def status(self, principal: PrincipalContext) -> dict:
        """Return content-free operator state for one exact principal issuance."""
        if not isinstance(principal, PrincipalContext):
            raise TypeError("principal must be a PrincipalContext")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT revoked, reason FROM principal_revocations WHERE principal_digest=?",
                (principal.binding_digest,)).fetchone()
            actor = db.execute(
                "SELECT revoked_before, reason FROM actor_revocations "
                "WHERE actor_authority_digest=?",
                (principal.actor_authority_digest,)).fetchone()
        exact_at = int(row["revoked"]) if row is not None else -1
        actor_at = (int(actor["revoked_before"])
                    if actor is not None
                    and principal.issued_at <= int(actor["revoked_before"])
                    else -1)
        if max(exact_at, actor_at) >= 0:
            latest = actor if actor_at >= exact_at else row
            return {"revoked": True, "revoked_at_ms": max(exact_at, actor_at),
                    "reason": latest["reason"]}
        return {"revoked": False}
