"""Server-owned panel configuration and timeline projection."""
from __future__ import annotations

from .panel_view import (collapse_timeline, panel_contract, panel_limits,
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
            "session_expires_at": self.expires_at,
            "principal_expires_at": (self.principal.expires_at
                                     if self.principal is not None else 0),
        }

    def panel_timeline(self, *, lease_id: str, after: str = "", before: str = "",
                       limit: int = 80, wait_s: float = 0) -> dict:
        """Return the server's own projection of this session onto the panel's timeline.

        The panel keeps no history of its own: a full page navigation replays from here
        (F8a), and every bound it renders by comes from the server (F8b).  Paging is by the
        same event cursor the runtime already uses -- ``after`` follows the live edge,
        ``before`` walks backwards into history.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise RemoteProtocolError("timeline limit must be an integer from 1 to 500",
                                      status=400)
        if after and before:
            raise RemoteProtocolError("timeline takes after or before, not both", status=400)
        with self._condition:
            self._require_active_lease(lease_id)
            cursor = _event_sequence(after)
            if cursor > len(self._events):
                raise RemoteProtocolError("timeline cursor is ahead of this session")
            if after and len(self._events) <= cursor and wait_s > 0:
                self._condition.wait_for(
                    lambda: (self._closed or len(self._events) > cursor
                             or lease_id != self._active_lease_id),
                    timeout=self._bounded_wait(wait_s),
                )
                self._require_active_lease(lease_id)
            return self._panel_timeline(after=after, before=before, limit=limit)

    def _panel_timeline(self, *, after: str, before: str, limit: int) -> dict:
        """Project the event log for the panel; the caller holds ``self._condition``."""
        entries: list[dict] = []
        replayed: list[dict] = []
        for record in self.recovered_history:
            projected = project_history_record(record, index=len(replayed) + 1)
            if projected is not None:
                replayed.append(projected)
        live = [projected for projected in
                (project_event(event, at=self._event_times.get(event.sequence, 0))
                 for event in self._events) if projected is not None]
        whole = collapse_timeline(replayed + live)

        if after:
            floor = _event_sequence(after)
            entries = [entry for entry in whole if entry["sequence"] > floor]
            has_more = False
        elif before:
            ceiling = _event_sequence(before)
            older = [entry for entry in whole
                     if entry["sequence"] < ceiling or entry["sequence"] == 0]
            entries = older[-limit:]
            has_more = len(older) > limit
        else:
            entries = whole[-limit:]
            has_more = len(whole) > limit

        payload = {
            "schema_version": SCHEMA_VERSIONS["TaskEvent"],
            "protocol": panel_contract()["protocol"],
            "entries": entries,
            "cursor": self._events[-1].event_id if self._events else "",
            "oldest": entries[0]["id"] if entries else "",
            "has_more": has_more,
            "closed": self._closed,
            "close_reason": self._close_reason,
            "pending_approval": None,
        }
        if self._pending_approval_id:
            pending = self._approvals[self._pending_approval_id]
            # A card whose deadline has passed is not something to ask the person for. The
            # turn that raised it is on its way to ending; the panel is told what happened
            # by that ending, not by a control it cannot use.
            if pending.expires_at > self._now_ms():
                payload["pending_approval"] = pending.to_json()
        return payload
