"""Application-owned admission fence for native page operations.

All browser API traffic must pass through the integrating proxy. A window belongs to
one browser cookie and actor; all windows for that actor block mutation admission.
This deliberately pauses other browser sessions of the same actor as well. Beginning
a window waits for previously admitted requests to finish. A lost upstream response
poisons the actor's fence rather than declaring an uncertain request completed.

This is an HTTP admission boundary, not a database transaction sandbox. Only reviewed
GET handlers without persistence or asynchronous effects may run inside the window.
Direct upstream access, background jobs and external services are outside its scope.

A deployment may instead choose the method fence: inside the window every plain GET under
the admin API is admitted except the handlers its release scan lists as writing, and every
other method is refused. That trades the per-handler review for runtime enforcement, so
refused requests are kept as bounded evidence: a control that tried to persist while the
window was open is known by its attempt, not by a prior review of its source.
"""
from __future__ import annotations

from collections import deque
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


#: A plain admin API path: no encoding, empty or dot segments, matrix parameters or
#: backslashes, so a refused handler cannot be reached under another spelling.
_PLAIN_GET = re.compile(r"/admin-api(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)+")
_WRITING_GET = re.compile(r"/admin-api(?:/(?:[A-Za-z0-9_-][A-Za-z0-9._-]*|\{[A-Za-z0-9_]+\}|\*\*))+")
_REFUSALS_KEPT = 64
#: No window is acknowledged for longer than this.
_LONGEST_WINDOW_S = 120


def _writing_get_pattern(template: str) -> re.Pattern:
    """A scanned handler path; `{name}` is one segment and a final `**` is any remainder."""
    if (not isinstance(template, str) or not _WRITING_GET.fullmatch(template)
            or "**" in template[:-2]):
        raise ValueError("writing GET handlers must be exact admin API path templates")
    segments = template.casefold().split("/")[1:]
    wildcard = segments[-1] == "**"
    parts = []
    for segment in (segments[:-1] if wildcard else segments):
        parts.append("[^/]+" if segment.startswith("{")
                     else re.escape(segment))
    return re.compile("/" + "/".join(parts) + ("(?:/.*)?" if wildcard else ""))


@dataclass
class _Actor:
    inflight: int = 0
    opening: int = 0
    uncertain: bool = False
    windows: dict[str, float] = field(default_factory=dict)
    #: (sequence, method, path) of requests refused while a window was open; no query,
    #: body or credential is kept.
    refused: deque = field(default_factory=lambda: deque(maxlen=_REFUSALS_KEPT))
    refusals: int = 0


class NativePageGuard:
    """Bounded, in-process fence; contains neither credentials nor model context."""

    def __init__(self, read_paths: frozenset[str], *, clock=time.monotonic,
                 actor_limit: int = 2048, journal: Path | None = None,
                 writing_gets: frozenset[str] | None = None):
        if not isinstance(read_paths, frozenset) or any(
                not isinstance(path, str) or not re.fullmatch(r"/admin-api/[a-z0-9][a-z0-9/-]*", path)
                for path in read_paths):
            raise ValueError("reviewed reads must be exact admin API paths")
        if isinstance(actor_limit, bool) or not isinstance(actor_limit, int) or actor_limit < 1:
            raise ValueError("actor limit must be positive")
        if writing_gets is not None and (not isinstance(writing_gets, frozenset) or read_paths):
            raise ValueError("the method fence takes scanned writing GETs and no reviewed reads")
        #: None keeps the reviewed-read window. A set, possibly empty, selects the method
        #: fence and names the GET handlers that stay refused inside it.
        self.writing_gets = (None if writing_gets is None else
                             tuple(_writing_get_pattern(path) for path in sorted(writing_gets)))
        self.read_paths, self.clock, self.actor_limit = read_paths, clock, actor_limit
        self._actors: dict[str, _Actor] = {}
        self._condition = threading.Condition()
        self.journal = Path(journal) if journal is not None else None
        if self.journal is not None:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.journal) as db:
                db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, actor TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS windows (actor TEXT NOT NULL, owner TEXT NOT NULL, PRIMARY KEY(actor, owner))")
                # A write whose outcome was never learned stays unknown across a restart.
                for (actor,) in db.execute("SELECT actor FROM requests"):
                    if not re.fullmatch(r"[a-f0-9]{64}", actor):
                        raise ValueError("invalid guard journal actor")
                    self._actors[actor] = _Actor(uncertain=True)
                # A browser may still be working under a window acknowledged before the
                # restart. When it was to end is not known, but no window outlasts the
                # longest one: it is held that long from now and then ends like any other.
                for actor, owner in db.execute("SELECT actor, owner FROM windows"):
                    if not re.fullmatch(r"[a-f0-9]{64}", actor) or not re.fullmatch(r"[a-f0-9]{64}", owner):
                        raise ValueError("invalid guard journal window")
                    self._actors.setdefault(actor, _Actor()).windows[owner] = self.clock() + _LONGEST_WINDOW_S

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
                or not math.isfinite(ttl_s) or not 1 <= ttl_s <= _LONGEST_WINDOW_S
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

    def refusal_mark(self, actor: str) -> int:
        """A position in this actor's refusals, to ask later what was refused after it."""
        with self._condition:
            return self._actor(actor).refusals

    def refused_since(self, actor: str, mark: int) -> tuple[tuple[str, str], ...]:
        """(method, path) of requests refused inside a window after `mark`.

        More refusals than are kept raise rather than report a partial list as complete.
        """
        with self._condition:
            state = self._actor(actor)
            if isinstance(mark, bool) or not isinstance(mark, int) or not 0 <= mark <= state.refusals:
                raise GuardRefused("invalid_window")
            if state.refusals - mark > len(state.refused):
                raise GuardRefused("refusal_evidence_incomplete")
            return tuple((method, path) for sequence, method, path in state.refused
                         if sequence > mark)

    def _admits(self, method: str, parsed) -> bool:
        # Encoded/absolute/fragment paths cannot borrow a reviewed route's admission.
        plain = (method == "GET" and not parsed.scheme and not parsed.netloc
                 and not parsed.fragment and "%" not in parsed.path)
        if self.writing_gets is None:
            return plain and parsed.path in self.read_paths
        path = parsed.path
        return (plain and _PLAIN_GET.fullmatch(path) is not None
                and not any(part in {".", ".."} for part in path.split("/"))
                and not any(pattern.fullmatch(path.casefold()) for pattern in self.writing_gets))

    @contextmanager
    def request(self, actor: str, method: str, target: str):
        parsed = urlsplit(target)
        safe = self._admits(method, parsed)
        with self._condition:
            state = self._actor(actor)
            if (state.windows or state.opening or state.uncertain) and not safe:
                state.refusals += 1
                state.refused.append((state.refusals, str(method)[:16], parsed.path[:200]))
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
                    # A read that was not answered changed nothing; only a write whose
                    # answer was lost leaves the actor's state unknown.
                    state.uncertain = state.uncertain or not safe
                elif request_id:
                    try:
                        with sqlite3.connect(self.journal) as db:
                            db.execute("DELETE FROM requests WHERE id=?", (request_id,))
                    except Exception:
                        state.uncertain = True
                self._condition.notify_all()


__all__ = ["NativePageGuard", "GuardRefused"]
