"""Append-only session ledger.

One JSON object per line, written and fsync'd as it happens. A crash must not take the
evidence with it, and a later pass must never be able to improve the record: `append` only
ever adds, there is no update and no delete.

Domain objects still expose explicit redacted views (see `ControlPlan.redacted`).  As the
single durable session boundary, `append` also applies the shared narrow credential redactor
to every field before it enters memory or disk. The ledger records *that* a control plan
existed and its digest, never its content.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from ..privacy import redact_sensitive_assignments, redact_session_handoffs


class SessionLedger:
    def __init__(self, path: str | Path, run_id: str, *, base: dict | None = None,
                 event_sink=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        # Stamped onto every record. A run id alone does not say which conversation, which
        # user turn or which tenant a line belongs to, and without that an operator reading a
        # shared ledger cannot scope an incident. Injected once here rather than threaded
        # through the call sites, because a caller that can forget it will.
        self.base = {key: value for key, value in (base or {}).items()
                     if isinstance(key, str) and isinstance(value, str) and value}
        self._lock = threading.Lock()
        self._seq = 0
        self.events: list[dict] = []
        if event_sink is not None and not callable(getattr(event_sink, "record", None)):
            raise TypeError("event_sink must provide record or be None")
        self._event_sink = event_sink

    def append(self, event: str, **fields) -> dict:
        with self._lock:
            self._seq += 1
            fields = redact_sensitive_assignments(redact_session_handoffs(fields))
            rec = {"seq": self._seq, "t": round(time.time(), 3), "run_id": self.run_id,
                   **self.base, "event": event, **fields}
            self.events.append(rec)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            if self._event_sink is not None:
                try:
                    self._event_sink.record(rec)
                except Exception:
                    # Telemetry is diagnostic and may never change product semantics.
                    pass
            return rec

    def of_kind(self, event: str) -> list[dict]:
        return [e for e in self.events if e["event"] == event]

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for e in self.events:
            counts[e["event"]] = counts.get(e["event"], 0) + 1
        return {"run_id": self.run_id, **self.base,
                "events": self._seq, "by_kind": counts,
                "path": str(self.path)}
