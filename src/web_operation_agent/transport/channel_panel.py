"""Server-owned panel configuration and timeline projection."""
from __future__ import annotations

import hashlib
from bisect import bisect_left, bisect_right

from .panel_view import (append_timeline_entry, collapse_timeline, panel_contract, panel_limits,
                          project_event, project_history_record)
from ..schemas import SCHEMA_VERSIONS
from .protocol import RemoteProtocolError, _copy_json, _event_sequence


class ChannelPanel:
    def _panel_configuration(self) -> dict:
        """Every limit, state and server-sent string the panel renders by.

        The panel holds no copy of these.  F8b was two sets of constants drifting apart;
        one of them is now a file the server reads and the panel is handed.
        """
        contract = panel_contract()
        return {
            "protocol": contract["protocol"],
            "limits": panel_limits(),
            "labels": dict(contract["label_keys"]),
            "notices": dict(contract["notices"]),
            "panel_states": dict(contract["panel_states"]),
            "progress_states": _copy_json(contract["progress_states"]),
            "terminal_error_codes": list(contract["terminal_error_codes"]),
            "terminal_panel_state_by_code": dict(contract["terminal_panel_state_by_code"]),
            "nonterminal_error_states": dict(contract["nonterminal_error_states"]),
            "lapse_codes": list(contract["lapse_codes"]),
            "session_expires_at": self.expires_at,
            "principal_expires_at": (self.principal.expires_at
                                     if self.principal is not None else 0),
            "read_only_mode": bool(getattr(self, "read_only_mode", False)),
        }

    def panel_timeline(self, *, lease_id: str, after: str = "", before: str = "",
                       limit: int | None = None, wait_s: float = 0) -> dict:
        """Return the server's own projection of this session onto the panel's timeline.

        The panel keeps no history of its own: a full page navigation replays from here
        (F8a), and every bound it renders by comes from the server (F8b). The
        live event cursor follows the live edge with ``after``. The opaque ``oldest``
        cursor walks backwards into history with ``before``, including across recovery.
        """
        if limit is None:
            limit = panel_limits()["history_page_size"]
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise RemoteProtocolError("timeline limit must be an integer from 1 to 500",
                                      status=400)
        if after and before:
            raise RemoteProtocolError("timeline takes after or before, not both", status=400)
        with self._condition:
            self._require_lease(lease_id)
            cursor = _event_sequence(after)
            if cursor > len(self._events):
                raise RemoteProtocolError("timeline cursor is ahead of this session")
            if after and len(self._events) <= cursor and wait_s > 0:
                self._condition.wait_for(
                    lambda: (self._closed or len(self._events) > cursor
                             or lease_id not in self._lease_hellos),
                    timeout=self._bounded_wait(wait_s),
                )
                self._require_lease(lease_id)
            return self._panel_timeline(after=after, before=before, limit=limit,
                                        lease_id=lease_id)

    def _history_prefix(self) -> str:
        # Stable across process restarts, unique across sessions kept in one open panel.
        return "h" + hashlib.sha256(self.session_id.encode()).hexdigest()[:16] + ":"

    def _project_live_timeline(self) -> None:
        """Project only newly appended events; seek pages in the resulting sequence index."""
        for event in self._events[self._panel_projected:]:
            entry = project_event(event, at=self._event_times.get(event.sequence, 0),
                                  target=self._action_targets.get(
                                      str(event.payload.get("action_id") or "")))
            if entry is None:
                continue
            durable_sequence = self._event_durable_sequences.get(event.sequence)
            if durable_sequence is not None:
                entry["id"] = f"{self._history_prefix()}{durable_sequence:08d}"
                entry["position"] = {"stream": self._history_prefix(),
                                     "sequence": durable_sequence}
            previous_length = len(self._panel_entries)
            append_timeline_entry(self._panel_entries, entry, self._panel_observed_turns)
            if len(self._panel_entries) > previous_length:
                self._panel_sequences.append(entry["sequence"])
            elif self._panel_entries and self._panel_entries[-1] is entry:
                self._panel_sequences[-1] = entry["sequence"]
        self._panel_projected = len(self._events)

    def _recovered_page(self, *, before: int, limit: int) -> list[dict]:
        """Seek bounded raw pages until enough visible historical entries are found.

        Invisible outbox rows still advance the database cursor. The snapshot ceiling
        excludes this incarnation's rows, which the live projection already supplies.
        """
        entries: list[dict] = []
        ceiling = min(before, self._history_upper + 1)
        # Include the exclusive boundary while collapsing, then remove it. Otherwise a
        # repeated progress run crossing a page boundary is shown twice on adjacent pages.
        boundary = None
        if before <= self._history_upper:
            records = self._durable_store.history(self._owner, before=before + 1, limit=1)
            if records and records[0]["sequence"] == before:
                boundary = project_history_record(records[0], index=before)
        while ceiling > 1 and len(entries) < limit:
            records = self._durable_store.history(self._owner, before=ceiling, limit=100)
            if not records:
                break
            projected = []
            for record in records:
                entry = project_history_record(record, index=record["sequence"])
                if entry is not None:
                    entry["id"] = f"{self._history_prefix()}{record['sequence']:08d}"
                    entry["position"] = {"stream": self._history_prefix(),
                                         "sequence": record["sequence"]}
                    projected.append(entry)
            joined = collapse_timeline(projected + entries + ([boundary] if boundary else []))
            entries = [entry for entry in joined if entry is not boundary]
            ceiling = records[0]["sequence"]
        return entries[-limit:]

    def _panel_timeline(self, *, after: str, before: str, limit: int,
                        lease_id: str = "") -> dict:
        """Page live and durable history without loading the entire outbox."""
        self._project_live_timeline()
        live = self._panel_entries
        has_newer = False
        cursor = self._events[-1].event_id if self._events else ""
        if after:
            start = bisect_right(self._panel_sequences, _event_sequence(after))
            entries = live[start:start + limit]
            has_newer = start + limit < len(live)
            if has_newer:
                cursor = f"e{entries[-1]['sequence']:08d}"
            has_more = False
        else:
            history_before = self._history_upper + 1
            end = len(live)
            if before:
                prefix = self._history_prefix()
                if before.startswith(prefix):
                    if self._durable_store is None:
                        raise RemoteProtocolError("session has no durable history", status=400)
                    raw_sequence = before[len(prefix):]
                    if (len(raw_sequence) != 8 or not raw_sequence.isascii()
                            or not raw_sequence.isdigit() or int(raw_sequence) < 1):
                        raise RemoteProtocolError("invalid history cursor", status=400)
                    history_before = int(raw_sequence)
                    # Durable IDs increase with the live sequence too. Binary seek keeps
                    # historical paging independent of the number of loaded live events.
                    end = bisect_left(live, history_before,
                                      key=lambda entry: entry["position"]["sequence"])
                elif before.startswith("h"):
                    raise RemoteProtocolError("history cursor belongs to another session", status=400)
                else:
                    end = bisect_left(self._panel_sequences, _event_sequence(before))
            entries = live[max(0, end - limit - 1):end]
            if len(entries) < limit + 1 and self._history_upper:
                entries = self._recovered_page(
                    before=history_before, limit=limit + 1 - len(entries)) + entries
            has_more = len(entries) > limit
            entries = entries[-limit:]

        payload = {
            "schema_version": SCHEMA_VERSIONS["TaskEvent"],
            "protocol": panel_contract()["protocol"],
            "incarnation": self._incarnation,
            "entries": _copy_json(entries),
            "cursor": cursor,
            "oldest": entries[0]["id"] if entries else "",
            "has_more": has_more,
            "has_newer": has_newer,
            "closed": self._closed,
            "close_reason": self._close_reason,
            "pending_approval": None,
        }
        # The card is for the page the turn runs on; another page is free to ask.
        if self._pending_approval_id and lease_id == self._active_lease_id:
            pending = self._approvals[self._pending_approval_id]
            # A card whose deadline has passed is not something to ask the person for. The
            # turn that raised it is on its way to ending; the panel is told what happened
            # by that ending, not by a control it cannot use.
            if pending.expires_at > self._now_ms():
                payload["pending_approval"] = pending.to_json()
        return payload
