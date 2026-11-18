"""Application-neutral unified write safety pipeline and shared invariants.

All four write routes (Skill-backed typed operations, native form/row submit,
typed status operations, and convention flat updates/toggles) execute through
this module's shared diff projection, value-source/append-preservation checks,
single-flight dispatch gate, pre-dispatch reread drift refusal, same-actor
readback verification, and indeterminate accounting.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
import hashlib
import json
import re
import threading
from typing import Any, Callable, Iterable

from ..schemas.approval_details import ApprovalDetail, validate_details
from .dispatch_evidence import mutation_evidence, verified_after_readback
from .effects import EffectProjection, RequestedEffect, canonical
from .errors import (
    BusinessMutationBlockedError,
    BusinessMutationIndeterminate,
    BusinessMutationRefusedError,
    BusinessToolError,
    BusinessToolPermissionError,
    BusinessToolReferenceError,
    BusinessToolRequestError,
)
from .prefill_conversion import MutationPrefillOffer, MutationPrefillPort
from .tools.contracts import BusinessMutationProposal, BusinessToolResult


DEFAULT_SECRET_FIELD_PATTERN = re.compile(
    r"pass(?:word)?|pwd|secret|private_?key|api[_-]?key|aes_?key|sign_?key|mch_?key|"
    r"token|authorization|credential|^config$|config$|密码|密钥|令牌",
    re.I,
)


# ---------------------------------------------------------------------------
# Structured Proposal Diff & ApprovalDetail Normalization
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FieldDiff:
    """One user-reviewable field transition projected onto an approval card."""

    label: str
    after: str
    before: str = ""
    kind: str = "change"
    has_before: bool = True

    def render_text(self) -> str:
        if self.has_before:
            return f"{self.before} → {self.after}"
        return self.after

    def to_detail(self) -> ApprovalDetail:
        return ApprovalDetail(self.kind, self.label, self.render_text())


def change_detail(label: str, before: str, after: str) -> ApprovalDetail:
    """Build an ``ApprovalDetail`` representing an ``old → new`` change."""
    return FieldDiff(label=label, before=before, after=after, kind="change", has_before=True).to_detail()


def create_detail(label: str, value: str, *, kind: str = "change") -> ApprovalDetail:
    """Build an ``ApprovalDetail`` representing a newly supplied value."""
    return FieldDiff(label=label, after=value, kind=kind, has_before=False).to_detail()


def preserved_detail(label: str, text: str) -> ApprovalDetail:
    return ApprovalDetail("preserved", label, text)


def side_effect_detail(label: str, text: str) -> ApprovalDetail:
    return ApprovalDetail("side_effect", label, text)


def derived_detail(label: str, text: str) -> ApprovalDetail:
    return ApprovalDetail("derived", label, text)


def target_rule_detail(label: str, text: str) -> ApprovalDetail:
    return ApprovalDetail("target_rule", label, text)


def body_detail(label: str, text: str) -> ApprovalDetail:
    return ApprovalDetail("body", label, text)


def normalize_approval_details(details: Iterable[ApprovalDetail | FieldDiff]) -> tuple[ApprovalDetail, ...]:
    """Coerce ``FieldDiff`` entries to ``ApprovalDetail`` and enforce bounded projection limits."""
    normalized = tuple(
        item.to_detail() if isinstance(item, FieldDiff) else item
        for item in details
    )
    validate_details(normalized)
    return normalized


@dataclass(frozen=True, slots=True)
class ProposalDiff:
    """Structured target / changes / preserved / side-effects diff for one write proposal."""

    targets: tuple[str, ...]
    changes: tuple[ApprovalDetail | FieldDiff, ...] = ()
    preserved: tuple[ApprovalDetail, ...] = ()
    side_effects: tuple[ApprovalDetail, ...] = ()
    derived: tuple[ApprovalDetail, ...] = ()
    target_rules: tuple[ApprovalDetail, ...] = ()
    extra: tuple[ApprovalDetail, ...] = ()
    ordered_details: tuple[ApprovalDetail | FieldDiff, ...] | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.targets, tuple)
            or not self.targets
            or len(self.targets) > 20
            or any(not isinstance(t, str) or not t.strip() or len(t.strip()) > 300 for t in self.targets)
        ):
            raise ValueError("proposal diff requires 1-20 bounded targets")
        object.__setattr__(self, "targets", tuple(t.strip() for t in self.targets))
        self.details()

    def details(self) -> tuple[ApprovalDetail, ...]:
        if self.ordered_details is not None:
            return normalize_approval_details(self.ordered_details)
        return normalize_approval_details((
            *self.target_rules,
            *self.changes,
            *self.preserved,
            *self.derived,
            *self.side_effects,
            *self.extra,
        ))

    @classmethod
    def from_details(
        cls,
        targets: Iterable[str],
        details: Iterable[ApprovalDetail | FieldDiff],
    ) -> "ProposalDiff":
        normalized = normalize_approval_details(details)
        return cls(
            targets=tuple(targets),
            changes=tuple(d for d in normalized if d.kind == "change"),
            preserved=tuple(d for d in normalized if d.kind == "preserved"),
            side_effects=tuple(d for d in normalized if d.kind == "side_effect"),
            derived=tuple(d for d in normalized if d.kind == "derived"),
            target_rules=tuple(d for d in normalized if d.kind == "target_rule"),
            extra=tuple(d for d in normalized if d.kind == "body"),
            ordered_details=normalized,
        )


# ---------------------------------------------------------------------------
# Value-Source, Append-Preservation & Snapshot Helpers
# ---------------------------------------------------------------------------

def verify_effect_projection(
    proposal: BusinessMutationProposal,
    expected: RequestedEffect | None,
    *,
    ledger=None,
    step: int = 0,
    tool: str = "",
) -> None:
    """Verify the application's prepared effect projection matches the locked request."""
    if expected is None:
        return
    if proposal.effect_projection is None:
        raise BusinessToolError("application did not project its prepared effect")
    proposal.effect_projection.require(expected)
    if ledger is not None:
        ledger.append(
            "skill_effect_bound",
            step=step,
            tool=tool or proposal.tool,
            proposal_digest=proposal.content_digest,
            side_effect_possible=False,
        )


