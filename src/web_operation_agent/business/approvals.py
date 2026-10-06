"""User-visible approvals bound to one principal, turn, and exact proposed effect."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit

from .runtime import BusinessMutationProposal, FormDraft
from ..state.identity import OperationContext, PrincipalContext
from ..state.ledger import SessionLedger
from ..schemas import PageObservation
from ..schemas.browser import POTENTIALLY_WRITING_ACTIONS


#: What a person authorises when they let the assistant work the page for a turn: the
#: actions that change what the page shows or holds. Reading, scrolling, waiting and pointing
#: at something need no one's leave, so a turn that only reads and answers never asks.
PAGE_OPERATIONS = POTENTIALLY_WRITING_ACTIONS


@dataclass(frozen=True)
class OperationPlan:
    """The page operation that first needed the person's leave in a turn.

    The leave covers the turn's page operations, not only this one; the plan is what the
    card's answer is bound to and what the audit records as the reason it was asked.
    """

    action: str
    url: str = ""

    @property
    def content_digest(self) -> str:
        return hashlib.sha256(json.dumps(
            {"kind": "operate", "action": self.action, "url": self.url},
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def redacted(self) -> dict:
        return {"kind": "operate", "action": self.action,
                "url_digest": hashlib.sha256(self.url.encode("utf-8")).hexdigest()}


@dataclass(frozen=True)
class MutationApproval:
    """A bound grant, or the terminal result that prevented one."""

    approval_id: str = ""
    approved_digest: str = ""
    reason: str = ""
    detail: str = ""

    @property
    def approved(self) -> bool:
        return not self.reason


class ApprovalWatch:
    """Bind approval requests and decisions to the current authenticated turn."""

    def __init__(self, *, ledger: SessionLedger, run_id: str,
                 principal: PrincipalContext | None, session_id: str, turn_id: str,
                 business_approval=None, operation_confirmation=None,
                 cancelled: Callable[[], bool],
                 approval_hold_s: int = 0, operation_hold_s: int = 0):
        self.ledger = ledger
        self.run_id = run_id
        self.principal = principal
        self.session_id = session_id
        self.turn_id = turn_id
        self.business_approval = business_approval
        self.operation_confirmation = operation_confirmation
        self.cancelled = cancelled
        self.deadline_ms = 0
        #: The longest a person may take over each kind of answer, and how long they have
        #: taken this turn. Time spent waiting on a person is theirs, not the assistant's:
        #: it extends the turn's deadline rather than spending it. Without that, a turn
        #: budget of two minutes held a confirmation to whatever the model had left of it.
        self.approval_hold_ms = approval_hold_s * 1000
        self.operation_hold_ms = operation_hold_s * 1000
        self.waited_ms = 0
        self.business_counts = {
            "approval_requested": 0,
            "approval_granted": 0,
            "approval_denied": 0,
            "mutation_execution_started": 0,
            "mutation_completed": 0,
            "mutation_indeterminate": 0,
        }
        self.operation_counts = {"requested": 0, "confirmed": 0, "not_confirmed": 0}
        #: Whether the person has let the assistant work the page this turn. A watch lives
        #: for one turn, so the leave ends with it: the next message asks again.
        self.operation_granted = False
        #: The operation the card was answered for, until the next dispatch settles it.
        self.answered_for: tuple[str, str] | None = None

    @property
    def mutations_enabled(self) -> bool:
        return self.business_approval is not None

    def start_turn(self, deadline_ms: int) -> None:
        self.deadline_ms = deadline_ms

    def context(self, step: int, tool: str, *, hold_ms: int = 0) -> OperationContext | None:
        """Bind a server tool call to the immutable principal and current turn.

        `hold_ms` is for an operation that will wait on a person first: its context outlives
        the turn's present deadline by the longest that wait may take, since the wait moves
        that deadline too. The principal's own expiry still bounds it.
        """
        if self.principal is None:
            return None
        if not self.deadline_ms:
            raise RuntimeError("operation context cannot be created before the turn starts")
        operation_seed = f"{self.run_id}\0{step}\0{tool}".encode("utf-8")
        return OperationContext.create(
            self.principal,
            session_id=self.session_id,
            turn_id=self.turn_id,
            operation_id="op-" + hashlib.sha256(operation_seed).hexdigest()[:32],
            deadline_ms=self.deadline_ms + hold_ms,
        )

    @contextmanager
    def awaiting_person(self):
        """Give the time a person takes to answer back to the turn."""
        started = time.monotonic()
        try:
            yield
        finally:
            waited = int((time.monotonic() - started) * 1000)
            self.waited_ms += waited
            if self.deadline_ms:
                self.deadline_ms += waited

    def confirm_form_handoff(self, draft: FormDraft, step: int) -> tuple[str, str]:
        """Ask before typing into a form the application saves by itself."""
        if self.business_approval is None:
            self.ledger.append(
                "form_handoff_approval_unavailable", step=step, draft=draft.redacted(),
                dispatched=False, side_effect_possible=False)
            return ("approval_unavailable",
                    "preparing this form requires an authenticated user decision")
        self.ledger.append("form_handoff_proposed", step=step, draft=draft.redacted(),
                           effect=draft.effect, targets=list(draft.targets),
                           dispatched=False, side_effect_possible=False)
        if self.cancelled():
            return "user_cancelled", "the user stopped this task before the confirmation"
        context = self.context(step, "form_handoff", hold_ms=self.approval_hold_ms)
        try:
            with self.awaiting_person():
                grant = (self.business_approval(draft)
                         if context is None
                         else self.business_approval(draft, context=context))
        except Exception as exc:
            self.ledger.append(
                "form_handoff_approval_failed", step=step, draft=draft.redacted(),
                error_type=type(exc).__name__, dispatched=False,
                side_effect_possible=False)
            return "approval_failed", "the confirmation for this form was unavailable"
        if self.cancelled():
            return ("user_cancelled",
                    "the user stopped this task while the confirmation was pending")
        decision = getattr(grant, "decision", "")
        if decision == "expired":
            self.ledger.append("form_handoff_approval_expired", step=step,
                               draft=draft.redacted(), dispatched=False,
                               side_effect_possible=False)
            return "approval_expired", "the confirmation expired before anything was typed"
        if (decision != "approved"
                or getattr(grant, "plan_digest", "") != draft.content_digest):
            self.ledger.append("form_handoff_denied", step=step, draft=draft.redacted(),
                               decision=str(decision)[:40], dispatched=False,
                               side_effect_possible=False)
            return "approval_denied", "the user declined preparing this form"
        self.ledger.append("form_handoff_approved", step=step, draft=draft.redacted(),
                           approval_id=str(getattr(grant, "approval_id", ""))[:200],
                           dispatched=False, side_effect_possible=False)
        return "", ""

    def approve_mutation(self, proposal: BusinessMutationProposal, *, step: int,
                         tool: str, context: OperationContext | None,
                         identity: dict) -> MutationApproval:
        """Ask once and verify that the answer belongs to this proposal and operation."""
        self.business_counts["approval_requested"] += 1
        self.ledger.append(
            "business_mutation_proposed", step=step, tool=tool,
            proposal=proposal.redacted(), effect=proposal.effect,
            targets=list(proposal.targets), dispatched=False,
            side_effect_possible=False, identity=identity)
        if self.business_approval is None:
            self.ledger.append(
                "business_mutation_approval_unavailable", step=step,
                proposal_digest=proposal.content_digest,
                dispatched=False, side_effect_possible=False)
            return MutationApproval(
                reason="approval_unavailable",
                detail="business mutation requires an authenticated user decision")
        if self.cancelled():
            return MutationApproval(
                reason="user_cancelled",
                detail="the user stopped this task before approval")
        try:
            with self.awaiting_person():
                grant = (self.business_approval(proposal)
                         if context is None
                         else self.business_approval(proposal, context=context))
        except Exception as exc:
            self.ledger.append(
                "business_mutation_approval_failed", step=step,
                proposal_digest=proposal.content_digest,
                error_type=type(exc).__name__, dispatched=False,
                side_effect_possible=False)
            return MutationApproval(
                reason="approval_failed",
                detail="business mutation approval was unavailable")
        if self.cancelled():
            self._mutation_cancelled(proposal, step=step, tool=tool, approval_id="")
            return MutationApproval(
                reason="user_cancelled",
                detail="the user stopped this task while approval was pending")
        decision = getattr(grant, "decision", "")
        approved_digest = getattr(grant, "plan_digest", "")
        approved_context_digest = getattr(grant, "operation_context_digest", "")
        approval_id = str(getattr(grant, "approval_id", ""))[:200]
        if decision == "expired":
            self.ledger.append(
                "business_mutation_approval_expired", step=step, tool=tool,
                approval_id=approval_id,
                proposal_digest=proposal.content_digest,
                dispatched=False, side_effect_possible=False)
            return MutationApproval(
                reason="approval_expired",
                detail="business mutation approval expired before dispatch")
        if decision != "approved":
            self.business_counts["approval_denied"] += 1
            self.ledger.append(
                "business_mutation_denied", step=step, tool=tool,
                approval_id=approval_id,
                proposal_digest=proposal.content_digest,
                dispatched=False, side_effect_possible=False)
            return MutationApproval(
                reason="approval_denied",
                detail="the user declined the proposed business mutation")
        if approved_digest != proposal.content_digest:
            self.ledger.append(
                "business_mutation_approval_mismatch", step=step, tool=tool,
                approval_id=approval_id,
                proposal_digest=proposal.content_digest,
                dispatched=False, side_effect_possible=False)
            return MutationApproval(
                reason="approval_failed",
                detail="business mutation approval did not match the proposal")
        if (context is not None
                and approved_context_digest != context.binding_digest):
            self.ledger.append(
                "business_mutation_approval_context_mismatch", step=step, tool=tool,
                approval_id=approval_id,
                proposal_digest=proposal.content_digest,
                operation_context_digest=context.binding_digest,
                dispatched=False, side_effect_possible=False)
            return MutationApproval(
                reason="approval_failed",
                detail="business mutation approval belongs to another operation context")
        self.business_counts["approval_granted"] += 1
        if self.cancelled():
            self._mutation_cancelled(
                proposal, step=step, tool=tool, approval_id=approval_id)
            return MutationApproval(
                reason="user_cancelled",
                detail="the user stopped this task before mutation dispatch")
        return MutationApproval(approval_id=approval_id, approved_digest=approved_digest)

    def mutation_claimed(self, proposal: BusinessMutationProposal, *, step: int,
                         tool: str, approval_id: str, identity: dict) -> None:
        self.ledger.append(
            "business_mutation_execution_started", step=step, tool=tool,
            approval_id=approval_id, proposal_digest=proposal.content_digest,
            side_effect_possible=True, identity=identity)
        self.business_counts["mutation_execution_started"] += 1

    def mutation_indeterminate(self) -> None:
        self.business_counts["mutation_indeterminate"] += 1

    def mutation_completed(self) -> None:
        self.business_counts["mutation_completed"] += 1

    def business_result(self) -> dict:
        return dict(self.business_counts)

    def _mutation_cancelled(self, proposal: BusinessMutationProposal, *, step: int,
                            tool: str, approval_id: str) -> None:
        self.ledger.append(
            "business_mutation_cancelled_before_dispatch", step=step,
            tool=tool, approval_id=approval_id,
            proposal_digest=proposal.content_digest,
            dispatched=False, side_effect_possible=False)

    def confirm_operation(self, step: int, obs: PageObservation, *, action: str,
                          url: str = "") -> tuple[str, str]:
        """Ask once per turn before working the page; later operations go ahead on that leave.

        Opening the page already shown operates nothing and asks nothing.
        """
        if self.operation_confirmation is None or action not in PAGE_OPERATIONS:
            return "", ""
        if self.operation_granted:
            return "", ""
        if action == "goto" and url and _same_page(url, obs.url):
            return "", ""
        plan = OperationPlan(action=action, url=url)
        self.operation_counts["requested"] += 1
        self.ledger.append("operation_confirmation_needed", step=step, tool=action,
                           plan=plan.redacted(), dispatched=False,
                           side_effect_possible=False)
        if self.cancelled():
            return "user_cancelled", "the user stopped this task before working the page"
        try:
            context = self.context(step, "operate", hold_ms=self.operation_hold_ms)
            with self.awaiting_person():
                grant = self.operation_confirmation(plan, context=context)
        except Exception as exc:
            self.operation_counts["not_confirmed"] += 1
            self.ledger.append("operation_confirmation_failed", step=step, tool=action,
                               error_type=type(exc).__name__, dispatched=False,
                               side_effect_possible=False)
            return "operation_not_confirmed", "working the page could not be confirmed"
        if self.cancelled():
            return ("user_cancelled",
                    "the user stopped this task while working the page awaited confirmation")
        decision = getattr(grant, "decision", "")
        if decision != "approved" or getattr(grant, "plan_digest", "") != plan.content_digest:
            self.operation_counts["not_confirmed"] += 1
            self.ledger.append("operation_not_confirmed", step=step, tool=action,
                               decision=str(decision)[:40], dispatched=False,
                               side_effect_possible=False)
            # A card nobody answered ends the turn the way any unanswered card does.
            if decision == "expired":
                return "approval_expired", "the card expired before working the page"
            return "operation_not_confirmed", "working the page was not confirmed"
        self.operation_counts["confirmed"] += 1
        self.operation_granted = True
        self.answered_for = (action, url)
        self.ledger.append("operation_confirmed", step=step, tool=action,
                           approval_id=str(getattr(grant, "approval_id", ""))[:200])
        return "", ""

    def answered_just_now(self, *, action: str, url: str) -> bool:
        """Whether the card was answered for exactly this operation, with nothing since."""
        return bool(url) and self.answered_for == (action, url)

    def settle_operation(self) -> None:
        """The operation the card was answered for has been dispatched, or something was."""
        self.answered_for = None

    def invalidate_operation(self) -> None:
        """A changed task interpretation needs new leave before further page work."""
        self.operation_granted = False
        self.answered_for = None

    def operation_result(self) -> dict | None:
        return (dict(self.operation_counts)
                if self.operation_confirmation is not None else None)


def _same_page(url: str, current: str) -> bool:
    """Whether ``url`` names the page already shown: same origin, path and query."""
    try:
        here = urlsplit(current)
        there = urlsplit(urljoin(current, url))
    except (TypeError, ValueError):
        return False
    return ((there.scheme, there.netloc, there.path.rstrip("/") or "/", there.query)
            == (here.scheme, here.netloc, here.path.rstrip("/") or "/", here.query))
