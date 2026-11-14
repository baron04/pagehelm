"""Server-to-browser publication and synchronous rendezvous operations."""
from __future__ import annotations

import time

from ..schemas import (ActionEnvelope, ActionResultEnvelope, ApprovalDecision,
                       ApprovalRequest, ClientHello, ObservationEnvelope, TaskEvent)
from ..schemas.browser import SCOPED_FORM_HANDOFF_CAPABILITY
from .channel_state import TURN_PAGE_EVENTS
from .panel_view import action_target
from .protocol import (RemoteProtocolError, _copy_json, _copy_wire, _event_sequence,
                       _safe_payload)


class ChannelServer:
    def enqueue_action(self, action: ActionEnvelope) -> TaskEvent:
        with self._condition:
            self._ensure_open()
            action = _copy_wire(action)
            if action.session_id != self.session_id:
                raise RemoteProtocolError("action belongs to another session", status=400)
            if self.principal is not None and action.turn_id != self._active_message_id:
                raise RemoteProtocolError(
                    "action does not belong to the active principal-bound turn",
                    code="turn_mismatch", status=403)
            if action.turn_id in self._cancelled_message_ids:
                raise RemoteProtocolError(
                    "the task was cancelled before action dispatch",
                    code="turn_cancelled",
                )
            _safe_payload(action.to_json())
            if self._hello is None:
                raise RemoteProtocolError("cannot act before the embedded runtime hello")
            capability = {
                "observe": "dom.observe", "click": "dom.click", "fill": "dom.fill",
                "select": "dom.select", "submit": "dom.submit", "goto": "page.goto",
                "back": "page.back", "scroll": "page.scroll", "wait_for": "page.wait",
                "apply_facet": "page.apply_facet", "highlight": "page.highlight",
                "handoff_form": "page.handoff_form", "reveal": "page.reveal",
                "form_model": "page.form_model", "form_submit": "page.form_submit",
            }[action.command.action]
            form_fields = action.command.args.get("fields")
            if action.command.action == "handoff_form" and (
                    "submit" in action.command.args or (isinstance(form_fields, list) and any(
                        isinstance(item, dict) and ("scope" in item or item.get("binding") == "label")
                        for item in form_fields))):
                capability = SCOPED_FORM_HANDOFF_CAPABILITY
            if capability not in self._hello.capabilities:
                raise RemoteProtocolError(
                    f"embedded runtime does not advertise {capability}",
                    code="capability_refused",
                )
            previous = self._actions.get(action.action_id)
            if previous is not None:
                if previous.content_digest != action.content_digest:
                    raise RemoteProtocolError("action_id was reused with different content")
                event = next(event for event in self._events
                             if event.kind == "action"
                             and event.payload["action_id"] == action.action_id)
                return _copy_wire(event)
            if len(self._actions) >= self.limits.max_actions:
                self._capacity_exhausted("action")
            if self._pending_action_id:
                raise RemoteProtocolError("another action is already in flight", code="action_in_flight")
            observation = self._latest_observation
            if observation is None:
                raise RemoteProtocolError("cannot act before the first observation")
            if (action.based_on_observation_id != observation.observation_id
                    or action.page_epoch != observation.page_epoch):
                raise RemoteProtocolError("action is based on a stale observation", code="stale_observation")
            if action.deadline <= self._now_ms():
                raise RemoteProtocolError("action deadline has already expired", code="deadline_expired")
            event = self._append_event("action", action.to_json(), turn_id=action.turn_id)
            self._actions[action.action_id] = action
            self._action_observations[action.action_id] = _copy_wire(observation)
            self._action_targets[action.action_id] = action_target(
                action.command.to_json(),
                {"elements": [element.to_json() for element in observation.observation.elements]})
            self._pending_action_id = action.action_id
            return event

    def publish(self, kind: str, payload: dict, *, turn_id: str = "") -> TaskEvent:
        if kind == "action":
            raise ValueError("use enqueue_action for action events")
        with self._condition:
            self._ensure_open()
            _safe_payload(payload)
            return self._append_event(kind, _copy_json(payload), turn_id=turn_id)

    def request_approval(self, request: ApprovalRequest) -> TaskEvent:
        """Publish one user-visible request while retaining no ControlPlan on the wire."""
        with self._condition:
            self._ensure_open()
            request = _copy_wire(request)
            if request.details and (self._hello is None
                                    or "approval.details.v1" not in self._hello.capabilities):
                raise RemoteProtocolError(
                    "the browser cannot display complete write details",
                    code="approval_details_unsupported", status=409)
            _safe_payload(request.to_json())
            if self.principal is None:
                if request.principal_digest or request.turn_id or request.operation_id:
                    raise RemoteProtocolError(
                        "session has no principal-bound approval context",
                        code="approval_context_mismatch", status=403)
            elif (request.principal_digest != self.principal.binding_digest
                  or request.turn_id != self._active_message_id
                  or not request.operation_id
                  or request.expires_at > self.principal.expires_at):
                raise RemoteProtocolError(
                    "approval context does not match the active principal and turn",
                    code="approval_context_mismatch", status=403)
            if request.expires_at <= self._now_ms():
                raise RemoteProtocolError(
                    "cannot publish an already expired approval",
                    code="approval_expired",
                )
            previous = self._approvals.get(request.approval_id)
            if previous is not None:
                if previous.content_digest != request.content_digest:
                    raise RemoteProtocolError(
                        "approval_id was reused with different content",
                        code="approval_conflict",
                    )
                return next(_copy_wire(event) for event in self._events
                            if event.kind == "approval_required"
                            and event.payload["approval_id"] == request.approval_id)
            if len(self._approvals) >= self.limits.max_approvals:
                self._capacity_exhausted("approval")
            if self._pending_approval_id:
                raise RemoteProtocolError(
                    "another approval is already pending",
                    code="approval_in_flight",
                )
            event = self._append_event(
                "approval_required", request.to_json(), turn_id=request.turn_id)
            self._approvals[request.approval_id] = request
            self._pending_approval_id = request.approval_id
            self._condition.notify_all()
            return event

    def events_after(self, event_id: str = "", *, lease_id: str,
                     wait_s: float = 0) -> list[TaskEvent]:
        after = _event_sequence(event_id)
        with self._condition:
            self._require_lease(lease_id)
            if after > len(self._events):
                raise RemoteProtocolError("Last-Event-ID is ahead of this session")
            if len(self._events) <= after and wait_s > 0:
                self._condition.wait_for(
                    lambda: (self._closed or len(self._events) > after
                             or lease_id not in self._lease_hellos),
                    timeout=self._bounded_wait(wait_s),
                )
            self._require_lease(lease_id)
            if len(self._events) <= after:
                self._ensure_open()
            # An action, and a confirmation to work the page, are for the page the turn runs
            # on; the other pages follow the conversation and stay free to ask.
            return [_copy_wire(event) for event in self._events[after:]
                    if event.kind not in TURN_PAGE_EVENTS
                    or self._event_leases.get(event.sequence) == lease_id]

    # -- synchronous server adapter waits ------------------------------------------
    def wait_for_client(self, timeout_s: float) -> ClientHello:
        with self._condition:
            ready = self._condition.wait_for(lambda: self._closed or self._hello is not None,
                                             timeout=self._bounded_wait(timeout_s))
            if self._hello is not None:
                self._ensure_open()
                return _copy_wire(self._hello)
            self._ensure_open()
            if not ready:
                raise TimeoutError("timed out waiting for embedded runtime hello")
            raise TimeoutError("timed out waiting for embedded runtime hello")

    def wait_for_observation(self, *, newer_than: int = -1, timeout_s: float) -> ObservationEnvelope:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._closed or (self._latest_observation is not None
                                         and self._latest_observation.page_epoch > newer_than),
                timeout=self._bounded_wait(timeout_s),
            )
            if (self._latest_observation is not None
                    and self._latest_observation.page_epoch > newer_than):
                self._ensure_open()
                return _copy_wire(self._latest_observation)
            self._ensure_open()
            if not ready:
                raise TimeoutError("timed out waiting for a fresh page observation")
            raise TimeoutError("timed out waiting for a fresh page observation")

    def wait_for_result(self, action_id: str, *, timeout_s: float) -> ActionResultEnvelope:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: (self._closed or action_id in self._results
                         or action_id in self._cancelled_action_ids),
                timeout=self._bounded_wait(timeout_s),
            )
            if action_id in self._results:
                return _copy_wire(self._results.pop(action_id))
            if action_id in self._cancelled_action_ids:
                raise RemoteProtocolError(
                    "the task was cancelled before browser execution",
                    code="turn_cancelled",
                )
            self._ensure_open()
            if not ready:
                raise TimeoutError(f"timed out waiting for result of {action_id}")
            raise TimeoutError(f"timed out waiting for result of {action_id}")

    def wait_for_message(self, *, timeout_s: float | None = None) -> dict:
        """Wait for the next message this session should run.

        With no timeout this means *until the session ends*, which is longer than the
        lifetime the session had when the wait began: a renewal extends that lifetime while
        the caller is parked here. Returning `TimeoutError` at the old expiry ended the
        worker thread of a session that was still perfectly alive, and the session went on
        accepting messages nobody would ever run -- the person saw their question arrive
        and then nothing at all, for as long as they were willing to wait.
        """
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._condition:
            while True:
                remaining_lifetime = self._remaining_lifetime_s()
                effective_timeout = (
                    remaining_lifetime if deadline is None
                    else min(max(0.0, deadline - time.monotonic()), remaining_lifetime))
                self._condition.wait_for(
                    lambda: self._closed or (bool(self._messages)
                                             and not self._active_message_id),
                    timeout=effective_timeout,
                )
                if self._messages and not self._active_message_id:
                    self._ensure_open()
                    message = _copy_json(self._messages.popleft())
                    if self._durable_store is not None:
                        self._durable_store.transition(
                            self._owner, message["message_id"], "running")
                    self._active_message_id = message["message_id"]
                    # The turn runs on the page that sent it.
                    self._activate_lease(
                        self._message_leases.pop(message["message_id"], ""))
                    return message
                # Raises if the session really is over; otherwise it outlived this wait.
                self._ensure_open()
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for a user message")

    def wait_for_approval(self, approval_id: str, *, timeout_s: float) -> ApprovalDecision:
        with self._condition:
            request = self._approvals.get(approval_id)
            if request is None:
                raise RemoteProtocolError(
                    "cannot wait for an unknown approval",
                    code="unknown_approval",
                    status=404,
                )
            remaining = max(0.0, (request.expires_at - self._now_ms()) / 1000)
            ready = self._condition.wait_for(
                lambda: self._closed or approval_id in self._approval_decisions,
                timeout=min(self._bounded_wait(timeout_s), remaining),
            )
            decision = self._approval_decisions.get(approval_id)
            if decision is not None:
                return _copy_wire(decision)
            self._ensure_open()
            if request.expires_at <= self._now_ms():
                # Nobody can answer it any more, so the session stops offering it. Leaving
                # it pending meant every later projection of the timeline handed the panel
                # a card that could only be refused -- it came back after a reload, and
                # answering it said the confirmation was no longer valid.
                if self._pending_approval_id == approval_id:
                    self._pending_approval_id = ""
                    self._condition.notify_all()
                raise RemoteProtocolError(
                    "approval request expired while awaiting a decision",
                    code="approval_expired",
                )
            if not ready:
                raise TimeoutError(f"timed out waiting for approval {approval_id}")
            raise TimeoutError(f"timed out waiting for approval {approval_id}")

    def close(self, reason: str = "session_closed") -> None:
        with self._condition:
            self._terminate(reason)
