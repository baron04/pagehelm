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
    "finished", "needs_input", "refused", "cancelled", "indeterminate", "failed",
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
            self._pending_clarification = None
            return dropped

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
                or any(not isinstance(item, str) or not item.strip() or len(item) > 200
                       for item in choices)
                or len(set(item.strip() for item in choices)) != len(choices)):
            raise ValueError("clarification choices are invalid")
        if kind == "single_choice" and len(choices) < 2:
            raise ValueError("single_choice clarification requires at least two choices")
        if kind == "free_text" and choices:
            raise ValueError("free_text clarification cannot carry choices")
        normalized = _redact_json({**value, "question": value["question"].strip(),
                                   "choices": [item.strip() for item in choices]})
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

    @property
    def content_digest(self) -> str:
        with self._lock:
            value = self._model_value()
            return hashlib.sha256(_canonical(value)).hexdigest()
