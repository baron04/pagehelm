"""Bounded, application-neutral context retained across turns in one conversation."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import deque

from .business import BusinessToolResult
from .privacy import redact_sensitive_assignments, redact_session_handoffs


_ALLOWED_STATUSES = frozenset({
    "finished", "refused", "cancelled", "indeterminate", "failed",
})


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
        self._lock = threading.RLock()

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
            return dropped

    def record(self, *, message_id: str, user_text: str, assistant_text: str,
               status: str, business_result: BusinessToolResult | None = None) -> None:
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
        turn = {
            "message_id": message_id,
            "user": _redact_json(user_text.strip()),
            "assistant": _redact_json(assistant_text.strip()),
            "status": status,
        }
        if status == "finished" and business_result is not None:
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
            while len(self._turns) > self.max_turns:
                self._turns.popleft()
            while self._turns and len(_canonical({
                    "schema_version": 1,
                    "prior_turns": list(self._turns),
            })) > self.max_bytes:
                self._turns.popleft()

    def model_context(self) -> dict | None:
        with self._lock:
            if not self._turns:
                return None
            value = {"schema_version": 1, "prior_turns": list(self._turns)}
            encoded = _canonical(value)
            if len(encoded) > self.max_bytes:
                raise RuntimeError("bounded conversation state exceeded its configured limit")
            return copy.deepcopy(value)

    @property
    def content_digest(self) -> str:
        with self._lock:
            value = {"schema_version": 1, "prior_turns": list(self._turns)}
            return hashlib.sha256(_canonical(value)).hexdigest()