def verify_preserves_unmentioned(
    proposal: BusinessMutationProposal,
    required_preserved_fields: Iterable[str] = (),
) -> None:
    """Assert a prepared proposal declares unmentioned-field preservation and expected fields."""
    projection = proposal.effect_projection
    if projection is None or not projection.preserves_unmentioned:
        raise BusinessToolRequestError("application cannot preserve all unmentioned business fields")
    required = set(required_preserved_fields)
    if required:
        preserved = set(json.loads(projection.preserved_json))
        if required - preserved:
            raise BusinessToolRequestError("requested preservation is not covered by the application")


def verify_value_sources(
    ledger,
    words: list[str] | tuple[str, ...],
    *,
    existing: dict[str, str] | None = None,
) -> tuple[list, list]:
    """Run value-source provenance checks and return ``(verdicts, missing_verdicts)``."""
    from ..execution.value_source import check_entries

    verdicts = check_entries(ledger, list(words), existing=existing)
    missing = [v for v in verdicts if not v.ok]
    return verdicts, missing


def verify_entry_provenance(
    entry,
    scoped_cands: list[tuple[str, str]],
    texts: tuple[str, ...] | list[str],
) -> bool:
    """Validate one value entry against user texts and scoped candidate values."""
    from ..execution.value_source import SourceLedger, check_entries

    sub = SourceLedger()
    if scoped_cands:
        sub.add_candidates([(entry.field, val) for _binding, val in scoped_cands])
    sub.add(entry)
    return check_entries(sub, list(texts))[0].ok


def check_append_preservation(
    prompt: str,
    checks: Iterable[tuple[tuple[str, ...], object, object]],
) -> list[str]:
    """Return primary labels of fields whose existing value was overwritten on an append request."""
    from ..execution.value_source import append_overwrites_existing

    overwritten: list[str] = []
    for names, old_val, new_val in checks:
        candidates = tuple(n for n in names if isinstance(n, str) and n)
        if not candidates:
            continue
        primary = candidates[0]
        if any(append_overwrites_existing(prompt, name, old_val, new_val) for name in candidates):
            if primary not in overwritten:
                overwritten.append(primary)
    return overwritten


def require_append_preservation(
    prompt: str,
    checks: Iterable[tuple[tuple[str, ...], object, object]],
    *,
    error_factory: Callable[[list[str]], Exception] | None = None,
) -> None:
    """Raise when an append/supplement instruction would overwrite an existing non-empty value."""
    overwritten = check_append_preservation(prompt, checks)
    if overwritten:
        if error_factory is not None:
            raise error_factory(overwritten)
        raise BusinessToolRequestError(
            f"用户要求在「{'、'.join(overwritten)}」中追加或补充内容，当前修改覆盖或清空了原有内容；"
            "请先读取并保留目标记录该字段原值，再在其后追加本次内容。"
        )


