"""A per-turn record of what the model was given and what it answered.

The audit ledger says what a turn did; this record says why. For each model request of
one turn it keeps the messages the request carried (each distinct message once), the
names of the tools it offered and the reply; for the turn, the person's words, the route
it started on, its terminal reason, duration and answer. A request is recorded only once
the provider has accepted it, so every message kept has already passed the egress check;
the whole record is checked again for credential forms before it is written, and only
its outcome is kept when one is found. The record holds the application data the model
was shown, so the host turns it on and bounds how long it is kept.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ..modeling.chat import require_chat_port
from ..privacy import (SessionHandoffPolicy, has_unredacted_sensitive_assignment,
                       redact_sensitive_assignments)

SCHEMA_VERSION = 1
_RECORD = re.compile(r"(\d{4,})\.json")


class TurnTrace:
    """One turn's model requests; `write` puts them in the turn's directory."""

    def __init__(self, directory: Path, handoffs: SessionHandoffPolicy):
        self.directory, self.handoffs = Path(directory), handoffs
        self._messages: list[dict] = []
        self._positions: dict[str, int] = {}
        self.calls: list[dict] = []
        self._deliveries: list[dict] = []
        self.first_route_kind: str = ""
        self.stop_reason: str = ""
        self.stage_timings: dict[str, float] = {}
        self.tool_timings: list[dict] = []
        self.checks: list[dict] = []

    def note_first_route(self, route_kind: str) -> None:
        """Record the first tool/route kind dispatched during this turn."""
        if not self.first_route_kind and isinstance(route_kind, str) and route_kind.strip():
            self.first_route_kind = route_kind.strip()[:80]

    def set_stop_reason(self, reason: str) -> None:
        """Record the programmatic termination/stop code for this turn."""
        if isinstance(reason, str) and reason.strip():
            self.stop_reason = reason.strip()[:120]

    def record_stage(self, stage: str, seconds: float) -> None:
        """Accumulate elapsed seconds for a named execution stage (ARC-08)."""
        if not isinstance(stage, str) or not stage.strip():
            return
        key = stage.strip()[:64]
        prev = self.stage_timings.get(key, 0.0)
        self.stage_timings[key] = round(prev + max(0.0, float(seconds)), 3)

    def record_tool_timing(self, *, step: int, tool: str, seconds: float,
                           ok: bool = True, error_code: str = "") -> None:
        """Append one structural tool-timing entry without free-form payload prose."""
        entry: dict[str, object] = {
            "step": int(step),
            "tool": str(tool or "")[:80],
            "seconds": round(max(0.0, float(seconds)), 3),
            "ok": bool(ok),
        }
        if error_code:
            entry["error_code"] = str(error_code)[:80]
        self.tool_timings.append(entry)

    def record_check(self, *, check: str, outcome: str, attempt: int,
                     answer_preserved: bool = False) -> None:
        """Append one structural ARC-12 programmatic check entry."""
        if not isinstance(check, str) or not check.strip():
            return
        self.checks.append({
            "check": check.strip()[:80],
            "outcome": str(outcome or "")[:40],
            "attempt": max(1, int(attempt)),
            "answer_preserved": bool(answer_preserved),
        })

    def record_delivery(self, *, reference: str, projection: dict, rendered_text: str,
                        presented_records: int, presentation_complete: bool,
                        source_reference: str = "") -> None:
        """Private server facts, never model assertions or ordinary audit payloads.

        A selected result may later fail completion review. Consumers must bind this
        receipt to the settled answer as well as the actor and turn before scoring it.
        """
        delivery = {"reference": reference, "projection": projection,
            "rendered_text": rendered_text, "presented_records": presented_records,
            "presentation_complete": presentation_complete}
        if source_reference:
            delivery["source_reference"] = source_reference
        self._deliveries.append(delivery)
        self._deliveries[:] = self._deliveries[-4:]

    def chat(self, delegate):
        return _TracedChat(delegate, self)

    def _position(self, message) -> int:
        key = json.dumps(message, sort_keys=True, ensure_ascii=False)
        if key not in self._positions:
            self._positions[key] = len(self._messages)
            self._messages.append(message)
        return self._positions[key]

    def record(self, messages, tools, reply, seconds: float) -> None:
        kept = {"content": (reply or {}).get("content") or ""}
        if (reply or {}).get("tool_calls"):
            kept["tool_calls"] = reply["tool_calls"]
        self.calls.append({"messages": [self._position(m) for m in messages],
                           "tools": [(t.get("function") or {}).get("name", "") for t in tools or ()],
                           "reply": kept, "seconds": round(seconds, 3)})

    def record_failure(self, error_type: str, seconds: float) -> None:
        # A request the provider refused may be the one that carried a credential.
        self.calls.append({"error": error_type, "seconds": round(seconds, 3)})

    def _clean(self, value):
        return redact_sensitive_assignments(self.handoffs.redact(value))

    def write(self, *, run_id: str, turn_id: str, prompt: str, start_route: str,
              terminal_reason: str, wall_seconds: float, answer: str, steps=(), actor=None) -> Path:
        outcome = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "turn_id": turn_id,
                   "terminal_reason": terminal_reason, "wall_seconds": wall_seconds}
        record = {**outcome, "prompt": self._clean(prompt), "start_route": self._clean(start_route),
                  "actor": actor or {}, "answer": self._clean(answer),
                  "steps": [self._clean(step) for step in steps],
                  "messages": self._messages, "calls": self.calls}
        if self._deliveries:
            record["server_deliveries"] = self._deliveries
        if self.first_route_kind:
            record["first_route_kind"] = self._clean(self.first_route_kind)
        if self.stop_reason:
            record["stop_reason"] = self._clean(self.stop_reason)
        if self.stage_timings:
            record["stage_timings"] = dict(self.stage_timings)
        if self.tool_timings:
            record["tool_timings"] = list(self.tool_timings)
        if self.checks:
            record["checks"] = list(self.checks)
        if (self.handoffs.has_unredacted(record, url_decode_depth=3)
                or has_unredacted_sensitive_assignment(record, url_decode_depth=3)):
            record = {**outcome, "withheld": "credential_form"}
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        number = 1 + max((int(m.group(1)) for p in self.directory.iterdir()
                          if (m := _RECORD.fullmatch(p.name))), default=0)
        path = self.directory / f"{number:04d}.json"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                       "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=1)
        return path


class _TracedChat:
    """Same ChatPort; records each request after the delegate returns."""

    def __init__(self, delegate, trace: TurnTrace):
        self._delegate = require_chat_port(delegate, role="traced chat delegate")
        self._trace = trace
        self.model = delegate.model
        self.meter = getattr(delegate, "meter", None)
        self.options = getattr(delegate, "options", None)

    def _call(self, method, messages, tools, run_id, kwargs):
        started = time.monotonic()
        try:
            reply = method(messages, tools, run_id, **kwargs)
        except Exception as exc:
            self._trace.record_failure(type(exc).__name__, time.monotonic() - started)
            raise
        self._trace.record(messages, tools, reply, time.monotonic() - started)
        return reply

    def complete(self, messages, tools, run_id, **kwargs):
        return self._call(self._delegate.complete, messages, tools, run_id, kwargs)

    def complete_cancellable(self, messages, tools, run_id, *, cancelled, **kwargs):
        return self._call(self._delegate.complete_cancellable, messages, tools, run_id,
                          {**kwargs, "cancelled": cancelled})


def prune_turn_traces(root: Path, days: int, *, now: float | None = None) -> int:
    """Delete turn records under ``root/*/turns/`` last written more than `days` days ago."""
    cutoff = (time.time() if now is None else now) - days * 86400
    removed = 0
    for path in Path(root).glob("*/turns/*.json"):
        if not _RECORD.fullmatch(path.name):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed
