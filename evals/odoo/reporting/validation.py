"""Schema and field validation for Odoo evaluation ledger events.

Every attempt is written down before its runner starts, every model request before it is
forwarded, and every result afterwards, so a failed or interrupted trial cannot be rerun
without leaving a trace. The file uses a private batch ledger -- a
``0600`` regular file, a POSIX lock held across read-validate-append, SHA-256 entry digests
linking each line to the previous one. It records attempts rather than completed trials:
an attempt may be interrupted, replaced within a closed policy, or end a batch.

Validation replays the whole chain on every append and on every read. The rules it enforces
are the protocol's, not conveniences: one open attempt at a time, a fixed record order per
attempt, legal combinations of the three outcome axes, bounded replacements that follow their
original immediately, and mandatory seals after safety events, reset failures, unclassified
results, provider model-string changes, requests that belong to no attempt, a refused provider
credential, a call the spend ledger could not record and a request outside the frozen request
contract.

Every entry carries the ledger version it was written under, and a ledger of any other version
is refused whole rather than read by rules it was not written under.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from evals.odoo.domain import outcomes
from evals.odoo.domain.population import digest_json

LEDGER_VERSION = 2
_ID = re.compile(r"[a-z0-9][a-z0-9_.:-]{2,119}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ARMS = frozenset({"pageagent_browser_only", "woa_product"})
_REPLICATES = ("primary", "replicate")
_ATTEMPT_KINDS = ("attempt_outcome", "reset_outcome", "attempt_cost", "attempt_metrics")
_METRIC_FIELDS = ("automatic_wall_ms", "approval_wait_ms", "first_feedback_ms",
                  "cancel_feedback_ms", "page_actions", "invalid_page_actions", "business_calls",
                  "repeated_observations", "user_turns")
#: Metrics an attempt may not have measured: no turn started, no turn was stopped, the driver
#: died before the attempt's times were taken, or the audit those counts come from could not be
#: read. A count nobody could read is null; it is never zero, which would report an attempt as
#: having done no work.
_OPTIONAL_METRICS = frozenset({"first_feedback_ms", "cancel_feedback_ms", "automatic_wall_ms",
                               "approval_wait_ms", "page_actions", "invalid_page_actions",
                               "business_calls", "repeated_observations"})
#: Times every attempt that finished normally measured; only an interrupted attempt lacks them.
_RUN_TIMES = ("automatic_wall_ms", "approval_wait_ms")
_TERMINAL_KINDS = frozenset({"attempt_terminal", "interrupted_attempt"})
_SEAL_REASONS = frozenset({
    "critical_safety_event", "classification_contract_drift", "reset_failed",
    "model_identity_changed", "unowned_model_request", "replacement_cap_exceeded",
    "ledger_corrupted", "state_contamination", "operator_stop",
    "provider_authentication_failed", "accounting_failure", "request_contract_violation",
})
#: The order seal reasons reduce in, defined with the outcome axes so an attempt's own reason and
#: its batch's reason are chosen by one rule rather than two.
_SEAL_PRIORITY = outcomes.SEAL_PRIORITY
#: What a batch may do next, most constrained first. A reader branches on one of these rather
#: than re-deriving it from `status`, `open_attempt`, `pending_seal` and `cost_unknown`.
DISPOSITIONS = ("complete", "sealed", "attempt_open", "must_seal", "holding", "running")
_MAX_LEDGER_BYTES = 64 * 1024 * 1024
_MAX_LINE_BYTES = 2 * 1024 * 1024


class EvaluationInputError(ValueError):
    """An evaluation artifact cannot support a deterministic decision."""


def _canonical_model_string(item: Any) -> bool:
    """Whether ``item`` is already in the canonical provider-model form."""
    return (isinstance(item, str) and item == item.strip() and 1 <= len(item) <= 120
            and not any(ord(character) < 32 or ord(character) == 127 for character in item))


KINDS = frozenset({
    "batch_header", "attempt_started", "model_request_dispatched", "model_request_refused",
    "model_response_received", "attempt_checkpoint", *_ATTEMPT_KINDS, *_TERMINAL_KINDS,
    "unowned_model_request", "model_response_unresolved", "billing_reconciliation_applied",
    "batch_sealed", "batch_completed",
})
RESPONSE_STATUSES = frozenset({"ok", "http_error", "transport_error", "contract_error",
                               "accounting_error"})
#: Why a dispatched call settled without an answer. The provider's own deadline is the only
#: reason a call may be left unresolved; the apparatus has no timeout of its own to add.
UNRESOLVED_REASONS = frozenset({"provider_deadline_elapsed", "driver_process_exit"})
REFUSAL_REASONS = frozenset({"model_call_budget", "output_token_budget", "input_size_budget",
                             "request_contract", "accounting_failure"})
class LedgerError(EvaluationInputError):
    """The ledger or a prospective entry violates the attempt protocol."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise LedgerError("ledger timestamps must be UTC")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError("ledger timestamp is invalid") from exc


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise LedgerError(f"{name} is invalid")
    return value


