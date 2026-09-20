"""The shared rolling provider-request quota, kept next to the durable state it rides on."""
from __future__ import annotations

import hashlib
import uuid

from .store import DurableStateError, SQLiteSessionStore


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