def non_volatile_snapshot(
    record: dict,
    volatile: frozenset[str],
    *,
    secret_pattern: re.Pattern[str] = DEFAULT_SECRET_FIELD_PATTERN,
) -> dict:
    """Copy non-volatile, non-secret fields from a read record for drift/preservation checks."""
    return {
        k: v
        for k, v in record.items()
        if k not in volatile and not secret_pattern.search(k)
    }


def restricted_snapshot_digests(
    record: dict,
    volatile: frozenset[str],
    *,
    secret_pattern: re.Pattern[str] = DEFAULT_SECRET_FIELD_PATTERN,
) -> dict[str, str]:
    """Digest excluded sensitive/config fields by hashed field name without exposing keys or values."""
    return {
        hashlib.sha256(k.encode()).hexdigest()[:16]: hashlib.sha256(canonical(record[k]).encode()).hexdigest()
        for k in sorted(record)
        if k not in volatile and secret_pattern.search(k)
    }


def _default_values_equal(left, right) -> bool:
    from ..execution.submit_contract import same_value

    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return same_value(left, right)


def verify_preservation_snapshot(
    before: dict,
    after: dict,
    *,
    volatile: frozenset[str] = frozenset(),
    ignore: frozenset[str] = frozenset(),
    restricted_before: dict[str, str] | None = None,
    restricted_after: dict[str, str] | None = None,
    values_equal: Callable[[object, object], bool] | None = None,
) -> list[str]:
    """Return sorted field names that drifted between ``before`` and ``after``."""
    from ..execution.submit_contract import same_value

    cmp_fn = values_equal or _default_values_equal
    excluded = volatile | ignore
    drifted: list[str] = []
    for key in sorted(set(before) | set(after)):
        if key in excluded:
            continue
        b_val, a_val = before.get(key), after.get(key)
        if isinstance(b_val, list) and isinstance(a_val, list):
            if len(b_val) != len(a_val) or any(not same_value(x, y) for x, y in zip(b_val, a_val)):
                drifted.append(key)
        elif isinstance(b_val, dict) or isinstance(a_val, dict):
            if canonical(b_val) != canonical(a_val):
                drifted.append(key)
        elif not cmp_fn(b_val, a_val):
            drifted.append(key)
    if (
        not drifted
        and restricted_before is not None
        and restricted_after is not None
        and restricted_before != restricted_after
    ):
        drifted.append("restricted_field")
    return drifted


# ---------------------------------------------------------------------------
# Single-Flight Inflight Write Tracker & Pipeline Stages
# ---------------------------------------------------------------------------