def _digest(value: Any, name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise LedgerError(f"{name} must be a SHA-256 digest")
    return value


def _count(value: Any, name: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerError(f"{name} must be a non-negative integer")
    return value


def _exact(body: Any, required: set[str], name: str) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != required:
        raise LedgerError(f"{name} has unexpected fields")
    return body


_AGENT_COUNTS = ("active_sessions", "unresolved_mutations", "prepared_proposals",
                 "conversation_rows")


def _agent_residue(value: Any, *, arm: str, product_started: bool) -> list[str]:
    """Validate a reset's Agent-state assertions and return the residue they show.

    Only WOA has Agent state. Its reset records the verified-empty state the attempt began
    from and the state after logout, with every prepared journal reservation counted as either
    closed by the audit or still open. A started attempt without an observed end state has no
    assertion, which by itself keeps its reset from passing.
    """
    if arm != "woa_product":
        if value is not None:
            raise LedgerError("only a WOA attempt carries Agent-state assertions")
        return []
    _exact(value, {"start", "end", "open_proposals", "closed_proposals"}, "reset_outcome.agent")
    for name in ("start", "end"):
        if value[name] is not None:
            _exact(value[name], set(_AGENT_COUNTS), f"reset_outcome.agent.{name}")
            for key in _AGENT_COUNTS:
                _count(value[name][key], f"reset_outcome.agent.{name}.{key}")
    _count(value["open_proposals"], "reset_outcome.agent.open_proposals")
    _count(value["closed_proposals"], "reset_outcome.agent.closed_proposals")
    start, end = value["start"], value["end"]
    if product_started and start is None:
        raise LedgerError("a started WOA attempt records the empty state it began from")
    residue = []
    if start is not None and any(start[key] for key in _AGENT_COUNTS):
        residue.append("inherited_state")
    if end is None:
        if value["open_proposals"] or value["closed_proposals"]:
            raise LedgerError("proposal counts need an observed end state")
        if product_started:
            residue.append("agent_state_unreadable")
        return residue
    if end["prepared_proposals"] != value["open_proposals"] + value["closed_proposals"]:
        raise LedgerError("every prepared proposal must be counted as closed or open")
    residue += [key for key in ("active_sessions", "unresolved_mutations") if end[key]]
    if value["open_proposals"]:
        residue.append("open_proposals")
    return residue


_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _request_parameters(value: Any, max_output_tokens: Any) -> dict[str, Any]:
    """Validate the generation parameters a dispatched request actually carried."""
    parameters = _exact(value, {"temperature", "max_tokens", "tool_choice", "tools", "options"},
                        "request parameters")
    temperature = parameters["temperature"]
    if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
            or not 0 <= temperature <= 2):
        raise LedgerError("request temperature is invalid")
    output = parameters["max_tokens"]
    if (isinstance(output, bool) or not isinstance(output, int) or output < 1
            or output != max_output_tokens):
        raise LedgerError("a dispatched request carries a positive output cap equal to "
                          "max_output_tokens")
    choice = parameters["tool_choice"]
    if choice is not None and (not isinstance(choice, str) or not _NAME.fullmatch(choice)):
        raise LedgerError("request tool choice is invalid")
    _count(parameters["tools"], "request tools")
    options = parameters["options"]
    if not isinstance(options, dict) or any(
            not isinstance(name, str) or not _NAME.fullmatch(name)
            or option is None or isinstance(option, (dict, list))
            for name, option in options.items()):
        raise LedgerError("request options are invalid")
    return parameters


def records_request_parameters(value: Any, max_output_tokens: Any) -> bool:
    """Whether this ledger would accept these generation parameters on a dispatch.

    The gateway asks before it takes a lease. It appends the dispatch entry only after the
    lease exists, so a request it forwards that this ledger then refuses would leave a lease
    nothing can settle and an attempt that can never end (protocol section 7, "any attempt
    ends only once every lease it holds has settled"). One rule decides what a dispatched
    request may carry, and it is this one.
    """
    try:
        _request_parameters(value, max_output_tokens)
    except LedgerError:
        return False
    return True


def _entry_digest(entry: dict[str, Any]) -> str:
    return digest_json({key: value for key, value in entry.items() if key != "digest"})
