"""Authenticated browser-to-server messages for a remote channel."""
from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from dataclasses import replace

from .channel_state import _is_transient_sqlite_error
from .panel_view import panel_contract, panel_limits, progress_message
from ..business.mutation import MutationJournalError
from ..privacy import redact_sensitive_assignments, redact_session_handoffs
from ..state.identity import PrincipalError
from ..state.store import DurableStateError
from ..schemas import (SCHEMA_VERSIONS, ActionResultEnvelope, ApprovalDecision,
                       CancellationRequest, ClientHello, ObservationEnvelope,
                       ReconciliationRequest)
from .protocol import (RemoteProtocolError, RemoteSessionClosed,
                       _MAX_MESSAGE_ID_LENGTH, _MAX_PAGE_URL_LENGTH, _copy_json,
                       _copy_wire, _event_sequence, _origin, _remote_args_echo,
                       _safe_payload)

_LOGGER = logging.getLogger(__name__)


class ChannelIngress:
    def accept_hello(self, hello: ClientHello) -> dict:
        with self._condition:
            self._ensure_open()
            hello = _copy_wire(hello)
            if hello.session_id != self.session_id:
                raise RemoteProtocolError("hello belongs to another session", status=400)
            if _origin(hello.page_origin) not in self.allowed_origins:
                raise RemoteProtocolError("page origin is outside this session", code="origin_refused", status=403)
            if self.principal is None:
                if hello.application_id or hello.principal_digest:
                    raise RemoteProtocolError(
                        "session has no principal binding",
                        code="principal_mismatch", status=403)
            elif (hello.application_id != self.principal.application_id
                  or (hello.principal_digest != self.principal.binding_digest
                      and self._principal_grace.get(hello.principal_digest, 0)
                          <= self._now_ms())):
                raise RemoteProtocolError(
                    "runtime principal binding does not match the session",
                    code="principal_mismatch", status=403)
            cursor = _event_sequence(hello.last_event_id)
            if cursor > len(self._events):
                raise RemoteProtocolError("Last-Event-ID is ahead of this session")
            runtime_instance_id = hello.runtime_instance_id
            if runtime_instance_id in self._retired_runtime_instances:
                raise RemoteProtocolError("this document was replaced", code="runtime_replaced")
            existing_lease = self._runtime_leases.get(runtime_instance_id)
            if existing_lease:
                # The same page again, after a navigation or a reconnect.
                prior = self._lease_hellos.get(existing_lease)
                stable = ("session_id", "client_id", "runtime", "runtime_instance_id",
                          "capabilities", "page_origin", "application_id",
                          "principal_digest", "window_id")
                if prior is not None and any(getattr(prior, name) != getattr(hello, name)
                                             for name in stable):
                    raise RemoteProtocolError("runtime instance changed its hello contract")
                self._lease_hellos[existing_lease] = hello
                self._lease_seen[existing_lease] = self._now_ms()
                if existing_lease == self._active_lease_id:
                    self._hello = hello
                return {"accepted": True, "lease_id": existing_lease,
                        "server_event_id": self.latest_event_id,
                        "panel": self._panel_configuration()}

            # A tab keeps its client id across a full document navigation. Retire that
            # tab's old document without taking a different tab's current turn away.
            replaced = next((lease for lease, prior in self._lease_hellos.items()
                             if prior.client_id == hello.client_id), "")
            active_replaced = replaced == self._active_lease_id and bool(replaced)
            if replaced:
                if len(self._retired_runtime_instances) >= self.limits.max_runtime_instances:
                    self._capacity_exhausted("runtime_instance")
                self._retired_runtime_instances.add(
                    self._lease_hellos[replaced].runtime_instance_id)
                self._release_lease(replaced)
            if len(self._runtime_leases) >= self.limits.max_runtime_instances:
                # A page long gone makes room; the page the current turn runs on never does.
                idle = [lease for lease in self._lease_hellos if lease != self._active_lease_id]
                if not idle:
                    self._capacity_exhausted("runtime_instance")
                self._release_lease(min(idle, key=lambda lease: self._lease_seen.get(lease, 0)),
                                    evicted=True)
            lease_id = secrets.token_urlsafe(24)
            self._runtime_leases[runtime_instance_id] = lease_id
            self._lease_clients[lease_id] = hello.client_id
            self._lease_hellos[lease_id] = hello
            self._lease_seen[lease_id] = self._now_ms()
            if replaced:
                for message_id, held in list(self._message_leases.items()):
                    if held == replaced:
                        self._message_leases[message_id] = lease_id
                for sequence, held in list(self._event_leases.items()):
                    if held == replaced:
                        self._event_leases[sequence] = lease_id
            if active_replaced:
                self._activate_lease(lease_id)
            if not self._active_lease_id or self._active_lease_id not in self._lease_hellos:
                self._activate_lease(lease_id)
            self._condition.notify_all()
            return {"accepted": True, "lease_id": lease_id,
                    "server_event_id": self.latest_event_id,
                    "panel": self._panel_configuration()}

    def _validate_observation(self, envelope: ObservationEnvelope,
                              lease_id: str) -> tuple[bool, str]:
        if envelope.session_id != self.session_id:
            raise RemoteProtocolError("observation belongs to another session", status=400)
        if _origin(envelope.observation.url) not in self.allowed_origins:
            raise RemoteProtocolError("observation URL is outside this session", code="origin_refused", status=403)
        _safe_payload(envelope.to_json())
        fingerprint = envelope.content_digest
        previous = self._observation_digests.get(envelope.observation_id)
        if previous is not None:
            if previous != fingerprint:
                raise RemoteProtocolError("observation_id was reused with different content")
            return True, fingerprint
        latest = self._lease_observations.get(lease_id)
        if latest is not None and envelope.page_epoch <= latest.page_epoch:
            raise RemoteProtocolError(
                f"observation epoch {envelope.page_epoch} is not newer than {latest.page_epoch}",
                code="stale_observation",
            )
        if len(self._observation_digests) >= self.limits.max_observations:
            self._capacity_exhausted("observation")
        return False, fingerprint

    def _record_observation(self, envelope: ObservationEnvelope, lease_id: str) -> bool:
        duplicate, fingerprint = self._validate_observation(envelope, lease_id)
        if duplicate:
            return True
        self._observation_digests[envelope.observation_id] = fingerprint
        self._lease_observations[lease_id] = envelope
        if lease_id == self._active_lease_id:
            self._latest_observation = envelope
        self._condition.notify_all()
        return False

    def accept_observation(self, envelope: ObservationEnvelope, *, lease_id: str) -> dict:
        with self._condition:
            self._ensure_open()
            self._require_lease(lease_id)
            envelope = _copy_wire(envelope)
            duplicate = self._record_observation(envelope, lease_id)
            return {"accepted": True, "duplicate": duplicate}

    def accept_result(self, result: ActionResultEnvelope, *, lease_id: str) -> dict:
        with self._condition:
            self._require_active_lease(lease_id)
            result = _copy_wire(result)
            if result.session_id != self.session_id:
                raise RemoteProtocolError("result belongs to another session", status=400)
            action = self._actions.get(result.action_id)
            if action is None:
                raise RemoteProtocolError("result names an unknown action", code="unknown_action", status=404)
            for field in ("turn_id", "based_on_observation_id", "page_epoch",
                          "command_digest"):
                if getattr(result, field) != getattr(action, field):
                    raise RemoteProtocolError(f"result {field} does not match its action")
            prior_digest = self._result_digests.get(result.action_id)
            if prior_digest is not None:
                if prior_digest != result.content_digest:
                    raise RemoteProtocolError("action_id was replayed with a conflicting result")
                return {"accepted": True, "duplicate": True}
            if result.receipt is not None:
                if result.receipt.action != action.command.action:
                    raise RemoteProtocolError("receipt action does not match its command")
                if result.receipt.args_echo != _remote_args_echo(action.command.args):
                    raise RemoteProtocolError("receipt arguments do not match its command")
                based_on = self._action_observations.get(action.action_id)
                if (based_on is None
                        or result.receipt.url_before != based_on.observation.url):
                    raise RemoteProtocolError(
                        "receipt url_before does not match its based-on observation")
            _safe_payload(result.to_json())
            self._ensure_open()
            claimed_lease = self._action_claims.get(result.action_id)
            if result.status in {"completed", "indeterminate"}:
                if not claimed_lease:
                    raise RemoteProtocolError(
                        "an executed action result requires a prior claim",
                        code="action_not_claimed",
                    )
                # A full-page navigation replaces the JavaScript context.  Its new active
                # lease may finish the action claimed by the old document, but a different
                # browser client can never take over that claim.
                if self._lease_clients.get(claimed_lease) != self._lease_clients.get(lease_id):
                    raise RemoteProtocolError(
                        "the active browser client does not own this action claim",
                        code="claim_owner_mismatch",
                        status=403,
                    )
            if result.observation_after is not None:
                self._record_observation(result.observation_after, lease_id)
            self._results[result.action_id] = result
            self._result_digests[result.action_id] = result.content_digest
            self._action_observations.pop(result.action_id, None)
            if self._pending_action_id == result.action_id:
                self._pending_action_id = ""
            self._condition.notify_all()
            return {"accepted": True, "duplicate": False}

    def claim_action(self, claim: dict, *, lease_id: str) -> dict:
        """Atomically grant one live document the right to execute one action once."""
        with self._condition:
            self._ensure_open()
            self._require_lease(lease_id)
            if lease_id != self._active_lease_id:
                return {"claimed": False, "reason": "another_page"}
            if not isinstance(claim, dict) or set(claim) != {"action_id"}:
                raise RemoteProtocolError(
                    "claim requires exactly action_id", code="invalid_claim", status=400)
            action_id = claim.get("action_id")
            if not isinstance(action_id, str) or not action_id:
                raise RemoteProtocolError(
                    "claim action_id must be a non-empty string",
                    code="invalid_claim",
                    status=400,
                )
            action = self._actions.get(action_id)
            if action is None:
                raise RemoteProtocolError(
                    "claim names an unknown action", code="unknown_action", status=404)
            if action_id in self._result_digests:
                raise RemoteProtocolError(
                    "action already has a result", code="action_completed")
            if action.turn_id in self._cancelled_message_ids:
                raise RemoteProtocolError(
                    "the task was cancelled before action claim",
                    code="turn_cancelled",
                )
            if self._pending_action_id != action_id:
                raise RemoteProtocolError(
                    "action is no longer pending", code="action_not_pending")
            previous = self._action_claims.get(action_id)
            if previous:
                if previous == lease_id:
                    return {"claimed": True, "duplicate": True}
                return {"claimed": False, "reason": "already_claimed"}
            if action.deadline <= self._now_ms():
                raise RemoteProtocolError(
                    "action deadline expired before claim",
                    code="deadline_expired",
                )
            self._action_claims[action_id] = lease_id
            return {"claimed": True, "duplicate": False}

    @staticmethod
    def _validate_message_id(message_id: object) -> str:
        if (not isinstance(message_id, str) or not message_id
                or len(message_id) > _MAX_MESSAGE_ID_LENGTH
                or any(not (char.isalnum() or char in "-_") for char in message_id)):
            raise RemoteProtocolError(
                "message_id must contain 1-128 letters, digits, '-' or '_'",
                status=400,
            )
        return message_id

    def _prune_pre_cancelled(self, now_ms: int) -> None:
        expired = [mid for mid, expires_at in self._pre_cancelled_messages.items()
                   if expires_at <= now_ms]
        for mid in expired:
            del self._pre_cancelled_messages[mid]

    def _close_on_durable_failure(self) -> None:
        self._closed = True
        self._heartbeat_stop.set()
        self._condition.notify_all()

    def _record_message_durable(self, message_id: str, fingerprint: str) -> bool:
        if self._durable_store is None:
            return True
        try:
            return self._durable_store.message(self._owner, message_id, fingerprint)
        except (DurableStateError, sqlite3.Error, OSError) as exc:
            if _is_transient_sqlite_error(exc):
                _LOGGER.warning(
                    "transient durable store error accepting message %s (%s)",
                    message_id, type(exc).__name__)
                raise RemoteProtocolError(
                    "durable session store is temporarily unavailable",
                    code="gateway_error",
                    status=503,
                ) from exc
            self._close_on_durable_failure()
            raise RemoteSessionClosed("durable message commit failed") from exc

    def _complete_message_durable(self, message_id: str, *,
                                  raise_on_irrecoverable: bool) -> None:
        if not message_id or self._durable_store is None:
            return
        try:
            self._durable_store.transition(self._owner, message_id, "completed")
        except (DurableStateError, sqlite3.Error, OSError) as exc:
            if _is_transient_sqlite_error(exc):
                _LOGGER.warning(
                    "transient durable store error completing message %s (%s)",
                    message_id, type(exc).__name__)
                return
            self._close_on_durable_failure()
            if raise_on_irrecoverable:
                raise RemoteSessionClosed("durable message transition failed") from exc

    def accept_message(self, message: dict, *, lease_id: str) -> dict:
        with self._condition:
            self._ensure_open()
            self._require_lease(lease_id)
            allowed = {"message_id", "text", "page_url"}
            if not isinstance(message, dict) or set(message) != allowed:
                raise RemoteProtocolError("message requires exactly message_id, text, and page_url", status=400)
            message_id, text = message["message_id"], message["text"]
            self._validate_message_id(message_id)
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise RemoteProtocolError("message text must contain 1-2000 characters", status=400)
            if (not isinstance(message["page_url"], str)
                    or len(message["page_url"]) > _MAX_PAGE_URL_LENGTH):
                raise RemoteProtocolError("message page_url exceeds 4096 characters", status=400)
            if _origin(message["page_url"]) not in self.allowed_origins:
                raise RemoteProtocolError("message page URL is outside this session", status=403)
            _safe_payload(message)
            fingerprint = hashlib.sha256(repr(sorted(message.items())).encode()).hexdigest()
            prior = self._message_digests.get(message_id)
            if prior is not None:
                if prior != fingerprint:
                    raise RemoteProtocolError("message_id was reused with different content")
                return {"accepted": True, "duplicate": True}
            if len(self._message_digests) >= self.limits.max_messages:
                self._capacity_exhausted("message")
            now_ms = self._now_ms()
            self._prune_pre_cancelled(now_ms)
            pre_cancelled = message_id in self._pre_cancelled_messages
            if not pre_cancelled and len(self._messages) >= self.limits.max_queued_messages:
                raise RemoteProtocolError(
                    "session message queue is full",
                    code="backpressure",
                    status=429,
                )
            if not self._record_message_durable(message_id, fingerprint):
                return {"accepted": True, "duplicate": True}
            if pre_cancelled:
                self._pre_cancelled_messages.pop(message_id, None)
                self._message_digests[message_id] = fingerprint
                self._cancelled_message_ids.add(message_id)
                self._completed_message_ids.add(message_id)
                self._complete_message_durable(
                    message_id, raise_on_irrecoverable=True)
                self._append_event("progress", {
                    "state": "cancelled",
                    "message": progress_message("cancelled"),
                    "message_id": message_id,
                }, turn_id=message_id)
                self._condition.notify_all()
                return {"accepted": True, "duplicate": False, "cancelled": True}
            self._message_digests[message_id] = fingerprint
            self._message_leases[message_id] = lease_id
            if self.turn_events is not None:
                self.turn_events.accepted(message_id)
            self._messages.append(_copy_json(message))
            # The panel replays its timeline from this event stream.  Without the goal here
            # a full page navigation would leave the user looking at progress lines that
            # belong to a task they can no longer see the text of.
            self._append_event("progress", {
                "state": "message_accepted", "message_id": message_id, "text": text,
            }, turn_id=message_id)
            # A turn waiting on a confirmation another page has not given would hold this
            # message behind it for as long as the confirmation lasts. Asking again from
            # another page is the answer: that turn stops, as a stop from its own page would
            # stop it, and this one runs.
            waiting = self._active_message_id
            if (waiting and self._pending_approval_id and lease_id != self._active_lease_id
                    and waiting not in self._cancelled_message_ids):
                self.accept_cancellation(CancellationRequest(
                    schema_version=SCHEMA_VERSIONS["CancellationRequest"],
                    session_id=self.session_id, message_id=waiting), lease_id=lease_id)
            self._condition.notify_all()
            return {"accepted": True, "duplicate": False}

    def accept_cancellation(self, request: CancellationRequest, *, lease_id: str) -> dict:
        with self._condition:
            self._ensure_open()
            self._require_lease(lease_id)
            request = _copy_wire(request)
            if request.session_id != self.session_id:
                raise RemoteProtocolError(
                    "cancellation belongs to another session", status=400)
            self._validate_message_id(request.message_id)
            if request.message_id not in self._message_digests:
                now_ms = self._now_ms()
                self._prune_pre_cancelled(now_ms)
                if request.message_id in self._pre_cancelled_messages:
                    self._pre_cancelled_messages[request.message_id] = (
                        now_ms + self._pre_cancel_ttl_ms)
                    return {"accepted": True, "duplicate": True, "pre_cancelled": True}
                while len(self._pre_cancelled_messages) >= self._pre_cancel_max_entries:
                    self._pre_cancelled_messages.pop(
                        next(iter(self._pre_cancelled_messages)))
                self._pre_cancelled_messages[request.message_id] = (
                    now_ms + self._pre_cancel_ttl_ms)
                self._condition.notify_all()
                return {"accepted": True, "duplicate": False, "pre_cancelled": True}
            if request.message_id in self._completed_message_ids:
                return {"accepted": False, "duplicate": False, "completed": True}
            duplicate = request.message_id in self._cancelled_message_ids
            if not duplicate:
                self._cancelled_message_ids.add(request.message_id)
                pending = self._actions.get(self._pending_action_id)
                if (pending is not None
                        and pending.turn_id == request.message_id
                        and pending.action_id not in self._action_claims):
                    self._cancelled_action_ids.add(pending.action_id)
                    self._pending_action_id = ""
                if self._active_message_id == request.message_id \
                        and self._pending_approval_id:
                    approval_id = self._pending_approval_id
                    self._approval_decisions[approval_id] = ApprovalDecision(
                        schema_version=SCHEMA_VERSIONS["ApprovalDecision"],
                        session_id=self.session_id,
                        approval_id=approval_id,
                        decision="denied",
                        principal_digest=self._approvals[approval_id].principal_digest,
                        turn_id=self._approvals[approval_id].turn_id,
                        operation_id=self._approvals[approval_id].operation_id,
                    )
                    self._pending_approval_id = ""
                queued = next((held for held in self._messages
                               if held["message_id"] == request.message_id), None)
                if queued is not None:
                    # Nothing has started, so nothing has to wind down: the message leaves
                    # the queue here and the turn ends now. Publishing only `cancelling`
                    # for a message no turn had picked up left the panel stopping forever,
                    # because the terminal event it was waiting for could only come from a
                    # turn that was never going to run.
                    self._messages.remove(queued)
                    self._completed_message_ids.add(request.message_id)
                    self._complete_message_durable(
                        request.message_id, raise_on_irrecoverable=True)
                    self._append_event("progress", {
                        "state": "cancelled",
                        "message": progress_message("cancelled"),
                        "message_id": request.message_id,
                    }, turn_id=request.message_id)
                else:
                    self._append_event("progress", {
                        "state": "cancelling",
                        "message_id": request.message_id,
                    }, turn_id=request.message_id)
                self._condition.notify_all()
            return {"accepted": True, "duplicate": duplicate}

    def request_new_conversation(self, *, lease_id: str) -> dict:
        """Record that the user started a new conversation.

        Only the model's reference history ends here.  The event log stays whole, which is
        why this appends a marker instead of deleting anything: the timeline above the
        divider is still the record of what this session did.
        """
        with self._condition:
            self._ensure_open()
            self._require_lease(lease_id)
            if self._active_message_id:
                raise RemoteProtocolError(
                    "a task is still running in this conversation",
                    code="turn_in_flight", status=409)
            self._new_conversation_pending = True
            self._append_event("progress", {"state": "new_conversation"})
            self._condition.notify_all()
            return {"accepted": True}

    def take_new_conversation(self) -> bool:
        """Consume a pending boundary; the host calls this before composing a turn."""
        with self._condition:
            pending = self._new_conversation_pending
            self._new_conversation_pending = False
            return pending

    def message_cancelled(self, message_id: str) -> bool:
        with self._condition:
            try:
                self._ensure_open()
            except RemoteSessionClosed:
                return True
            except RemoteProtocolError:
                pass
            return message_id in self._cancelled_message_ids

    def complete_message(self, message_id: str) -> None:
        with self._condition:
            self._complete_message_durable(
                message_id, raise_on_irrecoverable=False)
            if message_id:
                self._completed_message_ids.add(message_id)
            if self._active_message_id == message_id:
                self._active_message_id = ""
            # A confirmation belongs to the turn that raised it. Once that turn is over
            # there is nothing left for an answer to reach, so the session stops offering
            # it rather than leaving a card the panel can only be refused for.
            pending = self._approvals.get(self._pending_approval_id)
            if pending is not None and message_id and pending.turn_id == message_id:
                self._pending_approval_id = ""
            self._condition.notify_all()

    def accept_approval(self, decision: ApprovalDecision, *, lease_id: str) -> dict:
        """Accept one idempotent response from the currently authenticated page session.

        The panel renders inside the host application page, so this cannot distinguish a
        decision a person made from one a script in that page made.  That limit is stated
        in SECURITY.md rather than papered over here with evidence that would not survive
        an attacker who shares the panel's realm.
        """
        with self._condition:
            self._ensure_open()
            self._require_active_lease(lease_id)
            decision = _copy_wire(decision)
            if decision.session_id != self.session_id:
                raise RemoteProtocolError("approval belongs to another session", status=400)
            request = self._approvals.get(decision.approval_id)
            if request is None:
                raise RemoteProtocolError(
                    "approval response names an unknown request",
                    code="unknown_approval",
                    status=404,
                )
            if (decision.principal_digest != request.principal_digest
                    or decision.turn_id != request.turn_id
                    or decision.operation_id != request.operation_id):
                raise RemoteProtocolError(
                    "approval response does not match its operation context",
                    code="approval_context_mismatch",
                    status=403,
                )
            if decision.decision == "prefill" and not request.offer_prefill:
                raise RemoteProtocolError(
                    "form handoff was not offered for this approval",
                    code="approval_choice_unavailable",
                    status=403,
                )
            previous = self._approval_decisions.get(decision.approval_id)
            if previous is not None:
                if previous.content_digest != decision.content_digest:
                    raise RemoteProtocolError(
                        "approval response conflicts with the recorded decision",
                        code="approval_conflict",
                    )
                return {"accepted": True, "duplicate": True}
            if request.expires_at <= self._now_ms():
                raise RemoteProtocolError(
                    "approval request expired before the decision arrived",
                    code="approval_expired",
                )
            if self._pending_approval_id != decision.approval_id:
                raise RemoteProtocolError(
                    "approval request is no longer pending",
                    code="approval_not_pending",
                )
            self._approval_decisions[decision.approval_id] = decision
            self._pending_approval_id = ""
            self._append_event("progress", {
                "state": "approval_recorded",
                "approval_id": decision.approval_id,
                "decision": decision.decision,
            }, turn_id=request.turn_id)
            self._condition.notify_all()
            return {"accepted": True, "duplicate": False}

    def _find_reconciliation_target(self, turn_id: str, proposal_digest: str) -> tuple[str, str]:
        resolved_turn = turn_id
        resolved_proposal = proposal_digest
        if resolved_turn and not resolved_proposal:
            resolved_proposal = self._turn_proposal_digests.get(resolved_turn, "")
            if not resolved_proposal:
                for event in reversed(self._events):
                    if (event.kind == "progress" and event.turn_id == resolved_turn
                            and event.payload.get("state") == "indeterminate"):
                        candidate = event.payload.get("proposal_digest")
                        if isinstance(candidate, str) and candidate:
                            resolved_proposal = candidate
                            break
            if not resolved_proposal and self.recovered_history:
                for record in reversed(self.recovered_history):
                    payload = record.get("payload") or {}
                    if (record.get("kind") == "progress"
                            and record.get("turn_id") == resolved_turn
                            and payload.get("state") == "indeterminate"):
                        candidate = payload.get("proposal_digest")
                        if isinstance(candidate, str) and candidate:
                            resolved_proposal = candidate
                            break
        elif resolved_proposal and not resolved_turn:
            for tid, digest in self._turn_proposal_digests.items():
                if digest == resolved_proposal:
                    resolved_turn = tid
                    break
            if not resolved_turn:
                for event in reversed(self._events):
                    if (event.kind == "progress"
                            and event.payload.get("state") == "indeterminate"
                            and event.payload.get("proposal_digest") == resolved_proposal):
                        resolved_turn = event.turn_id
                        break
        elif resolved_turn and resolved_proposal:
            known = self._turn_proposal_digests.get(resolved_turn)
            if known and known != resolved_proposal:
                raise RemoteProtocolError(
                    "reconciliation turn_id does not match proposal_digest",
                    code="invalid_message", status=400)
        return resolved_turn, resolved_proposal

    @staticmethod
    def _normalize_reconciliation(raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise RemoteProtocolError("reconciliation handler returned invalid response", status=500)
        outcome = raw.get("outcome")
        settled = raw.get("settled")
        if outcome not in {"completed", "failed", "unknown"} or type(settled) is not bool:
            raise RemoteProtocolError("reconciliation outcome is invalid", status=500)
        if outcome == "unknown" and settled:
            settled = False
        labels = panel_contract()["label_keys"]
        default_message = {
            "completed": labels["reconcileCompleted"] if settled else labels["reconcileUnknown"],
            "failed": labels["reconcileNotApplied"] if settled else labels["reconcileUnknown"],
            "unknown": labels["reconcileUnknown"],
        }[outcome]
        raw_message = raw.get("message") or raw.get("reason") or default_message
        text = redact_sensitive_assignments(redact_session_handoffs(str(raw_message)))
        text = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32).strip()
        text = text[:panel_limits()["notice_max_chars"]]
        result: dict = {"outcome": outcome, "settled": settled}
        if text:
            result["message"] = text
        return result

    def accept_reconciliation(self, request: ReconciliationRequest, *, lease_id: str) -> dict:
        """Run read-only reconciliation for an indeterminate write and update timeline state."""
        with self._condition:
            self._ensure_open()
            self._require_lease(lease_id)
            request = _copy_wire(request)
            if request.session_id != self.session_id:
                raise RemoteProtocolError(
                    "reconciliation belongs to another session", status=400)
            turn_id, proposal_digest = self._find_reconciliation_target(
                request.turn_id, request.proposal_digest)
            cached = (
                (self._reconciliations.get(f"p:{proposal_digest}") if proposal_digest else None)
                or (self._reconciliations.get(f"t:{turn_id}") if turn_id else None)
            )
            if cached is not None and cached.get("settled") is True:
                return _copy_json(cached)
            reconciler = self._reconciler

        if reconciler is None:
            labels = panel_contract()["label_keys"]
            raw = {"outcome": "unknown", "settled": False, "message": labels["reconcileUnknown"]}
        else:
            resolved_req = ReconciliationRequest(
                schema_version=request.schema_version,
                session_id=request.session_id,
                turn_id=turn_id,
                proposal_digest=proposal_digest,
            )
            try:
                raw = reconciler(resolved_req)
            except RemoteProtocolError:
                raise
            except MutationJournalError as exc:
                raise RemoteProtocolError(
                    str(exc), code="unknown_reconciliation", status=404) from exc
            except PrincipalError as exc:
                raise RemoteProtocolError(
                    "session principal is no longer valid",
                    code="principal_expired", status=401) from exc

        projection = self._normalize_reconciliation(raw)
        with self._condition:
            if proposal_digest:
                self._reconciliations[f"p:{proposal_digest}"] = _copy_json(projection)
            if turn_id:
                self._reconciliations[f"t:{turn_id}"] = _copy_json(projection)
            if turn_id and proposal_digest:
                self._turn_proposal_digests[turn_id] = proposal_digest
            for idx, event in enumerate(self._events):
                if (event.kind == "progress"
                        and event.payload.get("state") == "indeterminate"
                        and ((turn_id and event.turn_id == turn_id)
                             or (proposal_digest
                                 and event.payload.get("proposal_digest") == proposal_digest))):
                    updated_payload = {
                        **event.payload,
                        "reconciliation": _copy_json(projection),
                        **({"proposal_digest": proposal_digest}
                           if proposal_digest and not event.payload.get("proposal_digest")
                           else {}),
                    }
                    self._events[idx] = replace(event, payload=updated_payload)
            for entry in self._panel_entries:
                if (entry.get("panel_state") == "indeterminate"
                        and ((turn_id and entry.get("turn_id") == turn_id)
                             or (proposal_digest
                                 and entry.get("proposal_digest") == proposal_digest))):
                    entry["reconciliation"] = _copy_json(projection)
                    if proposal_digest and not entry.get("proposal_digest"):
                        entry["proposal_digest"] = proposal_digest
            self._condition.notify_all()
        return _copy_json(projection)
