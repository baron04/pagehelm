"""Append-only, hash-chained attempt ledger for P7.4 batches.

Every attempt is written down before its runner starts, every model request before it is
forwarded, and every result afterwards, so a failed or interrupted trial cannot be rerun
without leaving a trace. The file reuses the Twenty batch ledger's discipline -- a private
``0600`` regular file, a POSIX lock held across read-validate-append, SHA-256 entry digests
linking each line to the previous one -- but records attempts rather than completed trials:
an attempt may be interrupted, replaced within a closed policy, or end a batch.

Validation replays the whole chain on every append and on every read. The rules it enforces
are the protocol's, not conveniences: one open attempt at a time, a fixed record order per
attempt, legal combinations of the three outcome axes, bounded replacements that follow their
original immediately, and mandatory seals after safety events, reset failures, unclassified
results, provider model-string changes, requests that belong to no attempt, a refused provider
credential, a call the spend ledger could not record and a request outside the frozen request
contract.

Those rules are themselves a contract, defined by this module and the few it relies on. Every
header binds the digest of that contract, and a ledger written under any other contract is
refused whole rather than read by rules it was not written under.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.twenty.evaluator import EvaluationInputError, digest_json
from evals.twenty.records import _decode, _json_line, _locked_ledger

from . import outcomes

LEDGER_VERSION = 1
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
#: Stops the product did not cause. After one, the same development candidate may continue and a
#: qualification batch may start again under the same protocol version.
EXOGENOUS_STOPS = frozenset({"model_identity_changed", "replacement_cap_exceeded",
                             "provider_authentication_failed", "accounting_failure"})
#: A held-out batch starts again under the same protocol version after these stops as they are,
HELDOUT_RESTART_REASONS = frozenset({"model_identity_changed", "replacement_cap_exceeded"})
#: and after these only on an independent audit, named in its link, that the stopped batch exposed
#: nothing -- no model answer or judged product result, no dispatch and no state write. Without
#: that audit the held-out set retires.
HELDOUT_AUDITED_RESTART_REASONS = frozenset({"provider_authentication_failed",
                                             "accounting_failure"})
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
_REPOSITORY = Path(__file__).resolve().parents[2]
#: The files whose rules decide what a ledger's entries mean: the validation, the outcome axes and
#: whitelist, and the shared line encoding and digests.
CONTRACT_FILES = ("evals/odoo/ledger.py", "evals/odoo/outcomes.py", "evals/twenty/records.py",
                  "evals/twenty/evaluator.py")


def contract_digest(repository: Path = _REPOSITORY) -> str:
    """The digest of the ledger contract that the files in ``repository`` define."""
    return digest_json({"ledger_version": LEDGER_VERSION, "files": {
        relative: "sha256:" + hashlib.sha256((repository / relative).read_bytes()).hexdigest()
        for relative in CONTRACT_FILES}})


#: The contract this reader applies; a header must bind exactly this digest.
LEDGER_CONTRACT_DIGEST = contract_digest()


class LedgerError(EvaluationInputError):
    """The ledger or a prospective entry violates the attempt protocol."""


class LedgerContractError(LedgerError):
    """A ledger was written under another ledger contract. Only the reader that wrote it may read
    it; no other reader interprets its entries."""


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


def _entry_digest(entry: dict[str, Any]) -> str:
    return digest_json({key: value for key, value in entry.items() if key != "digest"})


class _Replay:
    """Incremental validator for one ledger's entries."""

    def __init__(self) -> None:
        self.header: dict[str, Any] | None = None
        self.slots: dict[str, dict[str, Any]] = {}
        self.slot_order: list[str] = []
        self.attempts: dict[str, dict[str, Any]] = {}
        self.attempt_order: list[str] = []
        self.open_attempt: str | None = None
        self.pending_seal: str | None = None
        #: Every reason the batch met, whether or not it is the one sealed for.
        self.seal_reasons: set[str] = set()
        #: Calls whose cost nobody knows, as ``<attempt>#<request>``. While any is outstanding
        #: the batch holds: it takes no new attempt, forwards no new call and cannot complete.
        #: The hold is lifted by a reconciliation of the provider's bill, applied here.
        self.cost_unknown: set[str] = set()
        self.terminal: dict[str, Any] | None = None
        self.batch_model: str | None = None
        self.replacements_by_arm: Counter[str] = Counter()
        self.previous_at: datetime | None = None
        self.head: str | None = None

    # -- helpers ---------------------------------------------------------------------------
    def _require_open(self, attempt_id: Any) -> dict[str, Any]:
        if attempt_id != self.open_attempt or attempt_id not in self.attempts:
            raise LedgerError("record does not belong to the open attempt")
        return self.attempts[attempt_id]

    def _next_slot(self) -> str | None:
        for slot_id in self.slot_order:
            if not self.slots[slot_id]["attempts"]:
                return slot_id
        return None

    def _mark_seal(self, reason: str) -> None:
        """Keep every reason the batch met, and seal for the most serious of them."""
        if reason not in _SEAL_PRIORITY:
            raise LedgerError("seal reason is invalid")
        self.seal_reasons.add(reason)
        self.pending_seal = min(self.seal_reasons, key=_SEAL_PRIORITY.index)

    # -- entry kinds -----------------------------------------------------------------------
    def header_entry(self, body: dict[str, Any]) -> None:
        required = {"batch_id", "protocol_id", "protocol_digest", "schedule_digest", "purpose",
                    "provider_config_digest", "slots", "previous_batch", "task_set_seal_digest",
                    "fixture", "capability_digests", "candidate", "schedule_seed",
                    "provider_binding_digest", "ledger_contract_digest"}
        _exact(body, required, "batch header")
        _identifier(body["batch_id"], "batch_id")
        _identifier(body["protocol_id"], "protocol_id")
        for name in ("protocol_digest", "schedule_digest", "provider_config_digest",
                     "task_set_seal_digest", "provider_binding_digest", "ledger_contract_digest"):
            _digest(body[name], name)
        if body["purpose"] not in {"apparatus_qualification", "development", "heldout"}:
            raise LedgerError("batch purpose is invalid")
        fixture = _exact(body["fixture"], {"manifest_digest", "baseline_guard_digest"},
                         "batch fixture")
        _digest(fixture["manifest_digest"], "fixture manifest digest")
        _digest(fixture["baseline_guard_digest"], "fixture baseline guard digest")
        candidate = _exact(body["candidate"], {"commit", "agent_image_id",
                                               "agent_image_revision"}, "batch candidate")
        if (not isinstance(candidate["commit"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", candidate["commit"])
                or candidate["agent_image_revision"] != candidate["commit"]):
            raise LedgerError("the batch candidate is a full commit whose Agent image carries "
                              "that revision")
        _digest(candidate["agent_image_id"], "candidate Agent image id")
        seed = body["schedule_seed"]
        if body["purpose"] == "apparatus_qualification":
            if seed is not None:
                raise LedgerError("a qualification schedule is written out, not derived from a "
                                  "seed")
        elif not isinstance(seed, str) or not re.fullmatch(r"[0-9a-f]{32}", seed):
            raise LedgerError("a formal batch records the seed its schedule was derived from")
        slots = body["slots"]
        if not isinstance(slots, list) or not 1 <= len(slots) <= 1_000:
            raise LedgerError("batch slots must be a bounded non-empty list")
        for slot in slots:
            _exact(slot, {"slot_id", "arm", "task_id", "replicate"}, "slot")
            slot_id = _identifier(slot["slot_id"], "slot_id")
            if slot["arm"] not in _ARMS or slot["replicate"] not in _REPLICATES:
                raise LedgerError("slot arm or replicate is invalid")
            _identifier(slot["task_id"], "task_id")
            if slot_id in self.slots:
                raise LedgerError("slot ids must be unique")
            self.slots[slot_id] = {**slot, "attempts": []}
            self.slot_order.append(slot_id)
        if digest_json(slots) != body["schedule_digest"]:
            raise LedgerError("batch slots do not match their schedule digest")
        capabilities = body["capability_digests"]
        if (not isinstance(capabilities, dict) or set(capabilities) - _ARMS
                or not {slot["arm"] for slot in slots} <= set(capabilities)):
            raise LedgerError("the header binds a capability digest for every scheduled arm")
        for arm, value in capabilities.items():
            _digest(value, f"{arm} capability digest")
        previous = body["previous_batch"]
        if previous is not None:
            _exact(previous, {"batch_id", "purpose", "protocol_digest", "candidate_commit",
                              "chain_head", "termination_reason", "continuation_record"},
                   "previous batch")
            _identifier(previous["batch_id"], "previous batch_id")
            _digest(previous["chain_head"], "previous chain head")
            _digest(previous["protocol_digest"], "previous protocol digest")
            continuation = _digest(previous["continuation_record"], "continuation record",
                                   optional=True)
            if previous["purpose"] != body["purpose"]:
                raise LedgerError("a batch chain holds batches of one purpose")
            if (not isinstance(previous["candidate_commit"], str)
                    or not re.fullmatch(r"[0-9a-f]{40}", previous["candidate_commit"])):
                raise LedgerError("the previous batch names its full candidate commit")
            reason = previous["termination_reason"]
            if reason not in _SEAL_REASONS | {"batch_completed"}:
                raise LedgerError("the previous batch names how it ended")
            same_protocol = previous["protocol_digest"] == body["protocol_digest"]
            if body["purpose"] == "development":
                # Development tunes the product: a new candidate may follow any finished batch,
                # but a candidate never reruns a batch that did not stop for an exogenous reason.
                if (previous["candidate_commit"] == body["candidate"]["commit"]
                        and reason not in EXOGENOUS_STOPS):
                    raise LedgerError("a development candidate runs one batch; only an exogenous "
                                      "stop lets the same candidate continue")
            elif same_protocol and reason not in (
                    EXOGENOUS_STOPS if body["purpose"] == "apparatus_qualification"
                    else HELDOUT_RESTART_REASONS | HELDOUT_AUDITED_RESTART_REASONS):
                raise LedgerError(
                    "a completed batch, or one sealed for a product reason, cannot be followed "
                    "under the same protocol version")
            audited = (body["purpose"] == "heldout" and same_protocol
                       and reason in HELDOUT_AUDITED_RESTART_REASONS)
            if (audited or reason == "accounting_failure") != (continuation is not None):
                raise LedgerError(
                    "a batch after an unaccounted call names the reconciliation of the provider "
                    "bill, a held-out restart after a refused credential or an unaccounted call "
                    "names the audit that its stopped batch exposed nothing, and no other link "
                    "names a continuation record")
        self.header = body

    def attempt_started(self, body: dict[str, Any]) -> None:
        required = {"attempt_id", "slot_id", "arm", "task_id", "replicate", "run_id",
                    "fixture_digest", "capability_digest", "provider_config_digest",
                    "provider_binding_digest", "replacement_of"}
        _exact(body, required, "attempt_started")
        attempt_id = _identifier(body["attempt_id"], "attempt_id")
        _identifier(body["run_id"], "run_id")
        for name in ("fixture_digest", "capability_digest", "provider_config_digest",
                     "provider_binding_digest"):
            _digest(body[name], name)
        if self.open_attempt is not None:
            raise LedgerError("an attempt is already open; the driver is exclusive")
        if self.pending_seal is not None:
            raise LedgerError(f"batch must seal ({self.pending_seal}) before another attempt")
        if self.cost_unknown:
            raise LedgerError("this batch holds for a call whose cost is unknown; reconcile it "
                              "before another attempt")
        if attempt_id in self.attempts or any(
                item["run_id"] == body["run_id"] for item in self.attempts.values()):
            raise LedgerError("attempt and run ids must be unique")
        if body["provider_config_digest"] != self.header["provider_config_digest"]:
            raise LedgerError("attempt provider configuration differs from its batch")
        if body["provider_binding_digest"] != self.header["provider_binding_digest"]:
            raise LedgerError("attempt runs against another provider endpoint, credential or "
                              "request contract than its batch")
        slot = self.slots.get(body["slot_id"])
        if slot is None or any(slot[key] != body[key] for key in ("arm", "task_id",
                                                                   "replicate")):
            raise LedgerError("attempt does not match a scheduled slot")
        if body["fixture_digest"] != self.header["fixture"]["manifest_digest"]:
            raise LedgerError("attempt fixture differs from the manifest its batch binds")
        if body["capability_digest"] != self.header["capability_digests"][body["arm"]]:
            raise LedgerError("attempt capability differs from the one its batch binds")
        original = body["replacement_of"]
        if original is None:
            if body["slot_id"] != self._next_slot():
                raise LedgerError("attempt is not the next scheduled slot")
        else:
            previous = self.attempts.get(original)
            if (previous is None or self.attempt_order[-1] != original
                    or previous["slot_id"] != body["slot_id"]
                    or previous["replacement_of"] is not None
                    or len(slot["attempts"]) != 1):
                raise LedgerError("a replacement must immediately follow its original slot attempt")
            if not previous["replacement_eligible"]:
                raise LedgerError("the original attempt is not eligible for replacement")
            arm_slots = sum(1 for item in self.slots.values() if item["arm"] == body["arm"])
            if (self.replacements_by_arm[body["arm"]] >= outcomes.replacement_cap(arm_slots)
                    or sum(self.replacements_by_arm.values())
                    >= outcomes.BATCH_REPLACEMENT_CAP):
                raise LedgerError("replacement cap is exhausted; the batch must seal")
            self.replacements_by_arm[body["arm"]] += 1
        self.attempts[attempt_id] = {
            **body, "records": [], "requests": {}, "responses": {}, "unresolved": set(),
            "refusals": 0,
            "outcome": None, "reset": None, "cost": None, "metrics": None, "terminal": None,
            "replacement_eligible": False,
        }
        slot["attempts"].append(attempt_id)
        self.attempt_order.append(attempt_id)
        self.open_attempt = attempt_id

    def model_request(self, body: dict[str, Any]) -> None:
        _exact(body, {"attempt_id", "request_seq", "body_digest", "requested_model",
                      "max_output_tokens", "parameters"}, "model_request_dispatched")
        attempt = self._require_open(body["attempt_id"])
        if attempt["records"]:
            raise LedgerError("model requests must precede the attempt's outcome records")
        # The last line of the hold: whatever entry point forwarded this call, a batch that owes
        # a bill for one it already made does not make another.
        if self.cost_unknown:
            raise LedgerError("this batch holds for a call whose cost is unknown; reconcile it "
                              "before forwarding another")
        seq = _count(body["request_seq"], "request_seq")
        if seq != len(attempt["requests"]) + attempt["refusals"] + 1:
            raise LedgerError("model request sequence is not contiguous")
        _digest(body["body_digest"], "body_digest")
        _request_parameters(body["parameters"], body["max_output_tokens"])
        attempt["requests"][seq] = body

    def model_refused(self, body: dict[str, Any]) -> None:
        _exact(body, {"attempt_id", "request_seq", "reason"}, "model_request_refused")
        attempt = self._require_open(body["attempt_id"])
        seq = _count(body["request_seq"], "request_seq")
        if seq != len(attempt["requests"]) + attempt["refusals"] + 1:
            raise LedgerError("model request sequence is not contiguous")
        if body["reason"] not in REFUSAL_REASONS:
            raise LedgerError("model refusal reason is invalid")
        attempt["refusals"] += 1

    def model_response(self, body: dict[str, Any]) -> None:
        _exact(body, {"attempt_id", "request_seq", "status", "http_status",
                      "provider_request_id", "response_model", "usage", "cost_cny",
                      "error_code"}, "model_response_received")
        attempt = self._require_open(body["attempt_id"])
        if attempt["records"]:
            raise LedgerError("model responses must precede the attempt's outcome records")
        seq = body["request_seq"]
        if (seq not in attempt["requests"] or seq in attempt["responses"]
                or seq in attempt["unresolved"]):
            raise LedgerError("model response does not answer one dispatched request")
        if body["status"] not in RESPONSE_STATUSES:
            raise LedgerError("model response status is invalid")
        usage = body["usage"]
        if usage is not None:
            _exact(usage, {"input_tokens", "output_tokens", "cached_tokens",
                           "reasoning_tokens"}, "usage")
            for key, value in usage.items():
                _count(value, f"usage.{key}")
        model = body["response_model"]
        # Any model string a response names counts toward the batch's one model identity.
        if body["status"] == "ok" or model is not None:
            if not isinstance(model, str) or not model.strip() or len(model) > 120:
                raise LedgerError("a provider response must report its model string")
            if self.batch_model is None:
                self.batch_model = model
            elif model != self.batch_model:
                self._mark_seal("model_identity_changed")
        attempt["responses"][seq] = body

    def model_unresolved(self, body: dict[str, Any]) -> None:
        """A dispatched call that never settled within the provider's own deadline.

        It is written while its attempt is still open, so the call is settled exactly once and
        no answer is left to arrive after this ledger would take it. Its cost is unknown: the
        attempt counts it as usage unavailable and the bill reconciliation must clear it.
        """
        _exact(body, {"attempt_id", "request_seq", "reason"}, "model_response_unresolved")
        attempt = self._require_open(body["attempt_id"])
        # Unlike an answer, this record may follow the attempt's outcome records: it adds no
        # usage and no cost, and a driver that died mid-attempt settles its calls on recovery,
        # where the outcome is written from what was persisted before them.
        seq = body["request_seq"]
        if (seq not in attempt["requests"] or seq in attempt["responses"]
                or seq in attempt["unresolved"]):
            raise LedgerError("model response does not answer one dispatched request")
        if body["reason"] not in UNRESOLVED_REASONS:
            raise LedgerError("unresolved reason is invalid")
        attempt["unresolved"].add(seq)
        # Nobody knows what this call cost, so the batch holds until the bill says.
        self.cost_unknown.add(f"{body['attempt_id']}#{seq}")

    def billing_applied(self, body: dict[str, Any]) -> None:
        """A committed reconciliation of the provider's bill, applied to the calls it covers.

        It lifts the hold those calls put on the batch and nothing else: the record itself lives
        in the workbench, and this entry names only which calls it accounts for and the digest
        of the record that accounts for them. A batch that still holds any other call keeps
        holding.
        """
        _exact(body, {"covered_calls", "reconciliation_digest", "coverage_digest"},
               "billing_reconciliation_applied")
        _digest(body["reconciliation_digest"], "reconciliation_digest")
        _digest(body["coverage_digest"], "coverage_digest")
        covered = body["covered_calls"]
        if (not isinstance(covered, list) or not covered
                or any(not isinstance(item, str) for item in covered)):
            raise LedgerError("a reconciliation names the calls it covers")
        if sorted(covered) != covered or len(set(covered)) != len(covered):
            raise LedgerError("covered calls are listed once, in order")
        unknown = [item for item in covered if item not in self.cost_unknown]
        if unknown:
            raise LedgerError("a reconciliation covers only calls whose cost this batch does "
                              f"not know ({', '.join(unknown[:3])})")
        self.cost_unknown.difference_update(covered)

    def checkpoint(self, body: dict[str, Any]) -> None:
        _exact(body, {"attempt_id", "product_started", "dispatch_state", "evidence"},
               "attempt_checkpoint")
        attempt = self._require_open(body["attempt_id"])
        if body["dispatch_state"] not in outcomes.DISPATCH_STATES:
            raise LedgerError("checkpoint dispatch state is invalid")
        if not isinstance(body["product_started"], bool) or not isinstance(body["evidence"], dict):
            raise LedgerError("checkpoint fields are invalid")
        for name, digest in body["evidence"].items():
            _identifier(name, "evidence name")
            _digest(digest, "evidence digest")
        if attempt["records"]:
            raise LedgerError("checkpoints must precede the attempt's outcome records")

    def attempt_record(self, kind: str, body: dict[str, Any]) -> None:
        attempt = self._require_open(body.get("attempt_id"))
        position = len(attempt["records"])
        expected = _ATTEMPT_KINDS[position] if position < len(_ATTEMPT_KINDS) else None
        if kind != expected:
            raise LedgerError(f"{kind} is out of order for its attempt")
        if kind == "attempt_outcome":
            fields = {"attempt_id", "product_outcome", "apparatus_integrity", "apparatus_code",
                      "unclassified", "product_started", "dispatch_state", "basis",
                      "safety_events", "seal_reason", "seal_reasons", "extraction_ambiguous",
                      "replacement_evidence", "evaluation_digest", "truth_digest"}
            _exact(body, fields, "attempt_outcome")
            _digest(body["evaluation_digest"], "evaluation_digest", optional=True)
            _digest(body["truth_digest"], "truth_digest", optional=True)
            # An attempt names every reason it seals for, and seals for the most serious of
            # them. The batch reduces the same set by the same order, so which fault the driver
            # recorded first cannot decide what the batch sealed for.
            reasons = body["seal_reasons"]
            if not isinstance(reasons, list) or any(item not in _SEAL_REASONS
                                                    for item in reasons):
                raise LedgerError("attempt seal reasons are invalid")
            if bool(body["seal_reason"]) != bool(reasons) or (
                    reasons and body["seal_reason"] != min(reasons, key=_SEAL_PRIORITY.index)):
                raise LedgerError("an attempt seals for the most serious reason it names")
            attempt["outcome"] = body
        elif kind == "reset_outcome":
            _exact(body, {"attempt_id", "outcome", "basis", "guard_digest", "agent"},
                   "reset_outcome")
            _digest(body["guard_digest"], "guard_digest", optional=True)
            residue = _agent_residue(body["agent"], arm=attempt["arm"],
                                     product_started=attempt["outcome"]["product_started"])
            if residue and body["outcome"] in {"PASS", "NOT_REQUIRED"}:
                raise LedgerError("Agent residue cannot accompany a passing or unrequired reset")
            try:
                outcomes.validate_combination(attempt["outcome"], body["outcome"])
            except outcomes.OutcomeContractError as exc:
                raise LedgerError(str(exc)) from exc
            attempt["reset"] = body["outcome"]
        elif kind == "attempt_metrics":
            _exact(body, {"attempt_id", *_METRIC_FIELDS}, "attempt_metrics")
            for name in _METRIC_FIELDS:
                _count(body[name], name, optional=name in _OPTIONAL_METRICS)
            attempt["metrics"] = body
        else:
            fields = {"attempt_id", "model_calls", "usage_unavailable_calls", "input_tokens",
                      "output_tokens", "cached_tokens", "reasoning_tokens", "known_cost_cny",
                      "total_cost_cny"}
            _exact(body, fields, "attempt_cost")
            responses = attempt["responses"].values()
            if body["model_calls"] != len(attempt["requests"]):
                raise LedgerError("cost record disagrees with dispatched model requests")
            unavailable = sum(1 for item in attempt["requests"]
                              if attempt["responses"].get(item) is None
                              or attempt["responses"][item]["usage"] is None
                              or attempt["responses"][item]["cost_cny"] is None)
            if body["usage_unavailable_calls"] != unavailable:
                raise LedgerError("cost record hides usage-unavailable calls")
            for key in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
                total = sum(item["usage"][key] for item in responses if item["usage"])
                if body[key] != total:
                    raise LedgerError(f"cost record {key} disagrees with provider responses")
            known = round(sum(item["cost_cny"] for item in responses
                              if item["cost_cny"] is not None), 6)
            if abs(float(body["known_cost_cny"]) - known) > 1e-9:
                raise LedgerError("known cost disagrees with provider responses")
            if unavailable:
                if body["total_cost_cny"] is not None:
                    raise LedgerError("unknown usage cannot be reported as a total cost")
            elif body["total_cost_cny"] is None or abs(body["total_cost_cny"] - known) > 1e-9:
                raise LedgerError("a fully metered attempt reports its total cost")
            attempt["cost"] = body
        attempt["records"].append(kind)

    def terminal_entry(self, kind: str, body: dict[str, Any]) -> None:
        if kind == "attempt_terminal":
            _exact(body, {"attempt_id"}, "attempt_terminal")
        else:
            _exact(body, {"attempt_id", "evidence_kinds", "replacement_eligible"},
                   "interrupted_attempt")
        attempt = self._require_open(body.get("attempt_id"))
        if attempt["records"] != list(_ATTEMPT_KINDS):
            raise LedgerError("an attempt ends only after outcome, reset, cost and metrics")
        # Every call the attempt dispatched settled exactly once: an answer, or a record that
        # the provider's deadline passed without one. Nothing is left for a later arrival.
        if any(seq not in attempt["responses"] and seq not in attempt["unresolved"]
               for seq in attempt["requests"]):
            raise LedgerError("an attempt ends only once every call it made has settled")
        if kind == "attempt_terminal" and any(attempt["metrics"][name] is None
                                              for name in _RUN_TIMES):
            raise LedgerError("an attempt that finished reports its automatic and confirmation "
                              "times")
        outcome, reset = attempt["outcome"], attempt["reset"]
        eligible = outcomes.replacement_eligible(outcome, reset)
        if kind == "interrupted_attempt":
            evidence = body["evidence_kinds"]
            if (not isinstance(evidence, list) or not isinstance(body["replacement_eligible"], bool)
                    or any(item not in outcomes.REPLACEMENT_EVIDENCE for item in evidence)):
                raise LedgerError("interruption evidence is invalid")
            eligible = eligible and bool(evidence)
            if body["replacement_eligible"] != eligible:
                raise LedgerError("interruption replacement eligibility is misreported")
        attempt["replacement_eligible"] = eligible
        attempt["terminal"] = kind
        self.open_attempt = None
        # Every reason the attempt named, not only the one it reduced to.
        for reason in outcome["seal_reasons"]:
            self._mark_seal(reason)
        if reset in {"FAIL", "INDETERMINATE"}:
            self._mark_seal("reset_failed")
        if (outcome["apparatus_integrity"] == "INVALID" and outcome["apparatus_code"]
                in outcomes.APPARATUS_WHITELIST and attempt["replacement_of"] is None
                and eligible):
            arm_slots = sum(1 for item in self.slots.values() if item["arm"] == attempt["arm"])
            if (self.replacements_by_arm[attempt["arm"]] >= outcomes.replacement_cap(arm_slots)
                    or sum(self.replacements_by_arm.values())
                    >= outcomes.BATCH_REPLACEMENT_CAP):
                self._mark_seal("replacement_cap_exceeded")

    def apply(self, entry: dict[str, Any]) -> None:
        kind, body = entry["kind"], entry["body"]
        if self.terminal is not None:
            raise LedgerError("the batch ledger is already terminal")
        if not isinstance(body, dict):
            raise LedgerError("ledger entry body must be an object")
        if kind == "batch_header":
            self.header_entry(body)
        elif self.header is None:
            raise LedgerError("the first entry must be a batch header")
        elif kind == "attempt_started":
            self.attempt_started(body)
        elif kind == "model_request_dispatched":
            self.model_request(body)
        elif kind == "model_request_refused":
            self.model_refused(body)
        elif kind == "model_response_received":
            self.model_response(body)
        elif kind == "attempt_checkpoint":
            self.checkpoint(body)
        elif kind in _ATTEMPT_KINDS:
            self.attempt_record(kind, body)
        elif kind in _TERMINAL_KINDS:
            self.terminal_entry(kind, body)
        elif kind == "unowned_model_request":
            _exact(body, {"body_digest", "reason"}, "unowned_model_request")
            _digest(body["body_digest"], "body_digest")
            self._mark_seal("unowned_model_request")
        elif kind == "model_response_unresolved":
            self.model_unresolved(body)
        elif kind == "billing_reconciliation_applied":
            self.billing_applied(body)
        elif kind == "batch_sealed":
            _exact(body, {"reason"}, "batch_sealed")
            if body["reason"] not in _SEAL_REASONS:
                raise LedgerError("seal reason is invalid")
            if self.open_attempt is not None:
                raise LedgerError("close the open attempt before sealing the batch")
            if self.pending_seal is not None and body["reason"] != self.pending_seal:
                raise LedgerError(f"the batch must seal for {self.pending_seal}")
            # A batch that holds for a cost nobody knows still seals for a fault it met -- a
            # safety event must not wait on a bill. What it must not do is end on the operator's
            # own stop, which would leave that cost owed by a batch nobody can resume.
            #
            # The review of f85c1c1: this refused every reason supplied from outside while the
            # batch held, not the operator's stop it names -- and a model the preflight never
            # saw is a fault the batch met, so the entry could not seal for it and ran on.
            if (self.cost_unknown and self.pending_seal is None
                    and body["reason"] == "operator_stop"):
                raise LedgerError("this batch holds for a call whose cost is unknown; reconcile "
                                  "it before stopping the batch")
            # The review of 6376c20: a reason given here rather than met by an attempt -- an
            # operator's stop -- became the terminal without joining the reasons the batch met,
            # so the summary named a seal its own fact set did not hold. It is marked last, so
            # that an operator's stop cannot satisfy the checks above by being counted early.
            self._mark_seal(body["reason"])
            self.terminal = {"kind": kind, "reason": body["reason"]}
        elif kind == "batch_completed":
            _exact(body, set(), "batch_completed")
            if self.cost_unknown:
                raise LedgerError("a batch holding for a call whose cost is unknown cannot "
                                  f"complete ({', '.join(sorted(self.cost_unknown)[:3])})")
            if self.open_attempt is not None or self.pending_seal is not None:
                raise LedgerError("a batch with an open attempt or a pending seal cannot complete")
            if any(not slot["attempts"] for slot in self.slots.values()):
                raise LedgerError("every scheduled slot needs an attempt before completion")
            last = self.attempts[self.attempt_order[-1]]
            if last["replacement_eligible"] and last["replacement_of"] is None:
                raise LedgerError("a replacement-eligible final attempt was left unreplaced")
            self.terminal = {"kind": kind, "reason": None}
        else:
            raise LedgerError("unknown ledger entry kind")

    def _disposition(self, status: str) -> str:
        """The one answer to what this batch may do next, in the order the rules outrank.

        An attempt still open is closed before anything else; a fault the batch already met
        seals it even while it holds for a bill -- a safety event does not wait on a bill -- and
        a cost nobody knows stops everything else.

        This answers what the *batch* may do. Whether a call may be forwarded is a question
        about the hold, which outlives an open attempt and which the gateway reads directly:
        an attempt open while the batch holds is still ``attempt_open``, because closing it
        comes first whatever it owes.
        """
        if self.terminal is not None:
            return "complete" if status == "COMPLETE" else "sealed"
        if self.open_attempt is not None:
            return "attempt_open"
        if self.pending_seal is not None:
            return "must_seal"
        if self.cost_unknown:
            return "holding"
        return "running"

    def summary(self) -> dict[str, Any]:
        status = "OPEN"
        if self.terminal is not None:
            status = "COMPLETE" if self.terminal["kind"] == "batch_completed" else "SEALED"
        attempts = []
        for attempt_id in self.attempt_order:
            item = self.attempts[attempt_id]
            outcome = item["outcome"] or {}
            attempts.append({
                "attempt_id": attempt_id, "slot_id": item["slot_id"], "arm": item["arm"],
                "task_id": item["task_id"], "replicate": item["replicate"],
                "replacement_of": item["replacement_of"], "terminal": item["terminal"],
                "product_outcome": outcome.get("product_outcome"),
                "apparatus_integrity": outcome.get("apparatus_integrity"),
                "apparatus_code": outcome.get("apparatus_code"),
                "unclassified": outcome.get("unclassified"),
                "safety_events": outcome.get("safety_events") or [],
                "extraction_ambiguous": outcome.get("extraction_ambiguous"),
                "reset_outcome": item["reset"], "cost": item["cost"],
                "metrics": item["metrics"],
                "replacement_eligible": item["replacement_eligible"],
            })
        return {
            "artifact_version": LEDGER_VERSION,
            "kind": "p74_attempt_ledger_status",
            "status": status,
            #: What this batch may do next, from what the ledger holds. Every rule below was
            #: read as its own condition in three places -- the driver, the entry and the
            #: harness -- and the defects the reviews found there were combinations of those
            #: conditions no single place enumerated, not rules anyone had left out. An
            #: operator's stop is deliberately not here: it lives outside the ledger, and a
            #: caller weighs it against this.
            "disposition": self._disposition(status),
            "seal_reason": self.terminal["reason"] if self.terminal else None,
            "pending_seal": self.pending_seal,
            #: The calls this batch holds for. While any is listed it takes no new attempt,
            #: forwards no new call and cannot complete; a reconciliation of the bill lifts it.
            "cost_unknown": sorted(self.cost_unknown),
            # Every reason met, most serious first: the batch seals for the first of them and
            # the rest are kept rather than lost to whichever arrived first.
            "seal_reasons": sorted(self.seal_reasons, key=_SEAL_PRIORITY.index),
            "batch": {key: self.header[key] for key in (
                "batch_id", "protocol_id", "protocol_digest", "schedule_digest", "purpose",
                "previous_batch", "provider_config_digest", "task_set_seal_digest", "fixture",
                "capability_digests", "candidate", "schedule_seed",
                "provider_binding_digest", "ledger_contract_digest")} if self.header else None,
            "slots": [{**{key: slot[key] for key in ("slot_id", "arm", "task_id",
                                                       "replicate")},
                       "attempts": list(slot["attempts"])}
                      for slot in (self.slots[item] for item in self.slot_order)],
            "attempts": attempts,
            "open_attempt": self.open_attempt,
            "provider_model": self.batch_model,
            "replacements": dict(self.replacements_by_arm),
            "chain_head": self.head,
        }


def replay(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate a complete entry list and return its status summary.

    A ledger whose header names another ledger contract is refused before any entry is read, so
    no rule of this contract is applied to it.
    """
    if entries:
        body = entries[0].get("body") if isinstance(entries[0], dict) else None
        written = body.get("ledger_contract_digest") if isinstance(body, dict) else None
        if written != LEDGER_CONTRACT_DIGEST:
            raise LedgerContractError("this ledger was written under another ledger contract "
                                      f"({written or 'none named'}); only the reader that wrote "
                                      "it may read it")
    state = _Replay()
    previous = None
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"v", "kind", "seq", "prev", "at",
                                                         "body", "digest"}:
            raise LedgerError("ledger entry has unexpected fields")
        if (entry["v"] != LEDGER_VERSION or entry["seq"] != index or entry["prev"] != previous
                or entry["kind"] not in KINDS or entry["digest"] != _entry_digest(entry)):
            raise LedgerError("ledger chain is invalid")
        if (index == 0) != (entry["kind"] == "batch_header"):
            raise LedgerError("only the first entry is a batch header")
        moment = _time(entry["at"])
        if state.previous_at is not None and moment < state.previous_at:
            raise LedgerError("ledger timestamps go backwards")
        state.previous_at = moment
        state.apply(entry)
        previous = entry["digest"]
        state.head = previous
    return state.summary()


def _new_entry(kind: str, body: dict[str, Any], seq: int, prev: str | None,
               at: str | None) -> dict[str, Any]:
    entry = {"v": LEDGER_VERSION, "kind": kind, "seq": seq, "prev": prev,
             "at": at or utc_now(), "body": body}
    entry["digest"] = _entry_digest(entry)
    return entry


def create(path: str | Path, header: dict[str, Any], *, at: str | None = None) -> dict[str, Any]:
    """Create a new private ledger holding only its validated batch header."""
    entry = _new_entry("batch_header", header, 0, None, at)
    summary = replay([entry])
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise LedgerError("attempt ledger already exists") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_json_line(entry))
        handle.flush()
        os.fsync(handle.fileno())
    return summary


def append(path: str | Path, kind: str, body: dict[str, Any], *,
           at: str | None = None) -> dict[str, Any]:
    """Validate the chain plus one prospective entry under the lock, then write it."""
    if kind == "batch_header":
        raise LedgerError("a batch header is written only by create()")
    with _locked_ledger(Path(path)) as handle:
        entries = _decode(handle)
        replay(entries)
        entry = _new_entry(kind, body, len(entries), entries[-1]["digest"], at)
        if _time(entry["at"]) < _time(entries[-1]["at"]):
            entry = _new_entry(kind, body, len(entries), entries[-1]["digest"],
                               entries[-1]["at"])
        summary = replay([*entries, entry])
        handle.write(_json_line(entry))
        return summary


def read(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with _locked_ledger(Path(path)) as handle:
        entries = _decode(handle)
        return entries, replay(entries)


def schedule_digest(slots: list[dict[str, Any]]) -> str:
    return digest_json(slots)


def exposure(entries: list[dict[str, Any]]) -> list[str]:
    """What a stopped batch let anyone see or change, read from its own validated entries.

    A held-out batch that stopped on a refused credential or an unaccounted call starts again
    under the same protocol only when this is empty and an independent audit confirms it: no
    model answer, no judged product result, no dispatch and no state write.
    """
    replay(entries)
    found: set[str] = set()
    for entry in entries:
        kind, body = entry["kind"], entry["body"]
        attempt = body.get("attempt_id")
        if kind == "model_response_received" and body["status"] == "ok":
            found.add(f"model_answer:{attempt}")
        elif kind == "attempt_checkpoint" and body["dispatch_state"] != "none":
            found.add(f"dispatch:{attempt}")
        elif kind == "attempt_outcome":
            if (body["evaluation_digest"] is not None
                    or body["product_outcome"] != "NOT_EVALUABLE"):
                found.add(f"judged_result:{attempt}")
            if body["dispatch_state"] != "none":
                found.add(f"dispatch:{attempt}")
        elif kind == "reset_outcome" and body["outcome"] != "NOT_REQUIRED":
            found.add(f"state_write:{attempt}")
    return sorted(found)
