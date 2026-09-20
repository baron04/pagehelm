"""User-visible approvals bound to one principal, turn, and exact proposed effect."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit

from .business import FormDraft
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
