"""Server-owned read policy and user approval primitives.

The browser receives a redacted approval request. The exact mutation plan and its digest
remain on the server and in the audit ledger.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .controlplane import ControlPlaneClient
from .state.identity import OperationContext, PrincipalError
from .state.ledger import SessionLedger
from .transport.channel import RemoteSessionChannel
from .transport.protocol import ensure_safe_browser_payload
from .schemas import ApprovalDecision, ApprovalRequest, ControlPlan


class ReadOnlyControl:
    """An empty control trace for a run whose pre-dispatch policy forbids mutations.

    This adapter supports read-only navigation only. It is not a replacement for the hidden
    ``ControlPlan`` and enforcement gateway required by write-capable product tasks.
    """

    def begin_run(self, _run_id: str) -> None:
        return None

    def end_run(self, _run_id: str) -> None:
        return None

    def since(self, seen: int) -> tuple[list[dict], int]:
        if seen != 0:
            raise ValueError("read-only control trace cannot advance or rewind")
        return [], 0

    @staticmethod
    def outcome_of(events: list[dict]) -> dict:
        return ControlPlaneClient.outcome_of(events)

    def outcome(self) -> dict:
        return {
            "permit_consumed": 0,
            "write_blocked": 0,
            "consumed_requests": [],
            "blocked_requests": [],
            "trace_events": 0,
            "mode": "read_only_predispatch",
        }


class ApprovalDenied(RuntimeError):
    """The authenticated user declined the proposed effect."""

    def __init__(self, grant: "ApprovalGrant"):
        super().__init__(f"approval {grant.approval_id} was denied")
        self.grant = grant


@dataclass(frozen=True)
class ApprovalGrant:
    approval_id: str
    decision: str
    plan_digest: str
    decided_at: float
    operation_context_digest: str = ""


class ApprovalGate:
    """Bind one user-visible decision to one exact hidden plan or mutation proposal.

    The request sent to the browser contains only an effect and business targets.  The
    corresponding plan digest stays in the server ledger so the operator cannot swap in a
    different hidden plan after the user approves.
    """

    def __init__(self, channel: RemoteSessionChannel, ledger: SessionLedger):
        self.channel = channel
        self.ledger = ledger

    #: Audit event names per confirmation kind. A page switch is recorded apart from a write
    #: approval: every reader that counts approvals means writes, and a navigation counted
    #: among them would read as a write the person approved.
    _EVENTS = {"write": ("approval_requested", "approval_decided"),
               "navigation": ("navigation_confirmation_requested",
                              "navigation_confirmation_decided")}

    def decide_plan(self, plan, *, effect: str, targets: tuple[str, ...],
                    ttl_s: int = 120,
                    context: OperationContext | None = None,
                    kind: str = "write") -> ApprovalGrant:
        """Return the authenticated decision without treating denial as an exception."""
        if kind not in self._EVENTS:
            raise ValueError("approval kind is invalid")
        requested_event, decided_event = self._EVENTS[kind]
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or ttl_s < 1 or ttl_s > 600:
            raise ValueError("approval ttl_s must be an integer from 1 to 600")
        if (not isinstance(getattr(plan, "content_digest", None), str)
                or not callable(getattr(plan, "redacted", None))):
            raise ValueError("approval plan must expose content_digest and redacted()")
        principal = getattr(self.channel, "principal", None)
        if principal is None and context is not None:
            raise ValueError("an unbound session cannot approve a principal-bound operation")
        if principal is not None:
            if not isinstance(context, OperationContext):
                raise ValueError("a principal-bound session requires an operation context")
            try:
                context.require_live(application_id=principal.application_id)
            except PrincipalError as exc:
                raise ValueError(str(exc)) from exc
            if (context.principal.binding_digest != principal.binding_digest
                    or context.session_id != self.channel.session_id):
                raise ValueError("approval operation context does not match the session")
        approval_id = f"approval-{uuid.uuid4().hex}"
        expires_at = int(time.time() * 1000) + ttl_s * 1000
        if context is not None:
            expires_at = min(expires_at, context.expires_at)
        request = ApprovalRequest(
            schema_version=1,
            approval_id=approval_id,
            effect=effect,
            targets=targets,
            expires_at=expires_at,
            principal_digest=(context.principal.binding_digest if context else ""),
            turn_id=(context.turn_id if context else ""),
            operation_id=(context.operation_id if context else ""),
            kind=kind,
        )
        ensure_safe_browser_payload(request.to_json())
        self.ledger.append(
            requested_event,
            approval_id=approval_id,
            effect=effect,
            targets=list(targets),
            expires_at=request.expires_at,
            control_plan=plan.redacted(),
            identity=(context.audit_projection() if context else {}),
        )
        self.channel.request_approval(request)
        decision: ApprovalDecision = self.channel.wait_for_approval(
            approval_id, timeout_s=ttl_s)
        decided_at = time.time()
        self.ledger.append(
            decided_event,
            approval_id=approval_id,
            decision=decision.decision,
            control_plan_digest=plan.content_digest,
            operation_context_digest=(context.binding_digest if context else ""),
        )
        grant = ApprovalGrant(
            approval_id=approval_id,
            decision=decision.decision,
            plan_digest=plan.content_digest,
            decided_at=decided_at,
            operation_context_digest=(context.binding_digest if context else ""),
        )
        return grant

    def approve_plan(self, plan: ControlPlan, *, effect: str, targets: tuple[str, ...],
                     ttl_s: int = 120,
                     context: OperationContext | None = None) -> ApprovalGrant:
        grant = self.decide_plan(
            plan, effect=effect, targets=targets, ttl_s=ttl_s, context=context)
        if grant.decision != "approved":
            raise ApprovalDenied(grant)
        return grant
