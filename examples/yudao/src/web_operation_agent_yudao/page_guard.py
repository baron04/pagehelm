"""Application-owned admission fence for native page operations.

All browser API traffic must pass through the integrating proxy. A window belongs to
one page and actor; all windows for that actor block mutation admission.
This deliberately pauses the assistant's own marked writes on every page it works.
Beginning a window waits for previously admitted requests to finish. A lost upstream response
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
_READS_KEPT = 256
#: No window is acknowledged for longer than this.
_LONGEST_WINDOW_S = 120
#: A window owner: the browser's generated token or the page instance id it names.
WINDOW_KEY = re.compile(r"[A-Za-z0-9._-]{1,128}")


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
class _WindowEvidence:
    """What one window's assistant-marked requests were seen doing; bounded, in memory.

    Requests that arrive without a window name collect under "" -- a window the
    page never announced, or a person's own read. Sequence numbers stay actor-wide,
    so merging buckets still orders entries.
    """
    #: (sequence, path, query) of the assistant-marked reads the page made.
    reads: deque = field(default_factory=lambda: deque(maxlen=_READS_KEPT))
    read_seen: int = 0
    #: (sequence, method, path) of requests refused while a window was open; no query,
    #: body or credential is kept.
    refused: deque = field(default_factory=lambda: deque(maxlen=_REFUSALS_KEPT))
    refuse_seen: int = 0
    #: (sequence, path) of scan-listed GETs let through as this window's own page load.
    passed: deque = field(default_factory=lambda: deque(maxlen=_REFUSALS_KEPT))
    #: Until when this window's page may make the GETs its own loading needs.
    page_load_until: float = 0.0


@dataclass
class _Actor:
    inflight: int = 0
    opening: int = 0
    uncertain: bool = False
    windows: dict[str, float] = field(default_factory=dict)
    refusals: int = 0
    #: An approved click's permit: armed by the server, activated by the browser's own
    #: next `begin`, ended by its `end`. It replaces the read-only window for that click.
    permit_pending: object = None
    permit_pending_owner: str = ""
    permit: object = None
    permit_owner: str = ""
    last_permit: object = None
    read_count: int = 0
    #: (sequence, method, path, named id, returned id) of the person's own writes the
    #: proxy saw the application accept; observation only, never admission data.
    user_writes: deque = field(default_factory=lambda: deque(maxlen=64))
    write_count: int = 0
    passes: int = 0
    #: What each window's marked requests did, by window owner.
    evidence: dict[str, _WindowEvidence] = field(default_factory=dict)


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
            with self._journal_db() as db:
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
                    if not re.fullmatch(r"[a-f0-9]{64}", actor) or not WINDOW_KEY.fullmatch(owner):
                        raise ValueError("invalid guard journal window")
                    self._actors.setdefault(actor, _Actor()).windows[owner] = self.clock() + _LONGEST_WINDOW_S

    @contextmanager
    def _journal_db(self):
        db = sqlite3.connect(self.journal)
        try:
            with db:
                yield db
        finally:
            db.close()

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
                with self._journal_db() as db:
                    for key in expired:
                        db.execute("DELETE FROM windows WHERE actor=? AND owner=?", (actor, key))
            except Exception:
                raise GuardRefused("guard_journal_unavailable") from None
        for key in expired:
            del state.windows[key]

    @staticmethod
    def _owner(owner):
        if not isinstance(owner, str) or not WINDOW_KEY.fullmatch(owner):
            raise GuardRefused("invalid_browser")

    @staticmethod
    def _evidence(state: _Actor, window: str) -> _WindowEvidence:
        bucket = state.evidence.get(window)
        if bucket is None:
            if len(state.evidence) >= 128:
                window = ""
                bucket = state.evidence.get(window)
            if bucket is None:
                bucket = state.evidence[window] = _WindowEvidence()
        return bucket

    @staticmethod
    def _buckets(state: _Actor, window):
        """The evidence buckets a query sees: one named window's plus unattributed ones."""
        if window is None:
            return tuple(state.evidence.values())
        named = state.evidence.get(window)
        plain = state.evidence.get("") if window else None
        return tuple(bucket for bucket in (named, plain) if bucket is not None)

    @staticmethod
    def _incomplete(seen: int, kept, mark: int) -> bool:
        """True when entries after `mark` may already have been dropped from `kept`."""
        return seen - len(kept) > sum(1 for sequence, *_ in kept if sequence <= mark)

    def arm_page_load(self, actor: str, ttl_s: float = 20.0, *, window: str = "") -> None:
        """The page about to open may load itself: its own GETs, scan-listed or not, pass.

        Only inside an open window and only for a few seconds. Whatever passed is kept (path
        only) for the turn's private record, so a turn that let one through is known and its
        environment restored afterwards. Reads the assistant makes for itself are not the
        page's and stay refused where they were.
        """
        with self._condition:
            state = self._actor(actor)
            evidence = self._evidence(state, window)
            evidence.page_load_until = max(evidence.page_load_until,
                                           self.clock() + max(1.0, min(ttl_s, 60.0)))

    def clear_page_load(self, actor: str, *, window: str = "") -> None:
        """The page has arrived or the assistant is doing something else: its own loading is over."""
        with self._condition:
            bucket = self._actor(actor).evidence.get(window)
            if bucket is not None:
                bucket.page_load_until = 0.0

    def page_loads_since(self, actor: str, mark: int = 0, *,
                         window: str | None = None) -> tuple[tuple[int, str], ...]:
        with self._condition:
            state = self._actor(actor)
            passed = sorted(entry for bucket in self._buckets(state, window)
                            for entry in bucket.passed)
            return tuple((sequence, path) for sequence, path in passed if sequence > mark)

    def page_load_mark(self, actor: str) -> int:
        with self._condition:
            return self._actor(actor).passes

    def arm_permit(self, actor: str, permit, *, owner: str = "") -> None:
        """Let the browser's next `begin` open this approved click instead of a window.

        `owner` names the window the approval was given in: only that page's `begin`
        may claim the permit, so a sibling page's routine begin cannot seize it. Empty
        keeps the old any-begin-claims behaviour for pages that name no window.
        """
        with self._condition:
            state = self._actor(actor)
            if state.uncertain:
                raise GuardRefused("prior_request_uncertain")
            if state.permit_pending is not None or state.permit is not None:
                raise GuardRefused("permit_in_use")
            if state.opening or state.inflight:
                raise GuardRefused("prior_request_pending")
            state.permit_pending = permit
            state.permit_pending_owner = owner
            state.last_permit = None

    def disarm_permit(self, actor: str):
        """Withdraw an armed or active permit; returns it with the log of what it decided."""
        with self._condition:
            state = self._actor(actor)
            permit = state.permit or state.permit_pending
            state.permit = state.permit_pending = None
            state.permit_owner = state.permit_pending_owner = ""
            if permit is not None:
                state.last_permit = permit
            self._condition.notify_all()
            return permit

    def permit_active(self, actor: str) -> bool:
        with self._condition:
            return self._actor(actor).permit is not None

    def finished_permit(self, actor: str):
        with self._condition:
            state = self._actor(actor)
            return state.last_permit

    def wait_idle(self, actor: str, timeout_s: float = 5.0) -> bool:
        """Until every request the actor has in flight is settled (the permit's log is final)."""
        deadline = time.monotonic() + timeout_s
        with self._condition:
            state = self._actor(actor)
            while state.inflight:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._condition.wait(left)
            return True

    def note_read(self, actor: str, target: str) -> None:
        """Remember a GET the signed-in person's page made, marked or not: which interfaces a
        page reads is a fact about the page, and a person who opened it before asking the
        assistant to work on it has made the same reads. Path and query only; nothing is held
        back or changed by knowing it."""
        parsed = urlsplit(target)
        with self._condition:
            state = self._actor(actor)
            state.read_count += 1
            evidence = self._evidence(state, "")
            evidence.read_seen += 1
            evidence.reads.append((state.read_count, parsed.path[:200], parsed.query[:300]))

    def read_mark(self, actor: str) -> int:
        with self._condition:
            return self._actor(actor).read_count

    def recent_reads(self, actor: str, *, window: str | None = None) -> tuple[tuple[str, str], ...]:
        """(path, query) of the most recent assistant-marked reads the page made."""
        with self._condition:
            state = self._actor(actor)
            reads = sorted(entry for bucket in self._buckets(state, window)
                           for entry in bucket.reads)
            return tuple((path, query) for _sequence, path, query in reads)

    def reads_since(self, actor: str, mark: int, *,
                    window: str | None = None) -> tuple[tuple[str, str], ...]:
        """(path, query) of the page's assistant-marked reads after `mark`."""
        with self._condition:
            state = self._actor(actor)
            buckets = self._buckets(state, window)
            reads = sorted(entry for bucket in buckets for entry in bucket.reads)
            seen = (state.read_count if window is None
                    else sum(bucket.read_seen for bucket in buckets))
            if self._incomplete(seen, reads, mark):
                raise GuardRefused("read_evidence_incomplete")
            return tuple((path, query) for sequence, path, query in reads if sequence > mark)

    def user_write_mark(self, actor: str) -> int:
        """A position after every user write observed so far, for `user_write_after`."""
        with self._condition:
            return self._actor(actor).write_count

    def note_user_write(self, actor: str, method: str, path: str,
                        record_id: str = "", created_id: str = "") -> None:
        """The application accepted the person's own request; recorded so a handoff
        turn can notice and read back. Nothing here admits or holds anything."""
        with self._condition:
            state = self._actor(actor)
            state.write_count += 1
            state.user_writes.append((state.write_count, str(method)[:8], str(path)[:200],
                                      str(record_id)[:80], str(created_id)[:80]))

    def user_write_after(self, actor: str, mark: int, prefixes, record_id: str,
                         create_only: bool = False):
        """The first accepted user write under `prefixes` after `mark`, or None.

        When `record_id` is given the write must name or return that id; with
        `create_only` only this application's create interface counts and the
        returned event carries the new id."""
        with self._condition:
            for sequence, method, path, wanted, created in self._actor(actor).user_writes:
                if sequence <= mark or not any(path.startswith(prefix) for prefix in prefixes):
                    continue
                if record_id and record_id not in {wanted, created}:
                    continue
                if create_only and path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] != "create":
                    continue
                return {"seq": sequence, "method": method, "path": path,
                        "record_id": created or wanted or record_id}
            return None

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
                pending = state.permit_pending
                if pending is not None:
                    if any(key != owner for key in state.windows):
                        raise GuardRefused("other_window_open")
                    if state.permit_pending_owner and state.permit_pending_owner != owner:
                        raise GuardRefused("other_window_open")
                    if state.permit is not None:
                        raise GuardRefused("permit_in_use")
                    if self.journal is not None:
                        try:
                            with self._journal_db() as db:
                                db.execute("DELETE FROM windows WHERE actor=? AND owner=?", (actor, owner))
                        except Exception:
                            raise GuardRefused("guard_journal_unavailable") from None
                    state.windows.pop(owner, None)
                    state.permit, state.permit_pending = pending, None
                    state.permit_owner, state.permit_pending_owner = owner, ""
                    return min(float(ttl_s), float(pending.ttl_s))
                if owner not in state.windows and len(state.windows) >= 128:
                    raise GuardRefused("guard_capacity")
                if self.journal is not None:
                    try:
                        with self._journal_db() as db:
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
            if state.permit is not None and state.permit_owner == owner:
                state.last_permit, state.permit, state.permit_owner = state.permit, None, ""
                self._condition.notify_all()
            if state.uncertain:
                raise GuardRefused("prior_request_uncertain")
            if self.journal is not None:
                try:
                    with self._journal_db() as db:
                        db.execute("DELETE FROM windows WHERE actor=? AND owner=?", (actor, owner))
                except Exception:
                    raise GuardRefused("guard_journal_unavailable") from None
            state.windows.pop(owner, None)
            bucket = state.evidence.get(owner)
            if bucket is not None:
                bucket.page_load_until = 0.0

    def active(self, actor: str, owner: str) -> bool:
        self._owner(owner)
        with self._condition:
            return owner in self._actor(actor).windows

    def protected(self, actor: str) -> bool:
        with self._condition:
            state = self._actor(actor)
            return bool(state.windows or state.opening or state.uncertain or state.permit)

    def window_held(self, actor: str) -> bool:
        """An acknowledged, unexpired window; a poisoned actor is not proof of one."""
        with self._condition:
            state = self._actor(actor)
            return bool(state.windows) and not state.uncertain

    def refusal_mark(self, actor: str) -> int:
        """A position in this actor's refusals, to ask later what was refused after it."""
        with self._condition:
            return self._actor(actor).refusals

    def refused_since(self, actor: str, mark: int, *,
                      window: str | None = None) -> tuple[tuple[str, str], ...]:
        """(method, path) of requests refused inside a window after `mark`.

        More refusals than are kept raise rather than report a partial list as complete.
        """
        with self._condition:
            state = self._actor(actor)
            if isinstance(mark, bool) or not isinstance(mark, int) or not 0 <= mark <= state.refusals:
                raise GuardRefused("invalid_window")
            buckets = self._buckets(state, window)
            refused = sorted(entry for bucket in buckets for entry in bucket.refused)
            seen = (state.refusals if window is None
                    else sum(bucket.refuse_seen for bucket in buckets))
            if self._incomplete(seen, refused, mark):
                raise GuardRefused("refusal_evidence_incomplete")
            return tuple((method, path) for sequence, method, path in refused
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

    def _writing_get(self, parsed) -> bool:
        """A plain GET the release scan lists as writing: the page's own loading may need it."""
        path = parsed.path
        return (self.writing_gets is not None and not parsed.scheme and not parsed.netloc
                and not parsed.fragment and "%" not in path and _PLAIN_GET.fullmatch(path) is not None
                and not any(part in {".", ".."} for part in path.split("/"))
                and any(pattern.fullmatch(path.casefold()) for pattern in self.writing_gets))

    @contextmanager
    def request(self, actor: str, method: str, target: str, body: bytes = b"", *,
                window: str = ""):
        """Admit one assistant-marked request; yields a ticket the proxy settles with the status.

        Inside an approved click only the permit decides a write: it admits the requests
        the card shows, each effect once, and refuses the rest.
        """
        if not WINDOW_KEY.fullmatch(window or ""):
            window = ""
        parsed = urlsplit(target)
        safe = self._admits(method, parsed)
        ticket = _Ticket()
        with self._condition:
            state = self._actor(actor)
            held = window in state.windows if window else bool(state.windows)
            foreign = (state.permit is not None and bool(window)
                       and window != state.permit_owner)
            # A marked request may name any well-formed window; evidence is attributed to a
            # name only while that window is open or owns the active permit, so an entry that
            # lied about its window cannot hide from the checks that read a window's bucket.
            if window and window not in state.windows and window != state.permit_owner:
                window = ""
            evidence = self._evidence(state, window)
            if safe and method == "GET":
                state.read_count += 1
                evidence.read_seen += 1
                evidence.reads.append((state.read_count, parsed.path[:200], parsed.query[:300]))
            loading = (not safe and str(method) == "GET" and held
                       and evidence.page_load_until > self.clock() and self._writing_get(parsed))
            # While a permit decides, every marked write of another window is refused;
            # the window the click was approved in is the permit's to judge. Without a
            # permit the ordinary rule stands: any open window fences non-safe methods.
            fenced = (foreign or state.opening or state.uncertain
                      or (state.permit is None and bool(state.windows)))
            if fenced and not safe and not loading:
                state.refusals += 1
                evidence.refuse_seen += 1
                evidence.refused.append((state.refusals, str(method)[:16], parsed.path[:200]))
                raise GuardRefused("page_write_blocked")
            if state.uncertain:
                raise GuardRefused("prior_request_uncertain")
            if loading:
                state.passes += 1
                evidence.passed.append((state.passes, parsed.path[:200]))
            permit = state.permit if not safe and not loading else None
            state.inflight += 1
        if permit is not None:
            try:
                decision = permit.admit(str(method), parsed.path, parsed.query, bytes(body))
            except Exception:  # noqa: BLE001 -- a permit that cannot decide admits nothing
                decision = None
            if decision is None or not decision.allowed:
                with self._condition:
                    state.inflight -= 1
                    state.refusals += 1
                    evidence.refuse_seen += 1
                    evidence.refused.append((state.refusals, str(method)[:16], parsed.path[:200]))
                    self._condition.notify_all()
                raise GuardRefused("page_write_blocked")
            ticket.permit, ticket.sequence = permit, decision.sequence
        request_id = ""
        with self._condition:
            if not safe and self.journal is not None:
                request_id = uuid.uuid4().hex
                try:
                    with self._journal_db() as db:
                        db.execute("INSERT INTO requests VALUES (?, ?)", (request_id, actor))
                except Exception:
                    state.inflight -= 1
                    raise GuardRefused("guard_journal_unavailable") from None
        acknowledged = False
        try:
            yield ticket
            acknowledged = True
        finally:
            if ticket.permit is not None:
                ticket.permit.settle(ticket.sequence, ticket.status if acknowledged else None,
                                     ticket.response if acknowledged else b"")
            with self._condition:
                state.inflight -= 1
                if not acknowledged:
                    # A read that was not answered changed nothing; only a write whose
                    # answer was lost leaves the actor's state unknown.
                    state.uncertain = state.uncertain or not safe
                elif request_id:
                    try:
                        with self._journal_db() as db:
                            db.execute("DELETE FROM requests WHERE id=?", (request_id,))
                    except Exception:
                        state.uncertain = True
                self._condition.notify_all()


@dataclass
class _Ticket:
    permit: object = None
    sequence: int = 0
    status: int | None = None
    response: bytes = b""


__all__ = ["NativePageGuard", "GuardRefused"]
