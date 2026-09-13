"""Shared failure projection for every engine in the Twenty comparison harness."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any


_ENGINE_PROVIDER_CODES = frozenset({
    "model_call_budget", "provider_deadline", "cancelled",
})
_ENGINE_BUSINESS_CODES = frozenset({
    "business_request", "business_call_budget", "route_call_budget",
    "route_refused", "approval_denied", "approval_expired", "indeterminate_write", "cancelled",
    "business_mutation_blocked",
    # An authenticated actor's own authority refusing an operation is behaviour to score,
    # not a broken credential: the credential worked and named the actor who was refused.
    "business_permission_denied",
})
_IDENTITY_BUSINESS_CODES = frozenset({
    "business_authentication", "business_identity",
})
_WOA_BOUNDARY_IDENTITY_CODES = frozenset({
    "identity_unavailable", "identity_binding", "session_changed",
})
_WOA_TRANSPORT_TERMINALS = frozenset({"control_plane_error", "transport_error"})
_WOA_ENGINE_TERMINALS = frozenset({
    "action_policy_budget", "agent_finished", "agent_refused", "approval_denied",
    "approval_expired",
    "business_mutation_blocked", "indeterminate_write", "needs_input", "no_progress",
    "permission_denied", "step_budget", "token_budget",
    "user_cancelled", "wall_clock_budget",
})


def provider_apparatus_code(code: Any) -> str | None:
    """Return a ledger apparatus code for a provider failure, if it is external."""
    if code in (None, "") or code in _ENGINE_PROVIDER_CODES:
        return None
    if code in {"ledger_failure", "transport_contract"}:
        return str(code)
    return "provider_outage"


def business_apparatus_code(code: Any) -> str | None:
    """Return a ledger apparatus code for a typed business failure, if it is external."""
    if code in (None, "") or code in _ENGINE_BUSINESS_CODES:
        return None
    if code in _IDENTITY_BUSINESS_CODES:
        return "credential_or_identity"
    return "transport_contract"


def classify_host_errors(errors: Sequence[tuple[str, str]]) -> tuple[str | None, str | None]:
    """Project ordered Host refusals to one apparatus code and one terminal reason."""
    for host, code in errors:
        apparatus = (
            provider_apparatus_code(code)
            if host == "provider" else business_apparatus_code(code))
        if apparatus:
            return apparatus, code
    if errors:
        return None, errors[-1][1]
    return None, None


def classify_woa_outcome(*, terminal_reason: str,
                         events: Sequence[dict[str, Any]] = (),
                         boundary_error_code: str | None = None
                         ) -> tuple[str | None, str]:
    """Classify a WOA turn with the same typed provider/business rules as PageAgent."""
    if boundary_error_code in _WOA_BOUNDARY_IDENTITY_CODES:
        return "credential_or_identity", str(boundary_error_code)
    if boundary_error_code:
        return "transport_contract", str(boundary_error_code)

    typed_errors: list[tuple[str, str]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("event")
        if kind == "provider_failed":
            code = event.get("provider_error_code") or "provider_error"
            typed_errors.append(("provider", str(code)))
        elif kind in {
                "business_mutation_blocked", "business_mutation_failed",
                "business_mutation_permission_denied", "business_mutation_rejected",
                "business_tool_rejected"}:
            code = event.get("error_code") or "business_tool"
            typed_errors.append(("business", str(code)))
    apparatus, source_code = classify_host_errors(typed_errors)
    if apparatus:
        return apparatus, str(source_code)
    if terminal_reason == "provider_error":
        return "provider_outage", terminal_reason
    if terminal_reason in _WOA_TRANSPORT_TERMINALS:
        return "transport_contract", terminal_reason
    if terminal_reason in _WOA_ENGINE_TERMINALS:
        return None, terminal_reason
    return "transport_contract", terminal_reason or "execution_failed"
