"""Shared failure projection for every engine in the Twenty comparison harness."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any


_ENGINE_PROVIDER_CODES = frozenset({
    "model_call_budget", "provider_deadline", "cancelled",
})
_ENGINE_BUSINESS_CODES = frozenset({
    "business_request", "business_call_budget", "route_call_budget",
    "route_refused", "cancelled",
})
_IDENTITY_BUSINESS_CODES = frozenset({
    "business_authentication", "business_identity",
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