class InflightWriteTracker:
    """Thread-safe single-flight gate preventing concurrent or replayed write dispatches."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._consumed: set[str] = set()

    @contextmanager
    def single_flight(self, key: str, *, consume_on_enter: bool = False):
        if not key:
            yield
            return
        with self._lock:
            if key in self._inflight or (consume_on_enter and key in self._consumed):
                raise BusinessMutationBlockedError(
                    "business mutation proposal is already dispatching or consumed",
                    reason="already_consumed",
                    state="dispatching",
                )
            self._inflight.add(key)
            if consume_on_enter:
                self._consumed.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._inflight.discard(key)


@dataclass(frozen=True, slots=True)
class PipelineOutcome:
    """Unified outcome from executing one proposal through ``WritePipeline``."""

    state: str
    proposal: BusinessMutationProposal | None = None
    result: BusinessToolResult | None = None
    reason: str = ""
    error: Exception | None = None
    asked: bool = False
    prefill_offer: MutationPrefillOffer | None = None
    prefill_context: Any = None
    approval: Any = None


class WritePipeline:
    """Shared single-flight write safety pipeline across service and submit routes."""

    def __init__(self, inflight: InflightWriteTracker | None = None) -> None:
        self.inflight = inflight or InflightWriteTracker()

    def run_service_write(
        self,
        *,
        name: str,
        approvals,
        step: int,
        context,
        prepare_fn: Callable[[], BusinessMutationProposal],
        approve_fn: Callable[[BusinessMutationProposal, bool], Any],
        dispatch_fn: Callable[[BusinessMutationProposal, Any], BusinessToolResult],
        expected: RequestedEffect | None = None,
        prefill_port: MutationPrefillPort | None = None,
        prefill_observation=None,
        check_proposal: Callable[[BusinessMutationProposal], None] | None = None,
        acquire_single_flight: bool = True,
    ) -> PipelineOutcome:
        """Execute stages (a)-(f) for Skill/typed/convention write proposals."""
        ledger = approvals.ledger
        identity = context.audit_projection() if context is not None else {}
        proposal, asked, prefill_offer = None, False, None
        try:
            # Stage (a): proposal preparation + detail/effect/value-source validation
            proposal = prepare_fn()
            normalize_approval_details(proposal.details)
            verify_effect_projection(proposal, expected, ledger=ledger, step=step, tool=name)
            if check_proposal is not None:
                check_proposal(proposal)
            if prefill_port is not None:
                if not isinstance(prefill_port, MutationPrefillPort):
                    raise BusinessToolError("mutation prefill port is invalid")
                if context is not None and prefill_observation is not None:
                    prefill_offer = prefill_port.offer(
                        proposal,
                        context=context,
                        observation=prefill_observation,
                    )
                if prefill_offer is not None:
                    if not isinstance(prefill_offer, MutationPrefillOffer):
                        raise BusinessToolError("mutation prefill offer is invalid")
                    prefill_offer.require(proposal, context)
            # Stage (b): fresh single-use approval card request & consumption
            asked = True
            approval = approve_fn(proposal, prefill_offer is not None)
            if approval.prefill_selected:
                if prefill_offer is None:
                    raise AssertionError("unoffered prefill decision passed approval validation")
                return PipelineOutcome(
                    "prefill_selected",
                    proposal=proposal,
                    reason="prefill_selected",
                    asked=True,
                    prefill_offer=prefill_offer,
                    prefill_context=context,
                    approval=approval,
                )
            if not approval.approved:
                return PipelineOutcome(
                    "not_approved",
                    proposal=proposal,
                    reason=approval.reason,
                    asked=True,
                    approval=approval,
                )
            # Stage (c)-(e): single-flight journaled dispatch + reread + readback inside runtime/handler
            flight_cm = (
                self.inflight.single_flight(proposal.content_digest, consume_on_enter=True)
                if acquire_single_flight
                else nullcontext()
            )
            with flight_cm:
                result = dispatch_fn(proposal, approval)
        except BusinessMutationIndeterminate as exc:
            # Stage (f): indeterminate accounting, never retried
            approvals.mutation_indeterminate()
            ledger.append(
                "business_mutation_indeterminate",
                step=step,
                tool=name,
                proposal_digest=(proposal.content_digest if proposal is not None else ""),
                reconciliation=exc.reconciliation,
                side_effect_possible=True,
            )
            return PipelineOutcome(
                "indeterminate",
                proposal=proposal,
                reason="indeterminate_write",
                error=exc,
                asked=asked,
            )
        except BusinessToolPermissionError as exc:
            return PipelineOutcome(
                "permission_denied",
                proposal=proposal,
                reason="permission_denied",
                error=exc,
                asked=asked,
            )
        except BusinessToolReferenceError as exc:
            return PipelineOutcome(
                "reference",
                proposal=proposal,
                reason="agent_refused",
                error=exc,
                asked=asked,
            )
        except (BusinessToolError, ValueError) as exc:
            reason = (
                "business_mutation_blocked"
                if isinstance(exc, BusinessMutationBlockedError)
                else "business_mutation_refused"
                if isinstance(exc, BusinessMutationRefusedError)
                else "agent_refused"
            )
            return PipelineOutcome(
                "refused",
                proposal=proposal,
                reason=reason,
                error=exc,
                asked=asked,
            )

        ledger.append(
            "business_mutation_result",
            step=step,
            tool=name,
            proposal_digest=proposal.content_digest,
            result_digest=result.content_digest,
            audit=result.audit,
            completed=True,
            side_effect_possible=True,
            identity=identity,
        )
        return PipelineOutcome(
            "completed",
            proposal=proposal,
            result=result,
            asked=True,
            approval=approval,
        )

    def dispatch_with_evidence(
        self,
        *,
        proposal: BusinessMutationProposal,
        approval,
        approvals,
        step: int,
        context,
        identity: dict,
        execute_fn: Callable[[Callable[[], None]], BusinessToolResult],
    ) -> BusinessToolResult:
        """Run one journaled dispatch under single-flight tracking and transport evidence."""
        with self.inflight.single_flight(proposal.content_digest, consume_on_enter=True):
            with mutation_evidence(proposal, approval, context) as evidence:
                def claimed() -> None:
                    approvals.mutation_claimed(
                        proposal,
                        step=step,
                        tool=proposal.tool,
                        approval_id=approval.approval_id,
                        identity=identity,
                    )
                    if evidence:
                        evidence.claimed()

                result = execute_fn(claimed)
            approvals.mutation_completed()
            return result

    def run_submit_pipeline(
        self,
        *,
        proposal: BusinessMutationProposal,
        approvals,
        step: int,
        context,
        identity: dict,
        on_not_approved: Callable[[Any], Any],
        pre_dispatch_reread: Callable[[], Any],
        on_reread_drift: Callable[[Exception], Any],
        dispatch_once: Callable[[Any, Any], tuple[bool, Any]],
        settle_and_readback: Callable[[Any, bool, Any], Any],
    ):
        """Execute stages (a)-(f) for the native form/row submit route."""
        # Stage (a): validate structured details
        normalize_approval_details(proposal.details)
        # Stage (b): fresh single-use approval card request
        approval = approvals.approve_mutation(
            proposal,
            step=step,
            tool=proposal.tool,
            context=context,
            identity=identity,
        )
        if not approval.approved:
            return on_not_approved(approval)

        with self.inflight.single_flight(proposal.content_digest, consume_on_enter=True):
            # Stage (c): pre-dispatch reread drift comparison hook
            try:
                reread_state = pre_dispatch_reread()
            except Exception as exc:
                return on_reread_drift(exc)
            # Stage (d): single-flight armed dispatch, zero automatic retry
            uncertain, early_outcome = dispatch_once(approval, reread_state)
            # Stage (e)-(f): same-actor readback & indeterminate accounting
            return settle_and_readback(approval, uncertain, early_outcome)


_DEFAULT_HANDLER_INFLIGHT = InflightWriteTracker()


def execute_verified_handler_write(
    *,
    verify_actor: Callable[[], Any],
    pre_dispatch_reread: Callable[[Any], Any],
    dispatch_once: Callable[[Any, Any], Any],
    verify_readback: Callable[[Any, Any, Any, bool], BusinessToolResult],
    invalidate_reads: Callable[[], None] | None = None,
    allow_readback_recovery: bool = False,
    use_verification_interval: bool = False,
    readback_failure_message: str = "write acknowledged; same-actor readback failed",
    readback_reconciliation: Callable[[Any, Any], dict] | None = None,
    inflight_key: str = "",
    inflight: InflightWriteTracker | None = None,
) -> BusinessToolResult:
    """Shared handler dispatch pipeline (actor -> reread -> single-flight dispatch -> readback).

    Routes 1, 3, and 4 delegate their dispatch execution here so pre-dispatch drift
    refusal, single-flight no-retry dispatch, same-actor readback verification, and
    indeterminate escalation stay uniform across every application adapter.
    """
    tracker = inflight or _DEFAULT_HANDLER_INFLIGHT
    with tracker.single_flight(inflight_key):
        actor_state = verify_actor()
        reread_state = pre_dispatch_reread(actor_state)
        uncertain_dispatch = False
        try:
            dispatch_result = dispatch_once(actor_state, reread_state)
        except BusinessMutationIndeterminate:
            if not allow_readback_recovery:
                raise
            dispatch_result = None
            uncertain_dispatch = True

        interval_cm = nullcontext()
        if use_verification_interval:
            from ..execution.events import turn_interval

            interval_cm = turn_interval("verification")

        with interval_cm:
            try:
                result = verify_readback(actor_state, reread_state, dispatch_result, uncertain_dispatch)
            except BusinessMutationIndeterminate:
                raise
            except Exception as exc:
                reconcile = (
                    readback_reconciliation(reread_state, dispatch_result)
                    if readback_reconciliation is not None
                    else {}
                )
                raise BusinessMutationIndeterminate(
                    readback_failure_message,
                    phase="postcondition",
                    reconciliation=reconcile,
                ) from exc

        if invalidate_reads is not None:
            invalidate_reads()
        if uncertain_dispatch:
            verified_after_readback()
        return result


__all__ = [
    "DEFAULT_SECRET_FIELD_PATTERN",
    "FieldDiff",
    "InflightWriteTracker",
    "PipelineOutcome",
    "ProposalDiff",
    "WritePipeline",
    "body_detail",
    "change_detail",
    "check_append_preservation",
    "create_detail",
    "derived_detail",
    "execute_verified_handler_write",
    "non_volatile_snapshot",
    "normalize_approval_details",
    "preserved_detail",
    "require_append_preservation",
    "restricted_snapshot_digests",
    "side_effect_detail",
    "target_rule_detail",
    "verify_effect_projection",
    "verify_entry_provenance",
    "verify_preservation_snapshot",
    "verify_preserves_unmentioned",
    "verify_value_sources",
]
