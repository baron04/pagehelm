"""Process-shared auth side stores: expiring event nonces and principal revocations.

Both ride the same private SQLite file as session ownership but answer a different
question -- may this message be accepted right now -- so they live apart from the leases.
"""
from __future__ import annotations

from .identity import (PrincipalContext, PrincipalError, PrincipalRevokedError,
                      actor_authority_digest)
from .store import SQLiteSessionStore


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

    def prune(self, *, now_ms: int | None = None) -> int:
        now = self.store.now() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("nonce prune time must be a non-negative integer")
        with self.store.connect() as db:
            removed = db.execute(
                "DELETE FROM expiring_nonces WHERE namespace=? AND expires <= ?",
                (self.namespace, now)).rowcount
        return max(0, removed)


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

    def prune(self, *, older_than_ms: int, now_ms: int | None = None) -> dict:
        """Drop revocations only after every conforming principal has long expired."""
        now = self.store.now() if now_ms is None else now_ms
        if (isinstance(older_than_ms, bool) or not isinstance(older_than_ms, int)
                or isinstance(now, bool) or not isinstance(now, int)
                or older_than_ms < 0 or now < 0
                or older_than_ms > now - 86_400_000):
            raise ValueError("revocation retention cutoff must be at least one day old")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            exact = db.execute(
                "DELETE FROM principal_revocations WHERE revoked < ?",
                (older_than_ms,)).rowcount
            actors = db.execute(
                "DELETE FROM actor_revocations WHERE revoked_before < ?",
                (older_than_ms,)).rowcount
            db.commit()
        return {"exact": max(0, exact), "actors": max(0, actors)}
