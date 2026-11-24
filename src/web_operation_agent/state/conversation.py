"""Bounded, application-neutral context retained across turns in one conversation."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from collections import deque
from contextlib import closing, contextmanager

from ..business.runtime import BusinessToolResult
from ..privacy import redact_sensitive_assignments, redact_session_handoffs


_ALLOWED_STATUSES = frozenset({
    "finished", "needs_input", "declined", "refused", "cancelled", "indeterminate", "failed",
})

_CLARIFICATION_KINDS = frozenset({"free_text", "single_choice"})


def _redact_json(value):
    if isinstance(value, str):
        return redact_sensitive_assignments(redact_session_handoffs(value))
    if isinstance(value, dict):
        return {str(key): _redact_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_json(item) for item in value]
    return value


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("conversation state must contain finite JSON") from exc


class ConversationState:
    """Keep a small reference history without granting facts, routes, or authority.

    This store is deliberately in-memory. Its model projection is historical application
    data: a new turn must refresh page or API facts before acting. A future durable store
    can implement the same projection after defining encryption, retention, and recovery.
    """

    def __init__(self, *, max_turns: int = 4, max_bytes: int = 32 * 1024,
                 max_business_bytes: int = 16 * 1024):
        for name, value, ceiling in (
                ("max_turns", max_turns, 20),
                ("max_bytes", max_bytes, 256 * 1024),
                ("max_business_bytes", max_business_bytes, 128 * 1024)):
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 1 <= value <= ceiling):
                raise ValueError(f"{name} must be an integer from 1 to {ceiling}")
        if max_business_bytes > max_bytes:
            raise ValueError("max_business_bytes cannot exceed max_bytes")
        self.max_turns = max_turns
        self.max_bytes = max_bytes
        self.max_business_bytes = max_business_bytes
        self._turns: deque[dict] = deque()
        self._pending_clarification: dict | None = None
        self._page_operation_granted: bool = False
        self._lock = threading.RLock()

    @property
    def page_operation_granted(self) -> bool:
        """Whether the user has granted page-operation consent for this conversation."""
        with self._lock:
            return self._page_operation_granted

    @page_operation_granted.setter
    def page_operation_granted(self, value: bool) -> None:
        if not isinstance(value, bool):
            raise TypeError("page_operation_granted must be boolean")
        with self._lock:
            self._page_operation_granted = value

    def grant_page_operation(self) -> None:
        """Record conversation-level consent for the assistant to work the page."""
        self.page_operation_granted = True

    @property
    def turn_count(self) -> int:
        with self._lock:
            return len(self._turns)

    def clear(self) -> int:
        """Forget every retained turn and return how many were dropped.

        This is what "new conversation" means: the model's reference history ends here.
        It does not touch the session's event log or its audit ledger -- those are the
        record of what happened, and a user starting a new topic does not unmake them.
        """
        with self._lock:
            dropped = len(self._turns)
            self._turns.clear()
            self._pending_clarification = None
            self._page_operation_granted = False
            return dropped

    def last_user_text(self) -> str:
        """The person's words in the latest retained turn (redacted as stored)."""
        with self._lock:
            return str(self._turns[-1]["user"]) if self._turns else ""

    @property
    def pending_clarification(self) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._pending_clarification)

    def record(self, *, message_id: str, user_text: str, assistant_text: str,
               status: str, business_result: BusinessToolResult | None = None,
               clarification: dict | None = None) -> None:
        if (not isinstance(message_id, str) or not message_id.strip()
                or len(message_id) > 200):
            raise ValueError("conversation message_id is invalid")
        if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 2_000:
            raise ValueError("conversation user_text is invalid")
        if (not isinstance(assistant_text, str) or not assistant_text.strip()
                or len(assistant_text) > 20_000):
            raise ValueError("conversation assistant_text is invalid")
        if status not in _ALLOWED_STATUSES:
            raise ValueError("conversation status is invalid")
        if business_result is not None and not isinstance(business_result, BusinessToolResult):
            raise TypeError("business_result must be a BusinessToolResult or None")
        if status == "needs_input":
            clarification = self._validate_clarification(clarification)
        elif clarification is not None:
            raise ValueError("clarification is only valid for needs_input")
        turn = {
            "message_id": message_id,
            "user": _redact_json(user_text.strip()),
            "assistant": _redact_json(assistant_text.strip()),
            "status": status,
        }
        if business_result is not None and (status == "finished" or isinstance(business_result.content.get("outcomes"), list)
                and business_result.content["outcomes"]
                and all(isinstance(item, dict) and item.get("verified") is True for item in business_result.content["outcomes"])):
            facts = _redact_json(business_result.content)
            if len(_canonical(facts)) <= self.max_business_bytes:
                turn["prior_business_facts"] = facts
            else:
                turn["prior_business_facts"] = {
                    "omitted": "prior business result exceeded the conversation limit",
                    "content_digest": business_result.content_digest,
                }
        with self._lock:
            self._turns.append(turn)
            if status == "needs_input":
                self._pending_clarification = clarification
            elif status in {"finished", "refused", "cancelled", "indeterminate"}:
                self._pending_clarification = None
            while len(self._turns) > self.max_turns:
                self._turns.popleft()
            while self._turns and len(_canonical(self._model_value())) > self.max_bytes:
                self._turns.popleft()

    def _validate_clarification(self, value: object) -> dict:
        if not isinstance(value, dict) or set(value) != {
                "schema_version", "clarification_id", "task_id", "question",
                "response_kind", "choices"}:
            raise ValueError("needs_input requires one complete clarification")
        if value.get("schema_version") != 1:
            raise ValueError("clarification schema_version is unsupported")
        for key, limit in (("clarification_id", 100), ("task_id", 100),
                           ("question", 500)):
            item = value.get(key)
            if not isinstance(item, str) or not item.strip() or len(item) > limit:
                raise ValueError(f"clarification {key} is invalid")
        kind = value.get("response_kind")
        choices = value.get("choices")
        if kind not in _CLARIFICATION_KINDS:
            raise ValueError("clarification response_kind is invalid")
        if (not isinstance(choices, list) or len(choices) > 8
                or any(not isinstance(item, dict) or set(item) != {
                           "choice_id", "label", "record_ref"}
                       for item in choices)):
            raise ValueError("clarification choices are invalid")
        normalized_choices = []
        for item in choices:
            normalized_item = {}
            for key, limit in (("choice_id", 100), ("label", 200), ("record_ref", 500)):
                entry = item.get(key)
                if not isinstance(entry, str) or not entry.strip() or len(entry.strip()) > limit:
                    raise ValueError(f"clarification choice {key} is invalid")
                normalized_item[key] = entry.strip()
            normalized_choices.append(normalized_item)
        for key in ("choice_id", "label", "record_ref"):
            if len({item[key] for item in normalized_choices}) != len(normalized_choices):
                raise ValueError(f"clarification choice {key} values must be distinct")
        if kind == "single_choice" and len(choices) < 1:
            raise ValueError("single_choice clarification requires at least one choice")
        if kind == "free_text" and choices:
            raise ValueError("free_text clarification cannot carry choices")
        normalized = _redact_json({**value, "question": value["question"].strip(),
                                   "choices": normalized_choices})
        if len(_canonical({"schema_version": 1, "prior_turns": [],
                           "pending_clarification": normalized})) > self.max_bytes:
            raise ValueError("clarification exceeds the conversation limit")
        return normalized

    def _model_value(self) -> dict:
        value = {"schema_version": 1, "prior_turns": list(self._turns)}
        if self._pending_clarification is not None:
            value["pending_clarification"] = self._pending_clarification
        return value

    def model_context(self) -> dict | None:
        with self._lock:
            if not self._turns and self._pending_clarification is None:
                return None
            value = self._model_value()
            encoded = _canonical(value)
            if len(encoded) > self.max_bytes:
                raise RuntimeError("bounded conversation state exceeded its configured limit")
            return copy.deepcopy(value)

    def snapshot(self) -> dict:
        """Return the bounded, already-redacted value suitable for durable storage."""
        with self._lock:
            value = self._model_value()
            if len(_canonical(value)) > self.max_bytes:
                raise RuntimeError("bounded conversation state exceeded its configured limit")
            return copy.deepcopy(value)

    def restore(self, value: object) -> None:
        """Restore a persisted model projection after validating every field again."""
        if not isinstance(value, dict) or set(value) - {
                "schema_version", "prior_turns", "pending_clarification"}:
            raise ValueError("persisted conversation state has invalid fields")
        if value.get("schema_version") != 1:
            raise ValueError("persisted conversation state version is unsupported")
        turns = value.get("prior_turns")
        if not isinstance(turns, list) or len(turns) > self.max_turns:
            raise ValueError("persisted conversation turns are invalid")
        checked: list[dict] = []
        for turn in turns:
            if (not isinstance(turn, dict)
                    or set(turn) - {"message_id", "user", "assistant", "status",
                                    "prior_business_facts"}
                    or not {"message_id", "user", "assistant", "status"} <= set(turn)):
                raise ValueError("persisted conversation turn has invalid fields")
            message_id, user, assistant, status = (
                turn["message_id"], turn["user"], turn["assistant"], turn["status"])
            if (not isinstance(message_id, str) or not message_id.strip()
                    or len(message_id) > 200
                    or not isinstance(user, str) or not user.strip() or len(user) > 2_000
                    or not isinstance(assistant, str) or not assistant.strip()
                    or len(assistant) > 20_000 or status not in _ALLOWED_STATUSES):
                raise ValueError("persisted conversation turn is invalid")
            if "prior_business_facts" in turn:
                facts = turn["prior_business_facts"]
                if len(_canonical(facts)) > self.max_business_bytes:
                    raise ValueError("persisted business context exceeds its limit")
            checked.append(_redact_json(copy.deepcopy(turn)))
        pending = value.get("pending_clarification")
        if pending is not None:
            pending = self._validate_clarification(pending)
        restored = {"schema_version": 1, "prior_turns": checked}
        if pending is not None:
            restored["pending_clarification"] = pending
        if len(_canonical(restored)) > self.max_bytes:
            raise ValueError("persisted conversation state exceeds its limit")
        with self._lock:
            self._turns = deque(checked)
            self._pending_clarification = copy.deepcopy(pending)

    @property
    def content_digest(self) -> str:
        with self._lock:
            value = self._model_value()
            return hashlib.sha256(_canonical(value)).hexdigest()


_CONVERSATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")


class SQLiteConversationState(ConversationState):
    """One bounded conversation projection persisted for restart recovery.

    The database holds user-visible history and typed business projections, so it is a
    private deployment asset. It contains no capability, approval, cookie, API key, or
    execution authority. A restored reference still has to be re-read by the application
    before it can support a current claim or action.
    """

    def __init__(self, path: str | os.PathLike[str], conversation_id: str, **limits):
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID.fullmatch(
                conversation_id):
            raise ValueError("conversation_id is invalid")
        self.path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        self.conversation_id = conversation_id
        parent = os.path.dirname(self.path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("conversation store must be a regular file")
        finally:
            os.close(fd)
        os.chmod(self.path, 0o600)
        super().__init__(**limits)
        with self._connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                "CREATE TABLE IF NOT EXISTS conversation_contexts ("
                "conversation_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                "updated INTEGER NOT NULL, "
                "page_operation_granted INTEGER NOT NULL DEFAULT 0)")
            columns = {row[1] for row in database.execute(
                "PRAGMA table_info(conversation_contexts)")}
            if "page_operation_granted" not in columns:
                database.execute(
                    "ALTER TABLE conversation_contexts "
                    "ADD COLUMN page_operation_granted INTEGER NOT NULL DEFAULT 0")
            row = database.execute(
                "SELECT payload, page_operation_granted FROM conversation_contexts "
                "WHERE conversation_id=?",
                (conversation_id,)).fetchone()
        if row is not None:
            try:
                persisted = json.loads(row[0])
            except (TypeError, ValueError) as exc:
                raise ValueError("persisted conversation state is invalid JSON") from exc
            if isinstance(row[1], bool) or row[1] not in (0, 1):
                raise ValueError("persisted page_operation_granted is invalid")
            super().restore(persisted)
            with self._lock:
                self._page_operation_granted = bool(row[1])

    @contextmanager
    def _connect(self):
        database = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        database.execute("PRAGMA synchronous=FULL")
        try:
            yield database
        finally:
            database.close()

    def _persist(self) -> None:
        with self._lock:
            payload = _canonical(self.snapshot()).decode("utf-8")
            granted = int(self._page_operation_granted)
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "INSERT OR REPLACE INTO conversation_contexts "
                "(conversation_id, payload, updated, page_operation_granted) "
                "VALUES (?, ?, ?, ?)",
                (self.conversation_id, payload, int(time.time() * 1000), granted))
            database.commit()

    @ConversationState.page_operation_granted.setter
    def page_operation_granted(self, value: bool) -> None:
        if not isinstance(value, bool):
            raise TypeError("page_operation_granted must be boolean")
        with self._lock:
            self._page_operation_granted = value
            self._persist()

    def clear(self) -> int:
        dropped = super().clear()
        self._persist()
        return dropped

    def record(self, **fields) -> None:
        super().record(**fields)
        self._persist()


def prune_sqlite_conversations(path: str | os.PathLike[str], *,
                               older_than_ms: int,
                               now_ms: int | None = None) -> int:
    """Remove inactive bounded projections without creating a missing database."""
    target = os.path.abspath(os.path.expanduser(os.fspath(path)))
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if (isinstance(older_than_ms, bool) or not isinstance(older_than_ms, int)
            or isinstance(now, bool) or not isinstance(now, int)
            or older_than_ms < 0 or now < 0
            or older_than_ms > now - 86_400_000):
        raise ValueError("conversation retention cutoff must be at least one day old")
    if not os.path.exists(target):
        return 0
    with closing(sqlite3.connect(target, isolation_level=None, timeout=5)) as database:
        database.execute("BEGIN IMMEDIATE")
        removed = database.execute(
            "DELETE FROM conversation_contexts WHERE updated < ?",
            (older_than_ms,)).rowcount
        database.commit()
    return max(0, removed)
