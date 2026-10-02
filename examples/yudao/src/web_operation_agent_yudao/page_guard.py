"""Application-owned admission fence for native page operations.

All browser API traffic must pass through the integrating proxy. A window belongs to
one browser cookie and actor; all windows for that actor block mutation admission.
This deliberately pauses other browser sessions of the same actor as well. Beginning
a window waits for previously admitted requests to finish. A lost upstream response
poisons the actor's fence rather than declaring an uncertain request completed.

This is an HTTP admission boundary, not a database transaction sandbox. Only reviewed
GET handlers without persistence or asynchronous effects may run inside the window.
Direct upstream access, background jobs and external services are outside its scope.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import re
import threading
import time
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlsplit


class GuardRefused(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class _Actor:
    inflight: int = 0
    opening: int = 0
    uncertain: bool = False
    windows: dict[str, float] = field(default_factory=dict)


class NativePageGuard:
    """Bounded, in-process fence; contains neither credentials nor model context."""

    def __init__(self, read_paths: frozenset[str], *, clock=time.monotonic,
                 actor_limit: int = 2048, journal: Path | None = None):
        if not isinstance(read_paths, frozenset) or any(
                not isinstance(path, str) or not re.fullmatch(r"/admin-api/[a-z0-9][a-z0-9/-]*", path)
                for path in read_paths):
            raise ValueError("reviewed reads must be exact admin API paths")
        if isinstance(actor_limit, bool) or not isinstance(actor_limit, int) or actor_limit < 1:
            raise ValueError("actor limit must be positive")
        self.read_paths, self.clock, self.actor_limit = read_paths, clock, actor_limit
        self._actors: dict[str, _Actor] = {}
        self._condition = threading.Condition()
        self.journal = Path(journal) if journal is not None else None
        if self.journal is not None:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.journal) as db:
                db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, actor TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS windows (actor TEXT NOT NULL, owner TEXT NOT NULL, PRIMARY KEY(actor, owner))")
                # A browser may still be operating under an acknowledged window after
                # restart. Its monotonic deadline cannot be reconstructed safely.
                for (actor,) in db.execute("SELECT actor FROM requests UNION SELECT actor FROM windows"):
                    if not re.fullmatch(r"[a-f0-9]{64}", actor):
                        raise ValueError("invalid guard journal actor")
                    self._actors[actor] = _Actor(uncertain=True)

    def _actor(self, actor):
        if not isinstance(actor, str) or not re.fullmatch(r"[a-f0-9]{64}", actor):
            raise GuardRefused("invalid_actor")
        state = self._actors.get(actor)
        if state is None:
            # Only wholly idle, certain actors can be forgotten.
            for key, held in tuple(self._actors.items()):
                self._expire(held, key)
                if not held.inflight and not held.opening and not held.windows and not held.uncertain:
                    del self._actors[key]
            if len(self._actors) >= self.actor_limit:
                raise GuardRefused("guard_capacity")
            state = self._actors[actor] = _Actor()
        self._expire(state, actor)
        return state

    def _expire(self, state, actor):
        now = self.clock()
        expired = [key for key, until in state.windows.items() if until <= now]
        if expired and self.journal is not None:
            try:
                with sqlite3.connect(self.journal) as db:
                    for key in expired:
                        db.execute("DELETE FROM windows WHERE actor=? AND owner=?", (actor, key))
            except Exception:
                raise GuardRefused("guard_journal_unavailable") from None
        for key in expired:
            del state.windows[key]

    @staticmethod
    def _owner(owner):
        if not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{64}", owner):
            raise GuardRefused("invalid_browser")

    def begin(self, actor: str, owner: str, *, ttl_s: float = 120,
              wait_s: float = 10) -> float:
        self._owner(owner)
        if (isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float))
                or not math.isfinite(ttl_s) or not 1 <= ttl_s <= 120
                or isinstance(wait_s, bool) or not isinstance(wait_s, (int, float))
                or not math.isfinite(wait_s) or not 0 <= wait_s <= 30):
            raise GuardRefused("invalid_window")
        with self._condition:
            state = self._actor(actor)
            if state.uncertain:
                raise GuardRefused("prior_request_uncertain")
            state.opening += 1
            self._condition.notify_all()
            try:
                deadline = time.monotonic() + wait_s
                while state.inflight:
                    if state.uncertain:
                        raise GuardRefused("prior_request_uncertain")
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise GuardRefused("prior_request_pending")
                    self._condition.wait(left)
                if state.uncertain:
                    raise GuardRefused("prior_request_uncertain")
                if owner not in state.windows and len(state.windows) >= 128:
                    raise GuardRefused("guard_capacity")
                if self.journal is not None:
                    try:
                        with sqlite3.connect(self.journal) as db:
                            db.execute("INSERT OR IGNORE INTO windows VALUES (?, ?)", (actor, owner))
                    except Exception:
                        raise GuardRefused("guard_journal_unavailable") from None
                state.windows[owner] = self.clock() + ttl_s
                return ttl_s
            finally:
                state.opening -= 1

    def end(self, actor: str, owner: str) -> None:
        self._owner(owner)
        with self._condition:
            state = self._actor(actor)
            if state.uncertain:
                raise GuardRefused("prior_request_uncertain")
            if self.journal is not None:
                try:
                    with sqlite3.connect(self.journal) as db:
                        db.execute("DELETE FROM windows WHERE actor=? AND owner=?", (actor, owner))
                except Exception:
                    raise GuardRefused("guard_journal_unavailable") from None
            state.windows.pop(owner, None)

    def active(self, actor: str, owner: str) -> bool:
        self._owner(owner)
        with self._condition:
            return owner in self._actor(actor).windows

    def protected(self, actor: str) -> bool:
        with self._condition:
            state = self._actor(actor)
            return bool(state.windows or state.opening or state.uncertain)

    @contextmanager
    def request(self, actor: str, method: str, target: str):
        parsed = urlsplit(target)
        # Encoded/absolute/fragment paths cannot borrow a reviewed route's admission.
        safe = (method == "GET" and not parsed.scheme and not parsed.netloc
                and not parsed.fragment and "%" not in parsed.path
                and parsed.path in self.read_paths)
        with self._condition:
            state = self._actor(actor)
            if (state.windows or state.opening or state.uncertain) and not safe:
                raise GuardRefused("page_write_blocked")
            if state.uncertain:
                raise GuardRefused("prior_request_uncertain")
            state.inflight += 1
            request_id = uuid.uuid4().hex if not safe and self.journal is not None else ""
            if request_id:
                try:
                    with sqlite3.connect(self.journal) as db:
                        db.execute("INSERT INTO requests VALUES (?, ?)", (request_id, actor))
                except Exception:
                    state.inflight -= 1
                    raise GuardRefused("guard_journal_unavailable") from None
        acknowledged = False
        try:
            yield
            acknowledged = True
        finally:
            with self._condition:
                state.inflight -= 1
                if not acknowledged:
                    state.uncertain = True
                elif request_id:
                    try:
                        with sqlite3.connect(self.journal) as db:
                            db.execute("DELETE FROM requests WHERE id=?", (request_id,))
                    except Exception:
                        state.uncertain = True
                self._condition.notify_all()


__all__ = ["NativePageGuard", "GuardRefused"]
