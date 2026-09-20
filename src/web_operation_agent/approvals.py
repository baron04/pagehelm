"""User-visible approvals bound to one principal, turn, and exact proposed effect."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit

from .business import BusinessMutationProposal, FormDraft
from .identity import OperationContext, PrincipalContext
from .ledger import SessionLedger
from .schemas import ActionReceipt, PageObservation
from .writes import PRE_DISPATCH_ERRORS


@dataclass(frozen=True)
class NavigationPlan:
    """The one page switch a navigation confirmation authorises."""

    action: str
    url: str = ""

    @property
    def content_digest(self) -> str:
        return hashlib.sha256(json.dumps(
            {"kind": "navigation", "action": self.action, "url": self.url},
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def redacted(self) -> dict:
        return {"kind": "navigation", "action": self.action,
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
                 business_approval=None, navigation_confirmation=None,
                 cancelled: Callable[[], bool]):
        self.ledger = ledger
        self.run_id = run_id
        self.principal = principal
        self.session_id = session_id
        self.turn_id = turn_id
        self.business_approval = business_approval
        self.navigation_confirmation = navigation_confirmation
        self.cancelled = cancelled
        self.deadline_ms = 0
        self.business_counts = {
            "approval_requested": 0,
            "approval_granted": 0,
            "approval_denied": 0,
            "mutation_execution_started": 0,
            "mutation_completed": 0,
            "mutation_indeterminate": 0,
        }
        self.navigation_counts = {"requested": 0, "confirmed": 0, "not_confirmed": 0}
        self.confirmed_navigation = ""

    @property
    def mutations_enabled(self) -> bool:
        return self.business_approval is not None

    def start_turn(self, deadline_ms: int) -> None:
        self.deadline_ms = deadline_ms

    def context(self, step: int, tool: str) -> OperationContext | None:
        """Bind a server tool call to the immutable principal and current turn."""
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
            deadline_ms=self.deadline_ms,
        )

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
        context = self.context(step, "form_handoff")
        try:
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

    def confirm_navigation(self, step: int, obs: PageObservation, *, action: str,
                           url: str = "") -> tuple[str, str]:
        """Ask before leaving the current page, binding the answer to one destination."""
        if self.navigation_confirmation is None:
            return "", ""
        if action == "goto" and url and _same_page(url, obs.url):
            return "", ""
        plan = NavigationPlan(action=action, url=url)
        if plan.content_digest == self.confirmed_navigation:
            return "", ""
        self.navigation_counts["requested"] += 1
        self.ledger.append("navigation_confirmation_needed", step=step, tool=action,
                           plan=plan.redacted(), dispatched=False,
                           side_effect_possible=False)
        if self.cancelled():
            return "user_cancelled", "the user stopped this task before the page switch"
        try:
            grant = self.navigation_confirmation(
                plan, context=self.context(step, "navigation"))
        except Exception as exc:
            self.navigation_counts["not_confirmed"] += 1
            self.ledger.append("navigation_confirmation_failed", step=step, tool=action,
                               error_type=type(exc).__name__, dispatched=False,
                               side_effect_possible=False)
            return "navigation_not_confirmed", "the page switch could not be confirmed"
        if self.cancelled():
            return ("user_cancelled",
                    "the user stopped this task while the page switch awaited confirmation")
        decision = getattr(grant, "decision", "")
        if decision != "approved" or getattr(grant, "plan_digest", "") != plan.content_digest:
            self.navigation_counts["not_confirmed"] += 1
            self.ledger.append("navigation_not_confirmed", step=step, tool=action,
                               decision=str(decision)[:40], dispatched=False,
                               side_effect_possible=False)
            return "navigation_not_confirmed", "the page switch was not confirmed"
        self.navigation_counts["confirmed"] += 1
        self.confirmed_navigation = plan.content_digest
        self.ledger.append("navigation_confirmed", step=step, tool=action,
                           approval_id=str(getattr(grant, "approval_id", ""))[:200])
        return "", ""

    def settle_navigation(self, receipt: ActionReceipt) -> None:
        """Consume a confirmation once its dispatch may have reached the page."""
        if receipt.error_code not in PRE_DISPATCH_ERRORS:
            self.confirmed_navigation = ""

    def navigation_result(self) -> dict | None:
        return (dict(self.navigation_counts)
                if self.navigation_confirmation is not None else None)


def _same_page(url: str, current: str) -> bool:
    """Whether ``url`` names the page already shown: same origin, path and query."""
    try:
        here = urlsplit(current)
        there = urlsplit(urljoin(current, url))
    except (TypeError, ValueError):
        return False
    return ((there.scheme, there.netloc, there.path.rstrip("/") or "/", there.query)
            == (here.scheme, here.netloc, here.path.rstrip("/") or "/", here.query))
